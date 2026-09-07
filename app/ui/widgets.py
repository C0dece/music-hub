"""Мелкие общие виджеты интерфейса."""
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame, QLabel, QListWidget, QPushButton, QSizePolicy, QStackedWidget,
    QStyledItemDelegate, QTableView, QVBoxLayout, QWidget,
)

from . import player_icons, theme

# Роль, по которой делегат прогресса берёт число 0..100 из модели
PROGRESS_ROLE = Qt.UserRole + 1
STATE_ROLE = Qt.UserRole + 2


# Цвет значка по состоянию: зелёный «всё хорошо», жёлтый «внимание»,
# серый «выключено». Те же цвета, что и у фона чипа в QSS
_STATE_COLORS = {
    'ok': theme.color('ok'),
    'warn': theme.color('warn'),
    'off': theme.color('text_mute'),
}

CHIP_ICON = 16


class PagesStack(QStackedWidget):
    """Стопка страниц, которая меряется по текущей странице.

    Обычный QStackedWidget берёт минимум по самой требовательной странице и
    держит его для всех: из-за раздела видео (394 px) и библиотеки (320) окно
    не сжималось по высоте, даже когда открыта «Главная» с минимумом 68.
    Ширину меряем так же — иначе широкая страница не давала сузить окно."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Минимум стопки меняется вместе со страницей — раскладку надо
        # пересчитать, иначе окно держит требования уже закрытой страницы
        self.currentChanged.connect(lambda _row: self.updateGeometry())

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()


class StatusChip(QLabel):
    """Значок состояния службы: VK, YouTube, загрузки.

    Раньше здесь стояли надписи «YouTube готов» и «VK на связи». Они занимали
    полполосы, а в узком окне сжимались до цветной точки, которая уже ничего
    не объясняла. Значок читается одинаково при любой ширине, а подробности
    остаются в подсказке.

    Значок бывает и кнопкой: `set_clickable(True)` даёт `clicked`, руку под
    курсором и Enter/Пробел с клавиатуры. Так вход в VK и YouTube живёт там же,
    где показано их состояние, — нажимают ровно то, на что смотрят."""

    clicked = Signal()

    def __init__(self, icon: str = '', state: str = 'off', parent=None):
        super().__init__(parent)
        self.setObjectName('chip')
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(CHIP_ICON + 12, CHIP_ICON + 12)
        self._icon = icon
        self._state = state
        self._label = ''
        self._clickable = False
        self._redraw()

    def set_clickable(self, on: bool) -> None:
        self._clickable = on
        self.setCursor(Qt.PointingHandCursor if on else Qt.ArrowCursor)
        # Клавиатурой значок доступен только пока он кнопка: иначе Tab
        # останавливался бы на трёх подряд неинтерактивных картинках
        self.setFocusPolicy(Qt.StrongFocus if on else Qt.NoFocus)

    def mouseReleaseEvent(self, event) -> None:
        # Отпускание, а не нажатие: уехав курсором с виджета, нажатие ещё можно
        # отменить — так ведут себя обычные кнопки
        if (self._clickable and event.button() == Qt.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._clickable and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def _redraw(self) -> None:
        if not self._icon:
            self.clear()
            return
        color = _STATE_COLORS.get(self._state, _STATE_COLORS['off'])
        self.setPixmap(player_icons.pixmap(self._icon, color, CHIP_ICON))

    def set_icon(self, icon: str) -> None:
        if icon != self._icon:
            self._icon = icon
            self._redraw()

    def set_state(self, state: str) -> None:
        """state: 'ok' (всё хорошо) | 'warn' (внимание) | 'off' (выключено)."""
        if state != self._state:
            self._state = state
            self._redraw()
        self.setProperty('state', state)
        # Свойство меняется после разбора QSS: стиль нужно пересчитать вручную
        self.style().unpolish(self)
        self.style().polish(self)

    def update_chip(self, text: str, state: str, tooltip: str = '',
                    icon: str = '') -> None:
        """text — короткое название состояния, оно уходит в первую строку
        подсказки: без него значок не расшифровать."""
        self._label = text
        if icon:
            self.set_icon(icon)
        self.set_state(state)
        parts = [part for part in (text, tooltip) if part]
        self.setToolTip('\n'.join(parts))

    def label(self) -> str:
        """Надпись состояния — нужна меню и тестам, на экране её больше нет."""
        return self._label


class ElidedLabel(QLabel):
    """Подпись, которая в узком окне сокращается многоточием.

    Обычный QLabel требует себе ширину всего текста и не даёт окну сузиться:
    строки «Всего 12 файлов…» одни задирали минимальную ширину окна на треть.
    Полный текст остаётся в подсказке."""

    def __init__(self, text: str = '', parent=None):
        super().__init__(parent)
        self._full = ''
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text or ''
        self.setToolTip(self._full)
        self._apply_elide()

    def text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:
        # Считаем по полному тексту: иначе после первого сокращения подпись
        # запомнила бы урезанную ширину и в широком окне уже не развернулась
        hint = super().sizeHint()
        metrics = self.fontMetrics()
        padding = max(hint.width() - metrics.horizontalAdvance(QLabel.text(self)), 0)
        return QSize(metrics.horizontalAdvance(self._full) + padding, hint.height())

    def minimumSizeHint(self) -> QSize:
        # Ширину не требуем вовсе: сколько дадут, столько и покажем
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, width) if width > 1 else self._full
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)


class Card(QFrame):
    """Панель-карточка с внутренними отступами: контейнер для блока настроек."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        self.setFrameShape(QFrame.NoFrame)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(10)

    def layout(self) -> QVBoxLayout:
        return self._layout


