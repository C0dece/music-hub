"""Область видео: своя шапка, правильные пропорции и полный экран.

Раньше плеер YouTube жил в виджете с фиксированной минимальной высотой, и
широкий кадр 16:9 сплющивался в полоску. Здесь высота считается от ширины, как
у любого нормального видеоплеера, а не задаётся числом.

Сцена оформлена карточкой с шапкой: в ней видно, что показывают, и лежат
кнопки «во весь экран» и «свернуть». Без шапки чёрный прямоугольник посреди
страницы выглядел сбоем - убрать его было нечем, а пока поднимался Chromium,
он вообще стоял пустым. Теперь на месте картинки, которой ещё нет, подпись о
том, что происходит.

Полный экран сделан переносом того же виджета в отдельное окно. Это важно:
воспроизведение не перезапускается, позиция не теряется, страница не грузится
заново - меняется только то, в каком окне лежит уже работающий плеер."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import config
from . import player_icons
from .widgets import ElidedLabel

# Кадр 16:9. Ниже этого область не сжимаем - иначе снова получится полоска.
ASPECT_W, ASPECT_H = 16, 9
MIN_HEIGHT = 160
# В обычном окне видео не должно съедать список: выше этого не растём.
MAX_HEIGHT = 360
# Ширина кадра. Верхнего предела у неё нет: держит кадр отведённая высота
# (set_height_limit), а не круглое число. Пока здесь стояло 640, в широком окне
# по бокам от видео оставалось до 435 px пустоты с каждой стороны - кадр висел
# в середине пустой строки, и место под ним пропадало впустую
MAX_WIDTH = 1 << 20
# Ниже этого кадр превращается в марку: в узком окне лучше занять всю ширину
MIN_WIDTH = 240
HEADER_HEIGHT = 34
# Кнопка выхода из полного экрана: лежит поверх картинки в правом верхнем углу
EXIT_BUTTON = 40
EXIT_MARGIN = 16


class VideoStage(QWidget):
    """Контейнер для страницы плеера с пропорциями 16:9.

    Сам виджет плеера приходит снаружи (его создаёт движок воспроизведения) и
    здесь только лежит: движок один на всё приложение, пересоздавать его нельзя."""

    fullscreen_changed = Signal(bool)
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('videoStage')
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        # Высота зависит от ширины: об этом раскладку нужно предупредить явно
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumHeight(MIN_HEIGHT)
        self.setMaximumHeight(MAX_HEIGHT)
        # Своя колонка: в широком окне кадр не растягивается на всю страницу
        self.setMaximumWidth(MAX_WIDTH)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self._build_header())

        # Пока картинки нет, поверх экрана лежит подпись. Именно поверх, а не
        # вместо: прятать сам плеер нельзя - скрытому Chromium незачем грузить
        # страницу, и вернуть его потом к жизни выходит дороже, чем закрыть.
        self._body = _Body(self)
        self._body.setObjectName('videoBody')
        self._body_box = QVBoxLayout(self._body)
        self._body_box.setContentsMargins(0, 0, 0, 0)
        self._note = QLabel('Готовим видео…', self._body)
        self._note.setObjectName('videoNote')
        self._note.setAlignment(Qt.AlignCenter)
        self._body.set_overlay(self._note)
        box.addWidget(self._body, 1)

        self._widget: QWidget | None = None
        self._window: _FullscreenWindow | None = None
        self._max_height = MAX_HEIGHT
        # 16:9, пока не известно настоящее соотношение ролика
        self._aspect = ASPECT_W / ASPECT_H

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName('videoHeader')
        header.setFixedHeight(HEADER_HEIGHT)
        row = QHBoxLayout(header)
        row.setContentsMargins(12, 0, 6, 0)
        row.setSpacing(4)
        self._title = ElidedLabel('Видео')
        self._title.setObjectName('videoTitle')
        row.addWidget(self._title, 1)
        self._full_btn = self._button(row, 'fullscreen', 'Во весь экран',
                                      self.toggle_fullscreen)
        self._button(row, 'close', 'Свернуть видео, звук останется',
                     self.close_requested.emit)
        return header

    @staticmethod
    def _button(row: QHBoxLayout, icon: str, hint: str, slot) -> QPushButton:
        button = QPushButton()
        button.setObjectName('iconBtn')
        button.setIcon(player_icons.draw(icon, player_icons.COLOR_NORMAL, 16))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(hint)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        row.addWidget(button)
        return button

    # ---------- пропорции ----------
    def set_aspect(self, width: int, height: int) -> None:
        """Подогнать рамку под настоящее соотношение ролика.

        Вертикальное видео в рамке 16:9 плеер вписывает сам, добавляя чёрные
        поля слева и справа - на девятиминутном ролике 9:16 они съедали больше
        половины кадра. Рамка по размеру ролика полей не оставляет.

        Соотношение приходит не всегда: страница YouTube размер кадра не
        сообщает, и для неё остаётся 16:9 - как было."""
        if width <= 0 or height <= 0:
            aspect = ASPECT_W / ASPECT_H
        else:
            # Слишком узкие и слишком широкие рамки одинаково неудобны: кадр
            # либо превращается в столбик, либо не оставляет места очереди
            aspect = min(max(width / height, 0.5), 2.4)
        if abs(aspect - self._aspect) < 0.01:
            return
        self._aspect = aspect
        self.updateGeometry()
        self._sync_height()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        height = HEADER_HEIGHT + int(width / self._aspect)
        # MIN_HEIGHT - пожелание, а не закон: когда странице отвели меньше, сцена
        # обязана ужаться. Раньше пол побеждал потолок, и в окне высотой 300
        # сцена держала свои 179 при отведённых 117 - кадр вылезал за край
        # страницы и наезжал на всё, что под ним
        return min(max(MIN_HEIGHT, min(height, self._max_height)), self._max_height)

    def sizeHint(self) -> QSize:
        """Ширину берём от колонки, а не от содержимого.

        Сцена стоит в раскладке с выравниванием по центру, а такой виджет получает
        ровно sizeHint и ни пикселем больше. Пока ширина считалась от шапки,
        кадр сжимался до полоски в полтораста пикселей при любом размере окна."""
        width = self._column_width()
        return QSize(width, self.heightForWidth(width))

    def _column_width(self) -> int:
        """Ширина кадра - меньшее из двух ограничений: сколько отдаёт
        колонка и сколько позволяет отведённая высота.

        Второе важнее, чем кажется: в невысоком окне высота упиралась в потолок,
        ширина ставилась по колонке - и 16:9 превращалось в растянутую полосу почти
        вдвое шире нужного. Лучше показать кадр меньше, но в верных пропорциях."""
        parent = self.parentWidget()
        available = parent.width() if parent is not None else MAX_WIDTH
        by_height = int((self._max_height - HEADER_HEIGHT) * self._aspect)
        return max(MIN_WIDTH, min(available, MAX_WIDTH, by_height))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self) -> None:
        """Держать кадр 16:9 руками.

        Вертикальная раскладка спрашивает у виджета только sizeHint и растягивает
        его по остатку места - от heightForWidth здесь толку мало, и видео
        сплющивалось в полоску. Поэтому нужную высоту закрепляем сами.

        Считаем от той ширины, что виджету действительно досталась: в сетке она
        задаётся ячейкой и с предсказанием по колонке не совпадает - кадр выходил
        то вдвое шире положенного, то заметно уже."""
        # Шире, чем позволяет отведённая высота, кадр растягивать нельзя: 16:9
        # превратится в панораму. Ячейка сетки тянет виджет во всю ширину, так
        # что предел ставим сами - лишнее место рядом заберут списки
        limit = int((self._max_height - HEADER_HEIGHT) * self._aspect)
        limit = max(MIN_WIDTH, limit)
        if self.maximumWidth() != limit:
            self.setMaximumWidth(limit)
        height = self.heightForWidth(min(self.width() or limit, limit))
        # Минимум приравнивать к этой высоте нельзя. В широком окне 16:9 требует
        # под 450 px, и такой минимум уходил наверх: окно переставало уменьшаться
        # по-настоящему, а разницу между объявленным минимумом и настоящим Qt
        # разбирал, сжимая соседей ниже их собственных минимумов. Кадр держит
        # максимум, а вниз пускаем до MIN_HEIGHT - в низком окне полоска лучше,
        # чем разъехавшаяся раскладка на всех остальных страницах
        floor = min(height, MIN_HEIGHT)
        if self.minimumHeight() != floor or self.maximumHeight() != height:
            self.setMinimumHeight(floor)
            self.setMaximumHeight(height)

    def set_height_limit(self, value: int) -> None:
        """Ограничить высоту сверху - например, чтобы в невысоком окне под видео
        оставался список очереди.

        Пола у лимита нет намеренно: сколько отвели, столько и есть. Пока здесь
        стояло `max(MIN_HEIGHT, ...)`, низкое окно получало сцену выше страницы."""
        self._max_height = max(1, int(value))
        self._sync_height()
        self.updateGeometry()

    # ---------- содержимое ----------
    def set_title(self, text: str) -> None:
        self._title.setText(text or 'Видео')

    def set_note(self, text: str) -> None:
        """Подпись вместо картинки: «Готовим видео…» и подобное. Пустой текст
        возвращает на место сам плеер."""
        if self.fullscreen:
            # На экране сейчас своя подпись, и она важнее. Но запрос всё равно
            # запоминаем: раньше его просто выбрасывали, и если видео
            # догрузилось, пока кадр был во весь экран, на возврате сюда
            # вставало устаревшее «Готовим видео…» - непрозрачная заслонка
            # поверх работающей картинки, снять которую было уже нечем
            self._saved_note = text
            return
        self._note.setText(text)
        self._sync_note()

    def _sync_note(self) -> None:
        """Подпись видна, только пока показывать нечего."""
        wanted = bool(self._note.text())
        self._note.setVisible(wanted)
        if wanted:
            self._note.raise_()

    def set_widget(self, widget: QWidget) -> None:
        """Кто рисует картинку, зависит от источника: страница YouTube или
        видеовыход QMediaPlayer. Прежний виджет прячем, но не удаляем и не
        отвязываем - движок один на приложение и должен пережить переключение."""
        if widget is self._widget:
            return
        self.set_fullscreen(False)
        if self._widget is not None:
            self._body_box.removeWidget(self._widget)
            # Не только вынуть из раскладки, но и увести из контейнера. Внутри и
            # QWebEngineView, и QVideoWidget держат нативное окно, а такое окно
            # рисуется поверх обычных виджетов независимо от порядка в стопке:
            # оставшись ребёнком экрана сцены, прежний плеер накрывал нового
            # чёрным прямоугольником. setParent(None) прячет виджет сам, но
            # объект живёт дальше - движок один на приложение и переживает
            # переключение источника
            self._widget.setParent(None)
        self._widget = widget
        self._body_box.addWidget(widget)
        widget.show()
        # Подпись - ребёнок экрана, а не раскладки, и после добавления нового
        # виджета оказывается под ним. Поднимаем её обратно
        self._sync_note()

    @property
    def widget(self) -> QWidget | None:
        return self._widget

    # ---------- полный экран ----------
    @property
    def fullscreen(self) -> bool:
        return self._window is not None

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self.fullscreen)

    def set_fullscreen(self, enabled: bool) -> None:
        if bool(enabled) == self.fullscreen or self._widget is None:
            return
        if enabled:
            self._saved_note = self._note.text()
            self._window = _FullscreenWindow(self)
            self._window.layout().addWidget(self._widget)
            self._window.showFullScreen()
            self._widget.setFocus()
            # На месте уехавшей картинки - объяснение, а не чёрный провал
            self._note.setText('Видео открыто во весь экран.\n'
                               'Esc или кнопка в углу вернут его сюда')
            self._sync_note()
        else:
            window, self._window = self._window, None
            self._body_box.addWidget(self._widget)
            window.close()
            window.deleteLater()
            self._note.setText(getattr(self, '_saved_note', ''))
            self._sync_note()
        self._full_btn.setIcon(player_icons.draw(
            'collapse' if self.fullscreen else 'fullscreen',
            player_icons.COLOR_NORMAL, 16))
        self.fullscreen_changed.emit(self.fullscreen)

    def mouseDoubleClickEvent(self, event) -> None:
        self.toggle_fullscreen()
        event.accept()


class _Body(QWidget):
    """Чёрный экран сцены. Подпись лежит поверх плеера и растянута по нему."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overlay: QWidget | None = None

    def set_overlay(self, widget: QWidget) -> None:
        self._overlay = widget
        widget.setGeometry(self.rect())
        widget.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._overlay is not None:
            self._overlay.setGeometry(self.rect())


