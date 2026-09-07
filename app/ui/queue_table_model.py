from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from .widgets import PROGRESS_ROLE, STATE_ROLE

COLUMNS = ['Название', 'Источник', 'Прогресс', 'Статус']
COL_TITLE, COL_SOURCE, COL_PROGRESS, COL_STATUS = range(4)

_SOURCE_LABELS = {
    'youtube': 'YouTube',
    'vk_video': 'VK видео',
    'vk_audio': 'VK музыка',
}

_STATUS_COLORS = {
    'done': QColor('#61d6a2'),
    'error': QColor('#ff9088'),
    'cancelled': QColor('#8a92a3'),
}

# Статусы задачи -> состояние строки. Задача сообщает их текстом, здесь они нужны
# для цвета полосы и для того, чтобы понимать, какие строки можно убрать из списка.
_FINAL_STATES = {'Готово': 'done', 'Ошибка': 'error', 'Отменено': 'cancelled'}


class QueueTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._index_by_id = {}

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    @staticmethod
    def _state_of(item) -> str:
        return _FINAL_STATES.get(item.status, 'active')

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self._rows[index.row()]
        col = index.column()

        if role == PROGRESS_ROLE:
            return item.progress if col == COL_PROGRESS else None
        if role == STATE_ROLE:
            return self._state_of(item)

        if role == Qt.DisplayRole:
            if col == COL_TITLE:
                return item.title
            if col == COL_SOURCE:
                return _SOURCE_LABELS.get(item.source, item.source)
            if col == COL_PROGRESS:
                if item.status in _FINAL_STATES and item.status != 'Готово':
                    return '·'
                return f'{item.progress:.0f}%' + (f' · {item.speed}' if item.speed else '')
            if col == COL_STATUS:
                return item.status

        if role == Qt.ToolTipRole:
            if item.error:
                return f'{item.title}\n\n{item.error}'
            return item.path or item.title

        if role == Qt.ForegroundRole and col == COL_STATUS:
            return _STATUS_COLORS.get(self._state_of(item))

        if role == Qt.TextAlignmentRole and col in (COL_SOURCE, COL_STATUS):
            return int(Qt.AlignCenter)

        return None

    # ---------- изменения ----------
    def add_item(self, item) -> None:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(item)
        self._index_by_id[item.id] = row
        self.endInsertRows()

    def _row_of(self, item_id: str):
        return self._index_by_id.get(item_id)

    def _touch(self, row: int, *columns: int) -> None:
        for col in columns:
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx)

    def update_progress(self, item_id: str, pct: float, speed: str) -> None:
        row = self._row_of(item_id)
        if row is None:
            return
        self._rows[row].progress = pct
        self._rows[row].speed = speed
        self._touch(row, COL_PROGRESS)

    def update_status(self, item_id: str, status: str) -> None:
        row = self._row_of(item_id)
        if row is None:
            return
        self._rows[row].status = status
        self._touch(row, COL_PROGRESS, COL_STATUS)

    def set_finished(self, item_id: str, success: bool, message: str) -> None:
        row = self._row_of(item_id)
        if row is None:
            return
        item = self._rows[row]
        if success:
            item.path = message
            item.progress = 100.0
        else:
            item.error = message
        item.speed = ''
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def item_at(self, row: int):
        return self._rows[row]

    def is_finished(self, row: int) -> bool:
        return self._rows[row].status in _FINAL_STATES

    def remove_rows(self, rows: list[int]) -> None:
        """Убрать строки из списка. Индексы пересобираем целиком: сдвигать их
        по одной — самый простой способ разъехаться с моделью."""
        for row in sorted(set(rows), reverse=True):
            if 0 <= row < len(self._rows):
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._rows[row]
                self.endRemoveRows()
        self._index_by_id = {item.id: i for i, item in enumerate(self._rows)}

    def finished_rows(self) -> list[int]:
        return [i for i in range(len(self._rows)) if self.is_finished(i)]

    def active_count(self) -> int:
        return sum(1 for i in range(len(self._rows)) if not self.is_finished(i))
