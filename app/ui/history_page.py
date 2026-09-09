"""История прослушиваний: сегодня, вчера и раньше.

Здесь именно события, а не список уникальных треков: если песню включали трижды,
она и покажется трижды - так видно, что человек слушал в тот вечер.

Записи в историю попадают не сразу: короткое включение «на секунду» не считается
прослушиванием (см. player_controller). Поэтому список здесь не засоряется
случайными нажатиями."""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .flow_layout import FlowRow
from .track_list import ROW_HEIGHT, TrackListWidget
from .widgets import ElidedLabel, EmptyState

HISTORY_LIMIT = 300

GROUPS = ('Сегодня', 'Вчера', 'Ранее')


def group_for(played_at: float, now: float | None = None) -> str:
    """К какой группе относится время прослушивания."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    when = datetime.fromtimestamp(played_at or 0).date()
    delta = (today - when).days
    if delta <= 0:
        return GROUPS[0]
    if delta == 1:
        return GROUPS[1]
    return GROUPS[2]


class HistoryPage(QWidget):
    """Что и когда играло, с обычными действиями над треком."""

    cleared = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self._store = store
        self._blocks: dict[str, tuple[QLabel, TrackListWidget]] = {}
        # uid прослушивания по каждой строке: удалять нужно запись, а не трек
        self._entry_ids: dict[str, list[int]] = {}

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        row = FlowRow(spacing=8)
        self._summary = ElidedLabel('')
        self._summary.setObjectName('hint')
        row.add(self._summary)
        row.add_stretch()
        clear = self._clear_btn = QPushButton('Очистить историю')
        clear.setObjectName('secondary')
        clear.clicked.connect(self._clear)
        row.add(clear)
        box.addWidget(row)

        # Три списка друг под другом. Каждый ровно по своему содержимому: иначе
        # «Вчера» с двумя треками занимало бы треть экрана.
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self._inner_box = QVBoxLayout(inner)
        self._inner_box.setContentsMargins(0, 0, 0, 0)
        self._inner_box.setSpacing(8)
        for title in GROUPS:
            label = QLabel(title)
            label.setObjectName('h2')
            widget = TrackListWidget(self, allow_remove=True)
            widget.remove_requested.connect(
                lambda rows, name=title: self._remove(name, rows))
            label.hide()
            widget.hide()
            self._inner_box.addWidget(label)
            self._inner_box.addWidget(widget)
            self._blocks[title] = (label, widget)
        self._empty = EmptyState(
            'headphones', 'История пуста',
            'Здесь копится всё, что вы слушали: недавнее сверху, дальше по дням.')
        self._inner_box.addWidget(self._empty)
        self._inner_box.addStretch(1)
        area.setWidget(inner)
        box.addWidget(area, 1)

        self.reload()

    @property
    def lists(self) -> list[TrackListWidget]:
        """Списки для общей проводки в главном окне."""
        return [widget for _label, widget in self._blocks.values()]

    # ---------- данные ----------
    def reload(self) -> None:
        entries = self._store.history(HISTORY_LIMIT) if self._store is not None else []
        now = time.time()
        grouped: dict[str, list] = {name: [] for name in GROUPS}
        ids: dict[str, list[int]] = {name: [] for name in GROUPS}
        for entry in entries:
            name = group_for(entry.get('played_at') or 0, now)
            grouped[name].append(entry['track'])
            ids[name].append(int(entry.get('id') or 0))
        self._entry_ids = ids

        for name in GROUPS:
            label, widget = self._blocks[name]
            tracks = grouped[name]
            widget.set_tracks(tracks)
            visible = bool(tracks)
            label.setVisible(visible)
            widget.setVisible(visible)
            if visible:
                # Высота ровно под содержимое: список внутри прокрутки своей
                # полосой прокрутки только мешал бы
                widget.setFixedHeight(len(tracks) * ROW_HEIGHT + 4)
        self._empty.setVisible(not entries)
        # Чистить нечего - и кнопка над пустой страницей только сбивает с толку
        self._clear_btn.setVisible(bool(entries))
        self._summary.setText(f'Прослушиваний: {len(entries)}' if entries else '')

    def set_current(self, track) -> None:
        for widget in self.lists:
            widget.set_current(track)

    # ---------- действия ----------
    def _remove(self, group: str, rows) -> None:
        if self._store is None:
            return
        ids = self._entry_ids.get(group) or []
        for row in sorted(rows or (), reverse=True):
            if 0 <= row < len(ids):
                self._store.delete_history_entry(ids[row])
        self.reload()

    def _clear(self) -> None:
        if self._store is not None:
            self._store.clear_history()
        self.reload()
        self.cleared.emit()
