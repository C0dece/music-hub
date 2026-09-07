"""Сессия VK: кука должна доезжать до домена, с которого мы берём музыку.

Здесь проверяется корень «повторного входа»: вход через VK ID кладёт remixsid на
`.vk.com`, а все запросы за треками уходят на `m.vk.ru`. requests соблюдает домен
строго и такую куку туда не отправляет — VK отвечает адресом страницы входа,
приложение просит войти снова, повторный вход кладёт куку на тот же чужой домен,
и круг замыкается. Проверки «есть ли remixsid» по одному имени этого не видели.

Главный тест здесь не про булев ответ функции, а про подготовленный запрос: куку
в заголовке `Cookie` для m.vk.ru подделать нельзя, и она либо есть, либо нет.
"""
import http.cookiejar
import tempfile
import unittest
from pathlib import Path

import requests

from app import config
from app.core import vk_client
from app.core.vk_web_login import _is_site_domain, merge_cookies_to_file


def cookie(name='remixsid', domain='.vk.ru', value='sid-value'):
    """Кука в том виде, в каком её кладёт в мешок сам http.cookiejar."""
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith('.'),
        path='/', path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={})


def sent_to(session, url):
    """Какие куки requests реально приложит к запросу на этот адрес."""
    request = requests.Request('GET', url)
    prepared = session.prepare_request(request)
    return prepared.headers.get('Cookie', '')


class DomainTests(unittest.TestCase):
    """`_is_site_domain`: что считается рабочим доменом."""

    def test_site_domain_and_its_subdomains(self):
        for domain in ('vk.ru', '.vk.ru', 'm.vk.ru', '.m.vk.ru'):
            self.assertTrue(_is_site_domain(domain), domain)

    def test_other_vk_domains_are_not_the_site(self):
        """`.vk.com` — тот же сайт для человека, но не для requests."""
        for domain in ('vk.com', '.vk.com', 'm.vk.com', 'login.vk.com'):
            self.assertFalse(_is_site_domain(domain), domain)

    def test_lookalike_domain_is_rejected(self):
        """Проверка идёт по границе метки, а не по концу строки."""
        self.assertFalse(_is_site_domain('evilvk.ru'))
        self.assertFalse(_is_site_domain(''))


class ReachTests(unittest.TestCase):
    """`_session_reaches_site`: доедет ли кука до запросов за музыкой."""

    def test_cookie_on_the_site_domain_counts(self):
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.ru'))
        self.assertTrue(vk_client._session_reaches_site(session))

    def test_cookie_only_on_vk_com_does_not_count(self):
        """Ровно случай входа через VK ID — раньше он считался успехом."""
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.com'))
        self.assertFalse(vk_client._session_reaches_site(session))

    def test_other_cookies_are_not_a_session(self):
        session = requests.Session()
        session.cookies.set_cookie(cookie(name='remixlang', domain='.vk.ru'))
        session.cookies.set_cookie(cookie(name='remixmsts', domain='.vk.ru'))
        self.assertFalse(vk_client._session_reaches_site(session))


