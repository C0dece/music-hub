"""Полоса плеера внизу окна - она видна на любой вкладке.

Смысл раздела музыки в том, что трек играет независимо от того, куда ушёл
пользователь: из VK перешли в YouTube, оттуда в загрузки - музыка не прерывается,
а управление всегда под рукой.

Здесь собран весь набор управления: перемотка, громкость, перемешивание, повтор,
автопродолжение, избранное, «+ VK», очередь и переключение аудио/видео. Значки
векторные (player_icons), а не эмодзи: эмодзи в каждой сборке Windows выглядят
по-своему и не умеют подсвечиваться под включённый режим.

Ширину полоса держит скромно: длинные названия сокращаются (ElidedLabel), а ряды
кнопок при нехватке места переносятся (FlowRow). Из-за этого окно можно сузить до
620 px, как и остальные разделы."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

from ..core.player_controller import (MODE_AUDIO, MODE_AUTO, MODE_VIDEO, REPEAT_ALL,
                                      REPEAT_OFF, REPEAT_ONE, STATE_LABELS,
                                      STATE_PLAYING, STATE_STOPPED, PlayerController)
from ..core.track import SOURCE_VK, Track
from . import covers, player_icons
from .flow_layout import FlowRow
from .track_list import format_time
from .track_actions import TrackActions, fill_menu
from .widgets import ElidedLabel

COVER_SIZE = 52
# Длина сообщения о состоянии, после которой чип начинает распирать полосу
STATUS_LIMIT = 22
# Ширины полосы, на которых она сжимается.
#
# Правило одно: убрать с глаз можно, отобрать нельзя - всё спрятанное
# остаётся в меню «Ещё» (_add_hidden_toggles и fill_menu). Пороги не на глаз:
# измерено, что полосе нужно 182 (обложка с текстами) + 240 (перемотка) +
# правая зона + 56 на поля и зазоры. Правая зона весит 388 целиком,
# 332 без ползунка и 240 без ползунка и «+ VK» - отсюда и числа ниже.
#
# Порядок сжатия - от того, чему замена ближе всего: сначала узкий ползунок,
# потом один динамик (громкость - колесом по нему или пунктом меню), и лишь
# в самом узком окне - кнопки оценок и «+ VK», которым есть полноценный
# список действий над треком в том же меню.
#
# Раньше пороги были вдвое выше (900 и 1010): правая зона получала долю окна
# вместо своей ширины, и кнопки приходилось прятать задолго до нехватки места.
# Каждое число - измеренная цена своего набора, а не круглая прикидка: пока
# порог стоял ниже цены, кнопки включались там, где места на них уже не было,
# раскладке не хватало ширины, и она отыгрывалась высотой - полоса вырастала
# с 73 px до 94 и тянула за собой пустую вторую строку
WIDE_WIDTH = 940
COMPACT_WIDTH = 810
# Ниже этой ширины ползунок громкости прячется целиком, остаётся один динамик
VOLUME_HIDE_WIDTH = 866
# Самый сжатый набор: обложка с текстами, перемотка и четыре значка справа
MIN_BAR_WIDTH = 600
VOLUME_WIDTH, VOLUME_WIDTH_TIGHT = 84, 54
# Сколько всё ещё читается от названия трека и исполнителя
TEXTS_MIN_WIDTH = 120
# Круглая кнопка воспроизведения: размер задан и в QSS (`#playBtn`), менять надо там же
PLAY_SIZE = 44
# Высота центра: кнопки, отступ и полоса перемотки с таймингами
TRANSPORT_HEIGHT = PLAY_SIZE + 4 + 16
# Высота полосы: самая высокая из зон плюс поля 10 сверху и снизу, ещё пиксель на
# рамку. Раньше здесь стояло 73 - высота по левой зоне (обложка 52), и центру
# доставалось 52 вместо нужных 64. Кнопка воспроизведения жёстко зафиксирована в
# QSS, сжаться на эти 12 px она не могла и уезжала вниз, накрывая собой полосу
# перемотки; на глаз это и выглядело как «play налезает на другие элементы»
BAR_HEIGHT = max(COVER_SIZE, TRANSPORT_HEIGHT) + 10 * 2 + 1


class NowPlayingBar(QFrame):
    """Постоянная панель управления: обложка, название, перемотка, действия."""

    add_to_vk_requested = Signal(object)   # Track
    favorite_toggled = Signal(object)      # Track
    queue_requested = Signal()
    open_source_requested = Signal(object)
    radio_requested = Signal(object)
    download_requested = Signal(object)
    artist_requested = Signal(str)
    fullscreen_requested = Signal()
    expand_requested = Signal()
    mini_player_requested = Signal()
    playlist_requested = Signal(object, int)  # треки, id плейлиста
    hide_requested = Signal(object)           # список треков

    def __init__(self, player: PlayerController, parent=None):
        super().__init__(parent)
        self.setObjectName('nowPlaying')
        self.setFrameShape(QFrame.NoFrame)
        # Высота полосы постоянна и задана прямо: 73 px, столько же она получала
        # на всех ширинах от 600 до 1600, когда раскладка успевала устояться.
        #
        # Гибкой она была ради переноса кнопок на вторую строку, но переносить
        # нечего: правая зона сама прячет лишнее под «⋯». Зато resizeEvent
        # полосы приходит уже после того, как окно раздало высоты, и в этот
        # кадр полоса успевала получить 94, 134 или все 174 px - с пустотой над
        # собой. Каждое движение мышью за край окна давало такой кадр: полоса
        # дёргалась, а интерфейс выглядел подтормаживающим. Ширины это не
        # касается - по ней полоса по-прежнему тянется и сжимается
        self.setFixedHeight(BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._player = player
        self._track: Track | None = None
        self._seeking = False
        self._store = None
        self._cover_url = ''

        self._build_ui()

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)
        player.volume_changed.connect(self._on_volume)
        player.shuffle_changed.connect(self._on_shuffle)
        player.repeat_changed.connect(self._on_repeat)
        player.autoplay_changed.connect(self._on_autoplay)
        player.mode_changed.connect(self._on_mode)
        player.notice.connect(self._on_notice)

        self._on_track(None)
        self._on_volume(player.volume)
        self._on_shuffle(player.shuffle)
        self._on_repeat(player.repeat)
        self._on_autoplay(player.autoplay)
        self._on_mode(player.mode)

    # ---------- интерфейс ----------
    def _build_ui(self) -> None:
        """Три зоны: что играет - управление - всё остальное.

        Так полоса читается слева направо, а взгляд не ищет кнопку «играть»
        среди одинаковых значков: она одна крупная и стоит по центру."""
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(14)
        # Доли были 4:5:4, и правая зона получала четверть окна вместо того,
        # что ей нужно: семь кнопок вставали в строку только после 1700 px, а до
        # того последний значок уезжал на вторую строку и полоса растала с 73 до
        # 94 px ради одной кнопки. Лишнюю ширину забирает центр: его полосе
        # перемотки место всегда на пользу
        outer.addWidget(self._build_track_side(), 3)
        outer.addWidget(self._build_transport(), 5)
        # Без доли: правой зоне нужна не доля окна, а своя ширина - её
        # задаёт _sync_extras_width по фактически видимым кнопкам
        self._extras_pending = False
        self._extras = self._build_extras()
        outer.addWidget(self._extras, 0)

    def _build_track_side(self) -> QWidget:
        """Слева: обложка, название, исполнитель и сердце."""
        side = QWidget()
        box = QHBoxLayout(side)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        self._cover = QLabel()
        self._cover.setObjectName('cover')
        self._cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self._cover.setPixmap(covers.placeholder(COVER_SIZE))
        self._cover.setCursor(Qt.PointingHandCursor)
        self._cover.setToolTip('Открыть плеер целиком')
        box.addWidget(self._cover)

        # Тексты в отдельном виджете с потолком по ширине: иначе длинное
        # название растаскивало бы сердце к середине полосы
        texts = QWidget()
        texts.setMaximumWidth(340)
        titles = QVBoxLayout(texts)
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(2)
        # Растяжки сверху и снизу: без них лишняя высота полосы делится между
        # строками, и между названием и исполнителем появляется зазор
        titles.addStretch(1)
        self._title = ElidedLabel('Ничего не играет')
        self._title.setObjectName('npTitle')
        titles.addWidget(self._title)

        under = QHBoxLayout()
        under.setContentsMargins(0, 0, 0, 0)
        under.setSpacing(6)
        self._subtitle = ElidedLabel('')
        self._subtitle.setObjectName('hint')
        under.addWidget(self._subtitle)
        # Состояние («Загрузка…», «Ограниченный режим») - чипом под названием:
        # отдельной строки под него в полосе нет, а терять сообщения нельзя
        # Обычная метка, а не сжимаемая: чип с обрезанным словом «Загрузк…»
        # выглядит поломкой. Место уступает подзаголовок - он длиннее и не так
        # важен
        self._status = QLabel('')
        self._status.setObjectName('chip')
        self._status.setProperty('state', 'off')
        self._status.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._status.hide()
        under.addWidget(self._status)
        under.addStretch(1)
        titles.addLayout(under)
        titles.addStretch(1)
        # Название и исполнитель стоят вплотную под обложкой: сердце уехало
        # вправо, к «+ VK», и разрывать пару собой больше не может
        box.addWidget(texts)
        box.addStretch(1)
        # Минимум колонки с текстами меньше ширины строки: обе сжимаемые,
        # им есть куда уменьшаться
        texts.setMinimumWidth(TEXTS_MIN_WIDTH)
        # Раньше здесь стояло круглое 170 - меньше, чем занимают обложка
        # и тексты вместе. В узком окне раскладка верила этому числу, и
        # название трека выходило за свою зону, налезая на кнопки перемотки
        side.setMinimumWidth(COVER_SIZE + box.spacing() + TEXTS_MIN_WIDTH)
        return side

    def _build_transport(self) -> QWidget:
        """Центр: кнопки управления и полоса перемотки с таймингами."""
        center = QWidget()
        box = QVBoxLayout(center)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self._shuffle_btn = self._icon_button(buttons, 'shuffle', 'Перемешать',
                                              self._player.toggle_shuffle)
        self._prev_btn = self._icon_button(buttons, 'previous', 'Предыдущий',
                                           self._player.previous)
        self._play_btn = QPushButton()
        self._play_btn.setObjectName('playBtn')
        self._play_btn.setIcon(player_icons.draw('play', '#ffffff', 22))
        self._play_btn.setIconSize(QSize(22, 22))
        self._play_btn.setToolTip('Играть')
        self._play_btn.setCursor(Qt.PointingHandCursor)
        self._play_btn.clicked.connect(self._player.toggle)
        buttons.addWidget(self._play_btn)
        self._next_btn = self._icon_button(buttons, 'next', 'Следующий', self._player.next)
        self._repeat_btn = self._icon_button(buttons, 'repeat', 'Повтор',
                                             self._player.cycle_repeat)
        self._autoplay_btn = self._icon_button(
            buttons, 'autoplay', 'Продолжать похожим, когда очередь кончится',
            self._player.toggle_autoplay)
        buttons.addStretch(1)
        box.addLayout(buttons)

        seek_row = QHBoxLayout()
        seek_row.setContentsMargins(0, 0, 0, 0)
        seek_row.setSpacing(8)
        self._elapsed = QLabel('0:00')
        self._elapsed.setObjectName('npTime')
        seek_row.addWidget(self._elapsed)
        self._seek = QSlider(Qt.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.setMinimumWidth(90)
        self._seek.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._seek.sliderPressed.connect(self._on_seek_pressed)
        self._seek.sliderReleased.connect(self._on_seek_released)
        self._seek.sliderMoved.connect(
            lambda value: self._elapsed.setText(format_time(value)))
        seek_row.addWidget(self._seek, 1)
        self._total = QLabel('0:00')
        self._total.setObjectName('npTime')
        seek_row.addWidget(self._total)
        box.addLayout(seek_row)
        return center

    def _build_extras(self) -> QWidget:
        """Справа: перенос в VK, режим, громкость, очередь и общее меню.

        Полоска переносимая: в узком окне значки уедут на вторую строку, а не
        выдавят название трека за край."""
        row = FlowRow(spacing=6, align_right=True)
        row.add_stretch()
        self._fav_btn = self._icon_button(row, 'heart', 'В избранное', self._on_favorite)
        # Пара к сердцу: подборки строятся по обеим оценкам, а не по одной.
        # Раньше «не нравится» пряталось в меню правой кнопкой по строке списка,
        # и до играющего трека дотянуться было нечем
        self._dislike_btn = self._icon_button(row, 'dislike', 'Не нравится',
                                              self._on_dislike)
        self._vk_btn = row.add(QPushButton('+ VK'))
        self._vk_btn.setObjectName('secondary')
        self._vk_btn.setToolTip('Добавить трек в «Мою музыку» VK')
        self._vk_btn.setCursor(Qt.PointingHandCursor)
        self._vk_btn.clicked.connect(self._on_add_vk)
        # Одно нажатие включает и выключает картинку - за режимами «по
        # источнику / только звук» правая кнопка: меню на каждый показ видео
        # оказалось слишком долгой дорогой
        self._mode_btn = self._icon_button(row, 'video', 'Показать видео',
                                           self._toggle_video)
        self._mode_btn.setContextMenuPolicy(Qt.NoContextMenu)

        volume_box = QWidget()
        volume_layout = QHBoxLayout(volume_box)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(2)
        self._mute_btn = self._icon_button(volume_layout, 'volume', 'Выключить звук',
                                           self._player.toggle_mute)
        # Колесом по динамику громкость меняется и тогда, когда ползунка нет
        self._mute_btn.wheelEvent = self._volume_wheel
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setFixedWidth(84)
        self._volume.setToolTip('Громкость')
        self._volume.valueChanged.connect(self._on_volume_moved)
        volume_layout.addWidget(self._volume)
        # Контейнер держим под рукой: когда ползунок прячут, только его
        # собственная раскладка знает новую ширину пары «динамик + ползунок»
        self._volume_box = volume_box
        row.add(volume_box)

        self._queue_btn = self._icon_button(row, 'queue', 'Очередь',
                                            self.queue_requested.emit)
        self._more_btn = self._icon_button(row, 'more', 'Ещё', self._show_menu)
        return row

    def _icon_button(self, box, icon: str, tip: str, slot) -> QPushButton:
        """Значок без подписи. `box` - обычная раскладка или переносимая полоска."""
        button = QPushButton()
        button.setObjectName('iconBtn')
        button.setIcon(player_icons.draw(icon))
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(34, 34)
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        add = getattr(box, 'add', None)
        if add is not None:
            add(button)
        else:
            box.addWidget(button)
        return button

    def resizeEvent(self, event) -> None:
        """В узкой полосе ужимаем второстепенное, чтобы осталось название трека."""
        super().resizeEvent(event)
        width = self.width()
        if width >= WIDE_WIDTH:
            self._volume.setVisible(True)
            self._volume.setFixedWidth(VOLUME_WIDTH)
        elif width >= VOLUME_HIDE_WIDTH:
            # Короткий ползунок всё ещё удобнее, чем его отсутствие
            self._volume.setVisible(True)
            self._volume.setFixedWidth(VOLUME_WIDTH_TIGHT)
        else:
            # Динамик остаётся: он выключает звук, а громкость крутится колесом
            self._volume.setVisible(False)
        # Пересчитать зону громкости прямо сейчас, до замера ширины ниже.
        # Без этого контейнер отдавал ширину с ползунком (120 вместо 34) даже
        # после того, как ползунок спрятали: его раскладка обновлялась только
        # следующим проходом. Правая зона просила на 86 px больше, чем ей нужно,
        # в окно не влезала - и полоса на один кадр разъезжалась на две строки,
        # с 73 px до 94. Каждое движение мышью за край окна давало такой кадр:
        # полоса дёргалась, а под ней мигала пустота
        self._volume_box.layout().activate()
        self._volume_box.adjustSize()
        compact = width < COMPACT_WIDTH
        self._shuffle_btn.setVisible(not compact)
        self._autoplay_btn.setVisible(not compact)
        # Сердце и «+ VK» стоят парой и уходят вместе: одинокое сердце рядом с
        # пустым местом выглядит так, будто кнопку потеряли
        self._vk_btn.setVisible(not compact)
        self._fav_btn.setVisible(not compact)
        self._dislike_btn.setVisible(not compact)
        self._sync_extras_width()

    def minimumSizeHint(self) -> QSize:
        """Минимум - по самому сжатому набору, а не по нынешнему.

        Раскладка спрашивает минимум раньше, чем придёт resizeEvent с новой
        шириной, и пока минимум считался по видимым сейчас кнопкам, полоса
        требовала больше, чем есть в окне, и содержимое вылезало за край."""
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), MIN_BAR_WIDTH), hint.height())

    def _sync_extras_width(self) -> None:
        """Отдать правой зоне ровно столько, сколько нужно на одну строку.

        Раньше ширина шла долей от окна, и в середине диапазона последний значок
        не влезал, уезжал на вторую строку и тянул за собой всю полосу: 73 px
        превращались в 94 ради одной кнопки, а рядом зияла пустота.

        Считаем дважды: сразу и следующим проходом раскладки. Только что
        спрятанная кнопка и заданная ползунку ширина вступают в силу позже
        этого вызова, и по первому расчёту зона просила ширину прошлого
        размера окна - а лишнего места в узком окне нет, и полоса ломалась
        на две строки."""
        self._extras.setFixedWidth(self._extras.one_line_width())
        if not self._extras_pending:
            self._extras_pending = True
            QTimer.singleShot(0, self._resync_extras_width)

    def _resync_extras_width(self) -> None:
        self._extras_pending = False
        width = self._extras.one_line_width()
        if width != self._extras.width():
            self._extras.setFixedWidth(width)
            # Ширина сменилась - прежняя высота считалась по старой и держит
            # полосу в двух строках, хотя переносить уже нечего
            self.updateGeometry()

    def _set_status(self, text: str) -> None:
        """Чип состояния показываем, только когда есть что сказать.

        Длинные сообщения плеера («Видео недоступно в вашей стране») в полосу не
        влезают: на чипе остаётся начало, целиком текст лежит в подсказке."""
        short = text if len(text) <= STATUS_LIMIT else text[:STATUS_LIMIT - 1] + '…'
        self._status.setText(short)
        self._status.setToolTip(text if short != text else '')
        # Под названием помещается что-то одно. Пока плеер говорит о состоянии,
        # оно важнее исполнителя - тот и так написан в списке
        self._status.setVisible(bool(text))
        self._subtitle.setVisible(not text)

    # ---------- внешнее состояние ----------
    def set_vk_state(self, uid: str, label: str) -> None:
        """Подпись кнопки «+ VK» для конкретного трека (сервис переноса шлёт её сам)."""
        if self._track is not None and self._track.uid == uid:
            self._vk_btn.setText(label or '+ VK')
            # Пока перенос идёт, повторное нажатие ни к чему - сервис его всё равно
            # отбросит, но кнопка не должна выглядеть работающей
            self._vk_btn.setEnabled(not label or label == '+ VK')

    def set_favorite(self, is_favorite: bool) -> None:
        self._fav_btn.setIcon(player_icons.toggled('heart', is_favorite))
        self._fav_btn.setToolTip('Убрать из избранного' if is_favorite else 'В избранное')

    def set_note(self, text: str) -> None:
        """Короткая приписка под названием: «Ограниченный режим» и подобное."""
        self._set_status(text)

    # ---------- сигналы плеера ----------
    def _on_track(self, track) -> None:
        self._track = track
        has = track is not None
        self._title.setText(track.title if has else 'Ничего не играет')
        if has:
            parts = [part for part in (track.artist, track.source_label) if part]
            self._subtitle.setText(' · '.join(parts))
        else:
            self._subtitle.setText('Выберите трек в VK, YouTube или библиотеке')
        for button in (self._prev_btn, self._play_btn, self._next_btn,
                       self._fav_btn, self._more_btn):
            button.setEnabled(has)
        self._seek.setEnabled(has)
        # Метку ставит окно через set_vk_state: чужая запись VK тоже из VK, но
        # её можно добавить к себе, и знает об этом только сервис переноса
        self._vk_btn.setText('✓ В VK' if has and track.in_vk and track.source != SOURCE_VK
                             else '+ VK')
        self._vk_btn.setEnabled(has and not (track.in_vk and track.source != SOURCE_VK))
        self._set_cover(track.cover if has else '')
        self._seek.setRange(0, track.duration * 1000 if has else 0)
        self._total.setText(format_time(track.duration * 1000) if has else '0:00')
        self._elapsed.setText('0:00')
        self._set_status('')

    def _set_cover(self, url: str) -> None:
        self._cover_url = url or ''
        ready = covers.cached(url) if url else None
        if ready is not None:
            self._cover.setPixmap(covers.rounded(ready, COVER_SIZE))
            return
        self._cover.setPixmap(covers.placeholder(COVER_SIZE))
        if url:
            covers.load(url, self._on_cover_ready)

    def _on_cover_ready(self, url: str, pixmap) -> None:
        # Пока картинка ехала, мог смениться трек - чужую обложку не ставим
        if url == self._cover_url:
            self._cover.setPixmap(covers.rounded(pixmap, COVER_SIZE))

    def _on_state(self, state: str) -> None:
        # Кнопка показывает «пауза» только когда звук действительно идёт: на
        # загрузке и на ошибке она обязана оставаться кнопкой «играть»
        playing = state == STATE_PLAYING
        self._play_btn.setIcon(player_icons.draw('pause' if playing else 'play',
                                                 '#ffffff', 22))
        self._play_btn.setToolTip('Пауза' if playing else 'Играть')
        self._set_status(STATE_LABELS.get(state, ''))
        if state == STATE_STOPPED:
            self._seek.setValue(0)
            self._elapsed.setText('0:00')

    def _on_position(self, position: int, duration: int) -> None:
        if duration > 0 and self._seek.maximum() != duration:
            self._seek.setRange(0, duration)
            self._total.setText(format_time(duration))
        if not self._seeking:
            self._seek.setValue(position)
            self._elapsed.setText(format_time(position))

    def _volume_wheel(self, event) -> None:
        """Шаг в 5 %: колесо крутят грубо, а по одному проценту его не поймать."""
        step = 5 if event.angleDelta().y() > 0 else -5
        self._player.set_volume(max(0, min(100, self._player.volume + step)))
        event.accept()

    def _on_volume(self, volume: int) -> None:
        if self._volume.value() != volume:
            self._volume.blockSignals(True)
            self._volume.setValue(volume)
            self._volume.blockSignals(False)
        muted = volume == 0
        self._mute_btn.setIcon(player_icons.draw('mute' if muted else 'volume'))
        self._mute_btn.setToolTip('Включить звук' if muted else 'Выключить звук')

    def _on_shuffle(self, enabled: bool) -> None:
        self._shuffle_btn.setIcon(player_icons.toggled('shuffle', enabled))
        self._shuffle_btn.setToolTip('Перемешивание включено' if enabled
                                     else 'Перемешать очередь')

    def _on_repeat(self, mode: str) -> None:
        name = 'repeat_one' if mode == REPEAT_ONE else 'repeat'
        self._repeat_btn.setIcon(player_icons.toggled(name, mode != REPEAT_OFF))
        self._repeat_btn.setToolTip({REPEAT_OFF: 'Повтор выключен',
                                     REPEAT_ALL: 'Повторять очередь',
                                     REPEAT_ONE: 'Повторять трек'}.get(mode, 'Повтор'))

    def _on_autoplay(self, enabled: bool) -> None:
        self._autoplay_btn.setIcon(player_icons.toggled('autoplay', enabled))
        self._autoplay_btn.setToolTip('Продолжать похожим, когда очередь кончится'
                                      if enabled else 'Останавливаться в конце очереди')

    def _on_mode(self, mode: str) -> None:
        video = mode == MODE_VIDEO
        self._mode_btn.setIcon(player_icons.toggled('video' if video else 'audio', video))
        hint = 'Скрыть видео' if video else 'Показать видео'
        if mode == MODE_AUDIO:
            hint = 'Сейчас только звук, показать видео'
        self._mode_btn.setToolTip(hint)

    def _on_notice(self, text: str) -> None:
        self._set_status(text)

    # ---------- действия ----------
    def _on_volume_moved(self, value: int) -> None:
        self._player.set_volume(value)

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._player.seek(self._seek.value())

    def _on_add_vk(self) -> None:
        if self._track is not None:
            self.add_to_vk_requested.emit(self._track)

    def _on_favorite(self) -> None:
        if self._track is not None:
            self.favorite_toggled.emit(self._track)

    def _on_dislike(self) -> None:
        """Убрать трек из подборок и сразу перейти к следующему: держать в
        ушах то, что только что отметили лишним, незачем."""
        if self._track is not None:
            self.hide_requested.emit([self._track])

    def mouseDoubleClickEvent(self, event) -> None:
        if self._track is not None:
            self.expand_requested.emit()
        event.accept()

    def _toggle_video(self) -> None:
        """Показать картинку или вернуться к звуку. Выключаем в «по источнику»,
        а не в «только звук»: своё видео с диска смотреть всё-таки нужно."""
        self._player.set_mode(MODE_AUTO if self._player.mode == MODE_VIDEO
                              else MODE_VIDEO)

    def _show_menu(self) -> None:
        """Действия над треком, который играет.

        Раньше сюда складывали ещё и вид окна, и режимы плеера, хотя всё это
        есть кнопками рядом. Осталось то, чему в полосе места нет: список
        действий над записью. Переключатели показываются, только когда их
        кнопки спрятаны узким окном, а вид окна ушёл в отдельное подменю внизу.
        """
        menu = QMenu(self)
        track = self._track
        if track is not None:
            actions = TrackActions(
                add_vk=lambda tracks: self.add_to_vk_requested.emit(tracks[0]),
                favorite=lambda tracks: self.favorite_toggled.emit(tracks[0]),
                playlist=lambda tracks, pid: self.playlist_requested.emit(tracks, pid),
                download=lambda tracks: self.download_requested.emit(tracks[0]),
                radio=lambda item: self.radio_requested.emit(item),
                artist=lambda name: self.artist_requested.emit(name),
                hide=lambda tracks: self.hide_requested.emit(tracks),
                open_source=lambda item: self.open_source_requested.emit(item))
            fill_menu(menu, [track], actions, store=self._store)
            menu.addSeparator()

        self._add_hidden_toggles(menu)

        window = menu.addMenu('Окно плеера')
        window.addAction('Открыть страницу трека', self.expand_requested.emit)
        window.addAction('Мини-плеер поверх окон', self.mini_player_requested.emit)
        window.addAction('Показать очередь', self.queue_requested.emit)

        # Режим картинки живёт здесь: раньше он открывался правой кнопкой на
        # самом переключателе, и найти его можно было только случайно
        video = menu.addMenu('Картинка')
        for mode, title in ((MODE_AUTO, 'По источнику'), (MODE_AUDIO, 'Только звук'),
                            (MODE_VIDEO, 'Всегда показывать видео')):
            action = video.addAction(title, lambda m=mode: self._player.set_mode(m))
            action.setCheckable(True)
            action.setChecked(self._player.mode == mode)
        video.addSeparator()
        video.addAction('Видео во весь экран', self.fullscreen_requested.emit)

        menu.addSeparator()
        menu.addAction('Остановить воспроизведение', self._player.stop)
        menu.exec(self._more_btn.mapToGlobal(self._more_btn.rect().bottomLeft()))

    def _add_hidden_toggles(self, menu: QMenu) -> None:
        """Всё, чего сейчас нет кнопкой, обязано быть здесь.

        Узкое окно прячет часть полосы, но прячет - не значит отбирает: до любого
        действия должна оставаться дорога. Показываем только скрытое: иначе одно
        и то же предлагалось бы дважды - кнопкой и пунктом."""
        added = False
        if not self._shuffle_btn.isVisible():
            action = menu.addAction('Перемешивание', self._player.toggle_shuffle)
            action.setCheckable(True)
            action.setChecked(self._player.shuffle)
            added = True
        if not self._autoplay_btn.isVisible():
            action = menu.addAction('Продолжать похожим', self._player.toggle_autoplay)
            action.setCheckable(True)
            action.setChecked(self._player.autoplay)
            added = True
        if not self._volume.isVisible():
            # Ползунка нет - громкость оставалась только на колесе по динамику,
            # а это не угадать. Шаг крупный: меню - не место для точной настройки
            volume = menu.addMenu('Громкость')
            for value in (0, 25, 50, 75, 100):
                action = volume.addAction(f'{value} %',
                                          lambda v=value: self._player.set_volume(v))
                action.setCheckable(True)
                action.setChecked(abs(self._player.volume - value) < 13)
            added = True
        if added:
            menu.addSeparator()


    def set_store(self, store) -> None:
        self._store = store

