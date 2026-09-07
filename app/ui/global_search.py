"""Поиск везде — одно окно вместо трёх разделов.

Открывается по Ctrl+K. Запрос уходит сразу во все источники: VK, YouTube и
библиотеку на диске. Каждый источник ищет в своём фоновом потоке и показывается,
как только ответил, — самый быстрый не ждёт самого медленного.

Два обязательных условия. Первое: ответ устаревшего запроса не должен затирать
новый — за этим следит номер поколения (`_gen`). Второе: окно не должно
блокироваться, поэтому в UI-потоке здесь не происходит ни одного сетевого вызова.

Библиотека читается с диска, поэтому список файлов держится в коротком кэше:
иначе каждая буква запроса заново обходила бы папки."""
from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout,
)

from ..core import library
from ..core.async_task import run_async
from ..core.track import from_local, from_vk
from .flow_layout import FlowRow
from .track_list import SelectionBar, TrackListWidget
from .widgets import ElidedLabel

logger = logging.getLogger(__name__)

DEBOUNCE_MS = 350
LIMIT = 20
LIBRARY_TTL = 60          # секунд: дольше держать список файлов смысла нет

TAB_ALL, TAB_VK, TAB_YOUTUBE, TAB_LIBRARY = 'all', 'vk', 'youtube', 'library'
TABS = ((TAB_ALL, 'Все'), (TAB_VK, 'VK'), (TAB_YOUTUBE, 'YouTube'),
        (TAB_LIBRARY, 'Библиотека'))

# Порядок показа: своё и уже скачанное выше, чужой каталог ниже
SOURCE_ORDER = (TAB_LIBRARY, TAB_VK, TAB_YOUTUBE)
SOURCE_TITLES = {TAB_VK: 'VK', TAB_YOUTUBE: 'YouTube', TAB_LIBRARY: 'Библиотека'}


