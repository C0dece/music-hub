import logging
import os

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSizePolicy,
    QTableView, QTabBar, QTextEdit, QVBoxLayout, QWidget,
)

from .. import config
from ..core import history, js_runtime, library, proxy, url_detect, ytdlp_engine
from ..core import store as store_mod
from ..core import vk_client as vk_client_mod
from ..core import vk_session_keeper as keeper_mod
from ..core import bridge as bridge_mod
from ..core.async_task import run_async
from ..core.download_manager import DownloadManager
from ..core.hotkeys import MEDIA_KEYS, HotkeyManager
from ..core.mixer import MixConfig, Mixer
from ..core.offline import OfflineCache
from ..core.player_controller import (MODE_AUDIO, MODE_VIDEO, STATE_LOADING,
                                      STATE_RESOLVING, PlayerController)
from ..core.recommendations import Recommender
from ..core.track import (SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE, from_local,
                          from_vk, from_youtube, to_vk_row)
from ..core.vk_import import STATE_LABELS, VkImportService
from ..core.vk_session_keeper import VkSessionKeeper
from ..core.vk_uploader import VkUploadQueue
from ..core.vk_web_login import VkWebLoginDialog, clear_saved_login
from ..core.youtube import Discovery
from . import covers, player_icons
from .bulk_add_dialog import BulkAddDialog
from .flow_layout import FlowRow
from .global_search import GlobalSearchDialog
from .history_page import HistoryPage
from .home_page import HomePage
from .icon import app_icon
from .artist_page import ArtistPage
from .library_page import LibraryPage
from .mini_player import MiniPlayer
from .mix_page import MixPage
from .now_playing import MIN_BAR_WIDTH, NowPlayingBar
from .playlist_pick_dialog import PlaylistPickDialog
from .playlists_page import PlaylistsPage
from .queue_panel import QueuePanel
from .queue_table_model import COL_PROGRESS, COL_SOURCE, COL_STATUS, COL_TITLE, QueueTableModel
from .settings_dialog import TAB_ACCOUNTS, SettingsDialog
from .track_actions import NEW_PLAYLIST
from .tracks_page import PRESET_LOCAL, TracksPage
from .title_bar import TitleBar
from .tray import TrayIcon
from .video_stage import VideoStage
from .vk_panel import VkPanel, track_key
from .youtube_backend import YouTubeWebBackend
from .youtube_page import YouTubePage
from .web_preview import can_preview, open_preview, preload_cookies as preload_preview_cookies
from .widgets import (
    Card, ElidedLabel, EmptyState, PagesStack, ProgressDelegate, StatusChip,
)

logger = logging.getLogger(__name__)

VIDEO_QUALITIES = [
    ('Лучшее', 'best'), ('2160p', '2160'), ('1440p', '1440'),
    ('1080p', '1080'), ('720p', '720'), ('480p', '480'),
]
AUDIO_FORMATS = [('MP3', 'mp3'), ('M4A', 'm4a'), ('Opus', 'opus'), ('FLAC', 'flac')]
AUDIO_BITRATES = [
    ('128 кбит/с', '128'), ('192 кбит/с', '192'), ('256 кбит/с', '256'),
    ('320 кбит/с', '320'), ('Лучшее', 'best'),
]

(PAGE_HOME, PAGE_MIX, PAGE_YOUTUBE, PAGE_VK, PAGE_PLAYLISTS, PAGE_HISTORY,
 PAGE_QUEUE, PAGE_LIBRARY, PAGE_TRACKS, PAGE_LOCAL, PAGE_VIDEO) = range(11)
PAGE_TITLES = ['Главная', 'Микс', 'YouTube', 'Музыка VK', 'Плейлисты', 'История',
               'Загрузки', 'Библиотека', 'Треки', 'С компьютера', 'Видео']
# Короткие имена для переходов из других разделов и с главной
PAGE_NAMES = ['home', 'mix', 'youtube', 'vk', 'playlists', 'history',
              'downloads', 'library', 'tracks', 'local', 'video']

# Четыре раздела вместо девяти пунктов подряд: сначала выбирают занятие, и
# только внутри — источник. Избранного здесь нет: это плейлист «Любимое».
NAV_SECTIONS = [
    ('Главная', (PAGE_HOME,)),
    ('Слушать', (PAGE_MIX, PAGE_YOUTUBE, PAGE_VK, PAGE_LOCAL)),
    # «Кэша» здесь нет: офлайн-копия это признак трека, а не отдельная фонотека.
    # Раздел показывал те же строки второй раз, а искать в нём приходилось
    # отдельно. Теперь это фильтр «Только офлайн» в «Треках»
    ('Моя музыка', (PAGE_TRACKS, PAGE_PLAYLISTS, PAGE_HISTORY)),
    # «Библиотека» — про скачанные файлы, а не про фонотеку: её место рядом с
    # очередью загрузок, иначе «Моя музыка» показывала бы одно и то же дважды
    ('Загрузки', (PAGE_QUEUE, PAGE_LIBRARY)),
    # Видео живёт отдельным разделом и появляется в меню, только когда есть что
    # показывать. Раньше кадр стоял внутри чужих страниц: он налезал на их
    # содержимое, а в широком окне делил строку со списками и очередью — три
    # колонки сразу, и ни одной удобной. Своя страница снимает вопрос
    ('Видео', (PAGE_VIDEO,)),
]


# Значки разделов в том же порядке, что и NAV_SECTIONS
NAV_ICONS = ('home', 'headphones', 'audio', 'download', 'video')


def section_of_page(page: int) -> int:
    """В каком разделе меню живёт страница."""
    return next(row for row, (_title, pages) in enumerate(NAV_SECTIONS)
                if page in pages)

# Ширина окна, ниже которой очередь сбоку прячется сама: панель занимает 320 px,
# и в узком окне от списка треков остаётся полоска
QUEUE_PANEL_MIN_WIDTH = 1040
# С этой ширины в разделе видео очередь встаёт рядом с кадром. Порог не в том,
# где она физически влезает (780), а в том, где не портит кадр: на 820 очередь
# срезала его с 604 до 266 px — ступенька заметнее пользы от списка. Ниже порога
# кадр занимает страницу целиком, а очередь открывают из полосы плеера
VIDEO_QUEUE_MIN_WIDTH = 1040
# Узкий боковой список разделов — окно должно жить и на 620 px
NAV_WIDTH, NAV_WIDTH_NARROW = 190, 64
NARROW_WIDTH = 820
# Раздел «Видео» — последний в меню
VIDEO_SECTION = len(NAV_SECTIONS) - 1
# Ширина поля поиска — одна на все размеры окна: в узком окне группа с поиском
# переезжает на свою строку целиком, и места ей там хватает
SEARCH_WIDTH = 230

OPTS_AUDIO, OPTS_VIDEO = 0, 1

# Пауза до самостоятельной попытки войти в VK после обрыва связи. Растёт вдвое:
# короткий провал сети чинится первой же попыткой, а когда интернета нет вовсе,
# дёргать VK каждые пятнадцать секунд бессмысленно.
VK_RETRY_FIRST = 15
VK_RETRY_MAX = 300

# Отдельная лестница пауз для тихого перезахода веб-сессии. Раньше она была общей с
# обычным повтором входа, и это едва не стоило человеку аккаунта: окно дёргало keeper
# каждые пять минут, сбрасывая ему счётчик неудач, а сторож сессии независимо ходил в
# VK за треками каждые десять — около пятнадцати неудачных заходов в час, и VK
# отвечал на это блокировкой. Теперь первая пауза не короче собственной паузы keeper'а,
# а предельная равна его COOLDOWN: пока сессия не оживает, попыток остаётся не больше
# двух в час, и обе — руками самого keeper'а, с его антиштормом
VK_SESSION_RETRY_FIRST = int(keeper_mod.MIN_INTERVAL)      # 300 с
VK_SESSION_RETRY_MAX = int(keeper_mod.COOLDOWN)            # 1800 с

# Как часто сторож молча проверяет, жива ли ещё веб-сессия VK. Раньше о смерти
# сессии узнавали только из упавшего запроса — то есть в тот момент, когда человек
# уже нажал и уже увидел сбой. Проверка — один лёгкий запрос в фоне, так что дело не
# в нагрузке, а в окне беззащитности: сессия, слетевшая сразу после проверки, будет
# чиниться только к следующей. Десять минут — компромисс: за это время человек редко
# успевает дойти до VK, а часовой интервал такую дыру оставлял почти на весь день
VK_SESSION_WATCH = 600


def _short_url(url: str, limit: int = 70) -> str:
    """Ссылка для показа в отчёте: длинный хвост с параметрами только мешает читать."""
    url = url.strip()
    return url if len(url) <= limit else f'{url[:limit - 1]}…'


