"""Проверка, что разделы вообще собираются и не падают на пустых данных.

Окна рисуются вхолостую (QT_QPA_PLATFORM=offscreen), сеть не трогается: списки
наполняются готовыми объектами Track. Это дешёвая страховка от опечаток в
разметке, которые иначе всплывают только при запуске приложения.
"""
import os
import tempfile
import threading
import unittest

from PySide6.QtCore import Qt, QThreadPool

from app import config
from .qt_app import qt_app
from app.core.player_controller import PlayerController
from app.core.store import Store
from app.core.track import Track

MIN_WIDTH = 620          # окно должно жить и в такой ширине


def yt(video_id='abc', title='Numb', artist='Linkin Park') -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title=title, artist=artist, duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


def vk(audio_id=10) -> Track:
    return Track(source='vk', source_id=f'1_{audio_id}', title='Numb',
                 artist='Linkin Park', duration=186,
                 vk_owner_id=1, vk_audio_id=audio_id)


def row(audio_id, owner_id, artist, title, access_key='') -> dict:
    """Строка VK в том виде, в каком её отдаёт vk_client._row_to_track."""
    return {'id': audio_id, 'owner_id': owner_id, 'artist': artist, 'title': title,
            'duration': 186, 'url': None, 'access_key': access_key, 'cover': ''}


class FakeVkClient:
    """Клиент VK без сети: своя музыка, выдача поиска и учёт добавлений."""

    user_id = 1
    has_web_session = True

    def __init__(self, mixes=None):
        self.added: list[tuple] = []
        # None - «ключ не передавали»: по умолчанию у VK есть что предложить
        self._mixes = [{'id': 7, 'owner_id': -2, 'title': 'Волна дня',
                        'subtitle': 'Собрано VK', 'cover': '', 'access_hash': ''}
                       ] if mixes is None else mixes
        self.wave_calls = 0
        self.opened: list[dict] = []

    def get_my_tracks(self):
        return [row(10, 1, 'Linkin Park', 'Numb')]

    def wave_mixes(self, limit=40):
        self.wave_calls += 1
        return list(self._mixes)

    def get_playlist_tracks(self, playlist):
        self.opened.append(playlist)
        return [row(40, 7, 'Radiohead', 'Creep')]

    def get_playlists(self):
        return []

    def search_tracks(self, query, limit=60):
        return [row(10, 1, 'Linkin Park', 'Numb'),              # своя запись
                row(20, 5, 'linkin park', 'Numb (Official Video)'),  # чужая копия того же
                row(30, 9, 'Linkin Park', 'Faint', 'key')]      # ещё нет

    def add_audio(self, owner_id, audio_id, access_key=''):
        self.added.append((owner_id, audio_id))
        return {'id': audio_id, 'owner_id': self.user_id}


