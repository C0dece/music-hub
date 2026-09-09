"""Единая волна: доли источников, чистка повторов и «на усмотрение программы».

Сети здесь нет: вместо VK, YouTube и папок с файлами - заглушки с готовыми
ответами. Проверяем то, что человек замечает сразу: соблюдены ли доли, не идут
ли треки кучами по источникам, не звучит ли одна и та же песня дважды подряд -
сначала из VK, потом с YouTube.
"""
import time
import unittest

from PySide6.QtCore import QThreadPool

from .qt_app import qt_app
from app.core.recommendations import PROVENANCE
from app.core.youtube.discovery import KIND_FALLBACK, KIND_VK_RECOMS
from app.core.mixer import (DEFAULT_LIMIT, MODE_DISCOVER, MODE_KNOWN,
                            MODE_MIXED, MixConfig, Mixer, _interleave)
from app.core.track import SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE, Track


def yt(video_id: str, title: str = 'Numb', artist: str = 'Linkin Park') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title=title, artist=artist, duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


def row(audio_id: int, artist: str = 'Deftones', title: str = 'Sextape') -> dict:
    """Строка VK в том виде, в каком её отдаёт vk_client."""
    return {'id': audio_id, 'owner_id': 1, 'artist': artist, 'title': title,
            'duration': 186, 'url': None, 'access_key': '', 'cover': ''}


class FakeVk:
    """Музыка VK без сети: фонотека и выдача поиска."""

    user_id = 1
    has_web_session = True

    def __init__(self, library=None, found=None, recoms=None):
        self.library = library if library is not None else [
            row(i, 'Deftones', f'Song {i}') for i in range(1, 41)]
        self.found = found if found is not None else [
            row(100 + i, 'Placebo', f'Found {i}') for i in range(1, 21)]
        # По умолчанию своих рекомендаций у VK нет - так он отвечает чаще всего
        self.recoms = list(recoms or [])
        self.queries: list[str] = []

    def get_my_tracks(self):
        return list(self.library)

    def recommended_tracks(self, limit=60):
        return list(self.recoms)[:limit]

    def search_tracks(self, query, limit=60):
        self.queries.append(query)
        return list(self.found)[:limit]


class FakeSection:
    def __init__(self, tracks, genuine=True):
        self.title = 'Для вас'
        self.tracks = tracks
        self.genuine = genuine


class FakeRecommender:
    """Рекомендации YouTube без сети."""

    def __init__(self, genuine=True):
        self.genuine = genuine
        self.history = [yt(f'h{i}', f'History {i}') for i in range(1, 41)]
        self.feed = [yt(f'f{i}', f'Feed {i}') for i in range(1, 41)]

    def autoplay(self, seed, exclude, limit):
        return [t for t in self.history if t.uid not in (exclude or ())][:limit]

    def sections(self, per_section=16):
        return [FakeSection(self.feed[:per_section], self.genuine)]

    def artist_radio(self, artist, limit=25):
        return self.feed[:limit]


class FakeMedia:
    def __init__(self, path, name, kind='audio'):
        self.path = path
        self.name = name
        self.kind = kind


class FakeStore:
    """Только то, что микс спрашивает у базы."""

    def __init__(self, artists=(), hidden=(), hidden_artists=(), played=()):
        self._artists = list(artists)
        self._hidden = set(hidden)
        self._hidden_artists = set(hidden_artists)
        self._played = list(played)

    def top_artists(self, limit=10):
        return self._artists[:limit]

    def hidden_tracks(self):
        return set(self._hidden)

    def hidden_artists(self):
        return set(self._hidden_artists)

    def played_uids(self, limit=50):
        return self._played[:limit]


def files(count=20):
    return lambda: [FakeMedia(f'D:/music/song{i}.mp3', f'Muse - Song {i}')
                    for i in range(1, count + 1)]


