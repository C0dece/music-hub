"""Перенос трека в «Мою музыку» VK — то, что делает кнопка «+ VK».

Порядок такой:

1. Уже переносили — берём готовую связку из базы и ничего не делаем.
2. Ищем трек в VK и, если нашлась подходящая запись, добавляем её к себе
   (`audio.add`). Это мгновенно и не тратит трафик.
3. Не нашли — качаем звук существующим загрузчиком и заливаем существующей
   очередью заливки. Второго загрузчика и второго заливщика здесь нет.

Сервис ничего не знает про окна: наружу идут сигналы, показывает их интерфейс.
Так же его сможет использовать будущий клиент на другой платформе."""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, Signal

from . import history, matcher
from .async_task import run_async
from .track import SOURCE_VK, Track, from_vk

logger = logging.getLogger(__name__)

# Состояния переноса — они же подписи на кнопке
STATE_SEARCHING = 'searching'
STATE_ASKING = 'asking'
STATE_ADDING = 'adding'
STATE_DOWNLOADING = 'downloading'
STATE_UPLOADING = 'uploading'
STATE_DONE = 'done'
STATE_ERROR = 'error'

STATE_LABELS = {
    STATE_SEARCHING: 'Ищу в VK…',
    STATE_ASKING: 'Выберите запись',
    STATE_ADDING: 'Добавляю…',
    STATE_DOWNLOADING: 'Скачиваю…',
    STATE_UPLOADING: 'Загружаю…',
    STATE_DONE: '✓ В VK',
    STATE_ERROR: 'Ошибка',
}


