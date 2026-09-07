"""Иконка приложения.

Исходник — SVG: он не мылится на HiDPI и его видно в diff'ах. Растр нужен в двух
местах: QIcon для окон и .ico для панели задач Windows (её иконку Qt берёт не из
окна, а из файла, привязанного к процессу — см. app/main.py)."""
import io
import logging
import struct

from PySide6.QtCore import QBuffer, QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .. import config

logger = logging.getLogger(__name__)

ICON_SVG = config.RES_DIR / 'assets' / 'icon.svg'
# .ico собирается на ходу, поэтому пишется к данным, а не к ресурсам:
# в собранном .exe папка ресурсов временная и доступна только на чтение
ICON_ICO = config.BASE_DIR / 'assets' / 'icon.ico'

# Размеры, которые Windows реально просит: список, панель задач, alt-tab, крупные значки
_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QPixmap:
    """Отрисовать SVG в квадрат заданного размера."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    renderer = QSvgRenderer(str(ICON_SVG))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    """QIcon со всеми размерами. Пустой, если файла иконки нет — не повод падать."""
    icon = QIcon()
    if not ICON_SVG.is_file():
        logger.warning('icon: нет файла %s', ICON_SVG)
        return icon
    for size in _SIZES:
        icon.addPixmap(render(size))
    return icon


def _png_bytes(size: int) -> bytes:
    # QByteArray держим в переменной: QBuffer хранит на него ссылку, а временный
    # объект Python успевает умереть раньше буфера — и процесс падает
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.WriteOnly)
    render(size).toImage().convertToFormat(QImage.Format_RGBA8888).save(buffer, 'PNG')
    buffer.close()
    return bytes(data)


def write_ico(path=None) -> str:
    """Собрать многоразмерный .ico. Qt умеет писать только один размер за раз,
    поэтому контейнер складываем сами — формат простой, а иконка панели задач
    от этого перестаёт быть мылом."""
    path = path or ICON_ICO
    images = [(size, _png_bytes(size)) for size in _SIZES]

    out = io.BytesIO()
    out.write(struct.pack('<HHH', 0, 1, len(images)))  # ICONDIR: reserved, type=icon, count
    offset = 6 + 16 * len(images)
    for size, data in images:
        # 0 в поле размера означает 256 — так задумано в формате
        out.write(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _size, data in images:
        out.write(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.getvalue())
    logger.info('icon: записан %s', path)
    return str(path)
