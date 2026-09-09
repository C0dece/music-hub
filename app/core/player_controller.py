"""Единый плеер приложения: одна очередь, один текущий трек, разные источники звука.

Источники играются по-разному - файл и VK через QtMultimedia, YouTube через
встроенный плеер сайта в QtWebEngine (прямые ссылки googlevideo до QMediaPlayer
не доходят, см. app/ui/web_preview.py). Чтобы интерфейс об этом не знал, разница
спрятана в «движки» (PlaybackBackend), а наружу торчит один PlayerController.

Движок для YouTube живёт в app/ui/youtube_backend.py и подключается снаружи:
ядру нельзя зависеть от виджетов.

Здесь же живут повтор, перемешивание, автопродолжение по рекомендациям, режим
аудио/видео и сохранение сеанса. Это состояние одного плеера: панель внизу окна,
значок у часов, мини-плеер и горячие клавиши смотрят на один и тот же объект,
поэтому дублировать логику в интерфейсе не нужно."""
from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QObject, QUrl, Signal

from .async_task import run_async
from .playback_queue import PlaybackQueue
from .track import (SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE, VIDEO_EXTS, Track,
                    to_vk_row)

logger = logging.getLogger(__name__)

STATE_STOPPED = 'stopped'
STATE_LOADING = 'loading'
STATE_RESOLVING = 'resolving'   # ищем прямую ссылку у источника
STATE_PLAYING = 'playing'
STATE_PAUSED = 'paused'
STATE_BUFFERING = 'buffering'
STATE_ERROR = 'error'

# Подписи для интерфейса: одно место на всё приложение, чтобы панель плеера,
# мини-плеер и подсказка у часов не расходились в формулировках.
STATE_LABELS = {
    STATE_STOPPED: '',
    STATE_LOADING: 'Загрузка…',
    STATE_RESOLVING: 'Получаю ссылку…',
    STATE_PLAYING: '',
    STATE_PAUSED: 'Пауза',
    STATE_BUFFERING: 'Буферизация…',
    STATE_ERROR: 'Ошибка',
}

REPEAT_OFF, REPEAT_ALL, REPEAT_ONE = 'off', 'all', 'one'
REPEAT_ORDER = (REPEAT_OFF, REPEAT_ALL, REPEAT_ONE)

MODE_AUTO, MODE_AUDIO, MODE_VIDEO = 'auto', 'audio', 'video'

# Сколько треков должно остаться в очереди, чтобы попросить следующую порцию
# рекомендаций. Пять - это примерно пятнадцать минут музыки: сходить в сеть
# успеваем с запасом, а очередь не разрастается на сотни строк.
AUTOPLAY_TAIL = 5
AUTOPLAY_BATCH = 15

# Что считается прослушанным. Иначе перещёлкивание очереди засоряло бы историю.
MEANINGFUL_SECONDS = 30
MEANINGFUL_SHARE = 0.2

# Позицию в базу пишем не чаще этого: иначе перемотка устраивала бы шквал записей.
_POSITION_SAVE_INTERVAL = 8.0


class PlaybackBackend(QObject):
    """Общий вид движка воспроизведения.

    Движок ничего не знает про очередь: ему дают трек и просят играть, а о том,
    что случилось, он сообщает сигналами."""

    state_changed = Signal(str)          # STATE_*
    position_changed = Signal(int, int)  # позиция и длительность в миллисекундах
    ended = Signal()                     # трек доиграл до конца
    failed = Signal(str)                 # текст ошибки для человека

    def can_play(self, track: Track) -> bool:
        raise NotImplementedError

    def play(self, track: Track) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def seek(self, position_ms: int) -> None:
        pass

    def set_volume(self, volume: int) -> None:
        pass

    def shutdown(self) -> None:
        self.stop()


