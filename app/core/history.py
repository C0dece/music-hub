import json
import os
import threading

from .. import config

_lock = threading.Lock()
_cache: dict | None = None

# Расширения видеофайлов — по ним восстанавливаем режим у старых записей истории
_VIDEO_EXT = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv', '.ts', '.3gp'}


def key_for(source: str, item_id, mode: str = 'audio') -> str:
    """Ключ записи в истории.

    Музыка и видео одного и того же ролика — два разных скачивания, и ключ у них
    разный: иначе после скачанной дорожки то же видео считалось бы «уже скачанным».
    У треков VK режима нет — там всегда музыка."""
    if source == 'vk_audio':
        return f'vk_audio:{item_id}'
    return f'{source}:{item_id}:{"video" if mode == "video" else "audio"}'


def _migrate(data: dict) -> bool:
    """Дописать режим ключам, сделанным до его появления. True — если что-то изменилось.

    Старый ключ `youtube:ID` стоял и за музыку, и за видео. Какой это был режим,
    видно по расширению скачанного файла; если файла в записи нет — считаем музыкой,
    приложение по умолчанию качает именно её."""
    legacy = [key for key in data
              if key.count(':') == 1 and key.split(':', 1)[0] in ('youtube', 'vk_video')]
    for key in legacy:
        entry = data.pop(key) or {}
        ext = os.path.splitext((entry.get('path') or ''))[1].lower()
        mode = 'video' if ext in _VIDEO_EXT else 'audio'
        source, item_id = key.split(':', 1)
        data.setdefault(key_for(source, item_id, mode), entry)
    return bool(legacy)


def _ensure_loaded() -> None:
    global _cache
    if _cache is not None:
        return
    if config.HISTORY_FILE.exists():
        try:
            _cache = json.loads(config.HISTORY_FILE.read_text(encoding='utf-8'))
            if _migrate(_cache):
                _save()
            return
        except (json.JSONDecodeError, OSError):
            pass
    _cache = {}


def _save() -> None:
    config.ensure_dirs()
    config.HISTORY_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding='utf-8')


def is_downloaded(key: str) -> bool:
    with _lock:
        _ensure_loaded()
        return key in _cache


def sources_by_path() -> dict[str, str]:
    """Откуда какой файл взят: путь -> 'youtube' | 'vk_audio' | 'vk_video'.

    Ключ истории начинается с названия источника, а путь она хранит целиком —
    этого хватает, чтобы в библиотеке показывать, откуда файл. Скачанное другой
    программой или переименованное мимо неё в карту не попадёт: источник там
    неизвестен, и выдумывать его не нужно."""
    with _lock:
        _ensure_loaded()
        result: dict[str, str] = {}
        for key, entry in _cache.items():
            path = (entry or {}).get('path')
            if path:
                result[os.path.normcase(os.path.abspath(path))] = key.split(':', 1)[0]
        return result


def mark_downloaded(key: str, title: str, path: str) -> None:
    with _lock:
        _ensure_loaded()
        _cache[key] = {'title': title, 'path': path}
        _save()
