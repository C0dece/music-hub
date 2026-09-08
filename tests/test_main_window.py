"""Главное окно вхолостую: раскладка на разных ширинах и радио по треку.

Окно тяжёлое, поэтому всё, что лезет наружу, здесь отключено: значок у часов,
глобальные клавиши, мост для расширения, вход в VK и чтение куков браузера.
Проверяем ровно то, что ломается чаще всего, — раскладку и связи сигналов.

База своя, временная: тест не должен трогать настоящую историю прослушивания.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QThreadPool

from app import config
from app.core import store as store_mod
from app.core.store import Store
from app.core.track import Track
from app.ui.now_playing import BAR_HEIGHT, MIN_BAR_WIDTH
from .qt_app import qt_app

# Те же ширины, что и в требованиях: от самой узкой до полноэкранной.
# Начинаем с минимума окна, а не с круглого числа: ниже него окно не сжимается,
# и resize() на меньшую ширину проверял бы не раскладку, а границу Qt
WIDTHS = (664, 700, 800, 900, 1080, 1280, 1440, 1920)

QUIET = ('_setup_tray', '_setup_hotkeys', '_setup_bridge', '_try_auto_vk_login',
         '_detect_proxy')


def yt(video_id='abc', title='Numb', artist='Linkin Park') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title=title, artist=artist, duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


class FakeBackend:
    """Заглушка вместо настоящего воспроизведения: играть в тесте нечем."""

    def __init__(self):
        self.played: list[Track] = []

    def can_play(self, track) -> bool:
        return True

    def play(self, track) -> None:
        self.played.append(track)

    def stop(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        os.unlink(self.db_path)
        # Подменяем общую базу приложения: главное окно берёт её через store()
        self._saved_instance = store_mod._instance
        store_mod._instance = Store(self.db_path)

        from app.ui import main_window as mw
        self._patches = [mock.patch.object(mw.MainWindow, name, lambda *a, **k: None)
                         for name in QUIET]
        self._patches.append(mock.patch.object(mw, 'preload_preview_cookies',
                                               lambda *a, **k: None))
        # Окно читает настоящий settings.json: сохранённое у разработчика
        # «меню свёрнуто» роняло проверки раскладки. Берём чистые значения
        self._patches.append(mock.patch.object(
            config, 'load_settings', lambda: dict(config.DEFAULT_SETTINGS)))
        self._patches.append(mock.patch.object(config, 'save_settings',
                                               lambda settings: None))
        # Отметку о блокировке уводим во временную папку. Без этого тесты писали её в
        # настоящий config/ живого пользователя — с его собственным user_id, взятым из
        # настоящего vk_token.json. Программа при следующем запуске честно читала эту
        # отметку и объявляла аккаунт заблокированным: прогон тестов оставлял человека
        # без музыки до нажатия «мой аккаунт разблокирован»
        self._blocked_tmp = tempfile.TemporaryDirectory()
        self._patches.append(mock.patch.object(
            config, 'VK_BLOCKED_FILE',
            Path(self._blocked_tmp.name) / 'vk_blocked.json'))
        # Обложки едут по сети и переживают тест, который их заказал: ответ
        # приходит уже в чужой settle() и валит его таймаутами. Здесь нужны не
        # картинки, а проводка, поэтому загрузку закрываем на весь файл
        from app.ui import covers as covers_mod
        self._patches.append(mock.patch.object(
            covers_mod, '_fetch',
            lambda url, path: (_ for _ in ()).throw(OSError('обложки в тесте не грузим'))))
        for patch in self._patches:
            patch.start()
        self.window = mw.MainWindow()
        # Настоящее воспроизведение в тесте не нужно и невозможно
        self.window._player._backends = [FakeBackend()]
        # Автопродолжение здесь только мешает: оно полезло бы за подборками
        # в сеть и дописало бы в очередь настоящие треки
        self.window._player.set_recommender(None)

    def tearDown(self):
        self.window._quitting = True
        self.window.close()
        self.app.processEvents()
        for patch in self._patches:
            patch.stop()
        store_mod._instance = self._saved_instance
        self._blocked_tmp.cleanup()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def settle(self, rounds: int = 4) -> None:
        for _round in range(rounds):
            QThreadPool.globalInstance().waitForDone(2000)
            self.app.processEvents()

    # ---------- навигация ----------
    def test_sections_open_their_pages(self):
        """Меню слева выбирает раздел, вкладка внутри — страницу."""
        from app.ui import main_window as mw
        window = self.window
        for row, (_title, pages) in enumerate(mw.NAV_SECTIONS):
            window._nav.setCurrentRow(row)
            self.assertEqual(window._pages.currentIndex(), pages[0])
            # Вкладки показываем только там, где есть между чем выбирать
            self.assertEqual((not window._section_tabs.isHidden()), len(pages) > 1)
            for tab, page in enumerate(pages):
                window._section_tabs.setCurrentIndex(tab)
                self.assertEqual(window._pages.currentIndex(), page)

    def test_page_indices_match_widgets(self):
        """Номера PAGE_* и порядок виджетов в стопке обязаны совпадать.

        Карта разделов ссылается на страницы номерами, поэтому лишний addWidget
        не в том месте молча открывал бы соседнюю страницу.
        """
        from app.ui import main_window as mw
        window = self.window
        self.assertEqual(window._pages.count(), len(mw.PAGE_TITLES))
        self.assertEqual(len(mw.PAGE_NAMES), len(mw.PAGE_TITLES))
        expected = {mw.PAGE_TRACKS: window._tracks_page,
                    mw.PAGE_LOCAL: window._local_page,
                    mw.PAGE_HISTORY: window._history_page,
                    mw.PAGE_LIBRARY: window._library_page}
        for index, page in expected.items():
            self.assertIs(window._pages.widget(index), page, mw.PAGE_TITLES[index])
        # Все страницы разложены по разделам: недостижимых номеров нет
        listed = {page for _title, pages in mw.NAV_SECTIONS for page in pages}
        self.assertEqual(listed, set(range(len(mw.PAGE_TITLES))))

    def test_local_and_cache_pages_read_their_own_lists(self):
        """Обе страницы фонотеки смотрят в одну базу, но показывают разное.

        Отдельного «Кэша» больше нет: офлайн-копии отбирает фильтр в «Треках».
        """
        window = self.window
        saved, cached = yt('s1'), yt('s2')
        window._store.save_many_to_library([saved, cached])
        window._store.set_local_path(cached.uid, __file__, cached=True)
        # База изменилась мимо окна, поэтому страницы помечаем так же, как это
        # делают сами действия «в мою музыку» и «сохранить офлайн»
        window._mark_tracks_dirty()

        window._go_to('local')
        self.assertEqual(window._local_page.list.tracks(), [])   # своих файлов нет
        window._go_to('tracks')
        page = window._tracks_page
        self.assertEqual({t.uid for t in page.list.tracks()},
                         {saved.uid, cached.uid})
        # «Только офлайн» оставляет ровно то, что раньше показывал «Кэш»
        page._only_offline.setChecked(True)
        self.assertEqual([t.uid for t in page.list.tracks()], [cached.uid])
        page._only_offline.setChecked(False)

    def test_finished_offline_copy_reaches_the_offline_filter(self):
        """Копия докачалась в фоне — «Треки» узнают об этом сами, без чужих правок."""
        window = self.window
        track = yt('s3')
        window._store.save_to_library(track)
        window._go_to('tracks')
        page = window._tracks_page
        page._only_offline.setChecked(True)
        self.assertEqual(page.list.tracks(), [])

        # Так заканчивается настоящая загрузка: файл на месте, сигнал разослан
        window._store.set_local_path(track.uid, __file__, cached=True)
        window._offline.changed.emit(track.uid)
        self.app.processEvents()
        self.assertEqual([t.uid for t in page.list.tracks()], [track.uid])
        page._only_offline.setChecked(False)

    def test_go_to_finds_page_in_any_section(self):
        """Переход по имени сам поднимает нужный раздел и вкладку."""
        from app.ui import main_window as mw
        window = self.window
        window._go_to('vk')
        self.assertEqual(window._pages.currentIndex(), mw.PAGE_VK)
        self.assertEqual(window._nav.currentRow(), mw.section_of_page(mw.PAGE_VK))
        # Раздел помнит, что в нём открывали: возврат не сбрасывает на первую вкладку
        window._go_to('home')
        window._nav.setCurrentRow(mw.section_of_page(mw.PAGE_VK))
        self.assertEqual(window._pages.currentIndex(), mw.PAGE_VK)

    def test_favorites_open_playlists(self):
        """Отдельного «Избранного» нет — переход ведёт в «Любимое»."""
        from app.ui import main_window as mw
        window = self.window
        store_mod._instance.add_favorite(yt())
        window._go_to('favorites')
        self.settle()
        self.assertEqual(window._pages.currentIndex(), mw.PAGE_PLAYLISTS)
        self.assertEqual(window._playlists_page._title.text(), 'Любимое')

    # ---------- первый показ страниц ----------
    def test_every_lazy_page_is_dirty_from_the_start(self):
        """Страница, читающая базу при показе, обязана быть в карте с самого начала.

        Забытая покажет при первом открытии пустую рамку: списка нет, потому что
        `reload()` не звали, и подсказки нет, потому что `EmptyState` спрятан в
        конструкторе. Список берём из самого обработчика показа, чтобы новая
        страница не могла добавиться мимо этой проверки.
        """
        from app.ui import main_window as mw
        lazy = (mw.PAGE_HOME, mw.PAGE_TRACKS, mw.PAGE_LOCAL,
                mw.PAGE_PLAYLISTS, mw.PAGE_HISTORY)
        for index in lazy:
            with self.subTest(page=mw.PAGE_NAMES[index]):
                # Именно в карте, а не «истинно»: «Главная» открыта с самого начала
                # и свой флаг уже израсходовала, пока окно строилось
                self.assertIn(index, self.window._page_dirty,
                              f'{mw.PAGE_TITLES[index]}: не прочитает базу при показе')

    def test_new_pages_show_something_when_opened_first_time(self):
        """Открыли «С компьютера» на чистой базе — видно подсказку, не пустоту."""
        for name, page in (('local', self.window._local_page),):
            with self.subTest(page=name):
                self.window._go_to(name)
                self.settle()
                # `isHidden`, а не `isVisible`: окно в тесте не показывают, и
                # `isVisible` ложен у всего подряд — нужно состояние самого виджета
                self.assertFalse(page._empty.isHidden(),
                                 'пустой список без подсказки — это пустая рамка')
                self.assertTrue(page._list.isHidden(),
                                'пустой список не должен занимать место')

    # ---------- кнопки запуска на главной ----------
    def quiet_mix(self):
        """Собирать волну по-настоящему тесту нечем: за ней лезут в сеть.

        Проверяем проводку — переход и предзаполненные ручки, — поэтому сборку
        подменяем и заодно ловим конфигурацию, с которой её позвали.
        """
        asked: list = []

        def build(config):
            asked.append(config)
            return None

        self.window._mix_page._mixer.build = build
        return asked

    def test_launch_cards_open_their_pages(self):
        """Каждая плитка «включить музыку» ведёт на свою вкладку."""
        from app.ui import home_page as hp
        from app.ui import main_window as mw
        window = self.window
        self.quiet_mix()
        for launch in hp.LAUNCHES:
            with self.subTest(launch=launch.page):
                self.assertIn(launch.page, mw.PAGE_NAMES)
                window._start_mix_from_home(launch.page, launch.config().to_dict())
                self.assertEqual(window._pages.currentIndex(),
                                 mw.PAGE_NAMES.index(launch.page))

    def test_launch_cards_preload_the_mix_settings(self):
        """Плитка не чёрный ящик: её настройки видно на странице микса.

        Проверяем именно долю источника — ради неё плитки и различаются.
        """
        from app.ui import home_page as hp
        window = self.window
        self.quiet_mix()
        for launch in hp.LAUNCHES:
            with self.subTest(launch=launch.page):
                wanted = launch.config()
                window._start_mix_from_home(launch.page, wanted.to_dict())
                shown = window._mix_page.config()
                self.assertEqual(shown.weights, wanted.weights)
                self.assertEqual(shown.mode, wanted.mode)
                self.assertEqual(shown.vk_recoms, wanted.vk_recoms)

    def test_launch_card_signal_reaches_the_window(self):
        """Сигнал главной проведён: нажатие плитки открывает раздел на деле."""
        from app.ui import home_page as hp
        from app.ui import main_window as mw
        window = self.window
        self.quiet_mix()
        launch = hp.LAUNCHES[-1]        # «Из своих файлов» — не требует сети
        window._home_page.mix_requested.emit(launch.page, launch.config().to_dict())
        self.assertEqual(window._pages.currentIndex(),
                         mw.PAGE_NAMES.index(launch.page))

    def test_launch_keeps_the_portion_size_and_autoplay(self):
        """Кнопка про источники, а не про объём: порция и автопродолжение свои."""
        from app.ui import home_page as hp
        window = self.window
        self.quiet_mix()
        page = window._mix_page
        before_limit = page.config().limit
        before_autoplay = page.config().autoplay
        window._start_mix_from_home('mix', hp.LAUNCHES[0].config().to_dict())
        self.assertEqual(page.config().limit, before_limit)
        self.assertEqual(page.config().autoplay, before_autoplay)

    # ---------- моя музыка ----------
    def test_library_toggle_adds_and_removes(self):
        """«В мою музыку» — одно действие на всё выделение, туда и обратно."""
        from app.ui import main_window as mw
        window = self.window
        window._toggle_library([yt(), yt('b', title='Faint')])
        self.assertEqual(store_mod._instance.saved_count(), 2)
        # Страница «Треки» узнаёт об изменении даже закрытой
        self.assertTrue(window._page_dirty[mw.PAGE_TRACKS])

        window._toggle_library([yt(), yt('b', title='Faint')])
        self.assertEqual(store_mod._instance.saved_count(), 0)

    def test_offline_request_puts_track_in_library(self):
        """Сохранённое офлайн обязано быть и в фонотеке: иначе копия лежала бы
        на диске, а в списке трека не было бы."""
        window = self.window
        with mock.patch.object(window._offline, 'ensure', return_value=1) as ensure:
            window._toggle_offline([yt()])
        ensure.assert_called_once()
        self.assertTrue(store_mod._instance.is_saved(yt().uid))

    def test_favorite_is_saved_offline_when_asked(self):
        window = self.window
        window._settings['offline_favorites'] = True
        with mock.patch.object(window._offline, 'ensure', return_value=1) as ensure:
            window._toggle_favorites([yt()])
        self.assertEqual([t.uid for t in ensure.call_args[0][0]], [yt().uid])

        # Снятое сердечко копию не удаляет — место освободит лимит
        with mock.patch.object(window._offline, 'ensure') as ensure:
            window._toggle_favorites([yt()])
        ensure.assert_not_called()
        self.assertFalse(store_mod._instance.is_favorite(yt().uid))

    def test_favorite_without_auto_offline(self):
        window = self.window
        window._settings['offline_favorites'] = False
        with mock.patch.object(window._offline, 'ensure') as ensure:
            window._toggle_favorites([yt()])
        ensure.assert_not_called()

    def test_counters_stay_on_their_labels(self):
        """Счётчик главной страницы раздела — в меню, остальных — на вкладках."""
        from app.ui import main_window as mw
        window = self.window
        window._nav_counts[mw.PAGE_QUEUE] = 3
        window._nav_counts[mw.PAGE_LIBRARY] = 7
        window._nav.setCurrentRow(mw.section_of_page(mw.PAGE_LIBRARY))
        queue_row = mw.section_of_page(mw.PAGE_QUEUE)
        self.assertIn('(3)', window._nav.item(queue_row).text())
        tab = mw.NAV_SECTIONS[window._nav.currentRow()][1].index(mw.PAGE_LIBRARY)
        self.assertIn('(7)', window._section_tabs.tabText(tab))

    # ---------- раскладка ----------
    def test_window_fits_every_width(self):
        """Ни на одной ширине окно не должно требовать больше, чем ему дали."""
        window = self.window
        window.show()
        self.app.processEvents()
        for width in WIDTHS:
            window.resize(width, 820)
            self.app.processEvents()
            for index in range(window._pages.count()):
                window._pages.setCurrentIndex(index)
                self.app.processEvents()
                self.assertLessEqual(
                    window.minimumSizeHint().width(), width,
                    f'раздел {index} не помещается в {width} px')

    def test_window_never_shrinks_below_the_player_bar(self):
        """Полосе плеера нужно 600 px, и окно не вправе стать уже неё с меню."""
        window = self.window
        window.show()
        self.app.processEvents()
        window.resize(200, 200)
        self.app.processEvents()
        self.assertGreaterEqual(window._now_playing.width(), MIN_BAR_WIDTH)

    def test_player_bar_keeps_its_height_on_every_resize(self):
        """Полоса плеера не растёт ни на одном кадре изменения размера.

        Раньше её высота была гибкой, и на кадре, где середина окна ещё не
        пересчиталась, полоса получала 94, 134 или все 174 px вместо 73 — с
        пустотой над собой. При перетаскивании края мышью такой кадр приходил
        на каждое движение, и интерфейс выглядел подтормаживающим и чёрным."""
        window = self.window
        window.show()
        self.app.processEvents()
        bar = window._now_playing
        # Без processEvents между шагами: именно так приходят события при
        # перетаскивании края окна, и именно на них полоса разъезжалась
        for width, height in ((664, 400), (1500, 900), (700, 320), (1200, 700),
                              (664, 950), (1600, 300), (900, 600)):
            window.resize(width, height)
            self.app.processEvents()
            self.assertEqual(bar.height(), BAR_HEIGHT,
                             f'полоса разъехалась на {width}x{height}')

    # ---------- настоящий клип вместо обложки ----------
    def clip_run(self, track, answer='clip0000000'):
        """Прогнать поиск клипа без потоков: ответ отдаём сразу.

        Возвращает список того, что окно попросило подставить в страницу."""
        from app.ui import main_window as mw

        swapped = []
        # Включение видеорежима само зовёт поиск и записывает песню в память
        # окна: здесь нам нужен именно вызов из теста, а не тот, попутный
        self.window._clip_tried.clear()
        self.window._yt_backend.swap_clip = (
            lambda video_id, position=0: swapped.append(video_id) or True)
        with mock.patch.object(mw, 'run_async',
                               lambda fn, on_done, *a: on_done(answer, None)):
            self.window._want_clip(track)
        return swapped

    def test_clip_replaces_the_album_art(self):
        """Обычный случай: играет песня, клип нашёлся, страница переключается."""
        from app.core.player_controller import MODE_VIDEO

        track = yt()
        self.window._player.queue.set_tracks([track])
        self.window._player.set_mode(MODE_VIDEO)
        self.assertEqual(self.clip_run(track), ['clip0000000'])

    def test_clip_is_dropped_when_the_song_already_changed(self):
        """Пока искали, человек переключил трек.

        Подставить клип теперь означало бы оборвать другую песню на полуслове."""
        from app.core.player_controller import MODE_VIDEO

        track = yt()
        self.window._player.set_mode(MODE_VIDEO)
        self.window._player.queue.set_tracks([yt('other', 'Faint')])
        self.assertEqual(self.clip_run(track), [])

    def test_clip_is_dropped_in_audio_only_mode(self):
        """Пока искали, режим сменили на «только звук» — картинки больше нет."""
        from app.core.player_controller import MODE_AUDIO

        track = yt()
        self.window._player.queue.set_tracks([track])
        self.window._player.set_mode(MODE_AUDIO)
        self.assertEqual(self.clip_run(track), [])

    def test_a_song_without_a_clip_changes_nothing(self):
        from app.core.player_controller import MODE_VIDEO

        track = yt()
        self.window._player.queue.set_tracks([track])
        self.window._player.set_mode(MODE_VIDEO)
        self.assertEqual(self.clip_run(track, answer=''), [])

    def test_the_same_song_is_searched_once(self):
        """Второй заход за тем же клипом в сеть не идёт.

        Раздел «Видео» пересобирается на каждый чих — на смену размера окна, на
        возврат из полного экрана, — и без этой памяти каждый такой пересчёт
        оборачивался бы новым запросом."""
        from app.core.player_controller import MODE_VIDEO
        from app.ui import main_window as mw

        track = yt()
        self.window._player.queue.set_tracks([track])
        self.window._player.set_mode(MODE_VIDEO)
        asked = []
        self.window._clip_tried.clear()
        with mock.patch.object(mw, 'run_async',
                               lambda fn, on_done, *a: asked.append(a)):
            self.window._want_clip(track)
            self.window._want_clip(track)
        self.assertEqual(len(asked), 1)

    def test_video_note_clears_even_while_the_stage_is_hidden(self):
        """Видео пошло, пока раздел «Видео» ещё не открыт.

        Раньше окно молчало о состоянии, пока сцена скрыта, и «играю» пропадало.
        Человек открывал раздел, а поверх картинки стояла непрозрачная подпись
        «Готовим видео…» — та самая чернота, которая не уходила и после
        выключения видеорежима."""
        from app.core.player_controller import STATE_LOADING, STATE_PLAYING

        window = self.window
        window.show()
        self.app.processEvents()
        stage = window._video_stage
        self.assertFalse(stage.isVisible())
        window._on_video_state(STATE_LOADING)
        self.assertEqual(stage._note.text(), 'Готовим видео…')
        window._on_video_state(STATE_PLAYING)
        self.assertEqual(stage._note.text(), '')

    def test_queue_panel_gives_way_in_narrow_window(self):
        """Первой уступает очередь, потом сужается список разделов."""
        window = self.window
        window._queue_wanted = True
        # Пустую очередь окно прячет само, а здесь проверяется реакция на ширину
        window._player.enqueue([yt('q1'), yt('q2')])
        window.show()
        window.resize(1280, 820)
        self.app.processEvents()
        self.assertTrue(window._queue_panel.isVisible())
        wide_nav = window._nav.width()

        window.resize(700, 820)
        self.app.processEvents()
        self.assertFalse(window._queue_panel.isVisible())
        self.assertLess(window._nav.width(), wide_nav)

        window.resize(1280, 820)
        self.app.processEvents()
        self.assertTrue(window._queue_panel.isVisible())   # вернулась сама

    def test_empty_queue_hides_panel(self):
        """Пустая очередь не отнимает треть окна: панель ждёт первого трека."""
        window = self.window
        window._queue_wanted = True
        window.show()
        window.resize(1280, 820)
        self.app.processEvents()
        self.assertFalse(window._queue_panel.isVisible())

        window._player.enqueue([yt('q1')])
        self.app.processEvents()
        self.assertTrue(window._queue_panel.isVisible())   # появилась сама

        window._player.clear_queue()
        self.app.processEvents()
        self.assertFalse(window._queue_panel.isVisible())

    # ---------- радио ----------
    def test_radio_keeps_seed_first_and_appends(self):
        """Радио начинается с самого трека, похожее приезжает следом."""
        seed = yt(title='Sextape', artist='Deftones')
        found = [yt('b', title='Digital Bath'), yt('c', title='Passenger')]
        with mock.patch.object(self.window._recommender, 'radio',
                               return_value=found):
            self.window._start_radio(seed)
            self.assertEqual(self.window._player.current.uid, seed.uid)
            self.settle()
        queue = self.window._player.queue.tracks
        self.assertEqual([track.title for track in queue],
                         ['Sextape', 'Digital Bath', 'Passenger'])
        self.assertEqual(self.window._player.current.uid, seed.uid)

    def test_late_radio_answer_does_not_restart_playback(self):
        """Пока ходили в сеть, человек включил другое — прерывать его нельзя."""
        seed = yt(title='Sextape', artist='Deftones')
        other = yt('z', title='One Step Closer')
        found = [yt('b', title='Digital Bath')]

        def slow_radio(track, limit=25):
            # За время «запроса» очередь успевает смениться
            self.window._player.play_tracks([other], 0)
            return found

        with mock.patch.object(self.window._recommender, 'radio',
                               side_effect=slow_radio):
            self.window._start_radio(seed)
            self.settle()
        # Играет то, что выбрал человек, а найденное просто дописано в конец
        self.assertEqual(self.window._player.current.uid, other.uid)
        titles = [track.title for track in self.window._player.queue.tracks]
        self.assertEqual(titles, ['One Step Closer', 'Digital Bath'])

    # ---------- тихий перезаход в VK ----------
    def test_failed_silent_login_is_retried_not_surrendered(self):
        """Первая неудача — не повод просить пароль: пробуем снова сами."""
        with mock.patch.object(self.window._vk_panel, 'show_session_lost') as shown:
            self.window._on_vk_session_lost('нет сети')
        # Кнопку входа не показали: автоматика ещё не отработала своё
        shown.assert_not_called()
        self.assertTrue(self.window._vk_session_retry.isActive())

    def test_button_appears_only_when_pauses_reach_the_limit(self):
        """Дошли до предельной паузы — молчать дальше нечестно, показываем кнопку."""
        from app.ui import main_window as mw
        self.window._vk_session_delay = mw.VK_SESSION_RETRY_MAX
        with mock.patch.object(self.window._vk_panel, 'show_session_lost') as shown:
            self.window._on_vk_session_lost('и снова не вышло')
        shown.assert_called_once()

    def test_retry_never_resets_the_keepers_attempt_counter(self):
        """Сброс счётчика перед попыткой отменял весь антишторм keeper'а.

        Из-за него окно приводило keeper к VK каждые пять минут без конца, а сторож
        сессии добавлял свой запрос за треками каждые десять — за час набиралось около
        пятнадцати неудачных обращений, и VK блокировал аккаунт. Считать неудачи —
        работа keeper'а, окно только предлагает попытку."""
        with mock.patch.object(self.window._vk_keeper, 'reset') as reset,                 mock.patch.object(self.window._vk_keeper, 'try_restore',
                                  return_value=True):
            self.window._retry_vk_session()
        reset.assert_not_called()

    def test_watchdog_stays_quiet_while_a_repair_is_pending(self):
        """Починка уже назначена — лишний запрос за треками только злит VK."""
        self.window._vk_client = mock.Mock(user_id=1)
        try:
            # Без назначенной починки сторож ходит в VK как обычно
            with mock.patch('app.ui.main_window.run_async') as called:
                self.window._check_vk_session()
            called.assert_called_once()
            # А с ней — молчит: ответ и так известен, сессия мертва
            self.window._schedule_vk_session_retry()
            with mock.patch('app.ui.main_window.run_async') as called:
                self.window._check_vk_session()
            called.assert_not_called()
        finally:
            self.window._vk_client = None
            self.window._vk_session_retry.stop()
            self.window._vk_session_delay = 0

    def test_silent_retry_pauses_are_not_shorter_than_the_keepers_own(self):
        """Лестница пауз окна не должна обгонять антишторм keeper'а."""
        from app.core import vk_session_keeper as keeper_mod
        from app.ui import main_window as mw
        self.assertGreaterEqual(mw.VK_SESSION_RETRY_FIRST, keeper_mod.MIN_INTERVAL)
        self.assertGreaterEqual(mw.VK_SESSION_RETRY_MAX, keeper_mod.COOLDOWN)

    def test_logout_stops_the_silent_retry(self):
        """После выхода из аккаунта возвращать сессию сайта незачем."""
        self.window._schedule_vk_session_retry()
        self.assertTrue(self.window._vk_session_retry.isActive())
        self.window._cancel_vk_retry()
        self.assertFalse(self.window._vk_session_retry.isActive())
        self.assertEqual(self.window._vk_session_delay, 0)

    # ---------- VK заблокировал аккаунт ----------
    def test_blocked_account_stops_every_timer(self):
        """Повторять нечего: VK не пускает сам аккаунт, а не программу.

        Раньше блокировка проходила общей веткой «причина временная, повторим позже»:
        оба таймера тикали вхолостую, панель показывала «Обновляю вход», и снаружи это
        выглядело как зависшая программа."""
        from app.core.vk_client import VkAccountBlocked
        self.window._schedule_vk_session_retry()
        self.window._vk_session_watch.start(60_000)
        with mock.patch.object(self.window._vk_panel, 'show_account_blocked') as shown:
            self.window._on_vk_session_checked(False, VkAccountBlocked('заблокирован'))
        shown.assert_called_once()
        self.assertFalse(self.window._vk_session_retry.isActive())
        self.assertFalse(self.window._vk_session_watch.isActive())
        self.assertEqual(self.window._vk_session_delay, 0)

    def test_blocked_account_never_offers_a_new_login(self):
        """Кнопка входа здесь была бы обманом: пароль ничего не изменит."""
        with mock.patch.object(self.window._vk_panel, 'show_session_lost') as lost,                 mock.patch.object(self.window._vk_panel, 'show_account_blocked'):
            self.window._on_vk_account_blocked('заблокирован')
        lost.assert_not_called()

    def test_tests_never_touch_the_real_blocked_mark(self):
        """Прогон тестов не должен оставлять человека без музыки.

        Так и было: `_on_vk_account_blocked` звал `save_vk_blocked`, а тот писал в
        настоящий `config/`, подставляя user_id из настоящего `vk_token.json`. Отметка
        переживала прогон, и следующий запуск программы честно объявлял живой аккаунт
        заблокированным. Ловушка была невидимой — её никто не проверял."""
        self.assertNotEqual(config.VK_BLOCKED_FILE, config.CONFIG_DIR / 'vk_blocked.json')
        with mock.patch.object(self.window._vk_panel, 'show_account_blocked'):
            self.window._on_vk_account_blocked('заблокирован')
        self.assertFalse((config.CONFIG_DIR / 'vk_blocked.json').exists(),
                         'тест записал отметку о блокировке в настоящий config/')

    def _auto_login(self):
        """Настоящий `_try_auto_vk_login`.

        Брать его у класса нельзя: QUIET заглушает метод на всё время теста, и из
        класса пришла бы заглушка, молча ничего не делающая. Достаём исходную функцию
        из самой заплатки и привязываем к окну."""
        for patch in self._patches:
            if getattr(patch, 'attribute', None) == '_try_auto_vk_login':
                return patch.temp_original.__get__(self.window)
        self.fail('заглушка _try_auto_vk_login не найдена')

    def test_mark_does_not_replace_the_check_on_startup(self):
        """Отметка о блокировке не отменяет первую проверку за запуск.

        Сутки в `vk_blocked_expired` отсчитываются от постановки отметки, а программу
        закрывают на ночь: у того, кто закрывает вечером и открывает утром, срок не
        выходил никогда. Живой аккаунт объявлялся заблокированным по памяти, и вернуть
        музыку можно было только кнопкой «мой аккаунт разблокирован»."""
        with mock.patch.object(config, 'load_vk_blocked', lambda: {'user_id': 1,
                                                                  'since': time.time()}),                 mock.patch.object(config, 'load_vk_token',
                                  lambda: {'access_token': 'tok'}),                 mock.patch.object(self.window, '_connect_vk_client') as connect,                 mock.patch.object(self.window, '_on_vk_account_blocked') as blocked:
            self._auto_login()()
        connect.assert_called_once()
        blocked.assert_not_called()

    def test_mark_still_stops_repeat_attempts_within_one_run(self):
        """Проверка одна на запуск, а не одна на попытку.

        Иначе таймер повторов превратил бы отметку в пустой звук и погнал бы к VK
        поток запросов по аккаунту, который тот уже пометил, — ровно то поведение,
        из-за которого блокировку и не снимают."""
        with mock.patch.object(config, 'load_vk_blocked', lambda: {'user_id': 1,
                                                                  'since': time.time()}),                 mock.patch.object(config, 'load_vk_token',
                                  lambda: {'access_token': 'tok'}),                 mock.patch.object(self.window, '_connect_vk_client') as connect,                 mock.patch.object(self.window, '_on_vk_account_blocked') as blocked:
            auto = self._auto_login()
            auto()
            auto()
        self.assertEqual(connect.call_count, 1)
        blocked.assert_called_once()

    def test_reading_the_mark_does_not_rewrite_it(self):
        """Отказ по своей же отметке ничего не записывает.

        Прежде эта ветка звала `_on_vk_account_blocked`, а тот — `save_vk_blocked`:
        состояние подтверждало само себя, ни разу не спросив VK."""
        with mock.patch.object(config, 'load_vk_blocked', lambda: {'user_id': 1,
                                                                  'since': time.time()}),                 mock.patch.object(config, 'save_vk_blocked') as saved,                 mock.patch.object(self.window._vk_panel, 'show_account_blocked'):
            self.window._vk_blocked_rechecked = True
            self._auto_login()()
        saved.assert_not_called()