class MirrorTests(unittest.TestCase):
    """`_mirror_session_cookie`: одна сессия — оба домена одного сайта."""

    def test_cookie_from_vk_com_starts_reaching_the_site(self):
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.com'))
        self.assertNotIn('remixsid', sent_to(session, vk_client._SECTION_URL))

        vk_client._mirror_session_cookie(session)

        # Это и есть суть починки: заголовок запроса на m.vk.ru несёт сессию
        self.assertIn('remixsid', sent_to(session, vk_client._SECTION_URL))
        self.assertTrue(vk_client._session_reaches_site(session))

    def test_the_original_cookie_stays_where_it_was(self):
        """Зеркало добавляет, а не переносит: vk.com тоже должен работать."""
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.com'))
        vk_client._mirror_session_cookie(session)
        self.assertIn('remixsid', sent_to(session, 'https://vk.com/audio'))

    def test_a_live_cookie_is_never_overwritten_by_the_neighbour(self):
        """Своя сессия у рабочего домена уже есть — трогать её нельзя.

        В файле кук месяцами лежит remixsid со старых входов через VK ID. Если
        раскладывать куку на оба домена подряд, этот мусор затрёт только что
        полученную рабочую — вход держался бы до первого перечитывания файла,
        и «повторный вход» вернулся бы другой дорогой."""
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.ru', value='живая'))
        session.cookies.set_cookie(cookie(domain='.vk.com', value='мусор-с-прошлого-входа'))

        vk_client._mirror_session_cookie(session)

        self.assertIn('живая', sent_to(session, vk_client._SECTION_URL))
        self.assertNotIn('мусор', sent_to(session, vk_client._SECTION_URL))

    def test_mirroring_twice_changes_nothing(self):
        """Файл перечитывают при каждом фоновом перезаходе — накопления быть не должно."""
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.com', value='сессия'))
        vk_client._mirror_session_cookie(session)
        vk_client._mirror_session_cookie(session)
        self.assertEqual(len(vk_client._session_cookies(session)), 2)
        self.assertIn('сессия', sent_to(session, vk_client._SECTION_URL))

    def test_value_is_copied_as_is(self):
        session = requests.Session()
        session.cookies.set_cookie(cookie(domain='.vk.com', value='живая-сессия'))
        vk_client._mirror_session_cookie(session)
        self.assertIn('живая-сессия', sent_to(session, vk_client._SECTION_URL))

    def test_ordinary_cookies_are_left_alone(self):
        """Зеркалим только сессию: остальное VK ставит сам, где ему нужно."""
        session = requests.Session()
        session.cookies.set_cookie(cookie(name='remixlang', domain='.vk.com'))
        vk_client._mirror_session_cookie(session)
        self.assertNotIn('remixlang', sent_to(session, vk_client._SECTION_URL))


class LoadFromFileTests(unittest.TestCase):
    """`_load_vk_cookies_from_file`: ответ «сессия есть» по сохранённому файлу."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._real = config.VK_COOKIES_FILE
        config.VK_COOKIES_FILE = Path(self._tmp.name) / 'vk_cookies.txt'

    def tearDown(self):
        config.VK_COOKIES_FILE = self._real
        self._tmp.cleanup()

    def save(self, *cookies):
        jar = http.cookiejar.MozillaCookieJar(str(config.VK_COOKIES_FILE))
        for c in cookies:
            jar.set_cookie(c)
        jar.save(ignore_discard=True, ignore_expires=True)

    def test_missing_file_is_not_a_session(self):
        session = requests.Session()
        self.assertFalse(vk_client._load_vk_cookies_from_file(session))

    def test_file_without_a_session_cookie_is_not_a_session(self):
        """Файл после входа через VK ID существует всегда — сам по себе он ничего не значит."""
        self.save(cookie(name='remixlang', domain='.vk.ru'),
                  cookie(name='remixmsts', domain='.vk.ru'))
        session = requests.Session()
        self.assertFalse(vk_client._load_vk_cookies_from_file(session))

    def test_session_saved_on_vk_com_is_mirrored_and_accepted(self):
        """Вход через VK ID: кука пришла на чужой домен, но сайт один и тот же."""
        self.save(cookie(domain='.vk.com'), cookie(name='remixlang', domain='.vk.ru'))
        session = requests.Session()
        self.assertTrue(vk_client._load_vk_cookies_from_file(session))
        self.assertIn('remixsid', sent_to(session, vk_client._SECTION_URL))


class MergeSaveTests(unittest.TestCase):
    """`merge_cookies_to_file`: частичный сбор кук не должен убивать рабочий вход.

    Замер по журналу: keeper уходил по первой `remixsid` с четырьмя куками в руках и
    переписывал ими файл из двадцати пяти. `remixsid` оставалась на месте, а сессия
    умирала — VK на первом же запросе отвечал страницей входа."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._real = config.VK_COOKIES_FILE
        config.VK_COOKIES_FILE = Path(self._tmp.name) / 'vk_cookies.txt'

    def tearDown(self):
        config.VK_COOKIES_FILE = self._real
        self._tmp.cleanup()

    def names(self):
        jar = http.cookiejar.MozillaCookieJar(str(config.VK_COOKIES_FILE))
        jar.load(ignore_discard=True, ignore_expires=True)
        return {c.name: c.value for c in jar}

    def test_partial_harvest_keeps_the_rest_of_the_file(self):
        merge_cookies_to_file([cookie(name=n, value=f'old-{n}') for n in
                               ('remixsid', 'remixnsid', 'httoken', 'remixstid')])
        merge_cookies_to_file([cookie(value='fresh')])
        saved = self.names()
        self.assertEqual(saved['remixsid'], 'fresh')       # свежая вытеснила старую
        self.assertEqual(saved['httoken'], 'old-httoken')  # остальное на месте
        self.assertEqual(len(saved), 4)

    def test_same_name_on_another_domain_is_kept_separately(self):
        """remixsid для `.vk.com` и `.vk.ru` — разные куки, затирать друг друга нельзя."""
        merge_cookies_to_file([cookie(domain='.vk.com', value='com')])
        merge_cookies_to_file([cookie(domain='.vk.ru', value='ru')])
        jar = http.cookiejar.MozillaCookieJar(str(config.VK_COOKIES_FILE))
        jar.load(ignore_discard=True, ignore_expires=True)
        self.assertEqual({c.domain for c in jar}, {'.vk.com', '.vk.ru'})

    def test_missing_file_is_just_a_first_save(self):
        merge_cookies_to_file([cookie(value='first')])
        self.assertEqual(self.names(), {'remixsid': 'first'})


