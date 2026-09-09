"""Фоновый перезаход в VK: тихо, редко и никогда не открывая окно входа.

Ни сети, ни QtWebEngine здесь нет - движок подменяется фабрикой, а проверка сессии
и запись кук перехватываются. Проверяем ровно то, ради чего keeper написан: он не
штурмует VK, не дерётся с окном входа за профиль и молчит вместо того, чтобы
показывать пароль-форму.
"""
import time
import unittest

from app.core import vk_session_keeper as keeper_mod
from app.core.vk_client import VkAccountBlocked
from app.core.vk_session_keeper import VkSessionKeeper

from .qt_app import qt_app


class FakeEngine:
    """Движок, которым управляет сам тест: страница «грузится» по команде."""

    def __init__(self, on_cookies, on_error):
        self.on_cookies = on_cookies
        self.on_error = on_error
        self.closed = False

    def close(self):
        self.closed = True


def session_cookie(name='remixsid'):
    """Ключи словаря кук - (домен, имя); значение keeper смотрит только при записи."""
    return {('.vk.ru', name): object()}


class KeeperTests(unittest.TestCase):

    def setUp(self):
        qt_app()
        VkSessionKeeper.unlock_profile()
        self.engines = []
        self.saved = []
        self.checks = []
        self.args = []
        # Запись кук на диск и сетевую проверку подменяем: нас интересует поведение
        # keeper, а не файловая система и не VK
        self._real_save = keeper_mod._save_cookies
        self._real_run = keeper_mod.run_async
        keeper_mod._save_cookies = lambda cookies: self.saved.append(cookies) or len(cookies)

        def fake_run(fn, cb, *a, **kw):
            # Аргументы храним отдельно: `check_web_session` без владельца молча
            # падает с TypeError в чужом потоке, и такую подмену тест обязан ловить
            self.checks.append(cb)
            self.args.append(a)

        keeper_mod.run_async = fake_run

    def tearDown(self):
        keeper_mod._save_cookies = self._real_save
        keeper_mod.run_async = self._real_run
        VkSessionKeeper.unlock_profile()

    def keeper(self, user_id=1):
        """Владелец по умолчанию есть: без входа в VK keeper вообще не запускают.

        Сессию проверяют делом - запросом за треками, - а для запроса нужен чей-то
        id. Случай «владелец неизвестен» проверяем отдельным тестом."""
        def factory(on_cookies, on_error):
            engine = FakeEngine(on_cookies, on_error)
            self.engines.append(engine)
            return engine

        return VkSessionKeeper(engine_factory=factory, user_id=user_id)

    def outcome(self, k):
        """Собираем оба исхода: наружу уходит только сигнал, окон keeper не открывает."""
        result = {'restored': 0, 'failed': []}
        k.restored.connect(lambda: result.__setitem__('restored', result['restored'] + 1))
        k.failed.connect(result['failed'].append)
        return result

    # ---------- удачный путь ----------
    def test_live_session_is_saved_and_reported_quietly(self):
        k = self.keeper()
        out = self.outcome(k)

        self.assertTrue(k.try_restore())
        self.engines[0].on_cookies(session_cookie())
        # Куки записаны, но сессия ещё не подтверждена - сигнала нет
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(out['restored'], 0)

        self.checks[0](True, None)          # VK подтвердил: сессия живая
        self.assertEqual(out['restored'], 1)
        self.assertEqual(out['failed'], [])
        self.assertTrue(self.engines[0].closed)
        self.assertFalse(k.running)

    def test_success_clears_the_attempt_counter(self):
        """После удачи keeper снова готов помочь, а не сидит в отказе до перезапуска."""
        k = self.keeper()
        k.try_restore()
        self.engines[0].on_cookies(session_cookie())
        self.checks[0](True, None)
        # Пауза между попытками тоже сбрасывается: перезаход уже не «слишком ранний»
        k.reset()
        self.assertTrue(k.try_restore())

    # ---------- профилактика ----------
    def test_preventive_visit_does_not_shout_about_success(self):
        """Профилактика прошла - снаружи тишина.

        Сигнал `restored` означает «сессия вернулась», и окно по нему перечитывает
        списки. Но здесь сессия никуда не девалась: заход был именно потому, что она
        жива. Сигнал тут был бы ложью и лишней перезагрузкой списков на ровном месте."""
        k = self.keeper()
        out = self.outcome(k)

        self.assertTrue(k.refresh())
        self.engines[0].on_cookies(session_cookie())
        self.checks[0](True, None)

        self.assertEqual(out['restored'], 0)
        self.assertEqual(out['failed'], [])
        self.assertEqual(len(self.saved), 1)     # куки всё-таки обновились
        self.assertTrue(self.engines[0].closed)
        self.assertFalse(k.running)

    def test_failed_preventive_visit_is_silent_too(self):
        """Не вышло - тоже молчим: чинить нечего, сессия жива.

        `failed` окно понимает как «вход потерян» и начинает починку. Запустить её
        из-за неудачного профилактического захода значило бы сломать работающее."""
        k = self.keeper()
        out = self.outcome(k)

        k.refresh()
        self.engines[0].on_error('VK не открылся')

        self.assertEqual(out['failed'], [])
        self.assertEqual(out['restored'], 0)

    def test_failed_preventive_visit_does_not_eat_repair_attempts(self):
        """Промахи профилактики не копятся в серию, закрывающую путь починке.

        Серия из MAX_ATTEMPTS отправляет keeper в получасовое молчание. Она для того,
        чтобы не долбить недоступный VK попытками починки. Профилактика к этой серии
        отношения не имеет: за сутки её промахи набрали бы лимит, и настоящая
        поломка осталась бы без единой попытки."""
        k = self.keeper()
        for _ in range(keeper_mod.MAX_ATTEMPTS):
            self.assertTrue(k.refresh())
            self.engines[-1].on_error('VK не открылся')
            k._last_try = 0.0                # пауза между попытками тут не проверяется
        # Лимит не выбран - починка по-прежнему доступна
        self.assertTrue(k.try_restore())

    def test_block_during_preventive_visit_is_reported(self):
        """Блокировку молчанием не прикрыть даже на профилактике.

        Это единственная новость, ради которой стоит прервать человека: сама она не
        пройдёт, а до тех пор все походы в VK бессмысленны."""
        k = self.keeper()
        seen = []
        k.blocked.connect(seen.append)

        k.refresh()
        self.engines[0].on_cookies(session_cookie())
        self.checks[0](None, VkAccountBlocked('VK не пускает'))

        self.assertEqual(len(seen), 1)
        self.assertFalse(k.refresh())         # дальше keeper не ходит совсем

    # ---------- неудачи ----------
    def test_no_session_cookie_means_failure_not_a_login_window(self):
        k = self.keeper()
        out = self.outcome(k)
        k.try_restore()
        # VK отдал страницу, но нас не помнит: сессионной куки нет
        self.engines[0].on_cookies({('.vk.ru', 'remixlang'): object()})

        self.assertEqual(len(out['failed']), 1)
        self.assertEqual(out['restored'], 0)
        self.assertEqual(self.saved, [])    # пустым заходом рабочий файл не трогаем
        self.assertTrue(self.engines[0].closed)

    def test_cookie_on_the_wrong_domain_is_not_a_session(self):
        """VK ID кладёт remixsid на `.vk.com`, а музыку мы берём с m.vk.ru.

        Такая кука до наших запросов не доезжает вовсе - requests привязан к
        домену строго. Раньше keeper смотрел только на имя, объявлял успех, и
        человек получал «вход есть, музыки нет» с бесконечным повторным входом."""
        k = self.keeper()
        out = self.outcome(k)
        k.try_restore()
        self.engines[0].on_cookies({('.vk.com', 'remixsid'): object()})

        self.assertEqual(len(out['failed']), 1)
        self.assertEqual(out['restored'], 0)
        self.assertEqual(self.saved, [])
        self.assertEqual(self.checks, [])   # до сетевой проверки дело не дошло

    def test_dead_cookies_are_reported_after_the_check(self):
        k = self.keeper()
        out = self.outcome(k)
        k.try_restore()
        self.engines[0].on_cookies(session_cookie())
        self.checks[0](False, None)         # куки есть, но VK их не принял

        self.assertEqual(len(out['failed']), 1)
        self.assertEqual(out['restored'], 0)

    def test_unknown_owner_is_a_failure_not_a_silent_success(self):
        """Проверить сессию нечем - значит, честная неудача, а не мнимая победа.

        Куки могли лечь мёртвыми, и объявить успех, не спросив VK, - соврать: человек
        нажмёт и получит тот самый сбой, ради устранения которого keeper и написан."""
        k = self.keeper(user_id=None)
        out = self.outcome(k)
        k.try_restore()
        self.engines[0].on_cookies(session_cookie())

        self.assertEqual(len(out['failed']), 1)
        self.assertEqual(out['restored'], 0)
        self.assertEqual(self.checks, [])   # спрашивать VK не о ком
        self.assertFalse(k.running)

    def test_owner_may_be_a_callable_resolved_at_check_time(self):
        """id берём в момент проверки: аккаунт мог смениться за жизнь окна."""
        owner = {'id': 7}
        k = self.keeper(user_id=lambda: owner['id'])
        k.try_restore()
        owner['id'] = 42                    # человек перезашёл под другим аккаунтом
        self.engines[0].on_cookies(session_cookie())

        self.assertEqual(self.args, [(42,)])

    def test_engine_error_ends_the_attempt(self):
        k = self.keeper()
        out = self.outcome(k)
        k.try_restore()
        self.engines[0].on_error('страница VK не загрузилась')

        self.assertEqual(out['failed'], ['страница VK не загрузилась'])
        self.assertFalse(k.running)

    def test_broken_engine_does_not_break_the_program(self):
        """Движка может не быть вовсе - это неудача перезахода, а не падение."""
        def angry(on_cookies, on_error):
            raise RuntimeError('QtWebEngine недоступен')

        k = VkSessionKeeper(engine_factory=angry)
        out = self.outcome(k)
        self.assertFalse(k.try_restore())
        self.assertEqual(len(out['failed']), 1)
        self.assertFalse(k.running)

    # ---------- таймаут ----------
    def test_timeout_closes_a_hung_attempt(self):
        k = self.keeper()
        out = self.outcome(k)
        k.try_restore()
        k._on_timeout()                     # то же, что сделал бы таймер через 30 с

        self.assertEqual(len(out['failed']), 1)
        self.assertTrue(self.engines[0].closed)
        self.assertFalse(k.running)

    def test_late_cookies_after_timeout_are_ignored(self):
        """Движок ответил, когда мы уже сдались, - второго исхода быть не должно."""
        k = self.keeper()
        out = self.outcome(k)
        k.try_restore()
        k._on_timeout()
        self.engines[0].on_cookies(session_cookie())

        self.assertEqual(len(out['failed']), 1)
        self.assertEqual(out['restored'], 0)
        self.assertEqual(self.saved, [])

    # ---------- антишторм ----------
    def test_second_attempt_waits_out_the_pause(self):
        k = self.keeper()
        k.try_restore()
        k._on_timeout()
        # Прошла секунда, а не пять минут: ломиться в VK снова рано
        self.assertFalse(k.try_restore())
        self.assertEqual(len(self.engines), 1)

    def waited(self, k, seconds):
        """Отодвинуть прошлую попытку в прошлое: паузу выдержали, ждать нечего.

        Через `_last_try`, а не через сон: проверяем счётчик и сроки, а не терпение."""
        k._last_try = time.monotonic() - seconds

    def test_no_more_than_three_attempts_in_a_row(self):
        k = self.keeper()
        for _ in range(keeper_mod.MAX_ATTEMPTS):
            self.waited(k, keeper_mod.MIN_INTERVAL)   # проверяем именно счётчик
            self.assertTrue(k.try_restore())
            k._on_timeout()
        self.waited(k, keeper_mod.MIN_INTERVAL)
        self.assertFalse(k.try_restore())
        self.assertEqual(len(self.engines), keeper_mod.MAX_ATTEMPTS)

    def test_exhausted_attempts_are_forgiven_after_the_cooldown(self):
        """Иначе «без участия пользователя» кончается на третьей неудаче.

        Три провала подряд обычно означают, что VK сейчас недоступен, а не что нужен
        пароль. Замолчать до ручного входа - как раз то, чего просили избежать."""
        k = self.keeper()
        for _ in range(keeper_mod.MAX_ATTEMPTS):
            self.waited(k, keeper_mod.MIN_INTERVAL)
            k.try_restore()
            k._on_timeout()

        # Сразу после серии keeper молчит - VK не штурмуем
        self.waited(k, keeper_mod.MIN_INTERVAL)
        self.assertFalse(k.try_restore())

        # А переждав, пробует снова сам, без чьей-либо помощи
        self.waited(k, keeper_mod.COOLDOWN)
        self.assertTrue(k.try_restore())

    def test_parallel_attempt_is_refused(self):
        """Пока один заход идёт, второй движок на том же профиле не поднимаем."""
        k = self.keeper()
        self.assertTrue(k.try_restore())
        self.assertFalse(k.try_restore())
        self.assertEqual(len(self.engines), 1)

    # ---------- взаимная блокировка с окном входа ----------
    def test_open_login_window_blocks_the_keeper(self):
        k = self.keeper()
        VkSessionKeeper.lock_profile()      # человек сам открыл окно входа
        self.assertFalse(k.try_restore())
        self.assertEqual(self.engines, [])

        VkSessionKeeper.unlock_profile()
        self.assertTrue(k.try_restore())

    def test_keeper_releases_the_profile_when_it_finishes(self):
        """Иначе окно входа после неудачного перезахода осталось бы заблокированным."""
        k = self.keeper()
        k.try_restore()
        self.assertTrue(VkSessionKeeper._profile_busy)
        k._on_timeout()
        self.assertFalse(VkSessionKeeper._profile_busy)

    # ---------- блокировка аккаунта ----------
    def test_blocked_account_stops_the_series_instead_of_retrying(self):
        """Единственный случай, когда повторять бессмысленно: VK не пускает аккаунт.

        Раньше блокировка приходила сюда обычной ошибкой, keeper объявлял неудачу, и
        снаружи заводился таймер следующей попытки - по кругу до перезапуска программы."""
        k = self.keeper()
        out = self.outcome(k)
        blocked = []
        k.blocked.connect(blocked.append)

        k.try_restore()
        self.engines[0].on_cookies(session_cookie())
        self.checks[0](None, VkAccountBlocked('VK заблокировал аккаунт'))

        self.assertEqual(len(blocked), 1)
        self.assertIn('заблокировал', blocked[0])
        # `failed` не шлём: он значит «попробуем позже», а пробовать нечего
        self.assertEqual(out['failed'], [])
        self.assertEqual(out['restored'], 0)
        # Движок закрыт и профиль отпущен - окно входа должно остаться доступным
        self.assertTrue(self.engines[0].closed)
        self.assertFalse(k.running)

    def test_blocked_account_refuses_the_next_attempt(self):
        """Счётчик выкручен до предела нарочно: тихий перезаход тут только вредит."""
        k = self.keeper()
        k.try_restore()
        self.engines[0].on_cookies(session_cookie())
        self.checks[0](None, VkAccountBlocked('VK заблокировал аккаунт'))

        self.assertFalse(k.try_restore())
        self.assertEqual(len(self.engines), 1)   # второй движок не заводился




