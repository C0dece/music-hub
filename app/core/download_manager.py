import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from . import history, vk_client, ytdlp_engine

logger = logging.getLogger(__name__)

# Сколько раз задача пробует себя воскресить после обрыва связи и сколько ждёт
# перед каждым повтором. Три попытки покрывают переподключение VPN и короткий
# провал сети; дальше молчаливое ожидание только вводит в заблуждение.
MAX_ATTEMPTS = 3
RETRY_DELAYS = (5, 20)

# Признаки того, что виновата сеть, а не само видео. Разбираем текст ошибки: yt-dlp
# заворачивает всё подряд в DownloadError, и по типу исключения отличить обрыв связи
# от «видео удалено» нельзя.
_NETWORK_MARKERS = (
    'timed out', 'timeout', 'connection', 'connectionreset', 'unable to download',
    'temporary failure', 'name resolution', 'network', 'proxy', 'ssl', 'eof occurred',
    'remote end closed', 'incomplete read', 'broken pipe', 'unreachable',
    'read operation', 'http error 5',
)


def is_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _NETWORK_MARKERS)


@dataclass
class QueueItem:
    id: str
    title: str
    source: str  # 'youtube' | 'vk_video' | 'vk_audio'
    history_key: str
    url: str = ''
    vk_track: Optional[dict] = None
    status: str = 'В очереди'
    progress: float = 0.0
    speed: str = ''
    error: str = ''
    path: str = ''   # заполняется по завершении - по нему открываем готовый файл
    cancel_event: threading.Event = field(default_factory=threading.Event)


def progress_pct(d: dict) -> float:
    """Процент по данным progress-хука yt-dlp.

    У фрагментных потоков (HLS, DASH) итоговый размер неизвестен: total_bytes_estimate
    пересчитывается на каждом фрагменте, и проценты прыгали вперёд-назад. Номер фрагмента
    растёт монотонно, поэтому для них считаем по нему."""
    total_frags = d.get('fragment_count')
    if total_frags:
        return min((d.get('fragment_index') or 0) / total_frags * 100, 100.0)
    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
    if not total:
        return 0.0
    return min((d.get('downloaded_bytes') or 0) / total * 100, 100.0)


class ProgressTracker:
    """Прогресс задачи целиком, а не отдельного потока.

    Видео и звук YouTube отдаёт разными файлами, и yt-dlp качает их по очереди -
    каждый от нуля до ста. Полоса из-за этого дважды пробегала шкалу и откатывалась
    назад. Каждому потоку отводим свой участок шкалы: сколько их будет, заранее
    неизвестно (иногда формат один), поэтому первому отдаём почти всю шкалу, а
    остаток делим между следующими. Назад полоса не идёт никогда."""

    # Верхние границы участков: первый поток, второй, все прочие
    _BOUNDS = (85.0, 95.0, 99.0)
    # Байты скачаны, идёт склейка и конвертация - до конца недалеко, но и не мгновенно
    PROCESSING = 99.0

    def __init__(self) -> None:
        self._file: str | None = None
        self._phase = -1
        self._value = 0.0

    def update(self, d: dict) -> float:
        name = d.get('filename') or d.get('tmpfilename') or ''
        if name != self._file:
            self._file = name
            self._phase += 1
        phase = min(max(self._phase, 0), len(self._BOUNDS) - 1)
        low = self._BOUNDS[phase - 1] if phase else 0.0
        high = self._BOUNDS[phase]
        return self._advance(low + (high - low) * progress_pct(d) / 100.0)

    def processing(self) -> float:
        return self._advance(self.PROCESSING)

    def _advance(self, value: float) -> float:
        self._value = max(self._value, min(value, self.PROCESSING))
        return self._value


class _TaskSignals(QObject):
    progress = Signal(str, float, str)
    status = Signal(str, str)
    finished = Signal(str, bool, str)


