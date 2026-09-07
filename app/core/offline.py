"""Офлайн-копии треков «Моей музыки».

Кэш — это обычная загрузка, просто в свою папку: качает тот же DownloadManager,
что и всё остальное (второго загрузчика в приложении быть не должно). Отличие
только в адресе и в том, что о готовом файле мы сообщаем базе — трек получает
`local_path`, и плеер сам предпочтёт его сети.

Содержимое папки офлайна приложение считает своим: при превышении лимита самые
давние копии удаляются. Поэтому она отдельная — то, что человек скачал руками в
«музыку», не трогаем никогда."""
from __future__ import annotations

import logging
import os
import threading

from PySide6.QtCore import QObject, Signal

from .. import config
from .track import SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE, Track, to_vk_row

logger = logging.getLogger(__name__)


def offline_dir(settings: dict) -> str:
    """Папка офлайна из настроек; пустое значение — папка по умолчанию."""
    return str(settings.get('offline_dir') or config.OFFLINE_DIR)


def folder_size(path: str) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


class OfflineCache(QObject):
    """Кто из треков лежит на диске и что сейчас качается."""

    changed = Signal(str)        # uid — состояние офлайна изменилось
    failed = Signal(str, str)    # uid, текст ошибки

    def __init__(self, downloads, store, settings_provider, parent=None):
        super().__init__(parent)
        self._downloads = downloads
        self._store = store
        self._settings = settings_provider
        # item_id задачи → uid трека. Сигналы приходят из потока загрузки,
        # поэтому словарь под замком.
        self._jobs: dict[str, str] = {}
        self._lock = threading.RLock()
        downloads.item_finished.connect(self._on_finished)

    # ---------- состояние ----------
    def directory(self) -> str:
        return offline_dir(self._settings())

    def pending_uids(self) -> set[str]:
        with self._lock:
            return set(self._jobs.values())

    def is_pending(self, uid: str) -> bool:
        with self._lock:
            return uid in self._jobs.values()

    def owns(self, item_id: str) -> bool:
        """Наша ли это задача — чтобы очередь не считала её обычной загрузкой."""
        with self._lock:
            return item_id in self._jobs

    def usage(self) -> int:
        return folder_size(self.directory())

    # ---------- сохранение ----------
    def can_cache(self, track: Track) -> bool:
        """Файл с диска кэшировать не во что, а без ссылки качать нечего."""
        if track.source == SOURCE_LOCAL:
            return False
        if track.cached:
            return False
        return bool(track.url or track.youtube_id or track.vk_owner_id is not None)

    def ensure(self, tracks) -> int:
        """Поставить в очередь всё, чего ещё нет на диске. Возвращает, сколько встало."""
        started = 0
        for track in tracks:
            if not self.can_cache(track) or self.is_pending(track.uid):
                continue
            if self._enqueue(track):
                started += 1
        return started

    def _enqueue(self, track: Track) -> bool:
        directory = self.directory()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            logger.warning('offline: папка %s недоступна: %s', directory, exc)
            self.failed.emit(track.uid, 'Папка офлайна недоступна')
            return False

        # Офлайн — всегда звук в свою папку, что бы ни стояло на вкладке загрузок
        override = {'mode': 'audio', 'music_dir': directory, 'video_dir': directory}
        # Свой ключ истории: иначе «пропускать уже скачанное» решило бы, что
        # ролик скачан, и обычная загрузка того же трека молча не состоялась бы
        key = f'offline:{track.uid}'
        title = track.display_title
        try:
            if track.source == SOURCE_YOUTUBE:
                url = track.url or f'https://www.youtube.com/watch?v={track.youtube_id}'
                item = self._downloads.add_youtube(url, title, key, settings_override=override)
            elif track.source == SOURCE_VK:
                item = self._downloads.add_vk_track(to_vk_row(track), key,
                                                    settings_override=override)
            else:
                return False
        except Exception as exc:
            logger.exception('offline: не удалось поставить %s в очередь', track.uid)
            self.failed.emit(track.uid, str(exc))
            return False

        with self._lock:
            self._jobs[item.id] = track.uid
        self.changed.emit(track.uid)
        return True

    def _on_finished(self, item_id: str, success: bool, message: str) -> None:
        with self._lock:
            uid = self._jobs.pop(item_id, '')
        if not uid:
            return
        if success and message and os.path.exists(message):
            self._store.set_local_path(uid, message, cached=True)
            self.enforce_limit()
        else:
            self.failed.emit(uid, message or 'Не удалось сохранить офлайн')
        self.changed.emit(uid)

    # ---------- удаление ----------
    def remove(self, uid: str) -> None:
        """Убрать офлайн-копию. Чужой файл только отвязываем, но не удаляем."""
        track = self._store.get_track(uid)
        path = track.local_path if track else ''
        if path and self._is_ours(path):
            try:
                os.remove(path)
            except OSError as exc:
                logger.warning('offline: не удалось удалить %s: %s', path, exc)
        self._store.clear_local_path(uid)
        self.changed.emit(uid)

    def _is_ours(self, path: str) -> bool:
        """Файл лежит в папке офлайна — значит, копию делали мы."""
        try:
            root = os.path.normcase(os.path.abspath(self.directory()))
            target = os.path.normcase(os.path.abspath(path))
        except OSError:
            return False
        return target.startswith(root + os.sep)

    def enforce_limit(self) -> int:
        """Вычистить самые давние копии, если папка переросла лимит.

        Избранное не трогаем: его человек и просил держать под рукой."""
        limit_gb = float(self._settings().get('offline_limit_gb') or 0)
        if limit_gb <= 0:
            return 0
        limit = int(limit_gb * 1024 ** 3)
        used = self.usage()
        if used <= limit:
            return 0

        removed = 0
        for track in self._store.cached_tracks():
            if used <= limit:
                break
            if self._store.is_favorite(track.uid):
                continue
            path = track.local_path
            if not path or not self._is_ours(path):
                continue
            try:
                used -= os.path.getsize(path)
            except OSError:
                pass
            self.remove(track.uid)
            removed += 1
        if removed:
            logger.info('offline: освободили место, убрано копий: %d', removed)
        return removed
