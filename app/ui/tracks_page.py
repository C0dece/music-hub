"""«Треки» - фонотека из всех источников сразу.

Одна из главных мыслей раздела «Моя музыка»: человеку неважно, откуда взялась
песня. Здесь в одном списке лежат аудиозаписи VK, ролики YouTube и файлы с
диска - с одинаковыми обложками, одинаковым меню и одинаковым двойным щелчком.
Отличие видно только по метке источника и по галочке «офлайн».

Список берётся из базы (`store.saved_tracks`), а не из папок: папки - это способ
добавить свои файлы, а не сама фонотека. Иначе трек VK и трек с диска жили бы по
разным правилам, и раздел развалился бы на два."""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from ..core import library
from ..core import offline as offline_core
from ..core.async_task import run_async
from ..core.track import AUDIO_EXTS, SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE
from .flow_layout import FlowRow
from .track_list import SelectionBar, TrackListWidget
from .widgets import ElidedLabel, EmptyState

logger = logging.getLogger(__name__)

SOURCES = (('Все источники', ''), ('VK', SOURCE_VK), ('YouTube', SOURCE_YOUTUBE),
           ('Файлы', SOURCE_LOCAL))

# Три вида одной страницы. Списки разные, а поиск, меню строки и двойной щелчок
# одни и те же, поэтому это режимы, а не три отдельных класса.
PRESET_ALL = 'all'          # вся фонотека: «Треки»
PRESET_LOCAL = 'local'      # только файлы с диска: «С компьютера»
PRESET_CACHE = 'cache'      # офлайн-копии: «Кэш»

# Фильтр диалога выбора файлов собираем из тех же расширений, что понимает плеер
_AUDIO_FILTER = 'Музыка (' + ' '.join(f'*{ext}' for ext in sorted(AUDIO_EXTS)) + ')'


