import json
import sys
from pathlib import Path


def _res_dir() -> Path:
    """Откуда читать неизменяемое: иконку, таблицу стилей, ffmpeg, движок JS.

    В собранном .exe PyInstaller распаковывает всё это во временную папку и
    кладёт её путь в `sys._MEIPASS`. Из исходников такой папки нет — корень
    проекта и есть корень ресурсов."""
    packed = getattr(sys, '_MEIPASS', None)
    return Path(packed) if packed else Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Куда писать своё: музыку, настройки, базу, журналы.

    Временную папку .exe Windows чистит при выходе, и настройки в ней не
    пережили бы даже одного перезапуска. Поэтому данные живут рядом с самим
    .exe — программа остаётся переносимой: скопировали папку, унесли с собой."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Ресурсы и данные лежат порознь только в собранном .exe; из исходников это
# один и тот же корень проекта, и всё работает как раньше
RES_DIR = _res_dir()
BASE_DIR = _data_dir()

# Имя приложения в одном месте: заголовок окна, значок у часов, уведомления
# и панель задач Windows берут его отсюда, а не переписывают у себя.
APP_NAME = 'Music Hub'
# AppUserModelID для Windows: по нему система отличает наше окно от python.exe
APP_ID = 'MusicHub.App'

MUSIC_DIR = BASE_DIR / 'музыка'
VIDEO_DIR = BASE_DIR / 'видео'
# Офлайн-копии треков «Моей музыки» — отдельно от скачанного вручную: содержимое
# этой папки приложение считает своим и вправе чистить по лимиту (см. core/offline.py)
OFFLINE_DIR = BASE_DIR / 'офлайн'
LOGS_DIR = BASE_DIR / 'logs'
CONFIG_DIR = BASE_DIR / 'config'


def _external(*parts: str) -> Path:
    """Путь к сторонней программе: ffmpeg или движку JS.

    Ищем сначала рядом с приложением, потом внутри сборки. Порядок именно
    такой: собранный .exe несёт свою копию, но человек вправе положить рядом
    другую — свежее или собранную под свою систему, — и она победит. Из
    исходников обе ветки указывают в одно место, и разницы нет."""
    near = BASE_DIR.joinpath(*parts)
    return near if near.exists() else RES_DIR.joinpath(*parts)


FFMPEG_PATH = _external('ffmpeg', 'bin', 'ffmpeg.exe')
# JS-движок для yt-dlp: без него YouTube не отдаёт ссылки на потоки (см. core/js_runtime.py)
JS_RUNTIME_FILE = _external('runtime', 'qjs.exe')
# Куда движок скачивается: только рядом с приложением. Папка сборки в .exe
# временная — скачанное туда пропало бы при первом же выходе из программы
JS_RUNTIME_DOWNLOAD = BASE_DIR / 'runtime' / 'qjs.exe'
COOKIES_FILE = BASE_DIR / 'cookies.txt'
VK_COOKIES_FILE = BASE_DIR / 'vk_cookies.txt'

WEB_PROFILE_DIR = CONFIG_DIR / 'webprofile'

SETTINGS_FILE = CONFIG_DIR / 'settings.json'
VK_TOKEN_FILE = CONFIG_DIR / 'vk_token.json'
HISTORY_FILE = CONFIG_DIR / 'history.json'
# База музыкального раздела: связки YouTube↔VK, избранное, история прослушивания,
# свои плейлисты (см. core/store.py). history.json остаётся отдельно и не трогается.
DB_FILE = CONFIG_DIR / 'musichub.db'
LOG_FILE = LOGS_DIR / 'app.log'

