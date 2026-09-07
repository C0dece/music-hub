import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)


class _Signals(QObject):
    done = Signal(object, object)  # result, error


class AsyncJob(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.signals = _Signals()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def _emit(self, result, error, name: str) -> None:
        """Отдать результат в UI-поток, пережив закрытие приложения.

        При выходе Qt успевает удалить C++-часть сигналов раньше, чем фоновая
        задача добежит до emit. Слушать результат тогда уже некому, так что это
        не ошибка, а обычный конец работы."""
        try:
            self.signals.done.emit(result, error)
        except RuntimeError:
            logger.debug('run_async: %s завершился после закрытия окна', name)

    def run(self) -> None:
        name = getattr(self._fn, '__qualname__', repr(self._fn))
        logger.debug('run_async: старт %s', name)
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            # Ошибки, которые мы сами формулируем для пользователя (истёк вход, VK отказал),
            # программу не ломают — трейсбек в логе для них лишний шум
            if getattr(exc, 'user_facing', False):
                logger.warning('run_async: %s: %s', name, str(exc).splitlines()[0])
            else:
                logger.exception('run_async: %s завершился с исключением', name)
            self._emit(None, exc, name)
        else:
            logger.debug('run_async: %s завершился успешно', name)
            self._emit(result, None, name)


# AsyncJob — не QObject, и ничего в Qt не держит на него ссылку, пока QThreadPool
# им не завладеет. Ни один вызывающий код не сохраняет возвращённый job — значит,
# Python сборщик мусора удалял его (и его сигнал) сразу после return, зачастую
# раньше, чем фоновый поток успевал сделать emit(). Отсюда "RuntimeError: Signal
# source has been deleted" и полная тишина в UI при реально успешном результате.
_active_jobs: set[AsyncJob] = set()


def run_async(fn, on_done, *args, **kwargs) -> AsyncJob:
    """Выполняет fn(*args, **kwargs) в пуле потоков, вызывает on_done(result, error) в UI-потоке."""
    name = getattr(fn, '__qualname__', repr(fn))

    job = AsyncJob(fn, *args, **kwargs)
    _active_jobs.add(job)

    def safe_on_done(result, error):
        _active_jobs.discard(job)
        try:
            on_done(result, error)
        except Exception:
            # PySide6 по умолчанию тихо глотает исключения, брошенные внутри слота —
            # без этого лога такие сбои выглядели бы как "ничего не произошло".
            logger.exception('run_async: обработчик on_done для %s упал с исключением', name)

    job.signals.done.connect(safe_on_done)
    QThreadPool.globalInstance().start(job)
    return job