class QtMediaBackend(PlaybackBackend):
    """Файлы на диске и аудио VK - штатным QMediaPlayer.

    Ссылку VK получаем в последний момент и только для того трека, который сейчас
    включают: прямые ссылки живут недолго, а запрашивать их для всей очереди
    заранее - это минуты ожидания на большой библиотеке."""

    def __init__(self, vk_client_provider=None, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        self._vk_client_provider = vk_client_provider or (lambda: None)
        self._audio = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)
        # Пока ходим в сеть за ссылкой, человек может переключить трек: у ответа
        # проверяем номер запроса и старый молча выбрасываем.
        self._token = 0
        self._loading = False
        self._video = None

    @property
    def video_widget(self):
        """Куда рисовать картинку локального видео.

        Создаём по требованию: у большинства очередей видео нет, а QVideoWidget
        поднимает графический стек. Виджет один на всё приложение - его
        показывает VideoStage, как и страницу YouTube."""
        if self._video is None:
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self._video = QVideoWidget()
            self._player.setVideoOutput(self._video)
        return self._video

    def can_play(self, track: Track) -> bool:
        return track.cached or track.source in (SOURCE_LOCAL, SOURCE_VK)

    def play(self, track: Track) -> None:
        self._token += 1
        token = self._token
        if track.cached:
            self._start(QUrl.fromLocalFile(track.local_path))
            return
        if track.source == SOURCE_LOCAL:
            self.failed.emit('Файл не найден на диске')
            return
        if track.source != SOURCE_VK:
            self.failed.emit('Этот трек нельзя проиграть здесь')
            return

        direct = track.meta.get('direct_url')
        if direct:
            self._start(QUrl(direct))
            return

        client = self._vk_client_provider()
        if client is None:
            self.failed.emit('Нет соединения с VK, войдите в аккаунт')
            return

        self._loading = True
        self.state_changed.emit(STATE_RESOLVING)
        row = to_vk_row(track)

        def resolve():
            return client.resolve_url(row)

        def on_done(url, error):
            if token != self._token:
                return  # пока ходили в сеть, включили другой трек
            self._loading = False
            if error or not url:
                self.failed.emit(str(error) if error else 'VK не отдал ссылку на файл')
                return
            track.meta['direct_url'] = url
            self._start(QUrl(url))

        run_async(resolve, on_done)

    def _start(self, url: QUrl) -> None:
        self._player.stop()
        self._player.setSource(url)
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        self._player.play()

    def stop(self) -> None:
        self._token += 1
        self._loading = False
        self._player.stop()
        self._player.setSource(QUrl())

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(int(position_ms))

    def set_volume(self, volume: int) -> None:
        self._audio.setVolume(max(0, min(100, int(volume))) / 100)

    # ---------- сигналы QMediaPlayer ----------
    def _on_position(self, position: int) -> None:
        self.position_changed.emit(int(position), int(self._player.duration()))

    def _on_duration(self, duration: int) -> None:
        self.position_changed.emit(int(self._player.position()), int(duration))

    def _on_playback_state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if state == QMediaPlayer.PlayingState:
            self.state_changed.emit(STATE_PLAYING)
        elif state == QMediaPlayer.PausedState:
            self.state_changed.emit(STATE_PAUSED)
        elif not self._loading:
            self.state_changed.emit(STATE_STOPPED)

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.EndOfMedia:
            self.ended.emit()
        elif status == QMediaPlayer.StalledMedia:
            self.state_changed.emit(STATE_BUFFERING)

    def _on_error(self, _error, message: str) -> None:
        self._loading = False
        self.failed.emit(message or 'Не удалось проиграть трек')


