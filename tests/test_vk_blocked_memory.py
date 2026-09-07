"""Блокировка аккаунта VK запоминается на диске и переживает перезапуск.

Это ответ на повторную блокировку: прежде знание о ней жило только в памяти
процесса, и каждый запуск программы заново прогонял по помеченному аккаунту весь
стартовый залп — вход, список треков, список плейлистов, сторож. Для VK это
выглядело потоком обращений от заблокированного пользователя, то есть ровно тем
поведением, из-за которого блокировку и не снимали.

Сети здесь нет: `probe_blocked` получает подставную сессию, а хранилище отметки
уводится во временную папку.
"""
import tempfile
import unittest
from pathlib import Path

from app import config
from app.core import vk_client


class FakeResponse:
    def __init__(self, url, history=()):
        self.url = url
        self.history = [FakeResponse(u) for u in history]


class FakeSession:
    """Отдаёт заранее заданную цепочку редиректов вместо похода в VK."""

    def __init__(self, url, history=(), error=None):
        self._url = url
        self._history = history
        self._error = error
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self._error:
            raise self._error
        return FakeResponse(self._url, self._history)


class BlockedProbeTest(unittest.TestCase):
    """Один запрос на входе отличает блокировку от рабочего аккаунта."""

    def test_landing_on_blocked_page_is_a_block(self):
        """Цепочка редиректов осела на странице блокировки — этого достаточно.

        Именно так VK и отвечает: код 200, тело есть, исключения нет. Судим по
        адресу, потому что вёрстку VK меняет когда угодно."""
        session = FakeSession('https://m.vk.com/vkui/blocked',
                              history=('https://m.vk.ru/',
                                       'https://m.vk.ru/login?act=blocked'))
        self.assertTrue(vk_client.probe_blocked(session))

    def test_normal_landing_is_not_a_block(self):
        session = FakeSession('https://m.vk.ru/feed')
        self.assertFalse(vk_client.probe_blocked(session))

    def test_network_error_is_not_a_block(self):
        """Нет связи — это не «VK не пускает».

        Считать иначе значило бы пометить аккаунт заблокированным из-за
        отвалившегося вайфая и молча перестать входить."""
        import requests
        session = FakeSession('', error=requests.ConnectionError('нет сети'))
        self.assertFalse(vk_client.probe_blocked(session))
        self.assertEqual(session.calls, 1)


class BlockedMemoryTest(unittest.TestCase):
    """Отметка о блокировке: пишется, читается, снимается."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = config.VK_BLOCKED_FILE
        config.VK_BLOCKED_FILE = Path(self._tmp.name) / 'vk_blocked.json'

    def tearDown(self):
        config.VK_BLOCKED_FILE = self._saved
        self._tmp.cleanup()

    def test_absent_by_default(self):
        self.assertIsNone(config.load_vk_blocked())

    def test_saved_and_read_back(self):
        config.save_vk_blocked(181417216)
        got = config.load_vk_blocked()
        self.assertIsNotNone(got)
        self.assertEqual(got['user_id'], 181417216)
        self.assertIn('since', got)

    def test_repeat_keeps_first_time(self):
        """Важно, когда блокировку заметили впервые, а не когда напомнили о ней."""
        config.save_vk_blocked(1)
        first = config.load_vk_blocked()['since']
        config.save_vk_blocked(1)
        self.assertEqual(config.load_vk_blocked()['since'], first)

    def test_cleared(self):
        config.save_vk_blocked(1)
        config.clear_vk_blocked()
        self.assertIsNone(config.load_vk_blocked())

    def test_broken_file_is_not_a_block(self):
        """Испорченный файл не должен запирать человека без музыки навсегда."""
        config.VK_BLOCKED_FILE.write_text('{не json', encoding='utf-8')
        self.assertIsNone(config.load_vk_blocked())


if __name__ == '__main__':
    unittest.main()


class BlockedExpiryTest(unittest.TestCase):
    """Отметка о блокировке живёт сутки, а не вечно.

    Блокировки VK почти всегда снимаются, и узнать об этом можно только попыткой.
    Вечная отметка означала бы, что программа молчит про «заблокирован» на аккаунте,
    который VK давно пустил обратно, — и лечится это только удалением файла руками."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = config.VK_BLOCKED_FILE
        config.VK_BLOCKED_FILE = Path(self._tmp.name) / 'vk_blocked.json'

    def tearDown(self):
        config.VK_BLOCKED_FILE = self._saved
        self._tmp.cleanup()

    def test_no_mark_is_not_expired(self):
        """Нечему истекать: отметки нет, и обходить нечего."""
        self.assertFalse(config.vk_blocked_expired())

    def test_fresh_mark_holds(self):
        config.save_vk_blocked(1)
        self.assertFalse(config.vk_blocked_expired())

    def test_day_old_mark_expires(self):
        import time
        old = {'user_id': 1, 'since': time.time() - config.VK_BLOCKED_TTL - 1}
        self.assertTrue(config.vk_blocked_expired(old))

    def test_mark_without_time_expires(self):
        """Отметка без даты — из старой версии. Считаем её просроченной.

        Иначе такой файл стал бы вечным запретом: даты нет, сравнивать не с чем,
        и аккаунт остался бы «заблокированным» навсегда."""
        self.assertTrue(config.vk_blocked_expired({'user_id': 1}))


class RefreshMarkTest(unittest.TestCase):
    """Метка профилактического захода: не чаще раза в сутки, и переживает перезапуск."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = config.VK_REFRESH_FILE
        config.VK_REFRESH_FILE = Path(self._tmp.name) / 'vk_refresh.json'

    def tearDown(self):
        config.VK_REFRESH_FILE = self._saved
        self._tmp.cleanup()

    def test_first_run_is_due(self):
        """Метки нет — значит, неизвестно, когда браузер последний раз бывал на VK.

        Это ровно тот случай, ради которого заход и придуман."""
        self.assertEqual(config.load_vk_refresh(), 0.0)
        self.assertTrue(config.vk_refresh_due())

    def test_fresh_visit_is_not_due(self):
        config.save_vk_refresh()
        self.assertFalse(config.vk_refresh_due())

    def test_day_old_visit_is_due_again(self):
        import time
        config.save_vk_refresh(time.time() - config.VK_REFRESH_INTERVAL - 1)
        self.assertTrue(config.vk_refresh_due())

    def test_broken_file_does_not_stop_the_visit(self):
        """Испорченный файл не должен молча отменять профилактику навсегда."""
        config.VK_REFRESH_FILE.write_text('не json', encoding='utf-8')
        self.assertEqual(config.load_vk_refresh(), 0.0)
        self.assertTrue(config.vk_refresh_due())
