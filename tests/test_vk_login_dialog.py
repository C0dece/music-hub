"""Окно входа в VK: не отказывать в живой сессии и не гнать на второй заход в Kate.

QtWebEngine здесь нет: проверяются ровно те методы, что решают судьбу входа, на
скелете с подменёнными полями. Повод — снимок экрана от пользователя: окно писало
«VK не выдал сессию сайта» поверх открытой ленты VK, потому что спрашивало сайт
один раз сразу после выдачи прав, а VK в этот момент ещё отвечает страницей входа.
"""
import unittest

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl

from app.core import vk_web_login as login_mod

from .qt_app import qt_app


class Skeleton:
    """Только то, что трогают проверяемые методы: движка и разметки не заводим."""

    _succeed = login_mod.VkWebLoginDialog._succeed
    _check_session = login_mod.VkWebLoginDialog._check_session
    _on_session_checked = login_mod.VkWebLoginDialog._on_session_checked
    _on_new_token_verified = login_mod.VkWebLoginDialog._on_new_token_verified

    def __init__(self):
        self.checks = 0
        self.status = ''
        self.emitted = []
        self.accepted = 0
        self.urls = []
        self._session_tries = 0
        self._session_handled = True
        self._token_pending = True
        self._awaiting_user_continue = False
        self._verified_token = None
        self._alive = False

        outer = self

        class _Status:
            def setText(self, text):
                outer.status = text

        class _Signal:
            def emit(self, data):
                outer.emitted.append(data)

        self._status = _Status()
        self.logged_in = _Signal()

    # ---- подмены внешнего мира ----
    def _save_cookies(self):
        return 42

    def accept(self):
        self.accepted += 1

    class _View:
        def __init__(self, outer):
            self._outer = outer

        def setUrl(self, url):
            self._outer.urls.append(url.toString())

    @property
    def _view(self):
        return Skeleton._View(self)


def settle(page):
    """Прокрутить очередь событий: повтор проверки живёт на QTimer.singleShot.

    Пауза между попытками на время теста укорочена, но отмерять ожидание по ней
    нельзя: проверка сессии уходит в общий QThreadPool, а он в полном прогоне
    занят другими тестами, и третий ответ приходил уже после выхода из цикла —
    тест краснел только вместе со всеми. Дедлайн тут страховка от зависания,
    а выходим по делу: либо вход состоялся, либо окно сдалось."""
    deadline = QTimer()
    deadline.setSingleShot(True)
    loop = QEventLoop()
    deadline.timeout.connect(loop.quit)
    deadline.start(30000)

    def stop_when_done():
        # `_session_handled` опускается ровно там, где окно отказалось выпускать
        # с нерабочим входом: по одному счётчику попыток мы уходили раньше, чем
        # отработал последний ответ, и проверка читала ещё не выставленный статус
        if page.emitted or not page._session_handled:
            loop.quit()

    poll = QTimer()
    poll.timeout.connect(stop_when_done)
    poll.start(1)
    loop.exec()
    poll.stop()
    QCoreApplication.processEvents()


def settle_until(done, timeout_ms=30000):
    """Крутить очередь событий, пока не выполнится условие (или не выйдет срок)."""
    deadline = QTimer()
    deadline.setSingleShot(True)
    loop = QEventLoop()
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout_ms)
    poll = QTimer()
    poll.timeout.connect(lambda: loop.quit() if done() else None)
    poll.start(1)
    loop.exec()
    poll.stop()
    QCoreApplication.processEvents()


def with_checks(page, answers):
    """Ответы VK по порядку: `_check_session` берёт следующий, не ходя в сеть."""
    def check(token_data):
        page._session_tries += 1
        page.checks += 1
        ok = answers[min(page.checks - 1, len(answers) - 1)]
        page._on_session_checked(token_data, ok, None)
    page._check_session = check
    return page


