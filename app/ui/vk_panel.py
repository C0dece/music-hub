import logging
import re

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QStackedWidget, QTabBar, QToolButton, QVBoxLayout,
    QWidget,
)

from ..core import history
from ..core.async_task import run_async
from ..core.track import Track, from_vk, to_vk_row
from ..core.vk_client import VkSessionExpired
from . import covers
from .flow_layout import FlowRow
from .playlist_pick_dialog import PlaylistPickDialog
from .track_list import TrackListWidget
from .widgets import (
    CheckableListWidget, ElidedLabel, EmptyState, PagesStack,
)

logger = logging.getLogger(__name__)

TAB_TRACKS, TAB_WAVE, TAB_SEARCH, TAB_PLAYLISTS = 0, 1, 2, 3
# Последняя страница стека - заглушка вместо списков: когда показывать нечего,
# там написано почему и лежит кнопка, которая это чинит
PAGE_TRACKS, PAGE_WAVE, PAGE_SEARCH, PAGE_PLAYLISTS, PAGE_NOTICE = 0, 1, 2, 3, 4

SEARCH_DEBOUNCE_MS = 350   # пока человек печатает, в сеть не ходим
SEARCH_LIMIT = 60

# Волны: плитки такого же размера, что подборки YouTube, - вкладки соседние,
# и разнобой в размерах читался бы как разница по смыслу, которой нет
WAVE_LIMIT = 24
# Рекомендации плоским списком, когда каталог волн пуст
RECOMS_LIMIT = 60
WAVE_COVER = 104
WAVE_WIDTH = 132
WAVE_TITLE_CHARS = 34
# Один ряд плиток целиком - с обложкой и подписью, - остальное прокруткой.
# Резать плитку пополам нельзя: обрезанная подпись не даёт выбрать волну, а выбор
# здесь и есть смысл вкладки
WAVE_ROW_H = WAVE_COVER + 44
WAVE_CATALOG_MAX_H = 2 * WAVE_ROW_H + 12

MARK_MINE = '✓ в моей музыке'
MARK_ADDABLE = '+ можно добавить'
MARK_DOWNLOADED = '✓ скачано'


def _filtered(tracks: list[Track], needle: str) -> list[Track]:
    if not needle:
        return list(tracks)
    return [track for track in tracks if needle in track.display_title.lower()]


def match_key(artist: str, title: str) -> str:
    """Ключ сравнения «исполнитель + название».

    Одна и та же песня приходит из поиска и из своей музыки с разным регистром,
    приписками в скобках и знаками препинания, а номера записи у копий разные.
    Поэтому принадлежность к своей музыке определяется по приведённой строке."""
    text = f'{artist} {title}'.lower().replace('ё', 'е')
    text = re.sub(r'\(.*?\)|\[.*?\]', ' ', text)      # (feat. ...), [Official Video]
    text = re.sub(r'[^0-9a-zа-я]+', ' ', text)
    return ' '.join(text.split())


def track_key(track: dict) -> str:
    """Ключ истории для трека VK: владелец плюс номер записи."""
    return history.key_for('vk_audio', f"{track.get('owner_id')}_{track.get('id')}")


def _human_delay(seconds: int) -> str:
    """«15 секунд», «2 минуты» - по-русски, с правильным окончанием."""
    if seconds < 60:
        return f'{seconds} с'
    minutes = round(seconds / 60)
    tail = minutes % 10
    if minutes % 100 // 10 == 1 or tail == 0 or tail > 4:
        word = 'минут'
    elif tail == 1:
        word = 'минуту'
    else:
        word = 'минуты'
    return f'{minutes} {word}'


