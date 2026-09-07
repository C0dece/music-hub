"""Поиск и рекомендации в VK: запрос уходит латиницей, покалеченные строки
в ответе отбрасываются, а подборка VK не подменяется поиском втихую.

Сети здесь нет: подменяем единственный сетевой метод клиента и проверяем, что и
куда он отправляет и как разбирает ответ."""
import unittest

from app.core import vk_client


def row(audio_id, owner_id, title, artist, duration=200):
    """Строка выдачи m.vk.ru в той раскладке, которую разбирает _row_to_track."""
    data = [''] * 25
    data[0], data[1] = audio_id, owner_id
    data[3], data[4], data[5] = title, artist, duration
    data[13] = f'x/x/{audio_id}h/x/x/{audio_id}u'     # хэши, без них строку отбросят
    data[24] = 'accesskey'
    return data


class FakeClient(vk_client.VkClient):
    """Клиент без сети и без авторизации: только логика search_tracks."""

    user_id = 1          # у настоящего клиента это свойство поверх VkAudio

    def __init__(self, payload):
        self.payload = payload
        self.sent = []
        self._has_web_session = True

    def _post_section_raw(self, data: dict) -> dict:
        self.sent.append(data)
        return self.payload


def payload_with(*rows):
    return {'data': [{'list': list(rows)}]}


class SectionClient(FakeClient):
    """Ответ зависит от раздела: рекомендации перебирают несколько подряд."""

    def __init__(self, by_section: dict):
        super().__init__({})
        self.by_section = by_section

    def _post_section_raw(self, data: dict) -> dict:
        self.sent.append(data)
        return self.by_section.get(data.get('type'), payload_with())


class SearchQueryTests(unittest.TestCase):
    def test_cyrillic_query_sent_as_latin(self):
        client = FakeClient(payload_with(row(1, 2, 'Группа крови', 'Кино')))
        client.search_tracks('Кино Группа крови')
        self.assertEqual(client.sent[0]['search_q'], 'Kino Gruppa krovi')

    def test_latin_query_sent_as_is(self):
        client = FakeClient(payload_with(row(1, 2, 'Get Lucky', 'Daft Punk')))
        client.search_tracks('Daft Punk Get Lucky')
        self.assertEqual(client.sent[0]['search_q'], 'Daft Punk Get Lucky')

    def test_empty_query_makes_no_request(self):
        client = FakeClient(payload_with())
        self.assertEqual(client.search_tracks('   '), [])
        self.assertEqual(client.sent, [])


class SearchResultTests(unittest.TestCase):
    def test_cyrillic_results_are_kept(self):
        client = FakeClient(payload_with(row(1, 2, 'Группа крови', 'Кино', 284)))
        found = client.search_tracks('Кино Группа крови')
        self.assertEqual([t['title'] for t in found], ['Группа крови'])
        self.assertEqual(found[0]['access_key'], 'accesskey')

    def test_garbled_rows_are_dropped(self):
        client = FakeClient(payload_with(
            row(1, 2, '?4??4??7?', '?1??2??3?'),
            row(2, 2, 'Группа крови', 'Кино'),
        ))
        found = client.search_tracks('Кино Группа крови')
        self.assertEqual([t['title'] for t in found], ['Группа крови'])

    def test_garbled_artist_drops_row_too(self):
        client = FakeClient(payload_with(row(1, 2, 'Нормальное название', '?4??4??7?')))
        self.assertEqual(client.search_tracks('Кино'), [])

    def test_limit_counts_kept_rows_only(self):
        client = FakeClient(payload_with(
            row(1, 2, '?4??4??7?', '?1??2??3?'),
            row(2, 2, 'Первая', 'Кино'),
            row(3, 2, 'Вторая', 'Кино'),
        ))
        found = client.search_tracks('Кино', limit=2)
        self.assertEqual([t['title'] for t in found], ['Первая', 'Вторая'])

    def test_expired_session_raises(self):
        client = FakeClient({'location': 'https://login.vk.com/?act=login'})
        client._browser_cookies_tried = True
        client._cookies_browser = ''
        with self.assertRaises(vk_client.VkSessionExpired):
            client.search_tracks('Kino')
        self.assertFalse(client.has_web_session)


