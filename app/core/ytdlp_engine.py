import logging
import os
import re

import yt_dlp

from .. import config
from . import browser_cookies, js_runtime, proxy

logger = logging.getLogger(__name__)

_HEIGHT_MAP = {'2160': 2160, '1440': 1440, '1080': 1080, '720': 720, '480': 480}

# YouTube из России отдаёт потоки рывками: короткий таймаут по умолчанию (20 с) обрывал
# скачивание на «Read timed out» уже на первом видео. Ретраи здесь дешевле, чем упавшая задача.
_NETWORK_OPTS = {
    'socket_timeout': 60,
    'retries': 20,
    'fragment_retries': 20,
    'retry_sleep_functions': {'http': lambda n: min(2 ** n, 30)},
}


def _network_opts() -> dict:
    """Сетевая часть опций. Прокси добавляем именно здесь: он нужен и загрузке,
    и разбору ссылки, а забытый где-то ключ выглядел бы как «работает через раз»."""
    return {**_NETWORK_OPTS, **proxy.ytdlp_opts()}


class _YtdlpLogger:
    """Свой логгер вместо немоты: с quiet=True yt-dlp не говорит ни слова, и 20 повторов
    с паузами до 30 с выглядели как намертво вставшая загрузка на 0% — ни в логе, ни в
    очереди. При заданном logger yt-dlp шлёт сюда всё, включая сообщения о повторах,
    мимо quiet."""

    _RETRY_RE = re.compile(r'Retrying[^(]*\((\d+)/(\d+)\)')

    def __init__(self, retry_cb=None):
        self._retry_cb = retry_cb

    def debug(self, msg: str) -> None:
        logger.debug('yt-dlp: %s', msg)
        match = self._RETRY_RE.search(msg)
        if match and self._retry_cb:
            self._retry_cb(int(match.group(1)), int(match.group(2)))

    def info(self, msg: str) -> None:
        logger.debug('yt-dlp: %s', msg)

    def warning(self, msg: str) -> None:
        logger.warning('yt-dlp: %s', msg)

    def error(self, msg: str) -> None:
        logger.error('yt-dlp: %s', msg)


def _format_spec(mode: str, video_quality: str) -> str:
    if mode == 'audio':
        return 'bestaudio/best'
    height = _HEIGHT_MAP.get(video_quality)
    if height:
        return f'bestvideo[height<={height}]+bestaudio/best[height<={height}]'
    return 'bestvideo+bestaudio/best'


def build_opts(mode: str, video_quality: str, audio_format: str, audio_bitrate: str,
                output_dir: str, cookies_browser: str | None = None, progress_hook=None,
                postprocessor_hook=None, retry_cb=None,
                fragment_concurrency: int = 1) -> dict:
    opts = {
        'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        'format': _format_spec(mode, video_quality),
        'ffmpeg_location': str(config.FFMPEG_PATH),
        'ignoreerrors': False,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'logger': _YtdlpLogger(retry_cb),
        'overwrites': False,
        'nooverwrites': True,
        # Скорость режется на каждое соединение отдельно, поэтому файл, разбитый
        # на куски (DASH и HLS у YouTube), быстрее забрать в несколько потоков.
        # На цельный файл опция не влияет — там кусок ровно один
        'concurrent_fragment_downloads': max(1, int(fragment_concurrency)),
        **_network_opts(),
    }
    # Без JS-движка YouTube не отдаст ссылки на потоки — пусть падает здесь с понятным
    # текстом, а не позже с «Requested format is not available»
    opts.update(js_runtime.ytdlp_opts())
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]
    if postprocessor_hook:
        opts['postprocessor_hooks'] = [postprocessor_hook]
    opts.update(browser_cookies.ytdlp_cookie_opts(cookies_browser, config.COOKIES_FILE))

    if mode == 'audio':
        postprocessor = {'key': 'FFmpegExtractAudio', 'preferredcodec': audio_format}
        if audio_bitrate != 'best':
            postprocessor['preferredquality'] = audio_bitrate
        opts['postprocessors'] = [postprocessor]
    else:
        opts['merge_output_format'] = 'mp4'
        opts['postprocessors'] = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]

    return opts


def build_audio_file_opts(outtmpl: str, progress_hook=None, postprocessor_hook=None) -> dict:
    """Опции для скачивания одного готового аудиофайла по прямой ссылке — музыка VK.

    Формат и битрейт из настроек здесь намеренно не применяются: VK отдаёт уже готовый
    mp3, и перекодирование только ухудшило бы звук. Постпроцессор нужен лишь чтобы снять
    HLS/контейнерную обёртку — при совпадении кодека yt-dlp делает это через «-c copy».

    JS-движок здесь не нужен: подписи потоков — история про YouTube, ссылку на трек VK
    мы уже получили сами."""
    opts = {
        'outtmpl': outtmpl,
        'ffmpeg_location': str(config.FFMPEG_PATH),
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        **_network_opts(),
    }
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]
    if postprocessor_hook:
        opts['postprocessor_hooks'] = [postprocessor_hook]
    return opts


def downloaded_path(info: dict) -> str:
    """Путь к готовому файлу. После постпроцессора он есть только в requested_downloads —
    в info['filepath'] лежит имя до конвертации (или ничего)."""
    for entry in info.get('requested_downloads') or []:
        path = entry.get('filepath')
        if path:
            return path
    return info.get('filepath') or info.get('_filename') or ''


def extract_entries(url: str, cookies_browser: str | None = None) -> list[dict]:
    """Быстрый список элементов (без скачивания) — для одиночного видео или плейлиста."""
    opts = {
        'extract_flat': 'in_playlist',
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        **_network_opts(),
    }
    opts.update(js_runtime.ytdlp_opts())
    opts.update(browser_cookies.ytdlp_cookie_opts(cookies_browser, config.COOKIES_FILE))

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return []

    playlist_title = info.get('title') if info.get('entries') else None
    entries = info.get('entries')
    if not entries:
        return [{
            'id': info.get('id'),
            'title': info.get('title') or url,
            'url': info.get('webpage_url') or url,
            'duration': info.get('duration'),
            'uploader': info.get('uploader') or info.get('channel') or '',
            'playlist_title': None,
        }]

    result = []
    for e in entries:
        if not e:
            continue
        raw_url = e.get('url')
        entry_url = raw_url if raw_url and raw_url.startswith('http') else f"https://www.youtube.com/watch?v={e.get('id')}"
        result.append({
            'id': e.get('id'),
            'title': e.get('title') or e.get('id') or '???',
            'url': entry_url,
            'duration': e.get('duration'),
            'uploader': e.get('uploader') or e.get('channel') or '',
            'playlist_title': playlist_title,
        })
    return result


def search(query: str, limit: int = 25, cookies_browser: str | None = None) -> list[dict]:
    """Поиск по YouTube через сам yt-dlp — своего ключа к API для этого не нужно.

    Берём только список (`extract_flat`): разбор каждого ролика по отдельности занял
    бы десятки секунд, а для строки результата хватает названия, канала и длины.
    Обложку не запрашиваем — у YouTube она собирается по номеру ролика."""
    query = (query or '').strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    entries = extract_entries(f'ytsearch{limit}:{query}', cookies_browser)
    for entry in entries:
        entry['playlist_title'] = None
        if entry.get('id') and not entry.get('thumbnail'):
            entry['thumbnail'] = f"https://i.ytimg.com/vi/{entry['id']}/mqdefault.jpg"
    return entries


def download(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info and info.get('entries'):
        info = info['entries'][0]
    return info or {}