class VkPanel(QWidget):
    login_requested = Signal()
    reconnect_requested = Signal()
    # Человек говорит, что снял блокировку на сайте: только по этому сигналу
    # программа снова обращается к VK
    unblock_requested = Signal()
    download_tracks_requested = Signal(list)  # list[track dict]
    play_tracks_requested = Signal(list, int)  # list[track dict], с какого начинать
    enqueue_tracks_requested = Signal(list, bool)  # list[track dict], «следующим»
    session_expired = Signal()  # VK отозвал сессию сайта - чип в шапке тоже должен это узнать
    add_progress = Signal(str)  # ход добавления в свою музыку: сигнал, потому что считает фоновый поток

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client = None
        # Списки VK живут в двух видах: словарь нужен для скачивания и resolve_url,
        # Track - для показа тем же списком, что и везде в приложении
        self._current_tracks: list[dict] = []
        self._current_playlists: list[dict] = []
        self._found_tracks: list[dict] = []
        self._my_tracks: list[Track] = []
        self._wave_tracks: list[Track] = []
        self._wave_mixes: list[dict] = []
        self._search_tracks: list[Track] = []
        # Волну не тянем при входе: это лишний запрос к VK ради вкладки, на
        # которую могут и не зайти. Грузим при первом показе, дальше - по кнопке
        self._wave_loaded = False
        self._rows: dict[str, dict] = {}     # uid -> строка VK
        self._mine_uids: set[str] = set()    # из выдачи поиска - что уже своё
        self._busy_count = 0
        self._notice_action = None
        # Номер поколения: ответ прежнего запроса не должен затирать новый
        self._search_gen = 0
        self._searching = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._start_vk_search)
        self._add_note = ''
        self.add_progress.connect(self._on_add_progress)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_lists(), 1)
        layout.addWidget(self._build_actions())

        self.set_logged_out()

    # ---------- сборка ----------
    def _build_lists(self) -> QWidget:
        container = QWidget()
        box = QVBoxLayout(container)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        self._tab_bar = QTabBar()
        self._tab_bar.addTab('Моя музыка')
        self._tab_bar.addTab('Волна')
        self._tab_bar.addTab('Поиск')
        self._tab_bar.addTab('Мои плейлисты')
        self._tab_bar.setExpanding(False)
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        box.addWidget(self._tab_bar)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Поиск…')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_text)
        self._search.returnPressed.connect(self._on_search_enter)
        box.addWidget(self._search)

        # QStackedWidget, а не QTabWidget: поиск и кнопки общие для обеих вкладок,
        # и рамка вкладок вокруг них выглядела бы как отдельная лишняя коробка
        # PagesStack, а не QStackedWidget: обычный держал бы высоту по самой
        # большой вкладке (волна со скроллом - 168 px), и раздел не сжимался
        # бы даже на пустом списке
        self._stack = PagesStack()
        self._tracks_list = self._make_track_list()
        self._search_list = self._make_track_list()
        self._playlists_list = self._make_playlist_list()
        self._playlists_list.itemDoubleClicked.connect(self._open_playlist)
        # Порядок добавления обязан совпадать с номерами PAGE_*: вкладка
        # переключает стек по своему индексу напрямую
        self._stack.addWidget(self._tracks_list)
        self._stack.addWidget(self._make_wave_page())
        self._stack.addWidget(self._search_list)
        self._stack.addWidget(self._playlists_list)
        self._stack.addWidget(self._make_notice())
        box.addWidget(self._stack, 1)
        return container

    def _make_wave_page(self) -> QWidget:
        """Каталог волн: сверху плитки подборок, снизу треки выбранной.

        Одним списком это не показать: волн у VK много и они разные, а список умеет
        показать только одну. Плитки - как у подборок YouTube на соседней вкладке:
        то же действие должно выглядеть одинаково, в какой бы вкладке ни делалось."""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._wave_hint = ElidedLabel('Выберите волну, VK собрал их сам')
        self._wave_hint.setObjectName('hint')
        box.addWidget(self._wave_hint)

        self._wave_row = FlowRow(spacing=10)
        # Каталог в прокрутке с потолком по высоте: волн бывает много, и без потолка
        # они съедают страницу, оставляя выбранной волне полоску в две строки -
        # а слушают всё-таки её, каталог нужен только чтобы выбрать
        self._wave_area = QScrollArea()
        self._wave_area.setWidget(self._wave_row)
        self._wave_area.setWidgetResizable(True)
        self._wave_area.setFrameShape(QScrollArea.NoFrame)
        self._wave_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._wave_area.setMaximumHeight(WAVE_CATALOG_MAX_H)
        # Список волны растягивается и без нижней границы отобрал бы у каталога всё
        # до последней плитки, оставив от неё обрезок без подписи
        self._wave_area.setMinimumHeight(WAVE_ROW_H)
        box.addWidget(self._wave_area)

        self._wave_title = QLabel()
        self._wave_title.setObjectName('h2')
        self._wave_title.hide()      # пока волну не выбрали, заголовку нечего называть
        box.addWidget(self._wave_title)

        self._wave_list = self._make_track_list()
        self._wave_list.hide()       # пустой список - пустая рамка, лучше её не рисовать
        box.addWidget(self._wave_list, 1)
        return page

    def _make_notice(self) -> QWidget:
        # Та же заглушка, что и в остальных разделах: значок, заголовок,
        # пояснение и кнопка, которая устраняет причину
        self._notice = EmptyState('radio', '')
        self._notice_btn = self._notice.add_action('', self._on_notice_clicked)
        return self._notice

    def _make_track_list(self) -> TrackListWidget:
        """Тот же список треков, что в остальных разделах: обложки, метки,
        одинаковое контекстное меню. Отметки галочками сменились выделением -
        в списке на полторы тысячи строк оно и быстрее, и привычнее."""
        widget = TrackListWidget()
        widget.play_requested.connect(self._play_tracks)
        widget.enqueue_requested.connect(self._enqueue_tracks)
        widget.download_requested.connect(self._download_tracks)
        widget.itemSelectionChanged.connect(self._update_counter)
        return widget

    def _make_playlist_list(self) -> CheckableListWidget:
        widget = CheckableListWidget()
        widget.setAlternatingRowColors(True)
        widget.setUniformItemSizes(True)
        widget.itemChanged.connect(self._update_counter)
        return widget

    def _build_actions(self) -> QWidget:
        # overflow: в низком окне восемь кнопок переносились в три строки и
        # съедали высоту списка - от него оставалось полторы строки. Теперь
        # лишние уезжают под «⋯», а список получает место
        row = FlowRow(spacing=8, align_right=True, overflow=True)
        self._refresh_btn = QPushButton('Обновить')
        self._refresh_btn.setObjectName('secondary')
        self._refresh_btn.clicked.connect(self._refresh_current_tab)
        row.add(self._refresh_btn)

        self._select_btns = []
        for text, select in (('Выбрать всё', True), ('Снять всё', False)):
            button = QPushButton(text)
            button.setObjectName('secondary')
            button.clicked.connect(lambda _=False, on=select: self._set_all(on))
            row.add(button)
            self._select_btns.append(button)

        self._preview_btn = QPushButton('Прослушать')
        self._preview_btn.setObjectName('secondary')
        self._preview_btn.setToolTip('Послушать выбранное, ничего не скачивая')
        self._preview_btn.clicked.connect(self._preview_checked)
        row.add(self._preview_btn)

        self._enqueue_btn = QPushButton('В очередь')
        self._enqueue_btn.setObjectName('secondary')
        self._enqueue_btn.setToolTip('Поставить выбранное в очередь общего плеера')
        self._enqueue_btn.clicked.connect(self._enqueue_checked)
        row.add(self._enqueue_btn)

        self._add_btn = QPushButton('Добавить в мою музыку')
        self._add_btn.setObjectName('secondary')
        self._add_btn.setToolTip('Добавить выбранные записи к себе, не скачивая файлы')
        self._add_btn.clicked.connect(self._add_checked_to_my_music)
        self._add_btn.setVisible(False)
        row.add(self._add_btn)

        self._counter = ElidedLabel()
        self._counter.setObjectName('hint')
        row.add(self._counter)
        row.add_stretch()

        self._download_btn = QPushButton('Скачать выбранное')
        # Ширина фиксирована: текст на кнопке меняется вместе со счётчиком, и без
        # этого соседние кнопки прыгали бы при каждой галочке. 187 - это ширина
        # самого длинного варианта («Скачать выбранное (999)»), измеренная шрифтом
        # темы. Прежние 210 были запасом на глаз, и из-за них вся строка требовала
        # 777 px при 738 доступных - главная кнопка раздела пряталась под «⋯»
        self._download_btn.setMinimumWidth(187)
        self._download_btn.clicked.connect(self._download_selected)
        row.add(self._download_btn)
        return row

    # ---------- состояние входа ----------
    def set_logged_out(self) -> None:
        self._client = None
        self._current_tracks = []
        self._current_playlists = []
        self._show_notice(
            'Вход в VK не выполнен',
            'После входа здесь появятся ваша музыка, плейлисты и поиск по VK.',
            'Войти в VK', self.login_requested.emit)

    def set_connecting(self) -> None:
        """Подключение сохранённым токеном идёт в фоне: без этого состояния панель
        на старте показывала «вход не выполнен» и звала входить заново."""
        self._client = None
        self._show_notice('Подключаюсь к VK…',
                          'Проверяю сохранённый вход, это займёт пару секунд.')

    def set_connect_failed(self, error: str, retry_in: int = 0) -> None:
        """Связи нет, но сохранённый вход цел - предлагаем повторить попытку.
        Раньше повторить её можно было только перезапуском приложения.

        `retry_in` - через сколько секунд приложение попробует само. Про это стоит
        сказать вслух: иначе человек видит «не удалось» и думает, что всё замерло
        до его нажатия."""
        self._client = None
        if retry_in:
            plan = f'Повторю попытку сам через {_human_delay(retry_in)}.'
        else:
            plan = 'Проверьте интернет и настройки прокси, затем повторите попытку.'
        self._show_notice(
            'Не удалось подключиться к VK',
            f'{error}\n{plan}',
            'Повторить сейчас', self.reconnect_requested.emit)

    def set_client(self, client) -> None:
        logger.debug('VkPanel.set_client: client=%r', client)
        self._client = client
        if not client.has_web_session:
            # Плейлисты API отдаёт и без сессии сайта, а их содержимое - нет.
            # Но просить пароль сразу рано: в профиле встроенного браузера обычно
            # ещё жив вход, и перезаход проходит молча. Пока он идёт, показываем
            # ожидание, а не кнопку, - иначе человек вводил бы пароль там, где
            # ничего вводить не нужно. Не вышло - вернёт `show_session_lost`.
            self._show_restoring()
            self.session_expired.emit()
            return
        self._hide_notice()
        self._load_tracks()
        self._load_playlists()

    # ---------- тихий перезаход ----------
    def _show_restoring(self) -> None:
        """Сессия сайта отвалилась, но пароль спрашивать рано.

        Кнопки здесь нет намеренно: пока автоматика работает, нажимать нечего, а
        предложенный пароль человек ввёл бы зря. Текст обещает не результат, а
        занятие - обещать успех до того, как он случился, нечестно."""
        self._show_notice(
            'Обновляю вход в VK',
            'Сессия сайта VK истекла. Пробую вернуть её сам, пароль, скорее всего, '
            'не понадобится.')

    def show_account_blocked(self, message: str = '') -> None:
        """VK заблокировал аккаунт: чинить нечего, и предлагать вход было бы обманом.

        Кнопки входа здесь намеренно нет - единственное, что помогает, происходит на
        стороне VK, в обычном браузере. Раньше на этом месте висело «Обновляю вход»,
        и человек ждал починки, которой не могло случиться."""
        self._show_notice(
            'VK заблокировал аккаунт',
            message or 'Откройте vk.com в браузере: VK покажет причину и способ '
                       'снять блокировку. Вход в программу тут ни при чём.',
            # Кнопка одна и нажимается только руками. Сама программа в
            # заблокированный аккаунт больше не стучится: её попытки ничего не
            # возвращают, а VK считает их продолжением того самого потока запросов.
            # Человек снимает блокировку на сайте и говорит об этом нажатием
            'Блокировка снята, повторить', self.unblock_requested.emit)

    def show_session_lost(self) -> None:
        """Тихо не вышло: теперь кнопка входа уместна - и другого пути уже нет."""
        self._show_notice(
            'Сессия сайта VK больше не действует',
            'Вернуть её сам я не смог, музыка недоступна.\n'
            'Войдите в VK заново, чтобы вернуть список треков.',
            'Войти заново', self.login_requested.emit)

    # ---------- заглушка вместо списков ----------
    def _show_notice(self, title: str, text: str = '', button_text: str = '',
                     action=None, icon: str = 'radio') -> None:
        """Показываем причину вместо пустых списков - и кнопку, которая её устраняет."""
        self._notice.set_icon(icon)
        self._notice.set_title(title)
        self._notice.set_text(text)
        self._notice_action = action
        self._notice_btn.setText(button_text)
        self._notice_btn.setVisible(bool(button_text))
        self._my_tracks = []
        self._wave_tracks = []
        self._search_tracks = []
        self._found_tracks = []
        self._wave_loaded = False
        self._wave_mixes = []
        self._wave_row.clear()
        self._wave_area.show()       # прошлый заход мог спрятать полосу за ненадобностью
        self._wave_title.hide()
        self._tracks_list.set_tracks([])
        self._wave_list.set_tracks([])
        self._search_list.set_tracks([])
        self._playlists_list.clear()
        self._tab_bar.setEnabled(False)
        self._stack.setCurrentIndex(PAGE_NOTICE)
        self._set_controls_enabled(False)
        self._update_counter()

    def _hide_notice(self) -> None:
        self._tab_bar.setEnabled(True)
        self._stack.setCurrentIndex(self._tab_bar.currentIndex())
        self._set_controls_enabled(True)
        self._update_counter()

    def _on_notice_clicked(self) -> None:
        if self._notice_action:
            self._notice_action()

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._search.setEnabled(enabled)
        self._refresh_btn.setEnabled(enabled)
        self._download_btn.setEnabled(enabled)
        # Пока вместо списка стоит заглушка, выбирать и ставить в очередь нечего:
        # живые кнопки над пустотой обещают то, чего не будет
        for button in self._select_btns:
            button.setEnabled(enabled)
        self._enqueue_btn.setEnabled(enabled)

    # ---------- загрузка данных ----------
    def _refresh_current_tab(self) -> None:
        """Обновляем только видимый список: второй никто сейчас не смотрит."""
        index = self._tab_bar.currentIndex()
        if index == TAB_TRACKS:
            self._load_tracks()
        elif index == TAB_WAVE:
            self._load_wave()
        elif index == TAB_SEARCH:
            self._start_vk_search(force=True)
        else:
            self._load_playlists()

    def _load_tracks(self) -> None:
        client = self._client
        self._run_fetch(lambda: client.get_my_tracks(), self._fill_tracks)

    def _load_wave(self) -> None:
        client = self._client
        self._wave_loaded = True
        self._wave_hint.setText('Спрашиваю у VK, что он сегодня собрал…')
        # Клиент постарше умеет только плоский каталог волн - вкладка обязана
        # работать и с ним, иначе обновление приложения ломается на ровном месте
        fetch = (getattr(client, 'wave_shelves', None)
                 or (lambda limit: client.wave_mixes(limit)))
        self._run_fetch(lambda: fetch(WAVE_LIMIT), self._fill_wave)

    def _load_playlists(self) -> None:
        client = self._client
        self._run_fetch(lambda: client.get_playlists(), self._fill_playlists)

    def _run_fetch(self, fn, on_done) -> None:
        # Счётчик, а не флаг: set_client() запускает загрузку треков и плейлистов
        # одновременно - флаг блокировал бы второй запрос до завершения первого.
        if not self._client:
            return
        client = self._client
        self._busy_count += 1
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText('Загружаю…')

        def handle(result, error):
            self._busy_count -= 1
            if self._busy_count == 0:
                self._refresh_btn.setEnabled(True)
                self._refresh_btn.setText('Обновить')
            if self._client is not client:
                return  # пока запрос летел, вышли из аккаунта или сменили его
            if error:
                self._on_fetch_error(error)
                return
            on_done(result)

        run_async(fn, handle)

    def _on_fetch_error(self, error) -> None:
        """Протухшую сессию сайта VK показываем не как сбой запроса, а как ожидание.

        VkSessionExpired приходит, когда VK отправил нас на страницу входа;
        «permissions to browse» - тот же случай, только на чужом плейлисте. Пароль
        здесь не спрашиваем: сессия сайта протухает часто, а возвращается сама -
        кнопка появится, только когда тихий перезаход сдастся."""
        text = str(error)
        if isinstance(error, VkSessionExpired) or 'permissions to browse' in text.lower():
            self._show_restoring()
            self.session_expired.emit()
            return
        QMessageBox.warning(self, 'VK', f'Не удалось получить данные из VK:\n{text}')

    def _remember(self, rows: list[dict]) -> list[Track]:
        """Track для показа, исходная строка VK - под рукой: скачивание и
        прямые ссылки по-прежнему работают со словарём клиента."""
        tracks = [from_vk(row) for row in rows]
        for track, row in zip(tracks, rows):
            self._rows[track.uid] = row
        return tracks

    def _rows_for(self, tracks) -> list[dict]:
        return [self._rows.get(track.uid) or to_vk_row(track) for track in tracks]

    def _fill_tracks(self, tracks: list[dict]) -> None:
        self._current_tracks = tracks
        self._my_tracks = self._remember(tracks)
        self._tracks_list.set_badges(
            {track.uid: MARK_DOWNLOADED
             for track, row in zip(self._my_tracks, tracks)
             if history.is_downloaded(track_key(row))})
        self._remark_search()
        self._apply_search()

    def _fill_wave(self, mixes: list[dict]) -> None:
        """Разложить подборки по плиткам.

        Пусто здесь бывает не случайно: готовые волны (`blocks`) VK отдаёт далеко
        не всем аккаунтам, а полки строятся из его же разметки плейлистов, которой
        тоже может не оказаться. Если не набралось вообще ничего, показываем
        плоский список раздела `recoms` - это по-прежнему рекомендации самого VK,
        а не поиск, выданный за них (AGENTS.md): меняется форма подачи, а не
        происхождение."""
        self._wave_mixes = mixes
        self._wave_row.clear()
        for mix in mixes:
            tile = self._wave_tile(mix)
            if tile is not None:
                self._wave_row.add(tile)
        # Каталог пуст - плиткам взяться неоткуда, и полоса выбора только занимает место
        self._wave_area.setVisible(bool(mixes))
        if mixes:
            self._wave_hint.setText(f'Подборок: {len(mixes)}, выберите любую')
            self._update_counter()
            return
        client = self._client
        if not hasattr(client, 'recommended_tracks'):
            # Клиент без рекомендаций - падать из-за этого вкладке незачем: честно
            # говорим, что волн нет, как было до появления запасного пути
            self._fill_wave_recoms([])
            return
        self._wave_hint.setText('Собираю рекомендации VK…')
        self._update_counter()
        self._run_fetch(lambda: client.recommended_tracks(RECOMS_LIMIT),
                        self._fill_wave_recoms)

    def _fill_wave_recoms(self, tracks: list[dict]) -> None:
        """Рекомендации VK плоским списком - когда каталог волн пуст.

        Заголовок нужен: без плиток над списком иначе не понять, что это вообще
        такое и откуда взялось."""
        self._wave_tracks = self._remember(tracks)
        self._wave_title.setText('Рекомендации VK')
        self._wave_title.setVisible(bool(tracks))
        self._wave_list.setVisible(bool(tracks))
        self._wave_hint.setText(
            'VK собрал это по вашим прослушиваниям' if tracks
            else 'VK пока не собрал рекомендаций для этого аккаунта')
        self._apply_search()
        self._update_counter()

    def _wave_tile(self, mix: dict) -> QToolButton | None:
        """Плитка волны: обложка, название и открытие по нажатию."""
        title = (mix.get('title') or '').strip()
        if not title:
            return None
        button = QToolButton()
        button.setObjectName('tile')
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(WAVE_COVER, WAVE_COVER))
        button.setFixedWidth(WAVE_WIDTH)
        button.setCursor(Qt.PointingHandCursor)
        button.setText(title if len(title) <= WAVE_TITLE_CHARS
                       else title[:WAVE_TITLE_CHARS - 1] + '…')
        subtitle = (mix.get('subtitle') or '').strip()
        button.setToolTip(f'{title}\n{subtitle}' if subtitle else title)
        button.setIcon(QIcon(covers.placeholder(WAVE_COVER)))
        cover = mix.get('cover') or ''
        if cover:
            covers.load(cover, self._cover_setter(button))
        button.clicked.connect(lambda _checked=False, m=mix: self._open_wave(m))
        return button

    @staticmethod
    def _cover_setter(button: QToolButton):
        """Обложка приходит из сети, когда плитки может уже не быть на свете."""
        def apply(_url, pixmap):
            try:
                button.setIcon(QIcon(pixmap))
            except RuntimeError:
                pass                     # плитку успели снести вместе с каталогом
        return apply

    def _open_wave(self, mix: dict) -> None:
        """Треки выбранной волны - тем же путём, что треки плейлиста: это он и есть."""
        self._wave_title.setText(mix.get('title') or 'Волна')
        self._wave_title.show()
        self._wave_hint.setText('Загружаю волну…')
        client = self._client
        # Подборки теперь трёх родов - раздел, склейка альбомов, волна VK, - и
        # разбираться, какая перед нами, дело клиента, а не панели
        fetch = getattr(client, 'shelf_tracks', None)
        self._run_fetch((lambda: fetch(mix)) if fetch
                        else (lambda: client.get_playlist_tracks(mix)),
                        self._fill_wave_tracks)

    def _fill_wave_tracks(self, tracks: list[dict]) -> None:
        self._wave_tracks = self._remember(tracks)
        self._wave_hint.setText(f'Треков в подборке: {len(self._wave_tracks)}'
                                if self._wave_tracks else 'В этой подборке пусто')
        # Пустая волна бывает: VK показывает подборку, но треки в ней уже недоступны
        self._wave_list.setVisible(bool(self._wave_tracks))
        self._apply_search()

    def _fill_playlists(self, playlists: list[dict]) -> None:
        self._current_playlists = playlists
        self._playlists_list.clear()
        for playlist in playlists:
            # Раньше исполнитель показывался вместо количества, и у альбомов число
            # треков просто пропадало - показываем и то, и другое
            parts = [playlist.get('title') or 'Плейлист']
            count = playlist.get('count') or 0
            parts.append(f'{count} {_plural_tracks(count)}' if count else 'треки не указаны')
            artist = playlist.get('artist')
            if artist:
                parts.append(artist)
            item = QListWidgetItem('   ·   '.join(parts))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, item.text().lower())
            item.setData(Qt.UserRole + 1, playlist)
            item.setToolTip('Двойным щелчком выбирают отдельные треки')
            self._playlists_list.addItem(item)
        self._apply_search()

    # ---------- вкладки, поиск, отметки ----------
    def _current_list(self) -> QListWidget:
        return {TAB_TRACKS: self._tracks_list,
                TAB_WAVE: self._wave_list,
                TAB_SEARCH: self._search_list,
                TAB_PLAYLISTS: self._playlists_list}[self._tab_bar.currentIndex()]

    def _selected_rows(self) -> list[dict]:
        """Строки VK для выбранных треков активной вкладки."""
        widget = self._current_list()
        if isinstance(widget, TrackListWidget):
            return self._rows_for(widget.selected_tracks())
        return []

    def _on_tab_changed(self, index: int) -> None:
        if self._stack.currentIndex() != PAGE_NOTICE:
            self._stack.setCurrentIndex(index)
        self._search.setPlaceholderText(
            {TAB_TRACKS: 'Поиск по своим трекам…',
             TAB_WAVE: 'Поиск по волне…',
             TAB_SEARCH: 'Искать во всей музыке VK…',
             TAB_PLAYLISTS: 'Поиск по плейлистам…'}[index])
        self._add_btn.setVisible(index == TAB_SEARCH)
        if index == TAB_WAVE and not self._wave_loaded and self._client:
            self._load_wave()          # первый заход на вкладку - за подборкой
        if index == TAB_SEARCH:
            # На вкладку перешли с уже набранным словом - поищем сразу
            if self._search.text().strip() and not self._found_tracks:
                self._search_timer.start()
            self._update_counter()
        else:
            self._apply_search()

    # ---------- поиск по всей музыке VK ----------
    def _on_search_text(self, _text: str = '') -> None:
        """Одно поле на все вкладки: свои списки фильтруются на месте,
        вкладка «Поиск» уходит запросом в саму VK."""
        if self._tab_bar.currentIndex() != TAB_SEARCH:
            self._apply_search()
            return
        self._search_gen += 1        # то, что ещё летит из сети, уже неактуально
        self._add_note = ''
        if not self._search.text().strip():
            self._search_timer.stop()
            self._searching = False
            self._found_tracks = []
            self._search_tracks = []
            self._mine_uids = set()
            self._search_list.set_tracks([])
            self._update_counter()
            return
        self._search_timer.start()

    def _on_search_enter(self) -> None:
        if self._tab_bar.currentIndex() == TAB_SEARCH:
            self._start_vk_search(force=True)

    def _start_vk_search(self, force: bool = False) -> None:
        """Глобальный поиск VK - той же веб-сессией, что и своя музыка
        (audio.search токену Kate Mobile недоступен, см. vk_client.search_tracks)."""
        self._search_timer.stop()
        client = self._client
        query = self._search.text().strip()
        if client is None or not query:
            return
        if self._tab_bar.currentIndex() != TAB_SEARCH and not force:
            return
        self._search_gen += 1
        gen = self._search_gen
        self._searching = True
        self._update_counter()

        def on_done(rows, error):
            # Ответ на прежний запрос или на прежний вход - молча выбрасываем
            if gen != self._search_gen or self._client is not client:
                return
            self._searching = False
            if error:
                self._on_fetch_error(error)
                self._update_counter()
                return
            self._fill_search(rows or [])

        run_async(lambda: client.search_tracks(query, SEARCH_LIMIT), on_done)

    def _my_music_index(self) -> tuple[set, set]:
        ids = {(str(t.get('owner_id')), str(t.get('id'))) for t in self._current_tracks}
        names = {match_key(t.get('artist', ''), t.get('title', ''))
                 for t in self._current_tracks}
        return ids, names

    def _is_mine(self, track: dict, ids: set, names: set) -> bool:
        """VK в выдаче поиска не сообщает, есть ли запись у вас, - сверяемся
        с уже загруженной своей музыкой: сначала по номеру, потом по названию."""
        if self._client is not None and str(track.get('owner_id')) == str(self._client.user_id):
            return True
        if (str(track.get('owner_id')), str(track.get('id'))) in ids:
            return True
        return match_key(track.get('artist', ''), track.get('title', '')) in names

    def _fill_search(self, tracks: list[dict]) -> None:
        self._add_note = ''
        self._found_tracks = tracks
        self._search_tracks = self._remember(tracks)
        self._mark_found()
        self._apply_search()

    def _mark_found(self) -> None:
        """Метка «уже в моей музыке» - своя для каждой строки выдачи."""
        ids, names = self._my_music_index()
        self._mine_uids = {
            track.uid for track, row in zip(self._search_tracks, self._found_tracks)
            if self._is_mine(row, ids, names)}
        self._search_list.set_badges(
            {track.uid: (MARK_MINE if track.uid in self._mine_uids else MARK_ADDABLE)
             for track in self._search_tracks})

    def _remark_search(self) -> None:
        """Своя музыка перечиталась - пометки в выдаче поиска должны догнать её."""
        if self._search_tracks:
            self._mark_found()

    def _on_add_progress(self, text: str) -> None:
        self._add_note = text
        self._update_counter()

    def _add_checked_to_my_music(self) -> None:
        """«+ В мою музыку» для отмеченного: добавляем готовые записи VK,
        ничего не скачивая и не заливая заново.

        Ход работы пишем строкой рядом со счётчиком: модальное окно посреди
        добавления мешало бы слушать и отмечать дальше."""
        selected = self._search_list.selected_tracks()
        pending = [self._rows[track.uid] for track in selected
                   if track.uid not in self._mine_uids and track.uid in self._rows]
        already = len(selected) - len(pending)
        if not pending:
            self._on_add_progress('Выбранное уже есть в вашей музыке')
            return
        client = self._client
        total = len(pending)
        self._on_add_progress(f'Добавляю: 0 из {total}…')

        def add_all():
            added, failed = 0, []
            for number, track in enumerate(pending, 1):
                try:
                    client.add_audio(track.get('owner_id'), track.get('id'),
                                     track.get('access_key') or '')
                    added += 1
                except Exception as exc:      # причину показываем человеку целиком
                    logger.info('audio.add не прошёл: %s', exc)
                    failed.append(str(exc).splitlines()[0])
                note = f'Добавляю: {number} из {total}…'
                if already:
                    note += f' ({already} уже в VK)'
                self.add_progress.emit(note)
            return added, failed

        def done(result):
            added, failed = result
            parts = [f'Добавлено: {added}']
            if already:
                parts.append(f'уже были: {already}')
            if failed:
                parts.append(f'не удалось: {len(failed)} ({failed[0]})')
            self._on_add_progress('   ·   '.join(parts))
            if added:
                self._load_tracks()   # свою музыку перечитываем - пометки обновятся

        self._run_fetch(add_all, done)

    def _apply_search(self) -> None:
        """Фильтр применяем только к видимой вкладке: поле поиска одно на все,
        и прятать строки там, куда сейчас не смотрят, незачем."""
        index = self._tab_bar.currentIndex()
        needle = self._search.text().strip().lower()
        self._refill(self._tracks_list, self._my_tracks,
                     needle if index == TAB_TRACKS else '')
        self._refill(self._wave_list, self._wave_tracks,
                     needle if index == TAB_WAVE else '')
        self._refill(self._search_list, self._search_tracks,
                     needle if index == TAB_SEARCH else '')
        playlist_needle = needle if index == TAB_PLAYLISTS else ''
        for i in range(self._playlists_list.count()):
            item = self._playlists_list.item(i)
            item.setHidden(bool(playlist_needle)
                           and playlist_needle not in (item.data(Qt.UserRole) or ''))
        self._update_counter()

    @staticmethod
    def _refill(widget: TrackListWidget, tracks: list[Track], needle: str) -> None:
        """Перезаполняем список, только если он изменился: set_tracks сбрасывает
        выделение, а переключение вкладок сбрасывать его не должно."""
        wanted = _filtered(tracks, needle)
        if widget.tracks() != wanted:
            widget.set_tracks(wanted)

    def _set_all(self, select: bool) -> None:
        widget = self._current_list()
        if isinstance(widget, TrackListWidget):
            if select:
                widget.selectAll()
            else:
                widget.clearSelection()
            return
        for i in range(widget.count()):
            item = widget.item(i)
            if not item.isHidden():  # действуем только на видимое, чтобы поиск не обманывал
                item.setCheckState(Qt.Checked if select else Qt.Unchecked)

    def _checked_indexes(self, widget: QListWidget) -> list[int]:
        return [i for i in range(widget.count()) if widget.item(i).checkState() == Qt.Checked]

    def _chosen_count(self) -> int:
        widget = self._current_list()
        if isinstance(widget, TrackListWidget):
            return len(widget.selectedItems())
        return len(self._checked_indexes(widget))

    def _set_counter(self, text: str) -> None:
        """Подпись счётчика. Пустая прячется совсем, а не занимает место.

        В строке действий она стоит между кнопками, и её 6 px плюс отступ уводили
        «Скачать выбранное» под «⋯» ровно там, где счётчику нечего сказать, - до
        входа в VK."""
        self._counter.setText(text)
        self._counter.setVisible(bool(text))

    def _update_counter(self, *_args) -> None:
        if self._stack.currentIndex() == PAGE_NOTICE:
            self._set_counter('')
            self._download_btn.setEnabled(False)
            self._download_btn.setText('Скачать выбранное')
            self._preview_btn.setEnabled(False)
            return
        index = self._tab_bar.currentIndex()
        chosen = self._chosen_count()
        total = self._current_list().count()
        if not self._client:
            self._set_counter('')
        elif index == TAB_SEARCH and self._add_note:
            self._set_counter(self._add_note)
        elif index == TAB_SEARCH and self._searching:
            self._set_counter('Ищу в VK…')
        elif index == TAB_SEARCH and not self._search.text().strip():
            self._set_counter('Введите запрос, поищу во всей музыке VK')
        elif index == TAB_WAVE and not self._wave_tracks:
            # Каталог есть, волну ещё не выбрали - это не «ничего не найдено».
            # А пустой каталог - ответ VK, а не наш сбой, и звучать должен так же
            self._set_counter('Выберите волну' if self._wave_mixes
                                  else 'VK пока не собрал волн для этого аккаунта')
        elif not total:
            self._set_counter('Ничего не найдено')
        else:
            self._set_counter(f'Выбрано: {chosen} из {total}')
        self._download_btn.setEnabled(bool(self._client) and chosen > 0)
        self._download_btn.setText(f'Скачать выбранное ({chosen})' if chosen
                                   else 'Скачать выбранное')
        # Плейлист целиком слушать нечего: ссылки есть только у самих треков
        on_tracks = index in (TAB_TRACKS, TAB_WAVE, TAB_SEARCH)
        self._preview_btn.setEnabled(bool(self._client) and on_tracks and chosen > 0)
        self._enqueue_btn.setEnabled(bool(self._client) and on_tracks and chosen > 0)
        self._preview_btn.setToolTip(
            'Послушать выбранное, ничего не скачивая' if on_tracks
            else 'Откройте плейлист двойным щелчком, слушать можно и отдельные треки')
        addable = sum(1 for track in self._search_list.selected_tracks()
                      if track.uid not in self._mine_uids)
        self._add_btn.setEnabled(bool(self._client) and index == TAB_SEARCH and addable > 0)
        self._add_btn.setText(f'Добавить в мою музыку ({addable})' if addable
                              else 'Добавить в мою музыку')

    # ---------- прослушивание ----------
    # Играет общий плеер приложения: своё окно предпросмотра здесь больше не нужно -
    # иначе в приложении оказывалось бы два независимых плеера. Прямые ссылки VK
    # по-прежнему запрашиваются лениво, уже внутри PlayerController.
    def _play_tracks(self, tracks, index: int = 0) -> None:
        """Двойной клик или «Играть» из меню - включаем в общем плеере."""
        rows = self._rows_for(tracks)
        if rows:
            self.play_tracks_requested.emit(rows, index)

    def _enqueue_tracks(self, tracks, play_next: bool = False) -> None:
        rows = self._rows_for(tracks)
        if rows:
            self.enqueue_tracks_requested.emit(rows, play_next)

    def _download_tracks(self, tracks) -> None:
        rows = self._rows_for(tracks)
        if rows:
            self.download_tracks_requested.emit(rows)

    def _preview_checked(self) -> None:
        rows = self._selected_rows()
        if rows:
            self.play_tracks_requested.emit(rows, 0)

    def _enqueue_checked(self) -> None:
        rows = self._selected_rows()
        if rows:
            self.enqueue_tracks_requested.emit(rows, False)

    def _preview_entry(self, entry: dict) -> None:
        """Трек из диалога плейлиста - название там уже собрано целиком."""
        self.play_tracks_requested.emit([entry], 0)

    # ---------- скачивание ----------
    def _open_playlist(self, item: QListWidgetItem) -> None:
        playlist = item.data(Qt.UserRole + 1)
        self._run_fetch(
            lambda: self._client.get_playlist_tracks(playlist),
            lambda tracks: self._show_playlist_tracks(playlist, tracks),
        )

    def _show_playlist_tracks(self, playlist: dict, tracks: list[dict]) -> None:
        if not tracks:
            QMessageBox.information(self, 'VK', 'В этом плейлисте нет доступных треков.')
            return
        entries = [{**t, 'title': f"{t.get('artist', '')} · {t.get('title', '')}".strip(' ·')}
                   for t in tracks]
        dlg = PlaylistPickDialog(playlist.get('title') or 'Треки плейлиста', entries, self,
                                 history_key=track_key, preview=self._preview_entry,
                                 preview_text='Прослушать')
        if dlg.exec():
            selected = dlg.selected_entries()
            if selected:
                self.download_tracks_requested.emit(selected)

    def _download_selected(self) -> None:
        if self._tab_bar.currentIndex() != TAB_PLAYLISTS:
            selected = self._selected_rows()
            if selected:
                self.download_tracks_requested.emit(selected)
            return

        playlists = [self._current_playlists[i]
                     for i in self._checked_indexes(self._playlists_list)]
        if not playlists:
            return

        def collect():
            tracks = []
            for playlist in playlists:
                tracks.extend(self._client.get_playlist_tracks(playlist))
            return tracks

        self._run_fetch(collect, self._emit_tracks)

    def _emit_tracks(self, tracks: list[dict]) -> None:
        if not tracks:
            QMessageBox.information(self, 'VK', 'В выбранных плейлистах нет доступных треков.')
            return
        self.download_tracks_requested.emit(tracks)


def _plural_tracks(count: int) -> str:
    """«1 трек», «2 трека», «5 треков» - иначе строка выглядит небрежно."""
    if 11 <= count % 100 <= 14:
        return 'треков'
    return {1: 'трек', 2: 'трека', 3: 'трека', 4: 'трека'}.get(count % 10, 'треков')
