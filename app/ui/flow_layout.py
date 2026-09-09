"""Строка кнопок, которая переносится на следующую, когда места не хватает.

Обычный QHBoxLayout ужимает всё до нечитаемого, а сузиться меньше суммы кнопок
не даёт вовсе - из-за этого окно нельзя было сделать уже 1700 точек без обрезки.
Здесь то же самое, но с переносом: элементы, не влезшие в ширину, уезжают на
следующую строку, а высота считается по фактической ширине (heightForWidth)."""
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox, QLayout, QMenu, QSizePolicy, QSpacerItem, QToolButton, QWidget,
)


class FlowLayout(QLayout):
    """Раскладка с переносом. Растяжки (`addStretch`) работают в пределах строки:
    свободное место достаётся им, поэтому кнопки можно прижать вправо."""

    def __init__(self, parent=None, spacing: int = 8, align_right: bool = False):
        super().__init__(parent)
        self._items: list = []
        self._align_right = align_right
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    # ---------- обязательная часть QLayout ----------
    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.one_line_size()

    def minimumSize(self) -> QSize:
        # Уже самого широкого элемента строка стать не может - дальше только перенос
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size.grownBy(self.contentsMargins())

    # ---------- своё ----------
    def addStretch(self, stretch: int = 1) -> None:
        self.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum))

    def one_line_size(self) -> QSize:
        """Сколько нужно, чтобы всё поместилось в одну строку.

        Считаем так же, как раскладывает _do_layout: тот отступ добавляет после
        каждого элемента, включая растяжки нулевой ширины. Пока здесь их
        пропускали, ширина выходила меньше настоящей, и строка, которой как
        раз хватило места по расчёту, на деле всё равно переносилась."""
        width = height = 0
        items = self._laid_out()
        for item in items:
            if self._is_stretch(item):
                continue
            hint = self._hint(item)
            width += hint.width()
            height = max(height, hint.height())
        if items:
            width += self.spacing() * (len(items) - 1)
        return QSize(width, height).grownBy(self.contentsMargins())

    @staticmethod
    def _hint(item) -> QSize:
        """Размер элемента с оглядкой на жёстко заданную ширину.

        sizeHint виджета не знает про setFixedWidth, пока тот не применён: у
        ползунка громкости он остаётся 84 там, где полоске уже назначили 54.
        Из-за лишних 30 px правая зона просила больше места, чем ей нужно, не
        помещалась в окно - и раскладка разносила полосу на две строки."""
        hint = item.sizeHint()
        widget = item.widget()
        if widget is not None:
            fixed = widget.maximumWidth()
            if fixed == widget.minimumWidth():
                return QSize(fixed, hint.height())
        return hint

    def _laid_out(self) -> list:
        # Спрятанные виджеты (тот же чип заливки) места занимать не должны
        return [item for item in self._items if self._is_stretch(item) or not item.isEmpty()]

    @staticmethod
    def _is_stretch(item) -> bool:
        return item.widget() is None and item.spacerItem() is not None

    def _grows(self, item) -> bool:
        """Кому достаётся свободное место в строке: растяжкам и полям ввода."""
        return self._is_stretch(item) or bool(item.expandingDirections() & Qt.Horizontal)

    def _squeeze(self, width: int) -> int:
        """На сколько ужать каждый растяжимый элемент, чтобы обойтись одной строкой.

        Раздавать свободное место раскладка умела всегда, а отбирать - нет: поле
        ввода держалось за свой sizeHint в 210 px и выталкивало кнопку «Добавить»
        на вторую строку, хотя вся полоса помещалась в одну.

        Сжимаем только ради единственной строки: там, где перенос честный (три
        ряда опций в узком окне), отнимать у поля ввода незачем - оно съёжится,
        а строк меньше не станет. Поэтому 0, если после сжатия всё равно не
        уместимся, и 0, если и без него уже умещаемся.

        Вложенные полоски (`hasHeightForWidth`) не трогаем: они на сужение
        отвечают не сжатием, а собственным переносом - становятся выше, чем
        им отвели снаружи, и вылезают за строку."""
        items = self._laid_out()
        if not items:
            return 0
        need = sum(0 if self._is_stretch(i) else self._hint(i).width() for i in items)
        # Отступы считаем только между теми, у кого есть ширина: распорка её не
        # имеет и в раскладке отступ тоже не занимает
        need += self.spacing() * max(0, sum(
            1 for i in items if not self._is_stretch(i) and self._hint(i).width()) - 1)
        over = need - width
        if over <= 0:
            return 0
        growable = [i for i in items
                    if not self._is_stretch(i) and self._grows(i)
                    and not i.hasHeightForWidth()]
        if not growable:
            return 0
        # Больше минимума не отнять: ниже него элемент всё равно не сожмётся
        room = sum(self._hint(i).width() - i.minimumSize().width() for i in growable)
        if room < over:
            return 0
        return -(-over // len(growable))        # округление вверх

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        space = self.spacing()
        x, y, line_height = area.x(), area.y(), 0
        line: list = []

        def flush() -> None:
            """Разложить накопленную строку: свободное место - растяжкам и полям
            ввода, а если таких нет и просили выравнивание вправо - сдвинуть строку.

            По вертикали всё центрируется относительно самого высокого соседа:
            иначе подпись в 16 точек висела бы у верхнего края списка в 30."""
            if test_only or not line:
                return
            tall = max(geo.height() for _, geo in line)

            def place(item, geo, shift: int = 0) -> None:
                geo = geo.translated(shift, (tall - geo.height()) // 2)
                item.setGeometry(geo)

            used = sum(geo.width() for _, geo in line) + space * (len(line) - 1)
            free = area.width() - used
            growable = [i for i, (item, _) in enumerate(line) if self._grows(item)]
            if growable and free > 0:
                share, extra = divmod(free, len(growable))
                shift = 0
                for index, (item, geo) in enumerate(line):
                    if index in growable:
                        add = share + (extra if index == growable[-1] else 0)
                        geo = QRect(geo)
                        geo.setWidth(geo.width() + add)
                        place(item, geo, shift)
                        shift += add
                    else:
                        place(item, geo, shift)
            elif self._align_right and free > 0:
                for item, geo in line:
                    place(item, geo, free)
            else:
                for item, geo in line:
                    place(item, geo)

        squeeze = self._squeeze(area.width())
        for item in self._laid_out():
            hint = QSize(0, 0) if self._is_stretch(item) else self._hint(item)
            # Элемент шире строки (длинная подпись) сжимаем, иначе он вылезет за край
            hint = QSize(min(hint.width(), area.width()), hint.height())
            if (squeeze and not self._is_stretch(item) and self._grows(item)
                    and not item.hasHeightForWidth()):
                # Растяжимому уступать первым: строка ввода спокойно живёт
                # в половину ширины, а кнопка на второй строке ломает вид
                hint = QSize(max(item.minimumSize().width(),
                                 hint.width() - squeeze), hint.height())
            # Нулевой ширине отступ ни к чему: распорка сама по себе ничего не
            # показывает, а свои 8 px из строки забирала - у VK из-за них
            # «Скачать выбранное» переносилась, не добрав ровно этой мелочи
            gap = space if hint.width() else 0
            next_x = x + hint.width() + gap
            if next_x - space > area.right() + 1 and line_height > 0:
                flush()
                line = []
                x = area.x()
                y += line_height + space
                next_x = x + hint.width() + space
                line_height = 0
            geometry = QRect(QPoint(x, y), hint)
            if not test_only:
                line.append((item, geometry))
            x = next_x
            line_height = max(line_height, hint.height())
        flush()
        return y + line_height - rect.y() + margins.bottom()


class FlowRow(QWidget):
    """Готовая полоска с переносом: `row.add(button)`, `row.add_stretch()`.

    Держит собственную высоту в актуальном состоянии - иначе после переноса на
    вторую строку соседи в вертикальной раскладке не подвинулись бы."""

    def __init__(self, parent=None, spacing: int = 8, align_right: bool = False,
                 overflow: bool = False):
        super().__init__(parent)
        self._flow = FlowLayout(self, spacing=spacing, align_right=align_right)
        policy = self.sizePolicy()
        # Fixed, а не Minimum: высота полоски целиком определяется её шириной, и
        # растягивать её не по чему. С Minimum вертикальная раскладка считала
        # полоску растяжимой и отдавала ей весь остаток колонки - рисовалась она
        # по-прежнему в одну строку, но занятый прямоугольник наезжал на список
        # сверху. Перенос на вторую строку от этого не страдает: sizeHint отдаёт
        # высоту по heightForWidth
        policy.setVerticalPolicy(QSizePolicy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._wrapped_height = -1
        # Переполнение: вместо переноса на вторую строку лишние кнопки уезжают
        # под «⋯». Строка кнопок остаётся в одну высоту, и списку под ней не
        # приходится ужиматься до полутора строк в низком окне
        self._overflow = None
        self._items = []
        self._hidden = []
        # Защита от вложенных resizeEvent: hide/show внутри пересчёта
        # снова зовут _sync_height
        self._busy = False
        if overflow:
            self._overflow = QToolButton(self)
            self._overflow.setObjectName('overflowBtn')
            self._overflow.setText('⋯')
            self._overflow.setToolTip('Ещё действия')
            self._overflow.setPopupMode(QToolButton.InstantPopup)
            # Без этого Qt рисует к «⋯» ещё и служебную стрелку меню: она не
            # помещалась в кнопку по высоте и висела отдельной галочкой под ней.
            # Многоточие само по себе читается как «здесь есть продолжение»
            self._overflow.setStyleSheet('QToolButton::menu-indicator { image: none; }')
            self._overflow.setMenu(QMenu(self._overflow))
            self._overflow.hide()

    def add(self, widget: QWidget) -> QWidget:
        self._flow.addWidget(widget)
        self._items.append(widget)
        return widget

    def add_stretch(self) -> None:
        self._flow.addStretch()

    def clear(self) -> None:
        """Убрать содержимое: полоску собирают заново, когда пришли новые данные."""
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._wrapped_height = -1
        self._items = []
        self._hidden = []
        if self._overflow is not None:
            self._overflow.menu().clear()
            self._overflow.hide()
        self.updateGeometry()

    def one_line_width(self) -> int:
        """Ширина, при которой переноса не будет.

        Отдельно от sizeHint: тот подмешивает высоту по текущей ширине,
        а значит уже перенёсшаяся строка так и осталась бы перенесённой."""
        return self._flow.one_line_size().width()

    def sizeHint(self) -> QSize:
        hint = self._flow.one_line_size()
        # Ширину просим как для одной строки, высоту - какая получается на деле:
        # в горизонтальной раскладке heightForWidth соседей никто не спрашивает.
        # Берём высоту, посчитанную в _sync_height, а не считаем заново от
        # self.width(): на момент опроса геометрия бывает ещё старая, и полоса
        # обещала одну строку, а рисовалась в две - соседний ярус наезжал
        if self._wrapped_height >= 0:
            return QSize(hint.width(), max(hint.height(), self._wrapped_height))
        if self.width() > 0:
            return QSize(hint.width(), max(hint.height(), self._flow.heightForWidth(self.width())))
        return hint

    def minimumSizeHint(self) -> QSize:
        return self._flow.minimumSize()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event) -> None:
        """Пересчитать переполнение, когда кнопки наконец видимы.

        Ширину полоса получает раньше, чем её показывают, и единственный
        resizeEvent приходил на скрытые кнопки: `_sync_overflow` считал состав
        по видимым, видел ноль штук и не прятал ничего. Дальше ширина не
        менялась, второго повода пересчитать не было - и в широком окне строка
        молча уезжала в две, а нижняя половина «Удалить» и «В музыку VK»
        оказывалась под плеером, при том что «⋯» оставалась спрятанной."""
        super().showEvent(event)
        self._sync_height()

    def _sync_overflow(self, width: int) -> None:
        """Спрятать под «⋯» то, что не влезло в одну строку.

        Прячем с конца: полезные действия обычно стоят первыми, а «Обновить»
        и «Снять всё» переживут дорогу в меню. Возвращаем, как только место
        появилось, - иначе в широком окне меню осталось бы навсегда."""
        if self._overflow is None or width <= 0 or self._busy:
            return
        # Считаем состав с нуля от полного набора, а не правим прошлый: hide и
        # show порождают вложенные resizeEvent, и пошаговая правка «дрожала» -
        # на одной и той же ширине выходило то три кнопки в меню, то пять
        self._busy = True
        try:
            # Кандидаты - всё, кроме спрятанного самой страницей: раздел прячет
            # неподходящие фильтры по смыслу (например «Откуда» в разделе «С
            # компьютера»), и вернуть их на место значило бы соврать
            for widget in self._hidden:
                widget.setVisible(True)
            # Прячем только то, что доживёт до меню. Метку, поле ввода, список
            # или стопку страниц пунктом меню не покажешь - спрятанные, они
            # исчезали совсем: на «Загрузках» в узком окне так пропадал выбор
            # формата и битрейта, а «⋯» при этом даже не появлялась, потому что
            # меню выходило пустым. Такому элементу перенос на вторую строку -
            # единственный честный выход, и он лучше пропажи
            items = [w for w in self._items
                     if w.isVisible() and self._menu_ready(w)]
            # Правый отступ под «⋯» остался от прошлого пересчёта, а решаем мы
            # сейчас заново - с ним раскладка мерила бы себя по укороченной
            # полосе и повод спрятать находила бы сама себе. У плейлистов от
            # этого «Обновить» не возвращалась из меню, даже когда строку
            # расширили: 292 px хватало на 288, но мерилось-то по 254
            margins = self._flow.contentsMargins()
            if margins.right():
                self._flow.setContentsMargins(
                    margins.left(), margins.top(), 0, margins.bottom())
            self._flow.invalidate()
            row = self._flow.heightForWidth(1 << 16)
            hidden = []
            if self._flow.heightForWidth(width) > row:
                # Место под саму «⋯»: она стоит поверх строки у правого края и
                # в раскладке не участвует, иначе перенос утащил бы и её
                room = width - (self._overflow.sizeHint().width() + self._flow.spacing())
                # Прячем с конца: нужные действия обычно стоят первыми, а
                # «Обновить» и «Снять всё» переживут дорогу в меню
                for widget in reversed(items):
                    widget.setVisible(False)
                    hidden.append(widget)
                    self._flow.invalidate()
                    if self._flow.heightForWidth(room) <= row:
                        break
                hidden.reverse()
            self._hidden = hidden
        finally:
            self._busy = False
        self._sync_overflow_menu()

    def _row_height(self) -> int:
        """Высота одной строки - по ней понимаем, случился ли перенос.

        Спрашиваем саму раскладку на заведомо широкой полосе: складывать
        sizeHint кнопок нельзя - в высоту входят ещё и её отступы."""
        return self._flow.heightForWidth(1 << 16)

    @staticmethod
    def _menu_ready(widget: QWidget) -> bool:
        """Годится ли виджет в пункт меню.

        Пункт меню - это подпись и нажатие. Ни того, ни другого нет у метки,
        поля ввода и стопки страниц; у галочки `click` есть, но переключать её
        вслепую, не показав состояния, - не то же самое, что нажать кнопку.
        По этому же признаку решается, можно ли виджет прятать: спрятать то,
        что в меню не попадёт, значит просто отнять его у человека."""
        if isinstance(widget, QCheckBox):
            return False
        if not hasattr(widget, 'click') or not hasattr(widget, 'text'):
            return False
        return bool(widget.text())

    def _sync_overflow_menu(self) -> None:
        """Пересобрать меню под «⋯» по списку спрятанных кнопок."""
        menu = self._overflow.menu()
        menu.clear()
        for widget in self._hidden:
            if not self._menu_ready(widget):
                continue
            text = widget.text()
            action = menu.addAction(text)
            action.setEnabled(widget.isEnabled())
            action.triggered.connect(widget.click)
        self._overflow.setVisible(bool(menu.actions()))
        size = self._overflow.sizeHint()
        # Место под «⋯» отдаём правым отступом самой раскладки, а не вычитанием
        # при подсчёте: иначе кнопки растягивались бы на всю ширину и последняя
        # оказывалась под «⋯». Отступ снимаем, как только меню опустело
        margins = self._flow.contentsMargins()
        right = size.width() + self._flow.spacing() if self._overflow.isVisible() else 0
        if margins.right() != right:
            self._flow.setContentsMargins(
                margins.left(), margins.top(), right, margins.bottom())
        if self._overflow.isVisible():
            # «⋯» не в раскладке: она бы сама попала под перенос. Ставим её
            # к правому краю на высоте строки
            # По центру строки, а не от верхнего края: соседние кнопки выше «⋯»,
            # и приклеенная к нулю кнопка съезжала над ними
            top = max(0, (self.height() - size.height()) // 2)
            self._overflow.setGeometry(
                self.width() - size.width(), top, size.width(), size.height())
            self._overflow.raise_()

    def _sync_height(self, width: int = -1) -> None:
        """Пересчитать высоту по нынешней ширине.

        Отдельно от resizeEvent: ширину полоске могут задать снаружи
        (setFixedWidth), и тогда resizeEvent приходит позже, чем соседи
        спросят sizeHint - и полоса остаётся в две строки на пустом месте.

        Ширину можно передать явно: в момент setFixedWidth геометрия ещё
        старая, и по ней высота вышла бы прежней - с лишней пустой строкой."""
        if width < 0:
            width = self.width()
        self._sync_overflow(width)
        height = self._flow.heightForWidth(width)
        if height != self._wrapped_height:
            self._wrapped_height = height
            self.updateGeometry()

    def setFixedWidth(self, width: int) -> None:
        super().setFixedWidth(width)
        self._sync_height(width)