class _FullscreenWindow(QWidget):
    """Отдельное окно на весь экран.

    Выйти отсюда должно быть чем угодно: кнопкой в углу, Esc, F11 или двойным
    щелчком. Одной клавиши мало - видео показывает Chromium, а он забирает
    себе и нажатия, и щелчки, и до окна они не доходят. Поэтому Esc и F11
    висят на QShortcut уровня окна (он срабатывает раньше дочернего виджета),
    а поверх картинки лежит настоящая кнопка."""

    def __init__(self, stage: VideoStage):
        super().__init__(None)
        self.setWindowTitle(f'{config.APP_NAME}: видео')
        self.setStyleSheet('background:#000;')
        self._stage = stage
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)

        # Кнопка - ребёнок окна, а не раскладки: она должна лежать поверх
        # плеера, а не отнимать у него полосу сверху
        self._exit_btn = QPushButton(self)
        self._exit_btn.setObjectName('videoExit')
        self._exit_btn.setIcon(player_icons.draw('collapse', '#ffffff', 18))
        self._exit_btn.setIconSize(QSize(18, 18))
        self._exit_btn.setFixedSize(EXIT_BUTTON, EXIT_BUTTON)
        self._exit_btn.setToolTip('Свернуть видео обратно в окно, Esc')
        self._exit_btn.setCursor(Qt.PointingHandCursor)
        self._exit_btn.clicked.connect(lambda: stage.set_fullscreen(False))

        for key in (Qt.Key_Escape, Qt.Key_F11):
            shortcut = QShortcut(QKeySequence(key), self)
            # WindowShortcut ловит нажатие до того, как его съест Chromium
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(lambda: stage.set_fullscreen(False))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._exit_btn.move(self.width() - EXIT_BUTTON - EXIT_MARGIN, EXIT_MARGIN)
        self._exit_btn.raise_()

    def mouseDoubleClickEvent(self, event) -> None:
        self._stage.set_fullscreen(False)
        event.accept()

    def closeEvent(self, event: QEvent) -> None:
        # Закрыли крестиком или Alt+F4 - виджет плеера обязан вернуться в окно,
        # иначе он уйдёт вместе с этим окном и звук пропадёт
        if self._stage.fullscreen:
            self._stage.set_fullscreen(False)
            event.ignore()
            return
        super().closeEvent(event)
