"""Маленькое окно плеера поверх остальных программ.

Нужно, когда музыка играет фоном: большое окно занимает половину экрана, а от
плеера обычно требуется четыре вещи - увидеть, что играет, поставить на паузу,
перелистнуть и отметить сердечком.

Окно ничего не воспроизводит само: это второй вид на тот же PlayerController, что
и полоса внизу главного окна. Поэтому переключение «главное окно ↔ мини-плеер»
ничего не перезапускает - звук и позиция остаются на месте.

Закрытие мини-плеера не выходит из программы: об этом сообщается сигналом
`closed`, а решение принимает главное окно."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from ..core.player_controller import (REPEAT_OFF, REPEAT_ONE, STATE_LABELS,
                                      STATE_PLAYING, STATE_STOPPED, PlayerController)
from ..core.track import SOURCE_VK, Track
from .. import config
from . import covers, player_icons
from .track_list import format_time
from .widgets import ElidedLabel

COVER_SIZE = 56
MIN_WIDTH = 340


class MiniPlayer(QWidget):
    """Компактный вид на текущий трек: обложка, перемотка и основные кнопки."""

    favorite_toggled = Signal(object)      # Track
    hide_requested = Signal(object)        # список треков
    add_to_vk_requested = Signal(object)   # Track
    restore_requested = Signal()           # «вернуться в главное окно»
    closed = Signal()

    def __init__(self, player: PlayerController, parent=None):
        super().__init__(parent, Qt.Window)
        self.setObjectName('miniPlayer')
        self.setWindowTitle(f'{config.APP_NAME}: мини-плеер')
        self.setMinimumWidth(MIN_WIDTH)

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
        player.notice.connect(self._status.setText)

        self._on_track(player.current)
        self._on_state(player.state)
        self._on_shuffle(player.shuffle)
        self._on_repeat(player.repeat)

    # ---------- сборка ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._cover = QLabel()
        self._cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self._cover.setPixmap(covers.placeholder(COVER_SIZE))
        top.addWidget(self._cover)

        names = QVBoxLayout()
        names.setSpacing(2)
        self._title = ElidedLabel('Ничего не играет')
        self._title.setObjectName('npTitle')
        self._subtitle = ElidedLabel('')
        self._subtitle.setObjectName('hint')
        self._status = ElidedLabel('')
        self._status.setObjectName('hint')
        names.addWidget(self._title)
        names.addWidget(self._subtitle)
        names.addWidget(self._status)
        top.addLayout(names, 1)

        self._pin_btn = self._icon_button('fullscreen', 'Поверх других окон',
                                          self._toggle_on_top)
        self._pin_btn.setCheckable(True)
        top.addWidget(self._pin_btn)
        top.addWidget(self._icon_button('collapse', 'Вернуться в главное окно',
                                        self.restore_requested.emit))
        root.addLayout(top)

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
        root.addLayout(line)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self._shuffle_btn = self._icon_button('shuffle', 'Перемешать очередь',
                                              self._player.toggle_shuffle)
        buttons.addWidget(self._shuffle_btn)
        self._prev_btn = self._icon_button('previous', 'Предыдущий',
                                           self._player.previous)
        buttons.addWidget(self._prev_btn)
        self._play_btn = self._icon_button('play', 'Играть', self._player.toggle,
                                           primary=True)
        buttons.addWidget(self._play_btn)
        self._next_btn = self._icon_button('next', 'Следующий', self._player.next)
        buttons.addWidget(self._next_btn)
        self._repeat_btn = self._icon_button('repeat', 'Повтор',
                                             self._player.cycle_repeat)
        buttons.addWidget(self._repeat_btn)
        buttons.addStretch(1)
        self._fav_btn = self._icon_button('heart', 'В избранное', self._on_favorite)
        buttons.addWidget(self._fav_btn)
        # Пара к сердцу: обе оценки должны быть под рукой везде, где виден плеер
        self._dislike_btn = self._icon_button('dislike', 'Не нравится',
                                              self._on_dislike)
        buttons.addWidget(self._dislike_btn)
        self._vk_btn = QPushButton('+ VK')
        self._vk_btn.setObjectName('secondary')
        self._vk_btn.setCursor(Qt.PointingHandCursor)
        self._vk_btn.setToolTip('Добавить в свою музыку VK')
        self._vk_btn.clicked.connect(self._on_add_vk)
        buttons.addWidget(self._vk_btn)
        root.addLayout(buttons)

    def _icon_button(self, icon: str, tip: str, slot,
                     primary: bool = False) -> QPushButton:
        button = QPushButton()
        if not primary:
            button.setObjectName('secondary')
        button.setIcon(player_icons.draw(icon))
        button.setToolTip(tip)
        button.setFixedWidth(38)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    # ---------- внешнее состояние ----------
    def set_favorite(self, is_favorite: bool) -> None:
        self._fav_btn.setIcon(player_icons.toggled('heart', is_favorite))
        self._fav_btn.setToolTip('Убрать из избранного' if is_favorite else 'В избранное')

    def set_vk_state(self, uid: str, label: str) -> None:
        if self._track is not None and self._track.uid == uid:
            self._vk_btn.setText(label or '+ VK')
            self._vk_btn.setEnabled(not label or label == '+ VK')

    # ---------- сигналы плеера ----------
    def _on_track(self, track) -> None:
        self._track = track
        has = track is not None
        self._title.setText(track.title if has else 'Ничего не играет')
        self._subtitle.setText(
            ' · '.join(part for part in (track.artist, track.source_label) if part)
            if has else 'Выберите трек в главном окне')
        for button in (self._prev_btn, self._play_btn, self._next_btn,
                       self._fav_btn, self._dislike_btn):
            button.setEnabled(has)
        self._seek.setEnabled(has)
        self._seek.setRange(0, track.duration * 1000 if has else 0)
        self._total.setText(format_time(track.duration * 1000) if has else '0:00')
        self._elapsed.setText('0:00')
        self._status.setText('')
        # Метку ставит окно через set_vk_state: чужая запись VK тоже из VK, но
        # её можно добавить к себе, и знает об этом только сервис переноса
        self._vk_btn.setText('✓ В VK' if has and track.in_vk and track.source != SOURCE_VK
                             else '+ VK')
        self._vk_btn.setEnabled(has and not (track.in_vk and track.source != SOURCE_VK))
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
        self._status.setText(STATE_LABELS.get(state, ''))
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

    # ---------- действия ----------
    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._player.seek(self._seek.value())

    def _on_favorite(self) -> None:
        if self._track is not None:
            self.favorite_toggled.emit(self._track)

    def _on_dislike(self) -> None:
        if self._track is not None:
            self.hide_requested.emit([self._track])

    def _on_add_vk(self) -> None:
        if self._track is not None:
            self.add_to_vk_requested.emit(self._track)

    def _toggle_on_top(self, checked: bool) -> None:
        """«Поверх окон» меняет флаг окна, а флаг требует показать окно заново."""
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(checked))
        self.show()

    def closeEvent(self, event) -> None:
        # Крестик мини-плеера - не выход из программы: об этом знает главное окно
        self.closed.emit()
        super().closeEvent(event)
