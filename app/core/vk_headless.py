"""Невидимая страница VK: собрать куки и уйти.

Отдельный модуль ровно потому, что здесь живёт QtWebEngine. Keeper с ним не связан
напрямую — он получает любой объект с методом `close()`, — и оттого проверяется
тестами без движка и без сети.

Профиль тот же самый, что у окна входа (`ytd-vk`, `config.WEB_PROFILE_DIR`): в этом
весь смысл затеи. Живая сессия в профиле уже есть, её надо лишь переложить в файл кук,
которым пользуются обычные запросы, — пароль для этого не нужен.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from .. import config
from . import proxy
from .vk_web_login import (
    USER_AGENT, _VK_DOMAINS, _is_session_cookie, _is_site_domain,
)

logger = logging.getLogger(__name__)

# Сколько ждать сессионную куку после того, как страница догрузилась.
# Куки профиля приходят из `loadAllCookies` асинхронно, отдельным потоком движка, и
# на быстрой машине `loadFinished` опережал их: keeper тратил попытку за полторы
# секунды и объявлял «VK не выдал сессию сайта», хотя кука лежала в профиле и
# приезжала мигом позже. Ждём её здесь, а общий таймаут keeper'а длиннее — он
# останется страховкой на случай, если куки нет вовсе.
SETTLE_MS = 4000

# Сколько ещё ждать после прихода самой сессионной куки. Раньше страница уходила ровно
# на ней — и уносила 4 куки вместо 25: `remixnsid`, `httoken`, `remixstid`, `remixuas`
# приезжают следом, а без них VK на первом же `load_section` показывает страницу входа.
# Полторы секунды хватает, чтобы хвост профиля доехал, и это заметно меньше SETTLE_MS.
TAIL_MS = 1500


class HeadlessVkPage(QObject):
    """Грузит страницу VK без окна и отдаёт собранные куки одним словарём.

    Окна нет намеренно: `QWebEnginePage` без `QWebEngineView` прекрасно грузит страницу
    и выдаёт куки, а показывать человеку нечего — мы обещали тишину."""

    def __init__(self, url: str, on_cookies, on_error, parent=None):
        super().__init__(parent)
        self._on_cookies = on_cookies
        self._on_error = on_error
        self._cookies: dict[tuple[str, str], QNetworkCookie] = {}
        self._done = False

        self._profile = QWebEngineProfile('ytd-vk', self)
        self._profile.setPersistentStoragePath(str(config.WEB_PROFILE_DIR))
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.ForcePersistentCookies)
        self._profile.setHttpUserAgent(USER_AGENT)
        store = self._profile.cookieStore()
        store.cookieAdded.connect(self._on_cookie_added)
        # Куки прошлого входа лежат в профиле на диске и сами по себе сигнал не шлют
        store.loadAllCookies()

        # Ожидание кук после загрузки: таймер живёт здесь, а не в keeper'е, потому
        # что только эта страница знает, пришла ли уже сессионная кука
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._on_settled)

        self._page = QWebEnginePage(self._profile, self)
        self._page.loadFinished.connect(self._on_load_finished)
        self._page.proxyAuthenticationRequired.connect(self._on_proxy_auth)
        self._page.renderProcessTerminated.connect(self._on_crash)
        self._page.load(QUrl(url))

    # ---------- сбор ----------
    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        domain = cookie.domain().lstrip('.')
        if not any(domain == d or domain.endswith('.' + d) for d in _VK_DOMAINS):
            return
        name = bytes(cookie.name()).decode('utf-8', 'replace')
        self._cookies[(cookie.domain(), name)] = QNetworkCookie(cookie)
        # Сессионная кука — то, ради чего всё затевалось, но уходить прямо по ней нельзя:
        # рабочая сессия — это весь набор, а не одна кука. Считаем её сигналом «почти всё»
        # и даём хвосту TAIL_MS. Домен важен: remixsid для `.vk.com` уезжает не на тот
        # домен, с которого мы берём музыку, — нужная ставится следом, уже на m.vk.ru
        if _is_session_cookie(name) and _is_site_domain(cookie.domain()):
            self._settle.stop()
            self._settle.start(TAIL_MS)

    def _on_load_finished(self, ok: bool) -> None:
        """Страница догрузилась. Куки могли прийти и раньше — тогда мы уже ушли."""
        if self._done:
            return
        if not ok:
            self._fail('страница VK не загрузилась')
            return
        # Сдаваться прямо здесь нельзя: куки из профиля идут своим чередом и часто
        # приходят уже после загрузки. Даём им SETTLE_MS — придёт сессионная,
        # `_on_cookie_added` закончит раньше нас сам
        self._settle.start(SETTLE_MS)

    def _on_settled(self) -> None:
        """Ожидание закончилось: либо хвост кук после сессионной, либо пустое ожидание.

        Уходим с тем, что собрали: решает keeper — он проверяет сессию делом."""
        self._finish()

    def _on_proxy_auth(self, _url, authenticator, proxy_host: str) -> None:
        """Как и в окне входа: Chromium спрашивает пароль прокси отдельно."""
        user, password = proxy.credentials()
        if not user:
            logger.warning('Прокси %s требует логин, а он не задан в настройках',
                           proxy_host)
            return
        authenticator.setUser(user)
        authenticator.setPassword(password)

    def _on_crash(self, status, exit_code: int) -> None:
        logger.warning('VK keeper: процесс отрисовки упал (%s, код %s)',
                       status, exit_code)
        self._fail('встроенный браузер не справился со страницей VK')

    # ---------- выход ----------
    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._settle.stop()
        self._on_cookies(dict(self._cookies))

    def _fail(self, reason: str) -> None:
        if self._done:
            return
        self._done = True
        self._settle.stop()
        self._on_error(reason)

    def close(self) -> None:
        """Снять страницу с профиля, чтобы он освободился для окна входа."""
        self._done = True
        self._settle.stop()
        try:
            self._profile.cookieStore().cookieAdded.disconnect(self._on_cookie_added)
        except (RuntimeError, TypeError):
            pass                          # уже отключено или объект снесён
        self._page.deleteLater()
