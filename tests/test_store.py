"""Локальная база: треки, связки с VK, избранное, история, плейлисты."""
import os
import sqlite3
import tempfile
import unittest

from app.core.store import SCHEMA_VERSION, Store
from app.core.track import Track


def yt(video_id='abc', title='Numb', artist='Linkin Park') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title=title, artist=artist, duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


def vk(audio_id=10) -> Track:
    return Track(source='vk', source_id=f'1_{audio_id}', title='Numb',
                 artist='Linkin Park', duration=186,
                 vk_owner_id=1, vk_audio_id=audio_id)


class StoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        os.unlink(self.path)          # sqlite создаст файл сам
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_track_roundtrip(self):
        track = yt()
        self.store.save_track(track)
        loaded = self.store.get_track(track.uid)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, 'Numb')
        self.assertEqual(loaded.youtube_id, 'abc')
        self.assertEqual(loaded.duration, 187)

    def test_save_twice_updates(self):
        track = yt()
        self.store.save_track(track)
        track.title = 'Numb (Live)'
        self.store.save_track(track)
        self.assertEqual(self.store.get_track(track.uid).title, 'Numb (Live)')

    def test_mapping_persists(self):
        source, target = yt(), vk()
        self.store.save_track(source)
        self.store.set_mapping(source.uid, target, 'match')
        self.assertTrue(self.store.has_mapping(source.uid))
        self.assertEqual(self.store.mapped_uids([source.uid, 'youtube:zzz']),
                         {source.uid})
        mapped = self.store.mapping_target(source.uid)
        self.assertEqual(mapped.vk_audio_id, 10)
        self.assertEqual(self.store.get_mapping(source.uid)['method'], 'match')

    def test_mapping_survives_reopen(self):
        """После перезапуска кнопка должна сразу показывать «✓ В VK»."""
        source = yt()
        self.store.save_track(source)
        self.store.set_mapping(source.uid, vk(), 'upload')
        self.store.close()
        again = Store(self.path)
        try:
            self.assertTrue(again.has_mapping(source.uid))
        finally:
            again.close()

    def test_mapping_without_vk_coordinates(self):
        """Залили файл, а координат VK не отдал — факт переноса всё равно помним."""
        source = yt()
        self.store.save_track(source)
        self.store.set_mapping(source.uid, None, 'upload')
        self.assertTrue(self.store.has_mapping(source.uid))
        self.assertIsNone(self.store.mapping_target(source.uid))

    def test_favorites(self):
        track = yt()
        self.store.add_favorite(track)
        self.assertTrue(self.store.is_favorite(track.uid))
        self.assertEqual([t.uid for t in self.store.favorites()], [track.uid])
        self.store.remove_favorite(track.uid)
        self.assertFalse(self.store.is_favorite(track.uid))
        self.assertEqual(self.store.favorites(), [])

    def test_play_history(self):
        first, second = yt('a1'), yt('a2', title='Faint')
        self.store.log_play(first)
        self.store.log_play(second)
        recent = self.store.recent_plays(10)
        self.assertEqual(recent[0].uid, second.uid)
        self.assertIn(first.uid, [t.uid for t in recent])

    def test_playlists(self):
        playlist_id = self.store.create_playlist('Вечер')
        self.assertEqual([p['title'] for p in self.store.playlists()], ['Вечер'])
        first, second = yt('a1'), yt('a2', title='Faint')
        self.store.add_to_playlist(playlist_id, first)
        self.store.add_to_playlist(playlist_id, second)
        self.assertEqual([t.uid for t in self.store.playlist_tracks(playlist_id)],
                         [first.uid, second.uid])
        self.store.set_playlist_order(playlist_id, [second.uid, first.uid])
        self.assertEqual([t.uid for t in self.store.playlist_tracks(playlist_id)],
                         [second.uid, first.uid])
        self.store.remove_from_playlist(playlist_id, second.uid)
        self.assertEqual([t.uid for t in self.store.playlist_tracks(playlist_id)],
                         [first.uid])
        self.store.rename_playlist(playlist_id, 'Ночь')
        self.assertEqual(self.store.get_playlist(playlist_id)['title'], 'Ночь')
        self.store.link_playlist(playlist_id, 1, 55, 'key')
        self.assertEqual(self.store.get_playlist(playlist_id)['vk_playlist_id'], 55)
        self.store.delete_playlist(playlist_id)
        self.assertEqual(self.store.playlists(), [])

    def test_state_and_json(self):
        self.store.set_state('page', 'youtube')
        self.assertEqual(self.store.get_state('page'), 'youtube')
        self.assertEqual(self.store.get_state('нет такого', 'по умолчанию'),
                         'по умолчанию')
        self.store.set_json('queue', {'index': 2, 'uids': ['a', 'b']})
        self.assertEqual(self.store.get_json('queue')['index'], 2)
        self.assertEqual(self.store.get_json('нет такого', []), [])

    def test_library_keeps_all_sources_together(self):
        """Фонотека — один список для VK, YouTube и файлов с диска."""
        path = os.path.join(tempfile.gettempdir(), 'numb.mp3')
        local = Track(source='local', source_id=os.path.normcase(path),
                      title='Numb', artist='Linkin Park', local_path=path)
        for track in (yt(), vk(), local):
            self.store.save_to_library(track)
        self.assertEqual(len(self.store.saved_tracks()), 3)
        self.assertEqual([t.source for t in self.store.saved_tracks('vk')], ['vk'])
        self.assertEqual(self.store.saved_count(), 3)
        self.assertTrue(self.store.is_saved(yt().uid))
        self.assertEqual(self.store.saved_uids([yt().uid, 'нет:такого']), {yt().uid})

        self.store.remove_from_library(yt().uid)
        self.assertFalse(self.store.is_saved(yt().uid))
        # Сам трек остаётся в базе: он ещё может быть в плейлистах и истории
        self.assertIsNotNone(self.store.get_track(yt().uid))

    def test_saved_at_survives_second_save(self):
        """Порядок «новые сверху» не сбивается повторным добавлением."""
        def saved_at() -> float:
            rows = self.store._query('SELECT saved_at FROM tracks WHERE uid = ?',
                                     (yt().uid,))
            return rows[0]['saved_at']

        self.store.save_to_library(yt())
        first = saved_at()
        self.store.save_to_library(yt(title='Numb (Live)'))
        self.assertEqual(saved_at(), first)

    def test_offline_copy_marks_track_cached(self):
        self.store.save_to_library(yt())
        self.store.set_local_path(yt().uid, __file__, cached=True)
        self.assertEqual(self.store.cached_uids([yt().uid]), {yt().uid})
        self.assertEqual([t.uid for t in self.store.cached_tracks()], [yt().uid])
        # cached — это «файл действительно лежит на диске», а не просто путь
        self.assertTrue(self.store.get_track(yt().uid).cached)

        self.store.clear_local_path(yt().uid)
        self.assertEqual(self.store.cached_uids([yt().uid]), set())
        self.assertFalse(self.store.get_track(yt().uid).cached)

    def test_foreign_file_is_not_a_copy(self):
        """Скачанный вручную файл привязан, но офлайн-копией не считается:
        удалять его при нехватке места нельзя."""
        self.store.save_to_library(yt())
        self.store.set_local_path(yt().uid, __file__)
        self.assertTrue(self.store.get_track(yt().uid).cached)
        self.assertEqual(self.store.cached_uids([yt().uid]), set())


