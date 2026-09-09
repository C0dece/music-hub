"""Старые настройки должны открываться без ошибок и без потери значений.

У пользователя уже лежит settings.json, написанный прежними версиями - там нет
ни громкости, ни значка в трее, ни моста. Файл трогаем только временный.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import config

# Настройки самой первой версии - только загрузчик, ничего про музыку
OLD_SETTINGS = {
    'mode': 'audio',
    'audio_format': 'mp3',
    'audio_quality': '320',
    'video_quality': '1080',
    'music_dir': 'D:/Music',
    'cookies_browser': 'chrome',
    'proxy_enabled': True,
    'proxy_host': '127.0.0.1',
    'proxy_port': 8080,
}


class SettingsMigrationTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = config.SETTINGS_FILE
        config.SETTINGS_FILE = Path(self._dir.name) / 'settings.json'

    def tearDown(self):
        config.SETTINGS_FILE = self._saved
        self._dir.cleanup()

    def write(self, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        config.SETTINGS_FILE.write_text(text, encoding='utf-8')

    def test_old_file_gets_new_keys(self):
        self.write(OLD_SETTINGS)
        settings = config.load_settings()
        for key in ('volume', 'remember_volume', 'autoplay_next', 'tray_enabled',
                    'close_to_tray', 'tray_notifications', 'hotkeys_enabled',
                    'hotkey_play_pause', 'hotkeys_media_keys', 'bridge_enabled',
                    'bridge_port', 'bridge_token', 'vk_match_first',
                    'vk_ask_on_ambiguous', 'vk_upload_fallback'):
            self.assertIn(key, settings, key)

    def test_old_values_survive(self):
        self.write(OLD_SETTINGS)
        settings = config.load_settings()
        self.assertEqual(settings['music_dir'], 'D:/Music')
        self.assertEqual(settings['audio_quality'], '320')
        self.assertTrue(settings['proxy_enabled'])
        self.assertEqual(settings['proxy_port'], 8080)

    def test_unknown_keys_are_kept(self):
        """Чужой ключ - не повод его выбрасывать: вдруг это старая версия."""
        self.write({**OLD_SETTINGS, 'какой_то_старый_ключ': 1})
        self.assertEqual(config.load_settings()['какой_то_старый_ключ'], 1)

    def test_broken_file_falls_back_to_defaults(self):
        self.write('{ это не json')
        settings = config.load_settings()
        self.assertEqual(settings['volume'], config.DEFAULT_SETTINGS['volume'])

    def test_missing_file_is_defaults(self):
        self.assertFalse(config.SETTINGS_FILE.exists())
        self.assertEqual(config.load_settings(), dict(config.DEFAULT_SETTINGS))

    def test_save_and_load_roundtrip(self):
        settings = config.load_settings()
        settings['volume'] = 42
        settings['hotkey_play_pause'] = 'Ctrl+Alt+P'
        config.save_settings(settings)
        again = config.load_settings()
        self.assertEqual(again['volume'], 42)
        self.assertEqual(again['hotkey_play_pause'], 'Ctrl+Alt+P')

    def test_defaults_are_not_shared(self):
        """load_settings не должен отдавать сам словарь умолчаний."""
        first = config.load_settings()
        first['volume'] = 5
        self.assertNotEqual(config.DEFAULT_SETTINGS['volume'], 5)

    def test_bridge_token_is_not_default_secret(self):
        """Общего для всех ключа быть не может - он выдаётся при первом запуске."""
        self.assertEqual(config.DEFAULT_SETTINGS['bridge_token'], '')


if __name__ == '__main__':
    unittest.main()
