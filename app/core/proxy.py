"""Единая точка настройки прокси для всех сетевых частей приложения.

Приложение скачивает само (yt-dlp и requests в своём процессе), а не через браузер,
поэтому прокси-расширения браузера на загрузки не влияют - об этом спрашивают чаще
всего. Системный прокси Windows и режим TUN/VPN работают сами собой; клиент вроде
Clash или v2rayN без системного прокси приходится указывать адресом.

Режимы:
  auto   - системный прокси, а если его нет, ищем локальный клиент на обычных портах;
  off    - прямое подключение, системные настройки игнорируются;
  manual - заданный пользователем адрес.
"""

import logging
import os
import socket
import urllib.request
from urllib.parse import quote, unquote, urlparse, urlunparse

import requests

from . import frag_proxy

logger = logging.getLogger(__name__)

MODE_AUTO, MODE_OFF, MODE_MANUAL = 'auto', 'off', 'manual'

# Порты, на которых слушают распространённые клиенты: Clash Verge/mihomo (7897),
# Clash for Windows (7890), v2rayN (10809), Nekoray и сборки v2rayN (2080),
# Shadowsocks/mixed (1080), прочие (8889).
_LOCAL_PORTS = (7897, 7890, 10809, 2080, 1080, 8889)

# Что проверяет кнопка «Проверить». Видеосерверы отдельной строкой не для красоты:
# сайт YouTube открывается почти всегда, а файлы качаются с *.googlevideo.com, и
# типичная поломка - когда доступен только первый.
_VIDEO_TARGET = 'видеосерверы YouTube'
_CHECK_TARGETS = (
    ('YouTube', 'https://www.youtube.com/generate_204'),
    (_VIDEO_TARGET, 'https://redirector.googlevideo.com/generate_204'),
    ('VK', 'https://api.vk.ru/method/utils.getServerTime'),
)
# Тот же сервер по открытому порту: отвечает он или нет - разница между «нет маршрута»
# и «рвётся именно защищённое соединение» (см. _video_http_alive).
_VIDEO_HTTP_TARGET = 'http://redirector.googlevideo.com/'
_PROBE_URL = 'https://www.youtube.com/generate_204'
_ENV_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')

# Домены VK ходят мимо прокси всегда. Причина не в скорости: вход в аккаунт с
# зарубежного адреса, да ещё и меняющегося от запуска к запуску, VK считает угоном
# и замораживает аккаунт до подтверждения по телефону. В России VK и так открыт,
# прокси нужен для YouTube, поэтому VK отправляем напрямую.
# Обложки (`vkuserphoto`) входа не несут, и причина у них другая: их много -
# замер по журналу, 537 обращений против 416 к самому m.vk.ru, - и гнать этот
# поток картинок через посредник значит без нужды греть чужой канал и тормозить
# отрисовку списков.
VK_DIRECT_DOMAINS = ('vk.com', 'vk.ru', 'vk-cdn.net', 'vk-portal.net',
                     'userapi.com', 'vkuseraudio.net', 'vkuseraudio.com',
                     'vkuservideo.net', 'vkuservideo.com', 'vkuserphoto.ru',
                     'vkuserphoto.com', 'mycdn.me', 'vkgroup.net')

# Снимок системных настроек делаем до того, как сами начнём писать в окружение:
# иначе авто-режим на втором вызове находил бы собственный прокси.
_SYSTEM_PROXIES = urllib.request.getproxies()

# 'url' - что выбрал пользователь, 'local' - адрес посредника, режущего ClientHello
# (см. frag_proxy). Наружу отдаём local, если он есть: сам посредник ходит через url.
_state = {'mode': MODE_AUTO, 'url': None, 'fragment': False, 'local': None}

# Фабрика выбора прокси для Qt - одна на всё время работы, см. _vk_direct_factory.
_factory = None


def system_proxy() -> str | None:
    """Прокси из окружения или системных настроек Windows (снимок при запуске)."""
    return normalize(_SYSTEM_PROXIES.get('https') or _SYSTEM_PROXIES.get('http'))


def normalize(url: str | None) -> str | None:
    """«127.0.0.1:7897» → «http://127.0.0.1:7897»: схему пишут далеко не всегда."""
    url = (url or '').strip()
    if not url:
        return None
    if '://' not in url:
        url = 'http://' + url
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    return urlunparse(parsed)


