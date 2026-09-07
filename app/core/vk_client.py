import copy as copy_module
import datetime
import gc
import html
import http.cookiejar
import logging
import os
import re
import subprocess
import tempfile
import threading
import time

import requests
import vk_api
from vk_api.audio import VkAudio, scrap_tracks
from vk_api.exceptions import AccessDenied, ApiError
from vk_api.upload import VkUpload

from .. import config
from . import browser_cookies, proxy, ytdlp_engine
from .matcher import has_cyrillic, translit

logger = logging.getLogger(__name__)

_VK_DOMAINS = ('vk.com', 'vk.ru')
# Веб-сессию m.vk.ru держит именно эта кука. Вход через VK ID ставит ещё remixmsts/sui/sua,
# но они сайту не заменяют сессию: с ними список музыки приходит пустым.
_SESSION_COOKIE_PREFIXES = ('remixsid', 'remixnsid')
# Рабочий домен: сюда уходят все запросы за музыкой, и сессия нужна именно здесь
_SITE_DOMAIN = 'vk.ru'

# Отозванную сессию VK ошибкой не называет: вместо данных кладёт в JSON адрес страницы входа
_LOGIN_HOSTS = ('login.vk.ru', 'login.vk.com')
# Куда VK уводит заблокированный аккаунт. Внешне это тот же редирект, что и «войдите
# заново», но лечится он не входом, а разблокировкой на сайте: куки и токен целы, VK
# просто не пускает. Без этой развилки программа молча долбилась в перезаход по кругу
_BLOCKED_MARKERS = ('act=blocked', '/blocked', 'vkui/blocked')

API_VERSION = '5.131'
_INVALID_CHARS = '<>:"/\\|?*'
_HTTP_TIMEOUT = 15

_SECTION_URL = 'https://m.vk.ru/audio'

# Поиск музыки на m.vk.ru ломается на кириллице: запрос доезжает до VK покалеченным
# («Кино» превращается в «?4??4?…»), в ответ приходят случайные треки, а часть названий
# в них — тот же мусор. Тот же запрос латиницей ищет правильно и отдаёт нормальные
# кириллические названия («Kino» → «Кино — Кончится лето»), поэтому кириллицу перед
# отправкой транслитерируем, а строки с остатками мусора выбрасываем.
_GARBLED = re.compile(r'(\?\d\?){3}')
# Разделы рекомендаций m.vk.ru в порядке предпочтения. VK их периодически
# переименовывает и не у всех аккаунтов включает, поэтому идём по списку,
# а не полагаемся на один-единственный.
_RECOM_SECTIONS = ('recoms', 'recoms_audio', 'radio')

# Разделы-списки: те, что отдают не подборки, а сразу треки. Проверено запросами —
# из полутора десятков известных названий этому токену отвечают только эти два,
# остальные молчат. Заголовок фиксируем здесь: VK в ответе своего не присылает
_TRACK_SHELVES = (
    ('recoms', 'Рекомендации VK', 'Собрано по вашим прослушиваниям'),
    ('recent', 'Недавно прослушанное', 'То, что играло у вас последним'),
)

# Сколько плейлистов должно попасть в ось, чтобы полка по ней имела смысл. Полка из
# одного альбома — не подборка, а тот же альбом, только на два клика дальше
_MIN_SHELF = 3
# Плитке раздела нужно лишь знать, что треки там есть, и найти обложку. Весь список
# раздел отдаст потом, когда полку откроют. Не единица: обложка проставлена не у
# каждого трека, и на одном можно наткнуться ровно на тот, у которого её нет
_SHELF_PREVIEW = 10
# Больше десятка полок человек уже не разглядывает, а полоса выбора превращается
# в бесконечную ленту, по которой надо скроллить, чтобы понять, что там вообще есть
_MAX_SHELVES = 12
# Свежим считаем то, что вышло за последние пять лет: у VK год проставлен далеко не
# везде, и более узкая граница оставила бы полку «Новое» почти пустой
_FRESH_YEARS = 5


_TRACKS_PER_PAGE = 2000
_PLAYLISTS_PER_PAGE = 100

# Прямые ссылки VK отдаёт только отдельным запросом и не любит частых обращений
# (vk_api.audio.RPS_DELAY_RELOAD_AUDIO). Качаем в несколько потоков, поэтому запросы
# за ссылками выстраиваем в общую очередь — заодно к сессии обращается один поток за раз.
_RELOAD_DELAY = 1.5
_reload_lock = threading.Lock()
_last_reload = 0.0


# Коды VK, после которых токен точно недействителен: только в этих случаях сохранённый
# вход имеет смысл стирать. Сетевые сбои токен не портят, а прежде мы стирали и их —
# после каждого обрыва связи приходилось входить заново.
_TOKEN_REJECTED_CODES = (5, 27, 28)


class VkAuthError(Exception):
    # Текст готов для показа человеку, и в логе трейсбек по нему не нужен (см. async_task)
    user_facing = True

    def __init__(self, message: str, token_rejected: bool = False):
        super().__init__(message)
        self.token_rejected = token_rejected


class VkSessionExpired(VkAuthError):
    """Сессия сайта VK больше не действует: куки на месте, но VK их отозвал.

    Отдельный тип, потому что лечится это только новым входом, а не повтором запроса:
    токен при этом цел, и сбрасывать его не за что."""


class VkAccountBlocked(VkAuthError):
    """VK заблокировал аккаунт: ни куки, ни токен ни при чём.

    Отдельный тип нужен ровно затем, чтобы никто не пытался это чинить. Внешне
    блокировка неотличима от протухшей сессии — тот же редирект на страницу входа,
    та же ошибка [5], — и программа честно уходила в бесконечный тихий перезаход:
    заходила на m.vk.ru, получала редирект на страницу блокировки, не находила там
    сессионной куки и объявляла «VK не выдал сессию сайта». Раз в 15, 30, 60 секунд
    и так далее, без единого шанса на успех и без внятного слова человеку."""


class VkUploadError(Exception):
    """Файл не попал в музыку VK. Отдельный тип, потому что вход при этом рабочий:
    подсовывать человеку «войдите заново» здесь не за что."""

    user_facing = True


# Ограничения загрузки в «Мою музыку»: VK берёт только mp3 и не больше 200 МБ.
_AUDIO_MAX_BYTES = 200 * 1024 * 1024
_UPLOAD_BITRATE = '320k'
_UPLOAD_TIMEOUT = 600  # заливка идёт куда дольше обычного запроса к API
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_TITLE_SEPARATORS = (' — ', ' - ', ' – ')


def split_artist_title(name: str) -> tuple[str, str]:
    """Разобрать «Исполнитель — Название»: в VK это два отдельных поля."""
    for sep in _TITLE_SEPARATORS:
        if sep in name:
            artist, title = (part.strip() for part in name.split(sep, 1))
            # yt-dlp порой пишет исполнителя дважды («Кто-то - Кто-то - Песня»)
            if artist and title.startswith(artist):
                title = title[len(artist):].lstrip(' -—–')
            return artist, title or name.strip()
    return '', name.strip()


