"""Обложки треков: качаем в фоне, храним на диске и в памяти.

Списки VK и результаты поиска YouTube — это сотни картинок. Тянуть их в потоке
интерфейса нельзя (окно замрёт), качать каждый раз заново — тоже: одна и та же
обложка возвращается при каждой прокрутке. Поэтому здесь маленький общий кэш."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap

from .. import config
from ..core import proxy
from ..core.async_task import run_async
from . import player_icons, theme

logger = logging.getLogger(__name__)

CACHE_DIR = config.CONFIG_DIR / 'covers'
_MEMORY_LIMIT = 200
_MAX_BYTES = 4 * 1024 * 1024
# Сколько обложек держим на диске. Больше — просто мусор в профиле
_DISK_LIMIT = 150 * 1024 * 1024

# Сколько обложек тянем одновременно. Пул потоков общий на всё приложение (8 мест),
# а прокрутка списка просит до трёх десятков картинок разом — без ограничения они
# занимали все места, и воспроизведение с проверкой сессии ждали в очереди
_PARALLEL = 4
# Через сколько можно снова попробовать ссылку, которая не открылась. Насовсем
# помечать нельзя: почти все отказы — таймауты, а не мёртвые адреса, и обложка
# пропадала до перезапуска
_RETRY_AFTER = 60.0

_memory: OrderedDict[str, QPixmap] = OrderedDict()
_pending: dict[str, list] = {}
# Ссылки, которые не открылись, и когда это случилось
_failed: dict[str, float] = {}
# Очередь ожидающих загрузок: ключ и порядок нужны, чтобы отдавать видимое первым
_queue: list[str] = []
_running = 0


def _key(url: str) -> str:
    return hashlib.sha1(url.encode('utf-8', 'ignore')).hexdigest()


def _remember(url: str, pixmap: QPixmap) -> None:
    _memory[url] = pixmap
    _memory.move_to_end(url)
    while len(_memory) > _MEMORY_LIMIT:
        _memory.popitem(last=False)


# Одна сессия на все обложки: они идут к одному-двум хостам сотнями, и своя
# сессия на каждую означала своё рукопожатие TLS на каждую. Через прокси с
# разрезанием пакета такое рукопожатие особенно дорогое — отсюда и были таймауты
_session = None
_session_proxy = ''
_session_lock = threading.Lock()


def _http():
    """Общая сессия, живущая, пока не сменится прокси."""
    global _session, _session_proxy
    import requests
    from requests.adapters import HTTPAdapter

    current = proxy.effective() or ''
    with _session_lock:
        if _session is not None and _session_proxy == current:
            return _session
        if _session is not None:
            _session.close()
        session = requests.Session()
        proxy.apply_to_session(session)
        # Держим соединения открытыми: столько же, сколько качаем разом
        adapter = HTTPAdapter(pool_connections=_PARALLEL, pool_maxsize=_PARALLEL)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        _session, _session_proxy = session, current
        return session


def reset_session() -> None:
    """Забыть сессию: адрес прокси сменился, старые соединения ведут не туда."""
    global _session, _session_proxy
    with _session_lock:
        if _session is not None:
            _session.close()
        _session, _session_proxy = None, ''


# Обложки каналов YouTube приходят с yt3.googleusercontent.com, и именно к этому
# имени соединение у части провайдеров не встаёт: рукопожатие TLS висит до самого
# срока (598 таких отказов в журнале за неделю — треть всех загрузок). Картинки
# при этом лежат на общем хранилище Google и точно так же отдаются с lh3 — тот же
# путь, тот же файл, разница только в имени. Замер на живой сети: yt3 — 0 успешных
# из 12 за 122 с, lh3 — 10 из 10 за 7.7 с
_HOST_FALLBACK = {'yt3.googleusercontent.com': 'lh3.googleusercontent.com',
                  'yt3.ggpht.com': 'lh3.googleusercontent.com'}
# Хосты, которые уже не открылись, и когда. Пока метка свежая, идём сразу на
# запасной: иначе каждая обложка в списке платит свои десять секунд ожидания
_bad_hosts: dict[str, float] = {}


def _host_is_bad(host: str) -> bool:
    failed_at = _bad_hosts.get(host)
    if failed_at is None:
        return False
    if time.monotonic() - failed_at < _RETRY_AFTER:
        return True
    del _bad_hosts[host]     # срок вышел, пробуем основной адрес заново
    return False


def _alternate(url: str) -> str | None:
    """Тот же файл под другим именем хоста, если такое имя известно."""
    for host, spare in _HOST_FALLBACK.items():
        if f'//{host}/' in url:
            return url.replace(f'//{host}/', f'//{spare}/', 1)
    return None


def _fetch(url: str, path: Path) -> bytes:
    import requests

    host = url.split('/')[2]
    spare = _alternate(url)
    if spare is not None and _host_is_bad(host):
        # Этот хост только что не открылся. Ждать от него ещё десять секунд на
        # каждой обложке в списке незачем — идём сразу на запасной
        url, spare = spare, None
    try:
        # Раздельные сроки: на соединение много не нужно, а вот сама картинка через
        # прокси с разрезанием пакета едет медленно
        response = _http().get(url, timeout=(10, 30), stream=True)
        response.raise_for_status()
    except (requests.Timeout, requests.ConnectionError):
        # Не открылось — пробуем то же самое с запасного хранилища. Сразу с него
        # ходить нельзя: основной адрес рабочий, просто не у всех
        if spare is None:
            raise
        _bad_hosts[host] = time.monotonic()
        logger.debug('Обложка: %s не отвечает, идём на запасной хост', host)
        response = _http().get(spare, timeout=(10, 30), stream=True)
        response.raise_for_status()
    data = response.content[:_MAX_BYTES]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass  # без диска обойдёмся, картинка всё равно уже в руках
    return data


def rounded(pixmap: QPixmap, size: int, radius: int = 8) -> QPixmap:
    """Квадрат нужного размера со скруглёнными углами — как в списках приложения."""
    scaled = pixmap.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                           Qt.SmoothTransformation)
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    scaled = scaled.copy(x, y, size, size)

    result = QPixmap(size, size)
    result.fill(QColor(0, 0, 0, 0))
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return result


def placeholder(size: int, radius: int = 8) -> QPixmap:
    """Заглушка вместо обложки: приглушённый прямоугольник с нотой."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.fillPath(path, QColor(theme.color('raised')))
    # Ноту рисуем вектором, а не знаком «♪»: подходящего шрифта в системе может
    # не оказаться, и вместо ноты встаёт пустой квадрат
    glyph = max(12, size // 3)
    offset = (size - glyph) // 2
    player_icons.draw('audio', theme.color('text_mute'), glyph).paint(
        painter, offset, offset, glyph, glyph)
    painter.end()
    return pixmap


def cached(url: str) -> QPixmap | None:
    """Готовая обложка, если она уже в памяти. Сеть не трогает."""
    if not url:
        return None
    pixmap = _memory.get(url)
    if pixmap is not None:
        _memory.move_to_end(url)
    return pixmap


def _recently_failed(url: str) -> bool:
    """Ссылку недавно пробовали и не смогли. Через минуту попробуем ещё раз."""
    failed_at = _failed.get(url)
    if failed_at is None:
        return False
    if time.monotonic() - failed_at < _RETRY_AFTER:
        return True
    del _failed[url]
    return False


def load(url: str, callback) -> None:
    """Позвать `callback(url, QPixmap)`, когда обложка будет готова.

    Вызов дешёвый: то, что уже есть, отдаётся сразу; одинаковые ссылки
    объединяются в одну загрузку. При ошибке callback просто не зовётся —
    заглушка на месте уже стоит."""
    if not url or _recently_failed(url):
        return
    ready = cached(url)
    if ready is not None:
        callback(url, ready)
        return

    waiting = _pending.get(url)
    if waiting is not None:
        waiting.append(callback)
        # Свежий запрос — картинка снова на виду. Двигаем её в конец очереди:
        # там разбор начинается первым, а прокрутка вниз оставляет позади ровно
        # то, что уже уехало с экрана
        if url in _queue:
            _queue.remove(url)
            _queue.append(url)
        return
    _pending[url] = [callback]
    # Обложка из тегов лежит на диске: очередь ей ни к чему, и ждать за чужими
    # сетевыми таймаутами она не должна
    if not url.startswith('http'):
        _start(url, queued=False)
        return
    _queue.append(url)
    _pump()


def _pump() -> None:
    """Запустить столько загрузок, сколько разрешает предел одновременных."""
    global _running
    while _queue and _running < _PARALLEL:
        # С конца: последнее, что попросили, человек и видит перед собой
        _running += 1
        _start(_queue.pop())


def _start(url: str, queued: bool = True) -> None:
    path = CACHE_DIR / f'{_key(url)}.img'

    def work():
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                pass
        # Обложка из тегов файла уже лежит на диске (см. core/tags.py) — читаем
        # её оттуда: в сеть за локальным путём идти незачем
        if not url.startswith('http'):
            return Path(url).read_bytes()
        return _fetch(url, path)

    def on_done(data, error):
        global _running
        if queued:
            _running -= 1
        callbacks = _pending.pop(url, [])
        try:
            if error or not data:
                logger.debug('Обложка не загрузилась: %s', error)
                _failed[url] = time.monotonic()
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(QByteArray(data)):
                _failed[url] = time.monotonic()
                return
            _remember(url, pixmap)
            for handler in callbacks:
                try:
                    handler(url, pixmap)
                except RuntimeError:
                    pass  # виджет успели закрыть, пока картинка ехала
        finally:
            # Очередь обязана двигаться даже после неудачи, иначе места
            # кончатся и всё остальное встанет навсегда
            if queued:
                _pump()

    run_async(work, on_done)


def trim_disk_cache(limit: int = _DISK_LIMIT) -> int:
    """Убрать с диска самые старые обложки, если папка переросла предел.

    Кэш обложек не должен расти бесконечно: за пару месяцев списков VK и
    YouTube там набираются десятки тысяч картинок. Зовётся один раз при запуске
    из фонового потока — файлы, которые нужны сейчас, уже лежат в памяти."""
    try:
        files = [(path.stat().st_mtime, path.stat().st_size, path)
                 for path in CACHE_DIR.glob('*.img')]
    except OSError:
        return 0
    total = sum(size for _mtime, size, _path in files)
    if total <= limit:
        return 0
    removed = 0
    for _mtime, size, path in sorted(files):        # самые старые — первыми
        if total <= limit:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    if removed:
        logger.info('Кэш обложек подрезан, удалено файлов: %d', removed)
    return removed
