"""Просмотр видео до скачивания - штатным плеером сайта внутри QtWebEngine.

Прямые ссылки на файлы YouTube отдаёт с googlevideo.com, а его режут по имени в
TLS (см. `.agent/worklog.md`): QMediaPlayer ходит мимо настроек прокси приложения
и до такого потока не доберётся. Chromium из QtWebEngine получает прокси теми же
ключами, что и окно входа в VK, поэтому смотрим через встроенный плеер сайта -
заодно не тратя трафик на скачивание того, что может не подойти."""
import http.cookiejar
import logging
import re

from PySide6.QtCore import QDateTime, Qt, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import config
from ..core import browser_cookies, proxy, url_detect
from ..core.async_task import run_async
from .icon import app_icon

logger = logging.getLogger(__name__)

_VK_VIDEO_ID = re.compile(r'video(-?\d+)_(\d+)')
_YT_ID = re.compile(r'(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})')

# Адрес, от имени которого показываем свою страницу с плеером. Домен ненастоящий и
# в сеть за ним никто не ходит: он нужен только как origin - встроенный плеер
# YouTube требует, чтобы у страницы-хозяина он был, а «about:blank» не годится.
_LOCAL_BASE = 'https://ytd.local/'

# Своя страница ровно с одним плеером. Так открывается только видео, без ленты
# рекомендаций, комментариев и остальной страницы YouTube - и заметно быстрее.
# Первой страницей окна /embed/ отдаёт «Error 153», а вложенным кадром на странице
# со своим origin - работает (проверено).
_YT_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
 html,body{margin:0;height:100%;background:#0d1015;overflow:hidden}
 #note{position:absolute;left:0;right:0;top:50%;margin-top:-12px;text-align:center;
       color:#8b93a7;font:14px "Segoe UI",Arial,sans-serif}
 #player,#player iframe{position:absolute;left:0;top:0;width:100%;height:100%;border:0}
</style></head><body>
<div id="note">Загружаю плеер…</div><div id="player"></div>
<script>
var ID = '__ID__';
function fallback() { location.replace('https://www.youtube.com/watch?v=' + ID); }
function onYouTubeIframeAPIReady() {
  window.ytPlayer = new YT.Player('player', {
    videoId: ID,
    playerVars: {autoplay: 1, rel: 0, modestbranding: 1, playsinline: 1},
    events: {
      onReady: function (e) {
        document.getElementById('note').style.display = 'none';
        e.target.playVideo();
        // Проверку «подтвердите, что вы не робот» плеер ошибкой не считает: он просто
        // не начинает играть. Состояния -1 и 5 через восемь секунд - это она (или
        // видео, которое тут не пойдёт), и обычная страница ролика с ней справляется.
        setTimeout(function () {
          var state = e.target.getPlayerState();
          if (state === -1 || state === 5) { fallback(); }
        }, 8000);
      },
      // Запрещённое к встраиванию видео плеер не покажет - тогда уходим на
      // обычную страницу ролика, там оно играет
      onError: fallback
    }
  });
}
var s = document.createElement('script');
s.src = 'https://www.youtube.com/iframe_api';
s.onerror = fallback;
document.head.appendChild(s);
</script></body></html>"""

# Куки берём и у google.com: вход в YouTube хранится на обоих доменах, и без
# «родительских» кук Google сайт считает нас гостем.
_COOKIE_DOMAINS = ('youtube.com', 'google.com')

_profile: QWebEngineProfile | None = None
_cookies: list = []
_cookies_source: str | None = None


def preload_cookies(cookies_browser: str | None) -> None:
    """Забрать куки YouTube из браузера пользователя - заранее и в фоне.

    Без них YouTube через прокси нередко отвечает «Sign in to confirm you're not a
    bot» вместо видео: для него это анонимный посетитель с адреса дата-центра.
    С куками того же браузера, откуда их берёт и yt-dlp, предпросмотр выглядит
    обычным входом. Чтение хранилища браузера занимает секунды, поэтому делается
    не в момент открытия окна."""
    global _cookies_source
    if not cookies_browser or cookies_browser == _cookies_source:
        return
    _cookies_source = cookies_browser
    run_async(_read_cookies, _on_cookies_read, cookies_browser)


def _read_cookies(cookies_browser: str) -> list:
    if cookies_browser == 'file':
        jar = http.cookiejar.MozillaCookieJar(str(config.COOKIES_FILE))
        jar.load(ignore_discard=True, ignore_expires=True)
        return [c for c in jar if any(c.domain.endswith(d) for d in _COOKIE_DOMAINS)]
    return browser_cookies.domain_cookies(cookies_browser, _COOKIE_DOMAINS)


def _on_cookies_read(cookies, error) -> None:
    """Не получилось - предпросмотр всё равно работает, просто как у гостя."""
    global _cookies
    if error or not cookies:
        logger.debug('предпросмотр: куки YouTube недоступны (%s)', error)
        return
    _cookies = cookies
    logger.debug('предпросмотр: куки YouTube готовы, %d шт.', len(cookies))
    if _profile is not None:
        _install_cookies(_profile)


def _to_qt_cookie(cookie) -> QNetworkCookie:
    """http.cookiejar.Cookie → QNetworkCookie. SameSite=None обязателен: плеер живёт
    вложенным кадром на чужом домене, а куки без этой пометки туда не уходят."""
    qt_cookie = QNetworkCookie(cookie.name.encode(), (cookie.value or '').encode())
    # У «__Host-…» домена быть не должно - с ним Chromium такую куку отбрасывает
    if not cookie.name.startswith('__Host-'):
        qt_cookie.setDomain(cookie.domain)
    qt_cookie.setPath(cookie.path or '/')
    qt_cookie.setSecure(True)
    qt_cookie.setSameSitePolicy(QNetworkCookie.SameSite.None_)
    if cookie.expires:
        qt_cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(cookie.expires)))
    return qt_cookie


def _install_cookies(profile: QWebEngineProfile) -> None:
    store = profile.cookieStore()
    for cookie in _cookies:
        store.setCookie(_to_qt_cookie(cookie), QUrl(f'https://{cookie.domain.lstrip(".")}/'))


def _shared_profile() -> QWebEngineProfile:
    """Свой профиль просмотра, отдельный от профиля входа в VK.

    Один и тот же storageName в двух живых профилях Chromium не поддерживает, а
    окно входа может быть открыто одновременно с предпросмотром."""
    global _profile
    if _profile is None:
        _profile = QWebEngineProfile('ytd-preview')
        settings = _profile.settings()
        # Иначе встроенный плеер ждёт клика по кнопке, которой в чужой вёрстке
        # может и не оказаться
        settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        # Пометка QtWebEngine в User-Agent - лишний повод показать нам проверку на
        # робота; версия Chromium в строке остаётся настоящей
        _profile.setHttpUserAgent(re.sub(r'QtWebEngine/\S+ ', '', _profile.httpUserAgent()))
        _install_cookies(_profile)
    return _profile


def youtube_id(entry: dict) -> str:
    """Идентификатор ролика из записи или из её ссылки; пусто - если не нашёлся."""
    video_id = entry.get('id') or ''
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return video_id
    found = _YT_ID.search(entry.get('url') or '')
    return found.group(1) if found else ''


def preview_url(source: str, entry: dict) -> str | None:
    """Ссылка на встроенный плеер записи или None, если её не собрать.

    Для YouTube это обычная страница ролика: она же запасной путь для видео,
    которое запрещено встраивать."""
    url = entry.get('url') or ''
    if source == 'youtube':
        video_id = youtube_id(entry)
        return f'https://www.youtube.com/watch?v={video_id}' if video_id else url or None
    if source == 'vk_video':
        found = _VK_VIDEO_ID.search(url)
        if found:
            return (f'https://vk.com/video_ext.php?oid={found.group(1)}'
                    f'&id={found.group(2)}&hd=2&autoplay=1')
        return url or None
    return url or None


def open_preview(parent, source: str, entry: dict, on_download=None) -> bool:
    """Открыть предпросмотр записи. False - если по ссылке смотреть нечего."""
    url = preview_url(source, entry)
    if not url:
        return False
    html = ''
    if source == 'youtube':
        video_id = youtube_id(entry)
        if video_id:
            html = _YT_PAGE.replace('__ID__', video_id)
    title = entry.get('title') or entry.get('url') or 'Предпросмотр'
    dialog = WebPreviewDialog(title, url, parent, on_download, html)
    dialog.setAttribute(Qt.WA_DeleteOnClose)
    dialog.show()
    return True


class WebPreviewDialog(QDialog):
    """Окно предпросмотра: плеер сайта плюс кнопка «Скачать»."""

    def __init__(self, title: str, url: str, parent=None, on_download=None, html: str = ''):
        super().__init__(parent)
        self.setWindowIcon(app_icon())
        self.setWindowTitle(f'{title}: просмотр до скачивания')
        self.resize(920, 600)
        self.setMinimumSize(480, 320)
        self._on_download = on_download

        box = QVBoxLayout(self)
        box.setContentsMargins(14, 14, 14, 14)
        box.setSpacing(10)

        self._view = QWebEngineView(self)
        self._view.setPage(_make_page(self._view))
        if html:
            self._view.setHtml(html, QUrl(_LOCAL_BASE))
        else:
            self._view.load(QUrl(url))
        box.addWidget(self._view, 1)

        hint = QLabel('Это плеер самого сайта: ничего не скачивается, качество и реклама на их стороне. '
                      'Закрытые видео VK здесь могут не открыться, а скачаться получится.')
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        box.addWidget(hint)

        row = QHBoxLayout()
        row.addStretch(1)
        if on_download is not None:
            download_btn = QPushButton('Скачать')
            download_btn.clicked.connect(self._download)
            row.addWidget(download_btn)
        close_btn = QPushButton('Закрыть')
        close_btn.setObjectName('secondary')
        close_btn.clicked.connect(self.close)
        row.addWidget(close_btn)
        box.addLayout(row)

    def _download(self) -> None:
        # Закрываемся первыми: иначе звук из плеера продолжает идти поверх
        # только что начавшейся загрузки
        callback = self._on_download
        self.close()
        if callback is not None:
            callback()

    def closeEvent(self, event) -> None:
        # Без явной остановки Chromium продолжает играть звук закрытой страницы
        self._view.stop()
        self._view.setUrl(QUrl('about:blank'))
        super().closeEvent(event)


def _make_page(view: QWebEngineView):
    from PySide6.QtWebEngineCore import QWebEnginePage

    class _Page(QWebEnginePage):
        """Ссылки «в новом окне» (логотип YouTube, «Смотреть на сайте») открываем
        в том же плеере, иначе клик по ним не делает ровно ничего."""

        def createWindow(self, _type):
            return self

    page = _Page(_shared_profile(), view)
    page.proxyAuthenticationRequired.connect(_fill_proxy_auth)
    page.renderProcessTerminated.connect(_log_render_crash)
    return page


def _log_render_crash(status, exit_code: int) -> None:
    """Отрисовка страницы падает отдельным процессом, и молча.

    Для человека это выглядит как внезапно исчезнувшее окно: страница мертва, а
    почему - нигде не записано. Строка в журнале отличает такое падение от обычного
    выхода и показывает, что упал именно встроенный браузер, а не программа."""
    logger.warning('Встроенный браузер: процесс отрисовки завершился (%s, код %s)',
                   status, exit_code)


def _fill_proxy_auth(_url, authenticator, proxy_host: str) -> None:
    """Логин с паролем прямо в адресе прокси Chromium не принимает и спрашивает их
    отдельным окном - подставляем сохранённые, как в окне входа в VK."""
    user, password = proxy.credentials()
    if not user:
        logger.warning('Прокси %s требует логин, а он не задан в настройках', proxy_host)
        return
    authenticator.setUser(user)
    authenticator.setPassword(password)


def can_preview(url: str) -> bool:
    """Годится ли ссылка для предпросмотра - YouTube или видео VK."""
    return url_detect.detect(url).source in ('youtube', 'vk_video')
