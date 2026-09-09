import atexit
import logging
import os
import sys

from .. import config


def configure_logging() -> None:
    config.ensure_dirs()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s %(threadName)s %(levelname)s %(name)s: %(message)s')

    file_handler = logging.FileHandler(config.LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    _quiet_noisy_libraries()
    sys.excepthook = _log_unhandled_exception
    _mark_run_boundaries()
    _capture_qt_messages()


def _quiet_noisy_libraries() -> None:
    """Убрать из журнала чужой отладочный поток - прежде всего сетевой.

    Корневой уровень DEBUG нужен нам самим, но вместе с нашими записями его
    подхватывал `urllib3` и печатал каждый запрос целиком, вместе с адресом.
    В адресах VK едут `c_hash`, `r_hash` и прочие ключи сессии - то есть журнал,
    которым человек делится, когда просит помощи, отдавал вместе с собой доступ
    к аккаунту. Своих записей это не касается: длинные ключи мы и так режем."""
    for name in ('urllib3', 'requests', 'vk_api', 'charset_normalizer', 'PIL'):
        logging.getLogger(name).setLevel(logging.WARNING)


def _mark_run_boundaries() -> None:
    """Пометить начало и конец сеанса в журнале.

    Без этих строк внезапный перезапуск программы неотличим от продолжения работы:
    записи просто идут дальше, а понять, что между ними процесс умер и поднялся
    заново, нельзя. Обрыв без строки «завершение» - это и есть падение: значит,
    процесс убили мимо Python (Chromium, драйвер, диспетчер задач), и `excepthook`
    такого не видит."""
    logger = logging.getLogger('app.run')
    logger.info('=== запуск, pid %d ===', os.getpid())
    atexit.register(lambda: logger.info('=== завершение, pid %d ===', os.getpid()))


def _capture_qt_messages() -> None:
    """Забрать себе поток сообщений Qt: до сих пор он уходил мимо журнала.

    Смысл - в последней строке перед смертью процесса. Питоновское исключение ловит
    `sys.excepthook`, но Qt на своей фатальной ошибке зовёт abort(): окно исчезает,
    и в журнале не остаётся ничего. Особенно это касается встроенного браузера -
    падение его отрисовки Qt объявляет именно так.
    """
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    logger = logging.getLogger('qt')
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):
        level = levels.get(mode, logging.INFO)
        logger.log(level, '%s', message)
        if level >= logging.CRITICAL:
            # После фатального сообщения Qt процесс не переживёт - дописываем сейчас
            for h in logging.getLogger().handlers:
                h.flush()

    qInstallMessageHandler(handler)


def _log_unhandled_exception(exc_type, exc_value, exc_tb) -> None:
    logging.getLogger('unhandled').critical(
        'Необработанное исключение в главном потоке', exc_info=(exc_type, exc_value, exc_tb)
    )
