"""Сопоставление треков разных источников: «этот ролик с YouTube - вот этот трек в VK?»

Нужно для главного действия приложения: прежде чем качать и заливать файл, надо
понять, нет ли уже готовой записи в VK. Названия у источников оформлены по-разному
(«Deftones - Sextape (Official Video)» против «Deftones - Sextape»), поэтому
сравниваем не строки как есть, а приведённые к общему виду.

Модуль сознательно не знает ни про Qt, ни про сеть - его легко проверять тестами
(см. tests/test_matcher.py)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .track import Track

# Пометки, которые ничего не говорят о самой музыке. Убираем их только когда они
# занимают скобку целиком или стоят в хвосте: «(Live)», «(Acoustic)», «(Remix)»
# менять трек как раз могут, поэтому их не трогаем.
_JUNK_WORDS = (
    'official video', 'official music video', 'official audio', 'official lyric video',
    'official lyrics video', 'official visualizer', 'official', 'lyric video',
    'lyrics video', 'lyrics', 'lyric', 'music video', 'video clip', 'videoclip',
    'audio', 'video', 'clip', 'hd', 'hq', 'full hd', '4k', '1080p', '720p',
    'visualizer', 'mv', 'pv', 'официальный клип', 'официальное видео', 'клип',
    'премьера клипа', 'премьера песни', 'текст песни',
)
_JUNK_RE = re.compile(r'^(?:' + '|'.join(re.escape(w) for w in _JUNK_WORDS) + r')$')

# Скобки любого вида: круглые, квадратные, фигурные
_BRACKETS = re.compile(r'[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]')

# «feat.», «ft», «с участием» и прочее - вытаскиваем отдельно, чтобы не мешало
_FEAT = re.compile(r'\b(?:feat|ft|featuring|with|при участии|совместно с)\b\.?\s*', re.I)

_SEP_CHARS = re.compile(r'[‐-―⁃−]')   # разные виды дефиса → '-'
_QUOTES = re.compile(r'[‘’‚‛“”„‟`´"\']')
_NON_WORD = re.compile(r'[^\w\s]+', re.UNICODE)
_SPACES = re.compile(r'\s+')

# Разделители нескольких исполнителей
_ARTIST_SPLIT = re.compile(r'\s*(?:,|&|/|\+|\bx\b|\bvs\b|\bи\b)\s*', re.I)

# В VK половина русских записей подписана латиницей («Zemfira - Hochesh?»), да и
# поиск на m.vk.ru принимает только латиницу (см. vk_client.search_tracks). Поэтому
# держим таблицу транслита здесь: ей пользуется и сравнение, и поисковый запрос.
_CYRILLIC = re.compile('[\u0400-\u04ff]')
_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
    'я': 'ya', 'і': 'i', 'ї': 'yi', 'є': 'ye', 'ґ': 'g',
}


def translit(text: str) -> str:
    """Кириллица → латиница. Латиница остаётся как есть."""
    out = []
    for ch in text or '':
        low = ch.lower()
        if low in _TRANSLIT:
            latin = _TRANSLIT[low]
            out.append(latin.upper() if ch.isupper() and latin else latin)
        else:
            out.append(ch)
    return ''.join(out)


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC.search(text or ''))


def _fold(text: str) -> str:
    """Общая часть нормализации: регистр, юникод, кавычки, лишние знаки."""
    text = unicodedata.normalize('NFKC', text or '')
    text = _SEP_CHARS.sub('-', text)
    text = _QUOTES.sub('', text)
    # ё и е в русских названиях пишут как придётся
    text = text.replace('ё', 'е').replace('Ё', 'Е')
    text = text.casefold()
    text = _NON_WORD.sub(' ', text)
    return _SPACES.sub(' ', text).strip()


def _strip_junk_brackets(text: str) -> str:
    """Убрать скобки, внутри которых только служебная пометка."""
    def repl(match: re.Match) -> str:
        inner = _fold(match.group(1))
        return '' if _JUNK_RE.match(inner) else match.group(0)

    return _SPACES.sub(' ', _BRACKETS.sub(repl, text)).strip()


def _strip_trailing_junk(text: str) -> str:
    """Убрать служебный хвост без скобок: «… official video» в конце строки."""
    changed = True
    while changed:
        changed = False
        folded = _fold(text)
        for junk in _JUNK_WORDS:
            if folded.endswith(' ' + junk):
                # режем ровно столько слов, сколько занимает пометка
                words = text.split()
                text = ' '.join(words[:len(text.split()) - len(junk.split())]).rstrip(' -–—|,')
                changed = True
                break
    return text.strip()


def norm_artist(artist: str) -> str:
    """Нормализованное имя исполнителя. Соисполнители после feat. отбрасываются:
    один и тот же трек на YouTube и в VK подписан ими по-разному."""
    artist = _FEAT.split(artist or '', maxsplit=1)[0]
    return _fold(artist)


def artist_parts(artist: str) -> set[str]:
    """Отдельные исполнители: «Linkin Park & Jay-Z» → {'linkin park', 'jay z'}."""
    parts = {_fold(p) for p in _ARTIST_SPLIT.split(_FEAT.split(artist or '', maxsplit=1)[0])}
    return {p for p in parts if p}


def norm_title(title: str) -> str:
    """Нормализованное название без служебных пометок."""
    title = _strip_junk_brackets(title or '')
    title = _strip_trailing_junk(title)
    title = _FEAT.split(title, maxsplit=1)[0]
    return _fold(title)


def normalized_key(artist: str, title: str) -> str:
    """Ключ «исполнитель|название» - им помечаем треки в базе."""
    return f'{norm_artist(artist)}|{norm_title(title)}'


def _plain_ratio(a: str, b: str) -> float:
    """Похожесть строк по общим словам плюс посимвольное сравнение.

    difflib один плохо ведёт себя на перестановках («sextape deftones»), а голые
    множества слов - на опечатках, поэтому берём лучшее из двух."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    from difflib import SequenceMatcher
    seq = SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()), set(b.split())
    jaccard = len(wa & wb) / len(wa | wb) if wa | wb else 0.0
    return max(seq, jaccard)


