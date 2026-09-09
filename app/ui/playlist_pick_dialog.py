from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

from ..core import history
from .flow_layout import FlowRow
from .widgets import CheckableListWidget, ElidedLabel


def format_duration(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ''
    if total <= 0:
        return ''
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


class PlaylistPickDialog(QDialog):
    """Выбор элементов плейлиста перед скачиванием.

    Отметки живут на самих QListWidgetItem, а поиск только прячет строки - так
    выбор не теряется при вводе в поиск и «Скачать» получает именно то, что отмечено."""

    def __init__(self, title: str, entries: list[dict], parent=None, history_key=None,
                 preview=None, preview_text: str = 'Посмотреть'):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 560)
        self._entries = entries
        self._items: list[QListWidgetItem] = []
        # preview(entry) открывает запись до скачивания. Чем именно - знает
        # вызывающий: диалогу всё равно, видео это с YouTube или трек VK
        self._preview = preview
        self._preview_btn = None
        # history_key(entry) -> ключ истории; None - если отмечать скачанное не нужно.
        # Ключ зависит от режима (музыка/видео), а его знает только вызывающий
        self._history_key = history_key

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        heading = QLabel(title)
        heading.setObjectName('h2')
        layout.addWidget(heading)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Поиск по названию…')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_search)
        layout.addWidget(self._search)

        self._list = CheckableListWidget(self)
        self._list.setAlternatingRowColors(True)
        self._list.setUniformItemSizes(True)
        for entry in entries:
            already = self._is_downloaded(entry)
            item = QListWidgetItem(self._label_for(entry, already))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if already else Qt.Checked)
            item.setData(Qt.UserRole, item.text().lower())
            self._list.addItem(item)
            self._items.append(item)
        self._list.itemChanged.connect(self._update_summary)
        if preview is not None:
            self._list.itemDoubleClicked.connect(self._preview_item)
            self._list.currentRowChanged.connect(self._update_summary)
        layout.addWidget(self._list, 1)

        btn_row = FlowRow(spacing=8, align_right=True)
        for text, state in (('Выбрать всё', Qt.Checked), ('Снять всё', Qt.Unchecked)):
            button = QPushButton(text)
            button.setObjectName('secondary')
            button.clicked.connect(lambda _=False, s=state: self._set_visible(s))
            btn_row.add(button)
        invert = QPushButton('Инвертировать')
        invert.setObjectName('secondary')
        invert.clicked.connect(self._invert_visible)
        btn_row.add(invert)
        if preview is not None:
            self._preview_btn = QPushButton(preview_text)
            self._preview_btn.setObjectName('secondary')
            self._preview_btn.setToolTip(
                'Открыть выделенную строку, ничего не скачивая (двойной клик)')
            self._preview_btn.clicked.connect(self._preview_current)
            btn_row.add(self._preview_btn)
        btn_row.add_stretch()
        self._summary = ElidedLabel()
        self._summary.setObjectName('hint')
        btn_row.add(self._summary)
        layout.addWidget(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_button = buttons.button(QDialogButtonBox.Ok)
        self._ok_button.setText('Скачать')
        buttons.button(QDialogButtonBox.Cancel).setText('Отмена')
        buttons.button(QDialogButtonBox.Cancel).setObjectName('secondary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_summary()

    def _is_downloaded(self, entry: dict) -> bool:
        key = self._history_key(entry) if self._history_key is not None else ''
        return bool(key) and history.is_downloaded(key)

    @staticmethod
    def _label_for(entry: dict, already: bool) -> str:
        parts = [entry.get('title') or entry.get('id') or '???']
        duration = format_duration(entry.get('duration'))
        if duration:
            parts.append(duration)
        author = entry.get('uploader') or ''
        if author:
            parts.append(author)
        if already:
            parts.append('уже скачано')
        return '   ·   '.join(parts)

    # ---------- поиск и отметки ----------
    def _apply_search(self, text: str) -> None:
        needle = text.strip().lower()
        for item in self._items:
            item.setHidden(bool(needle) and needle not in item.data(Qt.UserRole))
        self._update_summary()

    def _visible_items(self) -> list[QListWidgetItem]:
        return [item for item in self._items if not item.isHidden()]

    def _set_visible(self, state) -> None:
        # Кнопки действуют на то, что видно: иначе «снять всё» при активном поиске
        # молча сбрасывало бы и отфильтрованные строки
        for item in self._visible_items():
            item.setCheckState(state)

    def _invert_visible(self) -> None:
        for item in self._visible_items():
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _update_summary(self, *_args) -> None:
        checked = sum(1 for item in self._items if item.checkState() == Qt.Checked)
        total = len(self._items)
        hidden = total - len(self._visible_items())
        text = f'Выбрано: {checked} из {total}'
        if hidden:
            text += f' · скрыто поиском: {hidden}'
        self._summary.setText(text)
        self._ok_button.setEnabled(checked > 0)
        self._ok_button.setText(f'Скачать ({checked})' if checked else 'Скачать')
        if self._preview_btn is not None:
            self._preview_btn.setEnabled(self._current_entry() is not None)

    # ---------- просмотр до скачивания ----------
    def _current_entry(self):
        row = self._list.currentRow()
        if not 0 <= row < len(self._entries) or self._items[row].isHidden():
            return None
        return self._entries[row]

    def _preview_current(self) -> None:
        entry = self._current_entry()
        if entry is not None:
            self._preview(entry)

    def _preview_item(self, item: QListWidgetItem) -> None:
        row = self._list.row(item)
        if 0 <= row < len(self._entries):
            self._preview(self._entries[row])

    def selected_entries(self) -> list[dict]:
        return [
            self._entries[i] for i, item in enumerate(self._items)
            if item.checkState() == Qt.Checked
        ]
