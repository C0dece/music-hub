"""Рекомендации, радио и подборки YouTube - в понятном приложению виде.

Слой над `innertube`: тот отдаёт словари из чужого JSON, здесь получаются
обычные `Track` и подборки с заголовками. Здесь же живут две важные вещи.

Первая - честность. Если внутренний API недоступен (нет кук, YouTube поменял
ответ, нет сети), подборки не выдумываются: вместо них подставляется обычный
поиск, и такая подборка прямо помечена как запасная (`kind = 'fallback'`).
Выдавать поиск за рекомендации нельзя - человек должен понимать, что видит.

Вторая - кэш. Ответы держатся считанные минуты: главная не должна перезапрашивать
мегабайты JSON при каждом переключении раздела, но и показывать вчерашнее ей
незачем.

Класс ходит в сеть, поэтому вызывать его методы можно только из фонового потока
(`run_async`), а не из обработчика нажатия."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from .. import ytdlp_engine
from ..track import SOURCE_YOUTUBE, Track, from_youtube, split_artist_title
from .innertube import InnerTube

logger = logging.getLogger(__name__)

# Откуда взялась подборка. Метка нужна и в журнале, и в подсказке над разделом.
KIND_RECOMMENDED = 'recommended'   # настоящая лента YouTube Music
KIND_RADIO = 'radio'               # радио по треку
KIND_FALLBACK = 'fallback'         # обычный поиск вместо рекомендаций
KIND_HISTORY = 'history'           # собрано из истории прослушиваний
KIND_VK_RECOMS = 'vk_recoms'       # собственная подборка VK

KIND_LABELS = {
    KIND_RECOMMENDED: 'Рекомендация YouTube',
    KIND_RADIO: 'Радио по треку',
    KIND_FALLBACK: 'Подборка по поиску',
    KIND_HISTORY: 'По истории прослушиваний',
    KIND_VK_RECOMS: 'Рекомендация VK',
}

# Сколько живёт кэш. Главная меняется редко, радио по одному треку - почти никогда.
_HOME_TTL = 600
_RADIO_TTL = 1800
_PLAYLIST_TTL = 900
_CACHE_LIMIT = 40
# Сколько записей берём с одной полки главной - и треков, и плиток подборок
_HOME_PER_SHELF = 30

# Запасные подборки, когда внутреннего API нет. Заголовки честные: это поиск.
_FALLBACK_QUERIES = (
    ('Новинки музыки', 'new music 2026'),
    ('Популярное', 'popular music'),
    ('Спокойное', 'chill music mix'),
)


@dataclass
class MixShelf:
    """Полка готовых подборок: заголовок и плитки, каждую можно открыть."""

    title: str
    mixes: list[dict] = field(default_factory=list)


@dataclass
class Section:
    """Подборка на главной: заголовок, треки и происхождение."""

    title: str
    tracks: list[Track] = field(default_factory=list)
    kind: str = KIND_RECOMMENDED

    @property
    def label(self) -> str:
        """Пояснение для подсказки - откуда эти треки."""
        return KIND_LABELS.get(self.kind, '')

    @property
    def genuine(self) -> bool:
        """Настоящая ли это рекомендация, а не поиск вместо неё."""
        return self.kind in (KIND_RECOMMENDED, KIND_RADIO)


def playlist_id_from(value: str) -> str:
    """Номер плейлиста из ссылки или из самого номера. Не нашли - пустая строка."""
    value = (value or '').strip()
    if not value:
        return ''
    match = re.search(r'[?&]list=([A-Za-z0-9_-]+)', value)
    if match:
        return match.group(1)
    if value.startswith('VL'):
        value = value[2:]
    if re.fullmatch(r'[A-Za-z0-9_-]{6,}', value) and not value.startswith('http'):
        return value
    return ''


def to_track(item: dict) -> Track:
    """Запись из innertube - в трек приложения."""
    title = item.get('title') or ''
    artist = item.get('artist') or ''
    if not artist:
        artist, title = split_artist_title(title, '')
    video_id = item.get('id') or ''
    return Track(
        source=SOURCE_YOUTUBE,
        source_id=video_id,
        title=title,
        artist=artist,
        duration=int(item.get('duration') or 0),
        url=f'https://www.youtube.com/watch?v={video_id}' if video_id else '',
        cover=item.get('cover') or '',
        youtube_id=video_id,
        meta={'channel': artist},
    )


class Discovery:
    """Рекомендации YouTube с запасным путём и коротким кэшем."""

    def __init__(self, cookies_browser_provider=None):
        self._browser = cookies_browser_provider or (lambda: None)
        self._api = InnerTube(self._browser)
        self._cache: dict[str, tuple[float, object]] = {}
        # Клипы живут отдельно от общего кэша и без срока годности: какой ролик
        # снят на песню, за сеанс не меняется, а пустой ответ помнить даже
        # важнее - иначе трек без клипа ходил бы в поиск при каждом повторе
        self._clips: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---------- состояние ----------
    @property
    def api(self) -> InnerTube:
        return self._api

    @property
    def authorized(self) -> bool:
        """Есть ли вход в аккаунт: без него личных подборок не будет."""
        return self._api.authorized

    def reset(self) -> None:
        """Сменился браузер или прокси - начинаем с чистого листа."""
        self._api.reset()
        with self._lock:
            self._cache.clear()
            self._clips.clear()

    def forget(self, *prefixes: str) -> None:
        """Забыть разобранное - следующий вызов пойдёт в сеть.

        От `reset` отличается тем, что не трогает сессию: перечитывать куки
        браузера ради кнопки «Обновить» незачем, это лишние секунды на ровном
        месте. Без аргументов забывает всё."""
        with self._lock:
            if not prefixes:
                self._cache.clear()
                return
            for key in list(self._cache):
                if key.startswith(prefixes):
                    self._cache.pop(key, None)

    # ---------- кэш ----------
    def _cached(self, key: str, ttl: int):
        with self._lock:
            entry = self._cache.get(key)
        if entry is None or time.monotonic() - entry[0] > ttl:
            return None
        return entry[1]

    def _store(self, key: str, value) -> None:
        with self._lock:
            if len(self._cache) >= _CACHE_LIMIT:
                # Кэш не бесконечный: выбрасываем самую старую запись
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[key] = (time.monotonic(), value)

    # ---------- подборки ----------
    def _home_payload(self) -> tuple[list[Section], list[MixShelf]]:
        """Разбор главной: полки с треками и полки с готовыми подборками.

        Обе части приходят одним ответом, поэтому и кэшируются вместе - иначе
        лента ходила бы за одним и тем же JSON дважды."""
        cached = self._cached('home_payload', _HOME_TTL)
        if cached is not None:
            return cached
        try:
            raw_sections, raw_mixes = self._api.home_full(_HOME_PER_SHELF)
        except Exception as exc:  # noqa: BLE001 - раздел не должен падать из-за YouTube
            logger.info('YouTube: главная не разобралась (%s)', type(exc).__name__)
            raw_sections, raw_mixes = [], []

        sections: list[Section] = []
        for title, items in raw_sections:
            tracks = [to_track(item) for item in items]
            tracks = [track for track in tracks if track.youtube_id]
            if tracks:
                sections.append(Section(title, tracks, KIND_RECOMMENDED))
        shelves = [MixShelf(title, list(mixes)) for title, mixes in raw_mixes if mixes]
        payload = (sections, shelves)
        self._store('home_payload', payload)
        return payload

    def home(self, per_section: int = 20, sections: int = 8) -> list[Section]:
        """Главная YouTube Music. Пусто не возвращаем: будет запасной вариант."""
        cached = self._cached('home', _HOME_TTL)
        if cached is not None:
            return list(cached)
        result = [Section(section.title, section.tracks[:per_section], section.kind)
                  for section in self._home_payload()[0][:sections]]
        if not result:
            result = self._fallback_home(per_section)
        self._store('home', result)
        return list(result)

    def mixes(self, per_shelf: int = 24, shelves: int = 12) -> list[MixShelf]:
        """Готовые подборки главной: миксы, настроения, жанры.

        Именно из них и состоит лента YouTube Music - треков там россыпью почти
        нет. Работает и без входа: главная отдаётся всем, просто без личного."""
        return [MixShelf(shelf.title, shelf.mixes[:per_shelf])
                for shelf in self._home_payload()[1][:shelves]]

    def _fallback_home(self, limit: int) -> list[Section]:
        """Внутреннего API нет - показываем поиск и честно об этом говорим."""
        logger.info('YouTube: рекомендаций нет, показываю подборки по поиску')
        result: list[Section] = []
        for title, query in _FALLBACK_QUERIES:
            tracks = self._search_ytdlp(query, limit)
            if tracks:
                result.append(Section(title, tracks, KIND_FALLBACK))
        return result

    # ---------- радио и похожее ----------
    def radio(self, seed: Track, limit: int = 25) -> list[Track]:
        """Бесконечная подборка по треку. Сам трек в неё не попадает."""
        video_id = seed.youtube_id if seed is not None else ''
        if video_id:
            key = f'radio:{video_id}'
            cached = self._cached(key, _RADIO_TTL)
            if cached is not None:
                return list(cached)
            items = self._api.radio(video_id, limit)
            tracks = [to_track(item) for item in items if item.get('id')]
            if tracks:
                self._store(key, tracks)
                return list(tracks)
        return self._fallback_similar(seed, limit)

    def similar(self, seed: Track, limit: int = 25) -> list[Track]:
        """Похожее на трек - то же радио, но берётся началом списка."""
        return self.radio(seed, limit)

    def music_video(self, track: Track) -> str:
        """Номер клипа для песни из YouTube Music. Нет клипа - пустая строка.

        Ходит в сеть, поэтому вызывать только из фонового потока. Ответ, включая
        отрицательный, запоминается на весь сеанс."""
        video_id = track.youtube_id if track is not None else ''
        if not video_id:
            return ''
        with self._lock:
            if video_id in self._clips:
                return self._clips[video_id]
        found = self._api.music_video(track.title, track.artist, track.duration)
        # Поиск умеет вернуть ту же самую запись: клипом её считать нечего
        found = '' if found == video_id else found
        with self._lock:
            self._clips[video_id] = found
        return found

    def _fallback_similar(self, seed: Track, limit: int) -> list[Track]:
        """Радио по треку не из YouTube (или API молчит) - ищем по исполнителю."""
        if seed is None:
            return []
        query = (seed.artist or seed.title or '').strip()
        if not query:
            return []
        logger.info('YouTube: радио через поиск по «%s»', query)
        return [track for track in self._search_ytdlp(query, limit)
                if track.uid != seed.uid]

    # ---------- плейлисты ----------
    def playlists(self, limit: int = 40) -> list[dict]:
        """Плейлисты пользователя. Нет входа - пустой список, и это нормально."""
        cached = self._cached('playlists', _PLAYLIST_TTL)
        if cached is not None:
            return list(cached)
        try:
            entries = self._api.playlists(limit)
        except Exception as exc:  # noqa: BLE001 - раздел не должен падать из-за YouTube
            logger.info('YouTube: плейлисты не получились (%s)', type(exc).__name__)
            entries = []
        self._store('playlists', entries)
        return list(entries)

    def liked(self, limit: int = 60) -> list[Track]:
        """«Мне понравилось» из YouTube Music. Без входа - пусто, и это честно."""
        cached = self._cached('liked', _PLAYLIST_TTL)
        if cached is not None:
            return list(cached)
        items = self._api.library(limit)
        tracks = [to_track(item) for item in items if item.get('id')]
        self._store('liked', tracks)
        return list(tracks)

    def playlist_tracks(self, playlist: str, limit: int = 200) -> list[Track]:
        """Треки плейлиста по номеру или по ссылке.

        Сначала внутренний API (быстро и с обложками), потом yt-dlp - он открывает
        любой публичный плейлист даже без входа. Ничего при этом не скачивается."""
        playlist_id = playlist_id_from(playlist)
        if not playlist_id:
            return []
        key = f'playlist:{playlist_id}'
        cached = self._cached(key, _PLAYLIST_TTL)
        if cached is not None:
            return list(cached)

        tracks: list[Track] = []
        try:
            items = self._api.playlist_items(playlist_id, limit)
            tracks = [to_track(item) for item in items if item.get('id')]
        except Exception as exc:  # noqa: BLE001
            logger.info('YouTube: плейлист через API не открылся (%s)', type(exc).__name__)
        if not tracks:
            tracks = self._playlist_ytdlp(playlist_id, limit)
        if tracks:
            self._store(key, tracks)
        return list(tracks)

    def _playlist_ytdlp(self, playlist_id: str, limit: int) -> list[Track]:
        """Запасной путь: тот же yt-dlp, что и у загрузчика, но без скачивания."""
        url = f'https://www.youtube.com/playlist?list={playlist_id}'
        try:
            entries = ytdlp_engine.extract_entries(url, self._browser())
        except Exception as exc:  # noqa: BLE001
            logger.info('YouTube: плейлист не прочитался (%s)', type(exc).__name__)
            return []
        tracks = [from_youtube(entry) for entry in entries if entry.get('id')]
        return tracks[:limit]

    # ---------- поиск ----------
    def search(self, query: str, limit: int = 25, music_only: bool = True) -> list[Track]:
        """Поиск. Музыкальный - через YouTube Music, обычный - через yt-dlp."""
        query = (query or '').strip()
        if not query:
            return []
        if music_only:
            items = self._api.search(query, limit, music_only=True)
            tracks = [to_track(item) for item in items if item.get('id')]
            if tracks:
                return tracks
        return self._search_ytdlp(query, limit)

    def _search_ytdlp(self, query: str, limit: int) -> list[Track]:
        """Опора на yt-dlp: работает всегда, пока работает сам загрузчик."""
        try:
            entries = ytdlp_engine.search(query, limit, self._browser())
        except Exception as exc:  # noqa: BLE001 - раздел не должен падать из-за поиска
            logger.info('YouTube: поиск не удался (%s)', type(exc).__name__)
            return []
        return [from_youtube(entry) for entry in entries if entry.get('id')]