class DownloadTask(QRunnable):
    def __init__(self, item: QueueItem, settings: dict, vk_client=None):
        super().__init__()
        self.item = item
        self.settings = settings
        self.vk_client = vk_client
        self.signals = _TaskSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        item = self.item
        if item.cancel_event.is_set():
            self.signals.status.emit(item.id, 'Отменено')
            self.signals.finished.emit(item.id, False, 'Отменено')
            return

        self.signals.status.emit(item.id, 'Скачивается')
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if item.source in ('youtube', 'vk_video'):
                    self._run_ytdlp()
                else:
                    self._run_vk_audio()
                return
            except Exception as exc:
                # Отмену видим по флагу, а не по типу исключения: yt-dlp заворачивает ошибку
                # из progress-хука в собственную (UnavailableVideoError), и без этой проверки
                # отменённая задача показывалась бы как упавшая.
                if item.cancel_event.is_set():
                    self.signals.status.emit(item.id, 'Отменено')
                    self.signals.finished.emit(item.id, False, 'Отменено')
                    return
                # Обрыв связи - не повод терять задачу: VPN переподключается, прокси
                # моргает, YouTube закрывает соединение. Пробуем ещё раз сами, чтобы
                # не заставлять человека ставить всё в очередь заново.
                if attempt < MAX_ATTEMPTS and is_network_error(exc):
                    logger.info('Задача %s: обрыв связи (%s), повтор %d из %d',
                                item.title, exc, attempt + 1, MAX_ATTEMPTS)
                    if self._wait_before_retry(attempt):
                        continue
                    self.signals.status.emit(item.id, 'Отменено')
                    self.signals.finished.emit(item.id, False, 'Отменено')
                    return
                self.signals.status.emit(item.id, 'Ошибка')
                self.signals.finished.emit(item.id, False, str(exc))
                return

    def _wait_before_retry(self, attempt: int) -> bool:
        """Пауза перед повтором с обратным отсчётом в строке статуса.

        Ждём короткими шагами, а не одним sleep: иначе «Отменить» не сработало бы,
        пока идёт ожидание."""
        item = self.item
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]
        item.progress = 0.0
        for left in range(delay, 0, -1):
            if item.cancel_event.wait(1.0):
                return False
            self.signals.status.emit(
                item.id, f'Нет связи, повтор через {left} с')
        self.signals.status.emit(item.id, 'Скачивается')
        return not item.cancel_event.is_set()

    def _run_ytdlp(self) -> None:
        item = self.item
        s = self.settings
        # Свой счётчик на попытку: после обрыва загрузка начинается заново, и
        # продолжать шкалу с прежнего места было бы враньём
        tracker = ProgressTracker()

        def hook(d):
            if item.cancel_event.is_set():
                raise InterruptedError('Отменено пользователем')
            if d['status'] == 'downloading':
                self.signals.progress.emit(
                    item.id, tracker.update(d), d.get('_speed_str', '') or '')
            elif d['status'] == 'finished':
                # Байты скачаны, но впереди склейка и конвертация - 100% ставим в самом конце
                self.signals.progress.emit(item.id, tracker.processing(), '')
                self.signals.status.emit(item.id, 'Обработка')

        def retry_cb(attempt, total):
            # Пока yt-dlp ждёт ответа сети, progress-хук не вызывается: без этого
            # отмена не срабатывала, а очередь молча стояла на 0%.
            if item.cancel_event.is_set():
                raise InterruptedError('Отменено пользователем')
            self.signals.status.emit(item.id, f'Сеть не отвечает, повтор {attempt}/{total}')

        def pp_hook(d):
            if d.get('status') == 'started':
                self.signals.status.emit(item.id, 'Обработка')

        out_dir = s['video_dir'] if s['mode'] == 'video' else s['music_dir']
        opts = ytdlp_engine.build_opts(
            mode=s['mode'],
            video_quality=s['video_quality'],
            audio_format=s['audio_format'],
            audio_bitrate=s['audio_bitrate'],
            output_dir=out_dir,
            cookies_browser=s.get('cookies_browser'),
            progress_hook=hook,
            postprocessor_hook=pp_hook,
            retry_cb=retry_cb,
            fragment_concurrency=s.get('fragment_concurrency', 1),
        )
        info = ytdlp_engine.download(item.url, opts)
        title = info.get('title', item.title)
        # Только requested_downloads знает имя после постпроцессора: в info['filepath']
        # остаётся исходный файл (или ничего), и в историю попадал несуществующий путь
        path = ytdlp_engine.downloaded_path(info)
        history.mark_downloaded(item.history_key, title, path)
        self.signals.progress.emit(item.id, 100.0, '')
        self.signals.status.emit(item.id, 'Готово')
        self.signals.finished.emit(item.id, True, path)

    def _run_vk_audio(self) -> None:
        item = self.item
        s = self.settings
        track = item.vk_track

        if not track.get('url'):
            # Список треков приходит без прямых ссылок - VK отдаёт их отдельным запросом
            if self.vk_client is None:
                raise ValueError('Нет активного входа в VK, войдите заново')
            self.signals.status.emit(item.id, 'Получаю ссылку')
            track['url'] = self.vk_client.resolve_url(track)

        name = item.title or 'track'

        def progress_cb(downloaded, total):
            pct = min(downloaded / total * 100, 100.0) if total else 0.0
            self.signals.progress.emit(item.id, pct, '')

        def status_cb(text):
            self.signals.status.emit(item.id, text)

        self.signals.status.emit(item.id, 'Скачивается')
        dest_path = vk_client.VkClient.download_track(
            track, s['music_dir'], vk_client.safe_filename(name),
            progress_cb=progress_cb, cancel_event=item.cancel_event, status_cb=status_cb,
        )
        history.mark_downloaded(item.history_key, name, dest_path)
        self.signals.progress.emit(item.id, 100.0, '')
        self.signals.status.emit(item.id, 'Готово')
        self.signals.finished.emit(item.id, True, dest_path)


