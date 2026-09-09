"""Всплывающее окно у значка часов - в духе панелей Windows 11.

Раньше по значку открывалось текстовое меню: чтобы понять, что играет, надо было
прочитать строку. Здесь то же самое показано так, как это делает система, -
обложка, название, транспорт и громкость, а не список пунктов.

Окно ничего не решает само: как и мини-плеер, это ещё один вид на тот же
PlayerController. Всё, что выходит за пределы плеера (открыть окно, выйти из
программы), уходит сигналами наружу.

Закрывается само по потере фокуса - этим занимается флаг Qt.Popup, поэтому
собственного «нажали мимо» здесь нет.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from ..core.player_controller import (REPEAT_OFF, REPEAT_ONE, STATE_PLAYING,
                                      STATE_STOPPED, PlayerController)
from ..core.track import SOURCE_VK, Track
from .. import config
from . import covers, player_icons
from .track_list import format_time
from .widgets import ElidedLabel

COVER_SIZE = 64
# Ширина не круглая, а посчитанная: в строку транспорта встают девять кнопок
# (BUTTON_WIDTH каждая, средняя шире) плюс восемь промежутков BUTTON_SPACING и
# поля SIDE_MARGIN с обеих сторон. Прежние 340 были меньше этой суммы на 70 px,
# и кнопки налезали друг на друга - на панели они выглядели слипшимися.
BUTTON_WIDTH = 34
PLAY_WIDTH = 42
BUTTON_SPACING = 3
SIDE_MARGIN = 14
FLYOUT_WIDTH = (BUTTON_WIDTH * 8 + PLAY_WIDTH + BUTTON_SPACING * 8
                + SIDE_MARGIN * 2)          # 366
SCREEN_MARGIN = 12          # отступ от края рабочей области, как у панелей Win11


class TrayFlyout(QWidget):
    """Панель управления плеером, всплывающая по щелчку на значке у часов."""

    open_window_requested = Signal()
    quit_requested = Signal()
    favorite_toggled = Signal()
    add_to_vk_requested = Signal()
    hide_requested = Signal(object)

    def __init__(self, player: PlayerController, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setObjectName('trayFlyout')
        self.setFixedWidth(FLYOUT_WIDTH)

        self._player = player
        self._track: Track | None = None
        self._seeking = False
        self._cover_url = ''

        self._build_ui()

        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.position_changed.connect(self._on_position)
        player.shuffle_changed.connect(self._on_shuffle)
        player.repeat_changed.connect(self._on_repeat)
        player.volume_changed.connect(self._on_volume)

        self._on_track(player.current)
        self._on_state(player.state)
        self._on_shuffle(player.shuffle)
        self._on_repeat(player.repeat)
        self._on_volume(player.volume)

    # ---------- сборка ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SIDE_MARGIN, 12, SIDE_MARGIN, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self._cover = QLabel()
        self._cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self._cover.setPixmap(covers.placeholder(COVER_SIZE))
        head.addWidget(self._cover)

        names = QVBoxLayout()
        names.setSpacing(2)
        names.addStretch(1)
        self._title = ElidedLabel('Ничего не играет')
        self._title.setObjectName('npTitle')
        self._subtitle = ElidedLabel('Откройте окно и выберите трек')
        self._subtitle.setObjectName('hint')
        names.addWidget(self._title)
        names.addWidget(self._subtitle)
        names.addStretch(1)
        head.addLayout(names, 1)

        # Крестик закрывает программу, а не окошко: окошко и так уходит по клику
        # мимо, и отдельная кнопка «спрятать панель» была бы кнопкой в никуда
        self._quit_btn = self._icon_button('close', f'Выйти из {config.APP_NAME}',
                                           self._on_quit)
        self._quit_btn.setFixedSize(26, 26)
        corner = QVBoxLayout()
        corner.setSpacing(0)
        corner.addWidget(self._quit_btn)
        corner.addStretch(1)
        head.addLayout(corner)
        root.addLayout(head)

        # Время, перемотка и громкость в одной строке: две полосы одна под другой
        # читались как две шкалы одного и того же, и панель была выше без нужды
        line = QHBoxLayout()
        line.setSpacing(6)
        self._elapsed = QLabel('0:00')
        self._elapsed.setObjectName('hint')
        self._total = QLabel('0:00')
        self._total.setObjectName('hint')
        self._seek = QSlider(Qt.Horizontal)
        self._seek.setRange(0, 0)
        self._seek.sliderPressed.connect(self._on_seek_pressed)
        self._seek.sliderReleased.connect(self._on_seek_released)
        line.addWidget(self._elapsed)
        line.addWidget(self._seek, 1)
        line.addWidget(self._total)

        self._mute_btn = self._icon_button('volume', 'Выключить звук', self._toggle_mute)
        self._mute_btn.setFixedSize(26, 26)
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setFixedWidth(72)
        self._volume.setToolTip('Громкость')
        self._volume.valueChanged.connect(self._on_volume_moved)
        line.addWidget(self._mute_btn)
        line.addWidget(self._volume)
        root.addLayout(line)

        buttons = QHBoxLayout()
        buttons.setSpacing(BUTTON_SPACING)
        self._shuffle_btn = self._icon_button('shuffle', 'Перемешать очередь',
                                              self._player.toggle_shuffle)
        buttons.addWidget(self._shuffle_btn)
        self._fav_btn = self._icon_button('heart', 'В избранное',
                                          self.favorite_toggled.emit)
        buttons.addWidget(self._fav_btn)
        # Оценка нужна там же, где играет музыка: из панели у часов до списка
        # с меню правой кнопки не добраться, а подборки строятся по обеим оценкам
        self._dislike_btn = self._icon_button('dislike', 'Не нравится',
                                              self._on_dislike)
        buttons.addWidget(self._dislike_btn)
        # Растяжек между группами больше нет: строка занята кнопками ровно по
        # ширине панели, а растяжка при нехватке места сжимала бы их ниже минимума
        self._prev_btn = self._icon_button('previous', 'Предыдущий',
                                           self._player.previous)
        buttons.addWidget(self._prev_btn)
        self._play_btn = self._icon_button('play', 'Играть', self._player.toggle,
                                           primary=True)
        self._play_btn.setFixedWidth(PLAY_WIDTH)
        buttons.addWidget(self._play_btn)
        self._next_btn = self._icon_button('next', 'Следующий', self._player.next)
        buttons.addWidget(self._next_btn)
        self._vk_btn = self._icon_button('plus', 'Добавить в музыку VK',
                                         self.add_to_vk_requested.emit)
        buttons.addWidget(self._vk_btn)
        self._repeat_btn = self._icon_button('repeat', 'Повтор',
                                             self._player.cycle_repeat)
        buttons.addWidget(self._repeat_btn)
        root.addLayout(buttons)

        self._open_btn = QPushButton(f'Открыть {config.APP_NAME}')
        self._open_btn.setObjectName('secondary')
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._on_open_window)
        root.addWidget(self._open_btn)

    def _icon_button(self, icon: str, tip: str, slot,
                     primary: bool = False) -> QPushButton:
        button = QPushButton()
        if not primary:
            button.setObjectName('secondary')
        button.setIcon(player_icons.draw(icon))
        button.setToolTip(tip)
        button.setFixedWidth(BUTTON_WIDTH)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    # ---------- показ ----------
    def popup_near_cursor(self) -> None:
        """Показывает панель у курсора, прижав её к краю рабочей области."""
        self.adjustSize()
        cursor = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        if screen is None:                       # экранов нет только в тестах
            self.show()
            return
        area = screen.availableGeometry()
        size = self.sizeHint()

        # Значок обычно внизу справа, поэтому панель раскрывается вверх и влево;
        # на другом краю экрана те же вычисления сами её развернут
        left = area.left() + SCREEN_MARGIN
        right = area.right() - size.width() - SCREEN_MARGIN
        x = min(max(cursor.x() - size.width() // 2, left), max(left, right))
        y = (area.bottom() - size.height() - SCREEN_MARGIN
             if cursor.y() > area.center().y() else area.top() + SCREEN_MARGIN)
        self.move(QPoint(int(x), int(y)))
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------- сигналы плеера ----------
    def _on_track(self, track) -> None:
        self._track = track
        has = track is not None
        self._title.setText(track.title if has else 'Ничего не играет')
        self._subtitle.setText(
            ' · '.join(part for part in (track.artist, track.source_label) if part)
            if has else 'Откройте окно и выберите трек')
        for button in (self._prev_btn, self._play_btn, self._next_btn,
                       self._fav_btn, self._dislike_btn):
            button.setEnabled(has)
        # Трек с YouTube кнопка переносит в VK, чужой из VK добавляет в вашу
        # музыку. Свои записи отмечает окно через set_vk_state: по источнику
        # своё от чужого не отличить
        in_vk = bool(has and track.in_vk and track.source != SOURCE_VK)
        self._vk_btn.setEnabled(has and not in_vk)
        self._vk_btn.setIcon(player_icons.draw('check' if in_vk else 'plus'))
        self._vk_btn.setToolTip('Уже в музыке VK' if in_vk else 'Добавить в музыку VK')
        self._seek.setEnabled(has)
        self._seek.setRange(0, track.duration * 1000 if has else 0)
        self._total.setText(format_time(track.duration * 1000) if has else '0:00')
        self._elapsed.setText('0:00')
        self._set_cover(track.cover if has else '')

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
        if url == self._cover_url:     # пока картинка ехала, трек мог смениться
            self._cover.setPixmap(covers.rounded(pixmap, COVER_SIZE))

    def _on_state(self, state: str) -> None:
        playing = state == STATE_PLAYING
        self._play_btn.setIcon(player_icons.draw('pause' if playing else 'play'))
        self._play_btn.setToolTip('Пауза' if playing else 'Играть')
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

    def _on_shuffle(self, enabled: bool) -> None:
        self._shuffle_btn.setIcon(player_icons.toggled('shuffle', enabled))

    def _on_repeat(self, mode: str) -> None:
        name = 'repeat_one' if mode == REPEAT_ONE else 'repeat'
        self._repeat_btn.setIcon(player_icons.toggled(name, mode != REPEAT_OFF))

    def _on_volume(self, value: int) -> None:
        # Ползунок сам шлёт set_volume, поэтому эхо от плеера гасим блокировкой
        self._volume.blockSignals(True)
        self._volume.setValue(int(value))
        self._volume.blockSignals(False)
        self._update_mute_btn(int(value))

    def _update_mute_btn(self, value: int) -> None:
        muted = value <= 0
        self._mute_btn.setIcon(player_icons.draw('mute' if muted else 'volume'))
        self._mute_btn.setToolTip('Включить звук' if muted else 'Выключить звук')

    # ---------- внешнее состояние ----------
    def set_favorite(self, is_favorite: bool) -> None:
        self._fav_btn.setIcon(player_icons.toggled('heart', is_favorite))
        self._fav_btn.setToolTip('Убрать из избранного' if is_favorite else 'В избранное')

    def set_vk_state(self, label: str) -> None:
        """Подписи у кнопки нет, поэтому о ходе переноса говорит подсказка."""
        self._vk_btn.setToolTip(label or 'Добавить в музыку VK')
        self._vk_btn.setEnabled(not label)
        if label.startswith('✓'):
            self._vk_btn.setIcon(player_icons.draw('check'))

    # ---------- действия ----------
    def _on_volume_moved(self, value: int) -> None:
        self._player.set_volume(value)
        self._update_mute_btn(int(value))

    def _toggle_mute(self) -> None:
        """Динамик глушит и возвращает звук. Прежнюю громкость помним сами:
        плеер знает только текущее значение."""
        if self._volume.value() > 0:
            self._muted_at = self._volume.value()
            self._volume.setValue(0)
        else:
            self._volume.setValue(getattr(self, '_muted_at', 0) or 50)

    def _on_dislike(self) -> None:
        """Трек уходит из подборок, а плеер - к следующему: слушать дальше то,
        что только что отметили лишним, незачем."""
        if self._track is not None:
            self.hide_requested.emit([self._track])

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._player.seek(self._seek.value())

    def _on_open_window(self) -> None:
        self.hide()
        self.open_window_requested.emit()

    def _on_quit(self) -> None:
        self.hide()
        self.quit_requested.emit()
