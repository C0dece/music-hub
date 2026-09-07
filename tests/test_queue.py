"""Очередь проигрывания: она своя и с очередью загрузок не связана."""
import unittest

from app.core.playback_queue import PlaybackQueue
from app.core.track import Track


def t(n: int) -> Track:
    return Track(source='youtube', source_id=f'v{n}', youtube_id=f'v{n}',
                 title=f'Трек {n}', artist='Исполнитель', duration=180)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.q = PlaybackQueue()

    def test_empty(self):
        self.assertFalse(self.q)
        self.assertIsNone(self.q.current())
        self.assertFalse(self.q.has_next())
        self.assertFalse(self.q.has_previous())
        self.assertIsNone(self.q.go_next())

    def test_set_and_move(self):
        self.q.set_tracks([t(1), t(2), t(3)], start=1)
        self.assertEqual(self.q.current().source_id, 'v2')
        self.assertEqual(self.q.go_next().source_id, 'v3')
        self.assertFalse(self.q.has_next())
        self.assertEqual(self.q.go_previous().source_id, 'v2')

    def test_start_out_of_range(self):
        self.q.set_tracks([t(1), t(2)], start=99)
        self.assertEqual(self.q.current().source_id, 'v2')

    def test_append_and_insert_next(self):
        self.q.set_tracks([t(1), t(2)])
        self.q.append(t(3))
        self.q.insert_next([t(4)])
        self.assertEqual([x.source_id for x in self.q.tracks],
                         ['v1', 'v4', 'v2', 'v3'])
        self.assertEqual(self.q.current().source_id, 'v1')

    def test_remove_before_current_keeps_track(self):
        self.q.set_tracks([t(1), t(2), t(3)], start=2)
        self.q.remove(0)
        self.assertEqual(self.q.current().source_id, 'v3')

    def test_remove_current_moves_to_next(self):
        self.q.set_tracks([t(1), t(2), t(3)], start=1)
        self.q.remove(1)
        self.assertEqual(self.q.current().source_id, 'v3')

    def test_remove_last_current(self):
        self.q.set_tracks([t(1), t(2)], start=1)
        self.q.remove(1)
        self.assertEqual(self.q.current().source_id, 'v1')

    def test_remove_uid(self):
        self.q.set_tracks([t(1), t(2)])
        self.q.remove_uid(t(2).uid)
        self.assertEqual([x.source_id for x in self.q.tracks], ['v1'])

    def test_move_keeps_playing_track(self):
        self.q.set_tracks([t(1), t(2), t(3)], start=0)
        self.q.move(2, 0)
        self.assertEqual([x.source_id for x in self.q.tracks], ['v3', 'v1', 'v2'])
        self.assertEqual(self.q.current().source_id, 'v1')

    def test_clear(self):
        self.q.set_tracks([t(1), t(2)])
        self.q.clear()
        self.assertEqual(len(self.q), 0)
        self.assertIsNone(self.q.current())

    def test_state_roundtrip(self):
        self.q.set_tracks([t(1), t(2), t(3)], start=1)
        restored = PlaybackQueue()
        restored.restore(self.q.to_state())
        self.assertEqual([x.source_id for x in restored.tracks], ['v1', 'v2', 'v3'])
        self.assertEqual(restored.current().source_id, 'v2')

    def test_restore_garbage_is_ignored(self):
        self.q.set_tracks([t(1)])
        self.q.restore('не словарь')
        self.q.restore({'tracks': [{'нет источника': 1}]})
        self.assertTrue(len(self.q.tracks) <= 1)


if __name__ == '__main__':
    unittest.main()
