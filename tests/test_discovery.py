"""Разбор ответов YouTube Music и честность подписей у подборок.

Настоящие ответы private API сюда не ходят: в `tests/data` лежат сохранённые
образцы той же формы. Именно разбор чаще всего и ломается, когда YouTube меняет
разметку, - поэтому проверяем его отдельно от сети.
"""
import json
import os
import unittest

from app.core.youtube import innertube
from app.core.youtube.discovery import (
    KIND_FALLBACK, KIND_RADIO, KIND_RECOMMENDED, Section, playlist_id_from, to_track,
)

DATA = os.path.join(os.path.dirname(__file__), 'data')


def sample(name: str):
    with open(os.path.join(DATA, name), encoding='utf-8') as handle:
        return json.load(handle)


class ParseTest(unittest.TestCase):
    """Треки, подборки и плейлисты из сохранённых ответов."""

    def setUp(self):
        self.home = sample('yt_music_home.json')
        self.radio = sample('yt_music_radio.json')

    def test_items_from_home(self):
        items = innertube.parse_items(self.home, 20)
        self.assertEqual([item['id'] for item in items],
                         ['sSAt1Ux1ODA', 'kXYiU_JCYtU'])
        first = items[0]
        self.assertEqual(first['title'], 'Sextape')
        self.assertEqual(first['artist'], 'Deftones')
        self.assertEqual(first['duration'], 243)
        # Обложка берётся самая крупная из списка
        self.assertEqual(first['cover'], 'https://lh3.googleusercontent.com/big=w226')
        # У второго трека обложки в ответе нет - подставляем стандартную
        self.assertIn('kXYiU_JCYtU', items[1]['cover'])
        self.assertEqual(items[1]['duration'], 187)

    def test_sections_keep_titles(self):
        sections = innertube.parse_sections(self.home, 20)
        titles = [title for title, _items in sections]
        self.assertEqual(titles[0], 'Слушать снова')
        self.assertEqual(len(sections[0][1]), 2)
        # Плитка плейлиста треком не притворяется: из полки с плейлистами
        # играть можно только микс, у него есть свой ролик
        mixes = dict(sections)['Ваши плейлисты']
        self.assertEqual([item['title'] for item in mixes], ['Микс: Deftones'])

    def test_playlists_skip_tracks_and_radio(self):
        playlists = innertube.parse_playlists(self.home, 20)
        self.assertEqual([entry['id'] for entry in playlists], ['PLabcdef1234567890'])
        entry = playlists[0]
        self.assertEqual(entry['title'], 'Вечерний рок')
        self.assertEqual(entry['count'], 42)

    def test_radio_items_without_duplicates(self):
        items = innertube.parse_items(self.radio, 25)
        # Третья запись - повтор первой, в очереди она не нужна
        self.assertEqual([item['id'] for item in items],
                         ['sSAt1Ux1ODA', 'MPlqSJ4gGgg'])
        self.assertEqual(items[1]['artist'], 'Deftones')
        self.assertEqual(items[1]['duration'], 255)

    def test_limit_is_respected(self):
        self.assertEqual(len(innertube.parse_items(self.home, 1)), 1)

    def test_mix_sections_keep_ready_made_playlists(self):
        """Полка с плитками подборок - это лента, из которой и выбирают."""
        sections = innertube.parse_mix_sections(self.home, 20)
        self.assertEqual([title for title, _mixes in sections], ['Ваши плейлисты'])
        self.assertEqual([entry['id'] for entry in sections[0][1]],
                         ['PLabcdef1234567890'])

    def test_mix_sections_take_radio_tiles(self):
        """Микс главной живёт под номером `RD…`: без него полок почти не остаётся."""
        tile = {'musicTwoRowItemRenderer': {
            'title': {'runs': [{'text': 'Микс - Deftones'}]},
            'subtitle': {'runs': [{'text': 'Deftones, Chevelle и другие'}]},
            'navigationEndpoint': {
                'watchPlaylistEndpoint': {'playlistId': 'RDCLAK5uy_abcdef'}}}}
        shelf = {'musicCarouselShelfRenderer': {
            'header': {'musicCarouselShelfBasicHeaderRenderer': {
                'title': {'runs': [{'text': 'Миксы для вас'}]}}},
            'contents': [tile]}}
        data = {'contents': [shelf]}
        sections = innertube.parse_mix_sections(data, 20)
        self.assertEqual([title for title, _mixes in sections], ['Миксы для вас'])
        self.assertEqual(sections[0][1][0]['id'], 'RDCLAK5uy_abcdef')
        # Треков на такой полке нет, поэтому обычный разбор её и не видел
        self.assertEqual(innertube.parse_sections(data, 20), [])
        # А в списке личных плейлистов радио по-прежнему не место
        self.assertEqual(innertube.parse_playlists(data, 20), [])

    def test_broken_payload_gives_nothing(self):
        """Чужая разметка не должна ронять приложение - просто пусто."""
        for payload in ({}, {'contents': None}, {'contents': [1, 2, 3]},
                        {'musicResponsiveListItemRenderer': {'videoId': 'short'}}):
            self.assertEqual(innertube.parse_items(payload, 10), [])
            self.assertEqual(innertube.parse_sections(payload, 10), [])
            self.assertEqual(innertube.parse_mix_sections(payload, 10), [])
            self.assertEqual(innertube.parse_playlists(payload, 10), [])


class ConvertTest(unittest.TestCase):
    """Запись InnerTube - в трек приложения."""

    def test_to_track(self):
        track = to_track({'id': 'sSAt1Ux1ODA', 'title': 'Sextape',
                          'artist': 'Deftones', 'duration': 243, 'cover': 'x'})
        self.assertEqual(track.source, 'youtube')
        self.assertEqual(track.youtube_id, 'sSAt1Ux1ODA')
        self.assertEqual(track.artist, 'Deftones')
        self.assertEqual(track.url, 'https://www.youtube.com/watch?v=sSAt1Ux1ODA')

    def test_to_track_splits_title_without_artist(self):
        track = to_track({'id': 'kXYiU_JCYtU', 'title': 'Linkin Park - Numb'})
        self.assertEqual(track.artist, 'Linkin Park')
        self.assertEqual(track.title, 'Numb')

    def test_playlist_id_from(self):
        self.assertEqual(playlist_id_from(
            'https://www.youtube.com/playlist?list=PL123456'), 'PL123456')
        self.assertEqual(playlist_id_from('VLPL123456'), 'PL123456')
        self.assertEqual(playlist_id_from('PL123456'), 'PL123456')
        self.assertEqual(playlist_id_from(''), '')
        self.assertEqual(playlist_id_from('https://example.com/'), '')


class HonestyTest(unittest.TestCase):
    """Поиск нельзя выдавать за рекомендацию - подпись должна это показывать."""

    def test_labels(self):
        self.assertTrue(Section('Для вас', [], KIND_RECOMMENDED).genuine)
        self.assertTrue(Section('Радио', [], KIND_RADIO).genuine)
        fallback = Section('Новинки музыки', [], KIND_FALLBACK)
        self.assertFalse(fallback.genuine)
        self.assertEqual(fallback.label, 'Подборка по поиску')


if __name__ == '__main__':
    unittest.main()
