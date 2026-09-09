"""Единая волна: одна очередь, собранная сразу из нескольких источников.

Смысл раздела «Микс» - слушать не «музыку VK» и не «YouTube», а просто музыку:
человек задаёт доли источников (или отдаёт выбор программе), а дальше треки идут
вперемешку. Ничего своего этот модуль не качает и не ищет: он только просит уже
существующие части - `VkClient`, `Recommender`/`Discovery` и обход папок в
`library` - и склеивает ответы в один список.

Qt здесь нет: микс собирается в фоновом потоке (`run_async`) и проверяется
обычными тестами. Наружу уходит `MixResult` - треки, сколько дал каждый источник
и честные замечания («вход в VK не выполнен», «YouTube не ответил»), потому что
молча подменять один источник другим нельзя.
"""
from __future__ import annotations

import logging
import math
import random
import time

from .matcher import normalized_key
from .recommendations import PROVENANCE
from .track import SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE, Track, from_local, from_vk
from .youtube.discovery import KIND_FALLBACK, KIND_VK_RECOMS

logger = logging.getLogger(__name__)

# Что берём за основу подборки
MODE_KNOWN = 'known'        # знакомое: фонотека VK, свои файлы, история
MODE_MIXED = 'mixed'        # поровну знакомое и новое
MODE_DISCOVER = 'discover'  # новое: лента YouTube Music и поиск по любимым исполнителям

MODE_LABELS = {
    MODE_KNOWN: 'Знакомое',
    MODE_MIXED: 'Знакомое и новое',
    MODE_DISCOVER: 'Новое',
}

SOURCE_ORDER = (SOURCE_VK, SOURCE_YOUTUBE, SOURCE_LOCAL)
SOURCE_TITLES = {SOURCE_VK: 'Музыка VK', SOURCE_YOUTUBE: 'YouTube',
                 SOURCE_LOCAL: 'Свои файлы'}

# Доли по умолчанию: VK - фонотека, YouTube - где ищут новое, файлы отдельно
# просить не приходится, они и так есть в VK, поэтому по умолчанию выключены
DEFAULT_WEIGHTS = {SOURCE_VK: 50, SOURCE_YOUTUBE: 50, SOURCE_LOCAL: 0}

DEFAULT_LIMIT = 60
MIN_LIMIT, MAX_LIMIT = 10, 300

# Насколько недавнее считаем «только что игравшим», когда его просят не повторять
RECENT_DEPTH = 40
# Сколько исполнителей из истории берём как затравку для «нового»
SEED_ARTISTS = 4
# Свежесть тяжёлых списков: фонотека VK и обход папок за одно слушание не меняются
POOL_TTL = 300.0