def build_url(url: str | None, user: str = '', password: str = '') -> str | None:
    """Собрать адрес с логином и паролем: «host:port» + логин → «http://user:pass@host:port».

    Логин и пароль кодируем: символы вроде @ и : в пароле иначе разорвали бы адрес,
    а платные прокси такие пароли выдают постоянно."""
    base = normalize(url)
    if not base:
        return None
    parsed = urlparse(base)
    user = (user or '').strip() or (parsed.username or '')
    if not user:
        return base
    # Пробелы по краям обрезаем и здесь, а не только в окне настроек: в уже сохранённых
    # настройках мог остаться пароль, вставленный из буфера вместе с пробелом, - прокси
    # такой не принимает и отвечает 407 при «всё введено верно».
    password = (password or parsed.password or '').strip()
    host = parsed.hostname or ''
    port = f':{parsed.port}' if parsed.port else ''
    credentials = f'{quote(user, safe="")}:{quote(password, safe="")}'
    return urlunparse(parsed._replace(netloc=f'{credentials}@{host}{port}'))


def credentials() -> tuple[str, str]:
    """Логин и пароль текущего прокси - для тех, кто не понимает user:pass в адресе
    (Chromium в окне входа VK спрашивает их отдельным диалогом).

    Когда работаем через посредник, отдавать нечего: браузер ходит на 127.0.0.1, а
    логин с паролем подставляет сам посредник."""
    if _state['local']:
        return '', ''
    parsed = urlparse(_state['url'] or '')
    if not parsed.username:
        return '', ''
    return unquote(parsed.username), unquote(parsed.password or '')


def _without_credentials(url: str) -> str:
    """Chromium в --proxy-server логин с паролем не принимает - их он спросит потом."""
    parsed = urlparse(url)
    if not parsed.username:
        return url
    port = f':{parsed.port}' if parsed.port else ''
    return urlunparse(parsed._replace(netloc=f'{parsed.hostname or ""}{port}'))


def safe(url: str | None) -> str:
    """Адрес для показа и логов: логин с паролем в прокси - тоже секрет."""
    if not url:
        return 'прямое подключение'
    parsed = urlparse(url)
    port = f':{parsed.port}' if parsed.port else ''
    user = '…@' if parsed.username else ''
    return f'{parsed.scheme}://{user}{parsed.hostname or ""}{port}'