class HeadlessSettleTests(unittest.TestCase):
    """Ожидание кук после загрузки страницы.

    Сам `HeadlessVkPage` тянет за собой QtWebEngine, которого в прогоне нет, поэтому
    проверяем его логику на «скелете»: объект с теми же методами и теми же полями,
    но без движка. Ровно та развилка, на которой keeper терял попытки: загрузка
    кончилась, а сессионная кука ещё едет из профиля."""

    def setUp(self):
        qt_app()

    def page(self):
        """Копия поведения HeadlessVkPage без QWebEnginePage и QWebEngineProfile."""
        from PySide6.QtCore import QTimer

        from app.core import vk_headless

        class Skeleton:
            _on_load_finished = vk_headless.HeadlessVkPage._on_load_finished
            _on_settled = vk_headless.HeadlessVkPage._on_settled
            _finish = vk_headless.HeadlessVkPage._finish
            _fail = vk_headless.HeadlessVkPage._fail
            _on_cookie_added = vk_headless.HeadlessVkPage._on_cookie_added

            def __init__(self, on_cookies, on_error):
                self._on_cookies = on_cookies
                self._on_error = on_error
                self._cookies = {}
                self._done = False
                self._settle = QTimer()
                self._settle.setSingleShot(True)
                self._settle.timeout.connect(self._on_settled)

        return Skeleton

    def test_load_finished_waits_for_cookies_instead_of_giving_up(self):
        """Куки профиля приходят после загрузки - и попытка обязана их дождаться."""
        got = []
        page = self.page()(got.append, lambda reason: got.append(reason))
        page._on_load_finished(True)
        self.assertEqual(got, [])                    # ещё не сдались
        self.assertTrue(page._settle.isActive())

    def test_cookie_after_load_shortens_the_wait_but_does_not_end_it(self):
        """Сессионная кука - сигнал «почти всё», а не «всё».

        Уйти прямо по ней значит унести 4 куки вместо 25: `remixnsid`, `httoken` и
        прочие приезжают следом, а без них VK показывает страницу входа."""
        from PySide6.QtNetwork import QNetworkCookie
        from app.core import vk_headless

        got = []
        page = self.page()(got.append, lambda reason: got.append(reason))
        page._on_load_finished(True)
        cookie = QNetworkCookie(b'remixsid', b'value123')
        cookie.setDomain('.vk.ru')
        page._on_cookie_added(cookie)
        self.assertEqual(got, [])                    # ждём хвост
        self.assertTrue(page._settle.isActive())
        self.assertLessEqual(page._settle.remainingTime(), vk_headless.TAIL_MS)

        page._on_settled()                           # хвост доехал, время вышло
        self.assertEqual(len(got), 1)
        self.assertIn(('.vk.ru', 'remixsid'), got[0])

    def test_tail_keeps_cookies_that_arrive_after_the_session_one(self):
        """Ровно ради этого хвост и нужен: сопутствующие куки попадают в набор."""
        from PySide6.QtNetwork import QNetworkCookie

        got = []
        page = self.page()(got.append, lambda reason: got.append(reason))
        page._on_load_finished(True)
        for name in (b'remixsid', b'remixnsid', b'httoken'):
            cookie = QNetworkCookie(name, b'value123')
            cookie.setDomain('.vk.ru')
            page._on_cookie_added(cookie)
        page._on_settled()
        self.assertEqual(len(got), 1)
        self.assertIn(('.vk.ru', 'httoken'), got[0])
        self.assertEqual(len(got[0]), 3)

    def test_cookie_from_another_domain_does_not_end_the_attempt(self):
        """`.vk.com` приходит первой, нужная - следом, при переходе на m.vk.ru.

        Уйти по чужой куке значило бы бросить страницу на полпути и вернуть
        keeper'у набор без сессии для рабочего домена."""
        from PySide6.QtNetwork import QNetworkCookie

        got = []
        page = self.page()(got.append, lambda reason: got.append(reason))
        page._on_load_finished(True)
        stray = QNetworkCookie(b'remixsid', b'value123')
        stray.setDomain('.vk.com')
        page._on_cookie_added(stray)
        self.assertEqual(got, [])                    # ещё ждём свою
        self.assertTrue(page._settle.isActive())

        good = QNetworkCookie(b'remixsid', b'value456')
        good.setDomain('.vk.ru')
        page._on_cookie_added(good)
        page._on_settled()
        self.assertEqual(len(got), 1)
        self.assertIn(('.vk.ru', 'remixsid'), got[0])

    def test_settle_timeout_gives_up_with_what_it_has(self):
        """Куки так и не пришли: уходим с пустым набором, врать про успех нельзя."""
        got = []
        page = self.page()(got.append, lambda reason: got.append(reason))
        page._on_load_finished(True)
        page._on_settled()
        self.assertEqual(got, [{}])

if __name__ == '__main__':
    unittest.main()
