"""Поиск JavaScript-движка для yt-dlp.

Начиная с версий 2025 года YouTube отдаёт ссылки на потоки, подписанные JS-функцией
из плеера (signature + «n challenge»). Считать её yt-dlp сам больше не умеет — нужен
внешний JS-рантайм (`--js-runtimes`) и скрипты решателя из пакета `yt-dlp-ejs`.
Без них извлечение заканчивается «The page needs to be reloaded» / «Requested format
is not available»: форматы просто выкидываются как нерабочие.

Поэтому рядом с приложением лежит `runtime/qjs.exe` (QuickJS-ng, ~2 МБ) — самый
маленький из поддерживаемых движков. Если его нет, ищем установленные deno/node/bun
в PATH, а если нет и их — говорим об этом человеческим языком вместо ошибки yt-dlp.
"""
import logging
import os
import shutil
import urllib.request

from .. import config

logger = logging.getLogger(__name__)

# Минимальные версии — из yt_dlp/utils/_jsruntime.py. Проверять их самим не нужно:
# yt-dlp сам отсеет неподходящий движок, здесь важен только порядок предпочтения.
_PATH_RUNTIMES = ('deno', 'node', 'bun', 'qjs')

QJS_DOWNLOAD_URL = (
    'https://github.com/quickjs-ng/quickjs/releases/download/v0.16.2/qjs-windows-x86_64.exe'
)

_NO_RUNTIME_HINT = (
    'Для YouTube нужен JavaScript-движок: без него yt-dlp не может расшифровать ссылки '
    'на потоки, и видео не скачивается.\n\n'
    'Откройте «Настройки» → «Движок JavaScript» → «Скачать»: приложение само положит '
    f'нужный файл в папку {config.JS_RUNTIME_FILE.parent.name}\\ (≈2 МБ). '
    'Либо установите Deno (deno.com) или Node.js 22+, они тоже подойдут.'
)


class JsRuntimeMissing(Exception):
    """Ни встроенного, ни системного JS-движка не нашлось."""

    def __init__(self, message: str = _NO_RUNTIME_HINT):
        super().__init__(message)


def bundled_path() -> str | None:
    """Путь к движку, лежащему рядом с приложением, если он на месте.

    Оба места проверяем каждый раз заново: только что скачанный движок должен
    заработать сразу, а не после перезапуска программы."""
    for path in (config.JS_RUNTIME_DOWNLOAD, config.JS_RUNTIME_FILE):
        if path.is_file():
            return str(path)
    return None


def find() -> tuple[str, str] | None:
    """(имя рантайма для yt-dlp, путь к бинарю) или None, если движка нет нигде."""
    bundled = bundled_path()
    if bundled:
        return 'quickjs', bundled
    for name in _PATH_RUNTIMES:
        found = shutil.which(name)
        if found:
            # yt-dlp знает движок под именем 'quickjs', а бинарь называется 'qjs'
            return ('quickjs' if name == 'qjs' else name), found
    return None


def ytdlp_opts() -> dict:
    """Кусок опций yt-dlp с выбранным движком. Бросает JsRuntimeMissing, если движка нет."""
    runtime = find()
    if not runtime:
        raise JsRuntimeMissing
    name, path = runtime
    logger.debug('js_runtime: используем %s (%s)', name, path)
    return {'js_runtimes': {name: {'path': path}}}


def describe() -> str:
    """Короткая строка для интерфейса настроек."""
    runtime = find()
    if not runtime:
        return 'не найден'
    name, path = runtime
    if bundled_path() == path:
        return f'встроенный QuickJS ({config.JS_RUNTIME_FILE.name})'
    return f'{name} ({path})'


def download_bundled(progress_cb=None) -> str:
    """Скачать QuickJS в папку приложения. progress_cb(получено, всего) — для индикатора."""
    dest = config.JS_RUNTIME_DOWNLOAD
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.part')

    with urllib.request.urlopen(QJS_DOWNLOAD_URL, timeout=60) as response:
        total = int(response.headers.get('Content-Length') or 0)
        received = 0
        with open(tmp, 'wb') as fh:
            while chunk := response.read(65536):
                fh.write(chunk)
                received += len(chunk)
                if progress_cb:
                    progress_cb(received, total)

    os.replace(tmp, dest)
    logger.info('js_runtime: движок скачан в %s', dest)
    return str(dest)
