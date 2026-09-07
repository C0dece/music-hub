"""Раздел YouTube: лента, поиск и действия над найденным.

Раздел открывается лентой, а не пустым полем ввода. Сверху — своё: «Мне
понравилось» и личные плейлисты YouTube Music. Ниже — подборки, которые YouTube
предлагает сам: сначала плитки готовых подборок (миксы, настроения, жанры) —
их много и выбирают обычно по ним, — а под ними полки с отдельными треками.
Ленту можно прокрутить и включить что угодно двойным щелчком, не набирая ни
одного запроса. Личное показывается только при входе: без кук браузера личных
плейлистов не существует, и выдумывать их нельзя.

Поиск идёт двумя путями. «Музыка» — каталог YouTube Music: там сразу
исполнитель, название и длительность, без обзоров и стримов. «Видео» — обычный
поиск через yt-dlp, тот же, что у загрузчика, со своими куками и прокси. «Всё»
показывает и то, и другое. Если музыкальный каталог недоступен, поиск молча
падает на yt-dlp — результат будет, просто менее музыкальный.

Ссылку сюда тоже можно вставить: строка поиска понимает и адрес видео, и адрес
плейлиста.

Поиск и открытый плейлист показываются на месте ленты, а не вместо неё насовсем:
кнопка «К ленте» возвращает обратно. Подборки занимают постоянные места (слоты)
и заполняются, когда придёт ответ: списки подключаются к главному окну один раз,
иначе новый список на каждый ответ остался бы без действий по правой кнопке. По
той же причине наружу отдаётся `lists` — все списки раздела разом.

Само окно плеера здесь больше не живёт: видео показывает общая сцена в главном
окне, одна на все разделы."""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from ..core import ytdlp_engine
from ..core.async_task import run_async
from ..core.track import from_youtube
from . import covers
from .flow_layout import FlowRow
from .track_list import ROW_HEIGHT, SelectionBar, TrackListWidget
from .widgets import ElidedLabel, Skeleton

logger = logging.getLogger(__name__)

RESULT_LIMIT = 25
DEBOUNCE_MS = 400

# Лента: сколько треков в подборке, сколько подборок помещается и как долго ответ
# считается свежим. Мегабайты JSON при каждом переходе в раздел тянуть незачем.
FEED_SECTION_LIMIT = 12
FEED_SLOTS = 4
FEED_TTL = 600

# Подборки главной. Их много и они разные по настроению — в этом весь смысл:
# человек выбирает плитку, а не вычитывает списки треков.
MIX_SLOTS = 8
MIX_PER_SHELF = 18
MIX_COVER = 104
MIX_WIDTH = 132
MIX_TITLE_CHARS = 34
MY_LIMIT = 30
LIKED_LIMIT = 60
PLAYLIST_LIMIT = 200

VIEW_FEED, VIEW_RESULTS = 0, 1

MODE_MUSIC, MODE_VIDEO, MODE_ALL = 'music', 'video', 'all'
MODES = ((MODE_MUSIC, 'Музыка'), (MODE_VIDEO, 'Видео'), (MODE_ALL, 'Всё'))
# Строка поиска подсказывает, куда сейчас уйдёт запрос: переключатель иначе
# незаметен, и разницу в выдаче принимают за случайность
MODE_HINTS = {
    MODE_MUSIC: 'Ищу музыку: название, исполнитель или ссылка',
    MODE_VIDEO: 'Ищу ролики: название, канал или ссылка',
    MODE_ALL: 'Ищу везде: название, исполнитель, канал или ссылка',
}


def looks_like_url(text: str) -> bool:
    return text.startswith('http://') or text.startswith('https://')


