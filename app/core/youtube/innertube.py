"""Один-единственный адаптер к внутреннему веб-API YouTube (InnerTube).

Это тот же API, которым пользуется сам сайт music.youtube.com в браузере. Он
недокументирован, поэтому здесь соблюдаются простые правила:

* обращения к нему собраны в одном файле — по интерфейсу они не расползаются;
* у каждого запроса есть таймаут и ограниченное число попыток;
* ответ не разбирается по жёсткому пути вида contents[0].tabs[3]: вложенность
  меняется, а нужные куски ищутся обходом дерева по именам блоков;
* любая ошибка возвращает пустой список, а не исключение наружу;
* в журнал не попадают ни куки, ни заголовок авторизации.

Авторизация берётся из браузера пользователя — теми же куками, что уже
используются для скачивания. Пароли нигде не хранятся и не запрашиваются: если
кук нет, API отвечает как гостю, и это допустимый режим работы.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time

import requests

from .. import proxy
from ..browser_cookies import load_domain_cookies_from_browser

logger = logging.getLogger(__name__)

_MUSIC_ORIGIN = 'https://music.youtube.com'
_API = _MUSIC_ORIGIN + '/youtubei/v1/'
# Ключ веб-клиента YouTube Music. Он публичный, одинаковый для всех и лежит в
# исходниках самой страницы — это не секрет пользователя.
_KEY = 'AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30'
_CLIENT = {'clientName': 'WEB_REMIX', 'clientVersion': '1.20240403.01.00'}
_DOMAINS = ('youtube.com', 'google.com')

_TIMEOUT = 12
_RETRIES = 2
# Ответ главной страницы — это мегабайты JSON. Больше просто не разбираем.
_MAX_BYTES = 12 * 1024 * 1024
# Сервис может лечь целиком (нет сети, YouTube поменял API). Тогда не долбимся
# в него на каждый чих, а ждём.
_COOLDOWN = 300

# Блоки ответа, в которых лежат треки. Их набор менялся не раз, поэтому смотрим
# на все известные разом: какой попадётся, из такого и соберём.
# Фильтр «Видео» в поиске YouTube Music — тот же параметр, что подставляет сайт
_VIDEO_FILTER = 'EgWKAQIQAWoKEAkQChAFEAMQBA%3D%3D'
# Клип и песня редко совпадают по длительности секунда в секунду: у клипа бывает
# вступление или затянутый конец. Пятнадцати секунд хватает, чтобы принять свой
# ролик и отсечь чужой — сборник или расширенную версию
_CLIP_SLACK = 15

_ITEM_KEYS = (
    'playlistPanelVideoRenderer',
    'musicResponsiveListItemRenderer',
    'musicTwoRowItemRenderer',
    'compactVideoRenderer',
    'videoRenderer',
)


def cookie_value(session: requests.Session, name: str) -> str:
    """Значение куки по имени, без падения на дубликатах.

    Браузер легко отдаёт одну и ту же куку для `.google.com` и `.youtube.com`;
    `session.cookies.get()` в таком случае бросает CookieConflictError и роняет
    весь запрос. Берём последнюю подходящую, предпочитая домен YouTube."""
    found = ''
    for cookie in session.cookies:
        if cookie.name != name or not cookie.value:
            continue
        if 'youtube' in (cookie.domain or ''):
            return cookie.value
        found = cookie.value
    return found


class InnerTubeError(Exception):
    """Внутренний API не ответил. Наружу не выходит — ловится здесь же."""


class InnerTube:
    """Тонкий клиент. Ходит в сеть, поэтому вызывать только из фонового потока."""

    def __init__(self, cookies_browser_provider=None):
        self._browser_provider = cookies_browser_provider or (lambda: None)
        self._session: requests.Session | None = None
        self._session_browser = ''
        self._authorized = False
        self._lock = threading.RLock()
        self._blocked_until = 0.0

    # ---------- сессия ----------
    @property
    def authorized(self) -> bool:
        """Есть ли куки пользователя. Без них ответы общие, без «для вас»."""
        return self._authorized

    @property
    def available(self) -> bool:
        """Не в паузе ли после серии неудач."""
        return time.monotonic() >= self._blocked_until

    def reset(self) -> None:
        """Забыть сессию — например, после смены браузера в настройках."""
        with self._lock:
            self._session = None
            self._session_browser = ''
            self._authorized = False
            self._blocked_until = 0.0

    def _ensure_session(self) -> requests.Session:
        browser = self._browser_provider() or ''
        with self._lock:
            if self._session is not None and self._session_browser == browser:
                return self._session
            session = requests.Session()
            session.headers.update({
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                               'AppleWebKit/537.36 (KHTML, like Gecko) '
                               'Chrome/122.0.0.0 Safari/537.36'),
                'Accept-Language': 'ru,en;q=0.8',
                'Content-Type': 'application/json',
                'Origin': _MUSIC_ORIGIN,
                'Referer': _MUSIC_ORIGIN + '/',
                'X-Goog-AuthUser': '0',
            })
            proxy.apply_to_session(session)
            self._authorized = False
            if browser and browser != 'file':
                try:
                    count = load_domain_cookies_from_browser(browser, session, _DOMAINS)
                    self._authorized = bool(cookie_value(session, 'SAPISID')
                                            or cookie_value(session, '__Secure-3PAPISID'))
                    # Имена и значения кук в журнал не пишем — только их количество
                    logger.info('InnerTube: куки из браузера %s: %d шт., вход %s',
                                browser, count, 'есть' if self._authorized else 'нет')
                except Exception as exc:  # noqa: BLE001 — браузер мог держать базу
                    logger.info('InnerTube: куки из браузера не прочитались (%s)',
                                type(exc).__name__)
            self._session = session
            self._session_browser = browser
            return session

    def _auth_header(self, session: requests.Session) -> str:
        """Подпись SAPISIDHASH — то же, что считает сам сайт в браузере.

        Сам секрет ни в журнал, ни куда-либо ещё не уходит."""
        sapisid = (cookie_value(session, 'SAPISID')
                   or cookie_value(session, '__Secure-3PAPISID') or '')
        if not sapisid:
            return ''
        stamp = int(time.time())
        digest = hashlib.sha1(
            f'{stamp} {sapisid} {_MUSIC_ORIGIN}'.encode('utf-8')).hexdigest()
        return f'SAPISIDHASH {stamp}_{digest}'

    # ---------- запросы ----------
    def _post(self, endpoint: str, payload: dict) -> dict:
        if not self.available:
            raise InnerTubeError('внутренний API временно отключён')
        session = self._ensure_session()
        body = {'context': {'client': dict(_CLIENT, hl='ru', gl='RU'),
                            'user': {'lockedSafetyMode': False}}}
        body.update(payload)
        headers = {}
        auth = self._auth_header(session)
        if auth:
            headers['Authorization'] = auth
        url = f'{_API}{endpoint}?key={_KEY}&prettyPrint=false'

        last = ''
        for attempt in range(_RETRIES):
            try:
                response = session.post(url, json=body, headers=headers,
                                        timeout=_TIMEOUT)
                if response.status_code in (401, 403):
                    # Куки протухли: дальше работаем как гость, а не молча
                    self._authorized = False
                    raise InnerTubeError(f'нет доступа ({response.status_code})')
                response.raise_for_status()
                if len(response.content) > _MAX_BYTES:
                    raise InnerTubeError('ответ слишком большой')
                data = response.json()
                if not isinstance(data, dict):
                    raise InnerTubeError('ответ не похож на JSON-объект')
                return data
            except InnerTubeError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = type(exc).__name__
                if attempt + 1 < _RETRIES:
                    time.sleep(1.0)
        self._blocked_until = time.monotonic() + _COOLDOWN
        raise InnerTubeError(f'нет ответа ({last})')

    # ---------- готовые запросы ----------
    def radio(self, video_id: str, limit: int = 25) -> list[dict]:
        """Очередь «радио» по ролику — то же, что даёт кнопка Radio на сайте."""
        video_id = (video_id or '').strip()
        if not video_id:
            return []
        payload = {'videoId': video_id, 'playlistId': f'RDAMVM{video_id}',
                   'isAudioOnly': True, 'params': 'wAEB'}
        try:
            data = self._post('next', payload)
        except InnerTubeError as exc:
            logger.info('InnerTube: радио не получилось (%s)', exc)
            return []
        items = parse_items(data, limit + 1)
        # Первым в ответе идёт сам ролик-затравка: в подборке он не нужен
        return [item for item in items if item['id'] != video_id][:limit]

    def related(self, video_id: str, limit: int = 25) -> list[dict]:
        """Похожее на ролик. Берётся из той же ленты, что и радио."""
        return self.radio(video_id, limit)

    def home_full(self, limit_per_section: int = 20):
        """Главная целиком: полки с треками и полки с подборками.

        Один запрос на оба списка: ответ главной — это мегабайты JSON, тянуть
        их дважды ради разных частей одного и того же ответа незачем."""
        try:
            data = self._post('browse', {'browseId': 'FEmusic_home'})
        except InnerTubeError as exc:
            logger.info('InnerTube: главная не получилась (%s)', exc)
            return [], []
        return (parse_sections(data, limit_per_section),
                parse_mix_sections(data, limit_per_section))

    def home(self, limit_per_section: int = 20) -> list[tuple[str, list[dict]]]:
        """Главная YouTube Music: подборки с их настоящими заголовками."""
        return self.home_full(limit_per_section)[0]

    def library(self, limit: int = 40) -> list[dict]:
        """Загруженное и сохранённое пользователем. Без кук вернёт пусто."""
        if not self._authorized:
            return []
        try:
            data = self._post('browse', {'browseId': 'FEmusic_liked_videos'})
        except InnerTubeError as exc:
            logger.info('InnerTube: библиотека не получилась (%s)', exc)
            return []
        return parse_items(data, limit)

    def playlists(self, limit: int = 40) -> list[dict]:
        """Плейлисты пользователя из его библиотеки YouTube Music.

        Без кук список личных плейлистов не существует — возвращаем пусто,
        а не выдумываем чужие подборки."""
        if not self._authorized:
            return []
        result: list[dict] = []
        seen: set[str] = set()
        for browse_id in ('FEmusic_liked_playlists', 'FEmusic_library_landing'):
            try:
                data = self._post('browse', {'browseId': browse_id})
            except InnerTubeError as exc:
                logger.info('InnerTube: плейлисты не получились (%s)', exc)
                continue
            for entry in parse_playlists(data, limit):
                if entry['id'] in seen:
                    continue
                seen.add(entry['id'])
                result.append(entry)
                if len(result) >= limit:
                    return result
            if result:
                break
        return result

    def playlist_items(self, playlist_id: str, limit: int = 200) -> list[dict]:
        """Треки плейлиста. Номер — обычный `PL…`/`VL…`, приставку добавим сами."""
        playlist_id = (playlist_id or '').strip()
        if not playlist_id:
            return []
        browse_id = playlist_id if playlist_id.startswith('VL') else 'VL' + playlist_id
        try:
            data = self._post('browse', {'browseId': browse_id})
        except InnerTubeError as exc:
            logger.info('InnerTube: плейлист не открылся (%s)', exc)
            return []
        return parse_items(data, limit)

    def music_video(self, title: str, artist: str, duration: int = 0) -> str:
        """Номер настоящего клипа для песни. Не нашли — пустая строка.

        YouTube Music отдаёт песни как «art tracks»: ролик, в котором вместо
        картинки одна обложка альбома. Смотреть такое в видеорежиме незачем, а
        ссылки на клип в ответе плеера нет — переключатель «Песня / Видео» на
        сайте берёт её из мобильного клиента, который сюда не пускает.

        Поэтому ищем клип тем же поиском, но с фильтром «Видео», и берём первое
        совпадение по трём признакам сразу: тот же исполнитель, название песни
        внутри названия ролика и длительность в пределах пятнадцати секунд.
        Порознь любой из них ошибается — поиск охотно подсовывает часовые
        сборники, каверы и чужие перезаливки того же трека."""
        title, artist = (title or '').strip(), (artist or '').strip()
        if not title:
            return ''
        try:
            data = self._post('search', {'query': f'{artist} {title}'.strip(),
                                         'params': _VIDEO_FILTER})
        except InnerTubeError as exc:
            logger.info('InnerTube: клип не искался (%s)', exc)
            return ''
        wanted_artist, wanted_title = _plain(artist), _plain(title)
        for item in parse_items(data, 8):
            if duration and (not item['duration']
                             or abs(item['duration'] - duration) > _CLIP_SLACK):
                continue
            if wanted_artist and _plain(item['artist']) != wanted_artist:
                continue
            if wanted_title not in _plain(item['title']):
                continue
            return item['id']
        return ''

    def search(self, query: str, limit: int = 25, music_only: bool = True) -> list[dict]:
        """Поиск внутри YouTube Music. Для обычных роликов есть yt-dlp."""
        query = (query or '').strip()
        if not query:
            return []
        payload = {'query': query}
        if music_only:
            # Фильтр «Песни» — параметр, который сайт подставляет сам
            payload['params'] = 'EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D'
        try:
            data = self._post('search', payload)
        except InnerTubeError as exc:
            logger.info('InnerTube: поиск не получился (%s)', exc)
            return []
        return parse_items(data, limit)


# ---------- разбор ответа ----------
# Обход дерева вместо точного пути — сознательно: у YouTube между версиями
# меняется вложенность, а имена блоков живут годами.
def _walk(node, key: str, found: list) -> None:
    """Собрать в `found` все блоки с указанным именем."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, dict):
                found.append(value)
            else:
                _walk(value, key, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, key, found)


