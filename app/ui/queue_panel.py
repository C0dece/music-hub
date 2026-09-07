"""Панель очереди сбоку от основного содержимого.

Раньше очередь жила в отдельном окне. Для музыкального клиента это неудобно:
очередь смотрят постоянно — что играет, что дальше, куда переставить трек, — и
ради этого не должно всплывать второе окно поверх первого.

Панель узкая и убирается кнопкой; в тесном окне главное окно прячет её само,
чтобы список треков не превратился в полоску.

Перетаскивание сделано вручную: Qt при внутреннем переносе пересоздаёт строки из
буфера обмена, а в строках лежат объекты Track, которые туда не укладываются.
Поэтому перенос мы только перехватываем, а переставляет треки очередь плеера —
после чего панель перерисовывается из неё же и никогда не расходится с правдой."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from ..core.player_controller import PlayerController
from ..core.track import Track
from . import player_icons
from .track_list import SelectionBar, TrackListWidget
from .widgets import EmptyState

PANEL_WIDTH = 320


class _QueueList(TrackListWidget):
    """Список очереди: то же оформление строк плюс перетаскивание."""

    reorder_requested = Signal(int, int)   # откуда, куда

    def __init__(self, parent=None):
        super().__init__(parent, allow_download=False, allow_remove=True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self._base_selection_mode = QAbstractItemView.SingleSelection
        # Здесь долгое зажатие уже значит другое: строку тянут на новое место.
        # Выбор включается только кнопкой «Выбрать» над списком
        self._hold_enabled = False

    def set_selection_mode(self, on: bool) -> None:
        super().set_selection_mode(on)
        # Пока отмечают строки, тянуть их нельзя: одно движение мыши на два действия
        self.setDragDropMode(QAbstractItemView.NoDragDrop if on
                             else QAbstractItemView.InternalMove)

    def dropEvent(self, event) -> None:
        source = self.currentRow()
        point = event.position().toPoint()
        index = self.indexAt(point)
        if index.isValid():
            at = index.row()
            if self.dropIndicatorPosition() != QAbstractItemView.AboveItem:
                at += 1
        else:
            at = self.count()
        # `at` — место вставки до удаления строки, очередь же ждёт итоговый номер
        target = at - 1 if source < at else at
        # Свой перенос Qt не делаем: список перерисуется из очереди
        event.ignore()
        if source >= 0 and target != source:
            self.reorder_requested.emit(source, target)


class QueuePanel(QFrame):
    """Что играет сейчас и что будет дальше."""

    add_vk_requested = Signal(object)        # список треков
    favorite_requested = Signal(object)      # список треков
    radio_requested = Signal(object)         # один трек
    open_source_requested = Signal(object)   # один трек
    hide_requested = Signal(object)          # список треков
    hide_artist_requested = Signal(str)
    artist_requested = Signal(str)
    playlist_requested = Signal(object, int)  # треки, id плейлиста
    close_requested = Signal()

    def __init__(self, player: PlayerController, parent=None):
        super().__init__(parent)
        self.setObjectName('queuePanel')
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(240)
        self.setMaximumWidth(PANEL_WIDTH + 80)
        self._player = player

        box = QVBoxLayout(self)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel('Очередь')
        title.setObjectName('sectionTitle')
        head.addWidget(title)
        head.addStretch(1)
        self._clear_btn = QPushButton('Очистить')
        self._clear_btn.setObjectName('link')
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.clicked.connect(player.clear_queue)
        head.addWidget(self._clear_btn)
        close = QPushButton()
        close.setObjectName('iconBtn')
        close.setFixedSize(28, 28)
        close.setIconSize(QSize(16, 16))
        close.setCursor(Qt.PointingHandCursor)
        # Стрелка вправо, а не значок «свернуть»: панель уезжает именно вправо, за
        # край окна, и направление читается без наведения на подсказку
        close.setIcon(player_icons.draw('chevron_right'))
        close.setToolTip('Скрыть очередь')
        close.clicked.connect(self.close_requested.emit)
        head.addWidget(close)
        box.addLayout(head)

        self._next_label = QLabel('Далее')
        self._next_label.setObjectName('sectionTitle')
        box.addWidget(self._next_label)

        self._list = _QueueList(self)
        self._list.play_requested.connect(self._on_play)
        self._list.enqueue_requested.connect(
            lambda tracks, first: player.enqueue(tracks, first))
        self._list.remove_requested.connect(self._on_remove)
        self._list.reorder_requested.connect(self._on_reorder)
        self._list.add_vk_requested.connect(self.add_vk_requested.emit)
        self._list.favorite_requested.connect(self.favorite_requested.emit)
        self._list.radio_requested.connect(self.radio_requested.emit)
        self._list.open_source_requested.connect(self.open_source_requested.emit)
        self._list.hide_requested.connect(self.hide_requested.emit)
        self._list.hide_artist_requested.connect(self.hide_artist_requested.emit)
        self._list.artist_requested.connect(self.artist_requested.emit)
        self._list.playlist_requested.connect(self.playlist_requested.emit)
        # Здесь полоска единственный вход в выбор: долгое зажатие в очереди
        # тянет строку, а не отмечает её
        self._select_bar = SelectionBar(self._list)
        box.addWidget(self._select_bar)
        box.addWidget(self._list, 1)

        self._empty = EmptyState(
            'queue', 'Очередь пуста',
            'Включите трек в VK, YouTube или библиотеке, он появится здесь, '
            'а следующие можно добавить командой «В очередь».')
        box.addWidget(self._empty, 1)

        player.queue_changed.connect(self.reload)
        player.track_changed.connect(self._on_track)
        self.reload()

    def sizeHint(self) -> QSize:
        return QSize(PANEL_WIDTH, 400)

    # ---------- содержимое ----------
    def reload(self) -> None:
        tracks = self._player.queue.tracks
        self._list.set_tracks(tracks)
        self._list.set_current(self._player.current)
        self._on_track(self._player.current)
        has = bool(tracks)
        self._list.setVisible(has)
        self._select_bar.setVisible(has)
        if not has:
            # Очередь опустела — выбирать больше нечего
            self._list.set_selection_mode(False)
        self._empty.setVisible(not has)
        self._next_label.setVisible(has)
        self._clear_btn.setVisible(has)
        left = self._player.queue.remaining()
        self._next_label.setText(f'Далее: {left}' if left else 'Далее ничего нет')

    def _on_track(self, track) -> None:
        # Что играет сейчас, панели рассказывать нечем и незачем: тот же трек
        # стоит подсвеченной строкой в списке ниже и целиком расписан в полосе
        # плеера. Карточка с обложкой над списком повторяла и то, и другое
        self._list.set_current(track)

    def set_store(self, store) -> None:
        self._list.set_store(store)

    def set_vk_state(self, uid: str, label: str) -> None:
        self._list.set_vk_state(uid, label)

    def set_vk_states(self, states: dict) -> None:
        self._list.set_vk_states(states)

    # ---------- действия ----------
    def _on_play(self, _tracks, row: int) -> None:
        # Строка списка — это и есть место в очереди
        self._player.play_at(row)

    def _on_remove(self, rows) -> None:
        # С конца: иначе после первого удаления остальные номера съедут
        for row in sorted(rows, reverse=True):
            self._player.remove_at(row)

    def _on_reorder(self, source: int, target: int) -> None:
        self._player.move_in_queue(source, target)

    def keyPressEvent(self, event) -> None:
        # Delete работает только когда очередь в фокусе — иначе клавиша начнёт
        # удалять треки, пока человек занят совсем другим разделом
        if event.key() == Qt.Key_Delete and self._list.hasFocus():
            self._on_remove([self._list.currentRow()])
            return
        super().keyPressEvent(event)