def _to_mp3(path: str, status_cb=None) -> tuple[str, bool]:
    """Путь к mp3 для заливки и признак «файл временный, удалить после».

    VK принимает только mp3, а с YouTube музыка приходит и в m4a, и в opus."""
    if os.path.splitext(path)[1].lower() == '.mp3':
        return path, False
    if not config.FFMPEG_PATH.exists():
        raise VkUploadError('VK принимает только mp3, а ffmpeg для перекодирования не найден')
    if status_cb:
        status_cb('Перекодирую в mp3')
    handle, dest = tempfile.mkstemp(prefix='ytd-vk-', suffix='.mp3')
    os.close(handle)
    result = subprocess.run(
        [str(config.FFMPEG_PATH), '-hide_banner', '-loglevel', 'error', '-y',
         '-i', path, '-vn', '-c:a', 'libmp3lame', '-b:a', _UPLOAD_BITRATE, dest],
        capture_output=True, text=True, errors='replace', creationflags=_NO_WINDOW)
    if result.returncode != 0 or not os.path.getsize(dest):
        _remove_quietly(dest)
        raise VkUploadError(f'Не вышло перекодировать в mp3: {result.stderr.strip()[:200]}')
    return dest, True


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        logger.debug('Не удалось убрать временный файл %s', path)


def _with_timeout(request, timeout: float):
    """Свой таймаут для заливки: без него оборванное соединение вешает поток навсегда,
    а обычные 15 секунд не хватило бы и на средний трек."""
    def wrapper(*args, **kwargs):
        kwargs.setdefault('timeout', timeout)
        return request(*args, **kwargs)
    return wrapper


def _upload_error_text(exc: ApiError) -> str:
    """Ответы VK на заливку — по-английски и без подробностей. Переводим на понятное."""
    code = getattr(exc, 'code', None)
    if code == 270:
        return ('VK не принял файл: правообладатель запретил загружать эту запись. '
                'Это ограничение самого VK, обойти его приложение не может.')
    if code == 15:
        return 'VK отказал в доступе к музыке: возможно, аудио для аккаунта отключено.'
    if code in _TOKEN_REJECTED_CODES:
        return 'VK не принял токен, войдите в аккаунт заново.'
    return f'VK не принял файл: {exc}'


def safe_filename(name: str) -> str:
    cleaned = ''.join('_' if c in _INVALID_CHARS else c for c in name).strip()
    return cleaned or 'track'


class _TimeoutSession(requests.Session):
    """vk_api не задаёт таймаут своим запросам — без него сетевые проблемы
    вешают фоновый поток навсегда, и пользователь не видит ни ошибки, ни результата."""

    def __init__(self):
        super().__init__()
        # vk_api сам не выставляет этот заголовок, если сессия передана снаружи
        self.headers['User-agent'] = 'Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0'
        proxy.apply_to_session(self)

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', _HTTP_TIMEOUT)
        return super().request(*args, **kwargs)


def _load_vk_cookies_from_file(session: requests.Session) -> bool:
    if not config.VK_COOKIES_FILE.exists():
        return False
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(str(config.VK_COOKIES_FILE), ignore_discard=True, ignore_expires=True)
    except OSError:
        logger.exception('VkClient: не смог прочитать %s', config.VK_COOKIES_FILE)
        return False
    # Файл читается с `ignore_expires=True` — иначе сессионные куки, записанные без
    # срока, до нас бы не доехали. Но платой за это шла отправка в VK всего протухшего,
    # что накопилось: замер файла — семь мёртвых кук, среди них `httoken` на четырёх
    # доменах VK. Живой браузер такого не шлёт, а протухший `httoken` VK встречает
    # ответом «войдите» на совершенно целой сессии. Выбрасываем их здесь, после чтения
    jar.clear_expired_cookies()
    for cookie in jar:
        session.cookies.set_cookie(cookie)
    _mirror_session_cookie(session)
    has_session = _session_reaches_site(session)
    logger.debug('VkClient: загружено %d куки из %s (сессия сайта: %s)',
                 len(jar), config.VK_COOKIES_FILE, 'есть' if has_session else 'нет')
    # Раньше отвечали «сессия есть» по самому факту файла. Файл после входа через VK ID
    # существует всегда, поэтому ошибка «VK не отдал список музыки» приходила без подсказки,
    # что делать. Сессией считаем только remixsid — и только такой, который доезжает
    # до m.vk.ru: кука на чужом домене для наших запросов всё равно что её нет.
    return has_session


def _session_cookies(session: requests.Session) -> list:
    """Сессионные куки VK из мешка запросов — по именам, независимо от домена."""
    return [c for c in session.cookies
            if c.name.startswith(_SESSION_COOKIE_PREFIXES)]


def _session_reaches_site(session: requests.Session) -> bool:
    """Дойдёт ли сессионная кука до m.vk.ru — домена, с которого мы берём музыку.

    Проверять просто наличие remixsid где угодно недостаточно. Вход через VK ID
    оставляет её на `.vk.com`, а все запросы за треками идут на `m.vk.ru`: requests
    такую куку туда не отправит, и VK ответит адресом страницы входа. Снаружи это
    выглядело как «вход есть, а музыки нет» — и повторный вход ничего не менял,
    потому что кука ставилась на тот же чужой домен."""
    for cookie in _session_cookies(session):
        domain = (cookie.domain or '').lstrip('.')
        if domain == _SITE_DOMAIN or domain.endswith('.' + _SITE_DOMAIN):
            return True
    return False


def _mirror_session_cookie(session: requests.Session) -> None:
    """Одолжить рабочему домену сессию с соседнего, если своей у него нет.

    vk.com и vk.ru — один и тот же сайт с одной сессией: VK сам отдаёт remixsid то на
    одном домене, то на другом, в зависимости от того, каким путём шёл вход. Куке от
    этого ни жарко ни холодно, а вот requests привязан к домену строго, поэтому копию
    кладём туда, где её ждут наши запросы.

    Копируем только в одну сторону и только при пустом рабочем домене. Раскладывать
    куку на оба домена подряд нельзя: старый remixsid с `.vk.com` остаётся в файле
    после прошлых входов и затирал бы свежий, только что полученный для vk.ru — вход
    работал бы ровно до следующего перечитывания файла."""
    if _session_reaches_site(session):
        return
    for cookie in _session_cookies(session):
        copy = copy_module.copy(cookie)
        copy.domain = '.' + _SITE_DOMAIN
        copy.domain_specified = True
        copy.domain_initial_dot = True
        session.cookies.set_cookie(copy)
        return


def _load_vk_cookies(session: requests.Session, cookies_browser: str | None) -> tuple[bool, str | None]:
    """VkAudio ходит не в официальный API, а в мобильный сайт m.vk.ru — там нужна
    авторизованная веб-сессия (cookie remixsid), которую OAuth-токен не даёт.
    По умолчанию куки достаются прямо из установленного браузера (тем же способом,
    что yt-dlp --cookies-from-browser) — ручной экспорт файла остаётся как запасной
    вариант на случай, если авто-извлечение не сработает.

    Возвращает (есть_ли_веб-сессия, текст_ошибки_извлечения | None)."""
    # Куки встроенного входа (vk_web_login) лежат в файле и работают всегда, поэтому
    # пробуем их первыми — независимо от того, какой браузер выбран для YouTube.
    if _load_vk_cookies_from_file(session):
        return True, None
    if cookies_browser and cookies_browser != 'file':
        try:
            count = browser_cookies.load_domain_cookies_from_browser(cookies_browser, session, _VK_DOMAINS)
        except Exception as exc:
            logger.exception('VkClient: не смог достать куки vk.* из браузера %s', cookies_browser)
            return False, str(exc)
        return count > 0, None
    return False, None


