"""Нормализация названий и поиск того же трека в VK."""
import unittest

from app.core import matcher
from app.core.track import Track


def yt(artist='', title='', duration=0) -> Track:
    return Track(source='youtube', source_id='x', artist=artist, title=title,
                 duration=duration)


def vk(artist='', title='', duration=0, audio_id=1) -> Track:
    return Track(source='vk', source_id=f'1_{audio_id}', artist=artist, title=title,
                 duration=duration, vk_owner_id=1, vk_audio_id=audio_id)


class NormalizationTests(unittest.TestCase):
    def test_case_and_spaces(self):
        self.assertEqual(matcher.norm_title('  Enter   SANDMAN '), 'enter sandman')

    def test_yo_and_quotes(self):
        self.assertEqual(matcher.norm_title('«Тёмная» ночь'), 'темная ночь')

    def test_service_markers_dropped(self):
        for raw in ('Numb (Official Video)', 'Numb [Official Music Video]',
                    'Numb (Lyrics)', 'Numb (HD)', 'Numb official video'):
            self.assertEqual(matcher.norm_title(raw), 'numb', raw)

    def test_meaningful_brackets_kept(self):
        self.assertEqual(matcher.norm_title('Numb (Live)'), 'numb live')
        self.assertEqual(matcher.norm_title('Numb (Acoustic Version)'),
                         'numb acoustic version')

    def test_feat_forms_dropped(self):
        for raw in ('Numb feat. Jay-Z', 'Numb ft. Jay-Z', 'Numb featuring Jay-Z',
                    'Numb (feat. Jay-Z)'):
            self.assertEqual(matcher.norm_title(raw), 'numb', raw)
        self.assertEqual(matcher.norm_artist('Linkin Park feat. Jay-Z'), 'linkin park')

    def test_separator_forms(self):
        self.assertEqual(matcher.norm_title('Jay‑Z'), matcher.norm_title('Jay-Z'))

    def test_key_is_stable(self):
        self.assertEqual(matcher.normalized_key('Linkin Park', 'Numb (Official Video)'),
                         matcher.normalized_key('linkin  park', 'NUMB'))

    def test_artist_parts(self):
        self.assertEqual(matcher.artist_parts('Linkin Park & Jay-Z'),
                         {'linkin park', 'jay z'})


class MatchTests(unittest.TestCase):
    def test_confident_match(self):
        result = matcher.match(yt('Linkin Park', 'Numb (Official Video)', 187),
                               [vk('Linkin Park', 'Numb', 186),
                                vk('Linkin Park', 'Faint', 162, audio_id=2)])
        self.assertTrue(result.confident)
        self.assertEqual(result.best.title, 'Numb')

    def test_duration_gap_rejects(self):
        """Часовой микс не должен подменить трёхминутный трек."""
        result = matcher.match(yt('Linkin Park', 'Numb', 187),
                               [vk('Linkin Park', 'Numb', 3600)])
        self.assertFalse(result.confident)

    def test_no_match(self):
        result = matcher.match(yt('Linkin Park', 'Numb', 187),
                               [vk('Гражданская оборона', 'Всё идёт по плану', 180)])
        self.assertEqual(result.kind, matcher.NO_MATCH)
        self.assertIsNone(result.best)

    def test_ambiguous_asks_human(self):
        result = matcher.match(yt('Artist', 'Song', 200),
                               [vk('Artist', 'Song', 200),
                                vk('Artist', 'Song', 201, audio_id=2)])
        self.assertIn(result.kind, (matcher.AMBIGUOUS_MATCH, matcher.CONFIDENT_MATCH))
        self.assertGreaterEqual(len(result.candidates), 2)

    def test_empty_candidates(self):
        result = matcher.match(yt('Artist', 'Song', 200), [])
        self.assertEqual(result.kind, matcher.NO_MATCH)
        self.assertEqual(result.candidates, [])

    def test_feat_difference_still_matches(self):
        result = matcher.match(yt('Linkin Park feat. Jay-Z', 'Numb / Encore', 205),
                               [vk('Linkin Park', 'Numb / Encore', 204)])
        self.assertTrue(result.confident)


class TranslitTests(unittest.TestCase):
    """Половина русских записей в VK подписана латиницей, а поиск m.vk.ru кириллицу
    вообще не понимает - поэтому транслит нужен и при сравнении, и при запросе."""

    def test_translit_basic(self):
        self.assertEqual(matcher.translit('Земфира'), 'Zemfira')
        self.assertEqual(matcher.translit('Хочешь?'), 'Hochesh?')
        self.assertEqual(matcher.translit('Кино Группа крови'), 'Kino Gruppa krovi')

    def test_translit_keeps_latin(self):
        self.assertEqual(matcher.translit('Daft Punk - Get Lucky'),
                         'Daft Punk - Get Lucky')

    def test_translit_empty(self):
        self.assertEqual(matcher.translit(''), '')
        self.assertEqual(matcher.translit(None), '')

    def test_has_cyrillic(self):
        self.assertTrue(matcher.has_cyrillic('Кино'))
        self.assertFalse(matcher.has_cyrillic('Kino'))
        self.assertFalse(matcher.has_cyrillic(''))

    def test_latin_upload_matches_cyrillic_query(self):
        result = matcher.match(yt('Земфира', 'Хочешь?', 197),
                               [vk('Zemfira', 'Hochesh', 197)])
        self.assertTrue(result.confident)

    def test_half_translit_upload_matches(self):
        result = matcher.match(yt('Земфира', 'Хочешь?', 197),
                               [vk('Земфира', 'Hochesh?', 196)])
        self.assertTrue(result.confident)

    def test_translit_does_not_glue_different_songs(self):
        result = matcher.match(yt('Кино', 'Группа крови', 284),
                               [vk('Kino', 'Konchitsya leto', 285)])
        self.assertEqual(result.kind, matcher.NO_MATCH)


class StubGuardTests(unittest.TestCase):
    """В VK попадаются обрезки с правильным названием - их нельзя брать за трек."""

    def test_stub_rejected_when_duration_unknown(self):
        result = matcher.match(yt('Ленинград', 'Экспонат', 0),
                               [vk('Ленинград', 'Экспонат', 4)])
        self.assertEqual(result.kind, matcher.NO_MATCH)

    def test_full_version_wins_over_stub(self):
        result = matcher.match(yt('Ленинград', 'Экспонат', 0),
                               [vk('Ленинград', 'Экспонат', 4),
                                vk('Ленинград', 'Экспонат', 230, audio_id=2)])
        self.assertTrue(result.confident)
        self.assertEqual(result.best.duration, 230)

    def test_longer_copy_wins_on_equal_score(self):
        result = matcher.match(yt('Artist', 'Song', 0),
                               [vk('Artist', 'Song', 180),
                                vk('Artist', 'Song', 200, audio_id=2)])
        self.assertEqual(result.best.duration, 200)


if __name__ == '__main__':
    unittest.main()
