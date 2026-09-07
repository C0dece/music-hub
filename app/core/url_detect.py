import re
from dataclasses import dataclass
from urllib.parse import urlparse

# Ссылка внутри произвольного текста: список часто вставляют с нумерацией,
# названиями и прочим мусором вокруг
_URL_TOKEN = re.compile(r'https?://\S+')

_YOUTUBE_HOSTS = {'www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}
_VK_HOSTS = {'vk.com', 'www.vk.com', 'm.vk.com', 'vk.ru', 'www.vk.ru', 'm.vk.ru'}


@dataclass
class LinkInfo:
    source: str   # 'youtube' | 'vk_video' | 'unknown'
    url: str


def detect(url: str) -> LinkInfo:
    url = url.strip()
    host = urlparse(url).netloc.lower()

    if host in _YOUTUBE_HOSTS:
        return LinkInfo('youtube', url)
    if host in _VK_HOSTS:
        return LinkInfo('vk_video', url)
    return LinkInfo('unknown', url)


def split_urls(text: str) -> list[str]:
    """Ссылки из вставленного текста — по строке, через пробел или вперемешку с ним.

    Повторы убираем (в скопированных списках их полно), порядок сохраняем. Если
    ссылок с http:// в тексте нет, отдаём его как есть — пусть о нераспознанном
    скажет detect(), а не тишина в ответ на нажатие кнопки."""
    text = (text or '').strip()
    if not text:
        return []
    tokens = [token.strip('.,;:)]}>»"\'') for token in _URL_TOKEN.findall(text)]
    if not tokens:
        return [text]
    seen: set[str] = set()
    urls = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            urls.append(token)
    return urls