# Полоса вдоль краёв окна, за которую его тянут: своей рамки у окна нет
# Полоса захвата у края окна. Шесть пикселей это ширина системной рамки, но
# своя рамка не помогает курсору «прилипать», как это делает Windows у обычных
# окон, поэтому целиться в неё приходилось. Восемь снаружи и столько же внутри
RESIZE_MARGIN = 8


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(config.APP_NAME)
        self.setWindowIcon(app_icon())
        # Своя рамка: системная полоса заголовка светлая и не подчиняется теме,
        # а нужное от неё (имя, кнопки, перетаскивание) есть в TitleBar
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMouseTracking(True)
        self.resize(1080, 700)
        # Раньше стояло 900x560, хотя раскладка требовала 1700: строки кнопок просто
        # обрезались. Теперь они переносятся, и окно честно живёт от этого размера.
        # Нижняя граница по ширине посчитана, а не выбрана: полосе плеера в самом
        # сжатом наборе нужно NowPlayingBar.MIN_BAR_WIDTH (600), и слева от неё
        # всегда стоит свёрнутое меню шириной NAV_WIDTH_NARROW. Прежние 620 были
        # меньше этой суммы: полоса не помещалась и лезла за край окна.
        # Высота не выбрана, а измерена: minimumSizeHint() центрального виджета.
        # Складывается из полосы заголовка (36), средней полосы (371: панель
        # очереди с карточкой и списком) и полосы плеера (73).
        # Прежние 320 были заметно меньше настоящего требования раскладки, и это
        # не давало окну «просто быть поменьше»: разницу QBoxLayout разбирал,
        # сжимая детей ниже их собственных минимумов. Сильнее всего доставалось
        # панели VK — вкладки схлопывались в полоску в 6 px, а строка поиска
        # налезала на них сверху. Занижать минимум ради маленького окна нельзя:
        # раскладка от этого не уменьшается, а ломается.
        # Само требование раскладки перед этим опущено с 664 до 480 — обложка в
        # очереди и кадр 16:9 больше не диктуют минимум (queue_panel, video_stage)
        self.setMinimumSize(MIN_BAR_WIDTH + NAV_WIDTH_NARROW, 480)

        self._title_bar = None
        # resizeEvent приходит и до сборки интерфейса: пусть будет чему отвечать
        self._search = None
        self._settings = config.load_settings()
        self._vk_client = None
        # Вход сохранён, но подключиться не вышло: чип наверху должен говорить то же,
        # что и панель VK, иначе «нет связи» рядом с «вход не выполнен» сбивает с толку
        self._vk_offline = False
        # Подключение к VK уже идёт: защита от второго клиента (см. _connect_vk_client)
        self._vk_connecting = False
        # Обрыв связи чиним сами: таймер повторяет вход сохранённым токеном, пока
        # не получится. Иначе человек, отошедший от компьютера, возвращался к
        # панели «нет связи», хотя интернет давно вернулся.
        self._vk_retry = QTimer(self)
        self._vk_retry.setSingleShot(True)
        self._vk_retry.timeout.connect(self._try_auto_vk_login)
        self._vk_retry_delay = 0
        # Отметка о блокировке лежит на диске и переживает закрытие программы, но
        # доверять ей на слово нельзя: блокировку снимают на сайте, а мы об этом не
        # узнаем, пока не спросим. Поэтому один раз за запуск проверяем по-настоящему,
        # и только потом отметка начинает работать как глушилка повторов
        self._vk_blocked_rechecked = False
        # Протухшую сессию сайта сперва пробуем вернуть молча: в профиле встроенного
        # браузера обычно ещё жив вход, и пароль спрашивать незачем. Это не дубль
        # _vk_retry — тот про связь и токен, а этот про куки сайта
        # id отдаём функцией, а не числом: аккаунт может смениться, и запомненный
        # при старте указывал бы на прежнего владельца
        self._vk_keeper = VkSessionKeeper(parent=self, user_id=self._vk_user_id)
        self._vk_keeper.restored.connect(self._on_vk_session_restored)
        self._vk_keeper.failed.connect(self._on_vk_session_lost)
        self._vk_keeper.blocked.connect(self._on_vk_account_blocked)
        # Keeper сам держит паузу между попытками и после трёх неудач подряд молчит
        # совсем. Молчание разумно для него, но не для окна: без этого таймера панель
        # так и осталась бы с «обновляю вход», хотя сеть давно вернулась
        self._vk_session_retry = QTimer(self)
        self._vk_session_retry.setSingleShot(True)
        self._vk_session_retry.timeout.connect(self._retry_vk_session)
        self._vk_session_delay = 0
        # Сторож: пока человек занят своими делами, регулярно и молча проверяем, жива
        # ли сессия сайта, и чиним её заранее. Без него о смерти сессии узнавали только
        # из упавшего запроса — то есть уже после того, как человек увидел сбой
        self._vk_session_watch = QTimer(self)
        self._vk_session_watch.setInterval(VK_SESSION_WATCH * 1000)
        self._vk_session_watch.timeout.connect(self._check_vk_session)
        # Библиотеку перечитываем не после каждого файла, а при переходе на вкладку:
        # обход папок во время активных загрузок только мешает
        self._library_dirty = True

        # Заливка в музыку VK: общая очередь для библиотеки и для режима
        # «скачал с YouTube — сразу в VK»
        self._uploader = VkUploadQueue(self)
        self._uploader.changed.connect(self._refresh_upload_chip)
        self._uploader.progress.connect(self._on_upload_progress)
        self._uploader.uploaded.connect(self._on_uploaded)
        self._upload_failures: list[str] = []
        self._upload_done = 0
        # Загрузки, которые после скачивания должны уехать в VK
        self._auto_upload_ids: set[str] = set()
        # Разбор списка ссылок: ходим по ним по одной, поэтому состояние живёт здесь
        self._batch: dict | None = None

        # Музыкальный центр: база, плеер и перенос «+ VK» создаются до интерфейса —
        # главная страница и панель плеера получают их в конструкторе
        self._store = store_mod.store()
        self._manager = DownloadManager(settings_provider=lambda: self._settings)
        self._manager.set_concurrency(self._settings.get('concurrency', 3))
        # Офлайн-копии «Моей музыки». Своего загрузчика у них нет — тот же
        # менеджер, только с другой папкой (см. core/offline.py)
        self._offline = OfflineCache(self._manager, self._store,
                                     lambda: self._settings, self)
        # Копия скачивается в фоне, и «Кэш» узнаёт о ней только отсюда: без этого
        # трек лежал бы на диске, а страница оставалась бы пустой до другой правки
        self._offline.changed.connect(lambda _uid: self._mark_tracks_dirty())
        self._player = PlayerController(lambda: self._vk_client, self._store, self)
        self._player.apply_settings(self._settings)
        self._player.error.connect(self._on_player_error)
        # Рекомендации: лента YouTube Music, радио по треку и продолжение очереди.
        # Куки берём те же, что и загрузчик, — отдельный вход не нужен
        self._discovery = Discovery(lambda: self._settings.get('cookies_browser'))
        self._recommender = Recommender(self._discovery, self._store)
        self._player.set_recommender(self._autoplay_next)
        # Единая волна из VK, YouTube и своих файлов. Своего загрузчика у неё
        # нет — она просит те же части, что и обычные разделы
        self._mixer = Mixer(self._store, self._recommender, lambda: self._vk_client,
                            self._local_media)
        # Пока играет микс, продолжать очередь должен тоже микс
        self._mix_follow = None
        self._import = VkImportService(lambda: self._vk_client, self._uploader,
                                       self._manager, self._store,
                                       lambda: self._settings, self)
        self._import.state_changed.connect(self._on_import_state)
        self._import.finished.connect(self._on_import_finished)
        self._import.ambiguous.connect(self._on_import_ambiguous)
        # uid → подпись кнопки «+ VK»: новые списки получают её при создании
        self._vk_states: dict[str, str] = {}
        self._track_lists: list = []
        # Очередь сбоку: человек может её убрать, а в узком окне она прячется сама
        self._queue_wanted = bool(self._settings.get('queue_panel', True))
        # Свёрнутое меню запоминается: разворачивать его каждый запуск заново
        # было бы ровно тем неудобством, ради которого кнопку и добавляли
        self._nav_collapsed = bool(self._settings.get('nav_collapsed', False))
        self._search_dialog: GlobalSearchDialog | None = None
        # Страницы читают базу при показе, а не при каждом изменении
        # Здесь должна быть каждая страница, читающая базу при показе: забытая
        # покажет при первом открытии пустую рамку без единого слова — ни списка,
        # ни подсказки, — пока её случайно не пометит правка фонотеки
        self._page_dirty = {PAGE_HOME: True, PAGE_TRACKS: True,
                            PAGE_LOCAL: True,
                            PAGE_PLAYLISTS: True, PAGE_HISTORY: True}
        # Счётчики у названий разделов и последняя открытая страница каждого
        self._nav_counts: dict[int, int] = {}
        self._last_page: dict[int, int] = {}

        # Значок у часов, глобальные клавиши и мост для расширения. Всё
        # необязательное: если система не даёт, приложение работает как прежде
        self._tray: TrayIcon | None = None
        self._mini: MiniPlayer | None = None
        self._artist_view: ArtistPage | None = None
        self._quitting = False
        self._hotkeys = HotkeyManager(self)
        self._hotkeys.triggered.connect(self._on_hotkey)
        self._bridge = bridge_mod.BridgeServer(self)
        self._bridge.command.connect(self._on_bridge_command)
        self._player.track_changed.connect(self._push_bridge_status)
        self._player.state_changed.connect(self._push_bridge_status)

        self._build_ui()
        self._apply_settings_to_controls()

        self._manager.item_added.connect(self._on_item_added)
        self._manager.item_progress.connect(self._model.update_progress)
        self._manager.item_status.connect(self._on_item_status)
        self._manager.item_finished.connect(self._on_item_finished)

        self._setup_tray()
        self._setup_hotkeys()
        self._setup_bridge()

        self._player.restore_state()
        self._detect_proxy()
        # Кэш обложек подрезаем один раз при запуске и в фоне: работа с диском
        run_async(covers.trim_disk_cache, lambda _count, _error: None)
        self._try_auto_vk_login()
        preload_preview_cookies(self._settings.get('cookies_browser'))
        self._install_edge_filter()

    # ================= сборка интерфейса =================
    def _build_ui(self) -> None:
        # Каркас: слева меню во всю высоту, справа полоса заголовка над
        # содержимым, внизу плеер во всю ширину. Отступы задают внутренние
        # части — так меню и очередь достают до края окна, а не висят в рамке.
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._title_bar = TitleBar()
        self._title_bar.minimize_requested.connect(self.showMinimized)
        self._title_bar.maximize_requested.connect(self._toggle_maximized)
        self._title_bar.close_requested.connect(self.close)
        root.addWidget(self._title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(0)
        right_box.addWidget(self._build_topbar())

        work = QHBoxLayout()
        work.setContentsMargins(0, 0, 0, 0)
        work.setSpacing(0)

        # Середина окна: сверху видео, под ним разделы. Сцена видео одна на всё
        # приложение — при переходе между разделами ролик не перезапускается
        center = QWidget()
        center_box = QVBoxLayout(center)
        center_box.setContentsMargins(16, 14, 16, 12)
        center_box.setSpacing(12)
        self._video_stage = VideoStage()
        self._video_size_watched = False
        # Песни, для которых клип уже искали. Discovery помнит и сам, но здесь
        # запрос ещё и не уходит повторно, пока предыдущий в пути
        self._clip_tried: set[str] = set()
        self._video_stage.fullscreen_changed.connect(self._on_fullscreen_changed)
        # «Свернуть» в шапке сцены — это просьба слушать дальше без картинки
        self._video_stage.close_requested.connect(lambda: self._player.set_mode(MODE_AUDIO))

        # Порядок добавления страниц обязан совпадать с PAGE_* и PAGE_TITLES
        # Стопка меряется по открытой странице, а не по самой большой из всех:
        # иначе раздел видео и библиотека не давали сузить окно нигде
        self._pages = PagesStack()

        self._home_page = HomePage(self._player, self._store, self._recommender)
        self._home_page.mix_requested.connect(self._start_mix_from_home)
        for widget in self._home_page.lists:
            self._wire_track_list(widget)
        self._pages.addWidget(self._home_page)

        self._mix_page = MixPage(self._mixer, MixConfig.from_dict(self._settings.get('mix')))
        self._mix_page.config_changed.connect(self._on_mix_config)
        self._mix_page.status_message.connect(self._status_message)
        self._mix_page.enqueue_requested.connect(self._enqueue_tracks)
        self._mix_page.play_requested.connect(self._play_mix)
        self._wire_track_list(self._mix_page.list, on_play=self._play_mix)
        self._pages.addWidget(self._mix_page)

        self._youtube_page = YouTubePage(lambda: self._settings, self._discovery,
                                         self._recommender)
        for widget in self._youtube_page.lists:
            self._wire_track_list(widget)
        # Плеер YouTube — это страница Chromium, и ей нужно место в окне
        self._yt_backend = YouTubeWebBackend(self)
        self._player.add_backend(self._yt_backend)
        self._video_stage.set_widget(self._yt_backend.view)
        self._pages.addWidget(self._youtube_page)

        self._vk_panel = VkPanel()
        self._vk_panel.login_requested.connect(self._open_vk_login)
        self._vk_panel.download_tracks_requested.connect(self._queue_vk_tracks)
        self._vk_panel.reconnect_requested.connect(self._try_auto_vk_login)
        self._vk_panel.unblock_requested.connect(self._retry_after_unblock)
        self._vk_panel.session_expired.connect(self._refresh_status_chips)
        self._vk_panel.session_expired.connect(self._restore_vk_session)
        self._vk_panel.play_tracks_requested.connect(self._play_vk_rows)
        self._vk_panel.enqueue_tracks_requested.connect(self._enqueue_vk_rows)
        self._pages.addWidget(self._vk_panel)

        self._playlists_page = PlaylistsPage(lambda: self._vk_client, self._store,
                                            self._discovery)
        self._playlists_page.add_vk_requested.connect(self._import_tracks_to_vk)
        self._playlists_page.status_message.connect(self._status_message)
        self._wire_track_list(self._playlists_page.list)
        self._pages.addWidget(self._playlists_page)

        self._history_page = HistoryPage(self._store)
        for widget in self._history_page.lists:
            self._wire_track_list(widget)

        self._library_page = LibraryPage(lambda: self._settings)
        self._library_page.count_changed.connect(self._on_library_count)
        self._library_page.set_uploader(self._uploader)
        self._library_page.play_files_requested.connect(self._play_local_files)
        self._library_page.enqueue_files_requested.connect(self._enqueue_local_files)

        self._tracks_page = TracksPage(self._store, lambda: self._settings,
                                       self._offline)
        self._tracks_page.status_message.connect(self._status_message)
        self._tracks_page.local_dirs_changed.connect(self._on_local_dirs_changed)
        self._wire_track_list(self._tracks_page.list)

        # «С компьютера» — та же страница в другом режиме: список берётся из
        # другой выборки базы, а поиск, меню строки и щелчок остаются общими
        self._local_page = TracksPage(self._store, lambda: self._settings,
                                      self._offline, preset=PRESET_LOCAL)
        self._local_page.status_message.connect(self._status_message)
        self._local_page.local_dirs_changed.connect(self._on_local_dirs_changed)
        self._wire_track_list(self._local_page.list)

        self._pages.addWidget(self._history_page)
        self._pages.addWidget(self._build_queue_page())
        self._pages.addWidget(self._library_page)
        self._pages.addWidget(self._tracks_page)
        self._pages.addWidget(self._local_page)
        self._pages.addWidget(self._build_video_page())

        center_box.addWidget(self._pages, 1)
        work.addWidget(center, 1)

        # Очередь сбоку: видно, что играет и что дальше, без второго окна
        self._queue_panel = QueuePanel(self._player)
        self._queue_panel.add_vk_requested.connect(self._import_tracks_to_vk)
        self._queue_panel.favorite_requested.connect(self._toggle_favorites)
        self._queue_panel.radio_requested.connect(self._start_radio)
        self._queue_panel.open_source_requested.connect(self._open_track_source)
        self._queue_panel.hide_requested.connect(self._hide_tracks)
        self._queue_panel.hide_artist_requested.connect(self._hide_artist)
        self._queue_panel.artist_requested.connect(self._show_artist)
        self._queue_panel.playlist_requested.connect(self._add_tracks_to_playlist)
        self._queue_panel.set_store(self._store)
        self._queue_panel.close_requested.connect(self._hide_queue_panel)
        # Очередь опустела или наполнилась — панель появляется и исчезает сама
        self._player.queue_changed.connect(self._apply_responsive)
        # Куда вернуть панель очереди, когда уходят из раздела видео
        self._queue_home = work
        self._queue_in_video = False
        work.addWidget(self._queue_panel)
        right_box.addLayout(work, 1)
        body.addWidget(right, 1)
        root.addLayout(body, 1)

        # Панель плеера видна из любого раздела
        self._now_playing = NowPlayingBar(self._player)
        self._now_playing.add_to_vk_requested.connect(
            lambda track: self._import_tracks_to_vk([track]))
        self._now_playing.favorite_toggled.connect(
            lambda track: self._toggle_favorites([track]))
        self._now_playing.queue_requested.connect(self._toggle_queue_panel)
        self._now_playing.open_source_requested.connect(self._open_track_source)
        self._now_playing.radio_requested.connect(self._start_radio)
        self._now_playing.download_requested.connect(
            lambda track: self._download_tracks([track]))
        self._now_playing.artist_requested.connect(self._show_artist)
        self._now_playing.hide_requested.connect(self._dislike_playing)
        self._now_playing.playlist_requested.connect(self._add_tracks_to_playlist)
        self._now_playing.set_store(self._store)
        self._now_playing.fullscreen_requested.connect(self._toggle_video_fullscreen)
        self._now_playing.expand_requested.connect(self._expand_player)
        self._now_playing.mini_player_requested.connect(self._toggle_mini_player)
        root.addWidget(self._now_playing)
        self._player.track_changed.connect(self._on_player_track)
        self._player.mode_changed.connect(lambda _mode: self._update_video())
        self._player.state_changed.connect(self._on_video_state)

        self.setCentralWidget(central)
        self._on_mode_changed()
        self._nav.setCurrentRow(0)
        self._update_queue_summary()
        self._update_queue_buttons()
        self._apply_responsive()

        # Ctrl+K — поиск везде. Через QShortcut, а не глобальным перехватом:
        # сочетание должно работать только пока окно активно
        QShortcut(QKeySequence('Ctrl+K'), self, activated=self._open_global_search)

    def _build_sidebar(self) -> QWidget:
        """Меню во всю высоту окна: марка, разделы, снизу настройки.

        Раньше шапка занимала верхнюю полосу окна целиком ради логотипа и трёх
        индикаторов. Сбоку то же самое ничего не отнимает у содержимого: там
        всё равно стоит список разделов."""
        bar = QFrame()
        bar.setObjectName('sidebar')
        box = QVBoxLayout(bar)
        box.setContentsMargins(0, 14, 0, 10)
        box.setSpacing(8)

        # Логотипа с названием здесь больше нет: то и другое стоит в полосе
        # заголовка над окном. На его месте кнопка, которой меню сворачивают
        # руками, не дожидаясь, пока окно станет узким
        head = QWidget()
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(10, 0, 10, 6)
        head_row.setSpacing(6)
        self._nav_toggle = QPushButton()
        self._nav_toggle.setObjectName('iconBtn')
        self._nav_toggle.setIconSize(QSize(18, 18))
        self._nav_toggle.setFixedSize(34, 34)
        self._nav_toggle.setCursor(Qt.PointingHandCursor)
        self._nav_toggle.clicked.connect(self._toggle_nav)
        head_row.addWidget(self._nav_toggle)
        head_row.addStretch(1)
        box.addWidget(head)

        box.addWidget(self._build_nav(), 1)

        footer = QWidget()
        footer_box = QVBoxLayout(footer)
        footer_box.setContentsMargins(8, 6, 8, 0)
        footer_box.setSpacing(6)
        self._settings_btn = QPushButton(' Настройки')
        self._settings_btn.setObjectName('secondary')
        self._settings_btn.setIcon(player_icons.draw('settings', player_icons.COLOR_MUTED))
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.clicked.connect(lambda: self._open_settings())
        footer_box.addWidget(self._settings_btn)
        box.addWidget(footer)
        self._sidebar_footer = footer

        self._sidebar = bar
        return bar

    def _build_topbar(self) -> QWidget:
        """Полоса над содержимым: где я, куда переключиться, что найти.

        Левая половина меняется вместе со страницей, правая нет. Раньше всё
        стояло в одной строке с переносом, и поиск сдвигался каждый раз, когда
        менялась ширина вкладок. Теперь правая часть отдельная и держит своё
        место: поле поиска всегда на одном и том же расстоянии от края."""
        bar = QFrame()
        bar.setObjectName('topbar')
        box = QHBoxLayout(bar)
        box.setContentsMargins(16, 10, 16, 10)
        box.setSpacing(10)

        row = self._header_row = FlowRow(spacing=10)
        row.setObjectName('header')
        self._page_title = QLabel(PAGE_TITLES[PAGE_HOME])
        self._page_title.setObjectName('pageTitle')
        row.add(self._page_title)
        row.add(self._section_tabs)
        row.add_stretch()
        box.addWidget(row, 1)

        # Поиск и кружки состояния — одна неразрывная группа. Отдельным соседом
        # в QHBoxLayout она откусывала у полосы заголовка свои 302 px намертво, и
        # вкладкам разделов оставалось 336 из нужных 387: QTabBar лишнее не
        # сжимает и не переносит, он рисовал вкладки прежней ширины и обрезал
        # последнюю по краю виджета — «С компьютера» превращалось в «С компьк».
        # Здесь группа стоит в самой полосе, за растяжкой: в широком окне она
        # по-прежнему прижата к правому краю, а когда места не хватает, FlowRow
        # переносит её на свою строку целиком — и вкладки получают всю ширину
        tools = QWidget()
        tools.setObjectName('header')
        tools_row = QHBoxLayout(tools)
        tools_row.setContentsMargins(0, 0, 0, 0)
        tools_row.setSpacing(8)

        # Поиск на виду, а не только по Ctrl+K: сочетание знают не все, а
        # искать везде нужно чаще всего, сразу после «включить»
        self._search = QLineEdit()
        self._search.setObjectName('search')
        self._search.setPlaceholderText('Поиск везде, Ctrl+K')
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(SEARCH_WIDTH)
        self._search.addAction(player_icons.draw('search', player_icons.COLOR_MUTED, 16),
                               QLineEdit.LeadingPosition)
        self._search.returnPressed.connect(self._search_from_topbar)
        tools_row.addWidget(self._search)

        # Значки не только показывают состояние, но и чинят его: по VK
        # открывается вход, по YouTube — настройки движка. Другого входа в
        # аккаунт из основного окна нет, и он всегда на виду
        self._vk_chip = StatusChip('vk', 'off')
        self._vk_chip.set_clickable(True)
        self._vk_chip.clicked.connect(self._on_vk_chip_clicked)
        tools_row.addWidget(self._vk_chip)
        self._js_chip = StatusChip('youtube', 'off')
        self._js_chip.set_clickable(True)
        self._js_chip.clicked.connect(lambda: self._open_settings(TAB_ACCOUNTS))
        tools_row.addWidget(self._js_chip)
        # Виден, только пока что-то заливается: постоянный «0 в очереди» ни о чём
        self._upload_chip = StatusChip('upload', 'warn')
        self._upload_chip.hide()
        tools_row.addWidget(self._upload_chip)
        # Fixed: внутри группы поле ввода, и без этого FlowRow считал её
        # растяжимой — отдавал ей весь остаток строки, а поиск отъезжал от края
        tools.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row.add(tools)

        self._refresh_status_chips()
        self._topbar = bar
        return bar

    def _toggle_maximized(self) -> None:
        """Двойной щелчок по полосе и средняя кнопка делают одно и то же."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._title_bar.set_maximized(self.isMaximized())

    def _edge_at(self, pos) -> Qt.Edges:
        """Край окна под курсором: без системной рамки тянуть его больше нечем."""
        margin = RESIZE_MARGIN
        edges = Qt.Edges()
        if pos.x() <= margin:
            edges |= Qt.LeftEdge
        elif pos.x() >= self.width() - margin:
            edges |= Qt.RightEdge
        if pos.y() <= margin:
            edges |= Qt.TopEdge
        elif pos.y() >= self.height() - margin:
            edges |= Qt.BottomEdge
        return edges

    def _apply_resize_cursor(self, edges: Qt.Edges) -> None:
        shapes = {
            Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor,
            Qt.TopEdge: Qt.SizeVerCursor, Qt.BottomEdge: Qt.SizeVerCursor,
            Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor,
            Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor,
            Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor,
            Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor,
        }
        shape = shapes.get(edges)
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def mouseMoveEvent(self, event) -> None:
        if not self.isMaximized():
            self._apply_resize_cursor(self._edge_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self.isMaximized():
            edges = self._edge_at(event.position().toPoint())
            handle = self.windowHandle()
            if edges and handle is not None:
                # Растягивает сама система: рамка окна остаётся ровной, а окно
                # не дёргается вслед за пересчётом раскладки на каждый пиксель
                handle.startSystemResize(edges)
                event.accept()
                return
        super().mousePressEvent(event)

    def _search_from_topbar(self) -> None:
        """Enter в строке поиска открывает общий поиск с уже набранным текстом."""
        text = self._search.text().strip()
        self._search.clear()
        self._open_global_search(text)

    def _refresh_header(self) -> None:
        """Пересчитать полосу заголовка после смены надписей на вкладках.

        Набор вкладок и счётчики в них меняются на ходу, а переносимая строка
        раскладывает элементы по их sizeHint — без явного пересчёта вкладки
        оставались в старой, более узкой рамке и подписи резались многоточием."""
        self._section_tabs.updateGeometry()
        self._header_row.layout().invalidate()
        self._header_row.updateGeometry()

    def _build_nav(self) -> QListWidget:
        # Вкладки раздела живут в полосе заголовка, но создать их надо здесь:
        # меню собирается первым и сразу выбирает раздел
        self._section_tabs = QTabBar()
        self._section_tabs.setObjectName('sectionTabs')
        self._section_tabs.setExpanding(False)
        self._section_tabs.setDrawBase(False)
        # Без кнопок прокрутки: две белые стрелки поверх заголовка раздела
        # выглядели чужеродно, а подписи вкладок и так короткие
        self._section_tabs.setUsesScrollButtons(False)
        self._section_tabs.setElideMode(Qt.ElideNone)
        self._section_tabs.currentChanged.connect(self._on_section_tab)
        self._section_tabs.hide()

        nav = QListWidget()
        nav.setObjectName('nav')
        self._nav_narrow = False
        # Ширину задаёт _apply_responsive: в узком окне список разделов сужается,
        # иначе на 620 px от содержимого ничего не остаётся
        nav.setFixedWidth(NAV_WIDTH)
        # Список разделов умеет прокручиваться, поэтому свою высоту он диктовать
        # не должен: иначе в низком окне он держал бы минимум по всем пунктам и
        # не давал сжать окно, хотя содержимое страницы уже помещается
        nav.setMinimumHeight(0)
        nav.setSizePolicy(nav.sizePolicy().horizontalPolicy(), QSizePolicy.Ignored)
        nav.setFrameShape(QListWidget.NoFrame)
        nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav.setIconSize(QSize(18, 18))
        for (title, _pages), icon in zip(NAV_SECTIONS, NAV_ICONS):
            item = QListWidgetItem(title)
            item.setIcon(player_icons.draw(icon, player_icons.COLOR_NORMAL, 18))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            nav.addItem(item)
        # Раздел видео закрыт, пока нечего показывать: откроет его _update_video
        nav.item(VIDEO_SECTION).setHidden(True)
        self._before_video_section = 0
        nav.currentRowChanged.connect(self._on_section_changed)
        self._nav = nav
        return nav

    def _build_video_page(self) -> QWidget:
        """Раздел «Видео»: кадр во всю ширину, очередь рядом.

        Кадр 16:9 не может расти в ширину, не вырастая в высоту, поэтому в
        широком окне рядом с ним всё равно остаётся место. Занимает его
        очередь — она к видео ближе всего по смыслу: видно, что играет и что
        дальше. Сама панель одна на всё приложение и переезжает сюда, пока
        раздел открыт, — второй экземпляр слушал бы плеер второй раз."""
        page = QWidget()
        box = QHBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        # Кадр прижат к верху: 16:9 не даёт ему занять всю высоту, и висящий
        # в середине он оставлял бы пустые поля и сверху, и снизу
        box.addWidget(self._video_stage, 1, Qt.AlignTop)
        # Место под очередь: панель кладут сюда при входе в раздел и забирают
        # при выходе, поэтому слот пустует, а не хранит свой виджет
        self._video_queue_slot = QVBoxLayout()
        self._video_queue_slot.setContentsMargins(0, 0, 0, 0)
        box.addLayout(self._video_queue_slot)
        return page

    def _build_queue_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)

        box.addWidget(self._build_add_card())
        box.addWidget(self._build_queue_table(), 1)
        # Пустая таблица с одними заголовками столбцов не подсказывает ничего:
        # вместо неё — та же заглушка, что и в остальных разделах
        self._queue_empty = EmptyState(
            'download', 'Загрузок пока нет',
            'Вставьте ссылку на видео или плейлист выше и нажмите «Добавить», '
            'готовые файлы появятся в «Библиотеке».')
        box.addWidget(self._queue_empty, 1)
        self._queue_actions = self._build_queue_actions()
        box.addWidget(self._queue_actions)
        return page

    def _build_add_card(self) -> Card:
        card = Card()

        add_row = FlowRow(spacing=8)
        self._url_edit = QLineEdit()
        # Подсказка короткая, а подробности — в подсказке при наведении: полный
        # текст требовал 396 px, а полю в узком окне доставалось 164, и от него
        # оставалось «Ссылка на видео или…» — то есть ровно та половина, которая
        # ничего не уточняет. Минимум держит поле читаемым: когда места мало,
        # FlowRow переносит «Добавить» на свою строку, и это лучше, чем душить
        # главное поле страницы ради того, чтобы всё встало в одну линию
        self._url_edit.setPlaceholderText('Ссылка на видео или плейлист…')
        self._url_edit.setToolTip(
            'Ссылка на видео или плейлист YouTube либо на видео VK')
        self._url_edit.setMinimumWidth(240)
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.returnPressed.connect(self._on_add_url)
        self._url_edit.textChanged.connect(self._update_preview_btn)
        add_row.add(self._url_edit)
        self._preview_btn = QPushButton('Посмотреть')
        self._preview_btn.setObjectName('secondary')
        self._preview_btn.setToolTip('Открыть видео по ссылке, ничего не скачивая')
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self._on_preview_url)
        add_row.add(self._preview_btn)
        # В строку ввода список не вставишь — QLineEdit склеивает переносы,
        # поэтому для многих ссылок сразу есть отдельное окно
        self._bulk_btn = QPushButton('Списком')
        self._bulk_btn.setObjectName('secondary')
        self._bulk_btn.setToolTip('Вставить сразу много ссылок, по одной в строке')
        self._bulk_btn.clicked.connect(self._open_bulk_add)
        add_row.add(self._bulk_btn)
        self._add_btn = QPushButton('Добавить')
        # Текст кнопки меняется на «Читаю ссылку…» — без запаса по ширине
        # строка ввода дёргалась бы при каждом добавлении
        self._add_btn.setMinimumWidth(150)
        self._add_btn.clicked.connect(self._on_add_url)
        add_row.add(self._add_btn)
        card.layout().addWidget(add_row)

        # overflow: выбор формата, качества и галочка VK в узком окне занимали
        # три строки внутри карточки и отнимали высоту у самого списка загрузок
        opts_row = FlowRow(spacing=8, overflow=True)
        opts_row.add(QLabel('Скачивать как:'))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem('Музыку', 'audio')
        self._mode_combo.addItem('Видео', 'video')
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        opts_row.add(self._mode_combo)

        # Раньше лишние поля прятались через setVisible, и вся строка перестраивалась
        # при смене режима. Стопка держит ширину по самой широкой странице —
        # соседние элементы остаются на месте.
        # PagesStack: обычная стопка держала бы высоту по самой высокой из
        # страниц вариантов, даже когда открыта другая
        self._opts_stack = PagesStack()
        self._opts_stack.addWidget(self._build_audio_opts())
        self._opts_stack.addWidget(self._build_video_opts())
        opts_row.add(self._opts_stack)

        # Галочка живёт снаружи стопки: внутри она делала страницу настолько
        # широкой, что в узком окне обрезались уже сами списки
        self._auto_vk_check = QCheckBox('Сразу в музыку VK')
        self._auto_vk_check.setToolTip(
            'Скачанное здесь само уедет в «Мою музыку» вашего аккаунта VK')
        self._auto_vk_check.toggled.connect(self._on_auto_vk_toggled)
        opts_row.add(self._auto_vk_check)
        opts_row.add_stretch()
        card.layout().addWidget(opts_row)
        return card

    def _build_audio_opts(self) -> QWidget:
        # Полоска с переносом, а не жёсткая строка: четыре элемента подряд
        # требовали почти 500 точек и в узком окне растягивали всю страницу
        row = FlowRow(spacing=8)
        row.add(QLabel('Формат:'))
        self._format_combo = QComboBox()
        for label, value in AUDIO_FORMATS:
            self._format_combo.addItem(label, value)
        row.add(self._format_combo)
        row.add(QLabel('Битрейт:'))
        self._bitrate_combo = QComboBox()
        for label, value in AUDIO_BITRATES:
            self._bitrate_combo.addItem(label, value)
        row.add(self._bitrate_combo)
        return row

    def _build_video_opts(self) -> QWidget:
        row = FlowRow(spacing=8)
        row.add(QLabel('Качество:'))
        self._quality_combo = QComboBox()
        for label, value in VIDEO_QUALITIES:
            self._quality_combo.addItem(label, value)
        row.add(self._quality_combo)
        return row

    def _build_queue_table(self) -> QTableView:
        self._model = QueueTableModel()
        table = QTableView()
        table.setModel(self._model)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(table))
        header = table.horizontalHeader()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        table.doubleClicked.connect(self._open_finished_file)
        table.selectionModel().selectionChanged.connect(self._update_queue_buttons)
        self._table = table
        # Жёсткие 220 точек в узком окне съедали весь столбец с названием
        table.viewport().installEventFilter(self)
        self._fit_progress_column()
        return table

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Resize:
            # Фильтр стоит на всём приложении, и события идут в том числе
            # пока окно ещё собирается: таблицы тогда просто нет
            table = getattr(self, '_table', None)
            if table is not None and obj is table.viewport():
                self._fit_progress_column()
        elif kind in (QEvent.MouseMove, QEvent.MouseButtonPress):
            if self._resize_from_child(obj, event):
                return True
        return super().eventFilter(obj, event)

    def _resize_from_child(self, obj, event) -> bool:
        """Край окна под курсором ловим и над дочерними виджетами.

        Списки и таблицы забирают мышь себе, поэтому окно само видит движение
        только в редких пустых местах, и попасть в край было почти нельзя."""
        if self.isMaximized() or not isinstance(obj, QWidget):
            return False
        if self.isAncestorOf(obj) is False and obj is not self:
            return False
        point = self.mapFromGlobal(event.globalPosition().toPoint())
        edges = self._edge_at(point)
        if event.type() == QEvent.MouseMove:
            # Курсор ставим, только пока кнопку не держат: иначе он бы прыгал
            # посреди выделения текста или перетаскивания строки
            if not event.buttons():
                self._apply_resize_cursor(edges)
            return False
        if edges and event.button() == Qt.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemResize(edges)
                return True
        return False

    def _fit_progress_column(self) -> None:
        """Полоска прогресса занимает долю ширины, но остаётся читаемой."""
        width = self._table.viewport().width()
        self._table.horizontalHeader().resizeSection(
            COL_PROGRESS, max(110, min(220, int(width * 0.28))))

    def _build_queue_actions(self) -> QWidget:
        row = FlowRow(spacing=8, align_right=True)
        self._queue_summary = ElidedLabel()
        self._queue_summary.setObjectName('hint')
        row.add(self._queue_summary)
        row.add_stretch()

        self._open_btn = self._queue_button(row, 'Открыть файл', self._open_finished_file)
        self._cancel_btn = self._queue_button(row, 'Отменить', self._cancel_selected)
        self._clear_done_btn = self._queue_button(row, 'Убрать завершённые', self._clear_finished)
        return row

    def _queue_button(self, row, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName('secondary')
        button.clicked.connect(slot)
        row.add(button)
        return button

    # ================= навигация и статусы =================
    def _on_section_changed(self, row: int) -> None:
        """Выбрали раздел — показываем его вкладки и последнюю его страницу."""
        if not 0 <= row < len(NAV_SECTIONS):
            return
        pages = NAV_SECTIONS[row][1]
        wanted = self._last_page.get(row, pages[0])
        self._section_tabs.blockSignals(True)
        while self._section_tabs.count():
            self._section_tabs.removeTab(0)
        for page in pages:
            self._section_tabs.addTab(PAGE_TITLES[page])
        self._section_tabs.setCurrentIndex(pages.index(wanted))
        self._section_tabs.blockSignals(False)
        # Единственная вкладка ничего не переключает — прячем её
        self._section_tabs.setVisible(len(pages) > 1)
        self._refresh_header()
        self._update_nav_counters()
        self._show_page(wanted)

    def _on_section_tab(self, tab: int) -> None:
        pages = self._section_pages()
        if 0 <= tab < len(pages):
            self._show_page(pages[tab])

    def _section_pages(self) -> tuple:
        row = self._nav.currentRow()
        return NAV_SECTIONS[row][1] if 0 <= row < len(NAV_SECTIONS) else ()

    def _show_page(self, index: int) -> None:
        section = section_of_page(index)
        self._last_page[section] = index
        # В заголовке — название раздела, а не страницы: страницу и так видно
        # на выбранной вкладке рядом, а два одинаковых слова подряд лишние
        self._page_title.setText(NAV_SECTIONS[section][0])
        self._pages.setCurrentIndex(index)
        self._sync_video_queue(index)
        if index == PAGE_LIBRARY and self._library_dirty:
            self._library_dirty = False
            self._library_page.reload()
        elif index == PAGE_MIX:
            # Источники могли появиться уже после запуска — например, вход в VK
            self._mix_page.refresh_sources()
        elif index == PAGE_YOUTUBE:
            # Лента сама решает, надо ли идти в сеть: у неё свой срок годности
            self._youtube_page.reload()
        # Остальные разделы читают базу при показе: держать их в свежем виде
        # постоянно незачем
        elif self._page_dirty.get(index):
            self._page_dirty[index] = False
            {PAGE_HOME: self._home_page, PAGE_TRACKS: self._tracks_page,
             PAGE_LOCAL: self._local_page,
             PAGE_PLAYLISTS: self._playlists_page,
             PAGE_HISTORY: self._history_page}[index].reload()

    def _select_page(self, index: int) -> None:
        """Открыть страницу откуда угодно: раздел и вкладку подберём сами."""
        section = section_of_page(index)
        self._last_page[section] = index
        if self._nav.currentRow() != section:
            self._nav.setCurrentRow(section)    # дальше сработает _on_section_changed
            return
        self._section_tabs.setCurrentIndex(NAV_SECTIONS[section][1].index(index))
        self._show_page(index)

    def _refresh_status_chips(self) -> None:
        """Цвет значка службы и подробности в подсказке.

        Надписи «VK на связи» и «YouTube готов» съедали полполосы, а в узком
        окне сжимались до безымянной точки. Теперь состояние передаёт цвет
        значка, а название и подробности лежат в подсказке."""
        if self._vk_offline:
            self._vk_chip.update_chip('VK без связи', 'warn',
                                      'Сохранённый вход цел, но подключиться не вышло.\n'
                                      'Нажмите, чтобы повторить попытку')
        elif self._vk_client and not self._vk_client.has_web_session:
            # Плейлисты через API видны, а треки нет: чип не должен обещать «всё хорошо»
            self._vk_chip.update_chip('VK не до конца', 'warn',
                                      'VK не выдал сессию сайта, список музыки не загрузится.\n'
                                      'Нажмите, чтобы войти заново')
        elif self._vk_client:
            self._vk_chip.update_chip('VK на связи', 'ok',
                                      f'Вы вошли, аккаунт id{self._vk_client.user_id}\n'
                                      'Нажмите, чтобы открыть настройки аккаунта')
        else:
            self._vk_chip.update_chip('VK не подключён', 'off',
                                      'Музыка VK доступна только после входа.\n'
                                      'Нажмите, чтобы войти')
        if js_runtime.find():
            self._js_chip.update_chip('YouTube готов', 'ok',
                                      js_runtime.describe() + '\nНажмите, чтобы открыть настройки')
        else:
            self._js_chip.update_chip('YouTube без движка', 'warn',
                                      'Без движка JavaScript YouTube не скачивается.\n'
                                      'Нажмите, чтобы скачать движок')

    def _on_library_count(self, count: int) -> None:
        self._nav_counts[PAGE_LIBRARY] = count
        self._update_nav_counters()

    def _update_nav_counters(self) -> None:
        """Числа у названий: сколько файлов в библиотеке и задач в работе.

        В меню слева показываем счётчик только главной страницы раздела —
        складывать числа разного смысла (задачи и файлы) нельзя, а без числа
        в меню идущие загрузки были бы не видны из других разделов. Остальные
        страницы показывают свои счётчики на вкладках."""
        for row, (title, pages) in enumerate(NAV_SECTIONS):
            count = self._nav_counts.get(pages[0], 0)
            item = self._nav.item(row)
            if self._nav_narrow:
                # В узком меню подписи не помещались и обрывались многоточием
                # («Моя му…»). Остаются значок и число — понятно и не режется
                item.setText(str(count) if count else '')
            else:
                item.setText(f'{title}  ({count})' if count else title)
            item.setToolTip(f'{title}: {count}' if count else title)
        for tab, page in enumerate(self._section_pages()):
            if tab < self._section_tabs.count():
                count = self._nav_counts.get(page, 0)
                self._section_tabs.setTabText(
                    tab, f'{PAGE_TITLES[page]}  ({count})' if count else PAGE_TITLES[page])
        self._refresh_header()

    # ================= музыкальный центр =================
    def _wire_track_list(self, widget, on_play=None) -> None:
        """Общая проводка списка треков: играть, в очередь, «+ VK», сердечко.

        Списки во всех разделах одинаковые, поэтому и обработчики одни: иначе один
        и тот же код пришлось бы повторять в каждом разделе."""
        widget.play_requested.connect(on_play or self._play_tracks)
        widget.enqueue_requested.connect(self._enqueue_tracks)
        widget.add_vk_requested.connect(self._import_tracks_to_vk)
        widget.favorite_requested.connect(self._toggle_favorites)
        widget.library_requested.connect(self._toggle_library)
        widget.offline_requested.connect(self._toggle_offline)
        widget.download_requested.connect(self._download_tracks)
        widget.open_source_requested.connect(self._open_track_source)
        widget.radio_requested.connect(self._start_radio)
        widget.hide_requested.connect(self._hide_tracks)
        widget.hide_artist_requested.connect(self._hide_artist)
        widget.artist_requested.connect(self._show_artist)
        widget.playlist_requested.connect(self._add_tracks_to_playlist)
        widget.set_store(self._store)
        if self._vk_states:
            widget.set_vk_states(self._vk_states)
        self._track_lists.append(widget)

    def _status_message(self, text: str) -> None:
        if text:
            self.statusBar().showMessage(text, 8000)

    def _go_to(self, name: str) -> None:
        if name == 'favorites':
            # Избранное — системный плейлист, своей страницы у него больше нет
            self._select_page(PAGE_PLAYLISTS)
            self._playlists_page.show_favorites()
        elif name in PAGE_NAMES:
            self._select_page(PAGE_NAMES.index(name))

    # ---------- очередь сбоку ----------
    def _toggle_queue_panel(self) -> None:
        # Считаем от желания, а не от видимости: панель бывает спрятана пустой
        # очередью, и тогда нажатие кнопки должно её показать, а не «выключить»
        self._queue_wanted = not self._queue_wanted
        self._settings['queue_panel'] = self._queue_wanted
        self._apply_responsive()
        if self._queue_wanted and not self._queue_panel.isVisible():
            # Окно слишком узкое — панель не влезет, и молчать об этом нечестно
            self._status_message('Очередь появится, когда окно станет шире')

    def _install_edge_filter(self) -> None:
        """Слушать мышь всего приложения. Только когда окно уже собрано.

        Край окна проходит поверх списков и таблиц, они забирают движение мыши
        себе, и попасть в полосу захвата иначе почти нельзя. Ставим фильтр в
        конце сборки: события, идущие во время неё, ловить нечем и незачем."""
        QApplication.instance().installEventFilter(self)

    def _toggle_nav(self) -> None:
        """Свернуть список разделов до значков и обратно."""
        self._nav_collapsed = not self._nav_collapsed
        self._settings['nav_collapsed'] = self._nav_collapsed
        self._apply_responsive()

    def _hide_queue_panel(self) -> None:
        self._queue_wanted = False
        self._settings['queue_panel'] = False
        self._queue_panel.hide()

    def _sync_video_queue(self, index: int) -> None:
        """Перевесить очередь в раздел видео и обратно.

        Панель одна на всё приложение: рядом с кадром она занимает место,
        которое 16:9 всё равно не может занять, а в остальных разделах стоит
        у правого края окна. Второй экземпляр слушал бы плеер второй раз."""
        on_video = index == PAGE_VIDEO
        if on_video == self._queue_in_video:
            return
        self._queue_in_video = on_video
        if on_video:
            self._video_queue_slot.addWidget(self._queue_panel)
        else:
            self._video_queue_slot.removeWidget(self._queue_panel)
            self._queue_home.addWidget(self._queue_panel)
        self._apply_responsive()

    def _apply_responsive(self) -> None:
        """Подогнать раскладку под ширину окна.

        Правило простое: сначала уступает очередь, потом сужается список
        разделов. Горизонтальной полосы прокрутки у окна быть не должно.

        По высоте то же самое: в низком окне уходит подвал меню — «Настройки»
        есть и в полосе заголовка под «⋯», а строка под них отнимала у списка
        разделов целый пункт."""
        width = self.width()
        self._sidebar_footer.setVisible(self.height() >= 380)
        # Пустая очередь панель не показывает: рамка с надписью «пусто» занимает
        # треть окна и ничего не сообщает. Появится сама, как только что-то заиграет.
        has_queue = bool(self._player.queue.tracks)
        # В разделе видео очередь — часть страницы, а не боковая панель: кнопка
        # «скрыть очередь» её не касается. Но в узком окне она уступает кадру:
        # вдвоём им остаётся меньше 16:9, и кадр приходилось бы плющить
        self._queue_panel.setVisible(
            has_queue and (self._queue_in_video and width >= VIDEO_QUEUE_MIN_WIDTH
                           or (not self._queue_in_video and self._queue_wanted
                               and width >= QUEUE_PANEL_MIN_WIDTH)))
        # Узкое окно сворачивает меню само, но и в широком его можно свернуть
        narrow = width < NARROW_WIDTH or self._nav_collapsed
        nav_width = NAV_WIDTH_NARROW if narrow else NAV_WIDTH
        if narrow != self._nav_narrow:
            self._nav_narrow = narrow
            align = Qt.AlignHCenter if narrow else Qt.AlignLeft
            for row in range(self._nav.count()):
                self._nav.item(row).setTextAlignment(align | Qt.AlignVCenter)
            self._update_nav_counters()
        self._nav.setFixedWidth(nav_width)
        self._sidebar.setFixedWidth(nav_width)
        # Поиск больше не ужимается: сужали его, чтобы группа с чипами не
        # выдавливала вкладки разделов на вторую строку, — но выдавливала она
        # их не шириной, а тем, что стояла отдельным соседом и забирала своё
        # место намертво. Теперь группа внутри самой полосы и переносится
        # целиком, когда не помещается, так что отнимать у поиска нечего:
        # в узком окне 150 px не хватало даже на подсказку, она обрывалась
        # на «Поиск вез…»
        self._settings_btn.setText('' if narrow else ' Настройки')
        # Уголок смотрит туда, куда поедет меню: вправо, если разворачивать
        self._nav_toggle.setIcon(player_icons.draw(
            'chevron_right' if narrow else 'chevron_left', player_icons.COLOR_MUTED))
        self._nav_toggle.setToolTip('Развернуть меню' if narrow else 'Свернуть меню')
        # Кнопка бесполезна, пока меню свёрнуто самой шириной окна: развернуть
        # его всё равно негде
        self._nav_toggle.setEnabled(width >= NARROW_WIDTH)
        # Кадру достаётся высота страницы целиком: он один на ней и ни с чем не
        # делит её по вертикали. Меряем именно страницу, а не окно: вычитание
        # круглой поправки из высоты окна оставляло под кадром до 308 px пустоты
        self._video_stage.set_height_limit(self._pages.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive()

    @staticmethod
    def _typing() -> bool:
        """Человек сейчас что-то печатает? Тогда пробел — это пробел."""
        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QTextEdit, QAbstractSpinBox, QComboBox))

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            # Сначала полный экран, потом очередь — в обратном порядке Esc
            # закрывал бы панель, оставляя видео на весь экран
            if self._video_stage.fullscreen:
                self._video_stage.set_fullscreen(False)
                return
            if self._queue_panel.isVisible():
                self._hide_queue_panel()
                return
        elif key == Qt.Key_Space and not self._typing():
            self._player.toggle()
            return
        super().keyPressEvent(event)

    # ---------- воспроизведение ----------
    def _play_tracks(self, tracks, index: int = 0) -> None:
        tracks = list(tracks or [])
        # Включили что-то помимо микса — продолжать очередь миксом больше незачем
        self._mix_follow = None
        if tracks:
            self._player.play_tracks(tracks, max(0, min(index, len(tracks) - 1)))

    def _enqueue_tracks(self, tracks, play_next: bool = False) -> None:
        tracks = list(tracks or [])
        if not tracks:
            return
        self._player.enqueue(tracks, play_next)
        self._status_message(f'В очередь добавлено: {len(tracks)}')

    def _play_mix(self, tracks, index: int = 0) -> None:
        """Включить волну микса.

        Отличие от обычного списка одно: когда очередь подойдёт к концу,
        продолжение собирает тот же микс, а не лента YouTube, — иначе
        «бесконечно» через час превращается в один источник."""
        config = self._mix_page.config()
        self._play_tracks(tracks, index)
        self._player.set_autoplay(config.autoplay)
        self._mix_follow = self._mixer.continuation(config) if config.autoplay else None

    def _autoplay_next(self, seed, exclude, limit):
        """Чем плеер продолжает очередь, когда она кончилась.

        Обычно это рекомендации YouTube, а во время микса — сам микс. Микс может
        и не дать ничего (нет связи с VK), поэтому за ним остаётся привычный запас."""
        follow = self._mix_follow
        if follow is not None:
            tracks = follow(seed, exclude, limit)
            if tracks:
                return tracks
        return self._recommender.autoplay(seed, exclude, limit)

    def _start_mix_from_home(self, page: str, data: dict) -> None:
        """Кнопка запуска с главной: сперва показать раздел, потом включить волну.

        Раздел открывается всегда, даже если микс не соберётся: человек нажал
        «Волна VK» и должен оказаться в музыке VK — там ему и скажут, чего не
        хватает. Сама волна собирается на странице микса: она умеет и показать
        настройки, и сообщить о неудаче, а дублировать это здесь незачем.
        """
        self._go_to(page)
        # Настройки уезжают на страницу микса: кнопка предзаполняет ручки, а не
        # прячет их — дальше волну правят руками, как после настроения
        self._mix_page.start_config(MixConfig.from_dict(data))

    def _on_mix_config(self, data: dict) -> None:
        self._settings['mix'] = data
        config.save_settings(self._settings)

    def _local_media(self) -> list:
        """Свои файлы для микса. Обход папок долгий, поэтому зовётся из фона."""
        return library.scan([self._settings.get('music_dir')])

    def _play_vk_rows(self, rows: list, start: int = 0) -> None:
        self._play_tracks([from_vk(row) for row in rows], start)

    def _enqueue_vk_rows(self, rows: list, play_next: bool = False) -> None:
        self._enqueue_tracks([from_vk(row) for row in rows], play_next)

    def _play_local_files(self, paths: list) -> None:
        self._play_tracks([from_local(path) for path in paths], 0)

    def _enqueue_local_files(self, paths: list) -> None:
        self._enqueue_tracks([from_local(path) for path in paths])

    def _on_player_track(self, track) -> None:
        self._update_video(track)
        if track is not None:
            label = self._vk_label(track)
            self._now_playing.set_favorite(self._store.is_favorite(track.uid))
            self._now_playing.set_vk_state(track.uid, label)
            if self._mini is not None:
                self._mini.set_favorite(self._store.is_favorite(track.uid))
                self._mini.set_vk_state(track.uid, label)
            if self._tray is not None:
                self._tray.set_favorite(self._store.is_favorite(track.uid))
                self._tray.set_vk_state(label)
            self._page_dirty[PAGE_HOME] = True
            self._page_dirty[PAGE_HISTORY] = True
        for widget in self._track_lists:
            widget.set_current(track)

    # ---------- видео ----------
    def _video_wanted(self, track) -> bool:
        """Нужна ли сейчас картинка на пол-окна.

        Ролик YouTube — это почти всегда музыка, и разворачивать его клип без
        спроса не за чем: раньше сцена в режиме «по источнику» висела над каждой
        страницей, чаще всего пустая. Само собой видео показываем только для
        своих видеофайлов — там кроме картинки ничего и нет; для остального
        нужен явный режим «с видео» (значок в полосе плеера)."""
        if track is None:
            return False
        mode = self._player.mode
        if mode == MODE_AUDIO:
            return False
        if mode == MODE_VIDEO:
            return True
        return bool(track.is_video and track.source != SOURCE_YOUTUBE)

    def _update_video(self, track=None) -> None:
        """Показывать картинку, только когда её есть чем наполнить.

        Ролик YouTube рисует своя страница, скачанное видео — видеовыход общего
        плеера; в остальное время это была бы пустая чёрная полоса на полстраницы.
        Режим «только звук» прячет её и для видео — воспроизведение при этом не
        трогаем: страница плеера продолжает работать невидимой."""
        track = track if track is not None else self._player.current
        wanted = self._video_wanted(track)
        if wanted:
            youtube = track.source == SOURCE_YOUTUBE
            self._video_stage.set_widget(
                self._yt_backend.view if youtube
                else self._player.video_widget())
            self._video_stage.set_title(track.display_title)
            # Соотношение нового ролика ещё неизвестно: до первого кадра рамка
            # стоит 16:9, дальше её поправит videoSizeChanged. Страница YouTube
            # размер не сообщает — там 16:9 остаётся навсегда
            self._video_stage.set_aspect(0, 0)
            if youtube:
                self._want_clip(track)
            else:
                self._watch_video_size()
        if not wanted and self._video_stage.fullscreen:
            self._video_stage.set_fullscreen(False)
        self._sync_video_section(wanted)
        self._on_video_state(self._player.state)

    def _want_clip(self, track) -> None:
        """Поискать настоящий клип для песни из YouTube Music.

        В YouTube Music песня чаще всего лежит «art track» — роликом, где вместо
        картинки одна обложка альбома. Для звука это неважно, а в видеорежиме
        смотреть нечего, поэтому здесь один фоновый запрос за клипом того же
        трека, и если он нашёлся — страница переключается на него.

        Ищем только отсюда, то есть только когда картинку правда показывают: в
        режиме «только звук» лишних походов в сеть не будет вовсе. Ответ, в том
        числе «клипа нет», Discovery помнит весь сеанс, так что повтор трека
        обходится без запроса."""
        video_id = track.youtube_id if track is not None else ''
        if not video_id or video_id in self._clip_tried:
            return
        self._clip_tried.add(video_id)

        def on_done(found, error):
            if error or not found:
                return
            # Пока искали, человек мог переключить трек или уйти в «только звук»
            current = self._player.current
            if current is None or current.youtube_id != video_id:
                return
            if not self._video_wanted(current):
                return
            self._yt_backend.swap_clip(found, self._player.position)

        run_async(self._discovery.music_video, on_done, track)

    def _sync_video_section(self, wanted: bool) -> None:
        """Показать или спрятать раздел «Видео» в меню.

        Нечего показывать — нет и пункта: пустой раздел с чёрным
        прямоугольником внутри только сбивает с толку. Если его прячут прямо
        во время просмотра, уводим на предыдущий раздел, иначе окно осталось
        бы на странице, которой в меню больше нет."""
        item = self._nav.item(VIDEO_SECTION)
        if item is None or item.isHidden() == (not wanted):
            return
        item.setHidden(not wanted)
        if not wanted and self._nav.currentRow() == VIDEO_SECTION:
            self._nav.setCurrentRow(self._before_video_section)
        elif wanted:
            row = self._nav.currentRow()
            if row != VIDEO_SECTION:
                self._before_video_section = row

    def _watch_video_size(self) -> None:
        """Следить за настоящим размером кадра локального видео.

        Подписываемся один раз на всё приложение: видеовыход один и переживает
        смену треков, а второй коннект приводил бы к двойному пересчёту."""
        if self._video_size_watched:
            return
        sink = self._player.video_widget().videoSink()
        if sink is None:
            return
        self._video_size_watched = True
        sink.videoSizeChanged.connect(
            lambda size: self._video_stage.set_aspect(size.width(), size.height()))

    def _on_video_state(self, state: str) -> None:
        """Пока плеер ищет ссылку или поднимает страницу, показывать нечего —
        вместо чёрного прямоугольника в сцене стоит подпись.

        Подпись ставим и когда сцена скрыта. Раньше здесь стоял ранний выход по
        isVisible(), и он давал ровно ту чёрную заслонку, от которой подпись
        должна была спасать: включаешь видеорежим, страница поднимается, пока
        раздел «Видео» ещё не открыт, — «играю» приходит в скрытую сцену и
        пропадает. Открываешь раздел, а поверх картинки висит «Готовим видео…»
        во всю ширину кадра, и уйти ей больше не с чего: состояние плеера
        второй раз не меняется. Оставалась она и после выключения режима."""
        self._video_stage.set_note('Готовим видео…'
                                   if state in (STATE_LOADING, STATE_RESOLVING) else '')

    def _toggle_video_fullscreen(self) -> None:
        if not self._video_stage.isVisible() and not self._video_stage.fullscreen:
            self._status_message('Сейчас нечего показывать во весь экран')
            return
        self._video_stage.toggle_fullscreen()

    def _on_fullscreen_changed(self, enabled: bool) -> None:
        self._status_message('Esc: выйти из полного экрана' if enabled else '')

    def _expand_player(self) -> None:
        """Развёрнутый вид: пока это раздел YouTube с видео и очередь сбоку."""
        track = self._player.current
        if track is not None and track.source == SOURCE_YOUTUBE:
            self._select_page(PAGE_YOUTUBE)
        self._queue_wanted = True
        self._apply_responsive()

    # ---------- радио и «не нравится» ----------
    def _start_radio(self, track) -> None:
        """Бесконечная подборка по треку: сам трек первый, остальное подтянется."""
        if track is None:
            return
        self._status_message(f'Собираю радио по «{track.display_title}»…')
        self._player.play_tracks([track], 0)

        def on_done(tracks, error):
            if error:
                logger.info('Радио не собралось: %s', error)
                self._status_message('Радио не собралось: нет связи с YouTube')
                return
            if not tracks:
                self._status_message('Похожего не нашлось')
                return
            # Очередь могла смениться, пока ходили в сеть: добавляем в конец,
            # ничего не перезапуская
            self._player.enqueue(tracks, False)
            self._status_message(f'Радио по «{track.display_title}»: +{len(tracks)}')

        run_async(self._recommender.radio, on_done, track)

    def _start_artist_radio(self, artist: str) -> None:
        artist = (artist or '').strip()
        if not artist:
            return
        self._status_message(f'Собираю радио по «{artist}»…')

        def on_done(tracks, error):
            if error or not tracks:
                self._status_message('Радио по исполнителю не собралось')
                return
            self._player.play_tracks(tracks, 0)
            self._status_message(f'Радио по «{artist}»: {len(tracks)}')

        run_async(self._recommender.artist_radio, on_done, artist)

    def _show_artist(self, artist: str) -> None:
        """Страница исполнителя: своё из базы плюс найденное в YouTube.

        Окно одно на всех: второй щелчок по другому имени не плодит окна,
        а показывает в том же нового исполнителя."""
        artist = (artist or '').strip()
        if not artist:
            return
        page = self._artist_view
        if page is None:
            page = ArtistPage('', self._store, self._discovery, self)
            self._wire_track_list(page.list)
            page.radio_requested.connect(self._start_artist_radio)
            page.hide_artist_requested.connect(self._hide_artist)
            self._artist_view = page
        page.set_artist(artist)
        page.show()
        page.raise_()
        page.activateWindow()

    def _hide_tracks(self, tracks) -> None:
        """«Не нравится»: трек больше не появится в рекомендациях и радио."""
        count = 0
        for track in tracks or []:
            self._store.hide_track(track.uid)
            count += 1
        if count:
            self._status_message(f'Больше не предложу: {count}')

    def _dislike_playing(self, tracks) -> None:
        """«Не нравится» у играющего трека: кроме скрытия сразу переключаем
        дальше. В списках так делать нельзя — там отмечают пачку чужих строк,
        а здесь речь ровно о том, что звучит в эту секунду."""
        self._hide_tracks(tracks)
        current = self._player.current
        if current is not None and any(track.uid == current.uid
                                       for track in tracks or []):
            self._player.next()

    def _hide_artist(self, artist: str) -> None:
        if artist:
            self._store.hide_artist(artist)
            self._status_message(f'«{artist}» больше не в рекомендациях')

    # ---------- поиск везде ----------
    def _open_global_search(self, text: str = '') -> None:
        if self._search_dialog is None:
            self._search_dialog = GlobalSearchDialog(
                self._discovery, lambda: self._vk_client, self._store,
                lambda: self._settings, self)
            self._wire_track_list(self._search_dialog.list)
            self._search_dialog.open_page_requested.connect(self._go_to)
        self._search_dialog.show()
        self._search_dialog.raise_()
        self._search_dialog.activateWindow()
        self._search_dialog.focus_search(text)

    def _on_player_error(self, message: str) -> None:
        self._status_message(message)

    # ---------- «+ VK» ----------
    def _import_tracks_to_vk(self, tracks) -> None:
        tracks = list(tracks or [])
        started = sum(1 for track in tracks if self._import.add(track))
        if tracks and not started:
            self._status_message('Переносить нечего: записи уже в VK или перенос уже идёт')

    def _vk_label(self, track) -> str:
        """Что написать на кнопке «+ VK» для этого трека.

        Пустая строка — кнопка работает. Свои записи VK и уже перенесённые
        треки отмечены галочкой, идущий перенос — своей подписью."""
        label = self._vk_states.get(track.uid)
        if label:
            return label
        state = self._import.state_of(track)
        return STATE_LABELS.get(state, '') if state else ''

    def _on_import_state(self, uid: str, _state: str, label: str) -> None:
        self._vk_states[uid] = label
        for widget in self._track_lists:
            widget.set_vk_state(uid, label)
        self._now_playing.set_vk_state(uid, label)
        if self._mini is not None:
            self._mini.set_vk_state(uid, label)
        current = self._player.current
        if self._tray is not None and current is not None and current.uid == uid:
            self._tray.set_vk_state(label)

    def _on_import_finished(self, uid: str, ok: bool, message: str) -> None:
        self._status_message(message)
        if ok:
            self._page_dirty[PAGE_HOME] = True
        else:
            logger.info('«+ VK» не получился для %s: %s', uid, message)

    def _on_import_ambiguous(self, uid: str, candidates) -> None:
        """Похожих записей несколько — выбирает человек, а не приложение."""
        items = [f'{track.display_title}  ({score:.0%})' for track, score in candidates]
        items.append('Ничего не подходит: скачать и залить свой файл')
        choice, ok = QInputDialog.getItem(
            self, 'Нашлось похожее', 'Что добавить в вашу музыку VK?', items, 0, False)
        if not ok:
            self._import.cancel(uid)
            self._on_import_state(uid, '', '')
            return
        index = items.index(choice)
        self._import.choose(uid, candidates[index][0] if index < len(candidates) else None)

    # ---------- избранное и скачивание ----------
    def _toggle_favorites(self, tracks) -> None:
        added = []
        for track in tracks or []:
            if self._store.is_favorite(track.uid):
                self._store.remove_favorite(track.uid)
            else:
                self._store.add_favorite(track)
                added.append(track)
        # «Любимое» человек слушает чаще всего — его и держим на диске. Копию при
        # снятии сердечка не удаляем: место освободит лимит, когда понадобится
        if added and self._settings.get('offline_favorites'):
            self._offline.ensure(added)
        self._mark_tracks_dirty()
        current = self._player.current
        if current is not None:
            self._now_playing.set_favorite(self._store.is_favorite(current.uid))
            if self._mini is not None:
                self._mini.set_favorite(self._store.is_favorite(current.uid))
            if self._tray is not None:
                self._tray.set_favorite(self._store.is_favorite(current.uid))
        # «Любимое» — обычный плейлист, и меняется он вместе с остальными
        if self._pages.currentIndex() == PAGE_PLAYLISTS:
            self._playlists_page.refresh_current()
        else:
            self._page_dirty[PAGE_PLAYLISTS] = True

    def _mark_tracks_dirty(self) -> None:
        """Фонотека изменилась: перечитать сразу или при следующем показе.

        Обе страницы смотрят в одну базу: сохранённый трек может оказаться
        и своим файлом, поэтому помечаются обе.
        """
        current = self._pages.currentIndex()
        pages = {PAGE_TRACKS: self._tracks_page, PAGE_LOCAL: self._local_page}
        for index, page in pages.items():
            if current == index:
                page.reload()
                self._page_dirty[index] = False
            else:
                self._page_dirty[index] = True

    def _mark_library_dirty(self) -> None:
        """Библиотеку перечитать. Открытую сразу, закрытую при следующем заходе.

        Раньше стоял только флаг, и если библиотека была открыта в момент, когда
        загрузка закончилась, список не обновлялся уже никогда: заход на страницу
        второй раз не происходил."""
        if self._pages.currentIndex() == PAGE_LIBRARY:
            self._library_dirty = False
            self._library_page.reload()
        else:
            self._library_dirty = True

    def _toggle_library(self, tracks) -> None:
        """«В мою музыку» — тот же список, что и на вкладке «Треки»."""
        tracks = [t for t in tracks or [] if t is not None]
        if not tracks:
            return
        # Действие одно на всё выделение: пункт меню обещал либо добавить, либо убрать
        saved = all(self._store.is_saved(t.uid) for t in tracks)
        for track in tracks:
            if saved:
                self._store.remove_from_library(track.uid)
            else:
                self._store.save_to_library(track)
        self._mark_tracks_dirty()
        self._status_message(f'Убрано из моей музыки: {len(tracks)}' if saved
                             else f'В мою музыку: {len(tracks)}')

    def _toggle_offline(self, tracks) -> None:
        """Сохранить офлайн или убрать копию. Качает общий загрузчик."""
        tracks = [t for t in tracks or [] if t is not None]
        if not tracks:
            return
        uids = {t.uid for t in tracks}
        if self._store.cached_uids(uids) >= uids:
            for uid in uids:
                self._offline.remove(uid)
            self._mark_tracks_dirty()
            self._status_message(f'Офлайн-копии убраны: {len(uids)}')
            return
        # Офлайн — это про фонотеку: то, что сохраняют на диск, должно быть и в ней
        for track in tracks:
            self._store.save_to_library(track)
        started = self._offline.ensure(tracks)
        self._mark_tracks_dirty()
        self._status_message(f'Сохраняю офлайн: {started}' if started
                             else 'Эти треки нечем сохранить офлайн')

    def _on_local_dirs_changed(self, dirs) -> None:
        self._settings['local_dirs'] = list(dirs or [])
        config.save_settings(self._settings)

    def _add_tracks_to_playlist(self, tracks, playlist_id: int) -> None:
        """Пункт «Добавить в плейлист» из любого списка треков.

        Раньше свои подборки было нечем наполнить: создать можно, положить в них
        трек — нельзя."""
        tracks = list(tracks or [])
        if not tracks:
            return
        if playlist_id == NEW_PLAYLIST:
            title, ok = QInputDialog.getText(self, 'Новый плейлист', 'Название:')
            title = (title or '').strip()
            if not ok or not title:
                return
            playlist_id = self._store.create_playlist(title)
        playlist = self._store.get_playlist(playlist_id)
        if playlist is None:
            return
        for track in tracks:
            self._store.add_to_playlist(playlist_id, track)
        self._page_dirty[PAGE_PLAYLISTS] = True
        if self._pages.currentIndex() == PAGE_PLAYLISTS:
            self._playlists_page.reload()
            self._page_dirty[PAGE_PLAYLISTS] = False
        self._status_message(f'В «{playlist["title"]}»: {len(tracks)} трек(ов)')

    def _download_tracks(self, tracks) -> None:
        """Обычное скачивание — тем же загрузчиком, что и на вкладке «Загрузки»."""
        added = 0
        for track in tracks or []:
            if track.source == SOURCE_VK:
                row = to_vk_row(track)
                self._manager.add_vk_track(row, track_key(row))
                added += 1
            elif track.source == SOURCE_LOCAL:
                continue
            elif track.url:
                key = history.key_for(track.source, track.source_id, 'audio')
                self._manager.add_youtube(track.url, track.display_title, key,
                                          settings_override={'mode': 'audio'})
                added += 1
        if added:
            self._select_page(PAGE_QUEUE)

    def _open_track_source(self, track) -> None:
        url = getattr(track, 'url', '')
        if url.startswith('http'):
            QDesktopServices.openUrl(QUrl(url))
        elif track is not None and track.cached:
            QDesktopServices.openUrl(QUrl.fromLocalFile(track.local_path))

    # ================= очередь =================
    def _on_item_added(self, item) -> None:
        self._model.add_item(item)
        self._update_queue_summary()

    def _on_item_status(self, item_id: str, status: str) -> None:
        self._model.update_status(item_id, status)
        self._update_queue_summary()

    def _on_item_finished(self, item_id: str, success: bool, message: str) -> None:
        self._model.set_finished(item_id, success, message)
        wanted_in_vk = item_id in self._auto_upload_ids
        self._auto_upload_ids.discard(item_id)
        if success:
            self._mark_library_dirty()
            # message при успехе — путь к готовому файлу
            if wanted_in_vk and message:
                self._uploader.add([message])
            # Файл лёг в папку с музыкой, и списки своих файлов о нём ещё не знают
            self._tracks_page.refresh_local_dirs()
            self._mark_tracks_dirty()
        self._update_queue_summary()
        self._update_queue_buttons()

    def _update_queue_summary(self) -> None:
        total = self._model.rowCount()
        active = self._model.active_count()
        self._table.setVisible(bool(total))
        self._queue_empty.setVisible(not total)
        # Три неактивные кнопки под пустой страницей ничего не дают
        self._queue_actions.setVisible(bool(total))
        if not total:
            # Про пустую очередь уже сказано заглушкой посреди страницы
            self._queue_summary.setText('')
        elif active:
            self._queue_summary.setText(f'В работе: {active} из {total}')
        else:
            self._queue_summary.setText(f'Всё готово · задач в списке: {total}')
        self._nav_counts[PAGE_QUEUE] = active
        self._update_nav_counters()
        self._clear_done_btn.setEnabled(bool(self._model.finished_rows()))

    def _selected_rows(self) -> list[int]:
        return [index.row() for index in self._table.selectionModel().selectedRows()]

    def _update_queue_buttons(self, *_args) -> None:
        rows = self._selected_rows()
        self._cancel_btn.setEnabled(any(not self._model.is_finished(r) for r in rows))
        self._open_btn.setEnabled(len(rows) == 1 and bool(self._model.item_at(rows[0]).path))

    def _cancel_selected(self) -> None:
        for row in self._selected_rows():
            self._manager.cancel(self._model.item_at(row).id)

    def _clear_finished(self) -> None:
        self._model.remove_rows(self._model.finished_rows())
        self._update_queue_summary()
        self._update_queue_buttons()

    def _open_finished_file(self, *_args) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        path = self._model.item_at(rows[0]).path
        if not path:
            return
        try:
            library.open_file(path)
        except OSError as exc:
            QMessageBox.warning(self, 'Открытие файла', f'Не удалось открыть файл:\n{exc}')

    # ================= заливка в музыку VK =================
    def _on_auto_vk_toggled(self, checked: bool) -> None:
        self._settings['auto_vk_upload'] = checked
        self._refresh_auto_vk_check()
        if checked and not self._vk_client:
            QMessageBox.information(
                self, 'Нужен вход в VK',
                'Скачанное будет ждать в очереди, пока не выполнен вход в аккаунт VK.\n\n'
                'Войти можно на вкладке «Музыка VK».')

    def _refresh_auto_vk_check(self) -> None:
        self._auto_vk_check.setToolTip(
            'Скачанное здесь само уедет в «Мою музыку» вашего аккаунта VK'
            if self._vk_client else
            'Скачанное будет ждать в очереди, пока не выполнен вход в VK')

    def _refresh_upload_chip(self) -> None:
        pending = self._uploader.pending
        self._upload_chip.setVisible(bool(pending))
        if pending:
            self._upload_chip.update_chip(f'В VK: {pending}', 'warn',
                                          'Столько файлов ждёт отправки в музыку VK')

    def _on_upload_progress(self, path: str, status: str) -> None:
        self._upload_chip.show()
        self._upload_chip.update_chip(f'В VK: {self._uploader.pending}', 'warn',
                                      f'{status}: {os.path.basename(path)}')

    def _on_uploaded(self, path: str, ok: bool, error: str) -> None:
        if ok:
            self._upload_done += 1
        else:
            self._upload_failures.append(f'{os.path.basename(path)}: {error}')
        self._refresh_upload_chip()
        if self._uploader.pending:
            return
        # Итог подводим, только когда очередь опустела: иначе на пачке файлов
        # окна лезли бы одно за другим
        done, failures = self._upload_done, self._upload_failures
        self._upload_done, self._upload_failures = 0, []
        if done:
            self._upload_chip.update_chip(f'В VK: готово ({done})', 'ok',
                                          'Файлы добавлены в «Мою музыку»')
            self._upload_chip.show()
            QTimer.singleShot(6000, self._refresh_upload_chip)
        if failures:
            QMessageBox.warning(
                self, 'Не всё уехало в VK',
                'В музыку VK не попали:\n\n' + '\n'.join(failures[:10])
                + ('' if len(failures) <= 10 else f'\n…и ещё {len(failures) - 10}'))

    # ================= добавление ссылок =================
    def _on_add_url(self) -> None:
        urls = url_detect.split_urls(self._url_edit.text())
        if not urls:
            return
        self._sync_settings_from_controls()
        # В поле может оказаться и несколько ссылок — например, вставленных через пробел
        if len(urls) > 1:
            self._url_edit.clear()
            self._start_batch(urls)
            return
        url = urls[0]

        link = url_detect.detect(url)
        if link.source == 'unknown':
            QMessageBox.warning(self, 'Ссылка не распознана',
                                'Это не похоже на ссылку YouTube или VK.')
            return

        self._url_edit.clear()
        self._set_add_busy(True)
        cookies_browser = self._settings.get('cookies_browser')

        def on_done(entries, error):
            self._set_add_busy(False)
            if error:
                QMessageBox.warning(self, 'Ошибка',
                                    f'Не удалось получить информацию по ссылке:\n{error}')
                return
            if not entries:
                QMessageBox.warning(self, 'Пусто', 'Не удалось найти видео по этой ссылке.')
                return
            self._handle_entries(link.source, entries)

        run_async(ytdlp_engine.extract_entries, on_done, url, cookies_browser)

    def _set_add_busy(self, busy: bool, text: str = '') -> None:
        self._add_btn.setEnabled(not busy)
        self._bulk_btn.setEnabled(not busy)
        self._add_btn.setText(text or ('Читаю ссылку…' if busy else 'Добавить'))

    # ---------- список ссылок ----------
    def _open_bulk_add(self) -> None:
        dlg = BulkAddDialog(self)
        if not dlg.exec():
            return
        urls = dlg.urls()
        if urls:
            self._sync_settings_from_controls()
            self._start_batch(urls)

    def _start_batch(self, urls: list[str]) -> None:
        """Пройти по списку ссылок по одной.

        Последовательно, а не всем скопом: yt-dlp по каждой ссылке ходит в сеть, и
        так видно, на какой из них он сейчас, а окно не подвисает на весь список."""
        self._batch = {'urls': urls, 'index': 0, 'queued': 0, 'skipped': 0, 'failed': []}
        self._batch_step()

    def _batch_step(self) -> None:
        batch = self._batch
        if batch is None:
            return
        index = batch['index']
        urls = batch['urls']
        if index >= len(urls):
            self._finish_batch()
            return

        url = urls[index]
        self._set_add_busy(True, f'Читаю {index + 1} из {len(urls)}…')
        link = url_detect.detect(url)
        if link.source == 'unknown':
            batch['failed'].append((url, 'не похоже на ссылку YouTube или VK'))
            batch['index'] += 1
            QTimer.singleShot(0, self._batch_step)
            return

        def on_done(entries, error):
            if self._batch is not batch:
                return  # список успели отменить или начать новый
            if error:
                batch['failed'].append((url, str(error)))
            elif not entries:
                batch['failed'].append((url, 'по ссылке ничего не нашлось'))
            else:
                queued, skipped = self._handle_entries(link.source, entries, batch=True)
                batch['queued'] += queued
                batch['skipped'] += skipped
            batch['index'] += 1
            self._batch_step()

        run_async(ytdlp_engine.extract_entries, on_done, url,
                  self._settings.get('cookies_browser'))

    def _finish_batch(self) -> None:
        batch, self._batch = self._batch, None
        self._set_add_busy(False)
        self._select_page(PAGE_QUEUE)

        parts = [f'Добавлено в очередь: {batch["queued"]}']
        if batch['skipped']:
            parts.append(f'Пропущено, уже скачано: {batch["skipped"]}')
        failed = batch['failed']
        if failed:
            lines = [f'{_short_url(url)}: {reason}' for url, reason in failed[:8]]
            if len(failed) > 8:
                lines.append(f'…и ещё {len(failed) - 8}')
            parts.append('Не получилось:\n' + '\n'.join(lines))
        QMessageBox.information(self, 'Список ссылок', '\n\n'.join(parts))

    # ---------- просмотр до скачивания ----------
    def _update_preview_btn(self, text: str = '') -> None:
        self._preview_btn.setEnabled(can_preview(text.strip()))

    def _on_preview_url(self) -> None:
        """Открыть видео по ссылке во встроенном плеере сайта, ничего не качая."""
        url = self._url_edit.text().strip()
        source = url_detect.detect(url).source
        # on_download повторяет обычное добавление: ссылка в поле никуда не делась
        if not open_preview(self, source, {'url': url},
                                        on_download=self._on_add_url):
            QMessageBox.information(self, 'Просмотр',
                                    'По этой ссылке смотреть нечего, только скачивать.')

    def _preview_entry(self, source: str, entry: dict) -> None:
        if not open_preview(self, source, entry):
            QMessageBox.information(self, 'Просмотр',
                                    'Для этой записи нет ссылки на просмотр.')

    def _handle_entries(self, source: str, entries: list[dict],
                        batch: bool = False) -> tuple[int, int]:
        """Поставить записи в очередь. Отдаёт «сколько добавлено, сколько пропущено».

        В режиме списка ссылок итог показывает не каждая ссылка, а `_finish_batch`."""
        selected = entries
        if len(entries) > 1:
            dlg = PlaylistPickDialog(f'Плейлист · {len(entries)} элементов', entries, self,
                                     history_key=lambda entry: self._history_key(source, entry),
                                     preview=lambda entry: self._preview_entry(source, entry))
            if not dlg.exec():
                return 0, 0
            selected = dlg.selected_entries()

        queued, skipped = self._queue_entries(source, selected)
        if not batch:
            self._select_page(PAGE_QUEUE)
            if skipped:
                self._notify_skipped(skipped)
        return queued, skipped

    def _queue_entries(self, source: str, entries: list[dict]) -> tuple[int, int]:
        queued = sum(1 for entry in entries if self._queue_entry(source, entry))
        return queued, len(entries) - queued

    def _history_key(self, source: str, entry: dict) -> str:
        """Ключ истории с учётом режима: музыка и видео одного ролика — разные загрузки."""
        return history.key_for(source, entry.get('id'), self._settings.get('mode', 'audio'))

    def _queue_entry(self, source: str, entry: dict) -> bool:
        key = self._history_key(source, entry)
        if self._settings.get('skip_downloaded', True) and history.is_downloaded(key):
            return False
        title = entry.get('title') or entry.get('id') or entry.get('url')
        if source == 'youtube':
            item = self._manager.add_youtube(entry['url'], title, key)
        else:
            item = self._manager.add_vk_video(entry['url'], title, key)
        # Видео в музыку VK не положишь — только то, что качается как аудио
        if self._auto_vk_check.isChecked() and self._settings.get('mode') == 'audio':
            self._auto_upload_ids.add(item.id)
        return True

    def _queue_vk_tracks(self, tracks: list[dict]) -> None:
        skip = self._settings.get('skip_downloaded', True)
        skipped = 0
        for track in tracks:
            key = track_key(track)
            if skip and history.is_downloaded(key):
                skipped += 1
                continue
            self._manager.add_vk_track(track, key)
        self._select_page(PAGE_QUEUE)
        if skipped:
            self._notify_skipped(skipped)

    def _notify_skipped(self, count: int) -> None:
        QMessageBox.information(
            self, 'Уже скачано',
            f'Пропущено записей: {count}, они уже скачивались раньше.\n\n'
            'Отключить пропуск можно в настройках, на вкладке «Загрузки».')

    # ================= настройки =================
    def _sync_settings_from_controls(self) -> None:
        self._settings['mode'] = self._mode_combo.currentData()
        self._settings['video_quality'] = self._quality_combo.currentData()
        self._settings['audio_format'] = self._format_combo.currentData()
        self._settings['audio_bitrate'] = self._bitrate_combo.currentData()
        self._settings['auto_vk_upload'] = self._auto_vk_check.isChecked()

    def _apply_settings_to_controls(self) -> None:
        s = self._settings
        self._set_combo_value(self._mode_combo, s.get('mode', 'audio'))
        self._set_combo_value(self._quality_combo, s.get('video_quality', 'best'))
        self._set_combo_value(self._format_combo, s.get('audio_format', 'mp3'))
        self._set_combo_value(self._bitrate_combo, s.get('audio_bitrate', '192'))
        # Без блокировки сигнала галочка при запуске сама показала бы окно «нужен вход»
        self._auto_vk_check.blockSignals(True)
        self._auto_vk_check.setChecked(bool(s.get('auto_vk_upload', False)))
        self._auto_vk_check.blockSignals(False)
        self._refresh_auto_vk_check()

    @staticmethod
    def _set_combo_value(combo: QComboBox, value) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _on_mode_changed(self) -> None:
        is_video = self._mode_combo.currentData() == 'video'
        self._opts_stack.setCurrentIndex(OPTS_VIDEO if is_video else OPTS_AUDIO)
        # Видео в музыку VK не заливается — галочке в этом режиме делать нечего
        self._auto_vk_check.setVisible(not is_video)
        self._settings['mode'] = self._mode_combo.currentData()

    def _open_settings(self, start_tab: str = '') -> None:
        self._sync_settings_from_controls()
        dlg = SettingsDialog(self._settings, bool(self._vk_client), self, start_tab)

        def do_login():
            login_dlg = VkWebLoginDialog(dlg)
            login_dlg.logged_in.connect(
                lambda data: (self._on_vk_logged_in(data), dlg.set_vk_status(True)))
            login_dlg.exec()

        def do_logout():
            if self._confirm_vk_logout():
                dlg.set_vk_status(False)

        dlg.login_button.clicked.connect(do_login)
        dlg.logout_button.clicked.connect(do_logout)
        if dlg.exec():
            old_dirs = (self._settings.get('music_dir'), self._settings.get('video_dir'))
            old_local = list(self._settings.get('local_dirs') or [])
            self._settings = dlg.result_settings()
            config.save_settings(self._settings)
            self._manager.set_concurrency(self._settings.get('concurrency', 3))
            # Браузер для кук могли сменить — предпросмотру нужны куки уже нового
            preload_preview_cookies(self._settings.get('cookies_browser'))
            proxy.apply(self._settings)
            # Открытые соединения обложек ведут через прежний прокси. Адрес
            # посредника при этом не меняется (порт держит Chromium), так что
            # сама сессия расхождения не заметит — рвём её здесь
            covers.reset_session()
            self._detect_proxy()
            self._apply_settings_to_controls()
            self._apply_music_settings()
            if old_dirs != (self._settings.get('music_dir'), self._settings.get('video_dir')):
                self._mark_library_dirty()
            if old_local != list(self._settings.get('local_dirs') or []):
                # Папку со своей музыкой добавили в настройках — читаем её сразу,
                # иначе список папок был бы, а треков из них не было
                self._tracks_page.refresh_local_dirs()
            self._mark_tracks_dirty()
        self._refresh_status_chips()
        self._refresh_auto_vk_check()

    # ================= сеть =================
    def _detect_proxy(self) -> None:
        """Поиск локального VPN-клиента в фоне: он ходит в сеть, а на старте и при
        сохранении настроек интерфейс замирать не должен."""
        if not proxy.needs_detect():
            return

        def on_done(url, error):
            if error:
                logger.debug('Поиск прокси не удался: %s', error)
                return
            proxy.set_detected(url)
            covers.reset_session()

        run_async(proxy.detect_local, on_done)

    # ================= VK =================
    def _on_vk_chip_clicked(self) -> None:
        """Нажатие на значок VK в шапке — единственный вход в аккаунт из окна.

        Что делать, зависит от того, что значок показывает: связи нет — пробуем
        подключиться сохранённым входом, вход не выполнен или сессия сайта
        потерялась — открываем окно входа, всё хорошо — показываем настройки
        аккаунта, где живут «Сменить аккаунт» и «Выйти»."""
        if self._vk_offline:
            self._try_auto_vk_login()
        elif self._vk_client and self._vk_client.has_web_session:
            self._open_settings(TAB_ACCOUNTS)
        else:
            self._open_vk_login()

    def _open_vk_login(self) -> None:
        dlg = VkWebLoginDialog(self)
        dlg.logged_in.connect(self._on_vk_logged_in)
        dlg.exec()

    def _on_vk_logged_in(self, token_data: dict) -> None:
        logger.debug('_on_vk_logged_in: получен токен из диалога логина')
        config.save_vk_token(token_data)
        self._connect_vk_client(token_data['access_token'])

    def _connect_vk_client(self, token: str) -> None:
        # Два клиента на один аккаунт дерутся за общий файл кук: тот, кто сохранился
        # вторым, затирает чужую сессию, и VK начинает гонять запросы по кругу
        # login.php ↔ index.php. Пути к этому месту два (окно входа и настройки), плюс
        # повторный сигнал от самого окна, поэтому сторожим здесь — в общей точке
        if self._vk_connecting:
            logger.debug('_connect_vk_client: подключение уже идёт, повтор пропущен')
            return
        self._vk_connecting = True
        logger.debug('_connect_vk_client: запускаю VkClient(token) в фоне')
        self._vk_panel.set_connecting()

        def on_done(client, error):
            self._vk_connecting = False
            logger.debug('_connect_vk_client.on_done: error=%r client=%r', error, client)
            if error or client is None:
                self._vk_client = None
                self._manager.vk_client = None
                self._uploader.set_client(None)
                if isinstance(error, vk_client_mod.VkAccountBlocked):
                    # Ни стирать вход, ни заводить таймер: и то и другое обещало бы
                    # починку, которой не будет. Токен цел — он пригодится, когда
                    # блокировку снимут, и заставлять входить заново незачем
                    self._vk_offline = False
                    self._cancel_vk_retry()
                    self._on_vk_account_blocked(str(error))
                    self._refresh_auto_vk_check()
                    return
                self._vk_offline = not getattr(error, 'token_rejected', False)
                # Сохранённый вход стираем, только если VK сам его отверг. Раньше это
                # делалось при любой ошибке, и обрыв связи требовал полного входа заново.
                if getattr(error, 'token_rejected', False):
                    # Повторять нечем: нужен новый вход руками
                    self._cancel_vk_retry()
                    config.clear_vk_token()
                    self._vk_panel.set_logged_out()
                    QMessageBox.warning(self, 'VK', f'Вход в VK больше не действует:\n{error}')
                else:
                    self._vk_panel.set_connect_failed(str(error), self._schedule_vk_retry())
                self._refresh_status_chips()
                self._refresh_auto_vk_check()
                return
            self._cancel_vk_retry()
            # Подключились — значит, VK пускает: прежняя отметка о блокировке устарела
            config.clear_vk_blocked()
            self._vk_client = client
            self._vk_offline = False
            # Задачам он нужен, чтобы получить прямую ссылку на трек перед скачиванием
            self._manager.vk_client = client
            self._uploader.set_client(client)
            self._vk_panel.set_client(client)
            # Вошли руками — прежние неудачи фонового перезахода больше ни о чём не
            # говорят: и счётчик keeper'а, и назначенная им починка с её паузой
            self._vk_keeper.reset()
            self._vk_session_retry.stop()
            self._vk_session_delay = 0
            # Сторожить есть кого: дальше сессию проверяем сами, не дожидаясь сбоя
            self._vk_session_watch.start()
            # И проверяем сразу же. Токен API и сессия сайта живут порознь: клиент
            # поднимется на живом токене, даже если куки давно протухли, — и без этой
            # проверки человек узнал бы о мёртвой сессии сам, открыв вкладку VK.
            # Ровно тот случай, ради которого перезаход и затевался
            self._check_vk_session()
            self._refresh_status_chips()
            self._refresh_auto_vk_check()
            self._on_vk_availability_changed()

        run_async(vk_client_mod.VkClient, on_done, token, self._settings.get('cookies_browser'))

    def _confirm_vk_logout(self) -> bool:
        """Спросить и выйти. Возвращает, вышли ли на самом деле: вызывающему
        (кнопка «Выйти» в настройках) нужно знать, обновлять ли свой вид."""
        answer = QMessageBox.question(
            self, 'Выход из VK',
            'Выйти из аккаунта VK?\n\nСписки музыки и плейлистов очистятся, '
            'скачанные файлы останутся на месте.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return False
        self._vk_logout()
        return True

    def _vk_logout(self) -> None:
        self._cancel_vk_retry()
        # Сторожить больше некого. Гасим здесь, а не в _cancel_vk_retry: тот зовётся
        # и после удачного входа, где сторож как раз и нужен
        self._vk_session_watch.stop()
        config.clear_vk_token()
        # Иначе встроенный браузер остаётся залогиненным и «выход» получается только на вид
        clear_saved_login()
        self._vk_client = None
        self._vk_offline = False
        self._manager.vk_client = None
        self._uploader.set_client(None)
        self._vk_panel.set_logged_out()
        self._refresh_status_chips()
        self._refresh_auto_vk_check()
        self._on_vk_availability_changed()

    # ---------- сторож веб-сессии ----------
    def _vk_user_id(self):
        """Чью сессию проверяем. None, когда в VK ещё не вошли."""
        return None if self._vk_client is None else self._vk_client.user_id

    def _check_vk_session(self) -> None:
        """Раз в час: жива ли ещё сессия сайта? Если нет — чиним, не дожидаясь человека.

        Проверка сетевая, поэтому в фоне: окно не должно замирать ради неё. Пока
        keeper и без нас занят перезаходом, лезть незачем — только помешаем."""
        if self._vk_client is None or self._vk_keeper.running:
            return
        if self._vk_session_retry.isActive():
            # Починка уже назначена и ждёт своей паузы. Проверять сейчас — значит
            # добавить к её попыткам собственный запрос за треками: именно из такой
            # пары «сторож плюс перезаход» и складывался поток обращений, на который
            # VK ответил блокировкой. Ответ мы и так знаем: сессия мертва
            return
        run_async(vk_client_mod.check_web_session, self._on_vk_session_checked,
                  self._vk_client.user_id)

    def _on_vk_session_checked(self, alive, error) -> None:
        """Ответ сторожа. Сессия мертва — начинаем перезаход молча, панель не трогаем.

        Панель здесь намеренно оставляем как есть: человек может смотреть совсем
        другую вкладку, и «обновляю вход» поверх неё — то самое мельтешение, которого
        просили избежать. Если он всё-таки откроет VK во время починки, `set_client`
        покажет ожидание сам."""
        if isinstance(error, vk_client_mod.VkAccountBlocked):
            self._on_vk_account_blocked(str(error))
            return
        if error is not None:
            # Нет сети — не повод считать сессию мёртвой: проверим в следующий раз
            logger.debug('VK: сторож не смог проверить сессию (%s)', error)
            return
        if alive:
            self._refresh_vk_session()
            return
        logger.info('VK: сторож заметил протухшую сессию, чиню заранее')
        self._restore_vk_session()

    def _refresh_vk_session(self) -> None:
        """Раз в сутки заглянуть на VK встроенным браузером, пока сессия ещё жива.

        До сих пор браузер открывался только на похоронах: сессия умерла — идём
        перезаходить. Но умирает она как раз потому, что между входами профилем никто
        не пользуется: приложение ходит на VK через `requests`, а куки сайта VK
        продлевает только сам сайт и только живому браузеру. Отсюда и повторяющееся
        «вход слетел» на аккаунте, куда никто не переставал заходить.

        Заход делается ровно здесь — сразу после того, как сторож подтвердил живую
        сессию. Дороже он не стоит: одна страница раз в сутки против такой же
        страницы при каждой поломке, которых становится меньше. И неудача тут ничего
        не значит — чинить нечего, keeper промолчит."""
        if not config.vk_refresh_due():
            return
        if self._vk_keeper.refresh():
            # Метку ставим по факту начала, а не успеха: смысл её — не дать заходам
            # повторяться чаще раза в сутки. Если бы её ставил только успех, заход,
            # спотыкающийся о недоступный VK, повторялся бы каждые десять минут
            config.save_vk_refresh()
            logger.info('VK: профилактический заход браузером (раз в сутки)')

    # ---------- тихий перезаход по веб-сессии ----------
    def _restore_vk_session(self) -> None:
        """Сессия сайта VK истекла — пробуем вернуть её сами, не спрашивая пароль.

        Панель показывает «обновляю вход» без кнопки: пока автоматика работает,
        нажимать нечего. Кнопку она покажет сама, когда мы скажем, что не вышло."""
        if self._vk_keeper.running:
            # Панель шлёт сигнал из каждого упавшего загрузчика (треки и плейлисты
            # грузятся параллельно), поэтому на один обрыв их приходит два. Заход уже
            # идёт — второй сигнал не повод ни начинать новый, ни ставить лишний таймер
            return
        if not self._vk_keeper.try_restore():
            # Попытку не начали — рано, занят профиль или исчерпаны подряд идущие
            # неудачи. Всё это лечится ожиданием, поэтому заходим позже, а не сдаёмся:
            # keeper молчит до успеха или сброса, и без таймера человек остался бы
            # с «обновляю вход» навсегда
            self._schedule_vk_session_retry()

    def _on_vk_session_restored(self) -> None:
        """Получилось молча. Дальше всё то же, что после обычного входа, кроме пароля.

        Свежие куки keeper записал в файл, но живой клиент об этом не знает — у него в
        сессии лежат прежние. Поэтому сперва `reload_web_session`, и лишь потом панель:
        она смотрит на `has_web_session` и без перечитывания показала бы заглушку
        «вход выполнен не до конца» поверх только что восстановленного доступа."""
        logger.info('VK: веб-сессия восстановлена в фоне')
        if self._vk_client is None or not self._vk_client.reload_web_session():
            # Файл записан, а сессии в нём нет — редкость, но обещать успех не за что
            self._on_vk_session_lost('свежие куки не подошли клиенту')
            return
        self._vk_session_retry.stop()
        self._vk_session_delay = 0
        self._vk_panel.set_client(self._vk_client)
        self._refresh_status_chips()
        self._on_vk_availability_changed()

    def _on_vk_session_lost(self, reason: str) -> None:
        """Не получилось с первого раза — но сдаваться ещё рано.

        Причины неудачи почти всегда временные: не было сети, VK ответил медленнее
        таймаута, профиль был занят окном входа. Поэтому пробуем снова по таймеру, а
        человека тревожим только один раз — когда паузы дорастут до предельной и
        станет ясно, что само уже не починится. Окно входа не открываем никогда:
        всплывающее поверх работы окно с паролем — это ровно то, чего просили избежать."""
        logger.info('VK: тихо вернуть сессию не удалось (%s)', reason)
        delay = self._schedule_vk_session_retry()
        if delay < VK_SESSION_RETRY_MAX:
            return
        # Дошли до предельной паузы: автоматика продолжит пробовать, но обещать, что
        # обойдётся без пароля, уже нечестно — показываем кнопку
        self._vk_panel.show_session_lost()
        self._notify('Не удалось обновить вход в VK, войдите заново на вкладке «Музыка VK»')

    def _on_vk_account_blocked(self, reason: str, *, remember: bool = True) -> None:
        """VK заблокировал аккаунт. Единственный случай, когда мы перестаём пробовать.

        Всё остальное в этом файле построено на «причина временная, повторим позже», и
        для блокировки это ровно неверно: сколько ни заходи, VK не пустит, пока человек
        не снимет блокировку на сайте. Поэтому гасим оба таймера и говорим прямо —
        молчаливые попытки по кругу выглядели как «программа сломалась и ничего не
        делает», хотя она делала, просто бесполезное.

        `remember=False` — когда мы не узнали о блокировке, а лишь прочитали свою же
        отметку. Записывать в этом случае нечего, и разделение здесь не про лишний
        вызов: без него источником правды становилась запись, сделанная с её же слов."""
        logger.warning('VK: аккаунт заблокирован, автоматические попытки остановлены')
        if remember:
            # Запоминаем на диск: без этого знание жило до закрытия программы, и каждый
            # следующий запуск начинал с нуля — полный залп запросов по аккаунту, который
            # VK уже пометил. Ровно это и не давало блокировке сняться
            # Клиента к этому моменту уже нет, поэтому чей это аккаунт — смотрим в
            # сохранённом входе: пригодится, чтобы не спутать отметку с чужой
            token_data = config.load_vk_token() or {}
            config.save_vk_blocked(self._vk_user_id() or token_data.get('user_id'))
        self._vk_session_retry.stop()
        self._vk_session_delay = 0
        # И почасового сторожа тоже: он бы завёл цикл заново со следующим тиком
        self._vk_session_watch.stop()
        self._vk_panel.show_account_blocked(reason)
        self._vk_chip.update_chip('VK заблокировал аккаунт', 'warn',
                                  'Музыка недоступна, пока VK не снимет блокировку')
        self._notify('VK заблокировал аккаунт — откройте vk.com в браузере')

    def _retry_after_unblock(self) -> None:
        """«Блокировка снята, повторить» — единственный путь обратно к VK.

        Отметку стираем до попытки: иначе `_try_auto_vk_login` увидел бы её и снова
        отказался идти. Если VK всё ещё не пускает, подключение упрётся в ту же
        проверку и отметка вернётся на место — но уже ценой одного запроса, а не
        целого залпа при каждом запуске."""
        logger.info('VK: человек сообщил о снятии блокировки, пробую подключиться')
        config.clear_vk_blocked()
        self._try_auto_vk_login(forced=True)

    def _retry_vk_session(self) -> None:
        """Очередная тихая попытка вернуть сессию сайта.

        Счётчик неудач keeper'а здесь не сбрасывается — и это главное. Раньше сброс
        стоял прямо перед попыткой «чтобы таймер не тикал вхолостую», и ценой оказался
        весь антишторм: keeper никогда не доходил ни до своих трёх неудач подряд, ни до
        получаса тишины, а окно приводило его к VK каждые пять минут. Считать, когда
        пора остановиться, — работа keeper'а; наше дело лишь предлагать попытку, а
        отказ принимать как ответ и приходить позже."""
        if not self._vk_keeper.try_restore():
            # Отказ — норма: рано, занят профиль или keeper выдерживает паузу после
            # серии неудач. Всё это лечится ожиданием, поэтому просто заходим позже
            self._schedule_vk_session_retry()

    def _schedule_vk_session_retry(self) -> int:
        """Завести таймер следующей тихой попытки и сказать, через сколько она будет."""
        self._vk_session_delay = (min(self._vk_session_delay * 2, VK_SESSION_RETRY_MAX)
                                  if self._vk_session_delay else VK_SESSION_RETRY_FIRST)
        self._vk_session_retry.start(self._vk_session_delay * 1000)
        logger.info('VK: следующая попытка вернуть сессию через %d с', self._vk_session_delay)
        return self._vk_session_delay

    def _on_vk_availability_changed(self) -> None:
        """Вход в VK появился или пропал: отложенные списки микса устарели."""
        self._mixer.forget()
        self._mix_page.refresh_sources()

    def _try_auto_vk_login(self, forced: bool = False) -> None:
        """Подключение сохранённым токеном — при запуске, по кнопке «Повторить сейчас»
        в панели VK и по таймеру после обрыва связи.

        `forced` ставит только кнопка «Повторить попытку»: это единственный случай,
        когда в заблокированный аккаунт стучаться уместно — человек сам говорит, что
        снял блокировку."""
        # Иначе ручное нажатие и сработавший таймер полезли бы в VK вдвоём
        self._vk_retry.stop()
        if not forced and config.load_vk_blocked() is not None:
            if not self._vk_blocked_rechecked:
                # Первая попытка за запуск. Отсчёт суток здесь не помогал: программу
                # закрывают на ночь, время идёт, а отметка не стареет ни на секунду —
                # у того, кто закрывает вечером и открывает утром, срок не выходил
                # никогда, и живой аккаунт объявлялся заблокированным по памяти.
                # Хуже того, отказ шёл через `_on_vk_account_blocked`, а тот заново
                # звал `save_vk_blocked` — состояние подтверждало само себя, ни разу
                # не спросив VK. Спрашиваем. Один запрос на запуск потоком не выглядит
                self._vk_blocked_rechecked = True
                logger.info('VK: есть отметка о блокировке, проверяю её настоящим запросом')
            elif config.vk_blocked_expired():
                # Отметке больше суток. Блокировки VK почти всегда снимаются на сайте,
                # и узнать об этом можно только попыткой: без неё программа молчала бы
                # про «заблокирован» на аккаунте, который VK давно пустил обратно.
                # Отметку не стираем — это сделает удавшееся подключение
                # (`_on_vk_client_ready`); не вышло — она останется, и следующая
                # проверка будет только через сутки
                logger.info('VK: отметке о блокировке больше суток, пробую ещё раз')
            else:
                # Про блокировку известно с прошлого раза. Молча не лезем: запросы по
                # такому аккаунту ничего не вернут, а VK видит очередной поток обращений
                # и держит блокировку дальше. Ждём человека — он снимет её на сайте и
                # нажмёт «Повторить попытку»
                logger.info('VK: аккаунт помечен заблокированным, автоподключение пропущено')
                self._on_vk_account_blocked(vk_client_mod.BLOCKED_MESSAGE, remember=False)
                return
        token_data = config.load_vk_token()
        if token_data and token_data.get('access_token'):
            self._connect_vk_client(token_data['access_token'])
        else:
            self._cancel_vk_retry()
            self._vk_panel.set_logged_out()

    def _schedule_vk_retry(self) -> int:
        """Завести таймер следующей попытки и сказать, через сколько секунд она будет."""
        self._vk_retry_delay = (min(self._vk_retry_delay * 2, VK_RETRY_MAX)
                                if self._vk_retry_delay else VK_RETRY_FIRST)
        self._vk_retry.start(self._vk_retry_delay * 1000)
        logger.info('VK: связи нет, повтор через %d с', self._vk_retry_delay)
        return self._vk_retry_delay

    def _cancel_vk_retry(self) -> None:
        """Повторять больше нечего: получилось, вышли или токен отвергнут."""
        self._vk_retry.stop()
        self._vk_retry_delay = 0
        # Тихий перезаход тоже отменяем: без токена возвращать сессию сайта незачем,
        # а после выхода из аккаунта — просто вредно
        self._vk_session_retry.stop()
        self._vk_session_delay = 0

    # ================= значок, клавиши, мост =================
    def _setup_tray(self) -> None:
        if not self._settings.get('tray_enabled', True):
            return
        tray = TrayIcon(self._player, self)
        if not tray.available:
            logger.info('Значок у часов недоступен в этой системе')
            return
        tray.toggle_window.connect(self._toggle_window)
        tray.play_pause.connect(self._player.toggle)
        tray.next_track.connect(self._player.next)
        tray.prev_track.connect(self._player.previous)
        tray.favorite.connect(self._favorite_current)
        tray.dislike.connect(self._dislike_playing)
        tray.add_to_vk.connect(self._add_current_to_vk)
        tray.shuffle.connect(self._player.toggle_shuffle)
        tray.repeat.connect(self._player.cycle_repeat)
        tray.mini_player.connect(self._toggle_mini_player)
        tray.quit_requested.connect(self._quit_from_tray)
        tray.set_notifications(self._settings.get('tray_notifications', True))
        tray.show()
        self._tray = tray

    def _apply_music_settings(self) -> None:
        """Перенастроить плеер, значок, клавиши и мост по сохранённым настройкам."""
        self._player.apply_settings(self._settings)

        wanted = bool(self._settings.get('tray_enabled', True))
        if wanted and self._tray is None:
            self._setup_tray()
        elif not wanted and self._tray is not None:
            self._tray.hide()
            self._tray = None
        elif self._tray is not None:
            self._tray.set_notifications(self._settings.get('tray_notifications', True))

        # Сочетания перерегистрировать дёшево, поэтому просто заводим заново
        self._hotkeys.stop()
        self._setup_hotkeys()

        # А мост дёргать зря не стоит: только что закрытый порт система какое-то время
        # держит занятым, и после перезапуска он уехал бы на соседний
        port = int(self._settings.get('bridge_port') or 48211)
        if not self._settings.get('bridge_enabled', True):
            self._bridge.stop()
        elif not self._bridge.running:
            self._setup_bridge()
        elif self._bridge.port != port:
            self._bridge.stop()
            self._setup_bridge()

    def _setup_hotkeys(self) -> None:
        if not self._settings.get('hotkeys_enabled', True):
            return
        bindings = {
            'play_pause': self._settings.get('hotkey_play_pause', ''),
            'next': self._settings.get('hotkey_next', ''),
            'prev': self._settings.get('hotkey_prev', ''),
            'add_vk': self._settings.get('hotkey_add_vk', ''),
            'favorite': self._settings.get('hotkey_favorite', ''),
            'show': self._settings.get('hotkey_show', ''),
        }
        if self._settings.get('hotkeys_media_keys', True):
            # Клавиши на клавиатуре отдельными действиями: их может не быть,
            # и отказ в регистрации не должен ронять остальные сочетания
            bindings.update({f'media_{name}': combo
                             for name, combo in MEDIA_KEYS.items()})
        if self._hotkeys.start(bindings) and self._hotkeys.failed:
            logger.info('Заняты другими программами: %s', ', '.join(self._hotkeys.failed))

    def _setup_bridge(self) -> None:
        """Мост слушает 127.0.0.1 и потому идёт мимо прокси приложения."""
        if not self._settings.get('bridge_enabled', True):
            return
        token = self._settings.get('bridge_token') or bridge_mod.new_token()
        port = self._bridge.start(int(self._settings.get('bridge_port') or 48211), token)
        if not port:
            return
        self._settings['bridge_token'] = token
        self._settings['bridge_port'] = port
        config.save_settings(self._settings)
        self._push_bridge_status()

    def _push_bridge_status(self, *_args) -> None:
        if not self._bridge.running:
            return
        track = self._player.current
        self._bridge.set_status({
            'playing': self._player.playing,
            'track': None if track is None else {
                'title': track.title, 'artist': track.artist,
                'source': track.source, 'uid': track.uid,
            },
        })

    def _on_bridge_command(self, action: str, payload: dict) -> None:
        """Команда из расширения. Ссылку мост уже проверил, здесь только действие."""
        url = payload.get('url', '')
        title = payload.get('title', '')
        if action == 'open':
            self._show_window()
            self._open_link_in_app(url)
            return
        if url_detect.detect(url).source != 'youtube':
            # Играть умеем то, что понимает музыкальный раздел; прочее — в загрузки
            self._show_window()
            self._open_link_in_app(url)
            self._status_message('Ссылку положил в «Загрузки»: играть её нечем')
            return
        self._status_message('Разбираю ссылку из браузера…')

        def work():
            return ytdlp_engine.extract_entries(url, self._settings.get('cookies_browser'))

        def on_done(entries, error):
            if error or not entries:
                self._status_message('Не удалось разобрать ссылку из браузера')
                return
            track = from_youtube(entries[0])
            if not track.title and title:
                track.title = title
            if action == 'play':
                self._play_tracks([track], 0)
                self._show_window()
            elif action == 'enqueue':
                self._enqueue_tracks([track])
                self._notify(f'В очередь: {track.display_title}')
            elif action == 'add_to_vk':
                self._import_tracks_to_vk([track])

        run_async(work, on_done)

    def _open_link_in_app(self, url: str) -> None:
        self._select_page(PAGE_QUEUE)
        self._url_edit.setText(url)
        self._url_edit.setFocus()

    def _notify(self, text: str) -> None:
        """Сообщение человеку: в строке состояния, а при спрятанном окне — у часов."""
        self._status_message(text)
        if self._tray is not None and not self.isVisible():
            self._tray.notify(text)

    # ---------- действия над текущим треком ----------
    def _favorite_current(self) -> None:
        track = self._player.current
        if track is not None:
            self._toggle_favorites([track])
            self._notify('В избранном' if self._store.is_favorite(track.uid)
                         else 'Убрано из избранного')

    def _add_current_to_vk(self) -> None:
        track = self._player.current
        if track is not None:
            self._import_tracks_to_vk([track])

    def _on_hotkey(self, action: str) -> None:
        actions = {
            'play_pause': self._player.toggle, 'media_play_pause': self._player.toggle,
            'next': self._player.next, 'media_next': self._player.next,
            'prev': self._player.previous, 'media_prev': self._player.previous,
            'favorite': self._favorite_current,
            'add_vk': self._add_current_to_vk,
            'show': self._toggle_window,
        }
        handler = actions.get(action)
        if handler is not None:
            handler()

    # ---------- окно и выход ----------
    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._show_window()

    # ---------- мини-плеер ----------
    def _toggle_mini_player(self) -> None:
        """Маленькое окно вместо большого.

        Это второй вид на тот же плеер: ничего не пересоздаётся и не
        перезапускается, поэтому звук и позиция при переключении не сбиваются."""
        if self._mini is not None:
            self._close_mini_player()
            return
        mini = MiniPlayer(self._player)
        mini.favorite_toggled.connect(lambda track: self._toggle_favorites([track]))
        mini.hide_requested.connect(self._dislike_playing)
        mini.add_to_vk_requested.connect(lambda track: self._import_tracks_to_vk([track]))
        mini.restore_requested.connect(self._close_mini_player)
        mini.closed.connect(self._on_mini_closed)
        current = self._player.current
        if current is not None:
            mini.set_favorite(self._store.is_favorite(current.uid))
            mini.set_vk_state(current.uid, self._vk_states.get(current.uid, ''))
        self._mini = mini
        mini.show()
        self.hide()

    def _close_mini_player(self) -> None:
        """Вернуться в главное окно по кнопке — крестик тут ни при чём."""
        mini, self._mini = self._mini, None
        if mini is not None:
            mini.closed.disconnect()
            mini.close()
            mini.deleteLater()
        self._show_window()

    def _on_mini_closed(self) -> None:
        """Крестик мини-плеера возвращает большое окно, а не выходит из программы."""
        self._mini = None
        self._show_window()

    def _quit_from_tray(self) -> None:
        self._quitting = True
        self.close()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.WindowStateChange:
            if self._title_bar is not None:
                # Развернуть можно и системным сочетанием: кнопка должна знать
                self._title_bar.set_maximized(self.isMaximized())
            if (self.isMinimized() and self._tray is not None
                    and self._settings.get('minimize_to_tray', False)):
                # hide() прямо в обработчике Qt не любит, прячем следующим тактом
                QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if (not self._quitting and self._tray is not None
                and self._settings.get('close_to_tray', False)):
            # Крестик прячет окно, только если человек сам это включил
            event.ignore()
            self.hide()
            self._tray.notify('Приложение свернулось к часам и продолжает играть')
            return
        if self._mini is not None:
            self._mini.closed.disconnect()
            self._mini.close()
            self._mini = None
        if self._artist_view is not None:
            self._artist_view.close()
            self._artist_view = None
        self._hotkeys.stop()
        self._bridge.stop()
        if self._tray is not None:
            self._tray.hide()
        self._cancel_vk_retry()
        self._manager.cancel_all()
        self._player.save_state()
        self._player.shutdown()
        self._sync_settings_from_controls()
        self._settings['volume'] = self._player.volume
        config.save_settings(self._settings)
        store_mod.close()
        super().closeEvent(event)
        # Программа живёт, пока открыт мини-плеер или спрятано окно, поэтому
        # закрытие последнего окна её не завершает — выходим отсюда явно
        QApplication.instance().quit()