class GlobalSearchDialog(QDialog):
    """Одно поле поиска на все источники сразу."""

    open_page_requested = Signal(str)   # уйти в раздел: vk / youtube / library

    def __init__(self, discovery, vk_provider, store, settings_provider, parent=None):
        super().__init__(parent)
        self._discovery = discovery
        self._vk = vk_provider
        self._store = store
        self._settings = settings_provider

        self._tab = TAB_ALL
        self._query = ''
        self._gen = 0
        self._pending = 0
        self._results: dict[str, list] = {}
        self._errors: dict[str, str] = {}
        self._library_cache: tuple[float, list] | None = None

        self.setWindowTitle('Поиск везде')
        self.setMinimumSize(560, 420)
        self.resize(760, 560)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start)

        self._build_ui()

    def _build_ui(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 14, 14, 14)
        box.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._input = QLineEdit()
        self._input.setPlaceholderText('Поиск везде…')
        self._input.setClearButtonEnabled(True)
        self._input.textChanged.connect(self._on_text)
        self._input.returnPressed.connect(self._start)
        row.addWidget(self._input, 1)
        box.addLayout(row)

        tabs = FlowRow(spacing=6)
        self._tabs = {}
        for name, title in TABS:
            button = QPushButton(title)
            button.setObjectName('tab')
            button.setCheckable(True)
            button.setChecked(name == self._tab)
            button.clicked.connect(lambda _c=False, key=name: self.set_tab(key))
            tabs.add(button)
            self._tabs[name] = button
        tabs.add_stretch()
        box.addWidget(tabs)

        self.list = TrackListWidget(self)
        # Включили трек — окно поиска больше не нужно
        self.list.play_requested.connect(lambda *_a: self.accept())
        box.addWidget(SelectionBar(self.list))
        box.addWidget(self.list, 1)

        self._status = ElidedLabel('Начните печатать, поищу в VK, на YouTube и в библиотеке')
        self._status.setObjectName('hint')
        box.addWidget(self._status)

    # ---------- внешнее управление ----------
    def focus_search(self, text: str = '') -> None:
        if text:
            self._input.setText(text)
        self._input.setFocus()
        self._input.selectAll()
        if self._input.text().strip():
            self._query = ''       # тот же запрос снаружи — искать заново
            self._start()

    def set_tab(self, name: str) -> None:
        if name not in self._tabs:
            return
        self._tab = name
        for key, button in self._tabs.items():
            button.setChecked(key == name)
        if self._input.text().strip():
            self._query = ''
            self._start()
        else:
            self._show()

    # ---------- поиск ----------
    def _on_text(self, text: str) -> None:
        if text.strip():
            self._debounce.start()
        else:
            self._debounce.stop()
            self._query = ''
            self._gen += 1        # обрываем то, что ещё летит из сети
            self._results.clear()
            self._errors.clear()
            self._show()

    def _start(self) -> None:
        self._debounce.stop()
        query = self._input.text().strip()
        if not query or query == self._query:
            return
        self._query = query
        self._gen += 1
        gen = self._gen
        self._results.clear()
        self._errors.clear()

        sources = [self._tab] if self._tab != TAB_ALL else list(SOURCE_ORDER)
        self._pending = len(sources)
        self._status.setText('Ищу…')
        for source in sources:
            self._run(source, query, gen)

    def _run(self, source: str, query: str, gen: int) -> None:
        finders = {TAB_VK: self._find_vk,
                   TAB_YOUTUBE: self._find_youtube,
                   TAB_LIBRARY: self._find_library}
        finder = finders.get(source)
        if finder is None:
            self._pending -= 1
            return

        def on_done(result, error):
            if gen != self._gen:
                return            # запрос уже сменился, ответ не нужен
            self._pending = max(0, self._pending - 1)
            if error:
                logger.info('Поиск везде: %s: %s', source, error)
                self._errors[source] = str(error).splitlines()[0]
            else:
                self._results[source] = result or []
            self._show()

        run_async(finder, on_done, query)

    def _find_vk(self, query: str) -> list:
        client = self._vk() if callable(self._vk) else self._vk
        if client is None:
            return []
        rows = client.search_tracks(query, LIMIT)
        # search_tracks у разных версий отдаёт то готовые треки, то строки VK
        return [row if not isinstance(row, dict) else from_vk(row) for row in rows][:LIMIT]

    def _find_youtube(self, query: str) -> list:
        if self._discovery is None:
            return []
        return self._discovery.search(query, LIMIT, music_only=True)[:LIMIT]

    def _find_library(self, query: str) -> list:
        needle = query.lower()
        files = self._library_files()
        found = [media for media in files if needle in media.name.lower()]
        return [from_local(media.path, media.name) for media in found[:LIMIT]]

    def _library_files(self) -> list:
        """Список файлов с коротким кэшем: обход папок на каждую букву — дорого."""
        now = time.monotonic()
        if self._library_cache and now - self._library_cache[0] < LIBRARY_TTL:
            return self._library_cache[1]
        settings = self._settings() if callable(self._settings) else self._settings
        dirs = [settings.get('music_dir'), settings.get('video_dir')]
        dirs = [d for d in dirs if d and os.path.isdir(d)]
        files = library.scan(dirs) if dirs else []
        self._library_cache = (now, files)
        return files

    # ---------- показ ----------
    def _show(self) -> None:
        sources = [self._tab] if self._tab != TAB_ALL else list(SOURCE_ORDER)
        tracks: list = []
        seen: set[str] = set()
        parts: list[str] = []
        for source in sources:
            found = self._results.get(source)
            if found:
                for track in found:
                    if track is None or track.uid in seen:
                        continue
                    seen.add(track.uid)
                    tracks.append(track)
                parts.append(f'{SOURCE_TITLES[source]}: {len(found)}')
            elif source in self._errors:
                parts.append(f'{SOURCE_TITLES[source]}: не ответил')
            elif found is not None:
                parts.append(f'{SOURCE_TITLES[source]}: 0')
        self.list.set_tracks(tracks)

        if not self._query:
            self._status.setText('Начните печатать, поищу в VK, на YouTube и в библиотеке')
        elif self._pending:
            self._status.setText('Ищу… ' + ' · '.join(parts) if parts else 'Ищу…')
        elif tracks:
            self._status.setText(' · '.join(parts))
        else:
            self._status.setText('Ничего не нашлось' + (' · ' + ' · '.join(parts) if parts else ''))

    # ---------- клавиши ----------
    def keyPressEvent(self, event) -> None:
        # Esc закрывает окно, но сначала — очищает непустой запрос
        if event.key() == Qt.Key_Escape and self._input.text():
            self._input.clear()
            return
        super().keyPressEvent(event)


__all__ = ['GlobalSearchDialog']