class EmptyState(QWidget):
    """Заглушка пустого списка: значок, заголовок, пояснение и одно действие.

    Пустой экран без объяснений выглядит поломкой. Раньше каждый раздел
    выкручивался своей серой строчкой посередине, поэтому одна и та же мысль
    звучала в пяти видах; здесь она одна на всё приложение."""

    def __init__(self, icon: str, title: str, text: str = '', parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 28, 24, 28)
        box.setSpacing(8)
        box.addStretch(1)

        # Значок рисуем крупно и приглушённо: он задаёт настроение, но читают всё
        # равно заголовок
        self._glyph = glyph = QLabel()
        glyph.setAlignment(Qt.AlignCenter)
        box.addWidget(glyph)
        self.set_icon(icon)

        self._head = head = QLabel(title)
        head.setObjectName('emptyTitle')
        head.setAlignment(Qt.AlignCenter)
        head.setWordWrap(True)
        box.addWidget(head)

        self._text = QLabel(text)
        self._text.setObjectName('emptyText')
        self._text.setAlignment(Qt.AlignCenter)
        self._text.setWordWrap(True)
        self._text.setVisible(bool(text))
        box.addWidget(self._text)

        self._button: QPushButton | None = None
        self._box = box
        box.addStretch(1)
        # Заглушка не должна диктовать высоту окна: со значком, отступами и
        # пояснением она требовала 170 px, и из-за неё окно не сжималось, а
        # список под ней получал полторы строки. В тесноте прячем сначала
        # значок, потом пояснение — заголовок остаётся всегда
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(hint.width(), self._head.minimumSizeHint().height() + 8)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        height = self.height()
        # Пороги от суммы частей: ниже 150 не помещаются все трое, ниже 90 —
        # даже значок с заголовком
        self._glyph.setVisible(height >= 150)
        tight = height < 120
        margin = 6 if tight else 28
        self._box.setContentsMargins(24, margin, 24, margin)
        if self._button is not None:
            self._button.parentWidget().setVisible(height >= 110)
        self._text.setVisible(bool(self._text.text()) and height >= 90)
        # Показываем часть только тогда, когда она поместится целиком. Одной
        # высоты окна для этого мало: текст переносится по словам, и те же две
        # строки в узком окне становятся четырьмя. Раньше пороги были
        # константами, и на «Плейлистах» при 664 px нижняя строка («…или
        # открыть по ссылке») срезалась по краю виджета. Спросить layout тоже
        # нельзя: про перенос он не знает — `minimumHeight` метки с переносом
        # это высота одной строки. Поэтому раскладываем и смотрим, сколько
        # каждой части в итоге досталось.
        #
        # Считаем не по одному пояснению, а по сумме: политика Ignored разрешает
        # раскладке ужимать нас ниже нужного, и тогда части наезжают друг на
        # друга — на «Загрузках» при 664 px заголовок перекрывался пояснением на
        # 12 px, хотя каждый по отдельности «помещался». Лишнее убираем снизу
        # вверх: пояснение, затем значок; заголовок остаётся всегда — пустая
        # заглушка хуже урезанной
        for _ in range(4):
            if self._need() <= height:
                break
            # Сначала ужимаем поля: они уходят безболезненно, в отличие от текста
            if margin > 6:
                margin = 6
                self._box.setContentsMargins(24, margin, 24, margin)
            elif not self._text.isHidden():
                self._text.setVisible(False)
            elif not self._glyph.isHidden():
                self._glyph.setVisible(False)
            elif self._button is not None and not self._button.parentWidget().isHidden():
                self._button.parentWidget().setVisible(False)
            else:
                break

    def _need(self) -> int:
        """Сколько высоты просят видимые части вместе с отступами и промежутками.

        Видимость спрашиваем через `isHidden`, а не `isVisible`: последний ложен и
        тогда, когда скрыт кто-то из предков — например пока страница ещё не
        показана. В этот момент resize уже приходит, и `isVisible` объявлял бы
        скрытым всё подряд, а спрятанное нами — по-прежнему видимым."""
        margins = self._box.contentsMargins()
        # Ширину берём свою, а не метки: в первом кадре после ресайза layout ещё
        # не раздал детям новые размеры, и `label.width()` вернула бы прошлую.
        # Именно на этом кадре наложение и было заметно глазом
        inner = max(1, self.width() - margins.left() - margins.right())
        parts = []
        if not self._glyph.isHidden():
            parts.append(self._glyph.sizeHint().height())
        parts.append(self._label_need(self._head, inner))
        if not self._text.isHidden():
            parts.append(self._label_need(self._text, inner))
        if self._button is not None and not self._button.parentWidget().isHidden():
            parts.append(self._button.parentWidget().sizeHint().height())
        return (sum(parts) + self._box.spacing() * (len(parts) - 1)
                + margins.top() + margins.bottom())

    @staticmethod
    def _label_need(label: QLabel, width: int) -> int:
        """Высота метки с учётом переноса по словам на заданной ширине."""
        if label.hasHeightForWidth():
            return label.heightForWidth(width)
        return label.sizeHint().height()

    def set_icon(self, icon: str) -> None:
        self._glyph.setPixmap(
            player_icons.draw(icon, theme.color('text_mute'), 40).pixmap(40, 40))

    def set_title(self, title: str) -> None:
        self._head.setText(title)

    def set_text(self, text: str) -> None:
        self._text.setText(text)
        self._text.setVisible(bool(text))

    def add_action(self, title: str, slot) -> QPushButton:
        """Кнопка-подсказка: пустой раздел должен предлагать выход из пустоты."""
        button = QPushButton(title)
        button.setObjectName('primary')
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        holder = QWidget()
        row = QVBoxLayout(holder)
        row.setContentsMargins(0, 6, 0, 0)
        row.setAlignment(Qt.AlignCenter)
        row.addWidget(button)
        # Перед нижней растяжкой, иначе кнопка уедет к самому краю
        self._box.insertWidget(self._box.count() - 1, holder)
        self._button = button
        return button