DEFAULT_SETTINGS = {
    'mode': 'audio',
    'video_quality': 'best',
    'audio_format': 'mp3',
    'audio_bitrate': '192',
    'music_dir': str(MUSIC_DIR),
    'video_dir': str(VIDEO_DIR),
    'concurrency': 3,
    # Сколько кусков одного файла тянуть разом. YouTube режет скорость каждому
    # соединению по отдельности, поэтому несколько параллельных идут заметно
    # быстрее одного; 1 возвращает прежнее поведение, если провайдер против
    'fragment_concurrency': 4,
    # None — без кук; 'file' — cookies.txt/vk_cookies.txt рядом с приложением (ручной экспорт);
    # имя браузера (chrome/yandex/edge/firefox/brave/vivaldi/opera) — куки берутся из него автоматически.
    'cookies_browser': 'chrome',
    # Пропускать то, что уже отмечено скачанным в history.json
    'skip_downloaded': True,
    # Скачанную музыку сразу заливать в «Мою музыку» VK (нужен выполненный вход)
    'auto_vk_upload': False,
    # Прокси для скачивания: 'auto' — системный или найденный локальный клиент,
    # 'off' — прямое подключение, 'manual' — адрес из proxy_url (см. core/proxy.py)
    'proxy_mode': 'auto',
    'proxy_url': '',
    # Логин и пароль платного прокси. Лежат здесь открытым текстом — файл настроек
    # такой же секрет, как config/vk_token.json: не показывать и не логировать.
    'proxy_user': '',
    'proxy_pass': '',
    # Отправлять первый пакет защищённого соединения по частям, чтобы фильтр не прочёл
    # имя сайта целиком (см. core/frag_proxy.py). Помогает, когда закрыты видеосерверы
    # YouTube; по умолчанию выключено — лишний слой нужен не всем.
    'proxy_fragment': False,

    # ---------- музыкальный раздел ----------
    # Громкость плеера, 0..100, и надо ли её запоминать между запусками
    'volume': 80,
    'remember_volume': True,
    # Доигрался трек — включать следующий из очереди
    'autoplay_next': True,
    # Настройки раздела «Микс»: доли источников и что играть. Пусто — значит
    # «как задумано»: значения по умолчанию живут в core/mixer.py, MixConfig
    'mix': {},

    # ---------- «Моя музыка»: свои файлы и офлайн ----------
    # Папки со своей музыкой: их содержимое попадает в раздел «Треки» как треки
    # источника «Файл». Папка загрузок туда же не приписывается — скачанное видно
    # в «Библиотеке», а в «Мою музыку» человек добавляет то, что хочет слушать.
    'local_dirs': [],
    # Куда складывать офлайн-копии. Папку можно перенести — старые файлы
    # останутся на месте и продолжат играть, новые поедут по новому адресу.
    'offline_dir': str(OFFLINE_DIR),
    # Сохранять офлайн всё, что попадает в «Любимое». Всё подряд не кэшируем:
    # тысяча треков VK — это гигабайты, о которых никто не просил.
    'offline_favorites': True,
    # Потолок папки офлайна, ГБ. 0 — без ограничения. При превышении вычищаются
    # самые давние копии, кроме избранного (см. core/offline.py).
    'offline_limit_gb': 5.0,

    # Значок у часов. close_to_tray по умолчанию выключен: раньше крестик закрывал
    # приложение, и менять это без ведома пользователя нельзя.
    'tray_enabled': True,
    'minimize_to_tray': False,
    'close_to_tray': False,
    'tray_notifications': True,

    # Глобальные сочетания клавиш (Windows). Ctrl+Alt+… выбраны, чтобы не мешать
    # обычному вводу в других программах.
    'hotkeys_enabled': True,
    'hotkey_play_pause': 'Ctrl+Alt+Space',
    'hotkey_next': 'Ctrl+Alt+Right',
    'hotkey_prev': 'Ctrl+Alt+Left',
    'hotkey_add_vk': 'Ctrl+Alt+V',
    'hotkey_favorite': 'Ctrl+Alt+L',
    'hotkey_show': 'Ctrl+Alt+M',
    # Мультимедийные клавиши клавиатуры (play/pause, next, prev)
    'hotkeys_media_keys': True,

    # Мост для расширения браузера: слушает только 127.0.0.1 (см. core/bridge.py).
    # Токен создаётся при первом запуске и показывается в настройках — это секрет.
    'bridge_enabled': True,
    'bridge_port': 48211,
    'bridge_token': '',

    # Поведение кнопки «+ VK»
    # Сначала искать готовую запись в VK и добавлять её, а качать только если не нашлось
    'vk_match_first': True,
    # Спрашивать, когда подходящих записей несколько и уверенного совпадения нет
    'vk_ask_on_ambiguous': True,
    # Не нашли в VK — скачать и залить своим файлом
    'vk_upload_fallback': True,
}


def ensure_dirs() -> None:
    for d in (MUSIC_DIR, VIDEO_DIR, OFFLINE_DIR, LOGS_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            return {**DEFAULT_SETTINGS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    ensure_dirs()
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')


def load_vk_token() -> dict | None:
    if VK_TOKEN_FILE.exists():
        try:
            return json.loads(VK_TOKEN_FILE.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_vk_token(token_data: dict) -> None:
    ensure_dirs()
    VK_TOKEN_FILE.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding='utf-8')


def clear_vk_token() -> None:
    if VK_TOKEN_FILE.exists():
        VK_TOKEN_FILE.unlink()