class RecommendationTests(unittest.TestCase):
    """Рекомендации VK: свои, если есть, и честная пустота, если их нет."""

    def test_first_available_section_wins(self):
        client = SectionClient({'recoms': payload_with(row(1, 10, 'Sleep', 'Godspeed'))})
        found = client.recommended_tracks()
        self.assertEqual([t['title'] for t in found], ['Sleep'])
        self.assertEqual([d['type'] for d in client.sent], ['recoms'])

    def test_empty_section_falls_through_to_the_next(self):
        client = SectionClient({'radio': payload_with(row(2, 10, 'Kyoto', 'Bridgers'))})
        found = client.recommended_tracks()
        self.assertEqual([t['title'] for t in found], ['Kyoto'])
        # Разделы перебраны по порядку, а не выбран наугад один
        self.assertEqual([d['type'] for d in client.sent],
                         list(vk_client._RECOM_SECTIONS))

    def test_no_recommendations_returns_empty_not_search(self):
        """Пустой ответ не подменяется поиском: подмену делает и метит вызывающий."""
        client = SectionClient({})
        self.assertEqual(client.recommended_tracks(), [])
        self.assertNotIn('search', [d['type'] for d in client.sent])
        self.assertFalse(any('search_q' in d for d in client.sent))

    def test_expired_session_raises(self):
        client = SectionClient({'recoms': {'location': 'https://login.vk.ru/?act=login'}})
        client._browser_cookies_tried = True
        client._cookies_browser = ''
        with self.assertRaises(vk_client.VkSessionExpired):
            client.recommended_tracks()
        self.assertFalse(client.has_web_session)

    def test_limit_applies(self):
        rows = [row(i, 10, f'T{i}', 'A') for i in range(5)]
        client = SectionClient({'recoms': payload_with(*rows)})
        self.assertEqual(len(client.recommended_tracks(limit=2)), 2)


def album(list_id, title, artists=(), genres=(), year=0, count=10):
    """Плейлист в том виде, в каком его отдаёт audio.getPlaylists."""
    return {
        'id': list_id, 'owner_id': -100, 'access_key': 'k', 'title': title,
        'count': count, 'year': year,
        'main_artists': [{'name': name} for name in artists],
        'genres': [{'name': name} for name in genres],
        'thumb': {'photo_300': f'https://cover/{list_id}.jpg'},
    }