class MixConfigTests(unittest.TestCase):
    def test_broken_file_falls_back_to_defaults(self):
        """Настройки читаются из settings.json - там может лежать что угодно."""
        config = MixConfig.from_dict({'weights': 'сломано', 'mode': 'нет такого',
                                      'limit': 'много'})
        self.assertEqual(config.mode, MODE_MIXED)
        self.assertEqual(config.limit, DEFAULT_LIMIT)
        self.assertEqual(config.sources, [SOURCE_VK, SOURCE_YOUTUBE])

    def test_limit_stays_in_range(self):
        self.assertEqual(MixConfig(limit=5000).limit, 300)
        self.assertEqual(MixConfig(limit=1).limit, 10)

    def test_shares_add_up_to_hundred(self):
        config = MixConfig(weights={SOURCE_VK: 70, SOURCE_YOUTUBE: 30,
                                    SOURCE_LOCAL: 0})
        self.assertEqual(config.share(SOURCE_VK), 70)
        self.assertEqual(config.share(SOURCE_YOUTUBE), 30)
        self.assertEqual(config.share(SOURCE_LOCAL), 0)

    def test_round_trip(self):
        config = MixConfig(weights={SOURCE_VK: 20, SOURCE_YOUTUBE: 80,
                                    SOURCE_LOCAL: 0},
                           mode=MODE_DISCOVER, query='Deftones', limit=40)
        again = MixConfig.from_dict(config.to_dict())
        self.assertEqual(again.to_dict(), config.to_dict())


class InterleaveTests(unittest.TestCase):
    def test_equal_weights_go_one_by_one(self):
        pools = {'a': [f'a{i}' for i in range(5)], 'b': [f'b{i}' for i in range(5)]}
        mixed = _interleave(pools, {'a': 50, 'b': 50}, 10)
        sources = ''.join(item[0] for item in mixed)
        self.assertNotIn('aaa', sources)
        self.assertNotIn('bbb', sources)
        self.assertEqual(sources.count('a'), 5)

    def test_rare_source_does_not_pile_up_at_the_end(self):
        """При 75/25 редкий источник должен попадаться по ходу, а не хвостом."""
        pools = {'a': [f'a{i}' for i in range(12)], 'b': [f'b{i}' for i in range(4)]}
        mixed = _interleave(pools, {'a': 75, 'b': 25}, 16)
        sources = ''.join(item[0] for item in mixed)
        self.assertEqual(sources.count('b'), 4)
        self.assertLess(sources.index('b'), 5)

    def test_exhausted_source_does_not_stop_the_mix(self):
        pools = {'a': ['a1'], 'b': [f'b{i}' for i in range(9)]}
        mixed = _interleave(pools, {'a': 50, 'b': 50}, 10)
        self.assertEqual(len(mixed), 10)


