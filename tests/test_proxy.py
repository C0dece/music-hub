"""Прокси: браузер не должен остаться на мёртвом адресе.

Встроенный Chromium читает адрес прокси один раз — когда поднимается движок.
Поэтому посредник обхода блокировки обязан держать свой порт, а смена настроек
должна доезжать до уже запущенного движка настройкой приложения Qt."""
import unittest
from unittest import mock

from app.core import frag_proxy, proxy


class FragProxyPortTests(unittest.TestCase):
    """Порт посредника живёт, пока живёт приложение."""

    def tearDown(self):
        frag_proxy.stop()

    def test_port_survives_upstream_change(self):
        first = frag_proxy.start(None)
        self.assertIsNotNone(first)
        second = frag_proxy.start('http://127.0.0.1:7897')
        self.assertEqual(first, second)

    def test_port_survives_repeated_apply(self):
        first = frag_proxy.start('http://127.0.0.1:7897')
        for _ in range(3):
            self.assertEqual(frag_proxy.start('http://127.0.0.1:1080'), first)

    def test_upstream_actually_switched(self):
        frag_proxy.start(None)
        frag_proxy.start('http://user:pass@127.0.0.1:7897')
        current = frag_proxy._current
        self.assertEqual(current._upstream.port, 7897)
        self.assertTrue(current._auth)  # логин и пароль пересобраны вместе с адресом

    def test_new_proxy_after_stop(self):
        first = frag_proxy.start(None)
        frag_proxy.stop()
        second = frag_proxy.start(None)
        self.assertNotEqual(first, second)


class QtProxyTests(unittest.TestCase):
    """Адрес, который получает уже работающий QtWebEngine."""

    def tearDown(self):
        frag_proxy.stop()
        proxy.apply({'proxy_mode': proxy.MODE_AUTO})

    def _applied(self, settings: dict):
        with mock.patch.object(proxy, '_apply_qt_proxy') as applied:
            proxy.apply(settings)
        self.assertTrue(applied.called)
        return applied.call_args[0]

    def test_manual_proxy_reaches_engine(self):
        mode, url = self._applied({'proxy_mode': proxy.MODE_MANUAL,
                                   'proxy_url': '10.0.0.1:8080'})
        self.assertEqual(mode, proxy.MODE_MANUAL)
        self.assertIn('10.0.0.1:8080', url)

    def test_bypass_gives_engine_local_address(self):
        _mode, url = self._applied({'proxy_mode': proxy.MODE_MANUAL,
                                    'proxy_url': '10.0.0.1:8080',
                                    'proxy_fragment': True})
        # За посредником прячется настоящий прокси, браузеру достаётся localhost
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        self.assertNotIn('10.0.0.1', url)

    def test_off_mode_reaches_engine(self):
        mode, url = self._applied({'proxy_mode': proxy.MODE_OFF})
        self.assertEqual(mode, proxy.MODE_OFF)
        self.assertIsNone(url)

    def test_detected_later_reaches_engine(self):
        """Главный случай: прокси нашёлся уже после запуска приложения."""
        proxy.apply({'proxy_mode': proxy.MODE_AUTO})
        with mock.patch.object(proxy, 'system_proxy', return_value=None), \
             mock.patch.object(proxy, '_apply_qt_proxy') as applied:
            proxy.set_detected('http://127.0.0.1:7897')
        self.assertTrue(applied.called)
        self.assertEqual(applied.call_args[0][1], 'http://127.0.0.1:7897')


