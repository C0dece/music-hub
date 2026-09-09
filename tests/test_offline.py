"""Офлайн-копии: постановка в очередь, привязка файла, лимит и уборка."""
import os
import shutil
import tempfile
import unittest

from PySide6.QtCore import QObject, Signal

from app.core.offline import OfflineCache, folder_size
from app.core.store import Store
from app.core.track import Track
from tests.qt_app import qt_app


def yt(video_id='abc') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title='Numb', artist='Linkin Park',
                 url=f'https://www.youtube.com/watch?v={video_id}')


def vk(audio_id=10) -> Track:
    return Track(source='vk', source_id=f'1_{audio_id}', title='Faint',
                 artist='Linkin Park', vk_owner_id=1, vk_audio_id=audio_id)


class FakeItem:
    def __init__(self, item_id: str):
        self.id = item_id


class FakeDownloads(QObject):
    """Загрузчик, который ничего не качает, но помнит, о чём его просили."""

    item_finished = Signal(str, bool, str)

    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []

    def add_youtube(self, url, title, history_key, settings_override=None):
        return self._add('youtube', history_key, settings_override, url=url)

    def add_vk_track(self, track, history_key, settings_override=None):
        return self._add('vk', history_key, settings_override, track=track)

    def _add(self, source, history_key, override, **extra):
        item = FakeItem(f'item-{len(self.jobs)}')
        self.jobs.append({'id': item.id, 'source': source, 'key': history_key,
                          'override': override or {}, **extra})
        return item