def _port_open(port: int, timeout: float = 0.2) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def _works(url: str | None, timeout: float) -> bool:
    session = requests.Session()
    session.trust_env = False
    if url:
        session.proxies = {'http': url, 'https': url}
    try:
        session.get(_PROBE_URL, timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return False
    return True


def detect_local(timeout: float = 5.0) -> str | None:
    """Поиск запущенного VPN/прокси-клиента. Ходит в сеть - только в фоновом потоке.

    Мало проверить, что порт занят: занять его может что угодно. Поэтому кандидата
    пробуем делом - иначе авто-режим сломал бы работавшее прямое подключение."""
    for port in _LOCAL_PORTS:
        if not _port_open(port):
            continue
        url = f'http://127.0.0.1:{port}'
        if _works(url, timeout):
            logger.info('Прокси: найден локальный клиент на %s', safe(url))
            return url
        logger.debug('Прокси: порт %s занят, но интернет через него не работает', port)
    return None


def apply(settings: dict) -> str | None:
    """Применить настройку. Быстрая часть: в сеть не ходим (см. detect_local)."""
    mode = settings.get('proxy_mode', MODE_AUTO)
    if mode == MODE_MANUAL:
        url = build_url(settings.get('proxy_url'),
                        settings.get('proxy_user', ''), settings.get('proxy_pass', ''))
    elif mode == MODE_OFF:
        url = None
    else:
        mode = MODE_AUTO
        url = system_proxy()
    _set(mode, url, bool(settings.get('proxy_fragment', False)))
    return url


def set_detected(url: str | None) -> None:
    """Результат фонового поиска. Применяем, только если авто-режим ещё в силе и
    прокси до сих пор не выбран: пока шёл поиск, настройку могли переключить."""
    if url and needs_detect():
        _set(MODE_AUTO, url, _state['fragment'])


def needs_detect() -> bool:
    return _state['mode'] == MODE_AUTO and not _state['url']


def _set(mode: str, url: str | None, fragment: bool = False) -> None:
    _state['mode'] = mode
    _state['url'] = url
    _state['fragment'] = fragment
    # Посредник поднимаем заново на каждое применение настроек: за ним мог смениться
    # и сам прокси, а перенастраивать работающий сложнее, чем поднять новый.
    _state['local'] = frag_proxy.start(url) if fragment else None
    if not fragment:
        frag_proxy.stop()
    _apply_env(mode, effective())
    _apply_webengine_flags(mode, effective())
    _apply_qt_proxy(mode, effective())
    logger.debug('Прокси: режим %s, адрес %s, обход блокировки %s',
                 mode, safe(url), 'включён' if _state['local'] else 'выключен')


def no_proxy_value() -> str:
    """Список доменов мимо прокси в том виде, в каком его понимают requests,
    urllib и yt-dlp: имена через запятую."""
    return ','.join(VK_DIRECT_DOMAINS)


def bypasses_proxy(url: str) -> bool:
    """Идёт ли этот адрес мимо прокси. Сравниваем по имени узла, а не по вхождению
    строки: «notvk.com» не должен считаться доменом VK."""
    host = (urlparse(url).hostname or '').lower().rstrip('.')
    return any(host == d or host.endswith('.' + d) for d in VK_DIRECT_DOMAINS)


def _apply_env(mode: str, url: str | None) -> None:
    """Переменные окружения нужны тем частям, куда опции не передашь: urllib
    (загрузка JS-движка) и вложенные процессы."""
    for name in _ENV_VARS:
        os.environ.pop(name, None)
    os.environ.pop('NO_PROXY', None)
    if url:
        os.environ['HTTP_PROXY'] = os.environ['HTTPS_PROXY'] = url
        os.environ['NO_PROXY'] = no_proxy_value()
    elif mode == MODE_OFF:
        # «Без прокси» должно отменять и системные настройки, а не только наши
        os.environ['NO_PROXY'] = '*'


def _apply_webengine_flags(mode: str, url: str | None) -> None:
    """Окно входа в VK - Chromium внутри QtWebEngine, свои настройки он берёт из
    этой переменной при первом запуске движка."""
    keep = [f for f in os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '').split()
            if not f.startswith('--proxy-server=')
            and not f.startswith('--proxy-bypass-list=')
            and f != '--no-proxy-server']
    if url:
        keep.append(f'--proxy-server={_without_credentials(url)}')
        # Само окно входа VK обязано идти напрямую, иначе VK видит вход из-за
        # границы. Домен пишем дважды: «*.vk.com» покрывает поддомены, но не сам
        # «vk.com», а вход открывается как раз на нём.
        bypass = ';'.join(part for d in VK_DIRECT_DOMAINS for part in (d, f'*.{d}'))
        keep.append(f'--proxy-bypass-list={bypass}')
    elif mode == MODE_OFF:
        keep.append('--no-proxy-server')
    if keep:
        os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = ' '.join(keep)
    else:
        os.environ.pop('QTWEBENGINE_CHROMIUM_FLAGS', None)


def _vk_direct_factory(proxy):
    """Фабрика выбора прокси для Qt: VK - напрямую, остальное - как задано.

    Фабрика на приложение всегда одна и живёт до его конца: меняем в ней адрес,
    а саму не пересоздаём. Причина - двойное освобождение. Владение объектом при
    установке остаётся за Python (`ownedByPython` так и остаётся истиной), но
    прежнюю фабрику `setApplicationProxyFactory` удаляет по-своему, и второй
    вызов рушил процесс с повреждением кучи. А второй вызов бывает всегда:
    сначала настройку применяют на старте, потом ещё раз - когда фоновый поиск
    находит локальный прокси-клиент.

    Класс объявлен внутри функции, а не рядом с модулем: наследовать
    QNetworkProxyFactory можно только после импорта PySide6, а тянуть Qt в модуль
    настроек не хочется - им пользуются и тесты, и консольные части."""
    from PySide6.QtNetwork import QNetworkProxy, QNetworkProxyFactory

    class _Factory(QNetworkProxyFactory):
        """Выбор прокси по адресу запроса.

        Одной `setApplicationProxy` тут не хватает: она действует на всё приложение
        разом, а окно входа VK обязано идти со своего адреса. Ключ
        `--proxy-bypass-list` решает это только для Chromium и только при старте
        движка, поэтому исключение держим и здесь - на случай смены прокси уже
        после запуска."""

        def __init__(self) -> None:
            super().__init__()
            self._direct = QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
            self._proxy = self._direct

        def queryProxy(self, query):
            host = (query.peerHostName() or '').lower().rstrip('.')
            if any(host == d or host.endswith('.' + d) for d in VK_DIRECT_DOMAINS):
                return [self._direct]
            return [self._proxy]

    global _factory
    if _factory is None:
        _factory = _Factory()
        QNetworkProxyFactory.setApplicationProxyFactory(_factory)
    _factory._proxy = proxy
    return _factory


