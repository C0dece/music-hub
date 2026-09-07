"""Тихое возвращение веб-сессии VK — без окна входа.

Куки сайта VK живут недолго, а без них не грузится ни музыка, ни плейлисты. Раньше
единственным выходом было окно входа: человек бросал начатое и вводил пароль заново,
хотя в постоянном профиле встроенного браузера чаще всего лежит живая сессия — просто
файл кук для запросов успел устареть.

Keeper проходит этот путь молча: открывает `m.vk.ru` в невидимом движке на том же
профиле `ytd-vk`, что и окно входа, ждёт `remixsid`, сохраняет куки и проверяет их делом.
Получилось — снаружи никто ничего не заметил, кроме вернувшейся музыки.

Чего keeper не делает никогда — не открывает окно входа сам. Пароль спрашивают только
тогда, когда человек сам этого захотел; при неудаче отсюда уходит лишь сигнал, а решение
остаётся за тем, кто его поймает.

Своего движка модуль не создаёт напрямую: он приходит фабрикой в конструктор. Так
антишторм, таймаут и взаимная блокировка проверяются тестами без сети и без QtWebEngine.
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .async_task import run_async
from .vk_client import VkAccountBlocked, check_web_session
from .vk_web_login import (
    VK_SITE_URL, _is_session_cookie, _is_site_domain, merge_cookies_to_file,
)

logger = logging.getLogger(__name__)

# Пауза между попытками: сессия не восстановится от того, что мы стучимся чаще,
# а вот выглядеть подозрительно для VK от частых заходов — вполне
MIN_INTERVAL = 300.0        # секунд между попытками
MAX_ATTEMPTS = 3            # неудач подряд, дальше пауза длиной COOLDOWN
LOAD_TIMEOUT = 30.0         # секунд на загрузку страницы и появление кук
# Три неудачи подряд — это чаще всего «VK сейчас недоступен», а не «нужен пароль»:
# упала сеть, лёг прокси, сайт отвечает ошибкой. Раньше после них keeper замолкал до
# ручного входа, и обещание «без участия пользователя» переставало выполняться ровно
# там, где оно нужнее всего. Полчаса тишины — достаточно, чтобы не долбить VK, и
# достаточно мало, чтобы починка случилась сама, пока человек занят другим
COOLDOWN = 1800.0           # секунд молчания после исчерпанной серии попыток


class VkSessionKeeper(QObject):
    """Возвращает веб-сессию VK в фоне, пока это возможно без пароля."""

    # Сессия вернулась сама — можно перечитывать списки
    restored = Signal()
    # Не вышло: причина текстом. Окно входа отсюда не открывается — это дело вызывающего
    failed = Signal(str)
    # VK заблокировал аккаунт: единственный случай, когда повторять бессмысленно.
    # Отдельный сигнал, потому что и реакция другая — не «попробуем позже», а «объясни»
    blocked = Signal(str)

    # Профиль `ytd-vk` на диске один, и два движка на нём дерутся за файл кук: тот,
    # кто закрылся вторым, затирает чужие изменения. Флаг класса разводит их по очереди
    _profile_busy = False

    def __init__(self, engine_factory=None, parent=None, user_id=None):
        super().__init__(parent)
        self._engine_factory = engine_factory or _make_engine
        # `check_web_session` проверяет сессию делом — запросом за треками, а для него
        # нужен владелец. Держим не число, а способ его узнать: аккаунт может смениться
        # за время жизни окна, и запомненный при старте id указывал бы на чужой
        self._user_id = user_id
        self._engine = None
        self._attempts = 0
        self._last_try = 0.0
        # Блокировка аккаунта — единственный отказ, который не проходит сам собой:
        # пауза тут ничего не лечит, и попытки прекращаются совсем, до нового входа
        self._blocked = False
        self._running = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

    # ---------- взаимная блокировка с окном входа ----------
    @classmethod
    def lock_profile(cls) -> None:
        """Окно входа занимает профиль: keeper в это время не стартует."""
        cls._profile_busy = True

    @classmethod
    def unlock_profile(cls) -> None:
        cls._profile_busy = False

    # ---------- запуск ----------
    @property
    def running(self) -> bool:
        return self._running

    def reset(self) -> None:
        """Сессия появилась другим путём (вошли руками) — счётчик неудач ни к чему.

        Заодно снимается и запрет по блокировке: раз вход удался, VK пускает."""
        self._attempts = 0
        self._last_try = 0.0
        self._blocked = False

    def try_restore(self) -> bool:
        """Попробовать вернуть сессию. Возвращает, началась ли попытка.

        Отказ здесь — обычное дело, а не ошибка: слишком рано, попытки исчерпаны,
        занят профиль или предыдущий заход ещё идёт."""
        reason = self._why_not()
        if reason:
            logger.debug('VK keeper: попытка пропущена: %s', reason)
            return False

        self._running = True
        self._last_try = time.monotonic()
        self._attempts += 1
        logger.info('VK keeper: пробую вернуть сессию VK в фоне (попытка %d)',
                    self._attempts)
        type(self).lock_profile()
        self._timeout.start(int(LOAD_TIMEOUT * 1000))
        try:
            self._engine = self._engine_factory(self._on_cookies, self._on_engine_error)
        except Exception as exc:  # noqa: BLE001 — движок не завёлся, но программа живёт
            self._finish_failure(f'встроенный браузер не запустился: {exc}')
            return False
        return True

    def _why_not(self) -> str:
        if self._running:
            return 'предыдущая попытка ещё идёт'
        # Заблокированный аккаунт перезаходом не лечится: встроенный браузер честно
        # пройдёт вход и упрётся в ту же страницу блокировки, а VK увидит ещё одну
        # попытку. Снимает этот запрет только `reset()` — то есть удавшийся вход
        if self._blocked:
            return 'VK держит аккаунт заблокированным'
        if type(self)._profile_busy:
            return 'профиль занят окном входа'
        idle = time.monotonic() - self._last_try if self._last_try else None
        if self._attempts >= MAX_ATTEMPTS:
            if idle is not None and idle >= COOLDOWN:
                # Серия кончилась давно — считаем её прошлой бедой и пробуем заново
                self._attempts = 0
                return ''
            return f'подряд не вышло {self._attempts} раз'
        if idle is not None and idle < MIN_INTERVAL:
            return 'слишком рано после прошлой попытки'
        return ''

    # ---------- ответы движка ----------
    def _on_cookies(self, cookies: dict) -> None:
        """Движок собрал куки VK. Дальше — сохранить и проверить делом."""
        if not self._running:
            return                       # опоздали: таймаут уже всё закрыл
        # Домен важен не меньше имени: remixsid на `.vk.com` до m.vk.ru не доедет,
        # и объявить по ней успех значило бы вернуть человеку вход без музыки
        if not any(_is_session_cookie(name) and _is_site_domain(domain)
                   for domain, name in cookies):
            self._finish_failure('VK не выдал сессию сайта, нужен вход с паролем')
            return
        self._timeout.stop()
        try:
            saved = _save_cookies(cookies)
        except OSError as exc:
            self._finish_failure(f'куки не сохранились: {exc}')
            return
        logger.debug('VK keeper: сохранил %d куки', saved)
        # Куки на месте — но живые ли они, знает только сам VK. Проверка сетевая,
        # поэтому в фоне: иначе окно замрёт ровно там, где мы обещали тишину
        owner = self._user_id() if callable(self._user_id) else self._user_id
        if owner is None:
            # Проверить делом нечем, а врать про успех нельзя: сессия могла и не ожить
            self._finish_failure('некого спросить: аккаунт VK неизвестен')
            return
        run_async(check_web_session, self._on_checked, owner)

    def _on_checked(self, alive, error) -> None:
        if isinstance(error, VkAccountBlocked):
            # Перезаход здесь не помогает и не поможет: VK не пускает сам аккаунт.
            # Гасим серию совсем, иначе таймер будет тикать вхолостую до перезапуска
            logger.info('VK keeper: аккаунт заблокирован, тихий перезаход отменяется')
            self._timeout.stop()
            self._close_engine()
            self._running = False
            self._attempts = MAX_ATTEMPTS
            self._blocked = True
            self.blocked.emit(str(error))
            return
        if error is not None:
            self._finish_failure(f'проверка сессии не удалась: {error}')
            return
        if not alive:
            self._finish_failure('сохранённая сессия VK не ожила, нужен вход с паролем')
            return
        logger.info('VK keeper: сессия VK вернулась сама')
        self._close_engine()
        self._running = False
        self._attempts = 0               # получилось — счётчик неудач обнуляем
        self.restored.emit()

    def _on_engine_error(self, message: str) -> None:
        self._finish_failure(message or 'встроенный браузер не открыл страницу VK')

    def _on_timeout(self) -> None:
        self._finish_failure('VK не ответил вовремя')

    # ---------- завершение ----------
    def _finish_failure(self, reason: str) -> None:
        if not self._running and self._engine is None:
            return
        self._timeout.stop()
        self._close_engine()
        self._running = False
        logger.info('VK keeper: тихо вернуть сессию не вышло: %s', reason)
        self.failed.emit(reason)

    def _close_engine(self) -> None:
        engine, self._engine = self._engine, None
        type(self).unlock_profile()
        if engine is None:
            return
        try:
            engine.close()
        except Exception as exc:  # noqa: BLE001 — закрытие не должно ронять программу
            logger.debug('VK keeper: движок не закрылся (%s)', exc)


def _save_cookies(cookies: dict) -> int:
    """Куки VK — в файл, которым пользуются запросы.

    Пишем слиянием: наличие `remixsid` (проверено выше) ещё не значит, что движок успел
    отдать весь сопутствующий набор, а без него сессия не работает."""
    return merge_cookies_to_file(cookies.values())


def _make_engine(on_cookies, on_error):
    """Невидимая страница VK на общем с окном входа профиле.

    Импорт внутри: QtWebEngine тяжёл, а при подменённой фабрике (тесты, окружение
    без движка) он вообще не нужен."""
    from .vk_headless import HeadlessVkPage
    return HeadlessVkPage(VK_SITE_URL, on_cookies, on_error)
