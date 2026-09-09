"""Горячие клавиши, работающие поверх других окон (Windows).

Своей зависимости ради этого не берём: RegisterHotKey есть в системе, а ctypes -
в стандартной поставке Python. Всё, что специфично для Windows, заперто в этом
модуле: на других системах менеджер просто ничего не делает.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

WINDOWS = sys.platform == 'win32'
if WINDOWS:  # ctypes.wintypes есть не во всех сборках, тянем только там, где нужен
    import ctypes.wintypes

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # без него зажатая клавиша сыплет событиями

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIERS = {
    'CTRL': MOD_CONTROL, 'CONTROL': MOD_CONTROL,
    'ALT': MOD_ALT, 'SHIFT': MOD_SHIFT,
    'WIN': MOD_WIN, 'META': MOD_WIN, 'SUPER': MOD_WIN,
}

# Имена клавиш пишутся в настройках человеком, поэтому список короткий и понятный
_KEYS = {
    'SPACE': 0x20, 'ENTER': 0x0D, 'RETURN': 0x0D, 'TAB': 0x09, 'ESC': 0x1B,
    'ESCAPE': 0x1B, 'BACKSPACE': 0x08, 'INSERT': 0x2D, 'DELETE': 0x2E,
    'HOME': 0x24, 'END': 0x23, 'PGUP': 0x21, 'PAGEUP': 0x21,
    'PGDOWN': 0x22, 'PAGEDOWN': 0x22,
    'LEFT': 0x25, 'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28,
    'MEDIAPLAY': 0xB3, 'MEDIANEXT': 0xB0, 'MEDIAPREV': 0xB1, 'MEDIASTOP': 0xB2,
}
for _i in range(1, 13):
    _KEYS[f'F{_i}'] = 0x70 + _i - 1  # VK_F1 = 0x70

# Мультимедийные клавиши клавиатуры: включаются отдельной настройкой
MEDIA_KEYS = {
    'play_pause': 'MediaPlay',
    'next': 'MediaNext',
    'prev': 'MediaPrev',
}


def parse(shortcut: str):
    """'Ctrl+Alt+Space' → (модификаторы, код клавиши). None, если запись непонятна."""
    if not shortcut:
        return None
    mods = 0
    key = None
    for part in str(shortcut).replace(' ', '').split('+'):
        if not part:
            continue
        name = part.upper()
        if name in _MODIFIERS:
            mods |= _MODIFIERS[name]
        elif name in _KEYS:
            key = _KEYS[name]
        elif len(name) == 1 and (name.isalpha() or name.isdigit()):
            key = ord(name)
        else:
            return None
    if key is None:
        return None
    return mods, key


class HotkeyManager(QObject):
    """Регистрирует сочетания и превращает их в сигнал triggered('play_pause')."""

    triggered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._bindings: dict[str, tuple[int, int]] = {}
        self.failed: list[str] = []  # что система не отдала - занято другой программой

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, bindings: dict) -> bool:
        """bindings: {'play_pause': 'Ctrl+Alt+Space', ...}. False - клавиши не работают."""
        self.stop()
        if not WINDOWS:
            logger.info('Горячие клавиши доступны только в Windows')
            return False
        parsed = {}
        for action, shortcut in (bindings or {}).items():
            combo = parse(shortcut)
            if combo is None:
                if shortcut:
                    logger.warning('Не понимаю сочетание %r для %s', shortcut, action)
                continue
            parsed[action] = combo
        if not parsed:
            return False
        self._bindings = parsed
        self.failed = []
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name='hotkeys', daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)
        return self.running

    def stop(self) -> None:
        if self._thread is not None and self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=3)
        self._thread = None
        self._thread_id = 0

    # ---------- поток сообщений ----------
    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        # Сочетания принадлежат потоку, который их зарегистрировал, поэтому и
        # регистрация, и цикл сообщений живут здесь
        ids: dict[int, str] = {}
        for number, (action, (mods, key)) in enumerate(self._bindings.items(), start=1):
            if user32.RegisterHotKey(None, number, mods | MOD_NOREPEAT, key):
                ids[number] = action
            else:
                self.failed.append(action)
                logger.warning('Сочетание для %s занято другой программой', action)
        self._ready.set()
        if not ids:
            return
        try:
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    action = ids.get(int(msg.wParam))
                    if action:
                        self.triggered.emit(action)
        finally:
            for number in ids:
                user32.UnregisterHotKey(None, number)
