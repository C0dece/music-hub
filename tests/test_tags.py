"""Теги музыкальных файлов и превращение файла в трек."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import config
from app.core import library, tags

try:
    import mutagen
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
except ImportError:  # без mutagen проверять нечего - модуль сам это переживает
    mutagen = None

# Один кадр MPEG1 Layer III 128 кбит/с, 44,1 кГц: заголовок и тишина. Настоящий
# файл нужен, потому что mutagen отказывается разбирать то, в чём нет потока.
_FRAME = b'\xff\xfb\x90\x64' + b'\x00' * 413
_PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 32


@unittest.skipIf(mutagen is None, 'mutagen не установлен')
class TagsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='tags-test-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        # Обложки складываются в папку конфигурации - в тестах она своя
        patch = mock.patch.object(config, 'CONFIG_DIR', self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def _mp3(self, name='song.mp3', **fields) -> str:
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as fh:
            fh.write(_FRAME * 40)
        if fields:
            frame = ID3()
            if fields.get('title'):
                frame.add(TIT2(encoding=3, text=fields['title']))
            if fields.get('artist'):
                frame.add(TPE1(encoding=3, text=fields['artist']))
            if fields.get('album'):
                frame.add(TALB(encoding=3, text=fields['album']))
            if fields.get('cover'):
                frame.add(APIC(encoding=3, mime='image/png', type=3, desc='',
                               data=fields['cover']))
            frame.save(path)
        return path

    def test_read_returns_tags_and_duration(self):
        path = self._mp3(title='Numb', artist='Linkin Park', album='Meteora')
        data = tags.read(path, with_cover=False)
        self.assertEqual(data['title'], 'Numb')
        self.assertEqual(data['artist'], 'Linkin Park')
        self.assertEqual(data['album'], 'Meteora')
        self.assertGreaterEqual(data['duration'], 1)

    def test_cover_is_extracted_once(self):
        path = self._mp3(title='Numb', cover=_PNG)
        cover = tags.read(path)['cover']
        self.assertTrue(os.path.exists(cover))
        with open(cover, 'rb') as fh:
            self.assertEqual(fh.read(), _PNG)
        # Второе чтение берёт уже вынутый файл, а не пишет новый
        self.assertEqual(tags.read(path)['cover'], cover)

    def test_broken_file_does_not_raise(self):
        path = os.path.join(self.dir, 'битый.mp3')
        with open(path, 'wb') as fh:
            fh.write('не музыка'.encode('utf-8'))
        self.assertEqual(tags.read(path), {})
        self.assertEqual(tags.read(os.path.join(self.dir, 'нет такого.mp3')), {})

    def test_track_prefers_tags_over_file_name(self):
        path = self._mp3('Кто-то - Что-то.mp3', title='Numb', artist='Linkin Park',
                         album='Meteora')
        track = library.audio_track(path)
        self.assertEqual(track.source, 'local')
        self.assertEqual((track.artist, track.title), ('Linkin Park', 'Numb'))
        self.assertEqual(track.meta['album'], 'Meteora')
        self.assertEqual(track.local_path, path)
        self.assertTrue(track.cached)      # свой файл всегда «офлайн»
        self.assertTrue(track.playable)

    def test_track_falls_back_to_file_name(self):
        """Без тегов исполнитель и название берутся из имени файла - иначе свои
        файлы выглядели бы в списке безымянными."""
        path = self._mp3('Linkin Park - Faint.mp3')
        track = library.audio_track(path)
        self.assertEqual((track.artist, track.title), ('Linkin Park', 'Faint'))

    def test_scan_finds_only_audio(self):
        self._mp3('Linkin Park - Numb.mp3', title='Numb')
        with open(os.path.join(self.dir, 'clip.mp4'), 'wb') as fh:
            fh.write(b'0')
        with open(os.path.join(self.dir, 'notes.txt'), 'wb') as fh:
            fh.write(b'0')
        found = library.scan_audio_tracks([self.dir])
        self.assertEqual([t.title for t in found], ['Numb'])
        self.assertEqual(found[0].uid,
                         'local:' + os.path.normcase(os.path.abspath(found[0].local_path)))


class WithoutMutagenTests(unittest.TestCase):
    """Пакета может не быть: тогда теги просто не читаются, но всё работает."""

    def test_read_without_mutagen(self):
        with mock.patch.object(tags, 'mutagen', None):
            self.assertFalse(tags.available())
            self.assertEqual(tags.read(__file__), {})


if __name__ == '__main__':
    unittest.main()
