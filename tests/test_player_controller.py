"""Поведение плеера: перемешивание, повтор, автопродолжение, история, сеанс.

Звук здесь не играет: вместо настоящего движка подставлен фальшивый, который
только запоминает, что его просили включить. Так проверяется именно логика
контроллера, а не QtMultimedia.
"""
import os
import shutil
import tempfile
import threading
import time
import unittest

from PySide6.QtCore import QThreadPool

from .qt_app import qt_app

from app.core import player_controller as pc
from app.core.player_controller import PlaybackBackend, PlayerController
from app.core.store import Store
from app.core.track import Track


def yt(video_id: str, title: str = '') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title=title or video_id, artist='Кто-то', duration=200,
                 url=f'https://www.youtube.com/watch?v={video_id}')


def local(path: str = 'C:/music/x.mp3') -> Track:
    return Track(source='local', source_id=path, title='x', local_path=path)


class FakeBackend(PlaybackBackend):
    """Движок-пустышка: играет всё, что не забрал QtMediaBackend."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.played: list[str] = []

    def can_play(self, track):
        return True

    def play(self, track):
        self.played.append(track.uid)
        self.state_changed.emit(pc.STATE_PLAYING)


class PickyBackend(PlaybackBackend):
    """Движок, который берёт не всё: так видно, кому достался трек."""

    def __init__(self, accepts, parent=None):
        super().__init__(parent)
        self._accepts = accepts

    def can_play(self, track):
        return self._accepts(track)

    def play(self, track):
        self.state_changed.emit(pc.STATE_PLAYING)


class ControllerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        os.unlink(self.db_path)
        self.store = Store(self.db_path)
        self.player = PlayerController(lambda: None, self.store)
        self.backend = FakeBackend()
        self.player.add_backend(self.backend)

    def settle(self, rounds: int = 6, until=None) -> None:
        """Дождаться фоновых задач: рекомендации уходят в пул потоков.

        Пул глобальный и общий на весь прогон, поэтому `waitForDone` ждёт не нашу
        задачу, а вообще все — включая чужие сетевые, застрявшие на таймауте другого
        теста. Тогда ожидание истекало впустую, и тест падал не по своей вине.
        Ждём короткими шагами и выходим, как только пришло нужное нам."""
        if until is None:
            # Ждать нечего конкретного — как раньше: пул опустел, значит всё сделано
            for _round in range(rounds):
                QThreadPool.globalInstance().waitForDone(2000)
                self.app.processEvents()
            return
        # Есть чёткий признак готовности — ждём именно его. Запас щедрый нарочно:
        # при успехе выходим сразу, поэтому длинный предел ничего не замедляет,
        # зато переживает занятый чужими задачами пул
        deadline = time.monotonic() + rounds * 10.0
        while time.monotonic() < deadline:
            QThreadPool.globalInstance().waitForDone(50)
            self.app.processEvents()
            if until():
                return
        self.app.processEvents()

    def tearDown(self):
        self.player.stop()
        self.store.close()
        self.app.processEvents()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class ShuffleTests(ControllerTestCase):
    def test_shuffle_keeps_current_track(self):
        tracks = [yt(f'v{i}') for i in range(10)]
        self.player.play_tracks(tracks, 3)
        current = self.player.current
        self.player.set_shuffle(True)
        # Включение перемешивания не должно перекидывать на другую песню
        self.assertIs(self.player.current, current)
        self.assertEqual(self.backend.played[-1], current.uid)

    def test_shuffle_off_restores_order(self):
        tracks = [yt(f'v{i}') for i in range(10)]
        self.player.play_tracks(tracks, 0)
        self.player.set_shuffle(True)
        self.player.next()
        self.player.set_shuffle(False)
        # Логический порядок вернулся целиком, очередь не потерялась
        self.assertEqual([t.uid for t in self.player.queue.tracks],
                         [t.uid for t in tracks])
        self.assertIsNotNone(self.player.current)

    def test_shuffle_visits_every_track_once(self):
        tracks = [yt(f'v{i}') for i in range(6)]
        self.player.play_tracks(tracks, 0)
        self.player.set_shuffle(True)
        seen = {self.player.current.uid}
        for _step in range(5):
            self.player.next()
            seen.add(self.player.current.uid)
        self.assertEqual(len(seen), 6)


class RepeatTests(ControllerTestCase):
    def test_repeat_one_replays_same_track(self):
        self.player.play_tracks([yt('a'), yt('b')], 0)
        self.player.set_repeat(pc.REPEAT_ONE)
        self.player._on_backend_ended()
        self.assertEqual(self.player.current.uid, yt('a').uid)

    def test_repeat_all_wraps_to_start(self):
        self.player.play_tracks([yt('a'), yt('b')], 1)
        self.player.set_repeat(pc.REPEAT_ALL)
        self.player.next()
        self.assertEqual(self.player.current.uid, yt('a').uid)

    def test_repeat_off_stops_at_end(self):
        self.player.set_autoplay(False)
        self.player.play_tracks([yt('a')], 0)
        self.player.next()
        self.assertEqual(self.player.state, pc.STATE_STOPPED)

    def test_cycle_repeat_goes_through_all_modes(self):
        seen = [self.player.repeat]
        for _step in range(3):
            self.player.cycle_repeat()
            seen.append(self.player.repeat)
        self.assertEqual(seen, [pc.REPEAT_OFF, pc.REPEAT_ALL, pc.REPEAT_ONE,
                                pc.REPEAT_OFF])


class AutoplayTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.calls: list[dict] = []

    def _recommender(self, seed, exclude, limit):
        self.calls.append({'seed': seed, 'exclude': set(exclude or ()), 'limit': limit})
        index = len(self.calls)
        return [yt(f'rec{index}_{i}') for i in range(3)]

    def test_tail_triggers_one_request(self):
        self.player.set_recommender(self._recommender)
        self.player.play_tracks([yt('a')], 0)
        self.settle(until=lambda: self.calls)
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(len(self.player.queue), 1)

    def test_no_parallel_requests(self):
        self.player.set_recommender(self._recommender)
        self.player.play_tracks([yt('a')], 0)
        for _click in range(5):
            self.player._maybe_extend()
        self.settle(until=lambda: self.calls)
        # Пять нажатий подряд не должны превратиться в пять запросов
        self.assertEqual(len(self.calls), 1)

    def test_queue_uids_are_excluded(self):
        self.player.set_recommender(self._recommender)
        self.player.play_tracks([yt('a'), yt('b')], 0)
        self.settle(until=lambda: self.calls)
        self.assertTrue(self.calls)
        self.assertIn(yt('a').uid, self.calls[0]['exclude'])
        self.assertIn(yt('b').uid, self.calls[0]['exclude'])

    def test_recommendations_are_not_duplicated(self):
        self.player.set_recommender(lambda seed, exclude, limit: [yt('same')])
        self.player.play_tracks([yt('a')], 0)
        self.settle()
        self.player._maybe_extend()
        self.settle()
        uids = [t.uid for t in self.player.queue.tracks]
        self.assertEqual(len(uids), len(set(uids)))

    def test_autoplay_off_asks_nothing(self):
        self.player.set_recommender(self._recommender)
        self.player.set_autoplay(False)
        self.player.play_tracks([yt('a')], 0)
        self.settle()
        self.assertEqual(self.calls, [])

    def test_stale_answer_is_dropped(self):
        released = threading.Event()

        def slow(seed, exclude, limit):
            released.wait(5)
            return [yt('late')]

        self.player.set_recommender(slow)
        self.player.play_tracks([yt('a')], 0)
        before = [t.uid for t in self.player.queue.tracks]
        # Пока запрос в пути, очередь сменилась — ответ уже не про неё
        self.player._rec_token += 1
        released.set()
        self.settle()
        self.assertEqual([t.uid for t in self.player.queue.tracks], before)


class HistoryTests(ControllerTestCase):
    def _listen(self, seconds: int, duration: int = 200):
        self.player.set_autoplay(False)
        self.player.play_tracks([yt('a')], 0)
        # Движок сообщает позицию шагами: прослушанное считается по приросту
        for step in range(1, seconds + 1):
            self.backend.position_changed.emit(step * 1000, duration * 1000)

    def test_second_of_playback_is_not_history(self):
        self._listen(1)
        self.player.stop()
        self.assertEqual(self.store.history(10), [])

    def test_thirty_seconds_counts(self):
        self._listen(31)
        self.player.stop()
        self.assertEqual(len(self.store.history(10)), 1)

    def test_share_of_short_track_counts(self):
        # Пятая часть минутной песни — это уже прослушивание
        self._listen(13, duration=60)
        self.player.stop()
        self.assertEqual(len(self.store.history(10)), 1)

    def test_track_is_logged_once(self):
        self._listen(40)
        self.player._maybe_log_history()
        self.player.stop()
        self.assertEqual(len(self.store.history(10)), 1)


class SessionTests(ControllerTestCase):
    def test_roundtrip(self):
        self.player.set_autoplay(False)   # иначе очередь дополнится рекомендациями
        self.player.play_tracks([yt('a'), yt('b'), yt('c')], 1)
        self.player.set_repeat(pc.REPEAT_ALL)
        self.player.set_mode(pc.MODE_AUDIO)
        self.player.set_shuffle(True)
        self.player._position = 108000
        self.player.save_state()

        other = PlayerController(lambda: None, self.store)
        other.add_backend(FakeBackend())
        other.restore_state()
        try:
            self.assertEqual(other.repeat, pc.REPEAT_ALL)
            self.assertFalse(other.autoplay)
            self.assertEqual(other.mode, pc.MODE_AUDIO)
            self.assertTrue(other.shuffle)
            self.assertEqual(other.position, 108000)
            self.assertEqual(other.current.uid, self.player.current.uid)
            # Восстановление не должно само включать звук
            self.assertEqual(other.state, pc.STATE_STOPPED)
        finally:
            other.stop()

    def test_broken_session_does_not_crash(self):
        self.store.set_json('playback_session', {'queue': 'мусор', 'repeat': 42})
        self.player.restore_state()
        self.assertIsNone(self.player.current)

    def test_old_format_with_bare_queue(self):
        self.player.set_autoplay(False)
        self.player.play_tracks([yt('a')], 0)
        state = self.player.queue.to_state()
        self.store.set_json('playback_session', None)
        self.store.set_json('queue', state)
        other = PlayerController(lambda: None, self.store)
        other.add_backend(FakeBackend())
        other.restore_state()
        try:
            self.assertIsNotNone(other.current)
        finally:
            other.stop()


class ModeTests(ControllerTestCase):
    def test_auto_shows_video_for_youtube_only(self):
        self.player.set_mode(pc.MODE_AUTO)
        self.assertTrue(self.player.video_expected(yt('a')))
        self.assertFalse(self.player.video_expected(local()))

    def test_audio_mode_hides_video(self):
        self.player.set_mode(pc.MODE_AUDIO)
        self.assertFalse(self.player.video_expected(yt('a')))

    def test_video_mode_shows_video_for_any_source(self):
        self.player.set_mode(pc.MODE_VIDEO)
        self.assertTrue(self.player.video_expected(local()))

    def test_mode_switch_does_not_restart_playback(self):
        self.player.set_autoplay(False)
        self.player.play_tracks([yt('a')], 0)
        played = len(self.backend.played)
        self.player.set_mode(pc.MODE_AUDIO)
        self.player.set_mode(pc.MODE_VIDEO)
        # Переключение режима — это только показ, трек заново не включается
        self.assertEqual(len(self.backend.played), played)


class BackendChoiceTests(ControllerTestCase):
    """Офлайн-копия ролика — это только звук, и картинку по ней не показать."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='offline-pick-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file_backend = PickyBackend(lambda t: bool(t.local_path))
        self.web_backend = PickyBackend(lambda t: t.source == 'youtube')
        self.player._backends = [self.file_backend, self.web_backend]

    def _copy(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as fh:
            fh.write(b'0')
        return path

    def test_audio_mode_plays_the_copy(self):
        self.player.set_mode(pc.MODE_AUDIO)
        track = yt('a').with_local_path(self._copy('a.mp3'))
        self.assertIs(self.player._pick_backend(track), self.file_backend)

    def test_video_mode_goes_back_to_the_site(self):
        self.player.set_mode(pc.MODE_VIDEO)
        track = yt('a').with_local_path(self._copy('a.mp3'))
        self.assertIs(self.player._pick_backend(track), self.web_backend)

    def test_video_copy_still_plays_from_disk(self):
        """Скачали именно видео — показывать его из сети незачем."""
        self.player.set_mode(pc.MODE_VIDEO)
        track = yt('a').with_local_path(self._copy('a.mp4'))
        self.assertIs(self.player._pick_backend(track), self.file_backend)

    def test_vk_copy_is_never_sent_to_the_web(self):
        self.player.set_mode(pc.MODE_VIDEO)
        track = Track(source='vk', source_id='1_10', title='Numb',
                      vk_owner_id=1, vk_audio_id=10,
                      local_path=self._copy('vk.mp3'))
        self.assertIs(self.player._pick_backend(track), self.file_backend)


class VolumeTests(ControllerTestCase):
    def test_mute_restores_previous_volume(self):
        self.player.set_volume(63)
        self.player.toggle_mute()
        self.assertTrue(self.player.muted)
        self.assertEqual(self.player.volume, 0)
        self.player.toggle_mute()
        self.assertEqual(self.player.volume, 63)

    def test_volume_is_clamped(self):
        self.player.set_volume(300)
        self.assertEqual(self.player.volume, 100)
        self.player.set_volume(-10)
        self.assertEqual(self.player.volume, 0)


if __name__ == '__main__':
    unittest.main()