class SessionRetryTests(unittest.TestCase):

    def setUp(self):
        qt_app()
        self.token = {'access_token': 'x', 'user_id': 1}
        # Настоящая пауза — секунды: тест ждал бы её впустую
        self._real_delay = login_mod._SESSION_RETRY_MS
        login_mod._SESSION_RETRY_MS = 1

    def tearDown(self):
        login_mod._SESSION_RETRY_MS = self._real_delay

    def test_first_refusal_does_not_end_the_login(self):
        """Отказ по первому ответу — то самое ложное «сессии сайта нет»."""
        page = with_checks(Skeleton(), [False, True])
        page._succeed(self.token)
        settle(page)
        self.assertEqual(page.emitted, [self.token])
        self.assertEqual(page.checks, 2)
        self.assertEqual(page.accepted, 1)

    def test_gives_up_after_the_last_try(self):
        """VK молчит по-настоящему — выпускать с нерабочим входом нельзя."""
        page = with_checks(Skeleton(), [False])
        page._succeed(self.token)
        settle(page)
        self.assertEqual(page.emitted, [])
        self.assertEqual(page.checks, login_mod._SESSION_RETRIES)
        self.assertIn('сессии сайта нет', page.status)
        self.assertEqual(page.urls, [login_mod.VK_SITE_URL])

    def test_token_is_not_requested_twice(self):
        """Ровно то, на что жаловался пользователь: «два раза пришлось заходить в kate».

        После отказа окно возвращает человека на сайт, и VK по дороге снова показывает
        редирект OAuth. Раньше `_token_pending` тут сбрасывался, окно ловило старый
        токен как новый и вело на вторую выдачу прав."""
        page = with_checks(Skeleton(), [False])
        page._succeed(self.token)
        settle(page)
        self.assertTrue(page._token_pending)

    def test_success_on_the_first_answer_does_not_wait(self):
        page = with_checks(Skeleton(), [True])
        page._succeed(self.token)
        self.assertEqual(page.checks, 1)
        self.assertEqual(page.emitted, [self.token])

    def test_verified_token_is_remembered_for_the_second_attempt(self):
        """«Продолжить» после отказа должен перепроверять сессию, а не просить токен."""
        page = with_checks(Skeleton(), [False])
        page._on_new_token_verified(self.token, None)
        self.assertEqual(page._verified_token, self.token)


class TokenStep(Skeleton):
    """Скелет для шага «есть сессия — нужен токен»: сюда добавлены только те поля,
    что читает `_on_session_ready`, и подменён уход за новым токеном."""

    _on_session_ready = login_mod.VkWebLoginDialog._on_session_ready
    _on_continue_clicked = login_mod.VkWebLoginDialog._on_continue_clicked
    _request_new_token = login_mod.VkWebLoginDialog._request_new_token
    _on_saved_token_verified = login_mod.VkWebLoginDialog._on_saved_token_verified

    def __init__(self, has_site_session=True):
        super().__init__()
        self._session_handled = False
        self._token_pending = False
        self._token_data = None
        self._has_site_session = has_site_session
        self.verified = []
        self.asked_new_token = 0
        self.saved_calls = 0

    def _has_session_cookie(self):
        return self._has_site_session

    def _save_cookies(self):
        self.saved_calls += 1
        return 42

    def _request_new_token(self):
        self.asked_new_token += 1
        login_mod.VkWebLoginDialog._request_new_token(self)


class SavedTokenTests(unittest.TestCase):
    """Мёртвый сохранённый токен не должен останавливать вход.

    Повод — жалоба «при входе не перенаправляет в katemobile и по сути всё стопорится».
    По журналу: VK отверг токен кодом 5, `main_window` стёр файл, а окно входа держало
    прежнее значение снимком из конструктора и уходило проверять заведомо мёртвый
    токен вместо страницы прав."""

    def setUp(self):
        qt_app()
        self._real_load = login_mod.config.load_vk_token
        self._real_clear = login_mod.config.clear_vk_token
        self._real_async = login_mod.run_async
        self.cleared = 0

        def clear():
            self.cleared += 1
        login_mod.config.clear_vk_token = clear

    def tearDown(self):
        login_mod.config.load_vk_token = self._real_load
        login_mod.config.clear_vk_token = self._real_clear
        login_mod.run_async = self._real_async

    def test_cleared_token_file_sends_user_to_kate(self):
        """Файла токена нет — окно обязано вести на выдачу прав, а не проверять пустоту."""
        login_mod.config.load_vk_token = lambda: None
        page = TokenStep()
        page._token_data = {'access_token': 'мёртвый'}   # снимок из конструктора
        page._on_session_ready()
        self.assertEqual(page.asked_new_token, 1)
        self.assertIn('Kate Mobile', page.status)
        self.assertEqual(page.urls, [login_mod.AUTH_URL])

    def test_saved_token_is_reread_from_disk(self):
        """Токен берём с диска: за время окна его могли обновить или стереть."""
        fresh = {'access_token': 'свежий', 'user_id': 1}
        login_mod.config.load_vk_token = lambda: fresh
        page = TokenStep()
        page._token_data = {'access_token': 'старый'}
        login_mod.run_async = lambda fn, cb, arg: page.verified.append(arg)
        page._on_session_ready()
        self.assertEqual(page.verified, [fresh])
        self.assertEqual(page.asked_new_token, 0)

    def test_dead_token_file_is_erased_before_asking_for_a_new_one(self):
        """Иначе мёртвый токен переживёт окно и повторит ту же остановку при следующем запуске."""
        page = TokenStep()
        page._on_saved_token_verified(None, ValueError('invalid access_token'))
        self.assertEqual(self.cleared, 1)
        self.assertIsNone(page._token_data)
        self.assertEqual(page.asked_new_token, 1)