class MixerTests(unittest.TestCase):
    def build(self, **kwargs):
        store = kwargs.pop('store', FakeStore(artists=['Deftones']))
        vk = kwargs.pop('vk', FakeVk())
        rec = kwargs.pop('rec', FakeRecommender())
        local = kwargs.pop('local', None)
        mixer = Mixer(store, rec, lambda: vk, local)
        return mixer, vk, rec

    def test_two_sources_split_evenly(self):
        mixer, _vk, _rec = self.build()
        config = MixConfig(weights={SOURCE_VK: 50, SOURCE_YOUTUBE: 50,
                                    SOURCE_LOCAL: 0}, limit=20)
        result = mixer.build(config)
        self.assertEqual(len(result), 20)
        self.assertEqual(result.counts[SOURCE_VK], 10)
        self.assertEqual(result.counts[SOURCE_YOUTUBE], 10)
        self.assertIn('Музыка VK 10', result.summary)

    def test_single_source_stays_single(self):
        """«Только YouTube» - значит только YouTube, без добавок из VK."""
        mixer, _vk, _rec = self.build()
        result = mixer.build(MixConfig(weights={SOURCE_VK: 0, SOURCE_YOUTUBE: 100,
                                                SOURCE_LOCAL: 0}, limit=15))
        self.assertTrue(all(t.source == SOURCE_YOUTUBE for t in result.tracks))
        self.assertEqual(len(result), 15)

    def test_local_files_join_the_mix(self):
        mixer, _vk, _rec = self.build(local=files())
        result = mixer.build(MixConfig(weights={SOURCE_VK: 40, SOURCE_YOUTUBE: 40,
                                                SOURCE_LOCAL: 20}, limit=20))
        self.assertTrue(result.counts[SOURCE_LOCAL])
        self.assertEqual(sum(result.counts.values()), 20)

    def test_known_mode_does_not_ask_for_the_feed(self):
        """«Знакомое» - это фонотека и история, лента YouTube здесь ни при чём."""
        mixer, vk, _rec = self.build()
        mixer.build(MixConfig(weights={SOURCE_VK: 100, SOURCE_YOUTUBE: 0,
                                       SOURCE_LOCAL: 0}, mode=MODE_KNOWN, limit=20))
        self.assertEqual(vk.queries, [])   # поиск нового не понадобился

    def test_discover_mode_searches_by_favourite_artists(self):
        mixer, vk, _rec = self.build()
        mixer.build(MixConfig(weights={SOURCE_VK: 100, SOURCE_YOUTUBE: 0,
                                       SOURCE_LOCAL: 0}, mode=MODE_DISCOVER,
                              limit=20))
        self.assertEqual(vk.queries, ['Deftones'])

    def test_query_is_passed_to_both_sources(self):
        mixer, vk, _rec = self.build()
        result = mixer.build(MixConfig(query='Placebo', limit=20))
        self.assertEqual(vk.queries, ['Placebo'])
        self.assertTrue(result.counts[SOURCE_YOUTUBE])

    def test_same_song_from_two_sources_is_taken_once(self):
        """«Та же песня, но с YouTube» подряд слушается как заедание."""
        vk = FakeVk(library=[row(1, 'Deftones', 'Sextape')])
        rec = FakeRecommender()
        rec.history = [yt('x', 'Sextape', 'Deftones'), yt('y', 'Digital Bath', 'Deftones')]
        rec.feed = list(rec.history)
        mixer = Mixer(FakeStore(), rec, lambda: vk, None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 70, SOURCE_YOUTUBE: 30,
                                                SOURCE_LOCAL: 0}, limit=20))
        titles = [t.title.lower() for t in result.tracks]
        self.assertEqual(titles.count('sextape'), 1)
        # Песня осталась за источником с большей долей
        kept = [t for t in result.tracks if t.title.lower() == 'sextape'][0]
        self.assertEqual(kept.source, SOURCE_VK)

    def test_copies_are_allowed_when_asked(self):
        vk = FakeVk(library=[row(1, 'Deftones', 'Sextape')])
        rec = FakeRecommender()
        rec.history = [yt('x', 'Sextape', 'Deftones')]
        rec.feed = list(rec.history)
        mixer = Mixer(FakeStore(), rec, lambda: vk, None)
        result = mixer.build(MixConfig(limit=20, unique_songs=False))
        titles = [t.title.lower() for t in result.tracks]
        self.assertEqual(titles.count('sextape'), 2)

    def test_hidden_and_recent_are_dropped(self):
        hidden = 'youtube:h1'
        recent = 'youtube:h2'
        store = FakeStore(hidden=[hidden], played=[recent])
        mixer = Mixer(store, FakeRecommender(), lambda: FakeVk(), None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 0, SOURCE_YOUTUBE: 100,
                                                SOURCE_LOCAL: 0}, limit=30))
        uids = {t.uid for t in result.tracks}
        self.assertNotIn(hidden, uids)
        self.assertNotIn(recent, uids)

    def test_recent_returns_when_repeats_allowed(self):
        recent = 'youtube:h2'
        store = FakeStore(played=[recent])
        mixer = Mixer(store, FakeRecommender(), lambda: FakeVk(), None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 0, SOURCE_YOUTUBE: 100,
                                                SOURCE_LOCAL: 0}, limit=30,
                                       shuffle=False, skip_recent=False))
        # Без перемешивания порядок источника сохраняется, иначе проверка
        # зависела бы от того, попал ли трек в случайную выборку
        self.assertIn(recent, {t.uid for t in result.tracks})

    def test_hidden_artist_is_dropped(self):
        store = FakeStore(hidden_artists=['linkin park'])
        mixer = Mixer(store, FakeRecommender(), lambda: FakeVk(), None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 0, SOURCE_YOUTUBE: 100,
                                                SOURCE_LOCAL: 0}, limit=30))
        self.assertEqual(result.tracks, [])

    def test_missing_source_is_named_honestly(self):
        """Молча подменять один источник другим нельзя - об этом должно быть сказано."""
        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: None, None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 50, SOURCE_YOUTUBE: 50,
                                                SOURCE_LOCAL: 0}, limit=20))
        self.assertTrue(any('Музыка VK' in note for note in result.notes))
        self.assertTrue(all(t.source == SOURCE_YOUTUBE for t in result.tracks))

    def test_search_instead_of_feed_is_marked(self):
        mixer = Mixer(FakeStore(), FakeRecommender(genuine=False), lambda: None, None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 0, SOURCE_YOUTUBE: 100,
                                                SOURCE_LOCAL: 0},
                                       mode=MODE_DISCOVER, limit=20))
        self.assertIn('YouTube: лента недоступна, взят поиск', result.notes)

    def test_no_sources_gives_empty_result(self):
        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: FakeVk(), None)
        result = mixer.build(MixConfig(weights={SOURCE_VK: 0, SOURCE_YOUTUBE: 0,
                                                SOURCE_LOCAL: 0}))
        self.assertEqual(result.tracks, [])
        self.assertEqual(result.summary, 'Пусто')

    def test_broken_source_does_not_break_the_mix(self):
        class Broken(FakeVk):
            def get_my_tracks(self):
                raise RuntimeError('VK не ответил')

        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: Broken(), None)
        result = mixer.build(MixConfig(mode=MODE_KNOWN, limit=20))
        self.assertTrue(result.tracks)
        self.assertTrue(any('VK не ответил' in note for note in result.notes))

    def test_library_is_asked_once_for_repeated_builds(self):
        """Пересобрать микс - обычное дело, а фонотека за минуту не меняется."""
        calls = []

        class Counting(FakeVk):
            def get_my_tracks(self):
                calls.append(1)
                return super().get_my_tracks()

        vk = Counting()
        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: vk, None)
        config = MixConfig(mode=MODE_KNOWN, limit=20)
        mixer.build(config)
        mixer.build(config)
        self.assertEqual(len(calls), 1)
        mixer.forget()      # после входа или выхода из VK список уже не тот
        mixer.build(config)
        self.assertEqual(len(calls), 2)

    # ---------- «на усмотрение программы» ----------
    def test_auto_config_uses_everything_that_works(self):
        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: FakeVk(), files())
        config = mixer.auto_config()
        self.assertEqual(config.sources, [SOURCE_VK, SOURCE_YOUTUBE, SOURCE_LOCAL])
        self.assertGreater(config.share(SOURCE_VK), config.share(SOURCE_LOCAL))

    def test_auto_config_without_vk(self):
        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: None, None)
        self.assertEqual(mixer.auto_config().sources, [SOURCE_YOUTUBE])

    def test_auto_config_without_network_takes_own_files(self):
        mixer = Mixer(FakeStore(), None, lambda: None, files())
        config = mixer.auto_config()
        self.assertEqual(config.sources, [SOURCE_LOCAL])
        self.assertEqual(config.share(SOURCE_LOCAL), 100)

    # ---------- рекомендации VK ----------
    def test_vk_recommendations_are_marked_as_vk_own(self):
        """Подборка самого VK помечается своей меткой, а не общей."""
        recoms = [row(500 + i, 'Slowdive', f'Recom {i}') for i in range(1, 21)]
        mixer, _vk, _rec = self.build(vk=FakeVk(recoms=recoms))
        config = MixConfig(weights={SOURCE_VK: 100, SOURCE_YOUTUBE: 0,
                                    SOURCE_LOCAL: 0}, mode=MODE_DISCOVER, limit=20)
        tracks = mixer.build(config).tracks
        self.assertTrue(tracks)
        self.assertEqual({t.meta.get(PROVENANCE) for t in tracks}, {KIND_VK_RECOMS})

    def test_search_instead_of_recommendations_is_labelled_and_said_aloud(self):
        """Своих рекомендаций нет - подмену поиском видно и в метке, и в примечании.

        Это и есть главное требование: поиск, выданный за рекомендации, - обман.
        """
        mixer, vk, _rec = self.build(vk=FakeVk(recoms=[]))
        config = MixConfig(weights={SOURCE_VK: 100, SOURCE_YOUTUBE: 0,
                                    SOURCE_LOCAL: 0}, mode=MODE_DISCOVER, limit=20)
        result = mixer.build(config)
        self.assertTrue(result.tracks)
        self.assertTrue(vk.queries)          # новое действительно искали
        self.assertEqual({t.meta.get(PROVENANCE) for t in result.tracks},
                         {KIND_FALLBACK})
        self.assertTrue(any('поиском' in note for note in result.notes), result.notes)

    def test_disabled_flag_skips_recommendations_entirely(self):
        recoms = [row(500 + i, 'Slowdive', f'Recom {i}') for i in range(1, 21)]
        mixer, vk, _rec = self.build(vk=FakeVk(recoms=recoms))
        config = MixConfig(weights={SOURCE_VK: 100, SOURCE_YOUTUBE: 0,
                                    SOURCE_LOCAL: 0}, mode=MODE_DISCOVER, limit=20,
                           vk_recoms=False)
        tracks = mixer.build(config).tracks
        self.assertTrue(vk.queries)
        self.assertEqual({t.meta.get(PROVENANCE) for t in tracks}, {KIND_FALLBACK})

    def test_broken_recommendations_do_not_break_the_mix(self):
        """VK ответил ошибкой - микс всё равно собирается, но уже поиском."""
        class Angry(FakeVk):
            def recommended_tracks(self, limit=60):
                raise RuntimeError('VK молчит')

        mixer, _vk, _rec = self.build(vk=Angry())
        config = MixConfig(weights={SOURCE_VK: 100, SOURCE_YOUTUBE: 0,
                                    SOURCE_LOCAL: 0}, mode=MODE_DISCOVER, limit=20)
        result = mixer.build(config)
        self.assertTrue(result.tracks)
        self.assertEqual({t.meta.get(PROVENANCE) for t in result.tracks},
                         {KIND_FALLBACK})

    def test_flag_survives_settings_round_trip(self):
        for value in (True, False):
            restored = MixConfig.from_dict(MixConfig(vk_recoms=value).to_dict())
            self.assertIs(restored.vk_recoms, value)
        # Старый settings.json без ключа не должен выключать рекомендации молча
        self.assertTrue(MixConfig.from_dict({'mode': MODE_MIXED}).vk_recoms)

    # ---------- продолжение волны ----------
    def test_continuation_keeps_the_mix_mixed(self):
        mixer, _vk, _rec = self.build()
        config = MixConfig(weights={SOURCE_VK: 50, SOURCE_YOUTUBE: 50,
                                    SOURCE_LOCAL: 0}, limit=20)
        played = {t.uid for t in mixer.build(config).tracks}
        more = mixer.continuation(config)(None, played, 12)
        self.assertTrue(more)
        self.assertFalse(played & {t.uid for t in more})
        self.assertEqual({t.source for t in more}, {SOURCE_VK, SOURCE_YOUTUBE})


