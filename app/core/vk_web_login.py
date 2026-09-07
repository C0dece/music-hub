import http.cookiejar
import logging
import re
import shutil

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .. import config
from . import proxy
from .async_task import run_async
from .vk_client import check_web_session
from .vk_oauth_login import AUTH_URL, _extract_token, _verify_and_build_token

logger = logging.getLogger(__name__)

# Именно мобильный сайт: десктопный vk.ru роняет процесс отрисовки QtWebEngine
# (STATUS_BREAKPOINT ещё до конца загрузки, GPU тут ни при чём — проверено), а m.vk.ru
# грузится нормально. Заодно это ровно тот домен, с которого vk_api читает музыку.
VK_SITE_URL = 'https://m.vk.ru/'

# Мобильный Chrome: с ним VK не пытается увести нас на десктопную версию, и в
# User-Agent нет пометки QtWebEngine, из-за которой вход выглядит нетипично.
USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36'
)

_SESSION_COOKIE_PREFIXES = ('remixsid', 'remixnsid')
_VK_DOMAINS = ('vk.com', 'vk.ru', 'vk.me')
# Домен, с которого приложение берёт музыку. Сессия нужна именно здесь: вход через
# VK ID кладёт remixsid на `.vk.com`, а запросы идут на `m.vk.ru`, и такая кука до
# них не доезжает. Раньше окно этого не различало и выпускало с половинчатым входом
_SITE_DOMAIN = 'vk.ru'

# Сколько раз перепроверять сессию сайта после выдачи прав и сколько ждать между
# проверками. Сразу после OAuth VK отвечает страницей входа даже тогда, когда сессия
# есть: замер по журналу — проверка через 0.4 с после сохранения кук вернула отказ, а
# та же самая проверка через 14 с отдала 416 КБ списка треков. Одна проверка без
# повтора выпускала ложное «сессии сайта нет» поверх открытой ленты VK и гнала человека
# на второй заход в Kate Mobile. Три попытки с паузой в 3 секунды закрывают эту дыру,
# не заставляя ждать тех, у кого всё сложилось сразу.
_SESSION_RETRIES = 3
_SESSION_RETRY_MS = 3000


def _claim_profile() -> None:
    """Занять профиль встроенного браузера на время входа.

    Профиль `ytd-vk` на диске один, и фоновый перезаход работает на нём же: два
    движка сразу дерутся за файл кук. Импорт здесь, а не наверху, потому что
    keeper импортирует этот модуль — наверху вышел бы круг."""
    from .vk_session_keeper import VkSessionKeeper
    VkSessionKeeper.lock_profile()


def _release_profile() -> None:
    from .vk_session_keeper import VkSessionKeeper
    VkSessionKeeper.unlock_profile()


def clear_saved_login() -> None:
    """Полный выход: и куки для запросов, и профиль встроенного браузера."""
    config.VK_COOKIES_FILE.unlink(missing_ok=True)
    shutil.rmtree(config.WEB_PROFILE_DIR, ignore_errors=True)


def _is_session_cookie(name: str) -> bool:
    return any(name.startswith(p) for p in _SESSION_COOKIE_PREFIXES)


def _is_site_domain(domain: str) -> bool:
    """Тот ли это домен, с которого приложение берёт музыку."""
    host = (domain or '').lstrip('.')
    return host == _SITE_DOMAIN or host.endswith('.' + _SITE_DOMAIN)


def _is_vk_url(url: str) -> bool:
    host = QUrl(url).host().lower()
    return any(host == d or host.endswith('.' + d) for d in _VK_DOMAINS)


def _token_from_redirect(url: str) -> dict | None:
    """Сверять адрес целиком с REDIRECT_URI нельзя: мы работаем на m.vk.ru, и VK уводит
    на oauth.vk.**ru**/blank.html, а константа указывала на .com — вход из-за этого
    останавливался на пустой странице с уже готовым токеном в адресе.
    Проверяем мягче: любой домен VK плюс сам `access_token=`. Нестрогая проверка нужна,
    потому что _extract_token любую длинную строку без пробелов принимает за токен."""
    if 'access_token=' not in url or not _is_vk_url(url):
        return None
    return _extract_token(url)


