"""Локальная база музыкального раздела: треки, связки YouTube↔VK, избранное,
история прослушивания, свои плейлисты.

Зачем база, а не ещё один JSON: связок и истории прослушивания со временем
становится тысячи, и каждый раз переписывать весь файл целиком (как это делает
`history.py`) уже дорого, а искать по нему - неудобно. Берём `sqlite3` из
стандартной библиотеки: отдельной зависимости не нужно, файл лежит рядом с
остальными настройками.

Скачанные файлы и история загрузок остаются там же, где были (`config/history.json`,
папки «музыка»/«видео») - этот модуль их не трогает и не дублирует. Библиотеку VK
целиком сюда тоже не копируем: в базе только то, чего у VK нет."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

from .. import config
from .matcher import normalized_key
from .track import SOURCE_VK, Track

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4

# Избранное живёт как системный плейлист: одна модель данных вместо двух.
FAVORITES_TITLE = 'Любимое'
FAVORITES_EXT_ID = 'favorites'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    uid            TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    title          TEXT DEFAULT '',
    artist         TEXT DEFAULT '',
    duration       INTEGER DEFAULT 0,
    url            TEXT DEFAULT '',
    cover          TEXT DEFAULT '',
    local_path     TEXT DEFAULT '',
    vk_owner_id    INTEGER,
    vk_audio_id    INTEGER,
    vk_access_key  TEXT DEFAULT '',
    youtube_id     TEXT DEFAULT '',
    norm_key       TEXT DEFAULT '',
    meta           TEXT DEFAULT '{}',
    added_at       REAL DEFAULT 0,
    -- «Моя музыка»: когда трек добавлен в фонотеку (0 - просто попадался в выдаче).
    -- Флагом, а не ещё одним системным плейлистом: порядок здесь не важен, зато
    -- важно уметь фильтровать и сортировать, а треки из обычных плейлистов должны
    -- считаться добавленными без второй записи о них.
    saved_at       REAL DEFAULT 0,
    -- Когда сделана офлайн-копия (см. core/offline.py). Сам путь лежит в local_path,
    -- отдельная метка нужна, чтобы отличать нашу копию от файла пользователя.
    cached_at      REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_norm ON tracks(norm_key);
-- Индекс по saved_at создаёт _migrate, а не этот скрипт: в старой базе колонки
-- ещё нет, а CREATE TABLE IF NOT EXISTS её не добавит - скрипт упал бы на индексе.

-- Связка «трек источника → аудиозапись VK». Главное, ради чего заведена база:
-- второй раз тот же ролик в VK уже не поедет.
CREATE TABLE IF NOT EXISTS mappings (
    uid            TEXT PRIMARY KEY,
    vk_owner_id    INTEGER,
    vk_audio_id    INTEGER,
    vk_access_key  TEXT DEFAULT '',
    method         TEXT DEFAULT '',      -- 'match' (нашли готовую) или 'upload' (залили свою)
    created_at     REAL DEFAULT 0
);

-- Таблицы favorites больше нет: избранное - системный плейлист «Любимое»
-- (playlists.kind = 'system'). Старые базы переносит _migrate_favorites.

-- История прослушивания. Записывается не по факту нажатия «играть», а когда трек
-- реально слушали (см. player_controller: больше 30 с или больше 20 % длины), иначе
-- перещёлкивание очереди засоряло бы историю случайными строками.
CREATE TABLE IF NOT EXISTS play_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    uid       TEXT NOT NULL,
    played_at REAL DEFAULT 0,
    source    TEXT DEFAULT '',
    listened  INTEGER DEFAULT 0,   -- сколько секунд реально слушали
    finished  INTEGER DEFAULT 0    -- 1 - доиграл до конца, 0 - переключили
);
CREATE INDEX IF NOT EXISTS idx_play_uid ON play_history(uid);
CREATE INDEX IF NOT EXISTS idx_play_at ON play_history(played_at);

-- Локальные «не нравится»: рекомендации, радио и автоплей обязаны их уважать.
CREATE TABLE IF NOT EXISTS hidden_tracks (
    uid       TEXT PRIMARY KEY,
    added_at  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hidden_artists (
    name      TEXT PRIMARY KEY,    -- в нижнем регистре, без лишних пробелов
    added_at  REAL DEFAULT 0
);

-- Короткий кэш выдачи рекомендаций и разделов главной. Не вечный: срок годности
-- задаёт вызывающая сторона (см. cache_get).
CREATE TABLE IF NOT EXISTS rec_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlists (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    kind            TEXT DEFAULT 'user',  -- user / system: системные нельзя удалить
    source          TEXT DEFAULT 'hub',   -- hub / youtube: откуда плейлист родом
    ext_id          TEXT DEFAULT '',      -- идентификатор во внешнем сервисе (list=...)
    vk_owner_id     INTEGER,
    vk_playlist_id  INTEGER,
    vk_access_hash  TEXT DEFAULT '',
    created_at      REAL DEFAULT 0,
    updated_at      REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id  INTEGER NOT NULL,
    uid          TEXT NOT NULL,
    position     INTEGER DEFAULT 0,
    added_at     REAL DEFAULT 0,
    PRIMARY KEY (playlist_id, uid)
);

CREATE TABLE IF NOT EXISTS state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

_TRACK_COLUMNS = ('uid', 'source', 'source_id', 'title', 'artist', 'duration', 'url',
                  'cover', 'local_path', 'vk_owner_id', 'vk_audio_id', 'vk_access_key',
                  'youtube_id')


class Store:
    """Одно соединение на всё приложение под общей блокировкой.

    Обращения идут и из фоновых задач (`run_async`), а sqlite3 по умолчанию не
    разрешает работать с соединением из другого потока. Своё соединение на поток
    здесь ни к чему: запросы короткие, а блокировка проще пула."""

    def __init__(self, path=None):
        self._path = str(path or config.DB_FILE)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self._migrate()

    # ---------- служебное ----------
    def _migrate(self) -> None:
        """Догнать старую базу до текущей схемы.

        CREATE TABLE IF NOT EXISTS новые колонки в уже существующей таблице не
        добавляет, поэтому недостающие дописываем вручную: ALTER TABLE ADD COLUMN
        в sqlite дешёвый и данные не трогает."""
        version = int(self.get_state('schema_version', '0') or 0)
        if version == SCHEMA_VERSION:
            return
        added = (('play_history', 'source', "TEXT DEFAULT ''"),
                 ('play_history', 'listened', 'INTEGER DEFAULT 0'),
                 ('play_history', 'finished', 'INTEGER DEFAULT 0'),
                 ('playlists', 'source', "TEXT DEFAULT 'hub'"),
                 ('playlists', 'ext_id', "TEXT DEFAULT ''"),
                 ('playlists', 'kind', "TEXT DEFAULT 'user'"),
                 ('playlist_items', 'added_at', 'REAL DEFAULT 0'),
                 ('tracks', 'saved_at', 'REAL DEFAULT 0'),
                 ('tracks', 'cached_at', 'REAL DEFAULT 0'))
        for table, column, decl in added:
            if column in self._columns(table):
                continue
            try:
                self._exec(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
            except sqlite3.OperationalError as exc:
                logger.warning('Store: не удалось добавить %s.%s: %s', table, column, exc)
        self._exec('CREATE INDEX IF NOT EXISTS idx_tracks_saved ON tracks(saved_at)')
        self._migrate_favorites()
        self._migrate_saved()
        self.set_state('schema_version', str(SCHEMA_VERSION))

    def _migrate_favorites(self) -> None:
        """Старое избранное (таблица favorites) - в системный плейлист «Любимое».

        Одно место вместо двух: сердечко больше не отдельная сущность, а строка
        плейлиста, поэтому «в избранное» и «добавить в плейлист» - одна механика."""
        if not self._query("SELECT name FROM sqlite_master WHERE type = 'table' "
                           "AND name = 'favorites'"):
            return
        rows = self._query('SELECT uid, added_at FROM favorites ORDER BY added_at')
        if rows:
            playlist_id = self.favorites_playlist_id()
            for position, row in enumerate(rows):
                self._exec('INSERT OR IGNORE INTO playlist_items '
                           '(playlist_id, uid, position, added_at) VALUES (?, ?, ?, ?)',
                           (playlist_id, row['uid'], position, row['added_at'] or 0.0))
        try:
            self._exec('DROP TABLE favorites')
        except sqlite3.OperationalError as exc:
            logger.warning('Store: не удалось убрать таблицу favorites: %s', exc)

    def _migrate_saved(self) -> None:
        """Всё, что лежит в плейлистах, - уже «Моя музыка».

        Иначе после обновления раздел оказался бы пустым, хотя избранное и свои
        плейлисты никуда не делись."""
        try:
            self._exec('UPDATE tracks SET saved_at = COALESCE(NULLIF(added_at, 0), ?) '
                       'WHERE saved_at <= 0 AND uid IN (SELECT uid FROM playlist_items)',
                       (time.time(),))
        except sqlite3.OperationalError as exc:
            logger.warning('Store: не удалось отметить треки плейлистов: %s', exc)

    def _columns(self, table: str) -> set[str]:
        try:
            return {row[1] for row in self._query(f'PRAGMA table_info({table})')}
        except sqlite3.Error:
            return set()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.commit()
                self._db.close()
            except sqlite3.Error:
                pass

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, params))

    # ---------- треки ----------
    def save_track(self, track: Track) -> None:
        """Запомнить трек. Повторный вызов обновляет поля, не плодя записей."""
        row = track.to_row()
        row['norm_key'] = normalized_key(track.artist, track.title)
        row['meta'] = json.dumps(track.meta, ensure_ascii=False)
        row['added_at'] = time.time()
        columns = list(row)
        placeholders = ', '.join('?' for _ in columns)
        updates = ', '.join(_update_expr(c) for c in columns if c not in ('uid', 'added_at'))
        self._exec(
            f'INSERT INTO tracks ({", ".join(columns)}) VALUES ({placeholders}) '
            f'ON CONFLICT(uid) DO UPDATE SET {updates}',
            [row[c] for c in columns])

    def save_tracks(self, tracks) -> None:
        for track in tracks:
            self.save_track(track)

    def get_track(self, uid: str) -> Track | None:
        rows = self._query('SELECT * FROM tracks WHERE uid = ?', (uid,))
        return _row_to_track(rows[0]) if rows else None

    def get_tracks(self, uids) -> dict[str, Track]:
        uids = list(uids)
        if not uids:
            return {}
        marks = ', '.join('?' for _ in uids)
        rows = self._query(f'SELECT * FROM tracks WHERE uid IN ({marks})', uids)
        return {row['uid']: _row_to_track(row) for row in rows}

    def alternatives(self, track: Track) -> list[Track]:
        """Тот же трек из других источников.

        Одна и та же песня живёт и в VK, и на YouTube, и файлом на диске. Ключ
        нормализованного имени уже считается при сохранении, поэтому «чем ещё это
        можно проиграть» - обычный запрос, а не отдельная таблица связок."""
        key = normalized_key(track.artist, track.title)
        if not key:
            return []
        rows = self._query('SELECT * FROM tracks WHERE norm_key = ? AND uid != ?',
                           (key, track.uid))
        return [_row_to_track(row) for row in rows]

    # ---------- связки с VK ----------
    def set_mapping(self, uid: str, vk_track: Track | None, method: str = '') -> None:
        """Запомнить, что трек уже есть в VK. `vk_track` может быть None: VK не
        всегда возвращает координаты залитой записи, но сам факт переноса важен."""
        self._exec(
            'INSERT INTO mappings (uid, vk_owner_id, vk_audio_id, vk_access_key, method, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET '
            'vk_owner_id=excluded.vk_owner_id, vk_audio_id=excluded.vk_audio_id, '
            'vk_access_key=excluded.vk_access_key, method=excluded.method',
            (uid,
             vk_track.vk_owner_id if vk_track else None,
             vk_track.vk_audio_id if vk_track else None,
             vk_track.vk_access_key if vk_track else '',
             method, time.time()))
        if vk_track is not None:
            self.save_track(vk_track)

    def get_mapping(self, uid: str) -> dict | None:
        rows = self._query('SELECT * FROM mappings WHERE uid = ?', (uid,))
        return dict(rows[0]) if rows else None

    def has_mapping(self, uid: str) -> bool:
        return bool(self._query('SELECT 1 FROM mappings WHERE uid = ? LIMIT 1', (uid,)))

    def mapped_uids(self, uids) -> set[str]:
        """Какие из переданных треков уже в VK - одним запросом, чтобы список
        результатов поиска не превращался в сотню обращений к базе."""
        uids = list(uids)
        if not uids:
            return set()
        marks = ', '.join('?' for _ in uids)
        rows = self._query(f'SELECT uid FROM mappings WHERE uid IN ({marks})', uids)
        return {row['uid'] for row in rows}

    def mapping_target(self, uid: str) -> Track | None:
        """Аудиозапись VK, на которую указывает связка (если координаты известны)."""
        mapping = self.get_mapping(uid)
        if not mapping or mapping.get('vk_audio_id') is None:
            return None
        return self.get_track(f'{SOURCE_VK}:{mapping["vk_owner_id"]}_{mapping["vk_audio_id"]}')

    def recent_imports(self, limit: int = 20) -> list[Track]:
        rows = self._query(
            'SELECT t.* FROM mappings m JOIN tracks t ON t.uid = m.uid '
            'ORDER BY m.created_at DESC LIMIT ?', (limit,))
        return [_row_to_track(row) for row in rows]

    # ---------- «Моя музыка» ----------
    # Фонотека - треки всех источников, отмеченные saved_at. Избранное и плейлисты
    # лежат внутри неё: добавление в плейлист ставит отметку автоматически.
    def save_to_library(self, track: Track) -> None:
        self.save_track(track)
        # Дату добавления не переписываем: сортировка «новые сверху» должна
        # переживать повторное сохранение того же трека
        self._exec('UPDATE tracks SET saved_at = ? WHERE uid = ? AND saved_at <= 0',
                   (time.time(), track.uid))

    def save_many_to_library(self, tracks) -> None:
        for track in tracks:
            self.save_to_library(track)

    def remove_from_library(self, uid: str) -> None:
        """Убрать из фонотеки. Из плейлистов трек при этом не вычищаем: удалять
        чужие списки по случайному нажатию нельзя - это делается там же, где они."""
        self._exec('UPDATE tracks SET saved_at = 0 WHERE uid = ?', (uid,))

    def is_saved(self, uid: str) -> bool:
        return bool(self._query('SELECT 1 FROM tracks WHERE uid = ? AND saved_at > 0',
                                (uid,)))

    def saved_uids(self, uids) -> set[str]:
        """Отметки сразу для списка - один запрос вместо запроса на строку."""
        uids = list(uids)
        if not uids:
            return set()
        marks = ', '.join('?' for _ in uids)
        rows = self._query(f'SELECT uid FROM tracks WHERE saved_at > 0 AND uid IN ({marks})',
                           uids)
        return {row['uid'] for row in rows}

    def saved_tracks(self, source: str = '', limit: int = 5000) -> list[Track]:
        """Фонотека, новые сверху. `source` - 'vk' | 'youtube' | 'local' или всё."""
        sql = 'SELECT * FROM tracks WHERE saved_at > 0'
        params: list = []
        if source:
            sql += ' AND source = ?'
            params.append(source)
        sql += ' ORDER BY saved_at DESC LIMIT ?'
        params.append(limit)
        return [_row_to_track(row) for row in self._query(sql, params)]

    def saved_count(self, source: str = '') -> int:
        sql = 'SELECT COUNT(*) AS n FROM tracks WHERE saved_at > 0'
        params: list = []
        if source:
            sql += ' AND source = ?'
            params.append(source)
        return int(self._query(sql, params)[0]['n'])

    # ---------- офлайн-копии ----------
    def set_local_path(self, uid: str, path: str, cached: bool = False) -> None:
        """Привязать файл к треку. `cached` - файл сделали мы (папка офлайна),
        значит его можно и удалить; чужие файлы приложение не трогает."""
        self._exec('UPDATE tracks SET local_path = ?, cached_at = ? WHERE uid = ?',
                   (path, time.time() if cached else 0.0, uid))

    def clear_local_path(self, uid: str) -> None:
        self._exec("UPDATE tracks SET local_path = '', cached_at = 0 WHERE uid = ?", (uid,))

    def cached_tracks(self) -> list[Track]:
        """Офлайн-копии, самые давние сверху - в таком порядке их и вычищают."""
        rows = self._query('SELECT * FROM tracks WHERE cached_at > 0 ORDER BY cached_at')
        return [_row_to_track(row) for row in rows]

    def cached_uids(self, uids) -> set[str]:
        uids = list(uids)
        if not uids:
            return set()
        marks = ', '.join('?' for _ in uids)
        rows = self._query(f'SELECT uid FROM tracks WHERE cached_at > 0 AND uid IN ({marks})',
                           uids)
        return {row['uid'] for row in rows}

    # ---------- избранное ----------
    # Избранное - системный плейлист «Любимое». Методы ниже оставлены фасадом:
    # вызывающему коду незачем знать, что под сердечком лежит обычный плейлист.
    def favorites_playlist_id(self, create: bool = True) -> int | None:
        rows = self._query("SELECT id FROM playlists WHERE kind = 'system' AND ext_id = ? "
                           'LIMIT 1', (FAVORITES_EXT_ID,))
        if rows:
            return int(rows[0]['id'])
        if not create:
            return None
        return self.create_playlist(FAVORITES_TITLE, ext_id=FAVORITES_EXT_ID, kind='system')

    def add_favorite(self, track: Track) -> None:
        self.add_to_playlist(self.favorites_playlist_id(), track)

    def remove_favorite(self, uid: str) -> None:
        playlist_id = self.favorites_playlist_id(create=False)
        if playlist_id is not None:
            self.remove_from_playlist(playlist_id, uid)

    def is_favorite(self, uid: str) -> bool:
        playlist_id = self.favorites_playlist_id(create=False)
        if playlist_id is None:
            return False
        return bool(self._query('SELECT 1 FROM playlist_items WHERE playlist_id = ? '
                                'AND uid = ? LIMIT 1', (playlist_id, uid)))

    def favorites(self, limit: int = 500) -> list[Track]:
        """Свежие сверху - в отличие от обычного плейлиста, где важен порядок."""
        playlist_id = self.favorites_playlist_id(create=False)
        if playlist_id is None:
            return []
        rows = self._query(
            'SELECT t.* FROM playlist_items i JOIN tracks t ON t.uid = i.uid '
            'WHERE i.playlist_id = ? ORDER BY i.added_at DESC, i.position DESC LIMIT ?',
            (playlist_id, limit))
        return [_row_to_track(row) for row in rows]

    def artist_tracks(self, artist: str, limit: int = 100) -> list[Track]:
        """Всё, что о исполнителе уже известно местной базе. Без сети."""
        name = (artist or '').strip().lower()
        if not name:
            return []
        rows = self._query(
            'SELECT * FROM tracks WHERE LOWER(artist) = ? ORDER BY title LIMIT ?',
            (name, limit))
        return [_row_to_track(row) for row in rows]

    # ---------- история прослушивания ----------
    def log_play(self, track: Track, listened: int = 0, finished: bool = False) -> None:
        """Записать прослушивание. `listened` - сколько секунд реально играло."""
        self.save_track(track)
        self._exec('INSERT INTO play_history (uid, played_at, source, listened, finished) '
                   'VALUES (?, ?, ?, ?, ?)',
                   (track.uid, time.time(), track.source, int(listened), int(bool(finished))))

    def history(self, limit: int = 200) -> list[dict]:
        """История событиями, без склейки одинаковых: раздел «История» показывает
        именно прослушивания, а не список уникальных треков."""
        rows = self._query(
            'SELECT p.id AS entry_id, p.played_at, p.listened, p.finished, t.* '
            'FROM play_history p JOIN tracks t ON t.uid = p.uid '
            'ORDER BY p.played_at DESC, p.id DESC LIMIT ?', (limit,))
        return [{'id': row['entry_id'], 'played_at': row['played_at'] or 0.0,
                 'listened': row['listened'] or 0, 'finished': bool(row['finished']),
                 'track': _row_to_track(row)} for row in rows]

    def delete_history_entry(self, entry_id: int) -> None:
        self._exec('DELETE FROM play_history WHERE id = ?', (entry_id,))

    def delete_history_uid(self, uid: str) -> None:
        self._exec('DELETE FROM play_history WHERE uid = ?', (uid,))

    def clear_history(self) -> None:
        self._exec('DELETE FROM play_history')

    def played_uids(self, limit: int = 50) -> list[str]:
        """Последние прослушанные uid - рекомендациям, чтобы не звать то же самое."""
        rows = self._query('SELECT uid FROM play_history ORDER BY played_at DESC, '
                           'id DESC LIMIT ?', (limit,))
        return [row['uid'] for row in rows]

    def top_artists(self, limit: int = 10) -> list[str]:
        """Кого слушают чаще: на этом строится «Для вас», даже когда внешний
        сервис рекомендаций недоступен."""
        rows = self._query(
            "SELECT t.artist AS artist, COUNT(*) AS plays FROM play_history p "
            "JOIN tracks t ON t.uid = p.uid WHERE t.artist != '' "
            "GROUP BY LOWER(t.artist) ORDER BY plays DESC LIMIT ?", (limit,))
        return [row['artist'] for row in rows]

    # ---------- «не нравится»: скрытые треки и исполнители ----------
    def hide_track(self, uid: str) -> None:
        self._exec('INSERT OR REPLACE INTO hidden_tracks (uid, added_at) VALUES (?, ?)',
                   (uid, time.time()))

    def unhide_track(self, uid: str) -> None:
        self._exec('DELETE FROM hidden_tracks WHERE uid = ?', (uid,))

    def hidden_tracks(self) -> set[str]:
        return {row['uid'] for row in self._query('SELECT uid FROM hidden_tracks')}

    def hide_artist(self, name: str) -> None:
        name = (name or '').strip().lower()
        if name:
            self._exec('INSERT OR REPLACE INTO hidden_artists (name, added_at) VALUES (?, ?)',
                       (name, time.time()))

    def unhide_artist(self, name: str) -> None:
        self._exec('DELETE FROM hidden_artists WHERE name = ?', ((name or '').strip().lower(),))

    def hidden_artists(self) -> set[str]:
        return {row['name'] for row in self._query('SELECT name FROM hidden_artists')}

    # ---------- короткий кэш рекомендаций ----------
    def cache_set(self, key: str, payload) -> None:
        self._exec('INSERT OR REPLACE INTO rec_cache (key, payload, created_at) '
                   'VALUES (?, ?, ?)',
                   (key, json.dumps(payload, ensure_ascii=False), time.time()))

    def cache_get(self, key: str, max_age: float = 900.0):
        """Значение, если оно не старше `max_age` секунд. Иначе None."""
        rows = self._query('SELECT payload, created_at FROM rec_cache WHERE key = ?', (key,))
        if not rows or time.time() - (rows[0]['created_at'] or 0) > max_age:
            return None
        try:
            return json.loads(rows[0]['payload'])
        except (TypeError, ValueError):
            return None

    def cache_clear(self, prefix: str = '') -> None:
        if prefix:
            self._exec('DELETE FROM rec_cache WHERE key LIKE ?', (prefix + '%',))
        else:
            self._exec('DELETE FROM rec_cache')

    def recent_plays(self, limit: int = 30) -> list[Track]:
        # time.time() на Windows тикает раз в ~16 мс, поэтому у двух подряд идущих
        # прослушиваний время совпадает - порядок доопределяем по номеру записи.
        rows = self._query(
            'SELECT t.*, MAX(p.played_at) AS last_play, MAX(p.rowid) AS last_row '
            'FROM play_history p JOIN tracks t ON t.uid = p.uid GROUP BY t.uid '
            'ORDER BY last_play DESC, last_row DESC LIMIT ?', (limit,))
        return [_row_to_track(row) for row in rows]

    # ---------- свои плейлисты ----------
    def create_playlist(self, title: str, vk_owner_id=None, vk_playlist_id=None,
                        vk_access_hash: str = '', source: str = 'hub',
                        ext_id: str = '', kind: str = 'user') -> int:
        now = time.time()
        cur = self._exec(
            'INSERT INTO playlists (title, kind, vk_owner_id, vk_playlist_id, vk_access_hash, '
            'source, ext_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (title, kind, vk_owner_id, vk_playlist_id, vk_access_hash, source, ext_id,
             now, now))
        return int(cur.lastrowid)

    def playlist_by_ext(self, source: str, ext_id: str) -> dict | None:
        """Уже импортированный внешний плейлист - чтобы не создавать его дважды."""
        rows = self._query('SELECT * FROM playlists WHERE source = ? AND ext_id = ?',
                           (source, ext_id))
        return dict(rows[0]) if rows else None

    def playlists(self, include_system: bool = True) -> list[dict]:
        """Системные («Любимое») идут первыми: это главный список пользователя."""
        where = '' if include_system else "WHERE kind != 'system' "
        rows = self._query('SELECT * FROM playlists ' + where +
                           "ORDER BY CASE WHEN kind = 'system' THEN 0 ELSE 1 END, title")
        return [dict(row) for row in rows]

    def get_playlist(self, playlist_id: int) -> dict | None:
        rows = self._query('SELECT * FROM playlists WHERE id = ?', (playlist_id,))
        return dict(rows[0]) if rows else None

    def rename_playlist(self, playlist_id: int, title: str) -> None:
        self._exec('UPDATE playlists SET title = ?, updated_at = ? WHERE id = ?',
                   (title, time.time(), playlist_id))

    def link_playlist(self, playlist_id: int, vk_owner_id, vk_playlist_id,
                      vk_access_hash: str = '') -> None:
        self._exec('UPDATE playlists SET vk_owner_id = ?, vk_playlist_id = ?, '
                   'vk_access_hash = ?, updated_at = ? WHERE id = ?',
                   (vk_owner_id, vk_playlist_id, vk_access_hash, time.time(), playlist_id))

    def delete_playlist(self, playlist_id: int) -> None:
        playlist = self.get_playlist(playlist_id)
        if playlist is not None and playlist.get('kind') == 'system':
            logger.debug('Store: системный плейлист %s удалять нельзя', playlist_id)
            return
        self._exec('DELETE FROM playlist_items WHERE playlist_id = ?', (playlist_id,))
        self._exec('DELETE FROM playlists WHERE id = ?', (playlist_id,))

    def add_to_playlist(self, playlist_id: int, track: Track) -> None:
        # Трек в плейлисте - это трек «Моей музыки»: иначе список и плейлисты
        # разошлись бы, а человек добавлял вроде бы одно и то же
        self.save_to_library(track)
        rows = self._query('SELECT COALESCE(MAX(position), -1) + 1 AS pos '
                           'FROM playlist_items WHERE playlist_id = ?', (playlist_id,))
        self._exec('INSERT OR IGNORE INTO playlist_items (playlist_id, uid, position, added_at) '
                   'VALUES (?, ?, ?, ?)',
                   (playlist_id, track.uid, int(rows[0]['pos']), time.time()))
        self._exec('UPDATE playlists SET updated_at = ? WHERE id = ?',
                   (time.time(), playlist_id))

    def remove_from_playlist(self, playlist_id: int, uid: str) -> None:
        self._exec('DELETE FROM playlist_items WHERE playlist_id = ? AND uid = ?',
                   (playlist_id, uid))

    def playlist_tracks(self, playlist_id: int) -> list[Track]:
        rows = self._query(
            'SELECT t.* FROM playlist_items i JOIN tracks t ON t.uid = i.uid '
            'WHERE i.playlist_id = ? ORDER BY i.position', (playlist_id,))
        return [_row_to_track(row) for row in rows]

    def set_playlist_order(self, playlist_id: int, uids) -> None:
        for position, uid in enumerate(uids):
            self._exec('UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND uid = ?',
                       (position, playlist_id, uid))

    # ---------- произвольное состояние ----------
    def set_state(self, key: str, value: str) -> None:
        self._exec('INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)', (key, value))

    def get_state(self, key: str, default: str = '') -> str:
        rows = self._query('SELECT value FROM state WHERE key = ?', (key,))
        return rows[0]['value'] if rows else default

    def set_json(self, key: str, value) -> None:
        self.set_state(key, json.dumps(value, ensure_ascii=False))

    def get_json(self, key: str, default=None):
        raw = self.get_state(key, '')
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            logger.debug('Store: значение %s повреждено, беру значение по умолчанию', key)
            return default


# Поля, которые нельзя затирать пустотой при повторном сохранении: тот же трек
# приходит из поиска без обложки и без файла, а офлайн-копию мы уже нашли.
_KEEP_IF_EMPTY = ('local_path', 'cover')


def _update_expr(column: str) -> str:
    if column in _KEEP_IF_EMPTY:
        return f"{column}=CASE WHEN excluded.{column} = '' OR excluded.{column} IS NULL "                f'THEN tracks.{column} ELSE excluded.{column} END'
    return f'{column}=excluded.{column}'


def _row_to_track(row: sqlite3.Row) -> Track:
    data = {key: row[key] for key in row.keys() if key in _TRACK_COLUMNS}
    data.pop('uid', None)
    meta = {}
    if 'meta' in row.keys() and row['meta']:
        try:
            meta = json.loads(row['meta'])
        except ValueError:
            meta = {}
    track = Track.from_row(data)
    track.meta.update(meta if isinstance(meta, dict) else {})
    return track


_instance: Store | None = None


def store() -> Store:
    """Единая база приложения. Создаётся при первом обращении."""
    global _instance
    if _instance is None:
        config.ensure_dirs()
        _instance = Store()
    return _instance


def close() -> None:
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None
