"""Кнопка «+ VK»: поиск готовой записи, добавление, а если не нашлось — заливка.

Настоящий VK здесь не трогается: клиент, очередь заливки и загрузчик — заглушки.
Проверяем ровно то, что обещано пользователю: сначала ищем, добавляем найденное,
качаем только когда искать нечего, и один и тот же трек не переносим дважды.
"""
import os
import tempfile
import unittest

from PySide6.QtCore import QObject, QThreadPool, Signal

from app.core import vk_import
from app.core.store import Store
from app.core.track import Track

from .qt_app import qt_app


def yt(video_id='abc', title='Numb', artist='Linkin Park') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title=title, artist=artist, duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


def vk_row(audio_id=10, title='Numb', artist='Linkin Park', duration=186) -> dict:
    return {'id': audio_id, 'owner_id': 1, 'artist': artist, 'title': title,
            'duration': duration, 'access_key': 'k'}


class FakeClient:
    """Столько, сколько от VK нужно сервису переноса."""

    def __init__(self, rows=None, add_result=None, search_error=None, add_error=None):
        self.rows = rows or []
        self.add_result = add_result
        self.search_error = search_error
        self.add_error = add_error
        self.searched = []
        self.added = []

    def search_tracks(self, query, limit=40):
        self.searched.append(query)
        if self.search_error:
            raise self.search_error
        return list(self.rows)

    def add_audio(self, owner_id, audio_id, access_key=None):
        self.added.append((owner_id, audio_id, access_key))
        if self.add_error:
            raise self.add_error
        return self.add_result


class FakeUploader(QObject):
    uploaded = Signal(str, bool, str)
    uploaded_info = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.calls = []

    def add(self, paths, artist='', title=''):
        self.calls.append((list(paths), artist, title))


class FakeItem:
    def __init__(self, item_id):
        self.id = item_id