class BlockedAccountTests(unittest.TestCase):
    """Блокировка аккаунта и протухшая сессия внешне неразличимы, а лечатся по-разному.

    VK на заблокированном аккаунте уводит на `/login?act=blocked`: адрес содержит
    `login`, и старая проверка честно считала это «войдите заново». Программа уходила
    в бесконечный тихий перезаход, который не мог закончиться ничем. Здесь заперта
    ровно эта развилка — чтобы её случайно не выпрямили обратно."""

    def test_blocked_redirect_is_not_an_ordinary_login_redirect(self):
        for location in ('https://m.vk.ru/login?act=blocked',
                         'https://m.vk.com/vkui/blocked/',
                         '/blocked'):
            with self.subTest(location=location):
                payload = {'location': location}
                self.assertTrue(vk_client._is_blocked_location(payload))

    def test_plain_login_redirect_is_not_a_block(self):
        """Обычный «войдите заново» обязан лечиться перезаходом, а не отказом."""
        payload = {'location': 'https://login.vk.ru/?act=grant_access'}
        self.assertFalse(vk_client._is_blocked_location(payload))
        self.assertTrue(vk_client._needs_login(payload))

    def test_payload_without_location_is_neither(self):
        for payload in ({'data': [[]]}, None, 'что-то не то'):
            with self.subTest(payload=payload):
                self.assertFalse(vk_client._is_blocked_location(payload))

    def test_api_error_text_separates_block_from_revoked_token(self):
        """Код 5 у VK один на оба случая — отличаем по тексту, другого признака нет."""
        blocked = Exception('[5] User authorization failed: user is blocked.')
        revoked = Exception('[5] User authorization failed: invalid access_token.')
        self.assertTrue(vk_client._is_blocked_api_error(blocked))
        self.assertFalse(vk_client._is_blocked_api_error(revoked))

    def test_check_web_session_raises_instead_of_asking_for_a_new_login(self):
        """False здесь значит «чини перезаходом». Чинить нечего — уходит исключение."""
        class FakeResponse:
            def json(self):
                return {'location': 'https://m.vk.ru/login?act=blocked'}

        class FakeSession:
            def post(self, *a, **kw):
                return FakeResponse()

        saved_session, saved_load = (vk_client._TimeoutSession,
                                     vk_client._load_vk_cookies_from_file)
        vk_client._TimeoutSession = FakeSession
        vk_client._load_vk_cookies_from_file = lambda session: True
        try:
            with self.assertRaises(vk_client.VkAccountBlocked):
                vk_client.check_web_session(1)
        finally:
            vk_client._TimeoutSession = saved_session
            vk_client._load_vk_cookies_from_file = saved_load

    def test_blocked_is_an_auth_error_but_never_a_reason_to_wipe_the_token(self):
        """Токен цел и пригодится после разблокировки: стирать его было бы вредно."""
        exc = vk_client.VkAccountBlocked(vk_client.BLOCKED_MESSAGE)
        self.assertIsInstance(exc, vk_client.VkAuthError)
        self.assertFalse(exc.token_rejected)
        self.assertIn('vk.com', str(exc))       # человеку сказано, куда идти


if __name__ == '__main__':
    unittest.main()
