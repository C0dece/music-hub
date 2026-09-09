"""Что играть дальше: радио по треку, автопродолжение очереди, подборки главной.

Здесь сходятся три источника: лента YouTube Music (`youtube.Discovery`), история
прослушиваний из базы и обычный поиск как запасной путь. Наружу это один объект
с несколькими понятными методами - разделы интерфейса не должны знать, откуда
именно пришёл трек.

Все методы ходят в сеть, поэтому вызывать их можно только из фонового потока
(`run_async`). Qt здесь нет сознательно: ядро не должно зависеть от интерфейса."""
from __future__ import annotations

import logging
import random

from .track import SOURCE_YOUTUBE, Track
from .youtube import Discovery
from .youtube.discovery import KIND_FALLBACK, KIND_HISTORY, KIND_RADIO, Section

logger = logging.getLogger(__name__)

# Метка происхождения. Кладётся в meta трека и видна в подсказке и в журнале,
# но не засоряет интерфейс постоянными надписями.
PROVENANCE = 'provenance'


class Recommender:
    """Подбор треков с оглядкой на скрытое, недавнее и уже стоящее в очереди."""

    def __init__(self, discovery: Discovery, store=None):
        self._discovery = discovery
        self._store = store

    @property
    def discovery(self) -> Discovery:
        return self._discovery

    # ---------- радио ----------
    def radio(self, seed: Track, limit: int = 25) -> list[Track]:
        """Подборка по треку. Сам трек в неё не попадает - он и так первый."""
        if seed is None:
            return []
        tracks = self._discovery.radio(seed, limit + 10)
        kind = KIND_RADIO if seed.source == SOURCE_YOUTUBE else KIND_FALLBACK
        return self._prepare(tracks, {seed.uid}, limit, kind)

    def artist_radio(self, artist: str, limit: int = 25) -> list[Track]:
        """Радио по исполнителю - поиском, потому что затравки-ролика тут нет."""
        artist = (artist or '').strip()
        if not artist:
            return []
        tracks = self._discovery.search(artist, limit + 10)
        return self._prepare(tracks, set(), limit, KIND_FALLBACK)

    # ---------- автопродолжение ----------
    def autoplay(self, seed: Track, exclude: set[str], limit: int = 10) -> list[Track]:
        """Чем продолжить очередь, когда она подходит к концу.

        Подпись совпадает с тем, что ждёт PlayerController.set_recommender."""
        exclude = set(exclude or ())
        result: list[Track] = []
        if seed is not None:
            result = self._prepare(self._discovery.radio(seed, limit + 15),
                                   exclude, limit, KIND_RADIO)
        if not result:
            result = self._from_history(exclude, limit)
        if not result and seed is not None and seed.artist:
            result = self._prepare(self._discovery.search(seed.artist, limit + 10),
                                   exclude, limit, KIND_FALLBACK)
        return result

    def _from_history(self, exclude: set[str], limit: int) -> list[Track]:
        """Запасной путь: искать похожее на то, что человек слушает чаще всего."""
        artists = self._top_artists(5)
        if not artists:
            return []
        random.shuffle(artists)
        result: list[Track] = []
        for artist in artists:
            found = self._prepare(self._discovery.search(artist, limit),
                                  exclude | {t.uid for t in result},
                                  limit - len(result), KIND_HISTORY)
            result.extend(found)
            if len(result) >= limit:
                break
        return result[:limit]

    def _top_artists(self, limit: int) -> list[str]:
        if self._store is None:
            return []
        try:
            return list(self._store.top_artists(limit))
        except Exception:  # noqa: BLE001 - без истории просто нет подсказок
            logger.debug('Рекомендации: история не прочиталась', exc_info=True)
            return []

    # ---------- подборки главной ----------
    def sections(self, per_section: int = 16) -> list[Section]:
        """Подборки YouTube Music, очищенные от скрытого пользователем."""
        hidden, artists = self._hidden()
        result: list[Section] = []
        for section in self._discovery.home(per_section + 6):
            tracks = [track for track in section.tracks
                      if track.uid not in hidden
                      and (track.artist or '').strip().lower() not in artists]
            if tracks:
                for track in tracks:
                    track.meta.setdefault(PROVENANCE, section.kind)
                result.append(Section(section.title, tracks[:per_section], section.kind))
        return result

    # ---------- общее ----------
    def _hidden(self) -> tuple[set[str], set[str]]:
        if self._store is None:
            return set(), set()
        try:
            return self._store.hidden_tracks(), self._store.hidden_artists()
        except Exception:  # noqa: BLE001
            logger.debug('Рекомендации: список скрытого не прочитался', exc_info=True)
            return set(), set()

    def _prepare(self, tracks, exclude: set[str], limit: int, kind: str) -> list[Track]:
        """Отсеять скрытое, недавнее и повторы, проставить происхождение."""
        if limit <= 0:
            return []
        hidden, artists = self._hidden()
        skip = set(exclude) | hidden
        try:
            if self._store is not None:
                skip |= set(self._store.played_uids(8))
        except Exception:  # noqa: BLE001
            logger.debug('Рекомендации: недавнее не прочиталось', exc_info=True)
        result: list[Track] = []
        seen: set[str] = set()
        for track in tracks or ():
            if track is None or track.uid in skip or track.uid in seen:
                continue
            if (track.artist or '').strip().lower() in artists:
                continue
            seen.add(track.uid)
            track.meta[PROVENANCE] = kind
            result.append(track)
            if len(result) >= limit:
                break
        return result