class FakeManager(QObject):
    item_finished = Signal(str, bool, str)
    item_status = Signal(str, str)
    item_progress = Signal(str, float, str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self._next = 0

    def add_youtube(self, url, title, key, settings_override=None):
        self._next += 1
        self.calls.append({'url': url, 'title': title, 'key': key,
                           'override': settings_override or {}})
        return FakeItem(f'dl{self._next}')


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        os.unlink(self.db_path)
        self.store = Store(self.db_path)
        self.uploader = FakeUploader()
        self.manager = FakeManager()
        self.client = FakeClient()
        self.settings = {'vk_match_first': True, 'vk_ask_on_ambiguous': True,
                         'vk_upload_fallback': True}
        self.service = vk_import.VkImportService(
            lambda: self.client, self.uploader, self.manager,
            self.store, lambda: self.settings)
        self.states = []
        self.results = []
        self.asked = []
        self.service.state_changed.connect(
            lambda uid, state, label: self.states.append(state))
        self.service.finished.connect(
            lambda uid, ok, text: self.results.append((uid, ok, text)))
        self.service.ambiguous.connect(
            lambda uid, cands: self.asked.append((uid, cands)))

    def tearDown(self):
        QThreadPool.globalInstance().waitForDone(5000)
        self.pump()
        self.store.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def pump(self, times=40):
        """Фоновая задача отдаёт результат сигналом — его нужно прокрутить."""
        for _ in range(times):
            QThreadPool.globalInstance().waitForDone(100)
            self.app.processEvents()

    def wait_for(self, check, times=60):
        for _ in range(times):
            if check():
                return True
            QThreadPool.globalInstance().waitForDone(100)
            self.app.processEvents()
        return check()

    # ---------- готовая запись нашлась ----------
    def test_confident_match_is_added_without_download(self):
        self.client.rows = [vk_row()]
        self.client.add_result = {'id': 99, 'owner_id': 555}
        track = yt()
        self.assertTrue(self.service.add(track))
        self.assertTrue(self.wait_for(lambda: self.results))

        uid, ok, _ = self.results[0]
        self.assertEqual(uid, track.uid)
        self.assertTrue(ok)
        self.assertEqual(self.client.added, [(1, 10, 'k')])
        self.assertEqual(self.manager.calls, [], 'качать было незачем')
        mapping = self.store.get_mapping(track.uid)
        self.assertEqual(mapping['method'], 'match')
        # VK кладёт к себе копию — запоминаем именно её координаты
        self.assertEqual(self.store.mapping_target(track.uid).vk_audio_id, 99)
        self.assertIn(vk_import.STATE_ADDING, self.states)
        self.assertEqual(self.states[-1], vk_import.STATE_DONE)

    def test_state_of_after_restart(self):
        """Связка лежит в базе — кнопка обязана сразу показывать «✓ В VK»."""
        track = yt()
        self.store.save_track(track)
        self.store.set_mapping(track.uid, None, 'upload')
        fresh = vk_import.VkImportService(
            lambda: self.client, self.uploader, self.manager,
            self.store, lambda: self.settings)
        self.assertEqual(fresh.state_of(track), vk_import.STATE_DONE)
        self.assertFalse(fresh.add(track), 'второй раз переносить нечего')
        self.assertEqual(self.client.searched, [])

    # ---------- ничего не нашлось ----------
    def test_no_match_downloads_and_uploads(self):
        self.client.rows = [vk_row(title='Совсем другое', artist='Другой')]
        track = yt()
        self.assertTrue(self.service.add(track))
        self.assertTrue(self.wait_for(lambda: self.manager.calls))

        call = self.manager.calls[0]
        self.assertEqual(call['url'], track.url)
        self.assertEqual(call['override'].get('mode'), 'audio',
                         'в музыку VK видео не положишь')
        self.assertEqual(self.client.added, [])

        handle, path = tempfile.mkstemp(suffix='.mp3')
        os.close(handle)
        try:
            self.manager.item_finished.emit('dl1', True, path)
            self.pump(3)
            self.assertEqual(self.uploader.calls[0][0], [path])
            self.uploader.uploaded_info.emit(path, {'id': 77, 'owner_id': 555})
            self.uploader.uploaded.emit(path, True, '')
            self.pump(3)
        finally:
            os.unlink(path)

        self.assertTrue(self.results[0][1])
        self.assertEqual(self.store.get_mapping(track.uid)['method'], 'upload')
        self.assertEqual(self.store.mapping_target(track.uid).vk_audio_id, 77)
        self.assertIn(vk_import.STATE_DOWNLOADING, self.states)
        self.assertIn(vk_import.STATE_UPLOADING, self.states)

    def test_upload_failure_is_reported(self):
        self.client.rows = []
        track = yt()
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.manager.calls))

        handle, path = tempfile.mkstemp(suffix='.mp3')
        os.close(handle)
        try:
            self.manager.item_finished.emit('dl1', True, path)
            self.pump(3)
            self.uploader.uploaded.emit(path, False, 'VK отказал')
            self.pump(3)
        finally:
            os.unlink(path)

        self.assertFalse(self.results[0][1])
        self.assertEqual(self.states[-1], vk_import.STATE_ERROR)
        self.assertFalse(self.store.has_mapping(track.uid),
                         'неудачу нельзя записывать как перенос')

    def test_download_failure_is_reported(self):
        self.client.rows = []
        track = yt()
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.manager.calls))
        self.manager.item_finished.emit('dl1', False, 'Скачать не удалось')
        self.pump(3)
        self.assertFalse(self.results[0][1])
        self.assertFalse(self.store.has_mapping(track.uid))

    # ---------- спорный случай ----------
    def test_ambiguous_asks_and_choice_adds(self):
        self.client.rows = [vk_row(audio_id=10, title='Song', artist='Artist',
                                   duration=200),
                            vk_row(audio_id=11, title='Song', artist='Artist',
                                   duration=201)]
        self.client.add_result = {'id': 90, 'owner_id': 555}
        track = Track(source='youtube', source_id='q1', youtube_id='q1',
                      title='Song', artist='Artist', duration=200,
                      url='https://www.youtube.com/watch?v=q1')
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.asked or self.results))

        if self.asked:
            uid, candidates = self.asked[0]
            self.assertEqual(uid, track.uid)
            self.assertGreaterEqual(len(candidates), 2)
            chosen = candidates[1][0] if isinstance(candidates[1], tuple) else candidates[1]
            self.service.choose(uid, chosen)
            self.assertTrue(self.wait_for(lambda: self.results))
        self.assertTrue(self.results[0][1])
        self.assertEqual(self.manager.calls, [])

    def test_ambiguous_choice_none_downloads(self):
        self.settings['vk_ask_on_ambiguous'] = True
        self.client.rows = [vk_row(audio_id=10, title='Song', artist='Artist',
                                   duration=200),
                            vk_row(audio_id=11, title='Song', artist='Artist',
                                   duration=201)]
        track = Track(source='youtube', source_id='q2', youtube_id='q2',
                      title='Song', artist='Artist', duration=200,
                      url='https://www.youtube.com/watch?v=q2')
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.asked or self.results))
        if self.asked:
            self.service.choose(self.asked[0][0], None)
            self.assertTrue(self.wait_for(lambda: self.manager.calls))
            self.assertTrue(self.manager.calls)

    # ---------- отказы и настройки ----------
    def test_second_click_does_not_start_second_job(self):
        self.client.rows = [vk_row()]
        self.client.add_result = {'id': 99, 'owner_id': 555}
        track = yt()
        self.assertTrue(self.service.add(track))
        self.assertFalse(self.service.add(track), 'перенос уже идёт')
        self.assertTrue(self.wait_for(lambda: self.results))
        self.assertEqual(len(self.client.added), 1)

    def test_no_client_no_silent_success(self):
        service = vk_import.VkImportService(
            lambda: None, self.uploader, self.manager, self.store,
            lambda: self.settings)
        seen = []
        service.finished.connect(lambda uid, ok, text: seen.append((ok, text)))
        track = yt()
        self.assertFalse(service.add(track))
        self.assertFalse(seen[0][0])
        self.assertIn('VK', seen[0][1])
        self.assertFalse(self.store.has_mapping(track.uid))

    def test_own_vk_track_is_not_transferred(self):
        self.client.user_id = 1
        track = Track(source='vk', source_id='1_5', title='Numb',
                      artist='Linkin Park', duration=186,
                      vk_owner_id=1, vk_audio_id=5)
        self.assertEqual(self.service.state_of(track), vk_import.STATE_DONE)
        self.assertFalse(self.service.add(track))

    def test_someone_elses_vk_track_is_added_to_your_music(self):
        """Запись из поиска VK уже в VK, но не у вас: её добавляют напрямую."""
        self.client.user_id = 1
        track = Track(source='vk', source_id='-77_5', title='Numb',
                      artist='Linkin Park', duration=186,
                      vk_owner_id=-77, vk_audio_id=5, vk_access_key='k')
        self.assertEqual(self.service.state_of(track), '')
        self.assertTrue(self.service.add(track))
        self.assertTrue(self.wait_for(lambda: self.client.added))
        self.assertEqual(self.client.searched, [], 'искать похожее незачем')
        self.assertEqual(self.manager.calls, [], 'качать и заливать незачем')

    def test_match_first_off_goes_straight_to_download(self):
        self.settings['vk_match_first'] = False
        self.client.rows = [vk_row()]
        track = yt()
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.manager.calls))
        self.assertEqual(self.client.searched, [], 'искать не просили')

    def test_upload_fallback_off_fails_honestly(self):
        self.settings['vk_upload_fallback'] = False
        self.client.rows = []
        track = yt()
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.results))
        self.assertFalse(self.results[0][1])
        self.assertEqual(self.manager.calls, [])
        self.assertFalse(self.store.has_mapping(track.uid))

    def test_search_error_falls_back_to_download(self):
        self.client.search_error = RuntimeError('VK не ответил')
        track = yt()
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.manager.calls))
        self.assertTrue(self.manager.calls)

    def test_add_error_falls_back_to_download(self):
        self.client.rows = [vk_row()]
        self.client.add_error = RuntimeError('audio.add отказал')
        track = yt()
        self.service.add(track)
        self.assertTrue(self.wait_for(lambda: self.manager.calls))
        self.assertTrue(self.manager.calls)


if __name__ == '__main__':
    unittest.main()