def _ratio(a: str, b: str) -> float:
    """То же сравнение, но кириллицу и латиницу считаем одним алфавитом.

    «Земфира - Хочешь?» и «Zemfira - Hochesh?» - один трек, а посимвольно у них нет
    ничего общего. Транслит приблизителен, поэтому такому совпадению даём чуть
    меньший вес, чем точному."""
    best = _plain_ratio(a, b)
    if best < 1.0 and (has_cyrillic(a) or has_cyrillic(b)):
        best = max(best, _plain_ratio(translit(a), translit(b)) * 0.97)
    return best


def _duration_factor(a: int, b: int) -> float:
    """Множитель за длительность. Ноль означает «неизвестно» - тогда не судим."""
    if not a or not b:
        # Сравнивать нечем - обычно это лёгкий штраф. Но если известная сторона
        # длится полминуты, это не песня, а обрубок: в VK такие лежат с правильным
        # названием, и без проверки они попадали бы в «Мою музыку» вместо трека.
        known = a or b
        return 0.4 if known < 45 else 0.95
    diff = abs(a - b)
    if diff <= 2:
        return 1.0
    if diff <= 5:
        return 0.98
    if diff <= 12:
        return 0.9
    if diff <= 25:
        return 0.7
    return 0.35          # разница больше 25 секунд - почти наверняка другая запись


def score(query: Track, candidate: Track) -> float:
    """Насколько кандидат похож на искомый трек, 0..1."""
    qa, qt = norm_artist(query.artist), norm_title(query.title)
    ca, ct = norm_artist(candidate.artist), norm_title(candidate.title)

    title_score = _ratio(qt, ct)
    if qa and ca:
        artist_score = _ratio(qa, ca)
        # Один из нескольких исполнителей совпал целиком - этого достаточно
        qp = {translit(p) for p in artist_parts(query.artist)}
        cp = {translit(p) for p in artist_parts(candidate.artist)}
        if qp & cp:
            artist_score = max(artist_score, 0.95)
    else:
        artist_score = 0.6      # у одной из сторон исполнителя нет - судим по названию

    base = 0.65 * title_score + 0.35 * artist_score

    # Разбор «исполнитель - название» мог не сработать (например, у ролика с
    # неудачным заголовком). Сравним ещё и всё целиком и возьмём лучший вариант.
    whole = _ratio(f'{qa} {qt}'.strip(), f'{ca} {ct}'.strip())
    base = max(base, whole * 0.97)

    return round(min(1.0, base) * _duration_factor(query.duration, candidate.duration), 4)


# Порог, выше которого совпадение считаем очевидным и берём без вопросов
CONFIDENT = 0.86
# Ниже этого кандидат вообще не рассматривается
MINIMUM = 0.62
# Если второй кандидат почти догоняет первого, выбор неочевиден
AMBIGUOUS_GAP = 0.05

CONFIDENT_MATCH = 'confident'
AMBIGUOUS_MATCH = 'ambiguous'
NO_MATCH = 'none'


@dataclass
class MatchResult:
    """Итог поиска: что нашли и насколько уверенно."""

    kind: str = NO_MATCH
    best: Track | None = None
    candidates: list[tuple[Track, float]] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.kind == CONFIDENT_MATCH


def match(query: Track, candidates, limit: int = 5) -> MatchResult:
    """Выбрать среди кандидатов тот же трек.

    Возвращает «уверенно», «неоднозначно» (пусть выберет человек) или «не найдено».
    Сомнительное не берём автоматически: чужой трек в своей музыке хуже, чем лишняя
    закачка."""
    scored = [(c, score(query, c)) for c in candidates]
    scored = [(c, s) for c, s in scored if s >= MINIMUM]
    # При равном счёте берём запись подлиннее: одинаково подписанные копии в VK
    # отличаются как раз тем, что одна из них обрезана.
    scored.sort(key=lambda pair: (pair[1], pair[0].duration), reverse=True)
    scored = scored[:limit]
    if not scored:
        return MatchResult()
    best, best_score = scored[0]
    if best_score >= CONFIDENT:
        # Два разных трека с почти одинаковой оценкой - это, как правило, одна и та
        # же запись в разном качестве, брать первую нормально. Спрашиваем только
        # когда лидер до уверенного не дотянул.
        return MatchResult(CONFIDENT_MATCH, best, scored)
    return MatchResult(AMBIGUOUS_MATCH, best, scored)