class VkImportService(QObject):
    """Очередь переносов «+ VK». На один трек — не больше одного переноса разом."""

    # uid трека, состояние (STATE_*), подпись для кнопки
    state_changed = Signal(str, str, str)
    # uid, получилось ли, текст для человека
    finished = Signal(str, bool, str)
    # uid, список (Track, оценка) — похожих записей несколько, нужен выбор человека
    ambiguous = Signal(str, object)

    def __init__(self, client_provider, uploader, download_manager,
                 store, settings_provider, parent=None):
        super().__init__(parent)
        self._client_provider = client_provider
        self._uploader = uploader
        self._manager = download_manager
        self._store = store
        self._settings = settings_provider

        # uid → что с ним сейчас происходит
        self._jobs: dict[str, dict] = {}
        # id задачи загрузчика → uid, путь файла → uid
        self._by_download: dict[str, str] = {}
        self._by_path: dict[str, str] = {}

        self._manager.item_finished.connect(self._on_download_finished)
        self._manager.item_status.connect(self._on_download_status)
        self._manager.item_progress.connect(self._on_download_progress)
        self._uploader.uploaded.connect(self._on_uploaded)
        self._uploader.uploaded_info.connect(self._on_upload_info)

    # ---------- состояние трека ----------
    def state_of(self, track: Track) -> str:
        """Что показывать на кнопке прямо сейчас, с учётом прошлых запусков."""
        if self._is_mine(track):
            return STATE_DONE
        if track.source != SOURCE_VK and track.in_vk:
            return STATE_DONE
        job = self._jobs.get(track.uid)
        if job:
            return job['state']
        if self._store is not None and self._store.has_mapping(track.uid):
            return STATE_DONE
        return ''

    def _is_mine(self, track: Track) -> bool:
        """Запись VK лежит в вашей музыке, если её владелец — вы.

        Треки из поиска и подборок тоже помечены `in_vk`: они в VK есть, но
        не у вас, и добавить их к себе можно."""
        if track.source != SOURCE_VK:
            return False
        client = self._client_provider()
        # Без входа и без известного владельца кнопке всё равно нечего делать
        mine = getattr(client, 'user_id', None) if client is not None else None
        if mine is None:
            return True
        try:
            return int(track.vk_owner_id or 0) == int(mine)
        except (TypeError, ValueError):
            return True

    def is_running(self, uid: str) -> bool:
        return uid in self._jobs

    # ---------- запуск ----------
    def add(self, track: Track) -> bool:
        """Начать перенос. False — если делать нечего или он уже идёт."""
        if track is None:
            return False
        uid = track.uid
        if self._is_mine(track):
            return False
        if track.source != SOURCE_VK and track.in_vk:
            return False
        if uid in self._jobs:
            logger.debug('«+ VK»: %s уже переносится', uid)
            return False
        if self._store is not None and self._store.has_mapping(uid):
            self._emit(uid, STATE_DONE)
            self.finished.emit(uid, True, 'Этот трек уже есть в вашей музыке VK')
            return False

        client = self._client_provider()
        if client is None:
            self.finished.emit(uid, False, 'Нет входа в VK, войдите на вкладке «Музыка VK»')
            return False

        if self._store is not None:
            self._store.save_track(track)
        self._jobs[uid] = {'track': track, 'state': '', 'client': client}

        if track.source == SOURCE_VK:
            # Запись уже в VK, просто не у вас: её достаточно добавить к себе,
            # искать похожее и тем более заливать файл незачем
            self._add_existing(uid, track)
            return True

        if self._settings().get('vk_match_first', True):
            self._search(uid)
        else:
            self._download(uid)
        return True

    def cancel(self, uid: str) -> None:
        """Забыть о переносе. Уже начатую заливку VK всё равно не прервать."""
        self._jobs.pop(uid, None)

    # ---------- поиск готовой записи ----------
    def _search(self, uid: str) -> None:
        job = self._jobs.get(uid)
        if job is None:
            return
        track = job['track']
        client = job['client']
        self._emit(uid, STATE_SEARCHING)
        query = ' '.join(part for part in (track.artist, track.title) if part).strip()
        if not query:
            self._download(uid)
            return

        def search():
            return client.search_tracks(query, limit=40)

        def on_done(rows, error):
            if uid not in self._jobs:
                return  # перенос отменили, пока искали
            if error:
                logger.warning('«+ VK»: поиск не удался (%s)', error)
                self._download(uid)
                return
            candidates = [from_vk(row) for row in (rows or [])]
            result = matcher.match(track, candidates)
            if result.confident and result.best is not None:
                self._add_existing(uid, result.best)
                return
            if (result.candidates and self._settings().get('vk_ask_on_ambiguous', True)):
                self._jobs[uid]['candidates'] = result.candidates
                self._emit(uid, STATE_ASKING)
                self.ambiguous.emit(uid, result.candidates)
                return
            self._download(uid)

        run_async(search, on_done)

    def choose(self, uid: str, track: Track | None) -> None:
        """Ответ на сигнал `ambiguous`: выбранная запись VK или None — «качать»."""
        if uid not in self._jobs:
            return
        if track is None:
            self._download(uid)
        else:
            self._add_existing(uid, track)

    def _add_existing(self, uid: str, found: Track) -> None:
        job = self._jobs.get(uid)
        if job is None:
            return
        client = job['client']
        self._emit(uid, STATE_ADDING)

        def add():
            return client.add_audio(found.vk_owner_id, found.vk_audio_id, found.vk_access_key)

        def on_done(saved, error):
            if uid not in self._jobs:
                return
            if error:
                logger.warning('«+ VK»: audio.add не прошёл (%s)', error)
                if self._settings().get('vk_upload_fallback', True):
                    self._download(uid)
                else:
                    self._fail(uid, str(error))
                return
            mapped = found
            if isinstance(saved, dict) and saved.get('id'):
                # VK кладёт к себе копию записи — сохраняем именно её координаты
                mapped = from_vk({'id': saved['id'], 'owner_id': saved.get('owner_id'),
                                  'artist': found.artist, 'title': found.title,
                                  'duration': found.duration, 'cover': found.cover})
            self._succeed(uid, mapped, 'match')

        run_async(add, on_done)

    # ---------- скачать и залить ----------
    def _download(self, uid: str) -> None:
        job = self._jobs.get(uid)
        if job is None:
            return
        track = job['track']
        if not self._settings().get('vk_upload_fallback', True):
            self._fail(uid, 'В VK такого трека нет, а загрузка своим файлом выключена')
            return
        if track.cached:
            self._upload(uid, track.local_path)
            return
        if not track.url:
            self._fail(uid, 'Нечего скачивать: у трека нет ссылки на источник')
            return

        self._emit(uid, STATE_DOWNLOADING)
        key = history.key_for(track.source, track.source_id or track.youtube_id, 'audio')
        title = track.display_title
        # Скачиваем звук, что бы ни стояло на вкладке «Загрузки»: в музыку VK
        # видео не положишь
        item = self._manager.add_youtube(track.url, title, key, settings_override={'mode': 'audio'})
        job['download_id'] = item.id
        self._by_download[item.id] = uid

    def _on_download_status(self, item_id: str, status: str) -> None:
        uid = self._by_download.get(item_id)
        if uid in self._jobs:
            self._emit(uid, STATE_DOWNLOADING, f'{status}…')

    def _on_download_progress(self, item_id: str, percent: float, _speed: str) -> None:
        uid = self._by_download.get(item_id)
        if uid in self._jobs and percent > 0:
            self._emit(uid, STATE_DOWNLOADING, f'Скачиваю {int(percent)}%')

    def _on_download_finished(self, item_id: str, ok: bool, payload: str) -> None:
        uid = self._by_download.pop(item_id, None)
        if uid is None or uid not in self._jobs:
            return
        if not ok or not payload or not os.path.exists(payload):
            self._fail(uid, payload or 'Скачать не удалось')
            return
        self._upload(uid, payload)

    def _upload(self, uid: str, path: str) -> None:
        job = self._jobs.get(uid)
        if job is None:
            return
        track = job['track']
        job['path'] = path
        self._by_path[path] = uid
        self._emit(uid, STATE_UPLOADING)
        # Очередь сама пропускает файл, который в ней уже стоит
        self._uploader.add([path], artist=track.artist, title=track.title)

    def _on_upload_info(self, path: str, saved) -> None:
        uid = self._by_path.get(path)
        if uid in self._jobs and isinstance(saved, dict):
            self._jobs[uid]['saved'] = saved

    def _on_uploaded(self, path: str, ok: bool, error: str) -> None:
        uid = self._by_path.pop(path, None)
        if uid is None or uid not in self._jobs:
            return
        if not ok:
            self._fail(uid, error or 'VK не принял файл')
            return
        job = self._jobs[uid]
        track = job['track']
        saved = job.get('saved') or {}
        mapped = None
        if saved.get('id'):
            mapped = from_vk({'id': saved.get('id'), 'owner_id': saved.get('owner_id'),
                              'artist': saved.get('artist') or track.artist,
                              'title': saved.get('title') or track.title,
                              'duration': track.duration})
        # Координат VK может и не быть — тогда запоминаем хотя бы сам факт переноса
        self._succeed(uid, mapped, 'upload')

    # ---------- итоги ----------
    def _succeed(self, uid: str, vk_track: Track | None, method: str) -> None:
        job = self._jobs.pop(uid, None)
        if self._store is not None:
            try:
                self._store.set_mapping(uid, vk_track, method)
                if vk_track is not None:
                    self._store.save_track(vk_track)
            except Exception:
                logger.exception('«+ VK»: связку не удалось сохранить')
        title = job['track'].display_title if job else uid
        self._emit(uid, STATE_DONE)
        self.finished.emit(uid, True, f'{title}: в музыке VK')

    def _fail(self, uid: str, message: str) -> None:
        job = self._jobs.pop(uid, None)
        title = job['track'].display_title if job else uid
        logger.warning('«+ VK»: %s: %s', title, message)
        self._emit(uid, STATE_ERROR, message)
        self.finished.emit(uid, False, message)

    def _emit(self, uid: str, state: str, label: str = '') -> None:
        job = self._jobs.get(uid)
        if job is not None:
            job['state'] = state
        self.state_changed.emit(uid, state, label or STATE_LABELS.get(state, ''))
