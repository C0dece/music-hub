"""Готовые настроения: каждое обязано разворачиваться в рабочий микс.

Пресет не исполняется отдельной веткой — он только предзаполняет `MixConfig`,
поэтому проверять здесь надо не музыку, а пригодность настроек: тот ли режим,
не пустые ли доли, переживает ли конфигурация запись в settings.json и, главное,
нет ли кириллицы в запросе — её VK в поиске портит (AGENTS.md).
"""
import unittest

from app.core import moods
from app.core.mixer import MIN_LIMIT, MODE_LABELS, MixConfig
from app.core.track import SOURCE_LOCAL
from app.ui import player_icons

from .qt_app import qt_app


class MoodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()      # значки рисуются Qt, без приложения их не собрать

    def test_every_mood_builds_a_valid_config(self):
        """Пресет разворачивается в настройки, которые миксер примет как свои."""
        for mood in moods.MOODS:
            with self.subTest(mood=mood.key):
                config = mood.config()
                self.assertIn(config.mode, MODE_LABELS)
                self.assertGreaterEqual(config.limit, MIN_LIMIT)
                # Пресет без включённых источников не собрал бы ни одного трека
                self.assertTrue(config.sources)
                for source in config.weights:
                    self.assertGreaterEqual(config.weights[source], 0)
                    self.assertLessEqual(config.weights[source], 100)
                self.assertEqual(sum(config.share(s) for s in config.sources), 100)

    def test_queries_are_latin(self):
        """Кириллицу в поиск VK не шлём: она возвращается искажённой."""
        for mood in moods.MOODS:
            with self.subTest(mood=mood.key):
                self.assertTrue(mood.config().query.isascii())

    def test_config_survives_settings_round_trip(self):
        """Настроение можно сохранить в settings.json и прочитать обратно."""
        for mood in moods.MOODS:
            with self.subTest(mood=mood.key):
                data = mood.config().to_dict()
                self.assertEqual(MixConfig.from_dict(data).to_dict(), data)

    def test_config_is_fresh_every_time(self):
        """Правка настроек одного пресета не должна менять его же в другом месте."""
        mood = moods.MOODS[0]
        first, second = mood.config(), mood.config()
        self.assertIsNot(first, second)
        first.weights[SOURCE_LOCAL] = 99
        self.assertNotEqual(first.weights, second.weights)

    def test_keys_and_titles_are_unique(self):
        """Ключ — то, чем настроение опознают в настройках, он обязан быть один."""
        keys = [mood.key for mood in moods.MOODS]
        self.assertEqual(len(set(keys)), len(keys))
        titles = [mood.title for mood in moods.MOODS]
        self.assertEqual(len(set(titles)), len(titles))

    def test_icons_exist(self):
        """Несуществующее имя значка рисуется пустотой, а не падением."""
        for mood in moods.MOODS:
            with self.subTest(mood=mood.key):
                self.assertFalse(player_icons.draw(mood.icon).isNull())

    def test_find_forgives_unknown_key(self):
        """Пресеты между версиями меняются, а сохранённый ключ остаётся."""
        self.assertIs(moods.find('energy'), moods.MOODS_BY_KEY['energy'])
        self.assertIsNone(moods.find('такого нет'))
        self.assertIsNone(moods.find(''))


if __name__ == '__main__':
    unittest.main()
