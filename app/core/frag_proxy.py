"""Локальный посредник, разрезающий первый пакет защищённого соединения.

Зачем он нужен. Файлы YouTube лежат на *.googlevideo.com, и у части провайдеров
соединения именно к этим именам обрываются: имя сайта едет в первом же пакете
(ClientHello) открытым текстом, даже когда соединение идёт через HTTP-прокси -
в «CONNECT host:443» имя тоже видно. Фильтр читает его и рвёт связь. Обычный
платный прокси от этого не спасает, спасает только шифрованный туннель.

Обход простой: тот же самый первый пакет отправить не одним куском, а несколькими.
Фильтр разбирает только первый кусок, целого имени в нём нет, и соединение живёт.
Замеры на живой блокировке: без разрезания 0 успешных попыток из 6, с разрезанием
по 64 байта - 6 из 6, рукопожатие за 0.75 с.

Посредник - обычный HTTP-прокси на 127.0.0.1, поэтому его понимают и yt-dlp, и
requests, и встроенный браузер: подменять им сетевой слой не нужно. Сам он ходит
либо напрямую, либо через вышестоящий прокси (тогда логин с паролем подставляет
он, а приложению остаётся адрес без секретов).
"""

import base64
import logging
import socket
import threading
import time
from urllib.parse import urlparse, unquote

logger = logging.getLogger(__name__)

# Размер куска подобран замерами (см. выше). На нагрузку он не влияет: 64, 32 и 16
# байт дают одинаковый результат, поэтому берём самый крупный - меньше отправок.
_CHUNK = 64
# Пауза между кусками. Одиночному соединению она не нужна, но когда yt-dlp открывает
# десяток соединений разом, без неё выживает половина: 20 параллельных запросов к
# googlevideo без паузы - 10 и 5 успешных из 20, с паузой 0.005 - 20 из 20 за 1.8 с.
# Больше 0.005 ничего не улучшает, поэтому оставляем минимум.
_DELAY = 0.005
_BUFFER = 65536
_TIMEOUT = 20.0
# Начало записи TLS handshake: 0x16 - тип записи, 0x03 - версия. Режем только такие
# пакеты, обычным HTTP-запросам разрезание ни к чему.
_TLS_HANDSHAKE = b'\x16\x03'
_DEFAULT_PORTS = {'http': 80, 'https': 443}


class FragProxy:
    """HTTP-прокси на localhost, режущий ClientHello на куски.

    Отдельным классом, а не одной функцией: кроме постоянного посредника такой же
    нужен проверке связи - на минуту, поверх ещё не сохранённых настроек."""

    def __init__(self, upstream: str | None = None) -> None:
        self._upstream = urlparse(upstream) if upstream else None
        self._auth = self._upstream_auth()
        self._server: socket.socket | None = None
        self.url: str | None = None

    def set_upstream(self, upstream: str | None) -> None:
        """Сменить вышестоящий прокси, не трогая слушающий порт.

        Порт посредника уже роздан наружу (Chromium читает его один раз при запуске
        движка), поэтому перезапуск на новом порту оставил бы браузер без сети.
        Уже открытые соединения доработают на прежнем адресе - это на один запрос."""
        self._upstream = urlparse(upstream) if upstream else None
        self._auth = self._upstream_auth()

    def start(self) -> str:
        """Занять свободный порт и начать принимать соединения. Порт выбирает система:
        фиксированный мог бы оказаться занятым чужой программой."""
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        server.listen(64)
        self._server = server
        self.url = f'http://127.0.0.1:{server.getsockname()[1]}'
        threading.Thread(target=self._accept_loop, args=(server,), daemon=True).start()
        return self.url

    def stop(self) -> None:
        """Закрыть слушающий сокет. Уже открытые соединения дорабатывают сами:
        обрывать чужую загрузку на полуслове хуже, чем подождать."""
        server, self._server = self._server, None
        self.url = None
        if server:
            try:
                server.close()
            except OSError:
                pass

    # ---------- приём соединений ----------

    def _accept_loop(self, server: socket.socket) -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return  # сокет закрыли в stop() - это штатное завершение
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        remote = None
        try:
            head, rest = _read_head(client)
            if not head:
                return
            method, target = (head.split('\r\n')[0].split() + ['', ''])[:2]
            if method.upper() == 'CONNECT':
                host, _, port = target.partition(':')
                remote = self._connect(host, int(port or 443), tunnel=True)
                if remote is None:
                    client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                    return
                client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                first = rest
            else:
                remote, first = self._plain(head, rest, target)
                if remote is None:
                    client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                    return
            # Резать имеет смысл только то, что уходит от клиента наружу: ClientHello
            # там. Обратный поток отправляем как есть.
            threading.Thread(target=_pump, args=(client, remote, first, True),
                             daemon=True).start()
            _pump(remote, client, b'', False)
        except OSError:
            pass
        finally:
            for sock in (client, remote):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    # ---------- соединение с целью ----------

    def _connect(self, host: str, port: int, tunnel: bool) -> socket.socket | None:
        """Соединение до цели: через вышестоящий прокси, если он задан, иначе напрямую."""
        upstream = self._upstream  # снимок: адрес могут сменить прямо во время соединения
        try:
            if not upstream:
                return _open(host, port)
            remote = _open(upstream.hostname or '', upstream.port or 8080)
        except OSError as exc:
            logger.debug('Обход блокировки: не удалось соединиться (%s)', exc)
            return None
        if not tunnel:
            return remote  # обычный HTTP прокси примет запрос целиком, туннель не нужен
        try:
            remote.sendall(_connect_request(host, port, self._auth))
            answer, _ = _read_head(remote)
            if ' 200 ' not in answer.split('\r\n')[0]:
                logger.warning('Обход блокировки: вышестоящий прокси отказал (%s)',
                               answer.split('\r\n')[0][:60])
                remote.close()
                return None
        except OSError as exc:
            logger.debug('Обход блокировки: туннель не открылся (%s)', exc)
            remote.close()
            return None
        return remote

    def _plain(self, head: str, rest: bytes, target: str) -> tuple[socket.socket | None, bytes]:
        """Обычный (не защищённый) запрос. Через вышестоящий прокси он идёт как есть,
        напрямую - с адресом, укороченным до пути: так требует сам сервер."""
        if self._upstream:
            if self._auth:
                head += f'\r\nProxy-Authorization: Basic {self._auth}'
            return self._connect('', 0, tunnel=False), head.encode('latin1') + b'\r\n\r\n' + rest
        parsed = urlparse(target)
        host = parsed.hostname
        if not host:
            return None, b''
        path = parsed.path or '/'
        if parsed.query:
            path += f'?{parsed.query}'
        lines = head.split('\r\n')
        lines[0] = f'{lines[0].split()[0]} {path} {lines[0].split()[-1]}'
        port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme, 80)
        try:
            remote = _open(host, port)
        except OSError:
            return None, b''
        return remote, '\r\n'.join(lines).encode('latin1') + b'\r\n\r\n' + rest

    def _upstream_auth(self) -> str:
        """Логин и пароль вышестоящего прокси в виде готового заголовка.

        Держим их здесь, чтобы наружу отдавать адрес посредника без секретов: в него
        ходят yt-dlp и встроенный браузер, и в их логи попадать паролю незачем."""
        if not self._upstream or not self._upstream.username:
            return ''
        user = unquote(self._upstream.username)
        password = unquote(self._upstream.password or '')
        return base64.b64encode(f'{user}:{password}'.encode()).decode()


