import logging
import os

import yt_dlp.cookies as _yt_cookies
from yt_dlp.cookies import extract_cookies_from_browser

logger = logging.getLogger(__name__)


def _yandex_browser_dir() -> str:
    return os.path.join(os.path.expandvars('%LOCALAPPDATA%'), 'Yandex', 'YandexBrowser', 'User Data')


def _register_yandex_browser() -> None:
    """yt-dlp не знает про Яндекс.Браузер (его нет в SUPPORTED_BROWSERS), хотя он тоже
    Chromium-based и хранит/шифрует куки точно так же, как Chrome (DPAPI на Windows) -
    поэтому просто регистрируем его в списках yt-dlp и подставляем путь к профилю,
    переиспользуя всю остальную логику поиска/расшифровки как есть."""
    if 'yandex' in _yt_cookies.SUPPORTED_BROWSERS:
        return  # модуль мог быть уже импортирован и пропатчен ранее
    _yt_cookies.SUPPORTED_BROWSERS.add('yandex')
    _yt_cookies.CHROMIUM_BASED_BROWSERS.add('yandex')

    original_settings = _yt_cookies._get_chromium_based_browser_settings

    def patched_settings(browser_name):
        if browser_name != 'yandex':
            return original_settings(browser_name)
        return {'browser_dir': _yandex_browser_dir(), 'keyring_name': 'Chrome', 'supports_profiles': True}

    _yt_cookies._get_chromium_based_browser_settings = patched_settings


_register_yandex_browser()

BROWSER_LABELS = [
    ('Chrome', 'chrome'),
    ('Яндекс Браузер', 'yandex'),
    ('Edge', 'edge'),
    ('Firefox', 'firefox'),
    ('Brave', 'brave'),
    ('Vivaldi', 'vivaldi'),
    ('Opera', 'opera'),
]


def ytdlp_cookie_opts(cookies_browser: str | None, cookies_file) -> dict:
    """cookies_browser: None - без кук; 'file' - cookies.txt рядом с приложением (ручной
    экспорт, как раньше); имя браузера (chrome/yandex/edge/firefox/...) - yt-dlp сам достаёт
    и расшифровывает куки из установленного браузера (--cookies-from-browser)."""
    if not cookies_browser:
        return {}
    if cookies_browser == 'file':
        return {'cookiefile': str(cookies_file)} if os.path.isfile(cookies_file) else {}
    return {'cookiesfrombrowser': (cookies_browser,)}


def domain_cookies(browser: str, domains: tuple[str, ...]) -> list:
    """Куки нужных доменов прямо из cookie-хранилища установленного браузера - тем же
    механизмом извлечения/расшифровки, что использует yt-dlp для --cookies-from-browser, но
    без ограничения на один сайт. Избавляет от ручного экспорта cookies.txt для сервисов,
    которые сам yt-dlp не качает (например VK-музыку через vk_api).

    Чтение всего хранилища не мгновенное - вызывать вне UI-потока."""
    cookies = [c for c in extract_cookies_from_browser(browser) if any(c.domain.endswith(d) for d in domains)]
    logger.debug('domain_cookies(%s): найдено %d куки для %s', browser, len(cookies), domains)
    return cookies


def load_domain_cookies_from_browser(browser: str, session, domains: tuple[str, ...]) -> int:
    """То же самое, но сразу в requests-сессию. Возвращает число перенесённых кук."""
    cookies = domain_cookies(browser, domains)
    for cookie in cookies:
        session.cookies.set_cookie(cookie)
    return len(cookies)
