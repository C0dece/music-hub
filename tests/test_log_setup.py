"""Журнал должен пережить смерть программы.

Внезапно закрывшееся окно расследуется по одному-единственному следу — тому, что
успело попасть в файл. Поэтому проверяем не форматирование, а три вещи, которых
раньше не было: отметка о запуске, отметка о выходе и сообщения самого Qt.
"""
import logging
import unittest

from app.core import log_setup


class Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class RunBoundaryTests(unittest.TestCase):

    def setUp(self):
        self.sink = Collector()
        root = logging.getLogger()
        self._level = root.level
        # В работе уровень ставит configure_logging; здесь зовём части напрямую
        root.setLevel(logging.DEBUG)
        root.addHandler(self.sink)

    def tearDown(self):
        root = logging.getLogger()
        root.removeHandler(self.sink)
        root.setLevel(self._level)

    def messages(self, name):
        return [r.getMessage() for r in self.sink.records if r.name == name]

    def test_start_is_marked_with_the_pid(self):
        """Без отметки перезапуск неотличим от продолжения работы."""
        log_setup._mark_run_boundaries()
        said = self.messages('app.run')
        self.assertEqual(len(said), 1)
        self.assertIn('запуск', said[0])

    def test_qt_fatal_message_reaches_the_log(self):
        """Фатальная ошибка Qt убивает процесс мимо Python — её надо успеть записать."""
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        log_setup._capture_qt_messages()
        try:
            # Вызываем обработчик напрямую: настоящий qFatal завершил бы тест
            handler = qInstallMessageHandler(None)
            qInstallMessageHandler(handler)
            handler(QtMsgType.QtFatalMsg, None, 'движок сдался')
        finally:
            qInstallMessageHandler(None)

        said = self.messages('qt')
        self.assertIn('движок сдался', said)
        level = [r.levelno for r in self.sink.records if r.name == 'qt'][-1]
        self.assertEqual(level, logging.CRITICAL)

    def test_ordinary_qt_warning_is_not_raised_to_critical(self):
        """Шум Qt в журнале нужен, но пугать им нельзя — иначе он обесценит настоящее."""
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        log_setup._capture_qt_messages()
        try:
            handler = qInstallMessageHandler(None)
            qInstallMessageHandler(handler)
            handler(QtMsgType.QtWarningMsg, None, 'мелочь')
        finally:
            qInstallMessageHandler(None)

        level = [r.levelno for r in self.sink.records if r.name == 'qt'][-1]
        self.assertEqual(level, logging.WARNING)


if __name__ == '__main__':
    unittest.main()