def _apply_qt_proxy(mode: str, url: str | None) -> None:
    """То же самое для уже запущенного QtWebEngine.

    Ключ `--proxy-server` Chromium читает один раз, при старте движка, а движок
    поднимается вместе с окном - раньше, чем заканчивается фоновый поиск прокси.
    Настройку приложения Qt он, в отличие от ключа, перечитывает на ходу (проверено
    на живом движке), поэтому смену адреса доносим ею."""
    from PySide6.QtNetwork import QNetworkProxy, QNetworkProxyFactory

    if url:
        parsed = urlparse(url)
        proxy = QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy,
                              parsed.hostname or '127.0.0.1', parsed.port or 8080)
    elif mode == MODE_OFF:
        proxy = QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
    else:
        # «Как в системе»: своего адреса нет, пусть Chromium решает сам
        proxy = QNetworkProxy(QNetworkProxy.ProxyType.DefaultProxy)
    # Фабрику не переустанавливаем и не снимаем - она ставится однажды и дальше
    # только получает новый адрес (см. _vk_direct_factory). Пары к ней в виде
    # `setApplicationProxy` быть не должно: этот вызов отключает фабрику, и после
    # возврата из «без прокси» в «свой адрес» весь трафик молча шёл бы напрямую.
    _vk_direct_factory(proxy)


def current() -> str | None:
    return _state['url']


def effective() -> str | None:
    """Адрес, который получают yt-dlp, requests и браузер. При включённом обходе это
    посредник на 127.0.0.1, а выбранный прокси прячется за ним."""
    return _state['local'] or _state['url']


def describe() -> str:
    bypass = ' + обход блокировки видеосерверов' if _state['local'] else ''
    if _state['mode'] == MODE_OFF:
        return f'Сейчас: без прокси, прямое подключение{bypass}'
    if _state['url']:
        source = 'вручную' if _state['mode'] == MODE_MANUAL else 'найден автоматически'
        return f'Сейчас: {safe(_state["url"])} ({source}){bypass}'
    return f'Сейчас: прямое подключение или системный VPN/TUN{bypass}'


def ytdlp_opts() -> dict:
    """Опции для yt-dlp. Пустая строка у него означает именно «без прокси», а
    отсутствие ключа - «взять системные настройки»."""
    url = effective()
    if url:
        return {'proxy': url}
    if _state['mode'] == MODE_OFF:
        return {'proxy': ''}
    return {}


def apply_to_session(session: requests.Session) -> None:
    """Прокси для requests. Их можно задать и окружением, но явная настройка не
    зависит от того, кто и когда правил os.environ.

    Домены VK при этом обязаны идти напрямую (см. VK_DIRECT_DOMAINS), и вот тут
    у requests ловушка: ключ `no_proxy` внутри `session.proxies` не значит ничего.
    Обход requests умеет читать только из окружения, а словарь сопоставляет по
    схеме - `https` находится, `no_proxy` молча пропускается. Замер: с таким
    словарём `https://m.vk.ru/audio` уходил на `http://127.0.0.1:7897`, из-за чего
    VK отвечал страницей входа и программа считала сессию потерянной на живом входе.

    Точечные ключи вида `https://vk.ru` тоже не спасают: они требуют перечислить
    каждый поддомен поимённо, а `api.vk.ru`, `m.vk.ru` и прочие заранее неизвестны.
    Поэтому решаем там, где адрес уже на руках, - в самом запросе."""
    url = effective()
    if url:
        session.proxies = {'http': url, 'https': url}
        _teach_session_to_skip_vk(session)
    elif _state['mode'] == MODE_OFF:
        session.trust_env = False
        session.proxies = {}


