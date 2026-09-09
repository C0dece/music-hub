import logging
import os

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)


class _TaskSignals(QObject):
    status = Signal(str, str)      # путь, что сейчас происходит
    done = Signal(str, bool, str)  # путь, успех, текст ошибки
    saved = Signal(str, object)    # путь, запись VK (id/owner_id) или None


class _UploadTask(QRunnable):
    def __init__(self, client, path: str, artist: str = '', title: str = ''):
        super().__init__()
        self.signals = _TaskSignals()
        self._client = client
        self._path = path
        self._artist = artist
        self._title = title
        self.setAutoDelete(True)

    def run(self) -> None:
        path = self._path
        try:
            saved = self._client.upload_audio(
                path, artist=self._artist, title=self._title,
                status_cb=lambda text: self.signals.status.emit(path, text))
        except Exception as exc:
            logger.warning('Заливка «%s» не удалась: %s', os.path.basename(path), exc)
            self.signals.done.emit(path, False, str(exc))
        else:
            # Координаты сохранённой записи нужны разделу музыки: по ним трек
            # связывается с исходником на YouTube (см. core/vk_import.py)
            self.signals.saved.emit(path, saved if isinstance(saved, dict) else None)
            self.signals.done.emit(path, True, '')


class VkUploadQueue(QObject):
    """Очередь заливки файлов в «Мою музыку» VK - по одному за раз.

    Параллельно смысла нет: канал вверх один, а VK не любит частых обращений.
    Если входа в VK сейчас нет, очередь просто ждёт - приложение переподключается
    само, и после этого заливка продолжится с того же места."""

    progress = Signal(str, str)        # путь, статус
    uploaded = Signal(str, bool, str)  # путь, успех, текст ошибки
    uploaded_info = Signal(str, object)  # путь, запись VK или None - до uploaded
    changed = Signal()                 # состав очереди изменился

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client = None
        self._meta: dict[str, tuple[str, str]] = {}
        self._queue: list[str] = []
        self._current = ''
        self._task = None  # держим ссылку: иначе сигналы задачи умрут раньше emit
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    @property
    def ready(self) -> bool:
        return self._client is not None

    @property
    def pending(self) -> int:
        return len(self._queue) + bool(self._current)

    @property
    def current(self) -> str:
        return self._current

    def set_client(self, client) -> None:
        self._client = client
        if client is not None:
            self._start_next()

    def add(self, paths, artist: str = '', title: str = '') -> int:
        """Ставит файлы в очередь, пропуская уже стоящие. Возвращает число добавленных.

        Исполнитель с названием нужны там, где имя файла им не соответствует -
        например, при переносе трека с YouTube. Пусто - берутся из имени файла."""
        added = 0
        for path in paths:
            if path and path != self._current and path not in self._queue:
                self._queue.append(path)
                if artist or title:
                    self._meta[path] = (artist, title)
                added += 1
        if added:
            self.changed.emit()
            self._start_next()
        return added

    def clear(self) -> None:
        """Убирает всё, что ещё не начали. Текущий файл дозаливается - обрывать
        отправку на середине VK всё равно не даст."""
        if self._queue:
            self._queue.clear()
            self._meta.clear()
            self.changed.emit()

    def _start_next(self) -> None:
        if self._current or not self._queue or self._client is None:
            return
        self._current = self._queue.pop(0)
        artist, title = self._meta.pop(self._current, ('', ''))
        self._task = _UploadTask(self._client, self._current, artist, title)
        self._task.signals.status.connect(self.progress)
        self._task.signals.saved.connect(self.uploaded_info)
        self._task.signals.done.connect(self._on_done)
        self._pool.start(self._task)
        self.changed.emit()

    def _on_done(self, path: str, ok: bool, error: str) -> None:
        self._current = ''
        self._task = None
        self.uploaded.emit(path, ok, error)
        self._start_next()
        self.changed.emit()
