"""Добавление сразу многих ссылок: одно поле, по ссылке в строке.

Обычное поле ввода - на одну ссылку, и вставить в него список нельзя: QLineEdit
склеивает перенос строки. Здесь тот же разбор (`url_detect.split_urls`), только
текста может быть сколько угодно."""
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

from ..core.url_detect import detect, split_urls
from .icon import app_icon


class BulkAddDialog(QDialog):
    """Список ссылок для скачивания. urls() отдаёт разобранное."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowIcon(app_icon())
        self.setWindowTitle('Добавить списком')
        self.resize(620, 460)
        self.setMinimumSize(380, 300)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        heading = QLabel('Ссылки списком')
        heading.setObjectName('h2')
        layout.addWidget(heading)

        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText(
            'По ссылке в строке:\n'
            'https://www.youtube.com/watch?v=…\n'
            'https://vk.com/video-1_2\n\n'
            'Плейлисты тоже можно, по каждому спросим, что скачать.')
        self._edit.textChanged.connect(self._update_summary)
        layout.addWidget(self._edit, 1)

        self._summary = QLabel()
        self._summary.setObjectName('hint')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_button = buttons.button(QDialogButtonBox.Ok)
        self._ok_button.setText('Добавить')
        buttons.button(QDialogButtonBox.Cancel).setText('Отмена')
        buttons.button(QDialogButtonBox.Cancel).setObjectName('secondary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._prefill_from_clipboard()
        self._update_summary()

    def _prefill_from_clipboard(self) -> None:
        """Если в буфере уже лежит список ссылок - подставить его сразу.

        Ради этого окно обычно и открывают: ссылки скопированы заранее. Чужой
        текст не трогаем - вставляем, только когда там действительно ссылки."""
        text = QApplication.clipboard().text()
        if not text or len(text) > 100_000:
            return
        links = [url for url in split_urls(text) if detect(url).source != 'unknown']
        if links:
            self._edit.setPlainText('\n'.join(links))

    def urls(self) -> list[str]:
        return split_urls(self._edit.toPlainText())

    def _update_summary(self) -> None:
        urls = self.urls()
        known = sum(1 for url in urls if detect(url).source != 'unknown')
        unknown = len(urls) - known
        text = f'Ссылок: {known}'
        if unknown:
            text += f' · непонятных: {unknown} (пропустим)'
        self._summary.setText(text if urls else 'Пока пусто, вставьте ссылки.')
        self._ok_button.setEnabled(known > 0)
        self._ok_button.setText(f'Добавить ({known})' if known else 'Добавить')
