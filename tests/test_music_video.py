"""Настоящий клип вместо обложки альбома.

В YouTube Music песня почти всегда лежит «art track»: ролик, в котором вместо
картинки одна неподвижная обложка. Слушать это не мешает, а смотреть нечего,
поэтому в видеорежиме приложение ищет клип той же песни и переключает страницу
на него.

Ссылки на клип в ответе плеера нет, так что ищем его обычным поиском с фильтром
«Видео». Поиск охотно подсовывает часовые сборники, каверы и чужие перезаливки
того же трека — поэтому здесь проверяется в первую очередь отбор: что берётся
только свой ролик, а на сомнительное приложение отвечает «клипа нет» и остаётся
с обложкой.
"""
import unittest

from app.core.track import Track
from app.core.youtube import discovery
from app.core.youtube.innertube import InnerTube


def found(video_id, title, artist, duration):
    """Один результат поиска в той же форме, в какой его отдаёт YouTube."""
    minutes, seconds = divmod(duration, 60)
    return {'videoRenderer': {
        'videoId': video_id,
        'title': {'runs': [{'text': title}]},
        'longBylineText': {'runs': [{'text': artist}]},
        'lengthText': {'runs': [{'text': f'{minutes}:{seconds:02d}'}]}}}


def song(video_id='atv0000000a', title='Numb', artist='Linkin Park', duration=187):
    return Track(source='youtube', source_id=video_id, title=title, artist=artist,
                 duration=duration, url='', youtube_id=video_id)


class FakeApi(InnerTube):
    """Тот же клиент и тот же разбор ответа — подменён только поход в сеть."""

    def __init__(self, results):
        super().__init__()
        self.results = results
        self.queries = []

    def _post(self, endpoint, payload):
        self.queries.append(payload.get('query', ''))
        return {'contents': self.results}


class PickTests(unittest.TestCase):
    """Что считается клипом этой песни, а что чужим роликом."""

    def pick(self, results, title='Numb', artist='Linkin Park', duration=187):
        return FakeApi(results).music_video(title, artist, duration)

    def test_takes_the_matching_clip(self):
        self.assertEqual(self.pick([found('clip0000000', 'Numb', 'Linkin Park', 188)]),
                         'clip0000000')

    def test_ignores_a_greatest_hits_compilation(self):
        """Часовой сборник — первое, что предлагает поиск по имени группы."""
        self.assertEqual(self.pick([found('mix00000000', 'Linkin Park Greatest Hits',
                                          'The Pulse Music', 4233)]), '')

    def test_ignores_a_cover_by_someone_else(self):
        """Название совпадает, исполнитель чужой."""
        self.assertEqual(self.pick([found('cover000000', 'Numb', 'Karaoke Band', 187)]), '')

    def test_ignores_an_extended_version(self):
        """Тот же исполнитель, но ролик вдвое длиннее песни."""
        self.assertEqual(self.pick([found('long0000000', 'Numb', 'Linkin Park', 400)]), '')

    def test_takes_a_clip_with_official_video_in_the_name(self):
        """К названию клипа почти всегда приписано «(Official Video)»."""
        self.assertEqual(
            self.pick([found('ovid0000000', '7 rings (Official Video)',
                             'Ariana Grande', 185)],
                      title='7 rings', artist='Ariana Grande', duration=178),
            'ovid0000000')

    def test_skips_the_junk_and_keeps_looking(self):
        """Свой ролик редко оказывается первым — до него нужно дойти."""
        results = [found('mix00000000', 'Linkin Park Greatest Hits', 'The Pulse', 4233),
                   found('cover000000', 'Numb', 'Karaoke Band', 187),
                   found('clip0000000', 'Numb', 'Linkin Park', 188)]
        self.assertEqual(self.pick(results), 'clip0000000')

    def test_nothing_found_is_not_an_error(self):
        self.assertEqual(self.pick([]), '')

    def test_a_song_without_a_title_is_not_searched(self):
        api = FakeApi([])
        self.assertEqual(api.music_video('', 'Linkin Park', 187), '')
        self.assertEqual(api.queries, [])


class CacheTests(unittest.TestCase):
    """Discovery помнит ответ на весь сеанс — в том числе отрицательный."""

    def setUp(self):
        self.disco = discovery.Discovery()
        self.asked = []

        def fake(title, artist, duration=0):
            self.asked.append(title)
            return self.answer

        self.disco._api.music_video = fake
        self.answer = 'clip0000000'

    def test_second_call_does_not_go_to_the_network(self):
        track = song()
        self.assertEqual(self.disco.music_video(track), 'clip0000000')
        self.assertEqual(self.disco.music_video(track), 'clip0000000')
        self.assertEqual(len(self.asked), 1)

    def test_a_song_without_a_clip_is_remembered_too(self):
        """Иначе трек без клипа ходил бы в поиск при каждом повторе."""
        self.answer = ''
        track = song()
        self.assertEqual(self.disco.music_video(track), '')
        self.assertEqual(self.disco.music_video(track), '')
        self.assertEqual(len(self.asked), 1)

    def test_the_song_itself_is_not_a_clip(self):
        """Поиск умеет вернуть ту же самую запись — подменять нечего."""
        self.answer = 'atv0000000a'
        self.assertEqual(self.disco.music_video(song()), '')

    def test_a_track_without_an_id_is_not_searched(self):
        self.assertEqual(self.disco.music_video(song(video_id='')), '')
        self.assertEqual(self.asked, [])


if __name__ == '__main__':
    unittest.main()
