"""Очередь воспроизведения.

Это не очередь загрузок: та живёт в `download_manager.py` и занимается файлами.
Здесь - список того, что играть дальше, указатель на текущий трек и режим
перемешивания. Никакого Qt, чтобы очередь можно было проверить обычными тестами.

Про перемешивание. Список в очереди - это и есть порядок проигрывания: человек
видит в панели ровно то, что заиграет дальше. Поэтому при включении shuffle мы
тасуем сами треки, а исходный порядок откладываем в `_original` и возвращаем при
выключении. Тасуется только хвост после текущего трека: играющее не должно
перескочить, а уже сыгранное остаётся историей, по которой работает «назад»."""
from __future__ import annotations

import random

from .track import Track


class PlaybackQueue:
    """Список треков и позиция в нём. Позиция -1 означает «ничего не выбрано»."""

    def __init__(self):
        self._tracks: list[Track] = []
        self._index: int = -1
        self._shuffle = False
        # Логический порядок на время перемешивания. None - перемешивания нет.
        self._original: list[Track] | None = None

    # ---------- чтение ----------
    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    @property
    def index(self) -> int:
        return self._index

    def __len__(self) -> int:
        return len(self._tracks)

    def __bool__(self) -> bool:
        return bool(self._tracks)

    def current(self) -> Track | None:
        if 0 <= self._index < len(self._tracks):
            return self._tracks[self._index]
        return None

    def at(self, position: int) -> Track | None:
        if 0 <= position < len(self._tracks):
            return self._tracks[position]
        return None

    def index_of(self, uid: str) -> int:
        for position, track in enumerate(self._tracks):
            if track.uid == uid:
                return position
        return -1

    def has_next(self) -> bool:
        return self._index + 1 < len(self._tracks)

    def has_previous(self) -> bool:
        return self._index > 0

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    def contains(self, uid: str) -> bool:
        return self.index_of(uid) >= 0

    def upcoming(self, limit: int = 0) -> list[Track]:
        """Что заиграет дальше. Нужно автоплею: он смотрит, сколько осталось."""
        rest = self._tracks[self._index + 1:] if self._index >= 0 else list(self._tracks)
        return rest[:limit] if limit else rest

    def remaining(self) -> int:
        return max(0, len(self._tracks) - self._index - 1)

    def uids(self) -> set[str]:
        return {t.uid for t in self._tracks}

    # ---------- изменение ----------
    def set_tracks(self, tracks, start: int = 0) -> Track | None:
        """Заменить очередь целиком и встать на выбранный трек."""
        self._tracks = list(tracks)
        if not self._tracks:
            self._index = -1
        else:
            self._index = max(0, min(start, len(self._tracks) - 1))
        # Новая очередь - новый логический порядок: старый отложенный уже не про неё
        self._original = list(self._tracks) if self._shuffle else None
        if self._shuffle:
            self._shuffle_tail()
        return self.current()

    def append(self, tracks) -> None:
        if isinstance(tracks, Track):
            tracks = [tracks]
        added = list(tracks)
        self._tracks.extend(added)
        if self._original is not None:
            self._original.extend(added)

    def insert_next(self, tracks) -> None:
        """Поставить сразу после текущего («играть следующим»)."""
        if isinstance(tracks, Track):
            tracks = [tracks]
        added = list(tracks)
        at = self._index + 1 if self._index >= 0 else 0
        self._tracks[at:at] = added
        if self._original is not None:
            # В логическом порядке «играть следующим» - тоже сразу за текущим
            current = self.current()
            base = self._original_index(current.uid) + 1 if current is not None else 0
            self._original[base:base] = added

    def remove(self, position: int) -> None:
        """Убрать трек. Указатель остаётся на том же треке, что и играл."""
        if not 0 <= position < len(self._tracks):
            return
        removed = self._tracks[position]
        del self._tracks[position]
        if self._original is not None:
            at = self._original_index(removed.uid)
            if at >= 0:
                del self._original[at]
        if not self._tracks:
            self._index = -1
        elif position < self._index:
            self._index -= 1
        elif position == self._index:
            # Играющий трек убрали: указатель показывает на вставший на его место
            self._index = min(self._index, len(self._tracks) - 1)

    def remove_uid(self, uid: str) -> None:
        self.remove(self.index_of(uid))

    def replace(self, position: int, track: Track) -> None:
        """Подменить трек на месте, не сдвигая очередь.

        Нужно, когда источник не отдал файл и та же песня играется с другого:
        «дальше» и «назад» должны остаться там же, где были."""
        if not 0 <= position < len(self._tracks):
            return
        old = self._tracks[position]
        self._tracks[position] = track
        if self._original is not None:
            at = self._original_index(old.uid)
            if at >= 0:
                self._original[at] = track

    def move(self, source: int, target: int) -> None:
        """Переставить трек, сохранив позицию играющего."""
        if not 0 <= source < len(self._tracks):
            return
        target = max(0, min(target, len(self._tracks) - 1))
        if source == target:
            return
        current_uid = self.current().uid if self.current() else None
        track = self._tracks.pop(source)
        self._tracks.insert(target, track)
        if self._original is not None:
            # Человек переставил вручную - это его решение и для порядка без shuffle
            at = self._original_index(track.uid)
            if at >= 0:
                self._original.pop(at)
                self._original.insert(min(target, len(self._original)), track)
        if current_uid is not None:
            self._index = self.index_of(current_uid)

    def clear(self) -> None:
        self._tracks.clear()
        self._index = -1
        self._original = None

    # ---------- перемещение по очереди ----------
    def set_index(self, position: int) -> Track | None:
        if 0 <= position < len(self._tracks):
            self._index = position
        return self.current()

    def go_next(self) -> Track | None:
        if not self.has_next():
            return None
        self._index += 1
        return self.current()

    def go_previous(self) -> Track | None:
        if not self.has_previous():
            return None
        self._index -= 1
        return self.current()

    # ---------- перемешивание ----------
    def set_shuffle(self, enabled: bool) -> None:
        """Включить или выключить перемешивание, не сбивая текущий трек."""
        enabled = bool(enabled)
        if enabled == self._shuffle:
            return
        self._shuffle = enabled
        if not self._tracks:
            self._original = list(self._tracks) if enabled else None
            return
        if enabled:
            self._original = list(self._tracks)
            self._shuffle_tail()
        else:
            self._restore_order()

    def _shuffle_tail(self) -> None:
        """Перетасовать всё после текущего трека. Играющее не двигаем."""
        head = self._index + 1 if self._index >= 0 else 0
        tail = self._tracks[head:]
        random.shuffle(tail)
        self._tracks[head:] = tail

    def _restore_order(self) -> None:
        """Вернуть логический порядок и остаться на том же треке."""
        if self._original is None:
            return
        current = self.current()
        known = {t.uid for t in self._original}
        # Всё, что добавили уже во время перемешивания, дописываем в конец -
        # иначе выключение shuffle молча выбрасывало бы эти треки из очереди
        restored = [t for t in self._original if any(x.uid == t.uid for x in self._tracks)]
        restored.extend(t for t in self._tracks if t.uid not in known)
        self._tracks = restored
        self._original = None
        self._index = self.index_of(current.uid) if current is not None else -1

    def _original_index(self, uid: str) -> int:
        if self._original is None:
            return -1
        for position, track in enumerate(self._original):
            if track.uid == uid:
                return position
        return -1

    # ---------- сохранение между запусками ----------
    def to_state(self) -> dict:
        state = {'index': self._index, 'shuffle': self._shuffle,
                 'tracks': [t.to_row() | {'meta': t.meta} for t in self._tracks]}
        if self._original is not None:
            state['original'] = [t.to_row() | {'meta': t.meta} for t in self._original]
        return state

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        self._tracks = _rows_to_tracks(state.get('tracks'))
        index = state.get('index', -1)
        self._index = (index if isinstance(index, int) and -1 <= index < len(self._tracks)
                       else -1)
        self._shuffle = bool(state.get('shuffle'))
        original = _rows_to_tracks(state.get('original'))
        self._original = original if (self._shuffle and original) else (
            list(self._tracks) if self._shuffle else None)


def _rows_to_tracks(rows) -> list[Track]:
    result = []
    for row in rows or []:
        if isinstance(row, dict) and row.get('source'):
            result.append(Track.from_row(row))
    return result
