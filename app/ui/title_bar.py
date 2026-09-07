"""Своя полоса заголовка вместо системной рамки.

Системная рамка Windows выпадала из оформления: светлая полоса над тёмным
окном и надпись, которую нельзя было привести к общему виду. Здесь та же
работа делается сами: значок, название, кнопки свернуть/развернуть/закрыть,
перетаскивание за пустое место и разворот двойным щелчком.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from .. import config
from .icon import render

BUTTON_SIZE = QSize(44, 32)


class TitleBar(QFrame):
    """Полоса заголовка приложения.

    Ничего не решает сама: о желании свернуть, развернуть или закрыть окно
    сообщает сигналами, как и остальные части интерфейса.
    """

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('titleBar')
        self.setFixedHeight(36)

        box = QHBoxLayout(self)
        box.setContentsMargins(10, 0, 0, 0)
        box.setSpacing(8)

        logo = QLabel()
        logo.setPixmap(render(18))
        logo.setFixedSize(18, 18)
        logo.setScaledContents(True)
        box.addWidget(logo)

        name = QLabel(config.APP_NAME)
        name.setObjectName('titleName')
        box.addWidget(name)
        box.addStretch(1)

        self._min_btn = self._button('─', 'Свернуть', self.minimize_requested)
        self._max_btn = self._button('□', 'Развернуть', self.maximize_requested)
        self._close_btn = self._button('✕', 'Закрыть', self.close_requested)
        self._close_btn.setObjectName('titleClose')
        for button in (self._min_btn, self._max_btn, self._close_btn):
            box.addWidget(button)

    def _button(self, text: str, hint: str, signal) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName('titleButton')
        button.setFixedSize(BUTTON_SIZE)
        button.setToolTip(hint)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.ArrowCursor)
        button.clicked.connect(signal.emit)
        return button

    def set_maximized(self, maximized: bool) -> None:
        """Значок средней кнопки показывает, что она сделает дальше."""
        self._max_btn.setText('❐' if maximized else '□')
        self._max_btn.setToolTip('Вернуть размер' if maximized else 'Развернуть')

    # ---- перетаскивание окна ----
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            window = self.window()
            handle = window.windowHandle()
            if handle is not None and not window.isMaximized():
                # Системное перетаскивание: окно едет силами Qt и оконного
                # менеджера, поэтому не отстаёт от курсора и цепляется к краям
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
