"""Область видео: подпись поверх кадра и смена плеера.

Проверяем ровно то, из-за чего кадр чернел. Подпись «Готовим видео…» —
непрозрачная заслонка во весь экран сцены, и если она останется висеть после
того, как видео пошло, снять её будет уже нечем: состояние плеера второй раз не
меняется. Второе — прежний плеер, оставшийся в контейнере: и QWebEngineView, и
QVideoWidget держат нативное окно, а оно рисуется поверх обычных виджетов
независимо от порядка в стопке.

Видимость подписи проверяем через isHidden(): это то, что задаёт setVisible.
isVisible() у ребёнка непоказанного окна всегда False, и такая проверка мерила бы
не подпись, а наличие окна на экране.
"""
import unittest

from PySide6.QtWidgets import QWidget

from app.ui.video_stage import VideoStage
from tests.qt_app import qt_app


class NoteTests(unittest.TestCase):
    """Подпись видна ровно тогда, когда показывать нечего."""

    def setUp(self):
        self.app = qt_app()
        self.stage = VideoStage()
        self.stage.resize(640, 400)
        self.stage.set_widget(QWidget())

    def test_note_hides_when_the_video_is_ready(self):
        self.stage.set_note('Готовим видео…')
        self.assertFalse(self.stage._note.isHidden())
        self.stage.set_note('')
        self.assertTrue(self.stage._note.isHidden())

    def test_note_cleared_during_fullscreen_does_not_come_back(self):
        """Видео догрузилось, пока кадр был во весь экран.

        Раньше такой запрос выбрасывали, и на возврате в окно вставало
        устаревшее «Готовим видео…» — поверх уже работающей картинки."""
        self.stage.set_note('Готовим видео…')
        self.stage.set_fullscreen(True)
        self.stage.set_note('')                      # плеер: картинка пошла
        self.stage.set_fullscreen(False)
        self.assertEqual(self.stage._note.text(), '')
        self.assertTrue(self.stage._note.isHidden())

    def test_note_set_during_fullscreen_shows_on_return(self):
        """Обратный случай: пока смотрели во весь экран, начался новый трек."""
        self.stage.set_fullscreen(True)
        self.stage.set_note('Готовим видео…')
        self.stage.set_fullscreen(False)
        self.assertFalse(self.stage._note.isHidden())
        self.assertEqual(self.stage._note.text(), 'Готовим видео…')


class WidgetSwapTests(unittest.TestCase):
    """Смена источника картинки: страница YouTube против видеовыхода плеера."""

    def setUp(self):
        self.app = qt_app()
        self.stage = VideoStage()
        self.stage.resize(640, 400)

    def test_previous_player_leaves_the_screen(self):
        """Прежний виджет уходит из контейнера, а не остаётся в нём скрытым.

        Скрытым он оставался ребёнком экрана сцены, и его нативное окно
        накрывало нового чёрным прямоугольником."""
        first, second = QWidget(), QWidget()
        self.stage.set_widget(first)
        self.stage.set_widget(second)
        self.assertIsNone(first.parentWidget())
        self.assertIs(second.parentWidget(), self.stage._body)

    def test_previous_player_stays_alive(self):
        """Движок один на приложение: отвязали — но не удалили."""
        first = QWidget()
        self.stage.set_widget(first)
        self.stage.set_widget(QWidget())
        first.setObjectName('жив')                   # упал бы на удалённом объекте
        self.assertEqual(first.objectName(), 'жив')

    def test_note_stays_on_top_after_a_swap(self):
        """Новый виджет добавляют в раскладку последним — подпись поднимаем."""
        self.stage.set_widget(QWidget())
        self.stage.set_note('Готовим видео…')
        self.stage.set_widget(QWidget())
        self.assertFalse(self.stage._note.isHidden())


if __name__ == '__main__':
    unittest.main()