# ---------- служебные функции ----------


def _open(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=_TIMEOUT)
    # Без этого система вправе склеить куски обратно в один пакет - и всё разрезание
    # окажется бессмысленным.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(_TIMEOUT)
    return sock


def _connect_request(host: str, port: int, auth: str) -> bytes:
    lines = [f'CONNECT {host}:{port} HTTP/1.1', f'Host: {host}:{port}']
    if auth:
        lines.append(f'Proxy-Authorization: Basic {auth}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin1')


def _read_head(sock: socket.socket) -> tuple[str, bytes]:
    """Заголовок запроса или ответа целиком плюс всё, что пришло следом."""
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(_BUFFER)
        if not chunk:
            return '', b''
        data += chunk
        if len(data) > 64 * 1024:  # заголовок такого размера - уже не запрос
            return '', b''
    head, _, rest = data.partition(b'\r\n\r\n')
    return head.decode('latin1'), rest


def _pump(src: socket.socket, dst: socket.socket, initial: bytes, split_first: bool) -> None:
    """Перекачка в одну сторону. Первый пакет с рукопожатием TLS уходит частями."""
    data, split = initial, split_first
    try:
        while True:
            if data:
                if split and data.startswith(_TLS_HANDSHAKE):
                    for i in range(0, len(data), _CHUNK):
                        dst.sendall(data[i:i + _CHUNK])
                        if _DELAY:
                            time.sleep(_DELAY)
                else:
                    dst.sendall(data)
                split = False
            data = src.recv(_BUFFER)
            if not data:
                return
    except OSError:
        return
    finally:
        # Закрываем встречное направление, иначе вторая перекачка будет ждать до таймаута
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ---------- постоянный посредник приложения ----------

_current: FragProxy | None = None


def start(upstream: str | None) -> str | None:
    """Поднять посредник поверх указанного прокси и вернуть его адрес.

    Уже работающий не перезапускаем, а перенацеливаем: его порт держит у себя
    Chromium, и смена порта оставила бы встроенный браузер без сети.

    Не получилось - возвращаем None: обход дело полезное, но ронять из-за него
    приложение нельзя, лучше работать без него."""
    global _current
    if _current is not None and _current.url:
        _current.set_upstream(upstream)
        logger.info('Обход блокировки: посредник на %s переключён на новый прокси',
                    _current.url)
        return _current.url
    stop()
    proxy = FragProxy(upstream)
    try:
        url = proxy.start()
    except OSError as exc:
        logger.warning('Обход блокировки: посредник не запустился (%s)', exc)
        return None
    _current = proxy
    logger.info('Обход блокировки: посредник слушает %s', url)
    return url


def stop() -> None:
    global _current
    if _current is not None:
        _current.stop()
        _current = None
