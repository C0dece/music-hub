"""Меню действий над треком собирается одинаково во всех разделах.

Раньше меню писалось заново в каждом месте и списки пунктов разошлись —
тест закрепляет, что набор один и что необязательные пункты появляются только
вместе со своим обработчиком.
"""
import os
import tempfile
import unittest

from .qt_app import qt_app
from app.core.store import Store
from app.core.track import Track
from app.ui.track_actions import NEW_PLAYLIST, TrackActions, build_menu


def yt(video_id='abc') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title='Numb', artist='Linkin Park', duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


def titles(menu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


class TrackActionsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_only_given_handlers_appear(self):
        menu = build_menu(None, [yt()], TrackActions(play=lambda tracks: None))
        self.assertEqual(titles(menu), ['Играть', 'Скопировать ссылку'])

    def test_favorite_label_follows_store(self):
        track = yt()
        actions = TrackActions(favorite=lambda tracks: None)
        menu = build_menu(None, [track], actions, store=self.store)
        self.assertIn('В избранное', titles(menu))
        self.store.add_favorite(track)
        menu = build_menu(None, [track], actions, store=self.store)
        self.assertIn('Убрать из избранного', titles(menu))

    def test_playlist_submenu_lists_own_playlists(self):
        self.store.create_playlist('Вечер')
        self.store.add_favorite(yt())          # системный список сюда попасть не должен
        chosen = []
        menu = build_menu(None, [yt()], TrackActions(
            playlist=lambda tracks, pid: chosen.append(pid)), store=self.store)
        submenu = [a.menu() for a in menu.actions()
                   if a.text() == 'Добавить в плейлист'][0]
        self.assertEqual(titles(submenu), ['Вечер', 'Новый плейлист…'])
        submenu.actions()[-1].trigger()
        self.assertEqual(chosen, [NEW_PLAYLIST])

    def test_vk_targets_only(self):
        """Записи VK кнопка добавляет к себе, а перенесённые с YouTube пропускает.

        Своя запись или чужая, по треку не видно — это знает сервис переноса,
        он же и отказывает. Меню отбрасывает только то, что заведомо в VK:
        трек с YouTube с отметкой `in_vk`."""
        vk_track = Track(source='vk', source_id='1_10', title='Numb',
                         artist='Linkin Park', vk_owner_id=1, vk_audio_id=10)
        menu = build_menu(None, [vk_track], TrackActions(add_vk=lambda tracks: None))
        self.assertIn('Добавить в VK', titles(menu))

        moved = yt()
        moved.in_vk = True
        menu = build_menu(None, [moved], TrackActions(add_vk=lambda tracks: None))
        self.assertNotIn('Добавить в VK', titles(menu))

    def test_remove_needs_rows(self):
        actions = TrackActions(remove=lambda rows: None)
        self.assertNotIn('Убрать', titles(build_menu(None, [yt()], actions)))
        self.assertIn('Убрать', titles(build_menu(None, [yt()], actions, rows=[0])))


if __name__ == '__main__':
    unittest.main()