def _needs_login(payload) -> bool:
    """VK просит войти: вместо данных в ответе адрес login.vk.*

    Без этой проверки наверх уходил KeyError('data'), по которому не отличить
    протухший вход от сломанного ответа."""
    location = payload.get('location') if isinstance(payload, dict) else None
    return isinstance(location, str) and any(host in location for host in _LOGIN_HOSTS)


def _is_blocked_location(payload) -> bool:
    """VK увёл на страницу блокировки, а не на страницу входа.

    Проверять надо раньше `_needs_login`: адрес блокировки тоже содержит `login`
    (`/login?act=blocked`), и без этой развилки блокировка всю дорогу выглядела
    протухшей сессией."""
    location = payload.get('location') if isinstance(payload, dict) else None
    return isinstance(location, str) and any(m in location for m in _BLOCKED_MARKERS)


def _is_blocked_api_error(exc) -> bool:
    """Та же беда со стороны API: код 5 у VK общий на «вход не удался», и отличить
    заблокированный аккаунт от отозванного токена можно только по тексту."""
    return 'blocked' in str(exc).lower()


def _is_blocked_url(url) -> bool:
    """Заблокированный аккаунт VK узнаётся по адресу, на котором осела страница."""
    return isinstance(url, str) and any(m in url for m in _BLOCKED_MARKERS)


def probe_blocked(session: requests.Session) -> bool:
    """Один запрос на входе: не заблокирован ли аккаунт?

    Нужен потому, что `VkAudio` о блокировке молчит. Её `GET m.vk.ru/` проходит
    цепочку редиректов через login.vk.ru и оседает на `/vkui/blocked`, но код
    ответа — 200, исключения нет, `user_id` получен, и клиент рапортует
    готовность. Блокировку замечали лишь следующие запросы, каждый по
    отдельности: список треков, список плейлистов, потом сторож. Пять обращений
    к аккаунту, который VK уже пометил, — и так при каждом запуске программы.

    Смотрим не тело страницы, а адрес, на котором остановились редиректы:
    вёрстку VK меняет когда захочет, а `act=blocked` в адресе — часть их
    собственной логики входа. Сетевую ошибку блокировкой не считаем: нет связи —
    это не «VK не пускает»."""
    try:
        resp = session.get('https://m.vk.ru/', allow_redirects=True)
    except requests.RequestException as exc:
        logger.debug('probe_blocked: проверка не удалась (%s)', exc)
        return False
    chain = [r.url for r in resp.history] + [resp.url]
    return any(_is_blocked_url(url) for url in chain)


BLOCKED_MESSAGE = (
    'VK заблокировал ваш аккаунт, поэтому музыка недоступна.\n\n'
    'Дело не во входе в программу: сохранённые куки и токен целы, VK отвечает '
    '«user is blocked» и на сайт, и в API. Повторные входы тут не помогут.\n\n'
    'Откройте vk.com в обычном браузере — VK покажет причину и способ снять '
    'блокировку (обычно это подтверждение по номеру телефона). После разблокировки '
    'нажмите «Повторить попытку».'
)


def check_web_session(user_id: int) -> bool:
    """Проверка входа делом, а не по именам кук: шлём ровно тот запрос, которым потом
    берём треки. Иначе окно входа рапортует «готово», а список музыки пустой."""
    session = _TimeoutSession()
    _load_vk_cookies_from_file(session)
    try:
        payload = session.post(
            _SECTION_URL,
            data={'act': 'load_section', 'owner_id': user_id, 'playlist_id': -1,
                  'offset': 0, 'type': 'playlist', 'is_loading_all': 1},
            headers={'X-Requested-With': 'XMLHttpRequest'},
            allow_redirects=False,
        ).json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug('check_web_session: запрос не удался (%s)', exc)
        return False
    if _is_blocked_location(payload):
        # Наверх уходит исключение, а не False: False здесь значит «чини перезаходом»,
        # а чинить нечего. Ловят его и сторож, и keeper — каждый по-своему
        logger.warning('check_web_session: VK заблокировал аккаунт')
        raise VkAccountBlocked(BLOCKED_MESSAGE)
    if _needs_login(payload):
        logger.debug('check_web_session: VK отправляет на страницу входа')
        return False
    return bool(payload.get('data'))


def _remove_partial(dest_dir: str, base_name: str) -> None:
    """Убрать недокачанные куски yt-dlp (.part, .ytdl) — иначе после отмены они остаются
    лежать в папке с музыкой. Сверяем имя целиком с точкой, чтобы не задеть параллельно
    качающийся трек с похожим названием.

    Вызывать только вне блока except: прерванный yt-dlp оставляет .part открытым, и
    Windows не даёт удалить файл, пока жив дескриптор. Держится он циклическими ссылками
    внутри yt-dlp, поэтому дескриптор закрывает сборка мусора — а пока мы внутри except,
    traceback не даёт освободить кадры, и collect() ничего не даёт."""
    gc.collect()
    prefix = base_name + '.'
    try:
        leftovers = [e.path for e in os.scandir(dest_dir)
                     if e.name.startswith(prefix) and e.name.endswith(('.part', '.ytdl'))]
    except OSError:
        return
    for path in leftovers:
        try:
            os.remove(path)
        except OSError:
            logger.debug('Не удалось убрать временный файл %s', path)


def _row_to_track(row: list) -> dict | None:
    """Раскладка строки из выдачи m.vk.ru — та же, что разбирает vk_api.audio.
    Возвращает None для треков, которые VK отдал без хэшей: скачать их всё равно
    нельзя (vk_api такие тоже молча пропускает)."""
    hashes = row[13].split('/')
    if len(hashes) < 6:
        return None
    full_id = (str(row[1]), str(row[0]), hashes[2], hashes[5])
    if not all(full_id):
        return None
    return {
        'id': row[0],
        'owner_id': row[1],
        # В выдаче попадаются HTML-сущности (&quot;, &#216;) — иначе они видны в списке
        'artist': html.unescape(row[4]),
        'title': html.unescape(row[3].strip()),
        'duration': row[5],
        'url': None,  # заполняется в resolve_url() перед скачиванием
        # Ключ доступа к чужой аудиозаписи: без него audio.add не добавит найденный
        # в поиске трек. Обложка нужна панели плеера.
        'access_key': row[24] if len(row) > 24 and isinstance(row[24], str) else '',
        'cover': row[14] if len(row) > 14 and isinstance(row[14], str) else '',
        '_full_id': full_id,
    }


def _rows_from_section(payload: dict, limit: int) -> list[dict]:
    """Треки из ответа load_section — общий разбор для поиска и рекомендаций."""
    data = (payload.get('data') or [{}])[0] or {}
    found = []
    for row in (data.get('list') or []):
        if _GARBLED.search(f'{row[3]} {row[4]}'):
            continue  # VK отдал название нечитаемым мусором — сопоставлять нечего
        track = _row_to_track(row)
        if track:
            found.append(track)
        if len(found) >= limit:
            break
    return found


