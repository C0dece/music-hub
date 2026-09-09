"""Готовые настроения - микс в одно нажатие.

Настроить микс руками можно и без этого файла: режим, доли источников и запрос
уже есть на странице. Но чтобы просто включить музыку, человеку приходилось
принимать четыре решения подряд. Пресет принимает их за него: это обычный
`MixConfig`, а не отдельная ветка исполнения, поэтому после нажатия все ручки
остаются на виду и их можно докрутить.

Запросы - латиницей. Кириллицу в поиск VK слать нельзя (AGENTS.md: VK её портит),
а пресеты проще держать в латинице сразу, чем прогонять через `matcher.translit`.

Qt здесь нет: это данные, и проверяются они обычными тестами.
"""
from __future__ import annotations

from .mixer import (MODE_DISCOVER, MODE_KNOWN, MODE_MIXED, MixConfig)
from .track import SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE


class Mood:
    """Настроение: как оно называется и каким миксом оборачивается."""

    def __init__(self, key: str, title: str, hint: str, icon: str, mode: str,
                 query: str = '', weights: dict | None = None,
                 shuffle: bool = True, skip_recent: bool = True):
        self.key = key
        self.title = title
        self.hint = hint
        self.icon = icon
        self._mode = mode
        self._query = query
        self._weights = weights
        self._shuffle = shuffle
        self._skip_recent = skip_recent

    def config(self, limit: int | None = None, autoplay: bool = True) -> MixConfig:
        """Свежий `MixConfig` под это настроение.

        Каждый раз новый: конфигурация уезжает на страницу, где её правят
        ползунками, и общий объект превратил бы правку одного пресета в правку
        всех сразу.
        """
        return MixConfig(weights=dict(self._weights) if self._weights else None,
                         mode=self._mode, query=self._query,
                         limit=limit if limit is not None else MixConfig().limit,
                         shuffle=self._shuffle, skip_recent=self._skip_recent,
                         autoplay=autoplay)


# Доли, которые повторяются: имена вместо словарей на каждой строке
_BALANCED = {SOURCE_VK: 50, SOURCE_YOUTUBE: 50, SOURCE_LOCAL: 0}
_OWN = {SOURCE_VK: 70, SOURCE_YOUTUBE: 0, SOURCE_LOCAL: 30}
_NEW = {SOURCE_VK: 25, SOURCE_YOUTUBE: 75, SOURCE_LOCAL: 0}
_LOCAL_ONLY = {SOURCE_VK: 0, SOURCE_YOUTUBE: 0, SOURCE_LOCAL: 100}

MOODS: tuple[Mood, ...] = (
    Mood('energy', 'Энергия', 'Быстрое и громкое', 'autoplay', MODE_MIXED,
         'energetic upbeat', _BALANCED),
    Mood('calm', 'Спокойное', 'Тихо и без резких переходов', 'audio', MODE_MIXED,
         'calm chill relax', _BALANCED),
    Mood('focus', 'Для работы', 'Не отвлекает от дела', 'headphones', MODE_MIXED,
         'focus instrumental concentration', _BALANCED, shuffle=False),
    Mood('workout', 'Тренировка', 'Держит темп', 'autoplay', MODE_DISCOVER,
         'workout gym motivation', _NEW),
    Mood('evening', 'Вечер', 'Медленное и тёплое', 'audio', MODE_MIXED,
         'evening mellow acoustic', _BALANCED),
    Mood('nostalgia', 'Ностальгия', 'То, что уже слушали', 'heart', MODE_KNOWN,
         '', _OWN, skip_recent=False),
    Mood('dance', 'Танцы', 'Ритм без пауз', 'audio', MODE_MIXED,
         'dance party electronic', _BALANCED),
    Mood('road', 'Дорога', 'Долгая дорога и знакомые песни', 'radio', MODE_MIXED,
         'road trip driving', _BALANCED),
    Mood('background', 'Фон', 'Играет и не мешает', 'audio', MODE_MIXED,
         'background ambient lounge', _BALANCED, shuffle=True),
    Mood('loud', 'Громко', 'Гитары и барабаны', 'autoplay', MODE_MIXED,
         'rock loud guitars', _BALANCED),
    Mood('melancholy', 'Меланхолия', 'Медленное и грустное', 'audio', MODE_MIXED,
         'sad melancholic slow', _BALANCED),
    Mood('fresh', 'Новое', 'Чего ещё не слышали', 'search', MODE_DISCOVER,
         '', _NEW),
    Mood('own', 'Своё', 'Только файлы с компьютера', 'audio', MODE_KNOWN,
         '', _LOCAL_ONLY),
)

MOODS_BY_KEY = {mood.key: mood for mood in MOODS}


def find(key: str) -> Mood | None:
    """Настроение по ключу. Неизвестный ключ - не ошибка: пресеты могут пропасть
    между версиями, а сохранённые настройки останутся."""
    return MOODS_BY_KEY.get(key or '')
