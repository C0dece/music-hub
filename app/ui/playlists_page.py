"""Раздел «Плейлисты»: подборки Music Hub, плейлисты VK и плейлисты YouTube.

Слева - списки с фильтром по источнику, справа - треки. Плейлисты VK и YouTube
показываются как есть; свои подборки живут в местной базе и могут быть связаны с
плейлистом VK. Связанную подборку можно синхронизировать: то, что уже есть в VK,
добавляется в плейлист методом `audio.addToPlaylist`, а то, чего в VK ещё нет,
переносится кнопкой «Перенести в VK» - тем же путём, что и везде в приложении
(сначала поиск в VK, скачивание только если записи там нет).

Плейлист YouTube открывается прямо здесь, без скачивания: сначала пробуем
внутренний API (он же отдаёт личные плейлисты пользователя, если есть вход),
потом - обычный `yt-dlp` со списком без загрузки. Личные плейлисты без входа не
изображаются: их просто нет.

Чего API текущего токена не умеет, мы не изображаем: порядок треков в VK
`audio.reorder` недоступен, поэтому перестановка остаётся местной, а состав
плейлиста YouTube отсюда не меняется - прав на это у нас нет."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QSplitter, QTabBar, QVBoxLayout, QWidget,
)

from ..core.async_task import run_async
from ..core.store import FAVORITES_EXT_ID
from ..core.track import SOURCE_VK, Track, from_vk
from ..core.youtube.discovery import playlist_id_from
from .flow_layout import FlowRow
from .track_list import SelectionBar, TrackListWidget
from .widgets import ElidedLabel, EmptyState

logger = logging.getLogger(__name__)

KIND_ROLE = Qt.UserRole + 1   # 'local', 'vk', 'youtube', 'header' или 'note'
DATA_ROLE = Qt.UserRole + 2   # словарь плейлиста

# Фильтр источников: подпись вкладки и то, что она оставляет в списке
SOURCES = (('Все', ''), ('Music Hub', 'local'), ('VK', 'vk'), ('YouTube', 'youtube'))
HEADERS = {'Music Hub': 'local', 'VK': 'vk', 'YouTube': 'youtube'}
YT_LIMIT = 200


class PlaylistsPage(QWidget):
    """Плейлисты VK и подборки Music Hub с максимально возможной синхронизацией."""

    add_vk_requested = Signal(object)   # список треков - общий путь «+ VK»
    status_message = Signal(str)

    def __init__(self, client_provider, store, discovery=None, parent=None):
        super().__init__(parent)
        self._client_provider = client_provider
        self._store = store
        self._discovery = discovery
        self._current: dict | None = None
        self._current_kind = ''
        self._loading = False
        self._gen = 0            # ответы на прежний список рисовать уже некуда

        self._build_ui()

    def _build_ui(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_box = QVBoxLayout(left)
        # Отступ справа - под ручку сплиттера: без него вкладки источников
        # упирались в неё и первая буква «VK» пропадала под захватом
        left_box.setContentsMargins(0, 0, 8, 0)
        left_box.setSpacing(8)
        caption = QLabel('Плейлисты')
        caption.setObjectName('h2')
        left_box.addWidget(caption)
        self._sources = QTabBar()
        self._sources.setExpanding(False)
        self._sources.setDrawBase(False)
        # Вкладок четыре, а колонка узкая: подписи сокращаются многоточием,
        # но минимальную ширину окна не задирают
        self._sources.setUsesScrollButtons(False)
        self._sources.setElideMode(Qt.ElideNone)
        for text, _key in SOURCES:
            self._sources.addTab(text)
        self._sources.currentChanged.connect(lambda _index: self._apply_filter())
        left_box.addWidget(self._sources)
        self._playlists = QListWidget()
        self._playlists.setFrameShape(QListWidget.NoFrame)
        self._playlists.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._playlists.setTextElideMode(Qt.ElideRight)
        self._playlists.setWordWrap(False)
        self._playlists.currentItemChanged.connect(self._on_playlist_selected)
        self._playlists.setContextMenuPolicy(Qt.CustomContextMenu)
        self._playlists.customContextMenuRequested.connect(self._playlist_menu)
        left_box.addWidget(self._playlists, 1)

        left_actions = FlowRow(spacing=6, overflow=True)
        for text, slot in (('Создать', self._create_local),
                           ('По ссылке', self._open_by_link),
                           ('Обновить', self.reload)):
            button = QPushButton(text)
            button.setObjectName('secondary')
            button.clicked.connect(slot)
            left_actions.add(button)
        left_box.addWidget(left_actions)
        splitter.addWidget(left)

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(8, 0, 0, 0)
        right_box.setSpacing(8)
        # Пока плейлист не выбран, об этом уже сказано посреди колонки -
        # второй раз повторять заголовком незачем
        self._title = ElidedLabel('')
        self._title.setObjectName('h2')
        right_box.addWidget(self._title)
        self._hint = ElidedLabel('')
        self._hint.setObjectName('hint')
        right_box.addWidget(self._hint)

        self.list = TrackListWidget(self)
        right_box.addWidget(SelectionBar(self.list))
        right_box.addWidget(self.list, 1)

        self._empty = EmptyState(
            'queue', 'Плейлист не выбран',
            'Выберите список слева, треки появятся здесь. '
            'Свой можно создать кнопкой «Создать» или открыть по ссылке.')
        right_box.addWidget(self._empty, 1)
        # Пока ничего не выбрано, показываем подсказку вместо пустой рамки
        self.list.hide()

        actions = self._actions = FlowRow(spacing=8, overflow=True)
        actions.add_stretch()
        self._buttons = {}
        for key, text, secondary in (('play', 'Играть', False),
                                     ('enqueue', 'В очередь', True),
                                     ('up', 'Вверх', True),
                                     ('down', 'Вниз', True),
                                     ('remove', 'Убрать', True),
                                     ('to_vk', 'Перенести в VK', True),
                                     ('sync', 'Синхронизировать', True)):
            button = QPushButton(text)
            if secondary:
                button.setObjectName('secondary')
            if key == 'to_vk':
                button.setToolTip('Найти записи в VK и добавить в «Мою музыку»; '
                                  'скачивание только для того, чего в VK нет')
            if key == 'sync':
                # Полное «Синхронизировать с VK» делало всю колонку шире 300 точек
                # и не давало окну сжаться до 620
                button.setToolTip('Синхронизировать плейлист с «Моей музыкой» VK')
            button.clicked.connect(getattr(self, f'_on_{key}'))
            actions.add(button)
            self._buttons[key] = button
        right_box.addWidget(actions)
        # Ряд действий над несуществующим плейлистом обманывает: показываем его
        # вместе со списком треков
        actions.hide()
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(8)
        # Колонку можно сузить, но не схлопнуть в ноль: пустая половина экрана с
        # невидимой ручкой выглядит поломкой
        splitter.setChildrenCollapsible(False)
        left.setMinimumWidth(170)
        right.setMinimumWidth(260)
        # 300 - чтобы в колонку помещались и вкладки источников («Все · Music Hub ·
        # VK · YouTube»), и все три кнопки под списком в одну строку. При 280 строке
        # оставалось 272 px против нужных 288, и «Обновить» пряталась под «⋯» при
        # любом размере окна - даже когда справа пустовала половина экрана
        splitter.setSizes([300, 600])
        box.addWidget(splitter, 1)
        self._update_buttons()

    # ---------- списки плейлистов ----------
    def show_favorites(self) -> None:
        """Открыть «Любимое».

        Отдельного раздела «Избранное» больше нет: избранное и так хранится
        системным плейлистом, а страница у него была та же самая - список треков
        и те же кнопки. Сюда приходят все переходы «в избранное»."""
        if not self._playlists.count():
            self.reload()
        for row in range(self._playlists.count()):
            item = self._playlists.item(row)
            if (item.data(KIND_ROLE) == 'local'
                    and (item.data(DATA_ROLE) or {}).get('ext_id') == FAVORITES_EXT_ID):
                self._sources.setCurrentIndex(0)   # фильтр мог прятать свои подборки
                self._playlists.setCurrentItem(item)
                self._playlists.scrollToItem(item)
                return

    def refresh_current(self) -> None:
        """Перечитать открытую подборку, не трогая список слева.

        Нужно, когда трек убрали из «Любимого», не уходя со страницы: полный
        reload сбросил бы выбор и увёл бы человека из плейлиста."""
        item = self._playlists.currentItem()
        if item is not None and item.data(KIND_ROLE) == 'local':
            self._show_local()

    def reload(self) -> None:
        """Перечитать оба списка: свои подборки - сразу, VK - в фоне."""
        self._playlists.clear()
        self._add_header('Music Hub')
        for row in (self._store.playlists() if self._store is not None else []):
            self._add_playlist(row['title'], 'local', row)
        self._gen += 1
        self._add_header('VK')
        client = self._client_provider()
        if client is None:
            self._add_note('Войдите в VK, чтобы увидеть плейлисты')
        else:
            self._add_note('Загружаю…')
        self._load_youtube_playlists()
        self._apply_filter()
        if client is None:
            return

        gen = self._gen

        def on_done(playlists, error):
            # Список мог обновиться ещё раз, пока ходили в сеть
            if gen != self._gen:
                return
            self._drop_note('Загружаю…', 'VK')
            if error:
                logger.warning('Плейлисты VK не загрузились: %s', error)
                self._add_note(f'VK не отдал плейлисты: {error}')
                return
            for row in playlists or []:
                title = row['title']
                if row.get('count'):
                    title = f"{title}  ({row['count']})"
                self._add_playlist(title, 'vk', row, before='YouTube')
            self._apply_filter()

        run_async(lambda: client.get_playlists(), on_done)

    def _load_youtube_playlists(self) -> None:
        """Личные плейлисты YouTube Music - только при живом входе в аккаунт."""
        self._add_header('YouTube')
        discovery = self._discovery
        if discovery is None:
            self._add_note('Плейлисты YouTube недоступны')
            return
        self._add_note('Загружаю…')
        gen = self._gen

        def on_done(rows, error):
            if gen != self._gen:
                return                      # список успели перечитать заново
            self._drop_note('Загружаю…', 'YouTube')
            if error:
                logger.info('Плейлисты YouTube не загрузились: %s', error)
                self._add_note('YouTube не отдал плейлисты')
                self._apply_filter()
                return
            if not rows:
                # Без входа личных плейлистов нет - так и пишем, не выдумывая чужих
                self._add_note('Нет входа в YouTube, плейлист можно открыть по ссылке')
            for row in rows or []:
                title = row.get('title') or 'Плейлист'
                if row.get('count'):
                    title = '{}  ({})'.format(title, row['count'])
                self._add_playlist(title, 'youtube', row)
            self._apply_filter()

        run_async(lambda: discovery.playlists(30), on_done)

    def _drop_note(self, text: str, section: str = '') -> None:
        """Убрать строку-заглушку - свою для каждого раздела."""
        current = ''
        for index in range(self._playlists.count() - 1, -1, -1):
            item = self._playlists.item(index)
            if item.data(KIND_ROLE) != 'note':
                continue
            current = self._section_of(index)
            if current == section and item.text().strip() == text:
                self._playlists.takeItem(index)

    def _section_of(self, row: int) -> str:
        """Заголовок, под которым лежит строка списка."""
        for index in range(row, -1, -1):
            item = self._playlists.item(index)
            if item.data(KIND_ROLE) == 'header':
                return item.text()
        return ''

    def _apply_filter(self) -> None:
        """Показать только выбранный источник; «Все» - показать всё."""
        wanted = SOURCES[max(0, self._sources.currentIndex())][1]
        section = ''
        for row in range(self._playlists.count()):
            item = self._playlists.item(row)
            kind = item.data(KIND_ROLE)
            if kind == 'header':
                section = HEADERS.get(item.text(), '')
                item.setHidden(bool(wanted) and section != wanted)
            elif kind == 'note':
                item.setHidden(bool(wanted) and section != wanted)
            else:
                item.setHidden(bool(wanted) and kind != wanted)

    def _add_header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        item.setData(KIND_ROLE, 'header')
        self._playlists.addItem(item)

    def _add_note(self, text: str) -> None:
        item = QListWidgetItem(f'  {text}')
        item.setFlags(Qt.NoItemFlags)
        item.setData(KIND_ROLE, 'note')
        self._playlists.addItem(item)

    def _add_playlist(self, title: str, kind: str, data: dict, before: str = '') -> None:
        item = QListWidgetItem(f'  {title}')
        item.setData(KIND_ROLE, kind)
        item.setData(DATA_ROLE, data)
        if kind == 'local' and data.get('vk_playlist_id'):
            item.setToolTip('Связан с плейлистом VK, доступна синхронизация')
        if kind == 'youtube':
            item.setToolTip('Откроется здесь, без скачивания')
        if before:
            for row in range(self._playlists.count()):
                head = self._playlists.item(row)
                if head.data(KIND_ROLE) == 'header' and head.text() == before:
                    self._playlists.insertItem(row, item)
                    self._apply_filter()
                    return
        self._playlists.addItem(item)

    # ---------- треки выбранного плейлиста ----------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Заголовок и подсказка справа - справка о выбранном плейлисте. В низком
        # окне они отнимали у списка две строки из четырёх; название плейлиста
        # и так подсвечено слева
        tight = self.height() < 240
        self._title.setVisible(not tight)
        self._hint.setVisible(not tight and bool(self._hint.text()))

    def _on_playlist_selected(self, item, _previous) -> None:
        if item is None or item.data(KIND_ROLE) in (None, 'header', 'note'):
            return
        self._current = item.data(DATA_ROLE)
        self._current_kind = item.data(KIND_ROLE)
        self._empty.hide()
        self.list.show()
        self._actions.show()
        self._title.setText(self._current.get('title') or 'Плейлист')
        self._update_buttons()
        if self._current_kind == 'local':
            self._show_local()
        elif self._current_kind == 'youtube':
            self._load_youtube_tracks()
        else:
            self._load_vk_tracks()

    def _show_local(self) -> None:
        tracks = self._store.playlist_tracks(self._current['id'])
        self.list.set_tracks(tracks)
        linked = bool(self._current.get('vk_playlist_id'))
        missing = sum(1 for t in tracks if not self._in_vk(t))
        parts = [f'Треков: {len(tracks)}']
        parts.append('связан с плейлистом VK' if linked else 'не связан с VK')
        if missing:
            parts.append(f'ещё не в VK: {missing}')
        parts.append('порядок хранится локально')
        self._hint.setText(' · '.join(parts))

    def _load_vk_tracks(self) -> None:
        client = self._client_provider()
        if client is None:
            return
        playlist = dict(self._current)
        self._hint.setText('Загружаю треки…')
        self.list.set_tracks([])
        self._loading = True

        def on_done(rows, error):
            self._loading = False
            if self._current is not playlist and self._current != playlist:
                return  # пока грузили, выбрали другой плейлист
            if error:
                self._hint.setText(f'Не открылся: {error}')
                return
            tracks = [from_vk(row) for row in rows or []]
            self.list.set_tracks(tracks)
            self._hint.setText(f'Треков: {len(tracks)} · плейлист VK')

        run_async(lambda: client.get_playlist_tracks(playlist), on_done)

    def _load_youtube_tracks(self) -> None:
        """Состав плейлиста YouTube. Ничего не скачиваем - только список."""
        discovery = self._discovery
        playlist = dict(self._current or {})
        if discovery is None or not playlist.get('id'):
            self._hint.setText('Плейлисты YouTube недоступны')
            return
        self._hint.setText('Открываю плейлист…')
        self.list.set_tracks([])
        self._loading = True
        self._gen += 1
        gen = self._gen

        def on_done(tracks, error):
            if gen != self._gen:
                return          # выбрали другой плейлист, пока ходили в сеть
            self._loading = False
            if error:
                logger.info('Плейлист YouTube не открылся: %s', error)
                self._hint.setText('Плейлист не открылся, попробуйте ещё раз')
                return
            self.list.set_tracks(tracks or [])
            missing = sum(1 for track in (tracks or []) if not self._in_vk(track))
            parts = ['Треков: {}'.format(len(tracks or [])), 'плейлист YouTube']
            if missing:
                parts.append('ещё не в VK: {}'.format(missing))
            self._hint.setText(' · '.join(parts))
            self._update_buttons()

        run_async(lambda: discovery.playlist_tracks(playlist['id'], YT_LIMIT), on_done)

    def _open_by_link(self) -> None:
        """Спросить ссылку на плейлист YouTube и открыть её."""
        link, ok = QInputDialog.getText(
            self, 'Плейлист по ссылке',
            'Ссылка на плейлист YouTube (или его номер list=…):')
        if ok and link.strip():
            self.open_playlist_link(link)

    def open_playlist_link(self, link: str) -> bool:
        """Открыть плейлист YouTube по ссылке - без входа и без скачивания."""
        playlist_id = playlist_id_from(link)
        if not playlist_id:
            self.status_message.emit('В ссылке нет номера плейлиста (list=…)')
            return False
        self._current = {'id': playlist_id, 'title': 'Плейлист YouTube'}
        self._empty.hide()
        self.list.show()
        self._actions.show()
        self._current_kind = 'youtube'
        self._playlists.setCurrentItem(None)
        self._title.setText('Плейлист YouTube')
        self._update_buttons()
        self._load_youtube_tracks()
        return True

    # ---------- действия ----------
    def _update_buttons(self) -> None:
        local = self._current_kind == 'local'
        for key in ('up', 'down', 'remove'):
            self._buttons[key].setEnabled(local)
        self._buttons['sync'].setEnabled(local)
        for key in ('play', 'enqueue'):
            self._buttons[key].setEnabled(self._current is not None)
        # Переносить есть смысл только то, чего в VK ещё нет
        self._buttons['to_vk'].setEnabled(bool(self._missing_in_vk()))

    def _selected_or_all(self) -> list[Track]:
        return self.list.selected_tracks() or self.list.tracks()

    def _missing_in_vk(self) -> list[Track]:
        return [track for track in self._selected_or_all() if not self._in_vk(track)]

    def _on_to_vk(self) -> None:
        """Перенести плейлист в VK общим путём: поиск в VK, скачивание - крайний случай."""
        missing = self._missing_in_vk()
        if not missing:
            self.status_message.emit('Все записи плейлиста уже есть в VK')
            return
        self.add_vk_requested.emit(missing)

    def _on_play(self) -> None:
        selected = self.list.selected_tracks()
        tracks = self.list.tracks()
        if selected:
            self.list.play_requested.emit(tracks, tracks.index(selected[0]))
        elif tracks:
            self.list.play_requested.emit(tracks, 0)

    def _on_enqueue(self) -> None:
        tracks = self._selected_or_all()
        if tracks:
            self.list.enqueue_requested.emit(tracks, False)

    def _on_up(self) -> None:
        self._move(-1)

    def _on_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        rows = [self.list.row(i) for i in self.list.selectedItems()]
        if self._current_kind != 'local' or len(rows) != 1:
            return
        row = rows[0]
        target = row + delta
        tracks = self.list.tracks()
        if not 0 <= target < len(tracks):
            return
        tracks[row], tracks[target] = tracks[target], tracks[row]
        self._store.set_playlist_order(self._current['id'], [t.uid for t in tracks])
        self.list.set_tracks(tracks)
        self.list.setCurrentRow(target)

    def _on_remove(self) -> None:
        if self._current_kind != 'local':
            return
        for track in self.list.selected_tracks():
            self._store.remove_from_playlist(self._current['id'], track.uid)
        self._show_local()

    # ---------- синхронизация ----------
    def _in_vk(self, track: Track) -> bool:
        if track.source == SOURCE_VK or track.in_vk:
            return True
        return self._store is not None and self._store.has_mapping(track.uid)

    def _vk_audio_id(self, track: Track) -> str:
        """Строка «owner_id_audio_id» для audio.addToPlaylist."""
        if track.vk_owner_id and track.vk_audio_id:
            return f'{track.vk_owner_id}_{track.vk_audio_id}'
        target = self._store.mapping_target(track.uid) if self._store is not None else None
        if target is not None and target.vk_owner_id and target.vk_audio_id:
            return f'{target.vk_owner_id}_{target.vk_audio_id}'
        return ''

    def _on_sync(self) -> None:
        if self._current_kind != 'local' or self._current is None:
            return
        client = self._client_provider()
        if client is None:
            self.status_message.emit('Для синхронизации нужен вход в VK')
            return
        playlist = dict(self._current)
        tracks = self._store.playlist_tracks(playlist['id'])
        if not tracks:
            self.status_message.emit('Плейлист пуст, синхронизировать нечего')
            return

        missing = [t for t in tracks if not self._vk_audio_id(t)]
        if missing:
            answer = QMessageBox.question(
                self, 'Синхронизация',
                f'В VK ещё нет записей: {len(missing)}.\n\n'
                'Перенести их в «Мою музыку» сейчас? После переноса нажмите '
                '«Синхронизировать» ещё раз, и они попадут в плейлист.',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                self.add_vk_requested.emit(missing)

        audio_ids = [aid for aid in (self._vk_audio_id(t) for t in tracks) if aid]
        if not audio_ids:
            return

        if not playlist.get('vk_playlist_id'):
            self._create_vk_playlist(playlist, audio_ids)
            return
        self._push_to_vk(client, int(playlist['vk_playlist_id']), audio_ids)

    def _create_vk_playlist(self, playlist: dict, audio_ids: list[str]) -> None:
        client = self._client_provider()
        title = playlist.get('title') or 'Music Hub'
        self.status_message.emit(f'Создаю плейлист «{title}» в VK…')

        def on_done(created, error):
            if error or not created or not created.get('id'):
                self.status_message.emit(f'VK не создал плейлист: {error or "пустой ответ"}')
                return
            self._store.link_playlist(playlist['id'], created.get('owner_id'),
                                      created['id'], created.get('access_hash') or '')
            self._push_to_vk(client, int(created['id']), audio_ids)
            self.reload()

        run_async(lambda: client.create_playlist(title), on_done)

    def _push_to_vk(self, client, vk_playlist_id: int, audio_ids: list[str]) -> None:
        self.status_message.emit('Отправляю треки в плейлист VK…')

        def work():
            # VK не жалуется на повторное добавление, поэтому отправляем список целиком:
            # узнать состав плейлиста заранее нечем - audio.get токену закрыт
            client.add_to_playlist(vk_playlist_id, audio_ids)
            return len(audio_ids)

        def on_done(count, error):
            if error:
                self.status_message.emit(f'VK не принял треки: {error}')
                return
            self.status_message.emit(f'Плейлист VK обновлён, отправлено записей: {count}')
            if self._current_kind == 'local':
                self._show_local()

        run_async(work, on_done)

    # ---------- свои подборки ----------
    def _create_local(self) -> None:
        title, ok = QInputDialog.getText(self, 'Новая подборка', 'Название:')
        if ok and title.strip():
            self._store.create_playlist(title.strip())
            self.reload()

    def _playlist_menu(self, point) -> None:
        item = self._playlists.itemAt(point)
        if item is None or item.data(KIND_ROLE) not in ('local', 'vk', 'youtube'):
            return
        kind = item.data(KIND_ROLE)
        data = item.data(DATA_ROLE)
        menu = QMenu(self)
        if kind == 'local':
            menu.addAction('Переименовать', lambda: self._rename_local(data))
            menu.addAction('Удалить подборку', lambda: self._delete_local(data))
            if data.get('vk_playlist_id'):
                menu.addAction('Отвязать от VK',
                               lambda: (self._store.link_playlist(data['id'], None, None, ''),
                                        self.reload()))
        elif kind == 'vk':
            menu.addAction('Скопировать в Music Hub', lambda: self._copy_vk(data))
        else:
            menu.addAction('Открыть', lambda: self._playlists.setCurrentItem(item))
            menu.addAction('Скопировать в Music Hub', lambda: self._copy_youtube(data))
            menu.addAction('Перенести в VK', lambda: self._transfer_youtube(data))
        menu.exec(self._playlists.viewport().mapToGlobal(point))

    def _rename_local(self, data: dict) -> None:
        title, ok = QInputDialog.getText(self, 'Переименовать', 'Название:',
                                         text=data.get('title', ''))
        if ok and title.strip():
            self._store.rename_playlist(data['id'], title.strip())
            self.reload()

    def _delete_local(self, data: dict) -> None:
        answer = QMessageBox.question(
            self, 'Удалить подборку',
            f'Удалить подборку «{data.get("title")}»?\n\n'
            'Треки останутся и в VK, и в библиотеке, удалится только список.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._store.delete_playlist(data['id'])
            self.reload()

    def _copy_vk(self, data: dict) -> None:
        """Сделать из плейлиста VK свою подборку - уже связанную с оригиналом."""
        client = self._client_provider()
        if client is None:
            return
        playlist_id = self._store.create_playlist(
            data.get('title') or 'Плейлист VK', data.get('owner_id'), data.get('id'),
            data.get('access_hash') or '')
        self.status_message.emit('Копирую состав плейлиста…')

        def on_done(rows, error):
            if error:
                self.status_message.emit(f'Не удалось прочитать плейлист: {error}')
                return
            for row in rows or []:
                self._store.add_to_playlist(playlist_id, from_vk(row))
            self.status_message.emit('Подборка создана')
            self.reload()

        run_async(lambda: client.get_playlist_tracks(data), on_done)

    # ---------- плейлисты YouTube ----------
    def _youtube_tracks(self, data: dict, done) -> None:
        """Состав плейлиста YouTube в фоне; `done` зовётся в потоке интерфейса."""
        discovery = self._discovery
        if discovery is None or not data.get('id'):
            self.status_message.emit('Плейлисты YouTube недоступны')
            return
        self.status_message.emit('Читаю плейлист YouTube…')

        def on_done(tracks, error):
            if error or not tracks:
                self.status_message.emit('Плейлист YouTube не открылся')
                return
            done(tracks)

        run_async(lambda: discovery.playlist_tracks(data['id'], YT_LIMIT), on_done)

    def _copy_youtube(self, data: dict) -> None:
        """Сделать из плейлиста YouTube свою подборку. Файлы не скачиваются."""
        if self._store is None:
            return

        def save(tracks):
            playlist_id = self._store.create_playlist(data.get('title') or 'Плейлист YouTube')
            for track in tracks:
                self._store.add_to_playlist(playlist_id, track)
            self.status_message.emit('Подборка создана, треков: {}'.format(len(tracks)))
            self.reload()

        self._youtube_tracks(data, save)

    def _transfer_youtube(self, data: dict) -> None:
        """Перенести плейлист YouTube в VK - тем же общим путём «+ VK»."""
        def transfer(tracks):
            missing = [track for track in tracks if not self._in_vk(track)]
            if not missing:
                self.status_message.emit('Все записи плейлиста уже есть в VK')
                return
            self.add_vk_requested.emit(missing)

        self._youtube_tracks(data, transfer)