def _cover_from_playlist(row: dict) -> str:
    """Обложка подборки. VK кладёт её то одним полем, то списком разных размеров."""
    for key in ('thumb', 'photo'):
        value = row.get(key)
        if isinstance(value, str) and value.startswith('http'):
            return value
        if isinstance(value, dict):
            # Ключи вида photo_300 / photo_600 — берём самый крупный из отданных
            sizes = [(int(k.rsplit('_', 1)[-1]), v) for k, v in value.items()
                     if k.startswith('photo_') and str(k.rsplit('_', 1)[-1]).isdigit()
                     and isinstance(v, str)]
            if sizes:
                return max(sizes)[1]
    return ''


def _mixes_from_section(payload: dict, limit: int) -> list[dict]:
    """Подборки-волны из ответа load_section.

    Рядом со списком треков VK кладёт готовые подборки, собранные его алгоритмами, —
    это и есть «волны». Здесь их только опознают и переводят в вид, понятный панели;
    треки внутри берутся потом обычным `get_playlist_tracks` по этим же координатам.

    Формат раздела VK меняет без предупреждения, поэтому разбор нарочно терпимый:
    неузнанный блок пропускаем, а не роняем всю вкладку."""
    data = (payload.get('data') or [{}])[0] or {}
    seen: set[tuple] = set()
    mixes: list[dict] = []
    for key in ('playlists', 'playlistsRaw', 'blocks'):
        for row in (data.get(key) or []):
            if not isinstance(row, dict):
                continue
            # Блок-обёртка: сами подборки лежат внутри
            nested = row.get('playlists')
            rows = nested if isinstance(nested, list) else [row]
            for item in rows:
                if not isinstance(item, dict):
                    continue
                owner_id, list_id = item.get('owner_id'), item.get('id')
                title = (item.get('title') or '').strip()
                if owner_id is None or list_id is None or not title:
                    continue
                mark = (owner_id, list_id)
                if mark in seen:
                    continue
                seen.add(mark)
                mixes.append({
                    'id': list_id,
                    'owner_id': owner_id,
                    'access_hash': item.get('access_key') or item.get('access_hash') or '',
                    'title': title,
                    'subtitle': (item.get('subtitle') or item.get('description') or '').strip(),
                    'cover': _cover_from_playlist(item),
                    'count': item.get('count') or 0,
                    '_original': None,
                })
                if len(mixes) >= limit:
                    return mixes
    return mixes


def _shelf(key: str, title: str, subtitle: str, rows: list[dict]) -> dict:
    """Полка из готовой пачки плейлистов — в том же виде, что и волна VK.

    Панель различает полки по `_items`: если он есть, треки собираются из
    перечисленных плейлистов, а не запрашиваются по координатам самой полки.
    Обложку берём у первого плейлиста с картинкой — своей у полки нет."""
    cover = ''
    for row in rows:
        cover = row.get('cover') or ''
        if cover:
            break
    return {
        'id': f'shelf:{key}', 'owner_id': 0, 'access_hash': '',
        'title': title, 'subtitle': subtitle, 'cover': cover,
        'count': sum(row.get('count') or 0 for row in rows),
        '_original': None, '_items': rows, '_section': '',
    }


def _shelves_from_playlists(playlists: list[dict]) -> list[dict]:
    """Разложить плейлисты аккаунта по осям, которые VK сам и проставил.

    Оси — только те, что взяты из данных: исполнитель (`main_artists`), жанр
    (`genres`), свежесть (`year`). Раскладывать по «настроениям» было бы честно
    лишь при живом каталоге VK, а его этому токену не дают: придумывать настроение
    самим — это выдавать свою догадку за мнение VK, чего AGENTS.md не разрешает.

    Ось попадает в выдачу, только если в ней набралось `_MIN_SHELF` плейлистов:
    полка из одного альбома человеку ничего не даёт."""
    by_artist: dict[str, list[dict]] = {}
    by_genre: dict[str, list[dict]] = {}
    fresh: list[dict] = []
    older: list[dict] = []
    this_year = datetime.date.today().year
    for row in playlists:
        for name in row.get('_artists') or []:
            by_artist.setdefault(name, []).append(row)
        for name in row.get('_genres') or []:
            by_genre.setdefault(name, []).append(row)
        year = row.get('_year') or 0
        if year:
            (fresh if year > this_year - _FRESH_YEARS else older).append(row)

    shelves: list[dict] = []
    # Жанры первыми: их мало и они шире всего — ближе всего к «под настроение»
    for name, rows in sorted(by_genre.items(), key=lambda kv: -len(kv[1])):
        if len(rows) >= _MIN_SHELF:
            shelves.append(_shelf(f'genre:{name}', name,
                                  'Жанр по данным VK', rows))
    if len(fresh) >= _MIN_SHELF:
        shelves.append(_shelf('fresh', 'Свежее',
                              f'Вышло с {this_year - _FRESH_YEARS + 1} года', fresh))
    if len(older) >= _MIN_SHELF:
        shelves.append(_shelf('older', 'Из прошлых лет',
                              f'Вышло до {this_year - _FRESH_YEARS + 1} года', older))
    for name, rows in sorted(by_artist.items(), key=lambda kv: -len(kv[1])):
        if len(rows) >= _MIN_SHELF:
            shelves.append(_shelf(f'artist:{name}', name,
                                  'Ваши альбомы этого исполнителя', rows))
    return _without_duplicates(shelves)


def _without_duplicates(shelves: list[dict]) -> list[dict]:
    """Убрать полки с точно тем же составом, что у соседней слева.

    Оси нарочно пересекаются: один альбом законно попадает и в жанр, и в год, и
    к исполнителю. Прятать вложенные полки нельзя — узкая внутри широкой это
    норма («ST1M» внутри «Рэпа»), и по такому правилу широкая полка съела бы все
    узкие, а выбор схлопнулся бы ровно там, где он и обещан.

    Совпавший до последнего альбома состав — другое дело: тогда полки отличаются
    только подписью, и вторая занимает место в полосе, ничего не давая."""
    seen: set[frozenset] = set()
    kept = []
    for shelf in shelves:
        mark = frozenset((row.get('owner_id'), row.get('id'))
                         for row in shelf['_items'])
        if mark in seen:
            continue
        seen.add(mark)
        kept.append(shelf)
    return kept


def _row_to_playlist(row: dict) -> dict:
    """Плейлист из ответа audio.getPlaylists в том виде, в каком его ждёт UI."""
    original = row.get('original') or {}
    return {
        'id': row.get('id'),
        'owner_id': row.get('owner_id'),
        'access_hash': row.get('access_key'),
        'title': row.get('title') or 'Плейлист',
        'artist': ', '.join(a.get('name', '') for a in row.get('main_artists') or []),
        'count': row.get('count') or 0,
        # Разметка самого VK — по ней и только по ней строятся полки рекомендаций
        # (см. _shelves_from_playlists). Хранить порознь от 'artist': там строка
        # для показа, здесь — имена, по которым группируют
        '_artists': [a['name'] for a in row.get('main_artists') or []
                     if isinstance(a, dict) and a.get('name')],
        '_genres': [g['name'] for g in row.get('genres') or []
                    if isinstance(g, dict) and g.get('name')],
        '_year': row.get('year') or 0,
        'cover': _cover_from_playlist(row),
        # Чужой плейлист, добавленный к себе: часть таких VK отдаёт только по координатам
        # оригинала, поэтому держим их про запас (см. get_playlist_tracks)
        '_original': {
            'owner_id': original['owner_id'],
            'id': original['playlist_id'],
            'access_hash': original.get('access_key'),
        } if original.get('playlist_id') else None,
    }