def merge_cookies_to_file(cookies) -> int:
    """Слить свежие куки с уже сохранёнными и записать файл.

    Файл раньше писался целиком тем набором, что собрал движок за один заход, — и это
    молча ломало вход. Замер: keeper уходит по первой же `remixsid` (`HeadlessVkPage`
    заканчивает работу сразу, как она пришла), в руках у него оказывается 4 куки вместо
    25, и запись затирает весь сопутствующий набор — `remixnsid`, `httoken`, `remixstid`,
    `remixuas`. Сессия после этого мертва: следующий же `load_section` получает страницу
    входа, хотя `remixsid` на месте. Поэтому пишем **слиянием**: свежая кука вытесняет
    одноимённую старую, остальные остаются лежать.

    Ключ — (домен, путь, имя): именно так куки различает и сам браузер, поэтому
    `remixsid` для `.vk.ru` и для `.vk.com` не затирают друг друга."""
    jar = http.cookiejar.MozillaCookieJar(str(config.VK_COOKIES_FILE))
    if config.VK_COOKIES_FILE.exists():
        try:
            jar.load(str(config.VK_COOKIES_FILE), ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            logger.debug('merge_cookies_to_file: прежний файл кук не прочитался, пишу заново')
    for qc in cookies:
        jar.set_cookie(_to_py_cookie(qc) if isinstance(qc, QNetworkCookie) else qc)
    jar.save(ignore_discard=True, ignore_expires=True)
    return len(jar)


def mask_token(url: str) -> str:
    """Токен — это доступ к аккаунту, в интерфейсе и логах он появляться не должен."""
    return re.sub(r'(access_token=)[^&\s]+', r'\1…', url)


class _AuthPage(QWebEnginePage):
    """Редирект ловим ещё до загрузки страницы: blank.html у VK нередко отдаёт ошибку
    сети, и тогда ни loadFinished, ни urlChanged до нас не доходят — а токен уже в адресе."""

    navigated = Signal(QUrl)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if is_main_frame:
            self.navigated.emit(url)
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


def _to_py_cookie(qc: QNetworkCookie) -> http.cookiejar.Cookie:
    domain = qc.domain()
    expires = None
    if not qc.isSessionCookie() and qc.expirationDate().isValid():
        expires = int(qc.expirationDate().toSecsSinceEpoch())
    return http.cookiejar.Cookie(
        version=0,
        name=bytes(qc.name()).decode('utf-8', 'replace'),
        value=bytes(qc.value()).decode('utf-8', 'replace'),
        port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith('.'),
        domain_initial_dot=domain.startswith('.'),
        path=qc.path(), path_specified=True,
        secure=qc.isSecure(),
        expires=expires,
        discard=qc.isSessionCookie(),
        comment=None, comment_url=None,
        rest={'HttpOnly': ''} if qc.isHttpOnly() else {},
    )


class VkWebLoginDialog(QDialog):
    """Вход в VK прямо внутри приложения. Заменяет возню с куками внешнего браузера:
    Chromium-браузеры держат свой файл кук эксклюзивно заблокированным, пока запущены,
    а Edge/новые Chrome вдобавок шифруют его App-Bound Encryption, которую DPAPI не берёт.
    Здесь браузер наш собственный, поэтому куки доступны напрямую.

    Порядок шагов важен: сперва обычный вход на сайт (он даёт куки веб-сессии, без них
    VK не отдаёт список музыки), и только потом — выдача прав приложению ради токена.
    Если рабочий токен уже сохранён, второй шаг пропускается совсем."""

    logged_in = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Вход в VK')
        self.resize(560, 720)
        # Пока это окно открыто, фоновый перезаход к профилю не подходит
        _claim_profile()

        self._cookies: dict[tuple[str, str], QNetworkCookie] = {}
        self._token_data: dict | None = config.load_vk_token()
        self._session_handled = False
        self._token_pending = False
        self._session_tries = 0
        # Ждём ли сейчас страницу выдачи прав Kate Mobile
        self._awaiting_oauth = False
        # Подтверждённый токен. Он живёт дольше отката состояния: когда проверка сессии
        # не удалась, окно возвращает человека на сайт, и VK по дороге снова показывает
        # редирект OAuth со старым access_token. Раньше окно ловило его как новый, гнало
        # на второй заход в Kate Mobile и в итоге отдавало наружу два `logged_in` подряд —
        # запускалось два VkClient, дравшихся за один файл кук
        self._verified_token: dict | None = None

        layout = QVBoxLayout(self)
        self._status = QLabel('Войдите в свой аккаунт VK, как в обычном браузере.')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._profile = QWebEngineProfile('ytd-vk', self)
        self._profile.setPersistentStoragePath(str(config.WEB_PROFILE_DIR))
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self._profile.setHttpUserAgent(USER_AGENT)
        store = self._profile.cookieStore()
        store.cookieAdded.connect(self._on_cookie_added)
        # Куки прошлого входа лежат в профиле на диске и сами по себе сигнал не шлют
        store.loadAllCookies()

        self._view = QWebEngineView(self)
        page = _AuthPage(self._profile, self._view)
        page.navigated.connect(self._on_url_changed)
        page.renderProcessTerminated.connect(self._on_render_crashed)
        page.proxyAuthenticationRequired.connect(self._on_proxy_auth)
        self._view.setPage(page)
        self._view.urlChanged.connect(self._on_url_changed)
        self._view.loadFinished.connect(self._on_load_finished)
        layout.addWidget(self._view, 1)

        # Адрес показываем всегда: без него не видно, на каком шаге застрял вход,
        # а токен в нём прячем — это ключ от аккаунта.
        self._address = QLineEdit()
        self._address.setReadOnly(True)
        self._address.setObjectName('hint')
        layout.addWidget(self._address)

        row = QHBoxLayout()
        reset_btn = QPushButton('Сбросить вход')
        reset_btn.setToolTip('Очистить сохранённые данные встроенного браузера и начать заново')
        reset_btn.clicked.connect(self._reset)
        row.addWidget(reset_btn)
        row.addStretch(1)
        # Если по кукам определить вход не удалось, пользователь может продолжить сам
        self._continue_btn = QPushButton('Продолжить')
        self._continue_btn.setToolTip('Нажмите, если вы уже вошли, а приложение этого не заметило')
        self._continue_btn.setEnabled(False)
        self._continue_btn.clicked.connect(self._on_session_ready)
        row.addWidget(self._continue_btn)
        # Страховка: если VK опять сменит домен редиректа, токен всё равно виден в адресе
        self._take_token_btn = QPushButton('Взять токен из адреса')
        self._take_token_btn.setToolTip('Нажмите, если VK уже перебросил на пустую страницу')
        self._take_token_btn.setVisible(False)
        self._take_token_btn.clicked.connect(self._take_token_from_address)
        row.addWidget(self._take_token_btn)
        cancel = QPushButton('Отмена')
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        layout.addLayout(row)

        self._view.setUrl(QUrl(VK_SITE_URL))

    # ---------- сбор кук ----------
    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        domain = cookie.domain().lstrip('.')
        if not any(domain == d or domain.endswith('.' + d) for d in _VK_DOMAINS):
            return
        name = bytes(cookie.name()).decode('utf-8', 'replace')
        self._cookies[(cookie.domain(), name)] = QNetworkCookie(cookie)
        if _is_session_cookie(name):
            self._on_session_ready()

    def _has_session_cookie(self) -> bool:
        """Есть ли сессия, годная для запросов за музыкой.

        Мало того, чтобы remixsid просто нашлась: она должна стоять на домене, куда
        мы потом постучимся. Вход «только через VK ID» оставляет её на `.vk.com`, и
        для `m.vk.ru` это всё равно что пустое место — отсюда и брались «токен есть,
        а музыки нет» и бесконечный повторный вход, который ничего не менял."""
        for domain, name in self._cookies:
            if not _is_session_cookie(name):
                continue
            if _is_site_domain(domain):
                return True
        return False

    @staticmethod
    def _saved_file_has_session() -> bool:
        if not config.VK_COOKIES_FILE.exists():
            return False
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(str(config.VK_COOKIES_FILE), ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            return False
        return any(_is_session_cookie(c.name) and _is_site_domain(c.domain) for c in jar)

    def _save_cookies(self) -> int:
        # Файл перезаписывается целиком, поэтому незавершённый вход затирал рабочую сессию
        # прошлого входа: музыка переставала грузиться после каждой неудачной попытки.
        if not self._has_session_cookie() and self._saved_file_has_session():
            logger.debug('VkWebLoginDialog: сохранённые куки лучше текущих, оставляю как есть')
            return 0
        total = merge_cookies_to_file(self._cookies.values())
        logger.debug('VkWebLoginDialog: сохранил %d куки (в файле %d) в %s',
                     len(self._cookies), total, config.VK_COOKIES_FILE)
        return total

    # ---------- шаги входа ----------
    def _on_load_finished(self, ok: bool) -> None:
        # Даже неудачная загрузка blank.html оставляет токен в адресе — проверяем всегда
        self._on_url_changed(self._view.url())
        if not ok:
            # Страница прав не открылась, а токен из адреса не пришёл (иначе `_token_pending`
            # уже поднят выше). Молчать тут нельзя: на экране висит обещание «сейчас
            # откроется страница Kate Mobile», и человеку остаётся только закрыть окно —
            # ровно то «всё стопорится», о котором и шла речь
            if self._awaiting_oauth and not self._token_pending:
                self._awaiting_oauth = False
                self._status.setText(
                    'Страница выдачи прав Kate Mobile не открылась. Проверьте связь и нажмите '
                    '«Продолжить», чтобы попробовать ещё раз.')
                self._continue_btn.setEnabled(True)
                # Шаг сессии пройден, повторное нажатие должно вести снова за токеном
                self._session_handled = False
            return
        if self._awaiting_oauth and 'oauth' in self._view.url().host().lower():
            self._awaiting_oauth = False
        self._continue_btn.setEnabled(True)
        # Подстраховка от гонки: куки прошлого входа могли прийти до подключения сигнала
        if self._has_session_cookie():
            self._on_session_ready()

    def _on_proxy_auth(self, _url, authenticator, proxy_host: str) -> None:
        """Chromium логин с паролем прямо в адресе прокси не принимает и спрашивает их
        отдельным окном — подставляем сохранённые, чтобы вход в VK не упирался в него."""
        user, password = proxy.credentials()
        if not user:
            logger.warning('Прокси %s требует логин, а он не задан в настройках', proxy_host)
            return
        authenticator.setUser(user)
        authenticator.setPassword(password)

    def _on_render_crashed(self, status, exit_code: int) -> None:
        """Иначе страница просто белеет и непонятно, что случилось."""
        logger.warning('VkWebLoginDialog: процесс отрисовки упал (%s, код %s)', status, exit_code)
        self._status.setText('Страница VK не открылась. Нажмите «Сбросить вход» и попробуйте снова.')

    def _on_session_ready(self) -> None:
        """Веб-сессия есть. Дальше нужен токен — либо уже сохранённый, либо новый."""
        if self._session_handled:
            return
        if not self._has_session_cookie():
            # Кнопку «Продолжить» жмут и на середине входа. Раньше это молча уводило на
            # выдачу прав: токен получался, а сессии сайта не было — «вход выполнен»,
            # но список музыки пустой.
            self._status.setText(
                'Вход ещё не завершён: VK не выдал сессию сайта. Откройте свою страницу VK '
                'в этом окне (лента, музыка), а потом нажмите «Продолжить».')
            self._view.setUrl(QUrl(VK_SITE_URL))
            return
        self._session_handled = True
        self._save_cookies()
        if self._verified_token:
            # Токен уже подтверждён на этом заходе, не хватало только сессии сайта —
            # человек её открыл и нажал «Продолжить», остаётся перепроверить
            self._status.setText('Проверяю доступ к музыке…')
            self._session_tries = 0
            self._check_session(self._verified_token)
            return
        # Токен перечитываем с диска, а не берём снимок, сделанный в конструкторе.
        # VK отвечает кодом 5 и на протухший токен, и тогда `main_window` стирает файл
        # (`clear_vk_token`) — но окно входа к этому моменту уже держало прежнее значение
        # в `_token_data`. Дальше оно уходило проверять заведомо мёртвый токен, `users.get`
        # снова отвечал «invalid access_token», и вход замирал на «проверяю сохранённый
        # доступ…»: до страницы прав Kate Mobile дело так и не доходило
        saved = config.load_vk_token()
        if saved and saved.get('access_token'):
            self._token_data = saved
            self._status.setText('Вход выполнен, проверяю сохранённый доступ…')
            run_async(_verify_and_build_token, self._on_saved_token_verified, saved)
            return
        self._token_data = None
        self._request_new_token()

    def _request_new_token(self) -> None:
        # Пользователи принимают этот шаг за повторный вход и пугаются — объясняем прямо здесь
        self._status.setText(
            'Вы вошли. Остался один шаг: VK отдаёт музыку только приложениям со своим ключом, '
            'поэтому сейчас откроется страница выдачи прав Kate Mobile, нажмите «Разрешить». '
            'Это не второй вход, пароль там не спрашивают.')
        # Помечаем, что ждём именно страницу прав: если она не откроется, человеку надо
        # сказать об этом, а не оставлять его перед обещанием, которое не сбылось
        self._awaiting_oauth = True
        self._view.setUrl(QUrl(AUTH_URL))

    def _on_url_changed(self, url: QUrl) -> None:
        raw = url.toString()
        self._address.setText(mask_token(raw))
        if self._session_handled and not self._token_pending:
            # Пока идёт выдача прав, держим запасной путь под рукой
            self._take_token_btn.setVisible('oauth' in url.host().lower())
        token_data = _token_from_redirect(raw)
        if not token_data or self._token_pending:
            return
        logger.debug('VkWebLoginDialog: поймал access_token из редиректа OAuth')
        self._token_pending = True
        self._status.setText('Доступ получен, проверяю токен…')
        run_async(_verify_and_build_token, self._on_new_token_verified, token_data)

    def _take_token_from_address(self) -> None:
        token_data = _token_from_redirect(self._view.url().toString())
        if not token_data:
            self._status.setText(
                'В адресе пока нет токена. Дойдите до шага «Разрешить» и дождитесь пустой страницы.')
            return
        self._token_pending = True
        self._status.setText('Доступ получен, проверяю токен…')
        run_async(_verify_and_build_token, self._on_new_token_verified, token_data)

    # ---------- завершение ----------
    def _on_saved_token_verified(self, token_data: dict | None, error: Exception | None) -> None:
        if error:
            logger.debug('VkWebLoginDialog: сохранённый токен не подошёл (%s), запрашиваю новый', error)
            self._token_data = None
            # Стираем и файл: иначе мёртвый токен переживает окно входа и при следующем
            # запуске снова уводит на ту же проверку вместо страницы прав
            config.clear_vk_token()
            self._request_new_token()
            return
        self._succeed(token_data)

    def _on_new_token_verified(self, token_data: dict | None, error: Exception | None) -> None:
        if error:
            self._token_pending = False
            self._status.setText(f'Не удалось подтвердить доступ: {error}')
            return
        self._verified_token = token_data
        self._succeed(token_data)

    def _succeed(self, token_data: dict) -> None:
        self._save_cookies()
        self._session_tries = 0
        self._status.setText('Токен получен, проверяю доступ к музыке…')
        self._check_session(token_data)

    def _check_session(self, token_data: dict) -> None:
        self._session_tries += 1
        run_async(check_web_session,
                  lambda ok, err: self._on_session_checked(token_data, ok, err),
                  token_data.get('user_id'))

    def _on_session_checked(self, token_data: dict, ok: bool | None, error: Exception | None) -> None:
        if error or not ok:
            logger.debug('VkWebLoginDialog: веб-сессия не подтвердилась (ok=%s, error=%r, попытка %d)',
                         ok, error, self._session_tries)
            if self._session_tries < _SESSION_RETRIES:
                # Куки могли ещё не доехать из движка в файл — перечитываем свежие и
                # пробуем снова, вместо того чтобы объявлять отказ по первому ответу
                self._save_cookies()
                self._status.setText('Токен получен, проверяю доступ к музыке…')
                QTimer.singleShot(_SESSION_RETRY_MS, lambda: self._check_session(token_data))
                return
            # Честнее задержать пользователя здесь, чем выпустить с нерабочим входом
            # `_token_pending` намеренно остаётся поднятым: токен у нас уже есть и
            # подтверждён, второй раз выдавать права незачем
            self._session_handled = False
            self._status.setText(
                'Токен получен, но VK не отдаёт музыку: сессии сайта нет. Так бывает, если вход '
                'прошёл только через VK ID. Откройте свою страницу VK в этом окне и нажмите '
                '«Продолжить».')
            self._view.setUrl(QUrl(VK_SITE_URL))
            return
        count = self._save_cookies()
        self._status.setText(f'Готово, сохранено {count} куки.')
        self.logged_in.emit(token_data)
        self.accept()

    def done(self, result: int) -> None:
        """Профиль освобождается на любом выходе из окна — и по «Отмена» тоже.

        Иначе фоновый перезаход считал бы окно входа вечно открытым и молчал бы
        до перезапуска программы."""
        _release_profile()
        super().done(result)

    def _reset(self) -> None:
        self._profile.cookieStore().deleteAllCookies()
        self._profile.clearHttpCache()
        self._cookies.clear()
        self._token_data = None
        self._session_handled = False
        self._token_pending = False
        self._session_tries = 0
        self._awaiting_oauth = False
        self._verified_token = None
        self._continue_btn.setEnabled(False)
        self._take_token_btn.setVisible(False)
        self._status.setText('Сохранённые данные очищены. Войдите в свой аккаунт VK заново.')
        self._view.setUrl(QUrl(VK_SITE_URL))
