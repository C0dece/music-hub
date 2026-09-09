"""Список треков, общий для всех разделов музыки.

Одна и та же строка нужна в результатах YouTube, в плейлистах, в избранном и на
главной - поэтому список один, а разделы только подключаются к его сигналам.

Строки рисует делегат, а не отдельный виджет на каждую: полторы тысячи треков VK
в виде полутора тысяч QWidget окно не переживёт. Обложка запрашивается лениво -
только когда строка действительно попала на экран."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QStyle, QStyledItemDelegate, QToolTip, QWidget,
)

from ..core.track import SOURCE_VK, Track
from . import covers, player_icons
from .track_actions import TrackActions, build_menu

TRACK_ROLE = Qt.UserRole + 10

COVER = 40
PADDING = 8
ROW_HEIGHT = COVER + PADDING * 2
# Плашка метки: высота под строку списка, отступы по бокам под скругление
BADGE_HEIGHT = 20
BADGE_PAD = 9
# Готовое состояние - кружок со значком: «✓ офлайн» занимало половину свободного
# места строки и читалось как надпись, а не как признак файла
MARK_SIZE = 22
MARK_GLYPH = 13
# Что показывать значком. Остальное остаётся надписью: «сохраняю…» и «файла нет»
# картинкой не передать, там важно именно то, что написано
MARK_ICONS = {
    'офлайн': 'download',
    'скачано': 'download',
    'в моей музыке': 'check',
}
# Режим выбора: сколько держать кнопку, чтобы он включился, и размер галки
HOLD_MS = 450
HOLD_SLIP = 12          # сдвинули мышь сильнее - это уже не удержание
CHECK_SIZE = 22

def format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ''
    if seconds >= 3600:
        return f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'
    return f'{seconds // 60}:{seconds % 60:02d}'


def format_time(ms: int) -> str:
    """То же самое от миллисекунд: в плеере ноль пишем цифрами, а не пустотой."""
    return format_duration(max(0, int(ms)) // 1000) or '0:00'


class TrackDelegate(QStyledItemDelegate):
    """Обложка, название, исполнитель, длительность и метка «в VK» в одной строке."""

    def __init__(self, owner: 'TrackListWidget'):
        super().__init__(owner)
        self._owner = owner

    def sizeHint(self, option, index) -> QSize:
        return QSize(200, ROW_HEIGHT)

    @staticmethod
    def _fill(painter, rect, color: str) -> None:
        """Подсветка строки скруглённой плашкой - как у карточек в остальном окне."""
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(2, 1, -2, -1), 10, 10)
        painter.fillPath(path, QColor(color))

    @staticmethod
    def _draw_check(painter, cover_rect, checked: bool) -> None:
        """Галка поверх обложки: в режиме выбора видно, что отмечено.

        Отдельного столбца под неё не делаем: строки тогда съезжали бы вбок
        при каждом входе в режим и выходе из него."""
        size = CHECK_SIZE
        box = QRect(cover_rect.center().x() - size // 2,
                    cover_rect.center().y() - size // 2, size, size)
        shade = QColor('#0d1017')
        shade.setAlpha(150)
        path = QPainterPath()
        path.addRoundedRect(cover_rect, 8, 8)
        painter.fillPath(path, shade)

        circle = QPainterPath()
        circle.addEllipse(box)
        painter.fillPath(circle, QColor('#4f8cff') if checked else QColor(0, 0, 0, 90))
        painter.setPen(QPen(QColor('#ffffff' if checked else '#c2c8d4'), 1.4))
        painter.drawEllipse(box)
        if checked:
            pen = QPen(QColor('#ffffff'), 2)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            left = QPoint(box.left() + 6, box.center().y())
            mid = QPoint(box.center().x() - 1, box.bottom() - 6)
            right = QPoint(box.right() - 5, box.top() + 6)
            painter.drawLine(left, mid)
            painter.drawLine(mid, right)

    @staticmethod
    def _draw_mark(painter, right: int, rect, icon: str, ink: QColor) -> int:
        """Кружок со значком вместо надписи. Возвращает новую правую границу."""
        size = MARK_SIZE
        top = rect.top() + (rect.height() - size) // 2
        box = QRect(right - size, top, size, size)
        fill = QColor(ink)
        fill.setAlpha(38)
        circle = QPainterPath()
        circle.addEllipse(box)
        painter.fillPath(circle, fill)
        glyph = MARK_GLYPH
        offset = (size - glyph) // 2
        player_icons.draw(icon, ink.name(), glyph).paint(
            painter, box.left() + offset, box.top() + offset, glyph, glyph)
        return right - size - 12

    def paint(self, painter, option, index) -> None:
        track: Track = index.data(TRACK_ROLE)
        if track is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        current = self._owner.current_uid == track.uid

        if selected:
            self._fill(painter, rect, '#2b4f80')
        elif current:
            self._fill(painter, rect, '#22304a')
        elif hovered:
            self._fill(painter, rect, '#1e232d')

        # обложка
        cover_rect = QRect(rect.left() + PADDING, rect.top() + PADDING, COVER, COVER)
        pixmap = covers.cached(track.cover) if track.cover else None
        if pixmap is not None:
            painter.drawPixmap(cover_rect, covers.rounded(pixmap, COVER))
        else:
            painter.drawPixmap(cover_rect, covers.placeholder(COVER))
            if track.cover:
                self._owner.request_cover(track.cover)

        if self._owner.selection_mode:
            self._draw_check(painter, cover_rect, selected)

        right = rect.right() - PADDING
        # длительность
        duration = format_duration(track.duration)
        if duration:
            painter.setPen(QPen(QColor('#97a0b2')))
            width = painter.fontMetrics().horizontalAdvance(duration)
            painter.drawText(QRect(right - width, rect.top(), width, rect.height()),
                             Qt.AlignVCenter | Qt.AlignRight, duration)
            right -= width + 12

        # метка справа: «уже в VK», «в моей музыке», «скачано». Смысл задаёт раздел,
        # а вид один: плашка в тон строке. Цветной текст на пустом месте читался
        # как случайно подсвеченное слово, а не как признак трека
        state = self._owner.badge(track)
        if state:
            done = state.startswith('✓')
            label = state[1:].strip() if done else state
            ink = QColor('#6fbf73') if done else QColor('#c8a95a')
            icon = MARK_ICONS.get(label)
            if icon:
                right = self._draw_mark(painter, right, rect, icon, ink)
            else:
                width = painter.fontMetrics().horizontalAdvance(label) + BADGE_PAD * 2
                height = BADGE_HEIGHT
                top = rect.top() + (rect.height() - height) // 2
                badge_rect = QRect(right - width, top, width, height)
                fill = QColor(ink)
                fill.setAlpha(38)
                path = QPainterPath()
                path.addRoundedRect(badge_rect, height / 2, height / 2)
                painter.fillPath(path, fill)
                painter.setPen(QPen(ink))
                painter.drawText(badge_rect, Qt.AlignCenter, label)
                right -= width + 12

        text_left = cover_rect.right() + 12
        text_width = max(20, right - text_left)

        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor('#ffffff' if (selected or current) else '#e7e9ef')))
        title = painter.fontMetrics().elidedText(track.title or track.display_title,
                                                 Qt.ElideRight, text_width)
        painter.drawText(QRect(text_left, rect.top() + PADDING, text_width, 20),
                         Qt.AlignVCenter | Qt.AlignLeft, title)

        painter.setFont(option.font)
        painter.setPen(QPen(QColor('#97a0b2')))
        parts = [part for part in (track.artist, track.source_label) if part]
        subtitle = painter.fontMetrics().elidedText(' · '.join(parts), Qt.ElideRight, text_width)
        painter.drawText(QRect(text_left, rect.top() + PADDING + 20, text_width, 18),
                         Qt.AlignVCenter | Qt.AlignLeft, subtitle)

        painter.restore()


class TrackListWidget(QListWidget):
    """Список треков со стандартным набором действий в контекстном меню."""

    play_requested = Signal(object, int)      # список треков, с какого начинать
    enqueue_requested = Signal(object, bool)  # список треков, ставить ли следующим
    add_vk_requested = Signal(object)         # список треков
    favorite_requested = Signal(object)       # список треков
    library_requested = Signal(object)        # список треков - «Моя музыка», переключить
    offline_requested = Signal(object)        # список треков - офлайн-копия, переключить
    download_requested = Signal(object)       # список треков
    open_source_requested = Signal(object)    # один трек
    radio_requested = Signal(object)          # один трек - запустить по нему радио
    remove_requested = Signal(object)         # номера строк (в очереди)
    hide_requested = Signal(object)           # список треков - «не нравится»
    hide_artist_requested = Signal(str)       # имя исполнителя
    artist_requested = Signal(str)            # открыть страницу исполнителя
    playlist_requested = Signal(object, int)  # список треков, id плейлиста (0 - новый)
    selection_mode_changed = Signal(bool)     # включён ли режим выбора
    selection_changed = Signal(int)           # сколько строк отмечено

    def __init__(self, parent=None, allow_download: bool = True,
                 allow_remove: bool = False):
        super().__init__(parent)
        self._allow_download = allow_download
        # «Убрать» осмысленно только там, где список и есть очередь
        self._allow_remove = allow_remove
        self._tracks: list[Track] = []
        self._vk_states: dict[str, str] = {}
        # произвольные метки строк (VK-панель: «в моей музыке», «скачано»)
        self._badges: dict[str, str] = {}
        self._current_uid = ''
        self._cover_requests: set[str] = set()
        # Режим выбора: строки отмечают обычным щелчком, а не Ctrl и Shift
        self._selection_mode = False
        self._hold_row = -1
        self._hold_at = QPoint()
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(HOLD_MS)
        self._hold.timeout.connect(self._on_hold)
        # Где строки тащат мышью (очередь), удержание занято перетаскиванием
        self._hold_enabled = True
        # нужен только меню: подписи «в избранном» и список плейлистов
        self._store = None

        self.setItemDelegate(TrackDelegate(self))
        self.setUniformItemSizes(True)
        self.setAlternatingRowColors(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._base_selection_mode = QAbstractItemView.ExtendedSelection
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)  # без этого делегат не увидит наведение
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.itemDoubleClicked.connect(self._on_double_click)
        self.itemSelectionChanged.connect(self._on_selection_changed)

    def set_store(self, store) -> None:
        self._store = store

    # ---------- режим выбора ----------
    @property
    def selection_mode(self) -> bool:
        return self._selection_mode

    def set_selection_mode(self, on: bool) -> None:
        """В режиме выбора щелчок отмечает строку, а не сбрасывает выделение."""
        on = bool(on)
        if on == self._selection_mode:
            return
        self._selection_mode = on
        # MultiSelection держит отметки без Ctrl: именно так работает выделение
        # после долгого зажатия в телефонах
        self.setSelectionMode(QAbstractItemView.MultiSelection if on
                              else self._base_selection_mode)
        if not on:
            self.clearSelection()
        self.viewport().update()
        self.selection_mode_changed.emit(on)
        self.selection_changed.emit(len(self.selectedItems()))

    def _on_hold(self) -> None:
        """Кнопку держали достаточно долго - включаем выбор с этой строки."""
        if self._hold_row < 0:
            return
        row = self._hold_row
        self._hold_row = -1
        self.set_selection_mode(True)
        item = self.item(row)
        if item is not None:
            item.setSelected(True)

    def _on_selection_changed(self) -> None:
        if self._selection_mode:
            self.selection_changed.emit(len(self.selectedItems()))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if self._selection_mode:
                # Щелчок мимо строк в режиме выбора ничего не сбрасывает
                if not index.isValid():
                    return
            elif self._hold_enabled and index.isValid():
                self._hold_row = index.row()
                self._hold_at = event.position().toPoint()
                self._hold.start()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._hold.isActive():
            moved = event.position().toPoint() - self._hold_at
            if moved.manhattanLength() > HOLD_SLIP:
                self._cancel_hold()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._cancel_hold()
        super().mouseReleaseEvent(event)

    def viewportEvent(self, event) -> bool:
        """Подсказка строки: название целиком и что означает значок справа.

        Метка живёт в словаре и меняется на лету, поэтому подсказку собираем в
        момент показа, а не кладём в QListWidgetItem один раз при заполнении."""
        if event.type() == QEvent.ToolTip:
            point = event.pos()
            track = self.track_at(self.indexAt(point).row())
            if track is None:
                QToolTip.hideText()
                return True
            lines = [track.title or track.display_title]
            if track.artist:
                lines.append(track.artist)
            state = self.badge(track)
            if state:
                lines.append(state.lstrip('✓+').strip().capitalize())
            QToolTip.showText(event.globalPos(), '\n'.join(lines), self)
            return True
        return super().viewportEvent(event)

    def _cancel_hold(self) -> None:
        self._hold.stop()
        self._hold_row = -1

    # ---------- содержимое ----------
    def set_tracks(self, tracks) -> None:
        self._tracks = list(tracks)
        self.clear()
        if self._selection_mode:
            # Очистка снимает отметки молча: полоска иначе покажет старое число
            self.selection_changed.emit(0)
        for track in self._tracks:
            item = QListWidgetItem()
            item.setData(TRACK_ROLE, track)
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self.addItem(item)

    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def selected_tracks(self) -> list[Track]:
        return [item.data(TRACK_ROLE) for item in self.selectedItems()]

    def track_at(self, row: int) -> Track | None:
        return self._tracks[row] if 0 <= row < len(self._tracks) else None

    # ---------- отметки ----------
    @property
    def current_uid(self) -> str:
        return self._current_uid

    def set_current(self, track: Track | None) -> None:
        self._current_uid = track.uid if track is not None else ''
        self.viewport().update()

    def set_vk_state(self, uid: str, label: str) -> None:
        if label:
            self._vk_states[uid] = label
        else:
            self._vk_states.pop(uid, None)
        self.viewport().update()

    def set_vk_states(self, states: dict) -> None:
        self._vk_states.update(states)
        self.viewport().update()

    def vk_state(self, track: Track) -> str:
        if track.source == SOURCE_VK or track.in_vk:
            return ''
        return self._vk_states.get(track.uid, '')

    def set_badge(self, uid: str, text: str) -> None:
        """Метка строки поверх «в VK»: раздел сам решает, что показать."""
        if text:
            self._badges[uid] = text
        else:
            self._badges.pop(uid, None)
        self.viewport().update()

    def set_badges(self, badges: dict) -> None:
        self._badges = dict(badges)
        self.viewport().update()

    def badge(self, track: Track) -> str:
        return self._badges.get(track.uid) or self.vk_state(track)

    def request_cover(self, url: str) -> None:
        """Делегат просит обложку для видимой строки - качаем один раз."""
        if url in self._cover_requests:
            return
        self._cover_requests.add(url)
        covers.load(url, self._on_cover)

    def _on_cover(self, _url: str, _pixmap) -> None:
        self.viewport().update()

    # ---------- действия ----------
    def _on_double_click(self, item: QListWidgetItem) -> None:
        # В режиме выбора два щелчка - это отметить и снять отметку, а не запуск
        if self._selection_mode:
            return
        row = self.row(item)
        if row >= 0:
            self.play_requested.emit(self._tracks, row)

    def _show_menu(self, point) -> None:
        menu = self.build_selection_menu()
        if menu is not None:
            menu.exec(self.viewport().mapToGlobal(point))

    def build_selection_menu(self):
        """Меню по выделенным строкам. Одно и то же по правой кнопке и под кнопкой
        массовых действий: два разных набора команд разошлись бы при первой же правке."""
        selected = self.selected_tracks()
        if not selected:
            return None
        rows = sorted(self.row(item) for item in self.selectedItems())
        actions = TrackActions(
            play=lambda tracks: self._play(tracks, rows[0]),
            enqueue=lambda tracks, first: self.enqueue_requested.emit(tracks, first),
            add_vk=lambda tracks: self.add_vk_requested.emit(tracks),
            favorite=lambda tracks: self.favorite_requested.emit(tracks),
            library=lambda tracks: self.library_requested.emit(tracks),
            offline=lambda tracks: self.offline_requested.emit(tracks),
            playlist=lambda tracks, pid: self.playlist_requested.emit(tracks, pid),
            download=lambda tracks: self.download_requested.emit(tracks),
            remove=lambda numbers: self.remove_requested.emit(numbers),
            radio=lambda track: self.radio_requested.emit(track),
            artist=lambda name: self.artist_requested.emit(name),
            hide=lambda tracks: self.hide_requested.emit(tracks),
            hide_artist=lambda name: self.hide_artist_requested.emit(name),
            open_source=lambda track: self.open_source_requested.emit(track))
        if not self._allow_download:
            actions.download = None
        if not self._allow_remove:
            actions.remove = None
        return build_menu(self, selected, actions, store=self._store, rows=rows)

    def _play(self, selected, first_row: int) -> None:
        """Один выделенный - играем весь список с этой строки, несколько - только их."""
        if len(selected) > 1:
            self.play_requested.emit(selected, 0)
        else:
            self.play_requested.emit(self._tracks, first_row)


class SelectionBar(QWidget):
    """Полоска над списком: включить выбор и что-то сделать с отмеченным.

    Долгое зажатие на строке делает то же самое, но о нём нельзя догадаться,
    глядя на экран, да и в очереди удержание занято перетаскиванием строк.
    """

    def __init__(self, track_list: 'TrackListWidget', parent=None):
        super().__init__(parent)
        self._list = track_list

        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self._toggle = QPushButton('Выбрать')
        self._toggle.setObjectName('tab')
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setToolTip('Отметить несколько треков сразу. То же самое делает долгое зажатие на строке')
        self._toggle.clicked.connect(self._on_toggle)
        box.addWidget(self._toggle)

        self._count = QLabel('')
        self._count.setObjectName('hint')
        box.addWidget(self._count)
        box.addStretch(1)

        self._all = QPushButton('Все')
        self._all.setObjectName('tab')
        self._all.setCursor(Qt.PointingHandCursor)
        self._all.clicked.connect(track_list.selectAll)
        box.addWidget(self._all)

        self._actions = QPushButton('Действия')
        self._actions.setObjectName('primary')
        self._actions.setCursor(Qt.PointingHandCursor)
        self._actions.clicked.connect(self._show_actions)
        box.addWidget(self._actions)

        track_list.selection_mode_changed.connect(self._on_mode)
        track_list.selection_changed.connect(self._on_count)
        self._on_mode(track_list.selection_mode)

    def _on_toggle(self) -> None:
        self._list.set_selection_mode(not self._list.selection_mode)

    def _on_mode(self, on: bool) -> None:
        self._toggle.setText('Готово' if on else 'Выбрать')
        for widget in (self._count, self._all, self._actions):
            widget.setVisible(on)
        self._on_count(len(self._list.selectedItems()) if on else 0)

    def _on_count(self, count: int) -> None:
        self._count.setText(f'Отмечено: {count}' if count else 'Ничего не отмечено')
        self._actions.setEnabled(bool(count))

    def _show_actions(self) -> None:
        menu = self._list.build_selection_menu()
        if menu is None:
            return
        menu.exec(self._actions.mapToGlobal(QPoint(0, self._actions.height())))
