import ctypes
import sys

from PySide6.QtWidgets import QApplication

from . import config
from .core import proxy
from .core.log_setup import configure_logging
from .ui import theme
from .ui.icon import app_icon
from .ui.main_window import MainWindow


def _set_taskbar_identity() -> None:
    """Своя иконка на панели задач Windows.

    Без собственного AppUserModelID Windows считает окно частью python.exe и
    показывает иконку интерпретатора."""
    if sys.platform != 'win32':
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(config.APP_ID)
    except Exception:  # noqa: BLE001 - косметика, падать из-за неё нельзя
        pass


def main() -> None:
    config.ensure_dirs()
    configure_logging()
    # До QApplication: настройки прокси для встроенного браузера (окно входа в VK)
    # QtWebEngine читает из окружения при первом запуске своего движка
    proxy.apply(config.load_settings())
    _set_taskbar_identity()
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_NAME)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(theme.stylesheet())

    # Окно может быть спрятано к часам или подменено мини-плеером - закрытие
    # последнего окна не должно завершать программу, выход делает само окно
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