class VkBypassTests(unittest.TestCase):
    """VK ходит мимо прокси.

    Вход в аккаунт с зарубежного адреса VK считает угоном и морозит аккаунт до
    подтверждения по телефону — так пользователь и попал в блокировку дважды подряд.
    Прокси нужен ради YouTube, поэтому VK обязан идти со своего адреса."""

    def setUp(self):
        proxy._set(proxy.MODE_MANUAL, 'http://127.0.0.1:7897', False)

    def tearDown(self):
        frag_proxy.stop()
        proxy.apply({'proxy_mode': proxy.MODE_AUTO})

    def test_vk_hosts_bypass(self):
        for url in ('https://api.vk.ru/method/users.get',
                    'https://vk.com/',
                    'https://m.vk.ru/audio',
                    'https://login.vk.ru/',
                    'https://cs9-4.vkuseraudio.net/a.mp3'):
            self.assertTrue(proxy.bypasses_proxy(url), url)

    def test_other_hosts_do_not_bypass(self):
        for url in ('https://www.youtube.com/', 'https://redirector.googlevideo.com/'):
            self.assertFalse(proxy.bypasses_proxy(url), url)

    def test_lookalike_domains_do_not_bypass(self):
        """Сравниваем по имени узла, а не по вхождению строки."""
        for url in ('https://notvk.com/', 'https://vk.com.attacker.net/',
                    'https://myuserapi.com/'):
            self.assertFalse(proxy.bypasses_proxy(url), url)

    def test_session_gets_no_proxy(self):
        import requests
        from requests.utils import should_bypass_proxies

        session = requests.Session()
        proxy.apply_to_session(session)
        no_proxy = session.proxies.get('no_proxy')
        self.assertTrue(should_bypass_proxies('https://api.vk.ru/method/x', no_proxy))
        self.assertFalse(should_bypass_proxies('https://www.youtube.com/', no_proxy))

    def test_chromium_bypass_covers_bare_domain(self):
        """Окно входа открывается на самом vk.com, а «*.vk.com» его не покрывает."""
        import os

        flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
        bypass = next(f.split('=', 1)[1] for f in flags.split()
                      if f.startswith('--proxy-bypass-list='))
        entries = bypass.split(';')
        self.assertIn('vk.com', entries)
        self.assertIn('*.vk.com', entries)

    def test_direct_mode_keeps_no_proxy_star(self):
        """Режим «без прокси» по-прежнему отменяет системные настройки целиком."""
        import os

        proxy._set(proxy.MODE_OFF, None, False)
        self.assertEqual(os.environ.get('NO_PROXY'), '*')


class QtFactoryTests(unittest.TestCase):
    """Выбор прокси для уже запущенного движка: VK напрямую, остальное через прокси."""

    def tearDown(self):
        frag_proxy.stop()
        proxy.apply({'proxy_mode': proxy.MODE_AUTO})

    def _route(self, host: str):
        from PySide6.QtNetwork import QNetworkProxyFactory, QNetworkProxyQuery

        query = QNetworkProxyQuery()
        query.setPeerHostName(host)
        query.setPeerPort(443)
        return QNetworkProxyFactory.proxyForQuery(query)[0]

    def test_factory_splits_vk_and_rest(self):
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QNetworkProxy

        if QCoreApplication.instance() is None:
            self._app = QCoreApplication([])
        proxy._set(proxy.MODE_MANUAL, 'http://127.0.0.1:7897', False)

        self.assertEqual(self._route('api.vk.ru').type(),
                         QNetworkProxy.ProxyType.NoProxy)
        youtube = self._route('www.youtube.com')
        self.assertEqual(youtube.type(), QNetworkProxy.ProxyType.HttpProxy)
        self.assertEqual(youtube.port(), 7897)

    def test_factory_is_installed_once(self):
        """Фабрика на приложение одна и та же: пересоздавать её нельзя.

        Замена установленной фабрики роняла процесс с повреждением кучи
        (0xc0000374), а происходила она при каждом запуске: настройки применяются
        на старте, а потом ещё раз, когда фоновый поиск находит прокси."""
        import gc

        proxy._set(proxy.MODE_MANUAL, 'http://127.0.0.1:7897', False)
        first = proxy._factory
        self.assertIsNotNone(first)
        gc.collect()
        for _ in range(5):
            proxy._set(proxy.MODE_MANUAL, 'http://127.0.0.1:7897', False)
        self.assertIs(proxy._factory, first)

    def test_proxy_returns_after_direct_mode(self):
        """Из режима «без прокси» и обратно — прокси снова работает.

        `setApplicationProxy` рядом с фабрикой её отключает, и YouTube молча
        уходил бы напрямую, хотя адрес прокси задан."""
        from PySide6.QtNetwork import QNetworkProxy

        proxy._set(proxy.MODE_MANUAL, 'http://127.0.0.1:7897', False)
        proxy._set(proxy.MODE_OFF, None, False)
        proxy._set(proxy.MODE_MANUAL, 'http://127.0.0.1:7897', False)
        youtube = self._route('www.youtube.com')
        self.assertEqual(youtube.type(), QNetworkProxy.ProxyType.HttpProxy)
        self.assertEqual(youtube.port(), 7897)
        self.assertEqual(self._route('vk.com').type(), QNetworkProxy.ProxyType.NoProxy)


if __name__ == '__main__':
    unittest.main()