class Skeleton(QWidget):
    """Серые заготовки строк, пока идёт загрузка.

    Пустое место на месте списка не отличить от поломки: непонятно, ещё грузится
    или уже нет. Рисуем сами, а не кладём в список выдуманные треки — такой трек
    можно было бы случайно включить.

    rows=0 — занять всю доступную высоту, иначе ровно столько строк."""

    ROW = 56

    def __init__(self, rows: int = 0, parent=None):
        super().__init__(parent)
        self._rows = max(0, int(rows))
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        if self._rows:
            self.setFixedHeight(self._rows * self.ROW)
        else:
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        pad = 8
        cover = self.ROW - pad * 2
        text_left = cover + pad * 2
        width = max(0, self.width() - text_left - pad)
        count = self._rows or max(1, self.height() // self.ROW)
        for index in range(count):
            top = index * self.ROW + pad
            painter.setBrush(QColor(theme.color('raised')))
            painter.drawRoundedRect(QRectF(pad, top, cover, cover), 6, 6)
            painter.setBrush(QColor(theme.color('hover')))
            painter.drawRoundedRect(QRectF(text_left, top + 4, width * 0.45, 10), 5, 5)
            painter.drawRoundedRect(QRectF(text_left, top + 22, width * 0.28, 8), 4, 4)


class ProgressDelegate(QStyledItemDelegate):
    """Полоса прогресса в ячейке таблицы.

    Рисуем сами, а не через QProgressBar в QStyle: системный стиль на Windows
    анимирует полосу и меняет её высоту, из-за чего строки таблицы «дышали»."""

    # Цвета из общей палитры: акцент, «готово» и «ошибка» те же, что у чипов
    _TRACK = QColor(theme.color('hover'))
    _FILL = QColor(theme.color('accent'))
    _FILL_DONE = QColor(theme.color('ok'))
    _FILL_FAIL = QColor(theme.color('danger'))
    _TEXT = QColor(theme.color('text'))

    def paint(self, painter: QPainter, option, index) -> None:
        pct = index.data(PROGRESS_ROLE)
        if pct is None:
            super().paint(painter, option, index)
            return

        state = index.data(STATE_ROLE) or ''
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(option.rect).adjusted(6, 0, -6, 0)
        height = 16.0
        track = QRectF(rect.x(), rect.center().y() - height / 2, rect.width(), height)
        radius = height / 2

        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, self._TRACK)

        ratio = max(0.0, min(float(pct), 100.0)) / 100.0
        if ratio > 0:
            # Ширина не меньше диаметра скругления, иначе полоса на 1% выглядит обрубком
            width = max(track.width() * ratio, height)
            fill = QRectF(track.x(), track.y(), width, track.height())
            fill_path = QPainterPath()
            fill_path.addRoundedRect(fill, radius, radius)
            colour = {'done': self._FILL_DONE, 'error': self._FILL_FAIL}.get(state, self._FILL)
            painter.fillPath(fill_path.intersected(path), colour)

        label = index.data(Qt.DisplayRole) or ''
        if label:
            font = QFont(option.font)
            font.setPointSizeF(max(font.pointSizeF() - 1.0, 7.0))
            painter.setFont(font)
            painter.setPen(self._TEXT)
            painter.drawText(track, Qt.AlignCenter, str(label))

        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), 28))