class ShelfTests(unittest.TestCase):
    """Полки «Волны»: их много, и каждая — из разметки самого VK.

    Одной подборки человеку мало, но и выдумывать оси нельзя: полка существует
    ровно тогда, когда VK сам проставил жанр, год или исполнителя."""

    def shelves(self, *albums):
        return vk_client._shelves_from_playlists(
            [vk_client._row_to_playlist(a) for a in albums])

    def test_genre_year_and_artist_each_make_a_shelf(self):
        """Один альбом законно попадает на разные полки: человек ищет то по
        жанру, то по году, то по исполнителю — оси независимы."""
        # Годы внутри группы исполнителя разные — иначе «ST1M» и «Свежее» сошлись
        # бы состав в состав и одна из полок ушла бы как точный дубликат
        rows = [album(i, f'A{i}', artists=['ST1M'], genres=['Рэп'], year=2024)
                for i in range(2)]
        rows += [album(2, 'A2', artists=['ST1M'], genres=['Рэп'], year=2005)]
        rows += [album(i, f'B{i}', artists=['Баста'], genres=['Рэп'], year=2005)
                 for i in range(3, 6)]
        rows += [album(6, 'C', artists=['Баста'], genres=['Рэп'], year=2024)]
        titles = [s['title'] for s in self.shelves(*rows)]
        self.assertIn('Рэп', titles)          # жанр
        self.assertIn('Свежее', titles)       # год
        self.assertIn('ST1M', titles)         # исполнитель

    def test_a_lone_album_is_not_a_shelf(self):
        """Полка из одного альбома — тот же альбом, только на два клика дальше."""
        rows = [album(1, 'A1', genres=['Джаз']),
                *[album(i, f'B{i}', genres=['Рэп']) for i in range(2, 6)]]
        titles = [s['title'] for s in self.shelves(*rows)]
        self.assertIn('Рэп', titles)
        self.assertNotIn('Джаз', titles)

    def test_unlabelled_albums_make_no_shelves(self):
        """Разметки нет — полок нет. Придумать настроение самим значит выдать
        свою догадку за мнение VK, а это та же подмена, что поиск вместо
        рекомендаций (AGENTS.md)."""
        rows = [album(i, f'A{i}') for i in range(6)]
        self.assertEqual(self.shelves(*rows), [])

    def test_shelves_with_the_same_albums_are_shown_once(self):
        """Совпал состав до последнего альбома — полки отличаются только подписью."""
        rows = [album(i, f'A{i}', artists=['ST1M'], genres=['Рэп'])
                for i in range(3)]
        titles = [s['title'] for s in self.shelves(*rows)]
        self.assertEqual(titles, ['Рэп'])

    def test_a_narrow_shelf_survives_inside_a_wide_one(self):
        """Узкая полка внутри широкой — норма, ради которой всё и затевалось.

        «ST1M» целиком лежит в «Рэпе», но прятать его значит оставить человека
        с одной общей полкой вместо выбора."""
        rows = [album(i, f'A{i}', artists=['ST1M'], genres=['Рэп'])
                for i in range(3)]
        rows += [album(i, f'B{i}', genres=['Рэп']) for i in range(3, 6)]
        titles = [s['title'] for s in self.shelves(*rows)]
        self.assertIn('Рэп', titles)
        self.assertIn('ST1M', titles)

    def test_partly_overlapping_shelves_both_stay(self):
        """Частичное пересечение — не повод прятать: оси-то разные.

        Ни одна из полок не лежит в другой целиком: у «Рэпа» есть чужой альбом,
        у «ST1M» — альбом без жанра."""
        rows = [album(i, f'A{i}', artists=['ST1M'], genres=['Рэп'])
                for i in range(3)]
        rows += [album(3, 'C', artists=['ST1M'])]      # у этого жанра нет
        rows += [album(4, 'D', genres=['Рэп'])]        # а у этого нет исполнителя
        titles = [s['title'] for s in self.shelves(*rows)]
        self.assertIn('Рэп', titles)
        self.assertIn('ST1M', titles)

    def test_shelf_carries_a_cover_and_its_albums(self):
        rows = [album(i, f'A{i}', genres=['Рэп'], count=7) for i in range(3)]
        shelf = self.shelves(*rows)[0]
        self.assertEqual(shelf['cover'], 'https://cover/0.jpg')
        self.assertEqual(len(shelf['_items']), 3)
        self.assertEqual(shelf['count'], 21)


class ShelfTrackTests(unittest.TestCase):
    """Открыть полку — не зная и не выясняя, какого она рода."""

    def client(self, by_section=None):
        client = SectionClient(by_section or {})
        client.opened = []

        def get_playlist_tracks(playlist):
            client.opened.append(playlist['id'])
            return [vk_client._row_to_track(
                row(playlist['id'], 10, f"T{playlist['id']}", 'A'))]

        client.get_playlist_tracks = get_playlist_tracks
        return client

    def test_section_shelf_asks_the_section_again(self):
        """У раздела нет координат плейлиста — открыть его можно только запросом."""
        client = self.client({'recent': payload_with(row(1, 10, 'Last', 'A'))})
        found = client.shelf_tracks({'_section': 'recent', '_items': None})
        self.assertEqual([t['title'] for t in found], ['Last'])

    def test_joined_shelf_merges_its_albums_without_repeats(self):
        client = self.client()
        shelf = {'_section': '', '_items': [{'id': 1}, {'id': 2}, {'id': 1}]}
        found = client.shelf_tracks(shelf)
        self.assertEqual([t['title'] for t in found], ['T1', 'T2'])

    def test_one_broken_album_does_not_lose_the_whole_shelf(self):
        client = self.client()
        real = client.get_playlist_tracks

        def flaky(playlist):
            if playlist['id'] == 1:
                raise vk_client.VkAuthError('альбом недоступен')
            return real(playlist)

        client.get_playlist_tracks = flaky
        found = client.shelf_tracks({'_section': '', '_items': [{'id': 1}, {'id': 2}]})
        self.assertEqual([t['title'] for t in found], ['T2'])

    def test_plain_wave_opens_by_its_own_coordinates(self):
        client = self.client()
        found = client.shelf_tracks({'id': 5, 'owner_id': -1, '_section': '',
                                     '_items': None})
        self.assertEqual(client.opened, [5])
        self.assertEqual([t['title'] for t in found], ['T5'])


if __name__ == '__main__':
    unittest.main()
