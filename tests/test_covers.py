"""Загрузка обложек: очередь, повторы и общая сессия.

Списки просят до трёх десятков картинок за секунду, а пул потоков на всё
приложение - восемь мест. Раньше обложки занимали их все, каждая открывала своё
соединение, и треть загрузок отваливалась по таймауту (598 таких в журнале за
неделю). Отвалившаяся ссылка помечалась мёртвой до перезапуска - обложка
пропадала насовсем."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.ui import covers


class FakePool:
    """Замена `run_async`: задачи копятся, а выполняются когда скажем.

    Настоящий пул потоков сюда не годится - очередь проверяется по числу
    одновременно запущенных задач, а с живыми потоками это гонка."""

    def __init__(self):
        self.jobs = []
        self.started = 0

    def run_async(self, work, on_done):
        self.started += 1
        self.jobs.append((work, on_done))

    def finish_one(self, result=b'data', error=None):
        work, on_done = self.jobs.pop(0)
        on_done(result, error)

    def finish_all(self, result=b'data', error=None):
        while self.jobs:
            self.finish_one(result, error)


class CoverTestCase(unittest.TestCase):
    def setUp(self):
        covers._memory.clear()
        covers._pending.clear()
        covers._failed.clear()
        covers._queue.clear()
        covers._running = 0
        self.pool = FakePool()
        patcher = mock.patch.object(covers, 'run_async', self.pool.run_async)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Картинку из байтов не собрать, а нам важна только механика очереди
        pixmap = mock.Mock()
        pixmap.loadFromData.return_value = True
        patcher = mock.patch.object(covers, 'QPixmap', return_value=pixmap)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        covers._memory.clear()
        covers._pending.clear()
        covers._failed.clear()
        covers._queue.clear()
        covers._running = 0


class QueueTests(CoverTestCase):
    """Одновременных загрузок не больше предела."""

    def test_burst_does_not_flood_the_pool(self):
        for i in range(30):
            covers.load(f'https://host/{i}.jpg', lambda *_: None)
        self.assertEqual(self.pool.started, covers._PARALLEL)
        self.assertEqual(len(covers._queue), 30 - covers._PARALLEL)

    def test_queue_moves_on_after_success(self):
        for i in range(10):
            covers.load(f'https://host/{i}.jpg', lambda *_: None)
        self.pool.finish_one()
        self.assertEqual(self.pool.started, covers._PARALLEL + 1)

    def test_queue_moves_on_after_failure(self):
        """Неудача не должна затыкать очередь: иначе места кончатся навсегда."""
        for i in range(10):
            covers.load(f'https://host/{i}.jpg', lambda *_: None)
        self.pool.finish_one(result=None, error=RuntimeError('таймаут'))
        self.assertEqual(self.pool.started, covers._PARALLEL + 1)

    def test_whole_queue_drains(self):
        for i in range(20):
            covers.load(f'https://host/{i}.jpg', lambda *_: None)
        self.pool.finish_all()
        self.assertEqual(self.pool.started, 20)
        self.assertEqual(covers._queue, [])
        self.assertEqual(covers._running, 0)

    def test_visible_cover_goes_first(self):
        """Прокрутка просит ту же ссылку снова - значит она сейчас на экране."""
        for i in range(10):
            covers.load(f'https://host/{i}.jpg', lambda *_: None)
        waiting = covers._queue[0]                  # разбирался бы последним
        covers.load(waiting, lambda *_: None)
        self.assertEqual(covers._queue[-1], waiting)

    def test_local_file_skips_the_queue(self):
        """Обложка из тегов лежит на диске - ждать сетевых таймаутов ей незачем."""
        for i in range(10):
            covers.load(f'https://host/{i}.jpg', lambda *_: None)
        started = self.pool.started
        covers.load(r'D:\music\track.jpg', lambda *_: None)
        self.assertEqual(self.pool.started, started + 1)

    def test_local_file_does_not_take_a_queue_slot(self):
        covers.load(r'D:\music\track.jpg', lambda *_: None)
        running = covers._running
        self.pool.finish_all()
        self.assertEqual(covers._running, running)   # счётчик не ушёл в минус


class RetryTests(CoverTestCase):
    """Отвалившаяся обложка возвращается, а не пропадает до перезапуска."""

    def test_failed_url_is_not_retried_at_once(self):
        covers.load('https://host/a.jpg', lambda *_: None)
        self.pool.finish_one(result=None, error=RuntimeError('таймаут'))
        covers.load('https://host/a.jpg', lambda *_: None)
        self.assertEqual(self.pool.started, 1)

    def test_failed_url_is_retried_later(self):
        covers.load('https://host/a.jpg', lambda *_: None)
        self.pool.finish_one(result=None, error=RuntimeError('таймаут'))
        covers._failed['https://host/a.jpg'] -= covers._RETRY_AFTER + 1
        covers.load('https://host/a.jpg', lambda *_: None)
        self.assertEqual(self.pool.started, 2)

    def test_success_after_retry_is_delivered(self):
        got = []
        covers.load('https://host/a.jpg', lambda *_: None)
        self.pool.finish_one(result=None, error=RuntimeError('таймаут'))
        covers._failed['https://host/a.jpg'] -= covers._RETRY_AFTER + 1
        covers.load('https://host/a.jpg', lambda url, pix: got.append(url))
        self.pool.finish_one()
        self.assertEqual(got, ['https://host/a.jpg'])


class FallbackTests(unittest.TestCase):
    """Запасной хост для обложек каналов.

    `yt3.googleusercontent.com` у части провайдеров не отвечает: рукопожатие TLS
    висит до конца срока. Те же файлы лежат на `lh3` и оттуда отдаются за доли
    секунды - замер на живой сети: yt3 ноль из двенадцати за 122 с, lh3 десять
    из десяти за 7.7 с."""

    URL = 'https://yt3.googleusercontent.com/ytc/abc=s88'
    SPARE = 'https://lh3.googleusercontent.com/ytc/abc=s88'

    def setUp(self):
        covers._bad_hosts.clear()
        self.addCleanup(covers._bad_hosts.clear)

    def _session(self, *answers):
        """Сессия, отвечающая по списку: исключение бросается, объект отдаётся."""
        session = mock.Mock()
        self.asked = []

        def get(url, **_kwargs):
            self.asked.append(url)
            answer = answers[len(self.asked) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer
        session.get.side_effect = get
        return mock.patch.object(covers, '_http', return_value=session)

    @staticmethod
    def _ok(body=b'jpeg'):
        response = mock.Mock()
        response.content = body
        return response

    def test_alternate_swaps_only_the_host(self):
        self.assertEqual(covers._alternate(self.URL), self.SPARE)
        self.assertEqual(covers._alternate('https://yt3.ggpht.com/ytc/abc=s88'),
                         self.SPARE)

    def test_unknown_host_has_no_alternate(self):
        self.assertIsNone(covers._alternate('https://i.ytimg.com/vi/x/hq.jpg'))

    def test_timeout_falls_back_to_the_spare_host(self):
        import requests

        with self._session(requests.ConnectTimeout(), self._ok()):
            data = covers._fetch(self.URL, Path(tempfile.mkdtemp()) / 'x.img')
        self.assertEqual(data, b'jpeg')
        self.assertEqual(self.asked, [self.URL, self.SPARE])

    def test_working_host_is_left_alone(self):
        """Основной адрес рабочий - просто не у всех. Первым идём к нему."""
        with self._session(self._ok()):
            covers._fetch(self.URL, Path(tempfile.mkdtemp()) / 'x.img')
        self.assertEqual(self.asked, [self.URL])
        self.assertEqual(covers._bad_hosts, {})

    def test_next_cover_skips_the_dead_host(self):
        """Главное ради скорости: ждать сорванный хост на каждой картинке в
        списке - это десять секунд на обложку (12 штук ехали 127 с вместо 8)."""
        import requests

        with self._session(requests.ConnectTimeout(), self._ok()):
            covers._fetch(self.URL, Path(tempfile.mkdtemp()) / 'x.img')
        with self._session(self._ok()):
            covers._fetch(self.URL, Path(tempfile.mkdtemp()) / 'x.img')
        self.assertEqual(self.asked, [self.SPARE])

    def test_dead_host_is_probed_again_later(self):
        """Сеть меняется: через минуту основной адрес пробуем заново."""
        import requests

        with self._session(requests.ConnectTimeout(), self._ok()):
            covers._fetch(self.URL, Path(tempfile.mkdtemp()) / 'x.img')
        covers._bad_hosts['yt3.googleusercontent.com'] -= covers._RETRY_AFTER + 1
        with self._session(self._ok()):
            covers._fetch(self.URL, Path(tempfile.mkdtemp()) / 'x.img')
        self.assertEqual(self.asked, [self.URL])

    def test_host_without_spare_raises(self):
        import requests

        with self._session(requests.ConnectTimeout()):
            with self.assertRaises(requests.ConnectTimeout):
                covers._fetch('https://i.ytimg.com/vi/x/hq.jpg',
                              Path(tempfile.mkdtemp()) / 'x.img')


class SessionTests(unittest.TestCase):
    """Соединения переиспользуются: своё рукопожатие TLS на каждую обложку -
    это и был источник таймаутов."""

    def tearDown(self):
        covers.reset_session()

    def test_same_session_is_reused(self):
        with mock.patch.object(covers.proxy, 'effective', return_value=None):
            self.assertIs(covers._http(), covers._http())

    def test_session_rebuilt_when_proxy_changes(self):
        with mock.patch.object(covers.proxy, 'effective', return_value=None):
            first = covers._http()
        with mock.patch.object(covers.proxy, 'effective',
                               return_value='http://127.0.0.1:7897'):
            self.assertIsNot(covers._http(), first)

    def test_reset_forces_a_new_session(self):
        """Обход блокировки держит один порт при смене прокси за ним, поэтому
        сессия расхождения не заметит - её рвут явно."""
        with mock.patch.object(covers.proxy, 'effective',
                               return_value='http://127.0.0.1:58695'):
            first = covers._http()
            covers.reset_session()
            self.assertIsNot(covers._http(), first)

    def test_pool_holds_open_connections(self):
        with mock.patch.object(covers.proxy, 'effective', return_value=None):
            adapter = covers._http().get_adapter('https://host/a.jpg')
        self.assertEqual(adapter._pool_maxsize, covers._PARALLEL)


if __name__ == '__main__':
    unittest.main()