def _teach_session_to_skip_vk(session: requests.Session) -> None:
    """Пустить запросы к VK мимо прокси, остальные - как задано.

    Подменяем `request` один раз на сессию: повторный вызов `apply_to_session`
    (адрес прокси меняется, когда фоновый поиск находит локальный клиент) не должен
    наматывать обёртку на обёртку."""
    if getattr(session, '_vk_direct', False):
        return
    session._vk_direct = True
    original = session.request

    def request(method, url, *args, **kwargs):
        if bypasses_proxy(url):
            # None по обеим схемам - это именно «напрямую», а не «возьми
            # системные»: пустой словарь requests дополнил бы из окружения
            kwargs.setdefault('proxies', {'http': None, 'https': None})
        return original(method, url, *args, **kwargs)

    session.request = request


def check(mode: str, manual_url: str = '', user: str = '', password: str = '',
          fragment: bool = False, timeout: float = 8.0) -> tuple[bool, str]:
    """Проверка связи для окна настроек: что именно доступно через выбранный прокси.

    Проверяем ровно ту связку, которая получится после сохранения, - вместе с обходом
    блокировки, если галочка стоит. Иначе окно показывало бы одно, а качалось бы другое."""
    if mode == MODE_MANUAL:
        url = build_url(manual_url, user, password)
        if not url:
            return False, 'Адрес прокси не указан.'
    elif mode == MODE_OFF:
        url = None
    else:
        url = system_proxy() or detect_local(timeout=timeout)

    helper = frag_proxy.FragProxy(url) if fragment else None
    try:
        session = requests.Session()
        session.trust_env = False
        probe_url = _start_helper(helper) or url
        if probe_url:
            # VK проверяем так же, как к нему потом ходим, - мимо прокси. Иначе кнопка
            # отчитывалась бы об адресе, которым VK никогда не пользуется.
            session.proxies = {'http': probe_url, 'https': probe_url,
                               'no_proxy': no_proxy_value()}

        bypass = ' с обходом блокировки' if helper and helper.url else ''
        lines = [f'Проверено через {safe(url)}{bypass}:']
        failed, kinds = [], set()
        for name, target in _CHECK_TARGETS:
            try:
                # Любой ответ означает, что соединение и TLS прошли - код здесь не важен
                session.get(target, timeout=timeout, allow_redirects=False)
            except requests.RequestException as exc:
                kind, reason = _describe_error(exc)
                failed.append(name)
                kinds.add(kind)
                lines.append(f'  ✕ {name}: {reason}')
            else:
                lines.append(f'  ✓ {name}')

        if not failed:
            lines.append('Прокси работает, можно закрывать настройки и качать.' if url
                         else 'Всё доступно напрямую, прокси не нужен.')
        else:
            # Помогает ли обход, выясняем делом: советовать галочку наугад - значит
            # гонять пользователя по настройкам без толку.
            helps = (not fragment and _VIDEO_TARGET in failed and 'auth' not in kinds
                     and _fragment_helps(url, timeout))
            sni_blocked = _VIDEO_TARGET in failed and _video_http_alive(session, timeout)
            lines.append(_advice(failed, kinds, url, sni_blocked, helps, fragment))
        return not failed, '\n'.join(lines)
    finally:
        if helper is not None:
            helper.stop()


def _start_helper(helper: 'frag_proxy.FragProxy | None') -> str | None:
    if helper is None:
        return None
    try:
        return helper.start()
    except OSError as exc:
        logger.warning('Проверка: посредник не запустился (%s)', exc)
        return None