class OfflineTests(unittest.TestCase):
    def setUp(self):
        qt_app()
        self.dir = tempfile.mkdtemp(prefix='offline-test-')
        handle, self.db = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        os.unlink(self.db)
        self.store = Store(self.db)
        self.settings = {'offline_dir': self.dir, 'offline_limit_gb': 0}
        self.downloads = FakeDownloads()
        self.cache = OfflineCache(self.downloads, self.store, lambda: self.settings)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def _file(self, name: str, size: int = 1024) -> str:
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as fh:
            fh.write(b'0' * size)
        return path

    # ---------- постановка в очередь ----------
    def test_enqueue_uses_offline_folder_and_own_history_key(self):
        track = yt()
        self.store.save_to_library(track)
        self.assertEqual(self.cache.ensure([track]), 1)

        job = self.downloads.jobs[0]
        self.assertEqual(job['override']['mode'], 'audio')
        self.assertEqual(job['override']['music_dir'], self.dir)
        self.assertEqual(job['override']['video_dir'], self.dir)
        # Свой ключ истории: обычная загрузка того же ролика не должна
        # посчитать его уже скачанным
        self.assertEqual(job['key'], f'offline:{track.uid}')
        self.assertEqual(self.cache.pending_uids(), {track.uid})
        self.assertTrue(self.cache.owns(job['id']))

    def test_second_request_does_not_duplicate_job(self):
        track = vk()
        self.store.save_to_library(track)
        self.cache.ensure([track])
        self.assertEqual(self.cache.ensure([track]), 0)
        self.assertEqual(len(self.downloads.jobs), 1)

    def test_local_file_and_cached_track_are_skipped(self):
        path = self._file('own.mp3')
        local = Track(source='local', source_id=os.path.normcase(path),
                      title='Numb', local_path=path)
        cached = yt('done').with_local_path(path)
        self.assertFalse(self.cache.can_cache(local))
        self.assertFalse(self.cache.can_cache(cached))
        self.assertEqual(self.cache.ensure([local, cached]), 0)

    # ---------- готовый файл ----------
    def test_finished_download_binds_file_to_track(self):
        track = yt()
        self.store.save_to_library(track)
        self.cache.ensure([track])
        path = self._file('numb.mp3')

        self.downloads.item_finished.emit(self.downloads.jobs[0]['id'], True, path)
        self.assertEqual(self.store.get_track(track.uid).local_path, path)
        self.assertEqual(self.store.cached_uids([track.uid]), {track.uid})
        self.assertEqual(self.cache.pending_uids(), set())

    def test_failed_download_leaves_track_alone(self):
        track = yt()
        self.store.save_to_library(track)
        self.cache.ensure([track])
        errors = []
        self.cache.failed.connect(lambda uid, text: errors.append((uid, text)))

        self.downloads.item_finished.emit(self.downloads.jobs[0]['id'], False, 'нет сети')
        self.assertEqual(errors, [(track.uid, 'нет сети')])
        self.assertEqual(self.store.cached_uids([track.uid]), set())
        self.assertEqual(self.cache.pending_uids(), set())

    def test_foreign_job_is_ignored(self):
        """Обычная загрузка мимо офлайна ничего в базе не меняет."""
        track = yt()
        self.store.save_to_library(track)
        self.downloads.item_finished.emit('чужая-задача', True, self._file('x.mp3'))
        self.assertEqual(self.store.cached_uids([track.uid]), set())

    # ---------- удаление ----------
    def test_remove_deletes_only_our_copy(self):
        track = yt()
        self.store.save_to_library(track)
        path = self._file('copy.mp3')
        self.store.set_local_path(track.uid, path, cached=True)

        self.cache.remove(track.uid)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.store.get_track(track.uid).local_path, '')

    def test_remove_keeps_file_downloaded_by_hand(self):
        """Файл из папки «музыка» приложение только отвязывает: его скачал
        человек, и удалять его нам нечего."""
        outside = tempfile.mkdtemp(prefix='music-test-')
        self.addCleanup(shutil.rmtree, outside, True)
        path = os.path.join(outside, 'numb.mp3')
        with open(path, 'wb') as fh:
            fh.write(b'0')
        track = yt()
        self.store.save_to_library(track)
        self.store.set_local_path(track.uid, path, cached=True)

        self.cache.remove(track.uid)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.store.get_track(track.uid).local_path, '')

    # ---------- лимит ----------
    def test_limit_evicts_oldest_but_spares_favorites(self):
        loved, old, new = yt('loved'), yt('old'), yt('new')
        paths = {}
        for track in (loved, old, new):
            self.store.save_to_library(track)
            paths[track.uid] = self._file(f'{track.youtube_id}.mp3', 4096)
            self.store.set_local_path(track.uid, paths[track.uid], cached=True)
        self.store.add_favorite(loved)
        # Порядок уборки - по времени копирования, а не по имени файла
        self.store._exec('UPDATE tracks SET cached_at = ? WHERE uid = ?',
                         (1.0, loved.uid))
        self.store._exec('UPDATE tracks SET cached_at = ? WHERE uid = ?', (2.0, old.uid))
        self.store._exec('UPDATE tracks SET cached_at = ? WHERE uid = ?', (3.0, new.uid))

        # Лимит меньше того, что уже лежит: одна копия должна уйти
        self.settings['offline_limit_gb'] = 8192 / 1024 ** 3
        self.assertEqual(self.cache.enforce_limit(), 1)
        self.assertTrue(os.path.exists(paths[loved.uid]))   # «Любимое» не трогаем
        self.assertFalse(os.path.exists(paths[old.uid]))    # самая давняя копия
        self.assertTrue(os.path.exists(paths[new.uid]))

    def test_no_limit_means_no_cleanup(self):
        track = yt()
        self.store.save_to_library(track)
        path = self._file('numb.mp3', 4096)
        self.store.set_local_path(track.uid, path, cached=True)
        self.settings['offline_limit_gb'] = 0
        self.assertEqual(self.cache.enforce_limit(), 0)
        self.assertTrue(os.path.exists(path))

    def test_folder_size_counts_subfolders(self):
        self._file('a.mp3', 100)
        nested = os.path.join(self.dir, 'вложенная')
        os.makedirs(nested)
        with open(os.path.join(nested, 'b.mp3'), 'wb') as fh:
            fh.write(b'0' * 50)
        self.assertEqual(folder_size(self.dir), 150)
        self.assertEqual(folder_size(os.path.join(self.dir, 'нет такой')), 0)


if __name__ == '__main__':
    unittest.main()
