"""Обложки треков: качаем в фоне, храним на диске и в памяти.

Списки VK и результаты поиска YouTube — это сотни картинок. Тянуть их в потоке
интерфейса нельзя (окно замрёт), качать каждый раз заново — тоже: одна и та же
обложка возвращается при каждой прокрутке. Поэтому здесь маленький общий кэш."""
from __future__ import annotations

import hashlib
import logging
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

_memory: OrderedDict[str, QPixmap] = OrderedDict()
_pending: dict[str, list] = {}
# Ссылки, которые уже не открылись: без этого списки просили бы их снова при
# каждой перерисовке строки
_failed: set[str] = set()


def _key(url: str) -> str:
    return hashlib.sha1(url.encode('utf-8', 'ignore')).hexdigest()


def _remember(url: str, pixmap: QPixmap) -> None:
    _memory[url] = pixmap
    _memory.move_to_end(url)
    while len(_memory) > _MEMORY_LIMIT:
        _memory.popitem(last=False)


def _fetch(url: str, path: Path) -> bytes:
    import requests

    session = requests.Session()
    proxy.apply_to_session(session)
    response = session.get(url, timeout=15, stream=True)
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


def load(url: str, callback) -> None:
    """Позвать `callback(url, QPixmap)`, когда обложка будет готова.

    Вызов дешёвый: то, что уже есть, отдаётся сразу; одинаковые ссылки
    объединяются в одну загрузку. При ошибке callback просто не зовётся —
    заглушка на месте уже стоит."""
    if not url or url in _failed:
        return
    ready = cached(url)
    if ready is not None:
        callback(url, ready)
        return

    waiting = _pending.get(url)
    if waiting is not None:
        waiting.append(callback)
        return
    _pending[url] = [callback]

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
        callbacks = _pending.pop(url, [])
        if error or not data:
            logger.debug('Обложка не загрузилась: %s', error)
            _failed.add(url)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(QByteArray(data)):
            _failed.add(url)
            return
        _remember(url, pixmap)
        for handler in callbacks:
            try:
                handler(url, pixmap)
            except RuntimeError:
                pass  # виджет успели закрыть, пока картинка ехала

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
