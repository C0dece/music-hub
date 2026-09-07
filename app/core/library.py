"""Библиотека — то, что уже скачано и лежит на диске.

Список строится сканированием папок из настроек, а не по history.json: файлы
переименовывают и удаляют мимо программы, и история быстро расходится с реальностью.
История нужна для другого — чтобы не качать одно и то же дважды."""
import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass

from . import history, tags
from .track import AUDIO_EXTS, VIDEO_EXTS, Track, from_local

logger = logging.getLogger(__name__)

MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


@dataclass
class MediaFile:
    path: str
    name: str          # имя файла без расширения
    ext: str           # '.mp3'
    kind: str          # 'audio' | 'video'
    size: int          # байт
    mtime: float       # время последнего изменения, unix
    folder: str        # папка, в которой лежит файл
    source: str = ''   # 'youtube' | 'vk_audio' | 'vk_video' — из истории, если файл в ней есть

    @property
    def size_text(self) -> str:
        return format_size(self.size)


def format_size(size: int) -> str:
    if size < 1024:
        return f'{size} Б'
    for unit in ('КБ', 'МБ', 'ГБ'):
        size /= 1024
        if size < 1024:
            return f'{size:.1f} {unit}'
    return f'{size:.1f} ТБ'


def scan(directories) -> list[MediaFile]:
    """Все медиафайлы в указанных папках (с подпапками), новые сверху."""
    files: list[MediaFile] = []
    seen: set[str] = set()
    # Сама папка не помнит, откуда файл, — это знает только история загрузок
    sources = history.sources_by_path()

    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        for root, _dirs, names in os.walk(directory):
            for name in names:
                ext = os.path.splitext(name)[1].lower()
                if ext not in MEDIA_EXTS:
                    continue
                path = os.path.join(root, name)
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue  # папка музыки и папка видео могут совпадать
                seen.add(key)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                files.append(MediaFile(
                    path=path,
                    name=os.path.splitext(name)[0],
                    ext=ext,
                    kind='audio' if ext in AUDIO_EXTS else 'video',
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    folder=root,
                    source=sources.get(key, ''),
                ))

    files.sort(key=lambda f: f.mtime, reverse=True)
    return files


def audio_track(path: str, name: str = '') -> Track:
    """Трек из файла — с тегами, если они читаются.

    Отдельно от `track.from_local`, потому что чтение тегов — это работа с диском:
    модель треков в неё не лезет, а здесь мы и так уже в файловом модуле."""
    return from_local(path, name, tags.read(path))


def scan_audio_tracks(directories) -> list[Track]:
    """Своя музыка из указанных папок треками, а не файлами.

    Нужно «Моей музыке»: там всё — Track, независимо от источника, поэтому файл с
    диска обязан выглядеть так же, как аудиозапись VK. Обход папок долгий (теги
    читаются у каждого файла), поэтому звать только из фона."""
    result: list[Track] = []
    for media in scan(directories):
        if media.kind != 'audio':
            continue
        result.append(audio_track(media.path, media.name))
    return result


def open_file(path: str) -> None:
    """Открыть файл программой по умолчанию."""
    if sys.platform == 'win32':
        os.startfile(path)  # noqa: S606 — штатный способ открыть файл в Windows
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


def reveal(path: str) -> None:
    """Показать файл в проводнике (выделенным)."""
    if sys.platform == 'win32':
        # Кавычки explorer разбирает сам, отсюда строка вместо списка аргументов
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', path])
    else:
        subprocess.Popen(['xdg-open', os.path.dirname(path)])


def rename(path: str, new_name: str) -> str:
    """Переименовать файл, сохранив расширение. Возвращает новый путь."""
    new_name = new_name.strip()
    if not new_name:
        raise ValueError('Пустое имя')
    ext = os.path.splitext(path)[1]
    target = os.path.join(os.path.dirname(path), new_name + ext)
    if os.path.normcase(target) == os.path.normcase(path):
        return path
    if os.path.exists(target):
        raise FileExistsError(f'Файл «{new_name}{ext}» уже есть в этой папке')
    os.rename(path, target)
    return target


def delete(paths: list[str]) -> None:
    """Удалить файлы. В Windows — в корзину, чтобы промах можно было отменить."""
    if sys.platform == 'win32' and _delete_to_recycle_bin(paths):
        return
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            logger.exception('library: не удалось удалить %s', path)
            raise


# --- корзина Windows -------------------------------------------------------
# Через SHFileOperationW из shell32 — это системный API, дополнительных пакетов
# не требует. Кроме корзины он даёт и штатный диалог «файл занят другой программой».

_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', ctypes.c_void_p),
        ('wFunc', ctypes.c_uint),
        ('pFrom', ctypes.c_wchar_p),
        ('pTo', ctypes.c_wchar_p),
        ('fFlags', ctypes.c_uint16),
        ('fAnyOperationsAborted', ctypes.c_int),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', ctypes.c_wchar_p),
    ]


def _delete_to_recycle_bin(paths: list[str]) -> bool:
    try:
        # Список путей для API — строки через \0 и ещё один \0 в конце
        buffer = '\0'.join(os.path.abspath(p) for p in paths) + '\0\0'
        op = _SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=_FO_DELETE,
            pFrom=buffer,
            pTo=None,
            fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT,
            fAnyOperationsAborted=0,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return result == 0 and not op.fAnyOperationsAborted
    except Exception:
        logger.exception('library: корзина недоступна, удаляю файлы напрямую')
        return False