class MigrationTests(unittest.TestCase):
    """База прошлой версии должна открыться и сохранить данные.

    Пользователь обновляет программу поверх — терять его историю и плейлисты
    из-за новых колонок нельзя."""

    # Схема первой версии: у истории нет источника и длительности прослушивания,
    # у плейлистов — происхождения
    V1 = """
    CREATE TABLE tracks (
        uid TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT, artist TEXT,
        duration INTEGER DEFAULT 0, url TEXT, cover TEXT, local_path TEXT,
        vk_owner_id INTEGER, vk_audio_id INTEGER, vk_access_key TEXT,
        youtube_id TEXT, norm_key TEXT, meta TEXT, added_at REAL DEFAULT 0);
    CREATE TABLE play_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, played_at REAL DEFAULT 0);
    CREATE TABLE playlists (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        vk_owner_id INTEGER, vk_playlist_id INTEGER, vk_access_hash TEXT DEFAULT '',
        created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0);
    CREATE TABLE favorites (uid TEXT PRIMARY KEY, added_at REAL DEFAULT 0);
    CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
    """

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        old = sqlite3.connect(self.path)
        old.executescript(self.V1)
        track = yt()
        old.execute(
            'INSERT INTO tracks (uid, source, source_id, title, artist, duration, '
            'youtube_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (track.uid, 'youtube', 'abc', 'Numb', 'Linkin Park', 187, 'abc'))
        old.execute('INSERT INTO play_history (uid, played_at) VALUES (?, ?)',
                    (track.uid, 1000.0))
        old.execute("INSERT INTO playlists (title) VALUES ('Старый список')")
        old.execute('INSERT INTO favorites (uid, added_at) VALUES (?, ?)',
                    (track.uid, 900.0))
        old.execute("INSERT INTO state (key, value) VALUES ('schema_version', '1')")
        old.commit()
        old.close()

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_old_database_is_upgraded(self):
        store = Store(self.path)
        try:
            self.assertEqual(store.get_state('schema_version'), str(SCHEMA_VERSION))
            # Новые колонки появились
            for column in ('source', 'listened', 'finished'):
                self.assertIn(column, store._columns('play_history'))
            for column in ('source', 'ext_id'):
                self.assertIn(column, store._columns('playlists'))
            # Старые данные на месте
            self.assertEqual([t.title for t in store.recent_plays(10)], ['Numb'])
            self.assertEqual([p['title'] for p in store.playlists()],
                             ['Любимое', 'Старый список'])
            # Избранное переехало в системный плейлист, таблицы favorites больше нет
            self.assertEqual([t.title for t in store.favorites()], ['Numb'])
            self.assertTrue(store.is_favorite(yt().uid))
            self.assertEqual(store._columns('favorites'), set())
            # И новая запись пишется без ошибок
            store.log_play(yt('b', title='Faint'), listened=45, finished=True)
            self.assertEqual(store.history(10)[0]['listened'], 45)
        finally:
            store.close()

    def test_second_open_does_not_break(self):
        """Повторное открытие уже обновлённой базы ничего не меняет."""
        Store(self.path).close()
        store = Store(self.path)
        try:
            self.assertEqual(store.get_state('schema_version'), str(SCHEMA_VERSION))
            self.assertEqual(len(store.history(10)), 1)
            self.assertEqual([t.title for t in store.favorites()], ['Numb'])
        finally:
            store.close()

if __name__ == '__main__':
    unittest.main()
