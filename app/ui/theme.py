"""Цвета и размеры оформления в одном месте.

Таблица стилей и рисование вручную (полоса прогресса, заготовки строк, значки)
раньше знали цвета порознь: правка палитры в styles.qss оставляла делегаты
прежними, и в окне соседствовали два синих. Теперь цвет объявлен здесь, а
styles.qss берёт его подстановкой `@имя` - расходиться нечему.

Палитра тёмная: почти чёрный фон, три уровня поверхностей и живой акцент
(синий VK, переходящий в фуксию). Градиент - только на главном действии и на
проигранной части полос: если им красить всё подряд, взгляду не за что
зацепиться."""
from __future__ import annotations

import re

from .. import config

# ---------- палитра ----------
COLORS: dict[str, str] = {
    # поверхности, от самой дальней к самой ближней
    'bg': '#0b0d12',           # фон окна
    'surface': '#12151d',      # сайдбар, полоса плеера, панель очереди
    'card': '#171b25',         # карточки и списки
    'raised': '#1e2430',       # поля ввода, вторичные кнопки, «таблетки»
    'hover': '#252c3a',        # наведение на поверхность
    'border': '#272e3c',       # обычная граница
    'border_soft': '#1d2331',  # деление внутри блока
    # текст
    'text': '#e9ecf3',
    'text_dim': '#98a1b4',
    'text_mute': '#6a7387',
    'text_on_accent': '#ffffff',
    # акцент
    'accent': '#4b7bec',
    'accent_hi': '#6a92f2',    # наведение
    'accent_lo': '#3a63c8',    # нажатие
    'accent_2': '#e0559a',     # второй конец градиента
    'accent_soft': '#1b2540',  # подложка выбранного состояния
    'accent_text': '#8fb2ff',  # ссылки и подписи акцентом
    # состояния
    'ok': '#3fbb87',
    'ok_soft': '#12332a',
    'warn': '#f0bf72',
    'warn_soft': '#38301c',
    'danger': '#e8695f',
    'danger_soft': '#3a2028',
}

# Градиент главного действия. Слева направо: синий VK → фуксия.
GRADIENT = ('qlineargradient(x1:0, y1:0, x2:1, y2:1, '
            f"stop:0 {COLORS['accent']}, stop:1 {COLORS['accent_2']})")
GRADIENT_HI = ('qlineargradient(x1:0, y1:0, x2:1, y2:1, '
               f"stop:0 {COLORS['accent_hi']}, stop:1 #ec6fab)")
# Горизонтальный - для полос прогресса и перемотки
GRADIENT_BAR = ('qlineargradient(x1:0, y1:0, x2:1, y2:0, '
                f"stop:0 {COLORS['accent']}, stop:1 {COLORS['accent_2']})")

# ---------- размеры ----------
RADIUS_SM, RADIUS, RADIUS_LG = 8, 12, 16
# Высота строки списка треков и стороны обложек: их знают и списки, и плеер
ROW_HEIGHT = 56

_VALUES = dict(COLORS)
_VALUES.update({'gradient': GRADIENT, 'gradient_hi': GRADIENT_HI,
                'gradient_bar': GRADIENT_BAR,
                'radius_sm': f'{RADIUS_SM}px', 'radius': f'{RADIUS}px',
                'radius_lg': f'{RADIUS_LG}px'})

_TOKEN = re.compile(r'@([a-z_0-9]+)')


def color(name: str) -> str:
    """Цвет по имени. Незнакомое имя - заметная фуксия, а не тихий сбой."""
    return COLORS.get(name, '#ff00ff')


# Значки, на которые ссылается таблица стилей. QSS умеет брать картинку только
# из файла, поэтому рисуем их один раз в кэш и подставляем путь.
_ICON_DIR = config.CONFIG_DIR / 'qss-icons'
_QSS_ICONS = {
    'icon_check': ('check', 'text_on_accent', 14),
    'icon_check_off': ('check', 'text_mute', 14),
    'icon_down': ('chevron_down', 'text_dim', 12),
    'icon_up': ('chevron_up', 'text_dim', 12),
}


def icon_url(name: str, color_name: str, size: int) -> str:
    """Путь к значку для QSS. Пустая строка, если нарисовать не вышло."""
    from . import player_icons  # цвета берутся отсюда - импорт только по месту
    try:
        _ICON_DIR.mkdir(parents=True, exist_ok=True)
        value = color(color_name)
        path = _ICON_DIR / f"{name}-{value.lstrip('#')}-{size}.png"
        if not path.is_file():
            player_icons.draw(name, value, size).pixmap(size, size).save(str(path))
        return path.as_posix()
    except Exception:
        # Без картинки поле останется без стрелки - это терпимо, падать незачем
        return ''


def stylesheet() -> str:
    """Таблица стилей приложения с подставленными цветами."""
    path = config.RES_DIR / 'app' / 'ui' / 'styles.qss'
    if not path.is_file():
        return ''
    text = path.read_text(encoding='utf-8')
    values = dict(_VALUES)
    values.update({token: icon_url(*args) for token, args in _QSS_ICONS.items()})
    return _TOKEN.sub(lambda match: values.get(match.group(1), match.group(0)), text)