class MixConfig:
    """Настройки микса. Хранится в settings.json под ключом `mix`."""

    def __init__(self, weights: dict | None = None, mode: str = MODE_MIXED,
                 query: str = '', limit: int = DEFAULT_LIMIT, shuffle: bool = True,
                 skip_recent: bool = True, unique_songs: bool = True,
                 autoplay: bool = True, vk_recoms: bool = True):
        self.weights = {source: _weight((weights or {}).get(source, DEFAULT_WEIGHTS[source]))
                        for source in SOURCE_ORDER}
        self.mode = mode if mode in MODE_LABELS else MODE_MIXED
        self.query = (query or '').strip()
        self.limit = max(MIN_LIMIT, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        self.shuffle = bool(shuffle)
        self.skip_recent = bool(skip_recent)
        self.unique_songs = bool(unique_songs)
        self.autoplay = bool(autoplay)
        # Брать ли волны и рекомендации VK. Выключается, когда человек хочет
        # слышать только свою фонотеку и ничего сверх неё
        self.vk_recoms = bool(vk_recoms)

    @property
    def sources(self) -> list[str]:
        """Источники, которые человек включил, - вес больше нуля."""
        return [source for source in SOURCE_ORDER if self.weights.get(source, 0) > 0]

    def share(self, source: str) -> int:
        """Доля источника в процентах - то, что видно в интерфейсе."""
        total = sum(self.weights.get(s, 0) for s in self.sources)
        if not total or source not in self.sources:
            return 0
        return round(self.weights[source] * 100 / total)

    def to_dict(self) -> dict:
        return {'weights': dict(self.weights), 'mode': self.mode, 'query': self.query,
                'limit': self.limit, 'shuffle': self.shuffle,
                'skip_recent': self.skip_recent, 'unique_songs': self.unique_songs,
                'autoplay': self.autoplay, 'vk_recoms': self.vk_recoms}

    @classmethod
    def from_dict(cls, data) -> 'MixConfig':
        """Настройки из файла. Чего нет или что испорчено - берётся по умолчанию."""
        if not isinstance(data, dict):
            return cls()
        weights = data.get('weights')
        return cls(weights=weights if isinstance(weights, dict) else None,
                   mode=data.get('mode', MODE_MIXED),
                   query=data.get('query', ''),
                   limit=_int(data.get('limit'), DEFAULT_LIMIT),
                   shuffle=data.get('shuffle', True),
                   skip_recent=data.get('skip_recent', True),
                   unique_songs=data.get('unique_songs', True),
                   autoplay=data.get('autoplay', True),
                   vk_recoms=data.get('vk_recoms', True))


def _labelled(rows, kind: str) -> list[Track]:
    """Треки VK с пометкой происхождения.

    Метка живёт в meta и видна в подсказке и журнале - интерфейс она не украшает,
    но и не даёт спутать подборку VK с обычным поиском."""
    tracks = [from_vk(row) for row in rows]
    if kind:
        for track in tracks:
            track.meta[PROVENANCE] = kind
    return tracks


class MixResult:
    """Готовая волна: треки, вклад каждого источника и что пошло не так."""

    def __init__(self, tracks: list[Track], counts: dict, notes: list[str]):
        self.tracks = tracks
        self.counts = counts
        self.notes = notes

    def __len__(self) -> int:
        return len(self.tracks)

    @property
    def summary(self) -> str:
        """Строка для панели: «48 треков · VK 24 · YouTube 24»."""
        if not self.tracks:
            return 'Пусто'
        parts = [f'{len(self.tracks)} треков']
        parts += [f'{SOURCE_TITLES[source]} {self.counts[source]}'
                  for source in SOURCE_ORDER if self.counts.get(source)]
        return ' · '.join(parts)


class Mixer:
    """Сборщик микса. Все методы ходят в сеть и на диск - только из фона."""

    def __init__(self, store=None, recommender=None, vk_client_provider=None,
                 files_provider=None):
        self._store = store
        self._recommender = recommender
        self._vk = vk_client_provider
        self._files = files_provider
        # Тяжёлые списки (фонотека VK, обход папок) держим недолго: пересобрать
        # микс - обычное дело, а лезть за полутора тысячами треков каждый раз не нужно
        self._cache: dict[str, tuple[float, list]] = {}
        self._rng = random.Random()

    # ---------- что вообще доступно ----------
    def available(self) -> dict[str, bool]:
        return {
            SOURCE_VK: self._client() is not None,
            SOURCE_YOUTUBE: self._recommender is not None,
            SOURCE_LOCAL: self._files is not None,
        }

    def auto_config(self) -> MixConfig:
        """«На усмотрение программы»: включить всё, что сейчас работает.

        Файлы добавляем маленькой долей - это тот же материал, что и в VK,
        и в большом количестве он только вытеснит остальное."""
        available = self.available()
        weights = {SOURCE_VK: 50 if available[SOURCE_VK] else 0,
                   SOURCE_YOUTUBE: 50 if available[SOURCE_YOUTUBE] else 0,
                   SOURCE_LOCAL: 0}
        if not any(weights.values()) and available[SOURCE_LOCAL]:
            weights[SOURCE_LOCAL] = 100
        elif available[SOURCE_LOCAL] and available[SOURCE_VK]:
            # Немного своего, чтобы попадалось скачанное и то, чего нет в сети
            weights[SOURCE_LOCAL] = 10
        return MixConfig(weights=weights, mode=MODE_MIXED)

    def forget(self) -> None:
        """Забыть отложенные списки - например, после входа в VK."""
        self._cache.clear()

    def continuation(self, config: MixConfig):
        """Чем продолжать волну, когда очередь подходит к концу.

        Плеер ждёт `fn(seed, exclude, limit)` и зовёт её из фона. Продолжение
        собираем тем же миксом: иначе бесконечная волна незаметно съехала бы
        в один источник - тот, которым плеер продолжает очередь обычно."""
        def extend(_seed, exclude, limit):
            follow = MixConfig.from_dict(config.to_dict())
            follow.limit = max(MIN_LIMIT, min(_int(limit, MIN_LIMIT), MAX_LIMIT))
            skip = set(exclude or ())
            return [t for t in self.build(follow).tracks if t.uid not in skip]

        return extend

    # ---------- сборка ----------
    def build(self, config: MixConfig) -> MixResult:
        notes: list[str] = []
        sources = [s for s in config.sources if self.available().get(s)]
        for source in config.sources:
            if source not in sources:
                notes.append(f'{SOURCE_TITLES[source]}: источник сейчас недоступен')
        if not sources:
            return MixResult([], {}, notes or ['Не выбрано ни одного источника'])

        blocked = self._blocked(config)
        pools: dict[str, list[Track]] = {}
        for source in sources:
            need = self._need(config, source)
            try:
                tracks = self._pool(source, config, need, notes)
            except Exception as exc:  # noqa: BLE001 - один источник не должен ронять микс
                logger.warning('Микс: %s не ответил: %s', source, exc)
                notes.append(f'{SOURCE_TITLES[source]}: {exc}')
                tracks = []
            pools[source] = tracks

        pools = self._clean(pools, config, blocked)
        empty = [SOURCE_TITLES[s] for s in sources if not pools.get(s)]
        for title in empty:
            if not any(title in note for note in notes):
                notes.append(f'{title}: подходящих треков не нашлось')

        weights = {s: config.weights[s] for s in sources if pools.get(s)}
        tracks = _interleave(pools, weights, config.limit)
        counts = {source: sum(1 for t in tracks if t.source == source)
                  for source in SOURCE_ORDER}
        return MixResult(tracks, counts, notes)

    # ---------- источники ----------
    def _pool(self, source: str, config: MixConfig, need: int,
              notes: list[str]) -> list[Track]:
        known_need, new_need = _split(need, config.mode)
        if source == SOURCE_VK:
            return self._vk_pool(config, known_need, new_need, notes)
        if source == SOURCE_YOUTUBE:
            return self._youtube_pool(config, known_need, new_need, notes)
        return self._local_pool(config, need)

    def _vk_pool(self, config: MixConfig, known_need: int, new_need: int,
                 notes: list[str]) -> list[Track]:
        client = self._client()
        if client is None:
            notes.append('Музыка VK: вход не выполнен')
            return []
        if config.query:
            # Кириллицу в поиск VK шлёт сам VkClient - транслитом, иначе ответ мусорный
            rows = client.search_tracks(config.query, (known_need + new_need) * 2)
            return _labelled(rows, KIND_FALLBACK)

        tracks: list[Track] = []
        if known_need:
            library = self._cached('vk_library', lambda: client.get_my_tracks())
            if not library:
                notes.append('Музыка VK: фонотека пуста')
            tracks += self._sample(_labelled(library, ''), known_need * 2)
        if new_need:
            found = self._vk_new(client, config, new_need * 2, notes)
            if not found and not tracks:
                notes.append('Музыка VK: новое искать не по чему, нет истории')
            tracks += found
        return tracks

    def _vk_new(self, client, config: MixConfig, need: int,
                notes: list[str]) -> list[Track]:
        """«Новое» из VK: сперва подборка самого VK, потом - поиск.

        Порядок важен ровно потому, что это разные вещи. Поиск по своим же
        исполнителям - не рекомендация, и выдавать его за неё нельзя (AGENTS.md),
        поэтому у каждой ветки своя метка, а замена названа вслух в примечаниях.
        """
        if config.vk_recoms:
            try:
                rows = self._cached('vk_recoms',
                                    lambda: client.recommended_tracks(need))
            except Exception as exc:  # noqa: BLE001 - микс важнее одного источника
                logger.debug('Микс: рекомендации VK недоступны: %s', exc)
                rows = []
            if rows:
                return _labelled(rows, KIND_VK_RECOMS)
            notes.append('Музыка VK: своих рекомендаций нет, подобрано поиском')
        return self._vk_by_artists(client, need)

    def _vk_by_artists(self, client, need: int) -> list[Track]:
        """Запасной путь: поиск по тем, кого человек уже слушает.

        Это именно поиск, поэтому треки уезжают с меткой `KIND_FALLBACK` -
        по ней видно в подсказке и в журнале, что подборка не от VK.
        """
        artists = self._top_artists()
        if not artists:
            return []
        self._rng.shuffle(artists)
        per_artist = max(5, math.ceil(need / len(artists)))
        tracks: list[Track] = []
        for artist in artists:
            try:
                rows = client.search_tracks(artist, per_artist)
            except Exception as exc:  # noqa: BLE001 - один исполнитель не важнее микса
                logger.debug('Микс: поиск в VK по «%s» не удался: %s', artist, exc)
                continue
            tracks += _labelled(rows, KIND_FALLBACK)
            if len(tracks) >= need:
                break
        return tracks

    def _youtube_pool(self, config: MixConfig, known_need: int, new_need: int,
                      notes: list[str]) -> list[Track]:
        rec = self._recommender
        if rec is None:
            return []
        if config.query:
            # artist_radio - это поиск с той же чисткой скрытого, что и у радио
            return rec.artist_radio(config.query, (known_need + new_need) * 2)

        tracks: list[Track] = []
        if known_need:
            # Затравки-трека здесь нет: продолжение строится по истории
            tracks += rec.autoplay(None, set(), known_need * 2)
        if new_need:
            fresh: list[Track] = []
            for section in rec.sections(max(8, new_need)):
                fresh += section.tracks
                if not section.genuine and 'YouTube: лента недоступна, взят поиск' not in notes:
                    # Подменять ленту поиском молча нельзя - так же честно, как в подборках
                    notes.append('YouTube: лента недоступна, взят поиск')
            tracks += self._sample(fresh, new_need * 2)
        if not tracks:
            notes.append('YouTube: рекомендации не пришли')
        return tracks

    def _local_pool(self, config: MixConfig, need: int) -> list[Track]:
        files = self._cached('files', self._files)
        audio = [f for f in files if getattr(f, 'kind', 'audio') == 'audio']
        tracks = [from_local(media.path, media.name) for media in audio]
        if config.query:
            needle = normalized_key('', config.query)
            tracks = [t for t in tracks if needle in normalized_key(t.artist, t.title)]
        return self._sample(tracks, need * 2)

    # ---------- общее ----------
    def _client(self):
        return self._vk() if callable(self._vk) else self._vk

    def _cached(self, key: str, loader) -> list:
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < POOL_TTL:
            return cached[1]
        value = list(loader() or ()) if loader is not None else []
        self._cache[key] = (time.monotonic(), value)
        return value

    def _sample(self, tracks: list[Track], need: int) -> list[Track]:
        """Взять кусок списка. Случайный - иначе микс каждый раз начинался бы
        с одних и тех же треков, стоящих в фонотеке первыми."""
        tracks = list(tracks)
        if need <= 0 or len(tracks) <= need:
            self._rng.shuffle(tracks)
            return tracks
        return self._rng.sample(tracks, need)

    def _need(self, config: MixConfig, source: str) -> int:
        """Сколько треков просить у источника: доля от длины плюс запас на повторы."""
        total = sum(config.weights[s] for s in config.sources) or 1
        share = config.weights[source] / total
        return max(8, math.ceil(config.limit * share) + 6)

    def _top_artists(self) -> list[str]:
        if self._store is None:
            return []
        try:
            return [a for a in self._store.top_artists(SEED_ARTISTS) if a]
        except Exception:  # noqa: BLE001 - без истории просто нет затравок
            logger.debug('Микс: история не прочиталась', exc_info=True)
            return []

    def _blocked(self, config: MixConfig) -> tuple[set[str], set[str]]:
        """Что не должно попасть в микс: скрытое и, если просили, недавнее."""
        uids: set[str] = set()
        artists: set[str] = set()
        if self._store is None:
            return uids, artists
        try:
            uids |= self._store.hidden_tracks()
            artists |= {a.strip().lower() for a in self._store.hidden_artists()}
            if config.skip_recent:
                uids |= set(self._store.played_uids(RECENT_DEPTH))
        except Exception:  # noqa: BLE001
            logger.debug('Микс: список скрытого не прочитался', exc_info=True)
        return uids, artists

    def _clean(self, pools: dict, config: MixConfig,
               blocked: tuple[set[str], set[str]]) -> dict:
        """Убрать скрытое, недавнее и повторы - в том числе одну песню из разных
        источников: «та же песня, но с YouTube» подряд слушается как заедание."""
        hidden_uids, hidden_artists = blocked
        seen_uids: set[str] = set()
        seen_keys: set[str] = set()
        # Сначала чистим пул источника с большей долей: за ним и остаётся песня,
        # если она есть и в VK, и на YouTube
        order = sorted(pools, key=lambda s: config.weights.get(s, 0), reverse=True)
        result: dict[str, list[Track]] = {}
        for source in order:
            kept: list[Track] = []
            for track in pools[source]:
                if track is None or track.uid in hidden_uids or track.uid in seen_uids:
                    continue
                if (track.artist or '').strip().lower() in hidden_artists:
                    continue
                key = normalized_key(track.artist, track.title)
                if config.unique_songs and key.strip('|') and key in seen_keys:
                    continue
                seen_uids.add(track.uid)
                seen_keys.add(key)
                kept.append(track)
            if config.shuffle:
                self._rng.shuffle(kept)   # без этого порядок источника остаётся как есть
            result[source] = kept
        return {source: result[source] for source in pools}


def _interleave(pools: dict, weights: dict, limit: int) -> list[Track]:
    """Разложить треки источников по долям, не сваливая их кучами.

    Плавное взвешенное чередование (как в nginx): у каждого источника счётчик,
    на каждом шаге он растёт на вес, играет наибольший, у победителя счётчик
    уменьшается на сумму весов. Так при долях 50/50 источники идут через один,
    а при 70/30 редкий не собирается в один хвост."""
    queues = {source: list(tracks) for source, tracks in pools.items() if tracks}
    counters = {source: 0 for source in queues}
    result: list[Track] = []
    while queues and len(result) < limit:
        total = sum(max(1, weights.get(source, 1)) for source in queues)
        for source in queues:
            counters[source] += max(1, weights.get(source, 1))
        pick = max(queues, key=lambda s: counters[s])
        counters[pick] -= total
        result.append(queues[pick].pop(0))
        if not queues[pick]:
            del queues[pick]
    return result


def _split(need: int, mode: str) -> tuple[int, int]:
    """Сколько знакомого и сколько нового просить у источника."""
    if mode == MODE_KNOWN:
        return need, 0
    if mode == MODE_DISCOVER:
        return 0, need
    known = need // 2
    return known, need - known


def _weight(value) -> int:
    try:
        return max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return 0


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
