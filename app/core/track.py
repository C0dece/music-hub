"""Единая модель трека для всего приложения.

Один и тот же трек может прийти из VK (аудиозапись), с YouTube (ролик) или лежать
файлом на диске. Интерфейс не должен знать, чем они отличаются: плеер, очередь,
избранное и «+ VK» работают с Track, а особенности источников остаются в
VkClient и ytdlp_engine.

Track намеренно не зависит ни от Qt, ни от базы: ту же структуру сможет
использовать будущий клиент на другой платформе (см. docs/android_plan.md)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# Что считаем музыкой, а что роликом: делит и сканер библиотеки, и плеер
AUDIO_EXTS = {'.mp3', '.m4a', '.opus', '.ogg', '.flac', '.wav', '.aac', '.wma'}
VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v', '.flv'}

SOURCE_VK = 'vk'
SOURCE_YOUTUBE = 'youtube'
SOURCE_LOCAL = 'local'

# Что показывать рядом с названием - коротко, чтобы влезало в узкую панель плеера
SOURCE_LABELS = {SOURCE_VK: 'VK', SOURCE_YOUTUBE: 'YouTube', SOURCE_LOCAL: 'Файл'}


@dataclass
class Track:
    """Трек любого источника.

    `uid` - устойчивый ключ внутри приложения: он же ключ в базе, в очереди и в
    избранном. Для VK это `vk:<owner>_<id>`, для YouTube `youtube:<video_id>`,
    для файла `local:<путь>`."""

    source: str
    source_id: str = ''
    title: str = ''
    artist: str = ''
    duration: int = 0                 # секунды, 0 - неизвестно
    url: str = ''                     # страница источника, не прямая ссылка на файл
    cover: str = ''                   # обложка: адрес или локальный путь
    local_path: str = ''              # файл на диске, если он есть

    # Координаты VK: нужны и для воспроизведения, и для audio.add
    vk_owner_id: int | None = None
    vk_audio_id: int | None = None
    vk_access_key: str = ''

    youtube_id: str = ''

    # Служебное: `_full_id` VK для resolve_url, канал YouTube, сырые поля источника
    meta: dict = field(default_factory=dict)

    # Состояние, которое считает приложение, а не источник
    in_vk: bool = False

    @property
    def uid(self) -> str:
        return f'{self.source}:{self.source_id}'

    @property
    def is_video(self) -> bool:
        """Есть ли что показывать, кроме обложки: ролик YouTube или файл-видео."""
        if self.source == SOURCE_YOUTUBE:
            return True
        path = self.local_path or (self.source_id if self.source == SOURCE_LOCAL else '')
        return os.path.splitext(path)[1].lower() in VIDEO_EXTS

    @property
    def display_title(self) -> str:
        if self.artist and self.title:
            return f'{self.artist} · {self.title}'
        return self.title or self.artist or self.url or 'Без названия'

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def playable(self) -> bool:
        """Есть ли чем играть трек прямо сейчас (сетевые сбои тут не учитываются)."""
        if self.local_path and os.path.exists(self.local_path):
            return True
        if self.source == SOURCE_VK:
            return bool(self.meta.get('_full_id') or self.meta.get('direct_url'))
        if self.source == SOURCE_YOUTUBE:
            return bool(self.youtube_id)
        return False

    @property
    def cached(self) -> bool:
        return bool(self.local_path) and os.path.exists(self.local_path)

    def with_local_path(self, path: str) -> 'Track':
        return replace(self, local_path=path)

    def to_row(self) -> dict:
        """Плоский словарь для базы (см. app/core/store.py)."""
        return {
            'uid': self.uid,
            'source': self.source,
            'source_id': self.source_id,
            'title': self.title,
            'artist': self.artist,
            'duration': self.duration,
            'url': self.url,
            'cover': self.cover,
            'local_path': self.local_path,
            'vk_owner_id': self.vk_owner_id,
            'vk_audio_id': self.vk_audio_id,
            'vk_access_key': self.vk_access_key,
            'youtube_id': self.youtube_id,
        }

    @classmethod
    def from_row(cls, row) -> 'Track':
        data = dict(row)
        meta = data.pop('meta', None)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known.setdefault('source', SOURCE_LOCAL)
        track = cls(**known)
        if isinstance(meta, dict):
            track.meta.update(meta)
        if track.source == SOURCE_VK:
            track.in_vk = True
        return track


# ---------- сборка Track из данных источников ----------

def from_vk(row: dict) -> Track:
    """Трек из словаря VkClient (`_row_to_track`) или из ответа audio.* API."""
    owner_id = row.get('owner_id')
    audio_id = row.get('id')
    meta = {}
    if row.get('_full_id'):
        meta['_full_id'] = list(row['_full_id'])
    if row.get('url'):
        meta['direct_url'] = row['url']
    return Track(
        source=SOURCE_VK,
        source_id=f'{owner_id}_{audio_id}',
        title=(row.get('title') or '').strip(),
        artist=(row.get('artist') or '').strip(),
        duration=int(row.get('duration') or 0),
        url=f'https://vk.com/audio{owner_id}_{audio_id}',
        vk_owner_id=int(owner_id) if owner_id is not None else None,
        vk_audio_id=int(audio_id) if audio_id is not None else None,
        vk_access_key=row.get('access_key') or '',
        cover=row.get('cover') or '',
        meta=meta,
        in_vk=True,
    )


def to_vk_row(track: Track) -> dict:
    """Обратное преобразование: resolve_url/download_track ждут словарь VkClient."""
    return {
        'id': track.vk_audio_id,
        'owner_id': track.vk_owner_id,
        'artist': track.artist,
        'title': track.title,
        'duration': track.duration,
        'url': track.meta.get('direct_url'),
        '_full_id': tuple(track.meta.get('_full_id') or ()),
    }


def from_youtube(entry: dict) -> Track:
    """Трек из записи yt-dlp (`extract_entries`) или из разобранной ссылки."""
    video_id = entry.get('id') or ''
    channel = entry.get('uploader') or entry.get('channel') or ''
    artist, title = split_artist_title(entry.get('title') or '', channel)
    return Track(
        source=SOURCE_YOUTUBE,
        source_id=video_id,
        title=title,
        artist=artist,
        duration=int(entry.get('duration') or 0),
        url=entry.get('webpage_url') or entry.get('original_url') or entry.get('url') or (
            f'https://www.youtube.com/watch?v={video_id}' if video_id else ''),
        cover=entry.get('thumbnail') or (
            f'https://i.ytimg.com/vi/{video_id}/mqdefault.jpg' if video_id else ''),
        youtube_id=video_id,
        meta={'channel': channel, 'raw_title': entry.get('title') or ''},
    )


def from_local(path: str, name: str = '', tags: dict | None = None) -> Track:
    """Трек из файла на диске.

    `tags` - то, что прочитал core/tags.py (может быть пустым). Сами теги здесь не
    читаем: модель не должна ходить на диск, а вызывающая сторона знает, стоит ли
    тратить на это время (см. library.audio_track)."""
    base = name or os.path.splitext(os.path.basename(path))[0]
    artist, title = split_artist_title(base, '')
    tags = tags or {}
    meta = {}
    if tags.get('album'):
        # Альбом нужен только для показа, отдельной колонки в базе он не стоит
        meta['album'] = tags['album']
    return Track(
        source=SOURCE_LOCAL,
        source_id=os.path.normcase(os.path.abspath(path)),
        title=tags.get('title') or title,
        artist=tags.get('artist') or artist,
        duration=int(tags.get('duration') or 0),
        cover=tags.get('cover') or '',
        local_path=path,
        meta=meta,
    )


# Разделители «исполнитель - название» в порядке убывания надёжности
_SEPARATORS = (' — ', ' – ', ' -- ', ' - ')


def split_artist_title(raw: str, channel: str = '') -> tuple[str, str]:
    """«Deftones - Sextape» -> ('Deftones', 'Sextape').

    Без разделителя исполнителем считаем канал: у музыкальных каналов YouTube
    («… - Topic») там как раз исполнитель."""
    raw = (raw or '').strip()
    for sep in _SEPARATORS:
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    channel = (channel or '').strip()
    if channel.endswith(' - Topic'):
        channel = channel[:-len(' - Topic')].strip()
    return channel, raw