class YouTubePage(QWidget):
    """Лента YouTube, поиск по нему и действия над найденным."""

    def __init__(self, settings_provider, discovery=None, recommender=None, parent=None):
        super().__init__(parent)
        self._settings = settings_provider
        self._discovery = discovery
        self._recommender = recommender
        self._query = ''
        self._mode = MODE_MUSIC
        # Номер запроса: ответ старого поиска не должен затирать новый список
        self._gen = 0
        self._mine_at = 0.0
        self._mine_busy = False
        self._sections_at = 0.0
        self._sections_busy = False
        self._sections_shown = 0
        self._mixes_shown = 0

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_search)

        self._build_ui()

    # ---------- сборка ----------
    def _build_ui(self) -> None:
        box = self._box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        # Возврат к ленте виден только тогда, когда есть куда возвращаться
        self._back_btn = QPushButton('← К ленте')
        self._back_btn.setObjectName('secondary')
        self._back_btn.clicked.connect(self.show_feed)
        self._back_btn.hide()
        search_row.addWidget(self._back_btn)
        self._input = QLineEdit()
        self._input.setPlaceholderText(MODE_HINTS[self._mode])
        self._input.setClearButtonEnabled(True)
        self._input.returnPressed.connect(self._start_search)
        self._input.textChanged.connect(self._on_text_changed)
        search_row.addWidget(self._input, 1)
        self._search_btn = QPushButton('Найти')
        self._search_btn.clicked.connect(self._start_search)
        search_row.addWidget(self._search_btn)
        box.addLayout(search_row)

        # Переключатели ищут заново сразу: человек уже ввёл запрос, ждать
        # повторного нажатия «Найти» незачем
        tabs = self._tabs_row = FlowRow(spacing=6)
        self._tabs: dict[str, QPushButton] = {}
        for mode, title in MODES:
            button = QPushButton(title)
            button.setObjectName('tab')
            button.setCheckable(True)
            button.setChecked(mode == self._mode)
            button.clicked.connect(lambda _checked=False, name=mode: self.set_mode(name))
            tabs.add(button)
            self._tabs[mode] = button
        tabs.add_stretch()
        # В ленте переключать нечего: подборки YouTube приходят готовыми, и
        # разделить их на музыку и ролики можно только новым запросом. Пока
        # ряд там висел, он выглядел сломанным, потому что нажатие ничего
        # не меняло. Показываем его вместе с результатами поиска
        tabs.hide()
        box.addWidget(tabs)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_feed())
        self._stack.addWidget(self._build_results())
        box.addWidget(self._stack, 1)

        actions = self._actions = FlowRow(spacing=8)
        self._status = ElidedLabel('Введите запрос, найду на YouTube')
        self._status.setObjectName('hint')
        actions.add(self._status)
        self._retry_btn = QPushButton('Повторить')
        self._retry_btn.setObjectName('secondary')
        self._retry_btn.clicked.connect(self._retry)
        self._retry_btn.hide()
        actions.add(self._retry_btn)
        actions.add_stretch()
        for text, slot, secondary in (
            ('Играть', self._play_selected, False),
            ('В очередь', self._enqueue_selected, True),
            ('+ VK', self._add_vk_selected, True),
            ('Скачать', self._download_selected, True),
        ):
            button = QPushButton(text)
            if secondary:
                button.setObjectName('secondary')
            button.clicked.connect(slot)
            actions.add(button)
        # В ленте действовать не над чем: у каждой подборки свои строки и своё меню
        actions.hide()
        box.addWidget(actions)

    def _build_feed(self) -> QWidget:
        """Лента целиком в прокрутке: на узком окне подборки уезжают вниз."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        # Кнопка обновления живёт наверху и не прячется: за ней приходят, когда
        # надоела текущая лента, а не только когда что-то сломалось
        status = FlowRow(spacing=8)
        self._feed_retry = QPushButton('Обновить ленту')
        self._feed_retry.setObjectName('secondary')
        self._feed_retry.setToolTip('Перезапросить подборки YouTube (F5)')
        self._feed_retry.clicked.connect(self.refresh_feed)
        status.add(self._feed_retry)
        self._feed_hint = ElidedLabel('Собираю ленту YouTube…')
        self._feed_hint.setObjectName('hint')
        status.add(self._feed_hint)
        status.add_stretch()
        box.addWidget(status)

        # F5 — только внутри раздела: на других страницах у окна свои дела
        refresh = QShortcut(QKeySequence.Refresh, self)
        refresh.setContext(Qt.WidgetWithChildrenShortcut)
        refresh.activated.connect(self.refresh_feed)

        self._mine_label = QLabel('Моё на YouTube')
        self._mine_label.setObjectName('h2')
        self._mine_label.hide()
        box.addWidget(self._mine_label)
        self._mine_row = FlowRow(spacing=8)
        self._mine_row.hide()
        box.addWidget(self._mine_row)
        # QLabel с переносом, а не ElidedLabel: тут не статус в строку, а объяснение,
        # что сделать, чтобы раздел заполнился. Обрезка отнимала бы у него как раз
        # хвост с условием — «браузер с выполненным входом», — и подсказка переставала
        # подсказывать. Ширины при этом не требуем, окно сузить она не мешает
        self._mine_hint = QLabel('')
        self._mine_hint.setObjectName('hint')
        self._mine_hint.setWordWrap(True)
        self._mine_hint.setMinimumWidth(1)
        self._mine_hint.hide()
        box.addWidget(self._mine_hint)

        # Пока лента едет, на её месте видно, что что-то грузится
        self._feed_skeleton = Skeleton(3)
        self._feed_skeleton.hide()
        box.addWidget(self._feed_skeleton)

        # Постоянные места под плитки подборок
        self._mix_slots: list[tuple[QLabel, FlowRow]] = []
        for _index in range(MIX_SLOTS):
            label = QLabel('')
            label.setObjectName('h2')
            label.hide()
            box.addWidget(label)
            row = FlowRow(spacing=10)
            row.hide()
            box.addWidget(row)
            self._mix_slots.append((label, row))

        # Постоянные места под подборки: заполняются, когда придёт ответ
        self._slots: list[tuple[QLabel, TrackListWidget]] = []
        for _index in range(FEED_SLOTS):
            label = QLabel('')
            label.setObjectName('h2')
            label.hide()
            box.addWidget(label)
            widget = TrackListWidget(self)
            widget.hide()
            box.addWidget(widget)
            self._slots.append((label, widget))

        box.addStretch(1)
        area.setWidget(inner)
        return area

    def _build_results(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        self.list = TrackListWidget(self)
        box.addWidget(SelectionBar(self.list))
        box.addWidget(self.list, 1)
        # Заготовки строк на месте списка: видно, что запрос ушёл, а не завис
        self._skeleton = Skeleton()
        self._skeleton.hide()
        box.addWidget(self._skeleton, 1)
        return page

    @property
    def lists(self) -> list[TrackListWidget]:
        """Все списки раздела — для общей проводки действий в главном окне."""
        return [self.list] + [widget for _label, widget in self._slots]

    # ---------- переключение вида ----------
    def show_feed(self) -> None:
        """Вернуться к ленте. Тот же запрос после возврата ищется заново."""
        self._debounce.stop()
        self._query = ''
        self._gen += 1          # ответ прошлого поиска рисовать уже некуда
        self._stack.setCurrentIndex(VIEW_FEED)
        self._back_btn.hide()
        self._tabs_row.hide()
        self._actions.hide()
        self.reload()

    def _show_results(self) -> None:
        self._stack.setCurrentIndex(VIEW_RESULTS)
        self._back_btn.show()
        self._tabs_row.show()
        self._actions.show()

    # ---------- лента ----------
    def reload(self) -> None:
        """Собрать ленту при переходе в раздел. Свежую заново не пересобираем."""
        self._load_mine()
        self._load_sections()

    def refresh_feed(self) -> None:
        """Обновить вручную: срок годности прошлого ответа сбрасываем сами.

        Забываем разобранное на нашей стороне, иначе кнопка вернула бы ту же
        ленту из кэша. Сессию не сбрасываем — куки уже прочитаны и годны."""
        if self._sections_busy or self._mine_busy:
            return
        self._mine_at = 0.0
        self._sections_at = 0.0
        if self._discovery is not None:
            self._discovery.forget('home', 'playlists', 'liked')
        self.reload()

    def _update_busy(self) -> None:
        """Пока лента едет, обновлять нечего: кнопка говорит, что занята."""
        busy = self._sections_busy or self._mine_busy
        self._feed_retry.setEnabled(not busy)
        self._feed_retry.setText('Обновляю…' if busy else 'Обновить ленту')

    def _load_mine(self) -> None:
        discovery = self._discovery
        if discovery is None or self._mine_busy:
            return
        if self._mine_at and time.monotonic() - self._mine_at < FEED_TTL:
            return
        self._mine_busy = True
        self._update_busy()

        def work():
            playlists = discovery.playlists(MY_LIMIT)
            # Про вход спрашиваем после запроса: до него сессии ещё нет
            return discovery.authorized, playlists

        def on_done(result, error):
            self._mine_busy = False
            self._update_busy()
            if error:
                logger.info('YouTube: своё не загрузилось (%s)', error)
                self._mine_hint.setText('Личные плейлисты не загрузились, попробуйте обновить')
                self._mine_hint.show()
                return
            self._mine_at = time.monotonic()
            authorized, playlists = result
            self._show_mine(bool(authorized), list(playlists or ()))

        run_async(work, on_done)

    def _show_mine(self, authorized: bool, playlists: list) -> None:
        self._mine_row.clear()
        if authorized:
            liked = QPushButton('Мне понравилось')
            liked.setObjectName('secondary')
            liked.clicked.connect(self._open_liked)
            self._mine_row.add(liked)
        for entry in playlists[:MY_LIMIT]:
            title = (entry.get('title') or '').strip()
            playlist_id = (entry.get('id') or '').strip()
            if not title or not playlist_id:
                continue
            button = QPushButton(title)
            button.setObjectName('secondary')
            subtitle = (entry.get('subtitle') or '').strip()
            if subtitle:
                button.setToolTip(subtitle)
            button.clicked.connect(
                lambda _checked=False, name=title, pid=playlist_id:
                self._open_playlist(name, pid))
            self._mine_row.add(button)
        self._mine_row.add_stretch()
        has_any = authorized or bool(playlists)
        self._mine_label.setVisible(has_any)
        self._mine_row.setVisible(has_any)
        if not authorized:
            # Своего без входа не бывает — говорим прямо, а не показываем чужое
            self._mine_hint.setText('Своё на YouTube появится, когда в настройках выбран '
                                    'браузер с выполненным входом в YouTube')
            self._mine_hint.show()
        elif not playlists:
            self._mine_hint.setText('Плейлистов на YouTube пока нет, '
                                    'здесь только «Мне понравилось»')
            self._mine_hint.show()
        else:
            self._mine_hint.hide()

    def _sections_source(self):
        """Откуда брать подборки. Рекомендатель ещё и вычищает скрытое."""
        if self._recommender is not None:
            return self._recommender.sections
        if self._discovery is not None:
            return self._discovery.home
        return None

    def _load_sections(self) -> None:
        source = self._sections_source()
        if source is None:
            self._feed_hint.setText('Лента YouTube недоступна, остаётся поиск')
            self._feed_hint.show()
            self._feed_skeleton.hide()
            self._feed_retry.setEnabled(False)
            return
        if self._sections_busy:
            return
        filled = self._sections_shown > 0 or self._mixes_shown > 0
        if filled and time.monotonic() - self._sections_at < FEED_TTL:
            return
        self._sections_busy = True
        self._update_busy()
        self._feed_hint.setText('Собираю ленту YouTube…')
        self._feed_hint.show()
        if not filled:
            self._feed_skeleton.show()
        discovery = self._discovery

        def work():
            # Сначала треки, потом плитки: обе части приходят одним ответом
            # главной, и второй вызов заберёт уже разобранное из кэша
            sections = source(FEED_SECTION_LIMIT)
            mixes = discovery.mixes(MIX_PER_SHELF, MIX_SLOTS) if discovery else []
            return sections, mixes

        def on_done(result, error):
            self._sections_busy = False
            self._update_busy()
            self._feed_skeleton.hide()
            if error:
                logger.warning('YouTube: лента не загрузилась: %s', error)
                self._feed_hint.setText('Лента не загрузилась, попробуйте позже')
                self._feed_hint.show()
                return
            self._sections_at = time.monotonic()
            sections, mixes = result
            self._show_mixes(mixes or [])
            self._show_sections(sections or [])

        run_async(work, on_done)

    def _show_mixes(self, shelves) -> None:
        """Разложить плитки подборок по полкам: заголовок YouTube и его плитки."""
        shelves = list(shelves)
        shown = 0
        for index, (label, row) in enumerate(self._mix_slots):
            shelf = shelves[index] if index < len(shelves) else None
            row.clear()
            if shelf is None or not shelf.mixes:
                label.hide()
                row.hide()
                continue
            for mix in shelf.mixes[:MIX_PER_SHELF]:
                tile = self._mix_tile(mix)
                if tile is not None:
                    row.add(tile)
            row.add_stretch()
            label.setText(shelf.title)
            label.show()
            row.show()
            shown += 1
        self._mixes_shown = shown

    def _mix_tile(self, mix: dict) -> QToolButton | None:
        """Плитка подборки: обложка, название и открытие по нажатию."""
        title = (mix.get('title') or '').strip()
        playlist_id = (mix.get('id') or '').strip()
        if not title or not playlist_id:
            return None
        button = QToolButton()
        button.setObjectName('tile')
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(MIX_COVER, MIX_COVER))
        button.setFixedWidth(MIX_WIDTH)
        button.setCursor(Qt.PointingHandCursor)
        short = title if len(title) <= MIX_TITLE_CHARS else title[:MIX_TITLE_CHARS - 1] + '…'
        button.setText(short)
        subtitle = (mix.get('subtitle') or '').strip()
        button.setToolTip(f'{title}\n{subtitle}' if subtitle else title)
        button.setIcon(QIcon(covers.placeholder(MIX_COVER)))
        cover = mix.get('cover') or ''
        if cover:
            covers.load(cover, self._cover_setter(button))
        button.clicked.connect(
            lambda _checked=False, name=title, pid=playlist_id:
            self._open_playlist(name, pid))
        return button

    @staticmethod
    def _cover_setter(button: QToolButton):
        def apply(_url, pixmap) -> None:
            try:
                button.setIcon(QIcon(covers.rounded(pixmap, MIX_COVER)))
            except RuntimeError:
                pass  # плитку уже пересобрали, картинке некуда встать
        return apply

    def _show_sections(self, sections) -> None:
        sections = list(sections)
        shown = 0
        for index, (label, widget) in enumerate(self._slots):
            section = sections[index] if index < len(sections) else None
            if section is None or not section.tracks:
                label.hide()
                widget.hide()
                widget.set_tracks([])
                continue
            title = section.title
            if not section.genuine:
                # Честно: это не рекомендация, а поиск вместо неё
                title = f'{title} · {section.label}'
            tracks = list(section.tracks[:FEED_SECTION_LIMIT])
            widget.set_tracks(tracks)
            widget.setToolTip(section.label)
            # Высота ровно по содержимому: лента и так прокручивается целиком,
            # вложенная полоса прокрутки в каждой подборке только мешала бы
            widget.setFixedHeight(len(tracks) * ROW_HEIGHT + 4)
            label.setText(title)
            label.show()
            widget.show()
            shown += 1
        # Считаем по данным, а не по видимости: раздел мог быть ещё не показан
        self._sections_shown = shown
        total = shown + self._mixes_shown
        self._feed_hint.setVisible(not total)
        if not total:
            self._feed_hint.setText('Подборок пока нет, найдите что-нибудь через поиск')

    # ---------- готовые списки ----------
    def _open_liked(self) -> None:
        discovery = self._discovery
        if discovery is None:
            return
        self._open_list('Мне понравилось', lambda: discovery.liked(LIKED_LIMIT))

    def _open_playlist(self, title: str, playlist_id: str) -> None:
        discovery = self._discovery
        if discovery is None or not playlist_id:
            return
        self._open_list(title,
                        lambda: discovery.playlist_tracks(playlist_id, PLAYLIST_LIMIT))

    def _open_list(self, title: str, work) -> None:
        """Показать готовый список на месте поиска: плейлист, «Мне понравилось»."""
        self._debounce.stop()
        self._query = ''      # это не поиск: следующий запрос должен отработать
        self._gen += 1
        gen = self._gen
        self._show_results()
        self._retry_btn.hide()
        self._status.setText(f'Открываю «{title}»…')
        self._set_loading(True)

        def on_done(tracks, error):
            if gen != self._gen:
                return   # успели уйти в другое место, пока ходили в сеть
            self._set_loading(False)
            if error:
                logger.warning('YouTube: «%s» не открылся: %s', title, error)
                self._status.setText(f'Не получилось открыть «{title}»: {error}')
                return
            tracks = list(tracks or ())
            self.list.set_tracks(tracks)
            self._status.setText(f'{title}: {len(tracks)}' if tracks
                                 else f'В «{title}» пусто')

        run_async(work, on_done)

    # ---------- поиск ----------
    def focus_search(self) -> None:
        self._input.setFocus()
        self._input.selectAll()

    def search_for(self, text: str) -> None:
        """Поиск снаружи — например, из расширения браузера или с главной."""
        self._query = ''   # тот же запрос снаружи должен искаться заново
        self._input.setText(text)
        self._start_search()

    def set_mode(self, mode: str) -> None:
        if mode not in self._tabs:
            return
        self._mode = mode
        for name, button in self._tabs.items():
            button.setChecked(name == mode)
        self._input.setPlaceholderText(MODE_HINTS[mode])
        if self._input.text().strip():
            self._query = ''  # тот же запрос, но в другом разделе — искать заново
            self._start_search()

    def _on_text_changed(self, text: str) -> None:
        # Ссылку разбираем только по нажатию: пока её вставляют, каждая правка
        # запускала бы разбор заново
        if text.strip() and not looks_like_url(text.strip()):
            self._debounce.start()
        else:
            self._debounce.stop()

    def _start_search(self) -> None:
        self._debounce.stop()
        query = self._input.text().strip()
        if not query:
            # Пустая строка — это не поиск, а возврат к тому, с чего раздел начинался
            if self._stack.currentIndex() == VIEW_RESULTS:
                self.show_feed()
            return
        if query == self._query:
            return
        self._query = query
        self._gen += 1
        gen = self._gen
        self._show_results()
        self._search_btn.setEnabled(False)
        self._retry_btn.hide()
        self._status.setText('Ищу на YouTube…')
        self._set_loading(True)
        cookies = self._settings().get('cookies_browser')
        mode = self._mode

        def work():
            if looks_like_url(query):
                return ('entries', ytdlp_engine.extract_entries(query, cookies))
            return ('tracks', self._search(query, mode, cookies))

        def on_done(result, error):
            if gen != self._gen:
                return  # запрос успел смениться, пока ходили в сеть
            self._search_btn.setEnabled(True)
            self._set_loading(False)
            if error:
                logger.warning('Поиск на YouTube не удался: %s', error)
                self._status.setText(f'Не получилось найти: {error}')
                self._retry_btn.show()
                return
            kind, payload = result
            if kind == 'entries':
                self._show_entries(payload or [])
            else:
                self._show_tracks(payload or [])

        run_async(work, on_done)

    def _set_loading(self, busy: bool) -> None:
        """Во время запроса вместо списка — заготовки строк."""
        self._skeleton.setVisible(busy)
        self.list.setVisible(not busy)

    def _retry(self) -> None:
        """Повторить последний запрос — тот же текст ищем заново."""
        self._query = ''
        self._start_search()

    def _search(self, query: str, mode: str, cookies) -> list:
        """Фоновая часть поиска. Ходит в сеть, поэтому только из run_async."""
        if self._discovery is None:
            entries = ytdlp_engine.search(query, RESULT_LIMIT, cookies)
            return [from_youtube(entry) for entry in entries if entry.get('id')]
        if mode == MODE_MUSIC:
            return self._discovery.search(query, RESULT_LIMIT, music_only=True)
        if mode == MODE_VIDEO:
            return self._discovery.search(query, RESULT_LIMIT, music_only=False)
        # «Всё»: сначала музыка, следом обычные ролики без повторов
        music = self._discovery.search(query, RESULT_LIMIT // 2, music_only=True)
        seen = {track.uid for track in music}
        rest = [track for track in self._discovery.search(query, RESULT_LIMIT, music_only=False)
                if track.uid not in seen]
        return music + rest

    def _show_tracks(self, tracks: list) -> None:
        self.list.set_tracks(tracks)
        self._status.setText(f'Найдено: {len(tracks)}' if tracks else 'Ничего не нашлось')

    def _show_entries(self, entries: list) -> None:
        tracks = [from_youtube(entry) for entry in entries if entry.get('id')]
        self.list.set_tracks(tracks)
        if not tracks:
            self._status.setText('Ничего не нашлось')
            return
        playlist = entries[0].get('playlist_title')
        if playlist:
            self._status.setText(f'Плейлист «{playlist}» : {len(tracks)} шт.')
        else:
            self._status.setText(f'Найдено: {len(tracks)}')

    # ---------- действия над выбранным ----------
    def _selected(self) -> list:
        chosen = self.list.selected_tracks()
        if chosen:
            return chosen
        return self.list.tracks()[:1]

    def _play_selected(self) -> None:
        chosen = self.list.selected_tracks()
        if chosen:
            self.list.play_requested.emit(chosen, 0)
        elif self.list.tracks():
            self.list.play_requested.emit(self.list.tracks(), 0)

    def _enqueue_selected(self) -> None:
        chosen = self._selected()
        if chosen:
            self.list.enqueue_requested.emit(chosen, False)

    def _add_vk_selected(self) -> None:
        chosen = self._selected()
        if chosen:
            self.list.add_vk_requested.emit(chosen)

    def _download_selected(self) -> None:
        chosen = self._selected()
        if chosen:
            self.list.download_requested.emit(chosen)