def is_checked(value) -> bool:
    """Галочка стоит? Модель отдаёт то Qt.CheckState, то целое — сравниваем аккуратно."""
    if isinstance(value, Qt.CheckState):
        return value == Qt.Checked
    return value == Qt.Checked.value


class _CheckDrag:
    """Проставление галочек протяжкой мыши.

    Отмечать полсотни треков по одному — мучение, поэтому нажатая левая кнопка
    ведёт галочку за собой: какое состояние получила первая строка, такое же
    достаётся всем от начала протяжки до курсора. Ведёте мышь обратно — строки,
    оставшиеся позади, возвращаются к тому, что было до протяжки, так что промах
    исправляется тем же движением, не отпуская кнопку. Начинать нужно с самой
    галочки — протяжка по остальной части строки, как и везде, выделяет строки."""

    # Ширина зоны галочки в списке. В таблице у галочек своя колонка, там проще.
    _CHECK_ZONE = 26
    # На сколько прокручивать список, когда курсор упёрся в край, и как часто
    _SCROLL_STEP = 12
    _SCROLL_INTERVAL = 40

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._drag_state = None
        self._drag_anchor = None
        self._drag_pos = None
        # Строка -> состояние до протяжки. По нему возвращаем строки, из которых
        # курсор ушёл обратно; заодно это отметка «здесь мы уже были»
        self._drag_before: dict[int, Qt.CheckState] = {}
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(self._SCROLL_INTERVAL)
        self._scroll_timer.timeout.connect(self._drag_scroll)

    # Подклассы решают, попал ли курсор в галочку
    def _check_index(self, pos):
        raise NotImplementedError

    def mousePressEvent(self, event):
        index = (self._check_index(event.position().toPoint())
                 if event.button() == Qt.LeftButton else None)
        if index is None:
            super().mousePressEvent(event)
            return
        self._drag_state = Qt.Unchecked if is_checked(
            index.data(Qt.CheckStateRole)) else Qt.Checked
        self._drag_anchor = index.row()
        self._drag_before = {}
        self._drag_pos = event.position().toPoint()
        self._apply_to(index.row())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_state is None:
            super().mouseMoveEvent(event)
            return
        self._drag_pos = event.position().toPoint()
        # Колонку и зону галочки по дороге не проверяем: ведя мышь вниз, легко
        # съехать вбок, и галочки переставали бы ставиться на полпути
        self._apply_to(self._row_at(self._drag_pos))
        if not self.viewport().rect().contains(self._drag_pos):
            self._drag_scroll()
            self._scroll_timer.start()
        else:
            self._scroll_timer.stop()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        # Два быстрых клика по галочке — это по-прежнему отметка, а не «открыть»:
        # иначе второй клик подряд открывал бы проигрыватель
        if (event.button() == Qt.LeftButton
                and self._check_index(event.position().toPoint()) is not None):
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_state is None:
            super().mouseReleaseEvent(event)
            return
        self._drag_state = None
        self._drag_anchor = None
        self._drag_before = {}
        self._scroll_timer.stop()
        event.accept()

    def _drag_scroll(self) -> None:
        """Курсор за краем списка — прокручиваем сами и отмечаем то, что подъехало.

        Иначе протяжка упиралась бы в нижнюю строку: мышь стоит на месте, событий
        движения нет, а список не едет."""
        if self._drag_pos is None:
            return
        rect = self.viewport().rect()
        if self._drag_pos.y() < rect.top():
            delta = -self._SCROLL_STEP
        elif self._drag_pos.y() > rect.bottom():
            delta = self._SCROLL_STEP
        else:
            self._scroll_timer.stop()
            return
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + delta)
        self._apply_to(self._row_at(self._drag_pos))

    def _apply_to(self, row) -> None:
        """Отметить строки от начала протяжки до текущей, остальным вернуть прежнее."""
        if row is None or self._drag_anchor is None:
            return
        low, high = sorted((self._drag_anchor, row))
        for touched in list(self._drag_before):
            if not low <= touched <= high:
                self._set_check(touched, self._drag_before.pop(touched))
        for target in range(low, high + 1):
            if target not in self._drag_before:
                index = self._check_index_for_row(target)
                self._drag_before[target] = (
                    Qt.Checked if is_checked(index.data(Qt.CheckStateRole)) else Qt.Unchecked)
                self._set_check(target, self._drag_state)

    def _row_at(self, pos):
        """Строка под курсором, а за краем списка — ближайшая крайняя.

        Уехав мышью ниже последней строки или вбок от колонки, протяжка иначе
        теряла бы строку и переставала отмечать."""
        model = self.model()
        if model is None or not model.rowCount():
            return None
        y = min(max(pos.y(), 0), max(self.viewport().height() - 1, 0))
        index = self.indexAt(QPoint(self._CHECK_ZONE // 2, y))
        if index.isValid():
            return index.row()
        return model.rowCount() - 1 if pos.y() > 0 else 0

    def _set_check(self, row: int, state) -> None:
        if state is None:
            return
        index = self._check_index_for_row(row)
        if index.isValid():
            self.model().setData(index, state, Qt.CheckStateRole)

    def _check_index_for_row(self, row: int):
        return self.model().index(row, 0)


class CheckableListWidget(_CheckDrag, QListWidget):
    """Список с галочками, которые ставятся протяжкой."""

    def _check_index(self, pos):
        index = self.indexAt(pos)
        if not index.isValid() or not (index.flags() & Qt.ItemIsUserCheckable):
            return None
        rect = self.visualRect(index)
        return index if pos.x() - rect.x() <= self._CHECK_ZONE else None


class CheckableTableView(_CheckDrag, QTableView):
    """Таблица с колонкой галочек, которые ставятся протяжкой."""

    CHECK_COLUMN = 0

    def _check_index(self, pos):
        index = self.indexAt(pos)
        if not index.isValid() or index.column() != self.CHECK_COLUMN:
            return None
        return index

    def _check_index_for_row(self, row: int):
        # Мышь идёт вниз по любой колонке, а галочка живёт только в своей
        return self.model().index(row, self.CHECK_COLUMN)


def spacer(height: int = 8) -> QWidget:
    """Пустой блок фиксированной высоты — вместо магии с addSpacing в разных местах."""
    widget = QWidget()
    widget.setFixedHeight(height)
    return widget