class TracksPage(QWidget):
    """Фонотека: поиск, фильтр по источнику, свои файлы и офлайн-копии."""

    status_message = Signal(str)
    local_dirs_changed = Signal(object)   # новый список папок со своей музыкой
    play_requested = Signal(object, int)  # проксируется дальше - как у других разделов

    def __init__(self, store, settings_provider, offline=None, parent=None,
                 preset: str = PRESET_ALL):
        super().__init__(parent)
        self._store = store
        self._settings = settings_provider
        self._offline = offline
        self._preset = preset
        self._all: list = []
        self._busy = False

        self._build_ui()
        if offline is not None:
            offline.changed.connect(self._on_offline_changed)
            offline.failed.connect(self._on_offline_failed)

    def _build_ui(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        # overflow: фильтры и до пяти кнопок в узком окне занимали три строки,
        # и списку оставалось полторы строки. Лишние уезжают под «⋯»
        row = FlowRow(spacing=8, overflow=True)
        self._search = QLineEdit()
        self._search.setPlaceholderText(
            {PRESET_LOCAL: 'Поиск по своим файлам…',
             PRESET_CACHE: 'Поиск по офлайн-копиям…'}.get(self._preset,
                                                          'Поиск по фонотеке…'))
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(200)
        self._search.textChanged.connect(lambda _text: self._apply_filter())
        row.add(self._search)

        self._source = QComboBox()
        for title, key in SOURCES:
            self._source.addItem(title, key)
        self._source.currentIndexChanged.connect(lambda _index: self._apply_filter())
        row.add(self._source)

        self._only_offline = QCheckBox('Только офлайн')
        self._only_offline.setToolTip('Показать то, что играет без интернета')
        self._only_offline.toggled.connect(lambda _on: self._apply_filter())
        row.add(self._only_offline)

        # В «С компьютера» и «Кэше» источник задан самим разделом: переключатель
        # там только вводил бы в заблуждение - выбрать нечего
        if self._preset != PRESET_ALL:
            self._source.hide()
            self._only_offline.hide()

        row.add_stretch()
        buttons = [('Добавить файлы…', self._add_files),
                   ('Добавить папку…', self._add_folder),
                   ('Обновить', self.reload)]
        if self._preset == PRESET_ALL:
            # Отдельного раздела под кэш больше нет, а стереть его целиком нужно
            # где-то уметь. Место рядом с фильтром «Только офлайн»: там же, где
            # кэш и видно
            buttons.insert(0, ('Очистить кэш', self._clear_cache))
        elif self._preset == PRESET_CACHE:
            # Кэш не пополняют вручную: сюда попадает то, что сохранил плеер
            buttons = [('Очистить кэш', self._clear_cache), ('Обновить', self.reload)]
        for text, slot in buttons:
            button = QPushButton(text)
            button.setObjectName('secondary')
            button.clicked.connect(slot)
            row.add(button)
        box.addWidget(row)

        self._summary = ElidedLabel('')
        self._summary.setObjectName('hint')
        box.addWidget(self._summary)

        self._list = TrackListWidget()
        self._list.play_requested.connect(self.play_requested)
        self._selection_bar = SelectionBar(self._list)
        box.addWidget(self._selection_bar)
        box.addWidget(self._list, 1)

        # Два разных «пусто»: фонотеки ещё нет - и фильтр ничего не нашёл.
        # Совет в каждом случае свой, поэтому текст меняем на месте
        title, text = self._empty_texts()
        self._empty = EmptyState('audio', title, text)
        if self._preset != PRESET_CACHE:
            self._empty.add_action('Добавить файлы…', self._add_files)
        self._empty.hide()
        box.addWidget(self._empty, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Сводка «столько-то треков» - справка, а не функционал: в низком окне
        # она отнимает строку у самого списка. Полосу выбора не трогаем, через
        # неё снимают отметки
        self._summary.setVisible(self.height() >= 220)

    def _empty_texts(self) -> tuple[str, str]:
        """Каждому разделу свой совет: «добавьте файлы» в кэше бессмысленно."""
        if self._preset == PRESET_LOCAL:
            return ('Своей музыки пока нет',
                    'Добавьте файлы или целую папку с диска, они станут '
                    'полноценной частью фонотеки и попадут в миксы.')
        if self._preset == PRESET_CACHE:
            return ('Офлайн-копий пока нет',
                    'Сохраните треки офлайн из любого списка, они будут играть '
                    'без интернета и появятся здесь.')
        return ('В фонотеке пусто',
                'Добавьте файлы с диска или сохраните треки из VK и YouTube, '
                'они соберутся здесь в один список.')

    # ---------- то, что нужно главному окну ----------
    @property
    def list(self) -> TrackListWidget:
        return self._list

    @property
    def lists(self) -> list:
        return [self._list]

    def set_current(self, track) -> None:
        self._list.set_current(track)

    # ---------- содержимое ----------
    def reload(self) -> None:
        if self._preset == PRESET_CACHE:
            # Кэш - это отдельная выборка базы, а не подмножество фонотеки:
            # офлайн-копию можно сделать и у трека, который в неё не сохранён
            self._all = self._store.cached_tracks()
        elif self._preset == PRESET_LOCAL:
            self._all = [t for t in self._store.saved_tracks()
                         if t.source == SOURCE_LOCAL]
        else:
            self._all = self._store.saved_tracks()
        self._apply_filter()
        self._update_summary()

    def _apply_filter(self) -> None:
        query = self._search.text().strip().lower()
        source = self._source.currentData() or ''
        tracks = self._all
        if source:
            tracks = [t for t in tracks if t.source == source]
        if query:
            tracks = [t for t in tracks
                      if query in t.title.lower() or query in t.artist.lower()]
        if self._only_offline.isChecked():
            tracks = [t for t in tracks if t.cached]
        self._list.set_tracks(tracks)
        self._update_empty(bool(tracks))
        self._update_badges()

    def _update_empty(self, has_rows: bool) -> None:
        """Пустой список подменяем подсказкой - их видно сразу, без вчитывания."""
        self._list.setVisible(has_rows)
        self._empty.setVisible(not has_rows)
        if self._all:
            self._empty.set_title('Ничего не нашлось')
            self._empty.set_text('Под фильтр не попал ни один трек. '
                                 'Смените источник или очистите поиск.')
        else:
            title, text = self._empty_texts()
            self._empty.set_title(title)
            self._empty.set_text(text)

    def _update_badges(self) -> None:
        """Метки строк: офлайн-копия, идущая загрузка и потерянный файл."""
        pending = self._offline.pending_uids() if self._offline is not None else set()
        badges = {}
        for track in self._list.tracks():
            if track.uid in pending:
                badges[track.uid] = 'сохраняю…'
            elif track.cached:
                badges[track.uid] = '✓ офлайн'
            elif track.source == SOURCE_LOCAL or self._preset == PRESET_CACHE:
                # Файл переименовали или унесли - честно говорим об этом, а не
                # выкидываем трек из списка: он есть в плейлистах и ещё вернётся.
                # В «Кэше» это тем более важно: запись есть, а играть нечего
                badges[track.uid] = 'файла нет'
        self._list.set_badges(badges)

    def _on_offline_changed(self, uid: str) -> None:
        track = self._store.get_track(uid)
        if track is not None:
            # Путь к файлу мог появиться или пропасть - обновляем и сам трек
            self._all = [track if item.uid == uid else item for item in self._all]
        self._apply_filter()
        self._update_summary()

    def _on_offline_failed(self, _uid: str, text: str) -> None:
        self.status_message.emit(f'Офлайн: {text}' if text
                                 else 'Не удалось сохранить офлайн')

    def _update_summary(self) -> None:
        total = len(self._all)
        cached = sum(1 for track in self._all if track.cached)
        if self._preset == PRESET_CACHE:
            # Здесь важно другое число: сколько записей числятся копиями, но
            # файла на диске уже нет - их видно строкой, а не догадкой
            lost = total - cached
            base = f'Офлайн-копий: {total}'
            if lost:
                base += f' · файлов нет: {lost}'
            self._summary.setText(base if total else '')
            if total and self._offline is not None:
                self._append_folder_size(base)
            return
        if not total:
            # Про пустую фонотеку уже написано посреди страницы - второй раз
            # повторять то же самое строкой сверху незачем
            self._summary.setText('')
            return
        base = f'Треков: {total} · офлайн: {cached}'
        self._summary.setText(base)
        if self._offline is not None:
            self._append_folder_size(base)

    def _append_folder_size(self, base: str) -> None:
        """Размер папки считаем в фоне: это обход диска, а не арифметика."""
        def done(size, error):
            if error or size is None:
                return
            limit = float(self._settings().get('offline_limit_gb') or 0)
            text = f'{base}, {library.format_size(size)}'
            if limit > 0:
                text += f' из {limit:g} ГБ'
            self._summary.setText(text)

        run_async(offline_core.folder_size, done, self._offline.directory())

    # ---------- кэш ----------
    def _clear_cache(self) -> None:
        """Удаляет офлайн-копии. Сами треки остаются: уходят только файлы."""
        if self._offline is None or not self._all:
            return
        # В «Треках» список это вся фонотека, поэтому берём только те строки,
        # у которых копия действительно есть
        uids = [track.uid for track in self._all if track.cached]
        if not uids:
            self.status_message.emit('Офлайн-копий нет, удалять нечего')
            return
        answer = QMessageBox.question(
            self, 'Очистить кэш',
            f'Удалить офлайн-копии ({len(uids)} шт.)? Треки останутся в фонотеке, '
            'но играть без интернета перестанут.')
        if answer != QMessageBox.Yes:
            return
        for uid in uids:
            self._offline.remove(uid)
        self.reload()
        self.status_message.emit(f'Офлайн-копии удалены: {len(uids)}')

    # ---------- свои файлы ----------
    def _add_files(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(
            self, 'Добавить музыку', self._settings().get('music_dir', ''),
            f'{_AUDIO_FILTER};;Все файлы (*)')
        if paths:
            self._start(lambda: [library.audio_track(path) for path in paths])

    def _add_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, 'Папка со своей музыкой', self._settings().get('music_dir', ''))
        if not directory:
            return
        dirs = list(self._settings().get('local_dirs') or [])
        if not any(os.path.normcase(d) == os.path.normcase(directory) for d in dirs):
            dirs.append(directory)
            # Папку запоминаем: «Обновить» потом подхватит из неё новые файлы сам
            self.local_dirs_changed.emit(dirs)
        self._start(lambda: library.scan_audio_tracks([directory]))

    def refresh_local_dirs(self) -> None:
        """Перечитать папки со своей музыкой - вдруг в них добавилось новое."""
        dirs = list(self._settings().get('local_dirs') or [])
        if dirs:
            self._start(lambda: library.scan_audio_tracks(dirs))

    def _start(self, work) -> None:
        """Чтение тегов - это диск и сотни файлов, поэтому всегда в фоне."""
        if self._busy:
            self.status_message.emit('Добавление уже идёт, подождите')
            return
        self._busy = True
        self.status_message.emit('Читаю файлы…')
        run_async(work, self._on_imported)

    def _on_imported(self, tracks, error) -> None:
        self._busy = False
        if error:
            logger.error('tracks: не удалось добавить файлы: %s', error)
            self.status_message.emit(f'Не удалось добавить: {error}')
            return
        tracks = tracks or []
        known = self._store.saved_uids([track.uid for track in tracks])
        self._store.save_many_to_library(tracks)
        self.reload()
        added = len(tracks) - len(known)
        if added:
            self.status_message.emit(f'Добавлено треков: {added}')
        elif tracks:
            self.status_message.emit('Всё это уже есть в фонотеке')
        else:
            self.status_message.emit('Музыки в этой папке не нашлось')