class GeometryAuditTests(MainWindowTests):
    """Ни один виджет не наезжает на соседа и не вылезает за родителя.

    Проверка на глаз здесь невозможна, а жалоба была именно про это: «UI много
    где неправильно накладывается друг на друга». Поэтому смотрим геометрию
    числами — все страницы на сетке размеров от минимума окна до 2560×1440,
    включая заведомо неудобные крайности вроде узкого высокого и широкого низкого.

    Порог в 2 пикселя намеренный: рамки и тени соседних виджетов законно делят
    общий пиксель, и без запаса тест ловил бы оформление, а не наложение."""

    # Не круглые числа ради красоты: 664×320 — минимум окна, 1920×340 и 664×1080 —
    # крайности, на которых раскладка ломалась раньше всего
    SIZES = ((664, 320), (700, 340), (760, 320), (800, 400), (900, 560),
             (1024, 640), (1100, 500), (1280, 720), (1280, 800), (1366, 768),
             (1440, 900), (1600, 900), (1920, 1080), (2560, 1440),
             (664, 1080), (1920, 340))
    TOLERANCE = 2

    @staticmethod
    def _in_scroll(widget) -> bool:
        """Внутри списка выезжать за рамку — нормально: на то он и прокрутка."""
        from PySide6.QtWidgets import QAbstractScrollArea
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                return True
            parent = parent.parentWidget()
        return False

    @staticmethod
    def _visible_kids(parent) -> list:
        from PySide6.QtWidgets import QWidget
        return [c for c in parent.children()
                if isinstance(c, QWidget) and c.isVisible() and not c.isWindow()
                and c.width() > 0 and c.height() > 0]

    def _defects(self) -> list:
        from PySide6.QtWidgets import QWidget, QStackedWidget
        found = []
        for parent in [self.window] + self.window.findChildren(QWidget):
            # Страницы стопки лежат друг на друге по устройству — это не дефект
            if not parent.isVisible() or isinstance(parent, QStackedWidget):
                continue
            kids = self._visible_kids(parent)
            for index, first in enumerate(kids):
                for second in kids[index + 1:]:
                    both = first.geometry().intersected(second.geometry())
                    if (both.width() > self.TOLERANCE
                            and both.height() > self.TOLERANCE):
                        found.append(
                            f'{_name(first)} наезжает на {_name(second)} '
                            f'на {both.width()}x{both.height()} в {_name(parent)}')
            for kid in kids:
                if self._in_scroll(kid):
                    continue
                box, room = kid.geometry(), parent.rect()
                if (box.left() < room.left() - 1 or box.top() < room.top() - 1
                        or box.right() > room.right() + 1
                        or box.bottom() > room.bottom() + 1):
                    found.append(f'{_name(kid)} {box.getRect()} вылез за '
                                 f'{_name(parent)} {room.getRect()}')
        return found

    def test_no_overlaps_on_any_page_at_any_size(self):
        pages = self.window._pages
        for index in range(pages.count()):
            pages.setCurrentIndex(index)
            page = _name(pages.widget(index))
            for width, height in self.SIZES:
                self.window.resize(width, height)
                self.settle(3)
                defects = self._defects()
                self.assertEqual(
                    defects, [],
                    f'{page} при {width}x{height}: ' + '; '.join(defects))


def _name(widget) -> str:
    return f'{type(widget).__name__}#{widget.objectName()}'.rstrip('#')


if __name__ == '__main__':
    unittest.main()