def _is_locked_cookies_error(error: str | None) -> bool:
    """yt-dlp не может скопировать файл с куками, пока браузер открыт (эксклюзивная блокировка
    файла), и падает с PermissionError/DownloadError — отличаем этот случай от «не залогинен»,
    чтобы дать пользователю понятную подсказку вместо общей."""
    if not error:
        return False
    return 'Permission denied' in error or 'Could not copy' in error


class VkClient:
    def __init__(self, token: str, cookies_browser: str | None = None):
        logger.debug('VkClient: создаю VkApi (токен: %s…)', token[:8] if token else token)
        http_session = _TimeoutSession()
        self._cookies_browser = cookies_browser
        self._browser_cookies_tried = False
        self._has_web_session, self._cookies_error = _load_vk_cookies(http_session, cookies_browser)
        self._session = vk_api.VkApi(token=token, api_version=API_VERSION, session=http_session)
        # Спрашиваем один раз и сразу: заблокирован ли аккаунт. Раньше этой развилки
        # не было, и подключение шло напролом — VkAudio молчит о блокировке, поэтому
        # о ней узнавали только треки, плейлисты и сторож, каждый своим запросом.
        # Проверка стоит здесь, до VkAudio: дальше пойдут обращения к VK, а по
        # заблокированному аккаунту им идти незачем
        if self._has_web_session and probe_blocked(http_session):
            logger.warning('VkClient: VK держит аккаунт заблокированным, подключение прекращено')
            raise VkAccountBlocked(BLOCKED_MESSAGE)
        logger.debug('VkClient: VkApi создан, инициализирую VkAudio (users.get + GET m.vk.ru)')
        try:
            self._audio = VkAudio(self._session)
        except Exception as exc:
            logger.exception('VkClient: инициализация VkAudio упала')
            if _is_blocked_api_error(exc):
                # Код 5 у VK один и на «токен отозван», и на «пользователь заблокирован».
                # Стереть здесь токен значило бы попросить новый вход, который не пройдёт:
                # VK не пускает сам аккаунт, а не приложение
                raise VkAccountBlocked(BLOCKED_MESSAGE) from exc
            rejected = isinstance(exc, ApiError) and getattr(exc, 'code', None) in _TOKEN_REJECTED_CODES
            raise VkAuthError(f'Не удалось авторизоваться в VK: {exc}', rejected) from exc
        logger.debug('VkClient: VkAudio готов, user_id=%s', self._audio.user_id)

    @property
    def user_id(self) -> int:
        return self._audio.user_id

    @property
    def has_web_session(self) -> bool:
        """Токен даёт плейлисты через API, а список треков — только сессия сайта.
        Без неё вход рабочий лишь наполовину, и в интерфейсе это должно быть видно."""
        return self._has_web_session

    def reload_web_session(self) -> bool:
        """Перечитать файл кук: сессию сайта обновили снаружи.

        Так делает фоновый перезаход (`VkSessionKeeper`): он пишет свежие куки в
        `VK_COOKIES_FILE`, но живой клиент об этом не знает — в его `requests`-сессии
        лежат прежние, уже мёртвые. Пересоздавать клиента ради этого незачем: токен,
        `VkAudio` и `user_id` не менялись, устарели только куки.

        Возвращает, появилась ли сессия сайта."""
        self._has_web_session = _load_vk_cookies_from_file(self._session.http)
        if self._has_web_session:
            self._cookies_error = None
            # Куки из браузера могли не подойти в прошлый раз — теперь путь другой,
            # и запрещать повторную попытку больше не за что
            self._browser_cookies_tried = False
        return self._has_web_session

    def get_my_tracks(self) -> list[dict]:
        return self._collect(self._iter_section())

    def get_playlists(self) -> list[dict]:
        """Список плейлистов — через официальный метод audio.getPlaylists.

        vk_api.audio.get_albums_iter() здесь бесполезен: он разбирает старую вёрстку
        m.vk.ru, которой у VK больше нет (страница отдаёт JS-приложение), и молча
        возвращает пустой список. Токен приложения audio.get не пускает, а
        audio.getPlaylists — пускает."""
        playlists = []
        offset = 0
        while True:
            try:
                page = self._session.method('audio.getPlaylists', {
                    'owner_id': self.user_id,
                    'count': _PLAYLISTS_PER_PAGE,
                    'offset': offset,
                })
            except ApiError as exc:
                logger.exception('VkClient: audio.getPlaylists отказал')
                if _is_blocked_api_error(exc):
                    raise VkAccountBlocked(BLOCKED_MESSAGE) from exc
                raise VkAuthError(f'VK не отдал список плейлистов: {exc}') from exc
            items = page.get('items') or []
            playlists.extend(_row_to_playlist(row) for row in items)
            offset += len(items)
            # Неполная страница — конец списка. Ориентироваться на page['count'] нельзя:
            # часть плейлистов VK не отдаёт, и по счётчику мы бы запросили ту же
            # страницу ещё раз и получили дубликаты
            if len(items) < _PLAYLISTS_PER_PAGE:
                break
        return playlists

    def get_playlist_tracks(self, playlist: dict) -> list[dict]:
        """Треки плейлиста — тем же load_section, что и «Моя музыка»."""
        try:
            return self._collect(self._iter_section(
                playlist['owner_id'], playlist['id'], playlist.get('access_hash')))
        except VkSessionExpired:
            raise  # дело не в плейлисте: без входа не откроется никакой
        except (VkAuthError, AccessDenied):
            original = playlist.get('_original')
            if original:
                logger.debug('Плейлист %s не открылся по своим координатам, пробуем оригинал',
                             playlist.get('id'))
                try:
                    return self._collect(self._iter_section(
                        original['owner_id'], original['id'], original.get('access_hash')))
                except (VkAuthError, AccessDenied):
                    pass
            if not self._has_web_session:
                raise  # дело не в плейлисте, а в отсутствии входа — подсказка уже внутри
            raise VkAuthError('VK не открыл этот плейлист: возможно, он удалён '
                              'или скрыт владельцем') from None

    # ---------- поиск ----------
    def search_tracks(self, query: str, limit: int = 60) -> list[dict]:
        """Поиск по музыке VK.

        Метода audio.search у токена Kate Mobile нет (VK отвечает «Unknown method
        passed»), поэтому ищем той же веб-сессией, что и «Мою музыку»: тот же
        load_section, но с type=search. Важно: строка запроса передаётся в
        `search_q`, а не в `q` — с `q` VK отвечает пустым списком без ошибки."""
        query = (query or '').strip()
        if not query:
            return []
        sent = translit(query) if has_cyrillic(query) else query

        def ask() -> dict:
            return self._post_section_raw({
                'act': 'load_section', 'owner_id': self.user_id, 'type': 'search',
                'search_q': sent, 'offset': 0, 'is_loading_all': 1,
            })

        payload = ask()
        if _needs_login(payload) and self._try_browser_cookies():
            payload = ask()
        if _needs_login(payload):
            self._has_web_session = False
            raise VkSessionExpired('Сессия сайта VK больше не действует, войдите заново.')
        return _rows_from_section(payload, limit)

    # ---------- рекомендации и волны ----------
    def recommended_tracks(self, limit: int = 60) -> list[dict]:
        """Что VK предлагает послушать: его собственная подборка.

        Каталожных методов у токена Kate Mobile нет, поэтому идём той же веб-сессией
        и тем же `load_section`, что «Моя музыка» и поиск, — меняется только `type`.
        VK со временем переименовывает разделы, поэтому пробуем несколько подряд:
        пустой ответ здесь не ошибка, а «этого раздела у вас нет».

        Возвращает пустой список, если VK не дал ничего: подменять рекомендации
        поиском внутри нельзя — тогда снаружи не отличить одно от другого
        (AGENTS.md: поиск, выданный за рекомендации, — обман). Подмену делает
        вызывающий и помечает её."""

        def ask(section: str) -> dict:
            return self._post_section_raw({
                'act': 'load_section', 'owner_id': self.user_id, 'type': section,
                'offset': 0, 'is_loading_all': 1,
            })

        for section in _RECOM_SECTIONS:
            try:
                payload = ask(section)
                if _needs_login(payload) and self._try_browser_cookies():
                    payload = ask(section)
            except (requests.RequestException, ValueError) as exc:
                logger.debug('VK: раздел %s не ответил (%s)', section, exc)
                continue
            if _needs_login(payload):
                self._has_web_session = False
                raise VkSessionExpired(
                    'Сессия сайта VK больше не действует, войдите заново.')
            found = _rows_from_section(payload, limit)
            if found:
                logger.debug('VK: рекомендации взяты из раздела %s (%d)',
                             section, len(found))
                return found
        logger.debug('VK: своих рекомендаций нет ни в одном из разделов')
        return []

    def wave_mixes(self, limit: int = 40) -> list[dict]:
        """Каталог волн VK: подборки, собранные его алгоритмами.

        Отличие от `recommended_tracks` — не «плоский список чужих треков», а выбор:
        VK отдаёт несколько подборок под разное настроение, и человек сам решает,
        какую слушать. Треки внутри каждой берутся обычным `get_playlist_tracks`
        по её координатам — отдельного метода для этого не нужно.

        Пустой список означает ровно то, что означает: волн для этого аккаунта VK
        не дал. Подставлять сюда поиск нельзя (AGENTS.md: поиск, выданный за
        рекомендации, — обман); подмену, если она где-то нужна, делает и помечает
        вызывающий."""

        def ask(section: str) -> dict:
            return self._post_section_raw({
                'act': 'load_section', 'owner_id': self.user_id, 'type': section,
                'offset': 0, 'is_loading_all': 1,
            })

        found: list[dict] = []
        seen: set[tuple] = set()
        # Волны раскиданы по разным разделам, и объединение даёт выбор шире, чем
        # любой из них поодиночке — ради выбора всё и затевалось
        for section in _RECOM_SECTIONS:
            try:
                payload = ask(section)
                if _needs_login(payload) and self._try_browser_cookies():
                    payload = ask(section)
            except (requests.RequestException, ValueError) as exc:
                logger.debug('VK: раздел %s не ответил (%s)', section, exc)
                continue
            if _needs_login(payload):
                self._has_web_session = False
                raise VkSessionExpired(
                    'Сессия сайта VK больше не действует, войдите заново.')
            for mix in _mixes_from_section(payload, limit):
                mark = (mix['owner_id'], mix['id'])
                if mark in seen:
                    continue
                seen.add(mark)
                found.append(mix)
                if len(found) >= limit:
                    logger.debug('VK: собрано %d волн', len(found))
                    return found
        logger.debug('VK: волн собрано %d', len(found))
        return found

    def section_tracks(self, section: str, limit: int = 60) -> list[dict]:
        """Треки одного раздела `load_section` — для полок, у которых нет плейлиста.

        `recoms` и `recent` отдают сразу список, а не подборку с координатами,
        поэтому открыть их как плейлист нельзя: нужен повторный запрос за тем же
        разделом."""
        try:
            payload = self._post_section_raw({
                'act': 'load_section', 'owner_id': self.user_id, 'type': section,
                'offset': 0, 'is_loading_all': 1,
            })
        except (requests.RequestException, ValueError) as exc:
            logger.debug('VK: раздел %s не ответил (%s)', section, exc)
            return []
        if _needs_login(payload):
            self._has_web_session = False
            raise VkSessionExpired('Сессия сайта VK больше не действует, войдите заново.')
        return _rows_from_section(payload, limit)

    def wave_shelves(self, limit: int = 40) -> list[dict]:
        """Все подборки вкладки «Волна»: и волны VK, и полки по его же разметке.

        Одной подборки мало — человеку нужен выбор, как в «джемах» YouTube. Но
        каталог VK этому токену почти ничего не даёт: живыми оказались только
        разделы `recoms` и `recent`, а сеяние по треку VK игнорирует — на любое
        семя приходит один и тот же список. Поэтому выбор набираем из того, что VK
        всё-таки размечает сам: разделы-списки, готовые волны, а дальше — плейлисты
        аккаунта, разложенные по проставленным самим VK жанру, году и исполнителю.

        Чего здесь нет и не будет — придуманных «настроений»: своя догадка,
        поданная как мнение VK, — та же подмена, что поиск вместо рекомендаций
        (AGENTS.md). Пустой список означает ровно то, что означает."""
        shelves: list[dict] = []
        # Разделы-списки идут первыми: это единственное, что VK собирает лично под
        # человека, — всё остальное ниже лишь раскладывает его же полку по осям
        for section, title, subtitle in _TRACK_SHELVES:
            rows = self.section_tracks(section, limit=_SHELF_PREVIEW)
            if rows:
                # Обложка есть не у каждого трека, поэтому берём у первого, у кого
                # она нашлась, — иначе плитка раздела осталась бы серой заглушкой
                art = next((r.get('cover') for r in rows if r.get('cover')), '')
                shelves.append({
                    'id': f'section:{section}', 'owner_id': 0, 'access_hash': '',
                    'title': title, 'subtitle': subtitle,
                    # У трека обложка приходит списком размеров через запятую,
                    # а загрузчику нужна одна ссылка
                    'cover': art.split(',')[0].strip(),
                    # Сколько треков в разделе, VK заранее не говорит, а спрашивать
                    # весь список ради числа — дорого. Ноль здесь честнее выдумки
                    'count': 0, '_original': None,
                    '_items': None, '_section': section,
                })
        for mix in self.wave_mixes(limit=limit):
            mix.setdefault('_items', None)
            mix.setdefault('_section', '')
            shelves.append(mix)
        try:
            playlists = self.get_playlists()
        except VkAuthError as exc:
            # Плейлисты — не главное на этой вкладке: разделы уже собраны, и ронять
            # из-за них всю «Волну» значит менять неполный ответ на пустой
            logger.debug('VK: плейлисты для полок не пришли (%s)', exc)
            playlists = []
        shelves.extend(_shelves_from_playlists(playlists))
        logger.debug('VK: подборок для «Волны» собрано %d', len(shelves))
        return shelves[:_MAX_SHELVES + len(_TRACK_SHELVES)]

    def shelf_tracks(self, shelf: dict, limit: int = 200) -> list[dict]:
        """Треки любой подборки «Волны» — какого бы рода она ни была.

        Полка бывает трёх видов, и панели незачем знать, чем они отличаются:
        раздел (`_section`), склейка из плейлистов (`_items`) или обычная волна VK
        со своими координатами."""
        section = shelf.get('_section') or ''
        if section:
            return self.section_tracks(section, limit=limit)
        items = shelf.get('_items')
        if not items:
            return self.get_playlist_tracks(shelf)
        found: list[dict] = []
        seen: set[tuple] = set()
        for item in items:
            try:
                rows = self.get_playlist_tracks(item)
            except (VkAuthError, requests.RequestException, ValueError) as exc:
                # Один недоступный альбом не повод оставить человека без полки
                logger.debug('VK: плейлист %s полки не отдал треки (%s)',
                             item.get('title'), exc)
                continue
            for row in rows:
                mark = tuple(row.get('_full_id') or ())
                if mark and mark in seen:
                    continue
                if mark:
                    seen.add(mark)
                found.append(row)
                if len(found) >= limit:
                    return found
        return found

    # ---------- добавление готовой записи в «Мою музыку» ----------
    def add_audio(self, owner_id: int, audio_id: int, access_key: str = '') -> dict:
        """Добавить существующую аудиозапись VK к себе.

        Это главный путь кнопки «+ VK»: если трек уже есть в VK, качать и заливать
        свой файл незачем. Метод audio.add токену доступен (в отличие от audio.get
        и audio.search). Возвращает координаты новой записи."""
        params = {'audio_id': int(audio_id), 'owner_id': int(owner_id)}
        if access_key:
            params['access_key'] = access_key
        try:
            new_id = self._session.method('audio.add', params)
        except ApiError as exc:
            logger.warning('audio.add не прошёл: %s', exc)
            raise VkUploadError(_upload_error_text(exc)) from exc
        return {'id': int(new_id) if new_id else None, 'owner_id': self.user_id}

    # ---------- плейлисты: изменение ----------
    def create_playlist(self, title: str, description: str = '') -> dict:
        """Создать плейлист в своём аккаунте и вернуть его описание."""
        try:
            created = self._session.method('audio.createPlaylist', {
                'owner_id': self.user_id, 'title': title, 'description': description})
        except ApiError as exc:
            raise VkAuthError(f'VK не создал плейлист: {exc}') from exc
        return _row_to_playlist(created) if isinstance(created, dict) else {}

    def edit_playlist(self, playlist_id: int, title: str, description: str = '') -> None:
        try:
            self._session.method('audio.editPlaylist', {
                'owner_id': self.user_id, 'playlist_id': int(playlist_id),
                'title': title, 'description': description})
        except ApiError as exc:
            raise VkAuthError(f'VK не переименовал плейлист: {exc}') from exc

    def delete_playlist(self, playlist_id: int) -> None:
        try:
            self._session.method('audio.deletePlaylist', {
                'owner_id': self.user_id, 'playlist_id': int(playlist_id)})
        except ApiError as exc:
            raise VkAuthError(f'VK не удалил плейлист: {exc}') from exc

    def add_to_playlist(self, playlist_id: int, audio_ids: list[str]) -> None:
        """audio_ids — строки вида «owner_id_audio_id»."""
        if not audio_ids:
            return
        try:
            self._session.method('audio.addToPlaylist', {
                'owner_id': self.user_id, 'playlist_id': int(playlist_id),
                'audio_ids': ','.join(audio_ids)})
        except ApiError as exc:
            raise VkAuthError(f'VK не добавил треки в плейлист: {exc}') from exc

    def remove_from_playlist(self, playlist_id: int, audio_ids: list[str]) -> None:
        if not audio_ids:
            return
        try:
            self._session.method('audio.removeFromPlaylist', {
                'owner_id': self.user_id, 'playlist_id': int(playlist_id),
                'audio_ids': ','.join(audio_ids)})
        except ApiError as exc:
            raise VkAuthError(f'VK не убрал треки из плейлиста: {exc}') from exc

    def _iter_section(self, owner_id: int | None = None, album_id: int | None = None,
                      access_hash: str | None = None):
        """Список треков — одним запросом на 2000 штук.

        Не используем vk_api.audio.get_iter(): он к тому же списку дополнительно
        запрашивает прямые ссылки пакетами по 10 треков с паузой 1.5 с между
        пакетами. Для 1500 треков это больше четырёх минут, и всё это время список
        пуст. Для показа ссылки не нужны — исполнитель, название и длительность уже
        есть в самой выдаче, — поэтому ссылки берём позже и только для тех треков,
        которые пользователь выбрал (resolve_url)."""
        owner_id = self.user_id if owner_id is None else owner_id
        offset = 0
        while True:
            data = self._load_section(owner_id, album_id, access_hash, offset)['data'][0]
            if not data:
                raise AccessDenied(f"You don't have permissions to browse {owner_id}'s audio")
            rows = data['list']
            if not rows:
                break
            for row in rows:
                track = _row_to_track(row)
                if track:
                    yield track
            if not data.get('hasMore'):
                break
            offset += _TRACKS_PER_PAGE

    def _load_section(self, owner_id, album_id, access_hash, offset) -> dict:
        """Один запрос к списку треков с разбором «войдите заново».

        Если VK отправляет на страницу входа, один раз пробуем куки из браузера:
        сохранённые в файле могли устареть, а в браузере человек обычно залогинен. Раньше
        файл всегда выигрывал, и живая сессия браузера не пробовалась вовсе."""
        payload = self._post_section(owner_id, album_id, access_hash, offset)
        if _is_blocked_location(payload):
            # Раньше блокировки: куки браузера пробовать незачем, там тот же аккаунт
            raise VkAccountBlocked(BLOCKED_MESSAGE)
        if _needs_login(payload) and self._try_browser_cookies():
            payload = self._post_section(owner_id, album_id, access_hash, offset)
        if _is_blocked_location(payload):
            raise VkAccountBlocked(BLOCKED_MESSAGE)
        if _needs_login(payload):
            self._has_web_session = False
            logger.warning('VkClient: VK отозвал сессию сайта, нужен новый вход')
            raise VkSessionExpired(
                'Сессия сайта VK больше не действует, VK просит войти заново.\n\n'
                'Нажмите «Войти в VK»: вход проходит прямо в приложении, '
                'нужные куки сохранятся автоматически.')
        return payload

    def _post_section(self, owner_id, album_id, access_hash, offset) -> dict:
        return self._post_section_raw({
            'act': 'load_section',
            'owner_id': owner_id,
            'playlist_id': album_id if album_id else -1,
            'offset': offset,
            'type': 'playlist',
            'access_hash': access_hash,
            'is_loading_all': 1,
        })

    def _post_section_raw(self, data: dict) -> dict:
        return self._session.http.post(
            _SECTION_URL,
            data=data,
            headers={'X-Requested-With': 'XMLHttpRequest'},
            allow_redirects=False,
        ).json()

    def _try_browser_cookies(self) -> bool:
        """Подтянуть куки vk.* из браузера — один раз за жизнь клиента."""
        if self._browser_cookies_tried or not self._cookies_browser or self._cookies_browser == 'file':
            return False
        self._browser_cookies_tried = True
        try:
            count = browser_cookies.load_domain_cookies_from_browser(
                self._cookies_browser, self._session.http, _VK_DOMAINS)
        except Exception as exc:
            logger.debug('VkClient: куки vk.* из браузера %s недоступны (%s)', self._cookies_browser, exc)
            return False
        if count:
            self._has_web_session = True
        return count > 0

    def resolve_url(self, track: dict) -> str:
        """Достать прямую ссылку на файл. В общем списке её нет — нужен отдельный
        запрос, поэтому делаем его прямо перед скачиванием конкретного трека."""
        global _last_reload
        full_id = track.get('_full_id')
        if not full_id:
            raise ValueError('У трека нет данных для получения ссылки')
        with _reload_lock:
            delay = _RELOAD_DELAY - (time.time() - _last_reload)
            if delay > 0:
                time.sleep(delay)
            # scrap_tracks заодно расшифровывает обфусцированные VK ссылки
            resolved = list(scrap_tracks([tuple(full_id)], self.user_id, self._session.http))
            _last_reload = time.time()
        if not resolved or not resolved[0].get('url'):
            raise ValueError('VK не отдал ссылку на файл (трек мог стать недоступен)')
        return resolved[0]['url']

    def _collect(self, iterator) -> list[dict]:
        try:
            return list(iterator)
        except (KeyError, AccessDenied) as exc:
            # Без веб-сессии VK отвечает по-разному: на список треков — мусором (KeyError
            # внутри vk_api), на список плейлистов — «нет прав». Причина одна и та же.
            logger.exception('VkClient: VK вернул неожиданный ответ (нет авторизованной веб-сессии?)')
            if self._has_web_session:
                hint = ''
            elif self._cookies_browser and _is_locked_cookies_error(self._cookies_error):
                hint = (f'\n\nКуки браузера «{self._cookies_browser}» прочитать не вышло: пока браузер '
                        'запущен, он держит файл с куками заблокированным. Нажмите «Войти в VK» и '
                        'войдите внутри приложения, тогда браузер вообще не нужен.')
            else:
                hint = ('\n\nНажмите «Войти в VK» и войдите в свой аккаунт: вход проходит прямо '
                        'в приложении, нужные куки сохранятся автоматически.')
            raise VkAuthError(f'VK не отдал список музыки (сессия не авторизована).{hint}') from exc

    # ---------- заливка в свою музыку ----------
    def upload_audio(self, path: str, artist: str = '', title: str = '',
                     status_cb=None) -> dict:
        """Положить файл в «Мою музыку» своего аккаунта VK.

        Исполнителя и название берём из имени файла, если их не передали.
        Возвращает описание сохранённой записи (id, owner_id, artist, title)."""
        if not os.path.exists(path):
            raise VkUploadError(f'Файла больше нет: {os.path.basename(path)}')
        if not artist and not title:
            artist, title = split_artist_title(os.path.splitext(os.path.basename(path))[0])

        mp3_path, temporary = _to_mp3(path, status_cb)
        try:
            size = os.path.getsize(mp3_path)
            if size > _AUDIO_MAX_BYTES:
                raise VkUploadError(
                    f'VK берёт файлы до {_AUDIO_MAX_BYTES // 1024 // 1024} МБ, '
                    f'а здесь {size // 1024 // 1024} МБ')
            if status_cb:
                status_cb('Загружаю в VK')
            uploader = VkUpload(self._session)
            # Своя сессия внутри VkUpload заводится без наших настроек: без этого
            # заливка пошла бы мимо прокси, а таймаута у неё нет вовсе
            proxy.apply_to_session(uploader.http)
            uploader.http.request = _with_timeout(uploader.http.request, _UPLOAD_TIMEOUT)
            try:
                saved = uploader.audio(mp3_path, artist, title)
            except ApiError as exc:
                logger.warning('Заливка в VK не прошла: %s', exc)
                raise VkUploadError(_upload_error_text(exc)) from exc
        finally:
            if temporary:
                _remove_quietly(mp3_path)
        logger.info('В музыку VK добавлено: %s — %s', artist, title)
        return saved

    @staticmethod
    def download_track(track: dict, dest_dir: str, base_name: str,
                       progress_cb=None, cancel_event=None, status_cb=None) -> str:
        """Скачать трек в dest_dir под именем base_name. Возвращает путь к готовому файлу
        (расширение выбирается при скачивании, поэтому имя передаём без него).

        Качаем через yt-dlp, а не обычным GET: сейчас VK отдаёт музыку не файлом, а
        HLS-плейлистом (index.m3u8), где часть сегментов зашифрована AES-128 — простой
        запрос сохранил бы текстовый плейлист вместо звука. Собственный HLS-демуксер
        ffmpeg на этих плейлистах молча теряет зашифрованные сегменты (трек выходит
        короче), а yt-dlp расшифровывает и склеивает их правильно."""
        url = track.get('url')
        if not url:
            raise ValueError('У трека нет ссылки на файл (возможно, он недоступен для скачивания)')

        def hook(d):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Скачивание отменено')
            if d['status'] == 'downloading' and progress_cb:
                # HLS не знает итогового размера: total_bytes_estimate пересчитывается на
                # каждом фрагменте, из-за чего проценты прыгали вперёд-назад. Считаем по
                # номеру фрагмента — он растёт монотонно.
                total_frags = d.get('fragment_count')
                if total_frags:
                    progress_cb(d.get('fragment_index') or 0, total_frags)
                else:
                    progress_cb(d.get('downloaded_bytes') or 0,
                                d.get('total_bytes') or d.get('total_bytes_estimate') or 0)
            elif d['status'] == 'finished' and status_cb:
                # Байты на месте, но впереди сборка mp3 — до неё это ещё не «готово»
                status_cb('Обработка')

        def pp_hook(d):
            if status_cb and d.get('status') == 'started':
                status_cb('Обработка')

        # '%' в названии трека для yt-dlp — начало подстановки, поэтому удваиваем
        outtmpl = os.path.join(dest_dir, base_name.replace('%', '%%') + '.%(ext)s')
        info = {}
        failure = ''
        try:
            info = ytdlp_engine.download(url, ytdlp_engine.build_audio_file_opts(outtmpl, hook, pp_hook))
        except Exception as exc:
            # Наружу выносим только текст: сам объект исключения держит traceback, а тот —
            # кадры yt-dlp с ещё открытым .part, который надо удалить (см. _remove_partial)
            failure = str(exc) or exc.__class__.__name__
        if failure:
            _remove_partial(dest_dir, base_name)
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Скачивание отменено')
            raise RuntimeError(failure)
        path = ytdlp_engine.downloaded_path(info)
        if not path or not os.path.exists(path):
            raise RuntimeError('Файл не сохранился: VK мог оборвать ссылку, попробуйте ещё раз')
        if progress_cb:
            progress_cb(1, 1)  # 100% ставим только когда файл реально готов
        return path
