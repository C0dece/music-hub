"""Значок у часов.

Окно можно спрятать и управлять музыкой отсюда: что играет, пауза, соседние треки,
сердечко и «+ VK». Значок ничего не решает сам, все действия уходят сигналами,
как и у остальных частей интерфейса.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.player_controller import REPEAT_ALL, REPEAT_OFF, REPEAT_ONE, STATE_PLAYING
from .. import config
from .icon import app_icon
from .tray_flyout import TrayFlyout

logger = logging.getLogger(__name__)


class TrayIcon(QObject):
    """Обёртка над QSystemTrayIcon с меню текущего трека."""

    toggle_window = Signal()
    play_pause = Signal()
    next_track = Signal()
    prev_track = Signal()
    favorite = Signal()
    dislike = Signal(object)
    add_to_vk = Signal()
    shuffle = Signal()
    repeat = Signal()
    mini_player = Signal()
    quit_requested = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self._player = player
        self._notifications = True
        self._icon = QSystemTrayIcon(app_icon(), self)
        self._icon.setToolTip(config.APP_NAME)

        menu = QMenu()
        self._title_action = menu.addAction('Ничего не играет')
        self._title_action.setEnabled(False)
        menu.addSeparator()
        self._play_action = menu.addAction('Играть')
        self._play_action.triggered.connect(self.play_pause)
        menu.addAction('Следующий').triggered.connect(self.next_track)
        menu.addAction('Предыдущий').triggered.connect(self.prev_track)
        self._shuffle_action = menu.addAction('Перемешивание')
        self._shuffle_action.setCheckable(True)
        self._shuffle_action.triggered.connect(self.shuffle)
        self._repeat_action = menu.addAction('Повтор: выключен')
        self._repeat_action.triggered.connect(self.repeat)
        menu.addSeparator()
        self._fav_action = menu.addAction('В избранное')
        self._fav_action.triggered.connect(self.favorite)
        self._dislike_action = menu.addAction('Не нравится')
        self._dislike_action.triggered.connect(self._on_dislike)
        self._vk_action = menu.addAction('Добавить в VK')
        self._vk_action.triggered.connect(self.add_to_vk)
        menu.addSeparator()
        menu.addAction('Показать окно').triggered.connect(self.toggle_window)
        menu.addAction('Мини-плеер').triggered.connect(self.mini_player)
        menu.addAction('Выход').triggered.connect(self.quit_requested)
        self._menu = menu  # QMenu без родителя нужно держать самим, иначе исчезнет
        self._icon.setContextMenu(menu)

        # Панель с обложкой — основной способ управления; меню правой кнопкой
        # остаётся запасным путём для тех, кому привычнее список пунктов
        self._flyout = TrayFlyout(player)
        self._flyout.open_window_requested.connect(self.toggle_window)
        self._flyout.quit_requested.connect(self.quit_requested)
        self._flyout.favorite_toggled.connect(self.favorite)
        self._flyout.add_to_vk_requested.connect(self.add_to_vk)
        self._flyout.hide_requested.connect(self.dislike)

        self._icon.activated.connect(self._on_activated)
        player.track_changed.connect(self._on_track)
        player.state_changed.connect(self._on_state)
        player.shuffle_changed.connect(self._on_shuffle)
        player.repeat_changed.connect(self._on_repeat)
        self._on_shuffle(player.shuffle)
        self._on_repeat(player.repeat)

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        if self.available:
            self._icon.show()

    def hide(self) -> None:
        self._flyout.hide()
        self._icon.hide()

    def set_notifications(self, enabled: bool) -> None:
        self._notifications = bool(enabled)

    def notify(self, text: str, title: str = '') -> None:
        """Короткое сообщение у часов. Молчит, если пользователь этого не хочет."""
        if self._notifications and self._icon.isVisible():
            self._icon.showMessage(title or config.APP_NAME, text,
                                   QSystemTrayIcon.Information, 4000)

    def set_favorite(self, is_favorite: bool) -> None:
        self._fav_action.setText('Убрать из избранного' if is_favorite else 'В избранное')
        self._flyout.set_favorite(is_favorite)

    def set_vk_state(self, label: str) -> None:
        self._vk_action.setText(label or 'Добавить в VK')
        self._vk_action.setEnabled(not label.startswith('✓'))
        self._flyout.set_vk_state(label)

    # ---------- реакции ----------
    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self.toggle_window.emit()
        elif reason == QSystemTrayIcon.Trigger:
            # Повторный щелчок по значку прячет панель, как это делает система
            if self._flyout.isVisible():
                self._flyout.hide()
            else:
                self._flyout.popup_near_cursor()

    def _on_dislike(self) -> None:
        """Пункт меню знает только сам плеер: трек сюда никто не передаёт."""
        track = self._player.current
        if track is not None:
            self.dislike.emit([track])

    def _on_track(self, track) -> None:
        name = track.display_title if track is not None else 'Ничего не играет'
        self._title_action.setText(name[:70])
        self._dislike_action.setEnabled(track is not None)
        # Подсказка у значка обрезается системой, поэтому длинное имя ей не отдаём
        self._icon.setToolTip(f'{config.APP_NAME}: {name}'[:120])

    def _on_state(self, state: str) -> None:
        self._play_action.setText('Пауза' if state == STATE_PLAYING else 'Играть')

    def _on_shuffle(self, enabled: bool) -> None:
        self._shuffle_action.setChecked(bool(enabled))

    def _on_repeat(self, mode: str) -> None:
        # Меню у часов должно оставаться коротким, поэтому режим повтора —
        # один пункт с подписью, а не три отдельные строки
        self._repeat_action.setText({REPEAT_OFF: 'Повтор: выключен',
                                     REPEAT_ALL: 'Повтор: очередь',
                                     REPEAT_ONE: 'Повтор: трек'}.get(mode, 'Повтор'))
