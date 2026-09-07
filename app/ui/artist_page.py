"""Лёгкая страница исполнителя.

Полноценной карточки артиста у нас быть не может: ни YouTube, ни VK не отдают
через доступные пути ни биографию, ни выверенную дискографию. Поэтому страница
честно показывает две вещи: что об исполнителе уже знает местная база и что
находится по его имени в YouTube. Из этого можно сразу играть, ставить в очередь
и запускать радио.

Окно отдельное и не модальное: музыка играет дальше, а список открывается поверх
главного окна из меню трека."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..core.async_task import run_async
from ..core.matcher import normalized_key
from ..core.track import Track
from .flow_layout import FlowRow
from .track_list import SelectionBar, TrackListWidget
from .widgets import ElidedLabel

logger = logging.getLogger(__name__)

FOUND_LIMIT = 30       # больше на одной странице всё равно не разглядеть


class ArtistPage(QWidget):
    """Треки исполнителя: сначала свои, потом найденные в YouTube."""

    radio_requested = Signal(str)          # имя исполнителя
    hide_artist_requested = Signal(str)    # «не рекомендовать»

    def __init__(self, artist: str, store=None, discovery=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.setObjectName('artistPage')
        self._artist = ''
        self._store = store
        self._discovery = discovery
        self._gen = 0
        self.resize(720, 560)

        box = QVBoxLayout(self)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)

        self._caption = QLabel('Исполнитель')
        self._caption.setObjectName('h2')
        box.addWidget(self._caption)

        self._hint = ElidedLabel('')
        self._hint.setObjectName('hint')
        box.addWidget(self._hint)

        self.list = TrackListWidget(self)
        box.addWidget(SelectionBar(self.list))
        box.addWidget(self.list, 1)

        row = FlowRow(spacing=8)
        row.add_stretch()
        for text, slot, secondary in (('Играть', self._play, False),
                                      ('В очередь', self._enqueue, True),
                                      ('Радио', self._radio, True),
                                      ('Не рекомендовать', self._hide, True)):
            button = QPushButton(text)
            if secondary:
                button.setObjectName('secondary')
            button.clicked.connect(slot)
            row.add(button)
        box.addWidget(row)

        self.set_artist(artist)

    # ---------- наполнение ----------
    def set_artist(self, artist: str) -> None:
        """Показать другого исполнителя в этом же окне.

        Окно одно: плодить по окну на каждое имя незачем, а старый ответ из
        сети отсекает счётчик поколений в reload()."""
        self._artist = (artist or '').strip()
        self.setWindowTitle(f'Исполнитель: {self._artist}' if self._artist
                            else 'Исполнитель')
        self._caption.setText(self._artist or 'Исполнитель')
        self.reload()

    def reload(self) -> None:
        """Своё показываем сразу, найденное в сети — как придёт."""
        known = self._store.artist_tracks(self._artist) if self._store else []
        self.list.set_tracks(known)
        self._hint.setText('У вас: {} · ищу в YouTube…'.format(len(known))
                           if self._discovery is not None
                           else 'У вас: {}'.format(len(known)))
        if self._discovery is None or not self._artist:
            return

        self._gen += 1
        gen = self._gen
        discovery = self._discovery
        artist = self._artist

        def on_done(found, error):
            if gen != self._gen:
                return              # окно успели перезагрузить
            if error:
                logger.info('Исполнитель «%s»: поиск не удался: %s', artist, error)
                self._hint.setText('У вас: {} · YouTube сейчас недоступен'
                                   .format(len(known)))
                return
            merged = self._merge(known, found or [])
            self.list.set_tracks(merged)
            self._hint.setText('У вас: {} · найдено в YouTube: {}'
                               .format(len(known), len(merged) - len(known)))

        run_async(lambda: discovery.search(artist, FOUND_LIMIT), on_done)

    @staticmethod
    def _merge(known: list[Track], found: list[Track]) -> list[Track]:
        """Добавить найденное к своему, не повторяя одно и то же дважды."""
        seen = {track.uid for track in known}
        keys = {normalized_key(track.artist, track.title) for track in known}
        result = list(known)
        for track in found:
            key = normalized_key(track.artist, track.title)
            if track.uid in seen or key in keys:
                continue
            seen.add(track.uid)
            keys.add(key)
            result.append(track)
        return result

    # ---------- действия ----------
    def _chosen(self) -> list[Track]:
        return self.list.selected_tracks() or self.list.tracks()

    def _play(self) -> None:
        tracks = self._chosen()
        if tracks:
            self.list.play_requested.emit(tracks, 0)

    def _enqueue(self) -> None:
        tracks = self._chosen()
        if tracks:
            self.list.enqueue_requested.emit(tracks, False)

    def _radio(self) -> None:
        if self._artist:
            self.radio_requested.emit(self._artist)

    def _hide(self) -> None:
        if self._artist:
            self.hide_artist_requested.emit(self._artist)
            self.close()