def _flatten(node):
    """Все словари внутри блока, включая его самого."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(v for v in item if isinstance(v, (dict, list)))


def _text(node) -> str:
    """Текст блока YouTube: он бывает `simpleText`, бывает списком `runs`."""
    if not isinstance(node, dict):
        return ''
    simple = node.get('simpleText')
    if isinstance(simple, str):
        return simple.strip()
    runs = node.get('runs')
    if isinstance(runs, list):
        return ''.join(r.get('text', '') for r in runs
                       if isinstance(r, dict) and isinstance(r.get('text'), str)).strip()
    return ''


def _duration(value: str) -> int:
    """«3:21» или «1:02:15» в секунды. Всё остальное — ноль."""
    parts = (value or '').strip().split(':')
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _thumbnail(node) -> str:
    """Самая крупная обложка из блока — они идут по возрастанию размера."""
    best = ''
    for item in _flatten(node):
        thumbs = item.get('thumbnails')
        if not isinstance(thumbs, list):
            continue
        for thumb in thumbs:
            if isinstance(thumb, dict) and isinstance(thumb.get('url'), str):
                best = thumb['url']
    return best


def _plain(value: str) -> str:
    """Строка для сравнения названий: без скобок, знаков и разного регистра.

    В названии ролика к песне обычно приписано «(Official Video)», а в имени
    исполнителя — «- Topic». Сравнивать такое как есть бессмысленно."""
    value = re.sub(r'\(.*?\)|\[.*?\]', ' ', (value or '').lower())
    value = re.sub(r'[^0-9a-zа-яё]+', ' ', value)
    return ' '.join(value.split())


def _video_id(node) -> str:
    """Номер ролика лежит то прямо в блоке, то внутри команды перехода."""
    for item in _flatten(node):
        value = item.get('videoId')
        if isinstance(value, str) and value:
            return value
    return ''


def _columns(node) -> list[str]:
    """Тексты колонок — так устроены строки списков в YouTube Music."""
    found: list = []
    _walk(node, 'musicResponsiveListItemFlexColumnRenderer', found)
    texts = [_text(col.get('text')) for col in found]
    return [text for text in texts if text]


def parse_item(node) -> dict | None:
    """Из блока YouTube — простая запись о треке. Не разобралось — None."""
    if not isinstance(node, dict):
        return None
    video_id = _video_id(node)
    # Номер ролика — ровно 11 знаков. Проверка заодно отсекает всё, что
    # обходом дерева зацепилось случайно.
    if len(video_id) != 11:
        return None
    title = _text(node.get('title'))
    subtitle = (_text(node.get('longBylineText')) or _text(node.get('shortBylineText'))
                or _text(node.get('subtitle')))
    length = _text(node.get('lengthText'))
    if not title or not subtitle:
        columns = _columns(node)
        if columns:
            title = title or columns[0]
            if len(columns) > 1:
                subtitle = subtitle or ' • '.join(columns[1:])
    if not title:
        return None

    # «Исполнитель • Альбом • 3:21» — имя слева, длительность где-то справа
    pieces = [piece.strip() for piece in subtitle.split('•')] if subtitle else []
    artist = ''
    duration = _duration(length)
    for piece in pieces:
        seconds = _duration(piece)
        if seconds:
            duration = duration or seconds
        elif not artist and piece.lower() not in ('песня', 'song', 'видео', 'video'):
            artist = piece
    cover = _thumbnail(node) or f'https://i.ytimg.com/vi/{video_id}/mqdefault.jpg'
    return {'id': video_id, 'title': title, 'artist': artist,
            'duration': duration, 'cover': cover}


def parse_items(data, limit: int) -> list[dict]:
    """Все треки из ответа, без повторов, в порядке появления."""
    result: list[dict] = []
    seen: set[str] = set()
    for key in _ITEM_KEYS:
        found: list = []
        _walk(data, key, found)
        for node in found:
            item = parse_item(node)
            if item is None or item['id'] in seen:
                continue
            seen.add(item['id'])
            result.append(item)
            if len(result) >= limit:
                return result
    return result


def _playlist_id(node, allow_mix: bool = False) -> str:
    """Номер плейлиста: в переходе он идёт как `VLPL…`, в командах — как `PL…`."""
    for item in _flatten(node):
        value = item.get('browseId')
        if isinstance(value, str) and value.startswith('VL') and len(value) > 4:
            return value[2:]
    for item in _flatten(node):
        value = item.get('playlistId')
        if not isinstance(value, str) or len(value) <= 4:
            continue
        # `RD…` — это радио. В списке личных плейлистов ему не место, а вот
        # миксы главной — ровно такие подборки, и без них лента почти пуста
        if value.startswith('RD') and not allow_mix:
            continue
        return value
    return ''


def parse_playlist(node, allow_mix: bool = False) -> dict | None:
    """Плитка плейлиста — в простую запись. Не разобралось — None."""
    if not isinstance(node, dict):
        return None
    playlist_id = _playlist_id(node, allow_mix)
    # Плитка трека тоже носит с собой номер плейлиста — отличаем по ролику
    if not playlist_id or _video_id(node):
        return None
    title = _text(node.get('title')) or ''
    if not title:
        columns = _columns(node)
        title = columns[0] if columns else ''
    if not title:
        return None
    subtitle = _text(node.get('subtitle'))
    count = 0
    for piece in (subtitle or '').split('•'):
        digits = ''.join(ch for ch in piece if ch.isdigit())
        if digits and any(word in piece.lower() for word in ('трек', 'song', 'видео', 'video')):
            count = int(digits)
            break
    return {'id': playlist_id, 'title': title, 'subtitle': subtitle,
            'count': count, 'cover': _thumbnail(node)}


def parse_playlists(data, limit: int, allow_mix: bool = False) -> list[dict]:
    """Все плейлисты из ответа, без повторов."""
    result: list[dict] = []
    seen: set[str] = set()
    for key in ('musicTwoRowItemRenderer', 'musicResponsiveListItemRenderer'):
        found: list = []
        _walk(data, key, found)
        for node in found:
            entry = parse_playlist(node, allow_mix)
            if entry is None or entry['id'] in seen:
                continue
            seen.add(entry['id'])
            result.append(entry)
            if len(result) >= limit:
                return result
    return result


def _shelves(data) -> list:
    """Полки ответа — и карусели, и обычные списки."""
    shelves: list = []
    _walk(data, 'musicCarouselShelfRenderer', shelves)
    _walk(data, 'musicShelfRenderer', shelves)
    return shelves


def _shelf_title(shelf) -> str:
    header: list = []
    _walk(shelf.get('header') or {}, 'title', header)
    for node in header:
        title = _text(node)
        if title:
            return title
    return ''


def parse_sections(data, limit: int) -> list[tuple[str, list[dict]]]:
    """Подборки главной страницы вместе с их заголовками."""
    sections: list[tuple[str, list[dict]]] = []
    seen_titles: set[str] = set()
    for shelf in _shelves(data):
        title = _shelf_title(shelf)
        if not title or title in seen_titles:
            continue
        items = parse_items(shelf, limit)
        if not items:
            continue
        seen_titles.add(title)
        sections.append((title, items))
    return sections


def parse_mix_sections(data, limit: int) -> list[tuple[str, list[dict]]]:
    """Полки, где лежат не треки, а готовые подборки.

    Главная YouTube Music в основном из них и состоит: миксы, подборки по
    настроению и жанрам. Если брать только полки с треками, от всей ленты
    остаётся одна-две штуки — а выбирать человеку не из чего."""
    sections: list[tuple[str, list[dict]]] = []
    seen_titles: set[str] = set()
    seen_ids: set[str] = set()
    for shelf in _shelves(data):
        title = _shelf_title(shelf)
        if not title or title in seen_titles:
            continue
        mixes = [entry for entry in parse_playlists(shelf, limit, allow_mix=True)
                 if entry['id'] not in seen_ids]
        if not mixes:
            continue
        seen_ids.update(entry['id'] for entry in mixes)
        seen_titles.add(title)
        sections.append((title, mixes))
    return sections
