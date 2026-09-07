import logging
import re
import time
from urllib.parse import urlencode

import requests
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QVBoxLayout,
)

from .async_task import run_async

logger = logging.getLogger(__name__)

# Kate Mobile — официальное приложение VK; только официальные app_id всё ещё
# получают scope=audio при OAuth-входе (у VK нет публичного API для музыки).
KATE_MOBILE_CLIENT_ID = 2685278
OAUTH_SCOPE = 'audio,offline,video'
REDIRECT_URI = 'https://oauth.vk.com/blank.html'
API_VERSION = '5.131'

AUTH_URL = 'https://oauth.vk.com/authorize?' + urlencode({
    'client_id': KATE_MOBILE_CLIENT_ID,
    'scope': OAUTH_SCOPE,
    'redirect_uri': REDIRECT_URI,
    'display': 'page',
    'response_type': 'token',
    'v': API_VERSION,
})


def _extract_token(pasted: str) -> dict | None:
    pasted = pasted.strip()
    if not pasted:
        return None

    match = re.search(r'access_token=([^&\s]+)', pasted)
    if match:
        data = {'access_token': match.group(1)}
        uid = re.search(r'[#&?]user_id=(\d+)', pasted)
        if uid:
            data['user_id'] = int(uid.group(1))
        return data

    if ' ' not in pasted and len(pasted) > 30:
        return {'access_token': pasted}

    return None


def _fetch_user(token: str) -> dict:
    # VK регулярно рвёт TLS-соединение на ровном месте ([SSL: UNEXPECTED_EOF_WHILE_READING]),
    # и одна такая случайность роняла весь вход. Пара повторов дешевле, чем заново логиниться.
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                'https://api.vk.com/method/users.get',
                params={'access_token': token, 'v': API_VERSION},
                timeout=15,
            )
            payload = resp.json()
            break
        except requests.RequestException as exc:
            last_error = exc
            logger.debug('_fetch_user: попытка %d не удалась (%s)', attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    else:
        raise ValueError(f'VK не отвечает: {last_error}')

    if 'error' in payload:
        raise ValueError(payload['error'].get('error_msg') or 'VK отклонил токен')
    users = payload.get('response') or []
    if not users:
        raise ValueError('VK не вернул данные пользователя для этого токена')
    return users[0]


def _verify_and_build_token(token_data: dict) -> dict:
    user = _fetch_user(token_data['access_token'])
    result = dict(token_data)
    result['user_id'] = result.get('user_id') or user.get('id')
    result['user_name'] = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return result


class VkOAuthLoginDialog(QDialog):
    logged_in = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Вход в VK')
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)

        hint = QLabel(
            '1. Нажмите «Открыть страницу входа VK», она откроется в вашем обычном браузере.\n'
            '2. Войдите в свой аккаунт VK, как обычно.\n'
            '3. VK перебросит на пустую страницу, скопируйте ссылку из адресной строки целиком.\n'
            '4. Вставьте её сюда и нажмите «Подтвердить».'
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        open_btn = QPushButton('Открыть страницу входа VK')
        open_btn.clicked.connect(self._open_browser)
        layout.addWidget(open_btn)

        self._link_edit = QLineEdit()
        self._link_edit.setPlaceholderText('Вставьте сюда ссылку из адресной строки после входа')
        self._link_edit.returnPressed.connect(self._on_confirm_clicked)
        layout.addWidget(self._link_edit)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._error_label = QLabel()
        self._error_label.setStyleSheet('color: #e06060;')
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton('Отмена')
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self._confirm_btn = QPushButton('Подтвердить')
        self._confirm_btn.setDefault(True)
        self._confirm_btn.clicked.connect(self._on_confirm_clicked)
        btn_row.addWidget(self._confirm_btn)
        layout.addLayout(btn_row)

        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        self.adjustSize()
        geo = screen.availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.center().y() - self.height() // 2)

    def _open_browser(self) -> None:
        QDesktopServices.openUrl(QUrl(AUTH_URL))

    def _set_busy(self, busy: bool) -> None:
        self._link_edit.setEnabled(not busy)
        self._confirm_btn.setEnabled(not busy)
        self._progress.setVisible(busy)
        if busy:
            self._error_label.setVisible(False)

    def _show_error(self, text: str) -> None:
        self._error_label.setText(text)
        self._error_label.setVisible(True)

    def _on_confirm_clicked(self) -> None:
        token_data = _extract_token(self._link_edit.text())
        if not token_data:
            self._show_error(
                'Не нашёл access_token в этой ссылке. Убедитесь, что скопировали её целиком '
                'после входа в VK.'
            )
            return

        self._set_busy(True)
        run_async(_verify_and_build_token, self._on_verified, token_data)

    def _on_verified(self, token_data: dict | None, error: Exception | None) -> None:
        # В журнал уходит только факт и имя пользователя: сам токен открывает
        # доступ к аккаунту, ему в логах не место
        logger.debug('VkOAuthLoginDialog._on_verified: error=%r, токен получен: %s',
                     error, bool(token_data))
        self._set_busy(False)
        if error:
            self._show_error(f'Не удалось подтвердить токен: {error}')
            return
        self.logged_in.emit(token_data)
        logger.debug('VkOAuthLoginDialog._on_verified: logged_in.emit выполнен, закрываю диалог')
        self.accept()