class DownloadManager(QObject):
    item_added = Signal(object)
    item_progress = Signal(str, float, str)
    item_status = Signal(str, str)
    item_finished = Signal(str, bool, str)

    def __init__(self, settings_provider: Callable[[], dict], parent=None):
        super().__init__(parent)
        self._settings_provider = settings_provider
        # Свой пул, а не глобальный: там же выполняются фоновые задачи интерфейса
        # (разбор ссылки, списки VK, скан библиотеки). На общем пуле три идущие
        # загрузки занимали все слоты, и интерфейс переставал отвечать на действия,
        # а «Одновременных загрузок: 1» делало однопоточным вообще всё приложение.
        self._pool = QThreadPool(self)
        self._items: dict[str, QueueItem] = {}
        # Нужен задачам, чтобы получить прямую ссылку на трек; ставится после входа в VK
        self.vk_client = None
        self.set_concurrency(3)

    def set_concurrency(self, n: int) -> None:
        self._pool.setMaxThreadCount(max(1, n))

    def add_youtube(self, url: str, title: str, history_key: str,
                    settings_override: dict | None = None) -> QueueItem:
        return self._enqueue(source='youtube', url=url, title=title, history_key=history_key,
                             settings_override=settings_override)

    def add_vk_video(self, url: str, title: str, history_key: str) -> QueueItem:
        return self._enqueue(source='vk_video', url=url, title=title, history_key=history_key)

    def add_vk_track(self, track: dict, history_key: str,
                     settings_override: dict | None = None) -> QueueItem:
        title = f"{track.get('artist', '')} - {track.get('title', '')}".strip(' -') or 'VK трек'
        return self._enqueue(source='vk_audio', vk_track=track, title=title,
                             history_key=history_key, settings_override=settings_override)

    def _enqueue(self, settings_override: dict | None = None, **kwargs) -> QueueItem:
        settings = dict(self._settings_provider())
        # Перенос трека в VK качает звук независимо от того, что выбрано на вкладке
        if settings_override:
            settings.update(settings_override)
        item = QueueItem(id=str(uuid.uuid4()), **kwargs)
        self._items[item.id] = item
        self.item_added.emit(item)

        task = DownloadTask(item, settings, self.vk_client)
        task.signals.progress.connect(self.item_progress)
        task.signals.status.connect(self.item_status)
        task.signals.finished.connect(self.item_finished)
        self._pool.start(task)
        return item

    def cancel(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item:
            item.cancel_event.set()

    def cancel_all(self) -> None:
        for item in self._items.values():
            item.cancel_event.set()
