"""Мост между расширением браузера и приложением.

Слушает только 127.0.0.1: наружу порт не выставляется никогда. Расширение умеет
ровно то, что перечислено в ACTIONS, и присылает только ссылку - ни путей к файлам,
ни команд оболочки мост не принимает и не выполняет. Токен VK и прочие секреты
остаются в приложении, расширению они не нужны и не передаются.

Модуль сознательно не знает про интерфейс: он лишь превращает разрешённый запрос
в сигнал command, а что с ним делать - решает окно.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

HOST = '127.0.0.1'
# Занятый порт - обычная ситуация (второй экземпляр, чужая программа), поэтому
# пробуем несколько подряд; расширение перебирает тот же диапазон
PORT_ATTEMPTS = 5
MAX_BODY = 8 * 1024
ACTIONS = ('status', 'open', 'play', 'enqueue', 'add_to_vk')

ALLOWED_HOSTS = {
    'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
    'youtu.be', 'www.youtu.be',
    'vk.com', 'www.vk.com', 'm.vk.com', 'vk.ru', 'www.vk.ru',
}
# Запросы принимаем от расширений браузера. Обычная веб-страница присылает
# http(s)-Origin - такие запросы отбиваем, иначе любой сайт мог бы дёргать мост
EXTENSION_SCHEMES = ('chrome-extension', 'moz-extension', 'extension',
                     'safari-web-extension')


def new_token() -> str:
    """Секрет для расширения. Показывается в настройках, в лог не пишется."""
    return secrets.token_urlsafe(24)


def check_url(value) -> str:
    """Ссылка, которую можно принять от расширения, или пустая строка.

    Разрешены только http/https и знакомые хосты: локальные пути и любые другие
    схемы (file:, data:) отсекаются здесь, а не где-то дальше по коду."""
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ''
    if parsed.scheme not in ('http', 'https'):
        return ''
    host = (parsed.hostname or '').lower()
    return value.strip() if host in ALLOWED_HOSTS else ''


class _Handler(BaseHTTPRequestHandler):
    server_version = 'MusicHubBridge'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    # ---------- служебное ----------
    def log_message(self, fmt, *args) -> None:
        # Стандартный обработчик пишет прямо в stderr; нам хватит отладочного лога.
        # В строке запроса может оказаться секрет, поэтому её не выводим
        logger.debug('bridge: запрос обработан')

    @property
    def _bridge(self):
        return self.server.bridge

    def _origin_ok(self) -> bool:
        origin = self.headers.get('Origin')
        if not origin:
            return True  # проверка curl-ом или наш собственный код
        return urlparse(origin).scheme in EXTENSION_SCHEMES

    def _token_ok(self) -> bool:
        token = self.headers.get('X-Auth-Token', '')
        return bool(self._bridge.token) and secrets.compare_digest(token, self._bridge.token)

    def _cors(self) -> None:
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).scheme in EXTENSION_SCHEMES:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Auth-Token')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        if not self._origin_ok():
            self._reply(403, {'ok': False, 'error': 'forbidden origin'})
            return False
        if not self._token_ok():
            self._reply(401, {'ok': False, 'error': 'bad token'})
            return False
        return True

    # ---------- запросы ----------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path != '/status':
            self._reply(404, {'ok': False, 'error': 'unknown endpoint'})
            return
        if self._allowed():
            self._reply(200, dict({'ok': True}, **self._bridge.status()))

    def do_POST(self) -> None:
        if urlparse(self.path).path != '/command':
            self._reply(404, {'ok': False, 'error': 'unknown endpoint'})
            return
        if not self._allowed():
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._reply(413, {'ok': False, 'error': 'body too large'})
            return
        try:
            data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, {'ok': False, 'error': 'bad json'})
            return
        if not isinstance(data, dict):
            self._reply(400, {'ok': False, 'error': 'bad json'})
            return

        action = data.get('action')
        if action not in ACTIONS:
            # Белый список: ничего, кроме перечисленного, мост выполнить не может
            self._reply(400, {'ok': False, 'error': 'unknown action'})
            return
        if action == 'status':
            self._reply(200, dict({'ok': True}, **self._bridge.status()))
            return

        url = check_url(data.get('url'))
        if not url:
            self._reply(400, {'ok': False, 'error': 'unsupported url'})
            return
        title = data.get('title')
        payload = {'url': url, 'title': title[:300] if isinstance(title, str) else ''}
        self._bridge.dispatch(action, payload)
        self._reply(200, {'ok': True, 'action': action})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # чужой порт лучше не перехватывать

    def __init__(self, address, handler, bridge):
        super().__init__(address, handler)
        self.bridge = bridge


class BridgeServer(QObject):
    """HTTP-мост на 127.0.0.1. Запрос приходит в чужом потоке, наружу идёт сигналом."""

    command = Signal(str, dict)  # действие из ACTIONS, проверенные данные

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self._status: dict = {'playing': False, 'track': None}
        self.token = ''
        self.port = 0

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, port: int = 48211, token: str = '') -> int:
        """Поднимает мост и возвращает занятый порт (0 - не удалось)."""
        if self._server is not None:
            return self.port
        self.token = token or new_token()
        first = port if 1024 < port < 65535 else 48211
        for candidate in range(first, first + PORT_ATTEMPTS):
            try:
                self._server = _Server((HOST, candidate), _Handler, self)
            except OSError:
                continue
            self.port = candidate
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            name='bridge', daemon=True)
            self._thread.start()
            logger.info('Мост для расширения слушает %s:%s', HOST, candidate)
            return candidate
        logger.warning('Мост не запущен: порты %s-%s заняты', first,
                       first + PORT_ATTEMPTS - 1)
        return 0

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self.port = 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)

    # ---------- обмен с интерфейсом ----------
    def set_status(self, status: dict) -> None:
        """Снимок «что играет»: копия нужна, чтобы не трогать плеер из чужого потока."""
        with self._lock:
            self._status = dict(status)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def dispatch(self, action: str, payload: dict) -> None:
        self.command.emit(action, payload)