class PlayerController(QObject):
    """Что играет, что дальше и что с этим делать.

    Весь интерфейс - панель плеера, списки, значок у часов, горячие клавиши -
    обращается только сюда."""

    track_changed = Signal(object)        # Track или None
    state_changed = Signal(str)           # STATE_*
    position_changed = Signal(int, int)   # позиция и длительность, мс
    volume_changed = Signal(int)
    queue_changed = Signal()
    error = Signal(str)
    notice = Signal(str)                  # короткое сообщение в панели плеера
    shuffle_changed = Signal(bool)
    repeat_changed = Signal(str)
    autoplay_changed = Signal(bool)
    mode_changed = Signal(str)            # MODE_*

    def __init__(self, vk_client_provider=None, store=None, parent=None):
        super().__init__(parent)
        self.queue = PlaybackQueue()
        self._store = store
        self._backends: list[PlaybackBackend] = []
        self._backend: PlaybackBackend | None = None
        self._state = STATE_STOPPED
        self._volume = 80
        self._muted_volume = 0
        self._advance_in_queue = True   # настройка «включать следующий трек»
        self._position = 0
        self._duration = 0
        self._pending_seek = 0

        self._repeat = REPEAT_OFF
        self._autoplay = True           # продолжать рекомендациями, когда очередь кончилась
        self._mode = MODE_AUTO

        # Автопродолжение: один запрос за раз и без повторов уже сыгранного.
        self._recommender = None
        self._rec_busy = False
        self._rec_token = 0
        self._rec_recent: list[str] = []
        self._await_recommendations = False

        # Учёт прослушивания текущего трека
        self._listened_ms = 0
        self._logged = False
        self._last_position = 0
        self._last_saved = 0.0
        # Источники, которыми уже пробовали проиграть текущую песню
        self._tried_uids: set[str] = set()

        self._qt_backend = QtMediaBackend(vk_client_provider, self)
        self.add_backend(self._qt_backend)

    def video_widget(self):
        """Видеовыход для файлов - окну нужно, чтобы показать его на экране."""
        return self._qt_backend.video_widget

    # ---------- настройка ----------
    def add_backend(self, backend: PlaybackBackend) -> None:
        backend.state_changed.connect(self._on_backend_state)
        backend.position_changed.connect(self._on_backend_position)
        backend.ended.connect(self._on_backend_ended)
        backend.failed.connect(self._on_backend_failed)
        backend.set_volume(self._volume)
        self._backends.append(backend)

    def apply_settings(self, settings: dict) -> None:
        self._advance_in_queue = bool(settings.get('autoplay_next', True))
        if settings.get('remember_volume', True):
            self.set_volume(int(settings.get('volume', 80)))

    def set_recommender(self, recommender) -> None:
        """Чем продолжать очередь, когда она кончилась.

        Ожидается вызываемое `fn(seed, exclude, limit) -> list[Track]`, работающее
        в фоновом потоке: сюда его зовут через run_async."""
        self._recommender = recommender

    # ---------- состояние ----------
    @property
    def state(self) -> str:
        return self._state

    @property
    def current(self) -> Track | None:
        return self.queue.current()

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._volume == 0 and self._muted_volume > 0

    @property
    def position(self) -> int:
        return self._position

    @property
    def duration(self) -> int:
        return self._duration

    @property
    def playing(self) -> bool:
        return self._state == STATE_PLAYING

    @property
    def busy(self) -> bool:
        """Плеер занят подготовкой: кнопка не должна показывать «играет»."""
        return self._state in (STATE_LOADING, STATE_RESOLVING, STATE_BUFFERING)

    @property
    def shuffle(self) -> bool:
        return self.queue.shuffle

    @property
    def repeat(self) -> str:
        return self._repeat

    @property
    def autoplay(self) -> bool:
        return self._autoplay

    @property
    def mode(self) -> str:
        return self._mode

    def video_expected(self, track: Track | None = None) -> bool:
        """Показывать ли видео для этого трека при текущем режиме."""
        if self._mode == MODE_AUDIO:
            return False
        if self._mode == MODE_VIDEO:
            return True
        track = track if track is not None else self.queue.current()
        return bool(track is not None and track.is_video)

    # ---------- управление ----------
    def play_tracks(self, tracks, start: int = 0) -> None:
        """Заменить очередь и начать играть с выбранного места."""
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            return
        self.queue.set_tracks(tracks, start)
        self.queue_changed.emit()
        self._play_current()

    def play_track(self, track: Track) -> None:
        self.play_tracks([track], 0)

    def enqueue(self, tracks, play_next: bool = False) -> int:
        """Добавить в очередь. Если ничего не играет - начать играть добавленное."""
        if isinstance(tracks, Track):
            tracks = [tracks]
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            return 0
        was_empty = len(self.queue) == 0
        if play_next:
            self.queue.insert_next(tracks)
        else:
            self.queue.append(tracks)
        self.queue_changed.emit()
        if was_empty:
            self.queue.set_index(0)
            self._play_current()
        return len(tracks)

    def play_at(self, position: int) -> None:
        if self.queue.set_index(position) is not None:
            self.queue_changed.emit()
            self._play_current()

    def remove_at(self, position: int) -> None:
        playing_removed = position == self.queue.index
        self.queue.remove(position)
        self.queue_changed.emit()
        if playing_removed:
            if self.queue.current() is not None:
                self._play_current()
            else:
                self.stop()

    def move_in_queue(self, source: int, target: int) -> None:
        self.queue.move(source, target)
        self.queue_changed.emit()

    def clear_queue(self) -> None:
        self.stop()
        self.queue.clear()
        self.queue_changed.emit()
        self.track_changed.emit(None)

    def toggle(self) -> None:
        if self._state == STATE_PLAYING:
            self.pause()
        elif self._state == STATE_PAUSED:
            self.resume()
        elif self.queue.current() is not None:
            self._play_current(seek_ms=self._position)

    def pause(self) -> None:
        if self._backend is not None:
            self._backend.pause()

    def resume(self) -> None:
        if self._backend is not None:
            self._backend.resume()

    def stop(self) -> None:
        self._flush_history(finished=False)
        if self._backend is not None:
            self._backend.stop()
        self._set_state(STATE_STOPPED)
        self._position = self._duration = 0
        self.position_changed.emit(0, 0)

    def next(self) -> None:
        if self.queue.go_next() is not None:
            self.queue_changed.emit()
            self._play_current()
            return
        if self._repeat == REPEAT_ALL and len(self.queue):
            self.queue.set_index(0)
            self.queue_changed.emit()
            self._play_current()
            return
        if self._request_recommendations(advance=True):
            return
        self.stop()

    def previous(self) -> None:
        # Как у всех плееров: в начале трека - предыдущий, дальше - в начало текущего
        if self._position > 3000 and self._backend is not None:
            self._backend.seek(0)
            return
        if self.queue.go_previous() is not None:
            self.queue_changed.emit()
            self._play_current()

    def seek(self, position_ms: int) -> None:
        if self._backend is not None:
            self._backend.seek(position_ms)

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        if volume > 0:
            self._muted_volume = 0
        self._volume = volume
        for backend in self._backends:
            backend.set_volume(self._volume)
        self.volume_changed.emit(self._volume)

    def toggle_mute(self) -> None:
        """Выключить звук и вернуть прежнюю громкость, а не «поставить 50»."""
        if self._volume > 0:
            previous = self._volume
            self.set_volume(0)
            self._muted_volume = previous
        else:
            self.set_volume(self._muted_volume or 50)

    # ---------- режимы ----------
    def set_shuffle(self, enabled: bool) -> None:
        if bool(enabled) == self.queue.shuffle:
            return
        self.queue.set_shuffle(enabled)
        self.shuffle_changed.emit(self.queue.shuffle)
        self.queue_changed.emit()
        self.save_state()

    def toggle_shuffle(self) -> None:
        self.set_shuffle(not self.queue.shuffle)

    def set_repeat(self, mode: str) -> None:
        mode = mode if mode in REPEAT_ORDER else REPEAT_OFF
        if mode == self._repeat:
            return
        self._repeat = mode
        self.repeat_changed.emit(mode)
        self.save_state()

    def cycle_repeat(self) -> None:
        index = REPEAT_ORDER.index(self._repeat)
        self.set_repeat(REPEAT_ORDER[(index + 1) % len(REPEAT_ORDER)])

    def set_autoplay(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._autoplay:
            return
        self._autoplay = enabled
        self.autoplay_changed.emit(enabled)
        self.save_state()
        if enabled:
            self._maybe_extend()

    def toggle_autoplay(self) -> None:
        self.set_autoplay(not self._autoplay)

    def set_mode(self, mode: str) -> None:
        """Аудио/видео/авто. Воспроизведение при этом не перезапускается -
        интерфейс просто прячет или показывает область видео."""
        mode = mode if mode in (MODE_AUTO, MODE_AUDIO, MODE_VIDEO) else MODE_AUTO
        if mode == self._mode:
            return
        self._mode = mode
        self.mode_changed.emit(mode)
        self.save_state()

    # ---------- автопродолжение ----------
    def _maybe_extend(self) -> None:
        """Дозаказать рекомендации, если хвост очереди подходит к концу."""
        if self.queue.remaining() > AUTOPLAY_TAIL:
            return
        self._request_recommendations(advance=False)

    def _request_recommendations(self, advance: bool) -> bool:
        """Попросить следующую порцию. Возвращает True, если запрос ушёл или уже идёт.

        `advance` - очередь кончилась прямо сейчас, и как только придут треки,
        нужно сразу включить первый из них."""
        if not self._autoplay or self._recommender is None:
            return False
        if self._rec_busy:
            # Уже идём в сеть: пять одновременных запросов ничего не ускорят
            self._await_recommendations = self._await_recommendations or advance
            return True
        seed = self.queue.current()
        if seed is None and len(self.queue):
            seed = self.queue.at(len(self.queue) - 1)
        if seed is None:
            return False
        exclude = self._exclude_uids()
        recommender = self._recommender
        self._rec_busy = True
        self._rec_token += 1
        token = self._rec_token
        self._await_recommendations = advance
        if advance:
            self._set_state(STATE_LOADING)

        def fetch():
            return recommender(seed, exclude, AUTOPLAY_BATCH)

        def on_done(tracks, error):
            self._rec_busy = False
            if token != self._rec_token:
                return  # ответ на старый запрос - очередь с тех пор сменилась
            if error:
                logger.info('Автоплей: рекомендации не пришли: %s', error)
            self._on_recommendations(list(tracks or []))

        run_async(fetch, on_done)
        return True

    def _exclude_uids(self) -> set[str]:
        """Чего рекомендациям предлагать нельзя."""
        exclude = self.queue.uids() | set(self._rec_recent[-40:])
        if self._store is not None:
            try:
                exclude |= self._store.hidden_tracks()
                exclude |= set(self._store.played_uids(10))
            except Exception:
                logger.debug('Автоплей: список исключений не прочитался', exc_info=True)
        return exclude

    def _on_recommendations(self, tracks: list[Track]) -> None:
        advance = self._await_recommendations
        self._await_recommendations = False
        fresh = self._filter_recommendations(tracks)
        if not fresh:
            if advance:
                self.stop()
            return
        for track in fresh:
            track.meta.setdefault('provenance', 'autoplay')
        self._rec_recent.extend(t.uid for t in fresh)
        del self._rec_recent[:-80]
        self.queue.append(fresh)
        self.queue_changed.emit()
        self.save_state()
        if advance and self.queue.go_next() is not None:
            self._play_current()

    def _filter_recommendations(self, tracks: list[Track]) -> list[Track]:
        """Убрать дубли, скрытое и то, что только что играло."""
        hidden_artists: set[str] = set()
        exclude = self._exclude_uids()
        if self._store is not None:
            try:
                hidden_artists = self._store.hidden_artists()
            except Exception:
                hidden_artists = set()
        result: list[Track] = []
        seen: set[str] = set()
        for track in tracks:
            if track is None or track.uid in exclude or track.uid in seen:
                continue
            if (track.artist or '').strip().lower() in hidden_artists:
                continue
            seen.add(track.uid)
            result.append(track)
        return result

    def shutdown(self) -> None:
        """Остановить звук и отпустить ресурсы при закрытии приложения."""
        self._flush_history(finished=False)
        self._rec_token += 1
        for backend in self._backends:
            try:
                backend.shutdown()
            except Exception:
                logger.debug('Плеер: движок %s не закрылся', type(backend).__name__)
        self._backend = None

    # ---------- сохранение сеанса ----------
    def session_state(self) -> dict:
        return {'queue': self.queue.to_state(), 'position': int(self._position),
                'repeat': self._repeat, 'autoplay': self._autoplay, 'mode': self._mode,
                'volume': self._volume}

    def save_state(self) -> None:
        if self._store is None:
            return
        try:
            self._store.set_json('playback_session', self.session_state())
        except Exception:
            logger.debug('Плеер: сеанс не сохранился', exc_info=True)
        self._last_saved = time.monotonic()

    def restore_state(self) -> None:
        """Вернуть прошлый сеанс, но не начинать играть.

        Звук при старте включать нельзя (приложение может запускаться вместе с
        Windows), поэтому восстанавливаем очередь, место в ней и позицию, а
        решение «продолжить» оставляем человеку."""
        if self._store is None:
            return
        try:
            session = self._store.get_json('playback_session', None)
            if not isinstance(session, dict):
                # Сборки до сеансов хранили только очередь
                session = {'queue': self._store.get_json('queue', {}) or {}}
            self.queue.restore(session.get('queue') or {})
        except Exception:
            logger.debug('Плеер: сеанс не восстановился', exc_info=True)
            return
        repeat = session.get('repeat')
        self._repeat = repeat if repeat in REPEAT_ORDER else REPEAT_OFF
        self._autoplay = bool(session.get('autoplay', True))
        mode = session.get('mode')
        self._mode = mode if mode in (MODE_AUTO, MODE_AUDIO, MODE_VIDEO) else MODE_AUTO
        self._position = max(0, int(session.get('position') or 0))
        self.repeat_changed.emit(self._repeat)
        self.autoplay_changed.emit(self._autoplay)
        self.mode_changed.emit(self._mode)
        self.shuffle_changed.emit(self.queue.shuffle)
        self.queue_changed.emit()
        current = self.queue.current()
        if current is not None:
            self._duration = current.duration * 1000
            # Играть не начинаем: восстановили только список, место в нём и позицию
            self.track_changed.emit(current)
            self.position_changed.emit(self._position, self._duration)

    def resume_session(self) -> None:
        """Продолжить восстановленный сеанс с сохранённой позиции."""
        if self.queue.current() is None:
            return
        self._play_current(seek_ms=self._position)

    # ---------- внутреннее ----------
    def _pick_backend(self, track: Track) -> PlaybackBackend | None:
        # Офлайн-копия ролика YouTube - это только звук. Когда ждут картинку,
        # играть надо со страницы источника, иначе на видеополосе осталась бы
        # пустая страница, а звук шёл бы мимо неё
        if (track.source == SOURCE_YOUTUBE and track.cached
                and self.video_expected(track)
                and os.path.splitext(track.local_path)[1].lower() not in VIDEO_EXTS):
            track = track.with_local_path('')
        for backend in self._backends:
            if backend.can_play(track):
                return backend
        return None

    def _play_current(self, seek_ms: int = 0) -> None:
        track = self.queue.current()
        if track is None:
            return
        self._flush_history(finished=False)
        backend = self._pick_backend(track)
        if backend is None:
            if self._try_fallback(track, 'нечем проиграть'):
                return
            self.error.emit(f'Нечем проиграть: {track.display_title}')
            self._set_state(STATE_ERROR)
            return
        if self._backend is not None and self._backend is not backend:
            self._backend.stop()
        self._backend = backend
        self._position = 0
        self._last_position = 0
        self._listened_ms = 0
        self._logged = False
        self._duration = track.duration * 1000
        self._pending_seek = max(0, int(seek_ms))
        self.track_changed.emit(track)
        self._set_state(STATE_LOADING)
        backend.play(track)
        if self._store is not None:
            try:
                self._store.save_track(track)
            except Exception:
                logger.debug('Плеер: трек не сохранился в базу', exc_info=True)
        self._maybe_extend()
        self.save_state()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def _on_backend_state(self, state: str) -> None:
        if self.sender() is not self._backend:
            return  # отголосок движка, который уже не играет
        if state == STATE_PLAYING:
            self._tried_uids.clear()
            if self._pending_seek > 0:
                # Продолжение сеанса: перематывать можно только когда уже играет
                position, self._pending_seek = self._pending_seek, 0
                self._backend.seek(position)
        self._set_state(state)

    def _on_backend_position(self, position: int, duration: int) -> None:
        if self.sender() is not self._backend:
            return
        position = int(position)
        if duration > 0:
            self._duration = int(duration)
        # Прослушанное считаем по приросту позиции: перемотка вперёд не должна
        # засчитываться как прослушивание.
        delta = position - self._last_position
        if 0 < delta <= 4000:
            self._listened_ms += delta
        self._last_position = position
        self._position = position
        self.position_changed.emit(self._position, self._duration)
        self._maybe_log_history()
        if time.monotonic() - self._last_saved >= _POSITION_SAVE_INTERVAL:
            self.save_state()

    def _meaningful(self) -> bool:
        seconds = self._listened_ms / 1000
        if seconds >= MEANINGFUL_SECONDS:
            return True
        duration = self._duration / 1000
        return duration > 0 and seconds >= duration * MEANINGFUL_SHARE

    def _maybe_log_history(self) -> None:
        if self._logged or not self._meaningful():
            return
        self._write_history(finished=False)

    def _flush_history(self, finished: bool) -> None:
        if self._logged or not self._meaningful():
            return
        self._write_history(finished=finished)

    def _write_history(self, finished: bool) -> None:
        track = self.queue.current()
        if track is None or self._store is None:
            return
        self._logged = True
        try:
            self._store.log_play(track, listened=int(self._listened_ms / 1000),
                                 finished=finished)
        except Exception:
            logger.debug('Плеер: история прослушивания не записалась', exc_info=True)

    def _on_backend_ended(self) -> None:
        if self.sender() is not self._backend:
            return
        if not self._logged:
            # Трек доиграл до конца - это прослушивание независимо от порогов
            self._write_history(finished=True)
        if self._repeat == REPEAT_ONE:
            self._play_current()
            return
        if not self._advance_in_queue:
            self.stop()
            return
        self.next()

    def _on_backend_failed(self, message: str) -> None:
        if self.sender() is not self._backend:
            return
        logger.warning('Плеер: %s', message)
        track = self.queue.current()
        if track is not None and self._try_fallback(track, message):
            return
        self._set_state(STATE_ERROR)
        self.error.emit(message)

    def _try_fallback(self, track: Track, reason: str) -> bool:
        """Подменить источник тем же треком из другого места.

        VK не отдал файл - играем с YouTube, и наоборот. Меняем именно запись в
        очереди, чтобы «дальше» и «назад» продолжали работать."""
        if self._store is None:
            return False
        self._tried_uids.add(track.uid)
        candidates: list[Track] = []
        try:
            target = self._store.mapping_target(track.uid)
            if target is not None:
                candidates.append(target)
            candidates.extend(self._store.alternatives(track))
        except Exception:
            logger.debug('Плеер: замена источника не подобралась', exc_info=True)
            return False
        index = self.queue.index
        if index < 0:
            return False
        for candidate in candidates:
            if candidate.uid in self._tried_uids or not candidate.playable:
                continue
            if self._pick_backend(candidate) is None:
                continue
            logger.info('Плеер: %s не пошёл (%s), переключаюсь на %s',
                        track.uid, reason, candidate.uid)
            self.queue.replace(index, candidate)
            self.queue_changed.emit()
            self.notice.emit(f'Переключено на {candidate.source_label}')
            self._play_current()
            return True
        return False