def _fragment_helps(url: str | None, timeout: float) -> bool:
    """Открываются ли видеосерверы, если первый пакет резать на части."""
    helper = frag_proxy.FragProxy(url)
    try:
        local = helper.start()
    except OSError:
        return False
    try:
        session = requests.Session()
        session.trust_env = False
        session.proxies = {'http': local, 'https': local}
        session.get(_CHECK_TARGETS[1][1], timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return False
    else:
        return True
    finally:
        helper.stop()


def _video_http_alive(session: requests.Session, timeout: float) -> bool:
    """Отвечает ли видеосервер по обычному HTTP, когда по HTTPS не отвечал.

    Разница важнее, чем кажется: если открытый порт отвечает, а защищённый молчит, то
    маршрут до сервера есть и рвётся именно TLS - так выглядит блокировка по имени сайта.
    Совет для этого случая совсем другой, чем при «нет маршрута»."""
    try:
        session.get(_VIDEO_HTTP_TARGET, timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return False
    return True


def _advice(failed: list[str], kinds: set[str], url: str | None,
            sni_blocked: bool = False, fragment_helps: bool = False,
            fragment_on: bool = False) -> str:
    """Совет по виду сбоя. Без него в окне остаётся английская строка от requests,
    по которой непонятно, что чинить - прокси, его пароль или настройки клиента."""
    if fragment_helps:
        return ('Видеосерверы закрыты по имени сайта, но обход помогает: поставьте галочку '
                '«Обходить блокировку видеосерверов» ниже и сохраните настройки: '
                'приложение будет отправлять первый пакет по частям, и файлы пойдут.')
    if fragment_on and _VIDEO_TARGET in failed:
        return ('Обход блокировки включён, но видеосерверы всё равно не отвечают, значит '
                'дело не в имени сайта. Проверьте, работает ли сам прокси, и попробуйте '
                'другой либо шифрованный туннель (VLESS, Shadowsocks, WireGuard).')
    if 'auth' in kinds:
        return ('Логин и пароль до прокси дошли, но он их не принял. Проверьте, не попал ли '
                'в поля лишний пробел, не закончился ли доступ и не выдан ли прокси по '
                'белому списку IP: тогда логин с паролем не нужны, а нужен ваш адрес в списке.')
    if sni_blocked:
        return ('Видеосервер отвечает по открытому HTTP, а защищённое соединение к нему '
                'рвётся: так выглядит блокировка по имени сайта. Обычный HTTP- или '
                'SOCKS-прокси от неё не спасает: имя googlevideo.com он передаёт открытым '
                'текстом, и правило вида DOMAIN-SUFFIX,googlevideo.com,PROXY тоже, если '
                'сервер в клиенте не шифрованный. Помогает шифрованный туннель (VLESS, '
                'Shadowsocks, WireGuard) либо галочка «Обходить блокировку видеосерверов» '
                'ниже, если с ней проверка станет зелёной.')
    if failed == [_VIDEO_TARGET]:
        return ('Сайт YouTube открывается, а файлы качаются с *.googlevideo.com: закрыты '
                'именно они. В своём клиенте включите режим TUN или Global либо добавьте '
                'правило DOMAIN-SUFFIX,googlevideo.com,PROXY. Либо укажите здесь другой '
                'прокси, через него пойдёт всё сразу.')
    if 'proxy' in kinds:
        return ('До прокси не удалось достучаться: проверьте адрес и порт, а также что '
                'клиент запущен.' if url else 'Соединение не устанавливается.')
    if len(failed) == len(_CHECK_TARGETS):
        return ('Не отвечает ничего: похоже, дело не в отдельном сервисе, а в самом '
                'подключении: проверьте интернет, адрес прокси и режим клиента.')
    return ('Если недоступны видеосерверы, скачивание с YouTube работать не будет: '
            'включите VPN или укажите другой прокси.')


def _describe_error(exc: requests.RequestException) -> tuple[str, str]:
    """Вид сбоя и человеческая формулировка: от вида зависит, какой совет показать."""
    text = str(exc)
    if '407' in text or 'Proxy Authentication' in text:
        return 'auth', 'прокси не принял логин и пароль (407)'
    if isinstance(exc, requests.exceptions.ProxyError):
        return 'proxy', f'прокси недоступен ({_short_error(exc)})'
    if isinstance(exc, requests.exceptions.Timeout):
        return 'timeout', 'нет ответа за отведённое время'
    if isinstance(exc, requests.exceptions.SSLError):
        return 'tls', 'соединение оборвано при установке TLS'
    return 'other', f'не отвечает ({_short_error(exc)})'


def _short_error(exc: Exception) -> str:
    """Сообщения requests - цепочка вложенных исключений на несколько строк."""
    text = str(exc).strip()
    tail = text.split(': ')[-1].strip('\'")( ')
    return (tail or text)[:90]