class MixPageTests(unittest.TestCase):
    """Раздел «Микс» вхолостую: настройки, сборка и «на усмотрение программы»."""

    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def page(self, mixer=None):
        from app.ui.mix_page import MixPage
        mixer = mixer or Mixer(FakeStore(artists=['Deftones']), FakeRecommender(),
                               lambda: FakeVk(), files())
        page = MixPage(mixer, MixConfig(limit=20))
        page.resize(620, 700)
        page.show()
        self.app.processEvents()
        # Раздел должен помещаться в самое узкое окно приложения
        self.assertLessEqual(page.minimumSizeHint().width(), 620)
        self.addCleanup(page.close)
        return page

    def settle(self, rounds: int = 6, until=None) -> None:
        """Дождаться фоновой сборки волны.

        Пул потоков общий на весь прогон: `waitForDone` ждёт и чужие задачи, поэтому
        застрявший сетевой тест по соседству съедал наше ожидание целиком. Шагаем
        мелко и выходим сразу, как только пришёл нужный ответ."""
        if until is None:
            # Ждать нечего конкретного - как раньше: пул опустел, значит всё сделано
            for _round in range(rounds):
                QThreadPool.globalInstance().waitForDone(2000)
                self.app.processEvents()
            return
        # Есть чёткий признак готовности - ждём именно его. Запас щедрый нарочно:
        # при успехе выходим сразу, поэтому длинный предел ничего не замедляет,
        # зато переживает занятый чужими задачами пул
        deadline = time.monotonic() + rounds * 10.0
        while time.monotonic() < deadline:
            QThreadPool.globalInstance().waitForDone(50)
            self.app.processEvents()
            if until():
                return
        self.app.processEvents()

    def test_builds_and_plays(self):
        page = self.page()
        played = []
        page.play_requested.connect(lambda tracks, index: played.append((tracks, index)))
        page.start(play=True)
        self.settle(until=lambda: played)
        self.assertTrue(played)
        tracks, index = played[0]
        self.assertEqual(index, 0)
        self.assertEqual(len(page.list.tracks()), len(tracks))
        self.assertIn('треков', page._status.text())

    def test_unavailable_source_is_switched_off(self):
        """Вход в VK не выполнен - доля VK не должна оставаться включённой."""
        mixer = Mixer(FakeStore(), FakeRecommender(), lambda: None, None)
        page = self.page(mixer)
        page.refresh_sources()
        self.assertFalse(page._rows[SOURCE_VK].check.isChecked())
        self.assertFalse(page._rows[SOURCE_VK].check.isEnabled())
        self.assertTrue(page._rows[SOURCE_YOUTUBE].check.isEnabled())

    def test_auto_button_fills_the_settings(self):
        page = self.page()
        page.start_auto()
        self.settle()
        # Кнопка «на усмотрение программы» не должна выглядеть непредсказуемой:
        # выбранные ею доли видны в тех же ползунках
        self.assertEqual(page.config().sources,
                         [SOURCE_VK, SOURCE_YOUTUBE, SOURCE_LOCAL])

    def test_mood_tile_fills_the_settings_and_plays(self):
        """Плитка настроения - не отдельная ветка, а те же ручки и тот же запуск."""
        from app.core import moods
        page = self.page()
        played = []
        page.play_requested.connect(lambda tracks, index: played.append(tracks))
        saved = []
        page.config_changed.connect(saved.append)

        mood = moods.MOODS_BY_KEY['focus']
        page.start_mood(mood)
        # Название видно сразу: сборка занимает время, и всё это время должно быть
        # понятно, что именно включили
        self.assertIn(mood.title, page._status.text())
        self.settle(until=lambda: played)

        config = page.config()
        self.assertEqual(config.mode, mood.config().mode)
        self.assertEqual(config.query, mood.config().query)
        self.assertEqual(config.sources, mood.config().sources)
        # Настройки уехали в settings.json, а не остались только на экране
        self.assertTrue(saved)
        self.assertTrue(played)

    def test_mood_tile_keeps_the_manual_portion_size(self):
        """Пресет задаёт характер, а размер порции человек выбрал сам."""
        from app.core import moods
        page = self.page()
        before = page.config().limit
        page.start_mood(moods.MOODS[0])
        self.assertEqual(page.config().limit, before)

    def test_settings_are_saved_on_change(self):
        page = self.page()
        saved = []
        page.config_changed.connect(saved.append)
        page._rows[SOURCE_LOCAL].check.setChecked(True)
        self.assertTrue(saved)
        self.assertTrue(saved[-1]['weights'][SOURCE_LOCAL])

    def test_nothing_selected_blocks_the_button(self):
        page = self.page()
        for row in page._rows.values():
            row.check.setChecked(False)
        self.assertFalse(page._play_btn.isEnabled())

    def test_late_answer_does_not_overwrite_a_newer_mix(self):
        """Пока собиралась первая волна, человек нажал «Слушать» ещё раз."""
        page = self.page()
        page.start(play=False)
        stale = page._gen
        page._busy = False        # как будто первая сборка ещё в пути
        page.start(play=False)
        self.settle(until=lambda: page.list.tracks())
        self.assertGreater(page._gen, stale)
        self.assertTrue(page.list.tracks())


if __name__ == '__main__':
    unittest.main()
