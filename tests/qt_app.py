"""Одно приложение Qt на весь прогон тестов.

Qt разрешает создать ровно один экземпляр приложения за процесс, и заменить
QCoreApplication на QApplication по ходу дела нельзя - процесс просто падает.
Поэтому все тесты, которым нужны сигналы или виджеты, берут приложение отсюда,
и оно всегда полноценное, но рисует вхолостую.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication  # noqa: E402


def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])
