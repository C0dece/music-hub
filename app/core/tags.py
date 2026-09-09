"""Теги музыкальных файлов: исполнитель, название, альбом, длительность, обложка.

Читаем mutagen, а не ffprobe: ffprobe у нас есть (лежит рядом с ffmpeg), но это
процесс на каждый файл - на папке в несколько тысяч треков сканирование заняло бы
минуты, а встроенную обложку пришлось бы вынимать отдельным запуском ffmpeg.
mutagen читает всё это одним обращением к файлу и без внешних процессов.

Без mutagen приложение продолжает работать: тогда исполнитель и название берутся
из имени файла, как было раньше. Отдельная зависимость не должна ронять запуск.

Обложки складываем в общий кэш обложек - тот же, что у сетевых картинок, поэтому
интерфейсу всё равно, откуда взялась картинка: у Track.cover просто путь."""
from __future__ import annotations

import hashlib
import logging
import os

from .. import config

logger = logging.getLogger(__name__)

try:
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
except ImportError:  # пакета нет - работаем по именам файлов
    mutagen = None
    FLAC = ID3 = MP4 = None

# Куда класть вынутые обложки. Отдельная папка внутри кэша: чистить кэш сетевых
# картинок можно, а эти пришлось бы вынимать заново при каждом сканировании.
_COVERS_SUBDIR = 'embedded'

# Расширение по типу картинки. jpeg покрывает почти всё, png встречается реже.
_IMAGE_EXTS = {'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png'}


def available() -> bool:
    return mutagen is not None


def read(path: str, with_cover: bool = True) -> dict:
    """Что известно о файле: artist, title, album, duration, cover.

    Пустой словарь - читать нечего (нет mutagen, файл битый, тегов нет).
    Вызывающая сторона сама решает, чем это дополнить."""
    if mutagen is None:
        return {}
    try:
        audio = mutagen.File(path)
    except Exception as exc:  # битый файл не должен ронять сканирование папки
        logger.debug('tags: %s не читается: %s', os.path.basename(path), exc)
        return {}
    if audio is None:
        return {}

    data: dict = {}
    for key, names in (('artist', ('artist', 'albumartist', 'author', 'TPE1', '\xa9ART')),
                       ('title', ('title', 'TIT2', '\xa9nam')),
                       ('album', ('album', 'TALB', '\xa9alb'))):
        value = _first(audio, names)
        if value:
            data[key] = value

    info = getattr(audio, 'info', None)
    length = getattr(info, 'length', 0) or 0
    if length:
        data['duration'] = int(length)

    if with_cover:
        cover = _cover_path(path, audio)
        if cover:
            data['cover'] = cover
    return data


def _first(audio, names) -> str:
    """Первое непустое значение из перечисленных тегов.

    У каждого контейнера свои имена (`TPE1` в mp3, `\xa9ART` в m4a, `artist` в
    flac/ogg), а mutagen.File отдаёт их как есть - отсюда список синонимов."""
    for name in names:
        try:
            value = audio.get(name)
        except Exception:
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ''
        value = str(value).strip()
        if value:
            return value
    return ''


def _cover_path(path: str, audio) -> str:
    """Вынуть встроенную обложку в файл кэша и вернуть путь к нему.

    Держать картинку в памяти нельзя: в списке они лежат тысячами. Имя файла -
    от пути и времени изменения, поэтому обложка вынимается один раз, а после
    правки тегов берётся заново."""
    data, mime = _cover_bytes(audio)
    if not data:
        return ''
    try:
        stat = os.stat(path)
        key = f'{os.path.normcase(os.path.abspath(path))}|{int(stat.st_mtime)}'
    except OSError:
        return ''
    name = hashlib.sha1(key.encode('utf-8')).hexdigest() + _IMAGE_EXTS.get(mime, '.jpg')
    target = os.path.join(_covers_dir(), name)
    if os.path.exists(target):
        return target
    try:
        with open(target, 'wb') as fh:
            fh.write(data)
    except OSError as exc:
        logger.debug('tags: обложка %s не сохранилась: %s', name, exc)
        return ''
    return target


def _cover_bytes(audio) -> tuple[bytes, str]:
    """Картинка из тегов. Три контейнера - три разных места, где она лежит."""
    tags = getattr(audio, 'tags', None)
    # FLAC и Ogg: список Picture
    pictures = getattr(audio, 'pictures', None)
    if pictures:
        picture = pictures[0]
        return bytes(picture.data), getattr(picture, 'mime', '') or 'image/jpeg'
    if tags is None:
        return b'', ''
    # MP4/M4A: атом covr
    covr = None
    try:
        covr = tags.get('covr')
    except Exception:
        covr = None
    if covr:
        item = covr[0]
        fmt = getattr(item, 'imageformat', None)
        mime = 'image/png' if fmt == 14 else 'image/jpeg'  # MP4Cover.FORMAT_PNG
        return bytes(item), mime
    # MP3: кадры APIC
    try:
        frames = tags.getall('APIC')
    except Exception:
        frames = []
    if frames:
        return bytes(frames[0].data), frames[0].mime or 'image/jpeg'
    return b'', ''


def _covers_dir() -> str:
    """Подпапка внутри кэша обложек: чистка кэша сетевых картинок её не трогает
    (trim_disk_cache смотрит только на «*.img» в корне), а лежит всё в одном месте."""
    directory = os.path.join(str(config.CONFIG_DIR), 'covers', _COVERS_SUBDIR)
    os.makedirs(directory, exist_ok=True)
    return directory
