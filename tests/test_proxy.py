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


if __name__ == '__main__':
    unittest.main()