class FakeDiscovery:
    """Слой рекомендаций без сети: один плейлист и два трека в нём."""

    def playlists(self, limit=40):
        return [{'id': 'PLdemo', 'title': 'Мой список', 'count': 2, 'cover': ''}]

    def playlist_tracks(self, playlist, limit=200):
        from app.core.track import SOURCE_YOUTUBE, Track
        return [Track(source=SOURCE_YOUTUBE, source_id=f'vid{n}', title=f'Песня {n}',
                      artist='Кто-то', youtube_id=f'vid{n}') for n in (1, 2)]

    def search(self, query, limit=25, music_only=True):
        return []

    def home(self, per_section=20, sections=8):
        return []


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        os.unlink(self.db_path)
        self.store = Store(self.db_path)
        self.player = PlayerController(lambda: None, self.store)
        self.settings = dict(config.DEFAULT_SETTINGS)

    def tearDown(self):
        self.player.stop()
        self.store.close()
        self.app.processEvents()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def settle(self, rounds: int = 6) -> None:
        """Дождаться фоновых задач: подборки грузятся в пуле потоков."""
        for _round in range(rounds):
            QThreadPool.globalInstance().waitForDone(2000)
            self.app.processEvents()

    def show(self, widget):
        """Показать вхолостую и убедиться, что узкое окно не ломает разметку."""
        widget.resize(MIN_WIDTH, 700)
        widget.show()
        self.app.processEvents()
        # Окно должно сжиматься до 620 px - это и проверяем
        self.assertLessEqual(widget.minimumSizeHint().width(), MIN_WIDTH)
        widget.close()
        return widget

    # ---------- полоса плеера ----------
    def test_now_playing_bar(self):
        from app.ui.now_playing import NowPlayingBar
        bar = self.show(NowPlayingBar(self.player))
        # Пустое состояние
        self.assertTrue(bar.isEnabled())
        # И с треком: подписи обязаны пережить длинное название
        self.player.queue.set_tracks([yt(title='Очень длинное название трека ' * 6)])
        bar._on_track(self.player.queue.current())
        self.app.processEvents()
        bar._on_position(1000, 187000)
        bar._on_volume(50)
        bar.close()

    def test_mini_player_shows_current_track(self):
        """Мини-плеер - второй вид на тот же плеер, а его крестик только сообщает."""
        from app.ui.mini_player import MiniPlayer
        self.player.queue.set_tracks([yt(title='Numb')])
        mini = self.show(MiniPlayer(self.player))
        mini._on_track(self.player.queue.current())
        self.app.processEvents()
        self.assertEqual(mini._title.toolTip(), 'Numb')
        self.assertTrue(mini._play_btn.isEnabled())

        favorites: list = []
        mini.favorite_toggled.connect(favorites.append)
        mini._on_favorite()
        self.assertEqual(len(favorites), 1)

        closed: list = []
        mini.closed.connect(lambda: closed.append(True))
        mini.show()                       # окно уже закрывали при проверке ширины
        self.app.processEvents()
        mini.close()
        self.assertEqual(closed, [True])

    def test_tray_flyout_controls_player(self):
        """Панель у часов: показывает трек и управляет тем же плеером."""
        from app.ui.tray_flyout import TrayFlyout
        self.player.queue.set_tracks([yt(title='Numb')])
        flyout = TrayFlyout(self.player)
        flyout._on_track(self.player.queue.current())
        self.app.processEvents()
        self.assertEqual(flyout._title.toolTip(), 'Numb')
        self.assertTrue(flyout._play_btn.isEnabled())

        # Панель фиксированной ширины: она всплывает у значка, а не тянется
        flyout.popup_near_cursor()
        self.app.processEvents()
        self.assertTrue(flyout.isVisible())

        # Крестик - выход из программы, кнопка снизу - показать окно
        quits: list = []
        opens: list = []
        flyout.quit_requested.connect(lambda: quits.append(True))
        flyout.open_window_requested.connect(lambda: opens.append(True))
        flyout._on_quit()
        self.assertEqual(quits, [True])
        self.assertFalse(flyout.isVisible())
        flyout._on_open_window()
        self.assertEqual(opens, [True])
        flyout.close()

    def test_tray_flyout_offers_vk_track_to_your_music(self):
        """Запись VK кнопка «+» добавляет к себе: чужая она или своя, тут не видно.

        Раньше кнопка гасла у всего, что пришло из VK, и добавить к себе трек
        из поиска или подборки было нечем. Свои записи отмечает окно подписью
        через set_vk_state - оно одно спрашивает об этом сервис переноса."""
        from app.core.track import Track
        from app.ui.tray_flyout import TrayFlyout
        flyout = TrayFlyout(self.player)

        vk_track = Track(source='vk', source_id='-77_5', title='Numb',
                         artist='Linkin Park', duration=186,
                         vk_owner_id=-77, vk_audio_id=5)
        flyout._on_track(vk_track)
        self.assertTrue(flyout._vk_btn.isEnabled())

        # Своя запись: подпись приходит от окна, кнопка гаснет
        flyout.set_vk_state('✓ В VK')
        self.assertFalse(flyout._vk_btn.isEnabled())

        # Трек с YouTube, уже перенесённый: делать нечего и без подписи
        moved = yt(title='Numb')
        moved.in_vk = True
        flyout._on_track(moved)
        self.assertFalse(flyout._vk_btn.isEnabled())
        flyout.close()

    def test_artist_page_merges_local_and_found(self):
        """Страница исполнителя: сначала своё, потом найденное, без повторов."""
        from app.ui.artist_page import ArtistPage

        class FakeDiscovery:
            def search(self, query, limit=25, music_only=True):
                return [yt(title='Numb'),                 # уже есть в базе
                        yt('b', title='Faint')]           # новое

        self.store.save_tracks([yt(title='Numb'), yt('c', title='One Step Closer')])
        page = self.show(ArtistPage('Linkin Park', self.store, FakeDiscovery()))
        self.settle()
        titles = [track.title for track in page.list.tracks()]
        self.assertEqual(titles[:2], ['Numb', 'One Step Closer'])
        self.assertIn('Faint', titles)
        self.assertEqual(len(titles), 3)          # «Numb» не задвоился

        # То же окно умеет показать другого исполнителя
        radios: list = []
        page.radio_requested.connect(radios.append)
        page.set_artist('Deftones')
        self.settle()
        self.assertEqual(page.windowTitle(), 'Исполнитель: Deftones')
        page._radio()
        self.assertEqual(radios, ['Deftones'])
        page.close()

    # ---------- разделы ----------
    def test_youtube_page(self):
        from app.ui.youtube_page import YouTubePage
        page = self.show(YouTubePage(lambda: self.settings))
        page.list.set_tracks([yt(), yt('b', title='Faint')])
        self.app.processEvents()
        page.close()

    def test_home_page(self):
        from app.ui.home_page import HomePage
        self.store.log_play(yt())
        self.store.add_favorite(yt('b', title='Faint'))
        page = self.show(HomePage(self.player, self.store))
        page.reload()
        self.app.processEvents()
        page.close()

    def test_home_page_shows_sections(self):
        from app.ui.home_page import HomePage
        from app.core.youtube.discovery import (
            KIND_FALLBACK, KIND_RECOMMENDED, Section,
        )

        class FakeRecommender:
            def sections(self, per_section=16):
                return [Section('Для вас', [yt(), yt('b')], KIND_RECOMMENDED),
                        Section('Похожее', [yt('c')], KIND_FALLBACK)]

        page = self.show(HomePage(self.player, self.store, FakeRecommender()))
        page.reload()
        self.settle()
        titles = [label.text() for label, widget in page._slots if widget.tracks()]
        self.assertIn('Для вас', titles)
        # Поиск вместо рекомендации подписывается честно
        self.assertTrue(any(title.startswith('Похожее ·') for title in titles))
        page.close()

    def test_favorites_open_in_playlists(self):
        """Избранное - плейлист «Любимое», и открывается оно в «Плейлистах»."""
        from app.ui.playlists_page import PlaylistsPage
        self.store.add_favorite(yt())
        page = self.show(PlaylistsPage(lambda: None, self.store))
        page.show_favorites()
        self.app.processEvents()
        self.assertEqual(page._title.text(), 'Любимое')
        self.assertEqual(len(page.list.tracks()), 1)
        page.close()

    def test_playlists_page_without_vk(self):
        """Без входа в VK раздел обязан открыться и честно сказать об этом."""
        from app.ui.playlists_page import PlaylistsPage
        self.store.create_playlist('Вечер')
        page = self.show(PlaylistsPage(lambda: None, self.store))
        page.reload()
        self.app.processEvents()
        page.close()

    def test_playlists_page_shows_youtube(self):
        """Плейлисты YouTube попадают в общий список и открываются без скачивания."""
        from app.ui.playlists_page import DATA_ROLE, KIND_ROLE, PlaylistsPage
        page = self.show(PlaylistsPage(lambda: None, self.store, FakeDiscovery()))
        try:
            page.reload()
            self.settle()
            rows = [page._playlists.item(i) for i in range(page._playlists.count())]
            youtube = [i for i in rows if i.data(KIND_ROLE) == 'youtube']
            self.assertEqual(len(youtube), 1)
            page._playlists.setCurrentItem(youtube[0])
            self.settle()
            self.assertEqual(len(page.list.tracks()), 2)
            self.assertEqual(youtube[0].data(DATA_ROLE)['id'], 'PLdemo')
        finally:
            page.close()

    def test_playlists_filter_hides_other_sources(self):
        """Фильтр «VK» убирает и подборки Music Hub, и плейлисты YouTube."""
        from app.ui.playlists_page import KIND_ROLE, PlaylistsPage
        self.store.create_playlist('Вечер')
        page = self.show(PlaylistsPage(lambda: None, self.store, FakeDiscovery()))
        try:
            page.reload()
            self.settle()
            page._sources.setCurrentIndex(2)      # «VK»
            visible = [page._playlists.item(i).data(KIND_ROLE)
                       for i in range(page._playlists.count())
                       if not page._playlists.item(i).isHidden()]
            self.assertNotIn('local', visible)
            self.assertNotIn('youtube', visible)
            page._sources.setCurrentIndex(0)      # «Все»
            visible = [page._playlists.item(i).data(KIND_ROLE)
                       for i in range(page._playlists.count())
                       if not page._playlists.item(i).isHidden()]
            self.assertIn('local', visible)
            self.assertIn('youtube', visible)
        finally:
            page.close()

    def test_playlists_transfer_to_vk_asks_only_for_missing(self):
        """В VK уходит только то, чего там ещё нет."""
        from app.ui.playlists_page import PlaylistsPage
        page = self.show(PlaylistsPage(lambda: None, self.store, FakeDiscovery()))
        sent = []
        page.add_vk_requested.connect(lambda tracks: sent.append(list(tracks)))
        try:
            page.open_playlist_link('PLdemo')
            self.settle()
            page._on_to_vk()
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(sent[0]), 2)
        finally:
            page.close()

    def test_youtube_playlist_id_from_link(self):
        from app.core.youtube.discovery import playlist_id_from
        self.assertEqual(
            playlist_id_from('https://www.youtube.com/playlist?list=PLabc123'), 'PLabc123')
        self.assertEqual(playlist_id_from('VLPLabc123'), 'PLabc123')
        self.assertEqual(playlist_id_from('https://example.com/nothing'), '')

    def test_library_page(self):
        from app.ui.library_page import LibraryPage
        page = self.show(LibraryPage(lambda: self.settings))
        page.close()

    # ---------- моя музыка ----------
    def _tracks_page(self):
        from app.ui.tracks_page import TracksPage
        return TracksPage(self.store, lambda: self.settings)

    def test_tracks_page_is_empty_without_library(self):
        page = self.show(self._tracks_page())
        page.reload()
        self.assertEqual(len(page.list.tracks()), 0)
        # О пустой фонотеке говорит подсказка посреди страницы, а не строка-сводка
        self.assertFalse(page._empty.isHidden())
        self.assertIn('пусто', page._empty._head.text())
        page.close()

    def test_tracks_page_mixes_all_sources(self):
        """Фонотека показывает VK, YouTube и файлы одним списком."""
        path = os.path.join(tempfile.gettempdir(), 'своя.mp3')
        with open(path, 'wb') as fh:
            fh.write(b'0')
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        local = Track(source='local', source_id=os.path.normcase(path),
                      title='Faint', artist='Linkin Park', local_path=path)
        self.store.save_many_to_library([yt(), vk(), local])

        page = self.show(self._tracks_page())
        page.reload()
        self.assertEqual({t.source for t in page.list.tracks()},
                         {'youtube', 'vk', 'local'})
        self.assertIn('Треков: 3', page._summary.text())

        # Фильтр по источнику
        page._source.setCurrentIndex([page._source.itemData(i)
                                      for i in range(page._source.count())].index('vk'))
        self.assertEqual([t.source for t in page.list.tracks()], ['vk'])
        page._source.setCurrentIndex(0)

        # Поиск идёт и по названию, и по исполнителю
        page._search.setText('faint')
        self.assertEqual([t.title for t in page.list.tracks()], ['Faint'])
        page._search.clear()

        # «Только офлайн» оставляет то, что лежит на диске
        page._only_offline.setChecked(True)
        self.assertEqual([t.local_path for t in page.list.tracks()], [path])
        page.close()

    def test_local_preset_shows_only_own_files(self):
        """«С компьютера» - та же страница, но чужие источники в неё не попадают."""
        from app.ui.tracks_page import PRESET_LOCAL, TracksPage
        path = os.path.join(tempfile.gettempdir(), 'своя.mp3')
        with open(path, 'wb') as fh:
            fh.write(b'0')
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        local = Track(source='local', source_id=os.path.normcase(path),
                      title='Faint', artist='Linkin Park', local_path=path)
        self.store.save_many_to_library([yt(), vk(), local])

        page = self.show(TracksPage(self.store, lambda: self.settings,
                                    preset=PRESET_LOCAL))
        page.reload()
        self.assertEqual([t.title for t in page.list.tracks()], ['Faint'])
        # Выбирать источник здесь нечего: раздел и есть источник
        self.assertTrue(page._source.isHidden())
        page.close()

    def test_cache_preset_counts_lost_copies(self):
        """«Кэш» показывает офлайн-копии и не скрывает пропавшие файлы."""
        from app.ui.tracks_page import PRESET_CACHE, TracksPage
        path = os.path.join(tempfile.gettempdir(), 'копия.mp3')
        with open(path, 'wb') as fh:
            fh.write(b'0')
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        alive, lost = yt(), yt('def', title='Faint')
        self.store.save_many_to_library([alive, lost, vk()])
        self.store.set_local_path(alive.uid, path, cached=True)
        self.store.set_local_path(lost.uid, os.path.join(tempfile.gettempdir(),
                                                         'унесли.mp3'), cached=True)

        page = self.show(TracksPage(self.store, lambda: self.settings,
                                    preset=PRESET_CACHE))
        page.reload()
        # Сохранённый без офлайн-копии трек (vk) сюда не попадает
        self.assertEqual({t.title for t in page.list.tracks()}, {'Numb', 'Faint'})
        self.assertIn('Офлайн-копий: 2', page._summary.text())
        self.assertIn('файлов нет: 1', page._summary.text())
        # Запись есть, а играть нечего - об этом сказано меткой, а не молчанием
        gone = [t for t in page.list.tracks() if t.title == 'Faint'][0]
        self.assertEqual(page.list.badge(gone), 'файла нет')
        page.close()

    def test_tracks_page_marks_missing_file(self):
        """Файл унесли - трек остаётся в списке, но с честной меткой."""
        gone = os.path.join(tempfile.gettempdir(), 'нет такого файла.mp3')
        self.store.save_to_library(Track(source='local', source_id=os.path.normcase(gone),
                                         title='Numb', local_path=gone))
        page = self.show(self._tracks_page())
        page.reload()
        track = page.list.tracks()[0]
        self.assertEqual(page.list.badge(track), 'файла нет')
        # И в «только офлайн» такой трек не попадает
        page._only_offline.setChecked(True)
        self.assertEqual(page.list.tracks(), [])
        page.close()

    def test_vk_panel_without_client(self):
        from app.ui.vk_panel import VkPanel
        panel = self.show(VkPanel())
        panel.close()

    # ---------- поиск по всей музыке VK ----------
    def _vk_panel_with_client(self, client):
        from app.ui.vk_panel import TAB_SEARCH, VkPanel
        panel = VkPanel()
        panel.resize(MIN_WIDTH, 700)
        panel.show()
        panel.set_client(client)
        self.settle()
        panel._tab_bar.setCurrentIndex(TAB_SEARCH)
        return panel

    # ---------- волна VK ----------
    def test_vk_wave_asks_for_the_catalogue_on_first_visit(self):
        """Каталог волн не тянем при входе: идём за ним, только когда вкладку открыли."""
        from app.ui.vk_panel import TAB_WAVE
        client = FakeVkClient()
        panel = self._vk_panel_with_client(client)
        try:
            self.assertEqual(client.wave_calls, 0, 'запрос ушёл до открытия вкладки')
            panel._tab_bar.setCurrentIndex(TAB_WAVE)
            self.settle()
            self.assertEqual(client.wave_calls, 1)
            # Второй заход на вкладку - без нового запроса к VK
            panel._tab_bar.setCurrentIndex(0)
            panel._tab_bar.setCurrentIndex(TAB_WAVE)
            self.settle()
            self.assertEqual(client.wave_calls, 1)
        finally:
            panel.close()

    def test_vk_wave_offers_a_choice_of_mixes_not_one_list(self):
        """Смысл вкладки - выбор: каждая волна отдельной плиткой, треки после выбора."""
        from app.ui.vk_panel import TAB_WAVE
        client = FakeVkClient(mixes=[
            {'id': 7, 'owner_id': -2, 'title': 'Волна дня', 'cover': '', 'access_hash': ''},
            {'id': 8, 'owner_id': -3, 'title': 'Для сна', 'cover': '', 'access_hash': ''},
        ])
        panel = self._vk_panel_with_client(client)
        try:
            panel._tab_bar.setCurrentIndex(TAB_WAVE)
            self.settle()
            self.assertEqual(len(panel._wave_mixes), 2, 'каталог из двух волн не собрался')
            # До выбора треков нет: список волны - не главное на вкладке
            self.assertEqual(panel._wave_list.tracks(), [])
            self.assertIn('Выберите волну', panel._counter.text())

            panel._open_wave(panel._wave_mixes[1])
            self.settle()
            self.assertEqual(client.opened[-1]['title'], 'Для сна',
                             'открылась не та волна, по которой нажали')
            self.assertEqual([t.title for t in panel._wave_list.tracks()], ['Creep'])
            self.assertEqual(panel._wave_title.text(), 'Для сна')
        finally:
            panel.close()

    def test_vk_wave_shows_many_shelves_and_opens_a_section_one(self):
        """Подборок должно быть много и разного рода - ради этого вкладка и есть.

        Полка-раздел («Рекомендации VK») координат плейлиста не имеет, и открыть
        её можно только повторным запросом за разделом. Панель об этом знать не
        обязана: разбирается клиент, а с плитки всё выглядит одинаково."""
        from app.ui.vk_panel import TAB_WAVE

        class WithShelves(FakeVkClient):
            def wave_shelves(self, limit=40):
                return [
                    {'id': 'section:recoms', 'owner_id': 0, 'title': 'Рекомендации VK',
                     'cover': '', 'access_hash': '', '_section': 'recoms', '_items': None},
                    {'id': 'shelf:genre:Рэп', 'owner_id': 0, 'title': 'Рэп',
                     'cover': '', 'access_hash': '', '_section': '',
                     '_items': [{'id': 7, 'owner_id': -2}]},
                ]

            def shelf_tracks(self, shelf, limit=200):
                self.opened.append(shelf)
                return [row(60, 4, 'Massive Attack', 'Teardrop')]

        client = WithShelves()
        panel = self._vk_panel_with_client(client)
        try:
            panel._tab_bar.setCurrentIndex(TAB_WAVE)
            self.settle()
            self.assertEqual([m['title'] for m in panel._wave_mixes],
                             ['Рекомендации VK', 'Рэп'])
            self.assertTrue(panel._wave_area.isVisibleTo(panel))

            panel._open_wave(panel._wave_mixes[0])
            self.settle()
            # Открывали разделом, а не координатами плейлиста, которых у полки нет
            self.assertEqual(client.opened[-1]['_section'], 'recoms')
            self.assertEqual([t.title for t in panel._wave_list.tracks()], ['Teardrop'])
            self.assertEqual(panel._wave_title.text(), 'Рекомендации VK')
        finally:
            panel.close()

    def test_empty_vk_wave_catalogue_says_so_instead_of_showing_search(self):
        """Пустой каталог и рекомендаций нет - говорим это прямо, а не ищем подмену."""
        from app.ui.vk_panel import TAB_WAVE
        # У этого клиента нет даже recommended_tracks - запасному пути взяться неоткуда
        panel = self._vk_panel_with_client(FakeVkClient(mixes=[]))
        try:
            panel._tab_bar.setCurrentIndex(TAB_WAVE)
            self.settle()
            self.assertEqual(panel._wave_mixes, [])
            self.assertEqual(panel._wave_list.tracks(), [])
            self.assertIn('волн', panel._counter.text())
        finally:
            panel.close()

    def test_empty_wave_catalogue_falls_back_to_vk_recommendations(self):
        """Плиток VK не дал, но рекомендации дал - показываем их, а не пустоту.

        Это по-прежнему выдача самого VK, поэтому подменой поиска не является."""
        from app.ui.vk_panel import TAB_WAVE

        class WithRecoms(FakeVkClient):
            def recommended_tracks(self, limit=60):
                return [row(50, 3, 'Portishead', 'Roads')]

        panel = self._vk_panel_with_client(WithRecoms(mixes=[]))
        try:
            panel._tab_bar.setCurrentIndex(TAB_WAVE)
            self.settle()
            # Полосе плиток взяться неоткуда - она только занимала бы место
            self.assertFalse(panel._wave_area.isVisibleTo(panel))
            self.assertEqual([t.title for t in panel._wave_tracks], ['Roads'])
            self.assertEqual(panel._wave_title.text(), 'Рекомендации VK')
        finally:
            panel.close()

    def test_vk_search_marks_membership(self):
        from app.ui.vk_panel import MARK_ADDABLE, MARK_MINE
        panel = self._vk_panel_with_client(FakeVkClient())
        try:
            panel._search.setText('linkin')
            panel._start_vk_search(force=True)
            self.settle()
            marks = [panel._search_list.badge(track)
                     for track in panel._search_list.tracks()]
            # Своя запись - по номеру, чужая копия той же песни - по названию
            self.assertEqual(marks, [MARK_MINE, MARK_MINE, MARK_ADDABLE])
        finally:
            panel.close()

    def test_vk_search_ignores_stale_answer(self):
        released = threading.Event()

        class SlowClient(FakeVkClient):
            def search_tracks(self, query, limit=60):
                released.wait(5)
                return [row(3, 3, 'Кто-то', 'Поздний ответ')]

        panel = self._vk_panel_with_client(SlowClient())
        try:
            panel._search.setText('linkin')
            panel._start_vk_search(force=True)
            panel._search_gen += 1        # запрос уже сменился, ответ не нужен
            released.set()
            self.settle()
            self.assertEqual(panel._search_list.count(), 0)
        finally:
            panel.close()

    def test_vk_search_adds_only_missing(self):
        client = FakeVkClient()
        panel = self._vk_panel_with_client(client)
        try:
            panel._search.setText('linkin')
            panel._start_vk_search(force=True)
            self.settle()
            panel._search_list.selectAll()
            panel._add_checked_to_my_music()
            self.settle()
            # Добавляем только то, чего нет: две отмеченные записи уже свои
            self.assertEqual(client.added, [(9, 30)])
        finally:
            panel.close()

    def test_vk_match_key_ignores_case_and_brackets(self):
        from app.ui.vk_panel import match_key
        self.assertEqual(match_key('Linkin Park', 'Numb (Official Video)'),
                         match_key('linkin  park', 'NUMB'))
        self.assertNotEqual(match_key('Linkin Park', 'Numb'),
                            match_key('Linkin Park', 'Faint'))

    def test_track_list_shows_states(self):
        from app.ui.track_list import TrackListWidget
        widget = TrackListWidget()
        widget.set_tracks([yt(), vk()])
        widget.resize(MIN_WIDTH, 400)
        widget.show()
        self.app.processEvents()
        self.assertEqual(len(widget.tracks()), 2)
        widget.close()

    # ---------- настройки ----------
    def test_settings_dialog_with_old_settings(self):
        """Старый settings.json без новых ключей не должен ломать окно настроек."""
        from app.ui.settings_dialog import SettingsDialog
        old = {'mode': 'audio', 'audio_format': 'mp3', 'music_dir': 'D:/Music'}
        settings = {**config.DEFAULT_SETTINGS, **old}
        dialog = SettingsDialog(settings, vk_logged_in=False)
        dialog.show()
        self.app.processEvents()
        result = dialog.result_settings()
        for key in ('volume', 'autoplay_next', 'tray_enabled', 'hotkeys_enabled',
                    'hotkey_play_pause', 'bridge_enabled', 'bridge_port',
                    'vk_match_first'):
            self.assertIn(key, result, key)
        self.assertEqual(result['music_dir'], 'D:/Music')
        dialog.close()

    def test_settings_dialog_keeps_bridge_token(self):
        """Ключ моста только показывается - окно не должно его терять или менять."""
        from app.ui.settings_dialog import SettingsDialog
        settings = {**config.DEFAULT_SETTINGS, 'bridge_token': 'секретный-ключ'}
        dialog = SettingsDialog(settings, vk_logged_in=True)
        self.assertEqual(dialog.result_settings()['bridge_token'], 'секретный-ключ')
        dialog.close()

    def test_settings_dialog_writes_back_values(self):
        from app.ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(dict(config.DEFAULT_SETTINGS), vk_logged_in=False)
        dialog._volume.setValue(33)
        dialog._autoplay_next.setChecked(False)
        dialog._tray_enabled.setChecked(False)
        dialog._hotkey_edits['hotkey_next'].setText('Ctrl+Alt+N')
        result = dialog.result_settings()
        self.assertEqual(result['volume'], 33)
        self.assertFalse(result['autoplay_next'])
        self.assertFalse(result['tray_enabled'])
        self.assertEqual(result['hotkey_next'], 'Ctrl+Alt+N')
        dialog.close()

    def test_settings_dialog_writes_offline_and_local_dirs(self):
        from app.ui.settings_dialog import SettingsDialog
        settings = dict(config.DEFAULT_SETTINGS)
        settings['local_dirs'] = [tempfile.gettempdir()]
        dialog = SettingsDialog(settings, vk_logged_in=False)
        dialog._offline_dir.setText('D:/офлайн')
        dialog._offline_limit.setValue(2.5)
        dialog._offline_favorites.setChecked(False)
        result = dialog.result_settings()
        self.assertEqual(result['offline_dir'], 'D:/офлайн')
        self.assertEqual(result['offline_limit_gb'], 2.5)
        self.assertFalse(result['offline_favorites'])
        # Список папок со своей музыкой возвращается как есть
        self.assertEqual(result['local_dirs'], [tempfile.gettempdir()])
        dialog.close()

    # ---------- очередь ----------
    def test_queue_panel(self):
        from app.ui.queue_panel import QueuePanel
        self.player.queue.set_tracks([yt(), yt('b', title='Faint')])
        panel = QueuePanel(self.player)
        panel.show()
        self.app.processEvents()
        # Панель узкая по замыслу: проверяем её собственный минимум, не 620
        self.assertLessEqual(panel.minimumSizeHint().width(), 320)
        panel.close()

    def test_queue_panel_follows_player(self):
        from app.ui.queue_panel import QueuePanel
        panel = QueuePanel(self.player)
        panel.show()
        self.player.queue.set_tracks([yt(), yt('b', title='Faint')])
        self.player.queue_changed.emit()
        self.app.processEvents()
        self.player.clear_queue()
        self.app.processEvents()
        panel.close()

    # ---------- история ----------
    def test_history_page(self):
        from app.ui.history_page import HistoryPage
        self.store.log_play(yt(), listened=90, finished=True)
        self.store.log_play(vk(), listened=45)
        page = self.show(HistoryPage(self.store))
        page.reload()
        self.app.processEvents()
        self.assertTrue(any(widget.tracks() for widget in page.lists))

    def test_history_groups_by_day(self):
        from app.ui.history_page import GROUPS, group_for
        now = 1_700_000_000.0
        day = 24 * 3600
        self.assertEqual(group_for(now, now), GROUPS[0])
        self.assertEqual(group_for(now - day, now), GROUPS[1])
        self.assertEqual(group_for(now - 5 * day, now), GROUPS[2])
        # Битая отметка времени не должна ронять страницу
        self.assertEqual(group_for(0, now), GROUPS[2])

    def test_empty_history_page(self):
        from app.ui.history_page import HistoryPage
        page = self.show(HistoryPage(self.store))
        page.reload()
        self.app.processEvents()
        self.assertFalse(any(widget.tracks() for widget in page.lists))

    # ---------- поиск везде ----------
    def test_global_search_dialog(self):
        from app.ui.global_search import GlobalSearchDialog
        dialog = GlobalSearchDialog(None, lambda: None, self.store,
                                    lambda: self.settings)
        dialog.show()
        self.app.processEvents()
        # Пустой запрос никуда не ходит и ничего не показывает
        dialog.set_tab('youtube')
        self.app.processEvents()
        self.assertEqual(dialog.list.tracks(), [])
        dialog.close()

    def test_global_search_ignores_stale_answer(self):
        from app.ui.global_search import GlobalSearchDialog
        dialog = GlobalSearchDialog(None, lambda: None, self.store,
                                    lambda: self.settings)
        dialog._query = 'linkin'
        dialog._gen = 5
        # Ответ на запрос, который человек уже сменил
        dialog._results['youtube'] = [yt()]
        dialog._gen = 6
        dialog._results.clear()
        dialog._show()
        self.assertEqual(dialog.list.tracks(), [])
        dialog.close()

    # ---------- видео ----------
    def test_video_stage_keeps_aspect(self):
        from app.ui.video_stage import VideoStage
        stage = VideoStage()
        stage.resize(800, 100)
        stage.show()
        self.app.processEvents()
        # 16:9, а не полоска в сто точек
        self.assertGreater(stage.heightForWidth(800), 300)
        self.assertTrue(stage.hasHeightForWidth())
        stage.close()

    def test_video_stage_shrinks_instead_of_stretching(self):
        """Невысокое окно раньше растягивало кадр в полосу: высота упиралась
        в потолок, а ширина бралась от колонки."""
        from app.ui.video_stage import ASPECT_H, ASPECT_W, HEADER_HEIGHT, VideoStage
        stage = VideoStage()
        stage.show()
        self.app.processEvents()
        stage.set_height_limit(220)
        self.app.processEvents()
        hint = stage.sizeHint()
        body = hint.height() - HEADER_HEIGHT
        self.assertGreater(body, 0)
        # Кадр остаётся 16:9, просто становится меньше
        self.assertAlmostEqual(hint.width() / body, ASPECT_W / ASPECT_H, delta=0.15)
        stage.close()

    def test_video_stage_keeps_widget_on_fullscreen(self):
        from PySide6.QtWidgets import QLabel
        from app.ui.video_stage import VideoStage
        stage = VideoStage()
        inner = QLabel('видео')
        stage.set_widget(inner)
        stage.show()
        self.app.processEvents()
        stage.set_fullscreen(True)
        self.app.processEvents()
        # Виджет плеера один на приложение: он переезжает, а не пересоздаётся
        self.assertIs(stage.widget, inner)
        stage.set_fullscreen(False)
        self.app.processEvents()
        self.assertIs(stage.widget, inner)
        stage.close()


if __name__ == '__main__':
    unittest.main()