class OAuthPageFailureTests(unittest.TestCase):
    """Страница прав не открылась — человеку надо это сказать.

    Раньше на экране оставалось обещание «сейчас откроется страница Kate Mobile»,
    кнопка «Продолжить» была недоступна, и выйти можно было только закрыв окно."""

    class _Page(Skeleton):
        _on_load_finished = login_mod.VkWebLoginDialog._on_load_finished

        def __init__(self):
            super().__init__()
            self._awaiting_oauth = True
            self._token_pending = False
            self._session_handled = True
            self.continue_enabled = False

        def _on_url_changed(self, url):
            pass

        @property
        def _continue_btn(self):
            outer = self

            class _Btn:
                def setEnabled(self, value):
                    outer.continue_enabled = value
            return _Btn()

        @property
        def _view(self):
            class _V:
                def url(self):
                    return QUrl('https://oauth.vk.ru/authorize')
            return _V()

    def setUp(self):
        qt_app()

    def test_failed_oauth_page_is_reported_and_recoverable(self):
        page = self._Page()
        page._on_load_finished(False)
        self.assertIn('не открылась', page.status)
        self.assertTrue(page.continue_enabled)
        # «Продолжить» должен снова повести за токеном, а не считать шаг пройденным
        self.assertFalse(page._session_handled)
        self.assertFalse(page._awaiting_oauth)

    def test_failed_page_with_token_in_address_is_left_alone(self):
        """blank.html часто отдаёт ошибку сети, но токен уже в адресе — это успех."""
        page = self._Page()
        page._token_pending = True
        page._on_load_finished(False)
        self.assertEqual(page.status, '')
        self.assertTrue(page._session_handled)


class RetryLoopTests(unittest.TestCase):
    """Окно не должно молотить VK по кругу.

    Повод — журнал пользователя: 18 повторов входа за три минуты, ровно по три
    проверки каждые 8-9 секунд. Исчерпав попытки, окно уводило человека на сайт VK,
    загрузка этой страницы сама поднимала `_on_session_ready`, тот обнулял счётчик —
    и всё начиналось заново, пока окно открыто."""

    def setUp(self):
        qt_app()
        self.token = {'access_token': 'x', 'user_id': 1}
        self._real_delay = login_mod._SESSION_RETRY_MS
        login_mod._SESSION_RETRY_MS = 1

    def tearDown(self):
        login_mod._SESSION_RETRY_MS = self._real_delay

    def _exhausted(self):
        """Скелет после круга проверок, закончившегося отказом."""
        page = with_checks(TokenStep(), [False])
        page._verified_token = self.token
        page._succeed(self.token)
        # Ждём именно поднятого флага, а не общего `settle`: на этом скелете
        # `_session_handled` опущен с самого начала, и `settle` вышел бы посреди круга,
        # оставив последний повтор на таймере — он лёг бы в счётчик уже внутри проверки
        settle_until(lambda: page._awaiting_user_continue)
        return page

    def test_page_reload_does_not_start_a_new_round(self):
        """Главный случай: страница догрузилась сама — новых обращений к VK нет."""
        page = self._exhausted()
        spent = page.checks
        for _ in range(5):
            page._on_session_ready()
        self.assertEqual(page.checks, spent)

    def test_continue_click_starts_a_new_round(self):
        """Осознанное нажатие по-прежнему даёт человеку второй шанс."""
        page = self._exhausted()
        spent = page.checks
        page._on_continue_clicked()
        self.assertGreater(page.checks, spent)

    def test_blocked_account_stops_at_the_first_answer(self):
        """VK ответил «заблокирован» — повторы бессмысленны, добивать его незачем."""
        page = TokenStep()
        page._verified_token = self.token
        blocked = login_mod.VkAccountBlocked('VK заблокировал ваш аккаунт.')

        def check(token_data):
            page._session_tries += 1
            page.checks += 1
            page._on_session_checked(token_data, None, blocked)
        page._check_session = check

        page._succeed(self.token)
        settle_until(lambda: page._awaiting_user_continue)
        self.assertEqual(page.checks, 1)
        self.assertEqual(page.emitted, [])
        self.assertIn('заблокировал', page.status)

    def test_blocked_account_does_not_bounce_the_page(self):
        """Ходить на сайт по кругу тоже не надо: чинится это только на стороне VK."""
        page = TokenStep()
        page._verified_token = self.token
        blocked = login_mod.VkAccountBlocked('VK заблокировал ваш аккаунт.')

        def check(token_data):
            page._session_tries += 1
            page.checks += 1
            page._on_session_checked(token_data, None, blocked)
        page._check_session = check

        page._succeed(self.token)
        settle_until(lambda: page._awaiting_user_continue)
        for _ in range(5):
            page._on_session_ready()
        self.assertEqual(page.checks, 1)


if __name__ == '__main__':
    unittest.main()
