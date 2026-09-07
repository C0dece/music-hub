"""Режим выбора в списках треков.

Отмечать несколько треков раньше можно было только с Ctrl и Shift, о чём ничто
не сообщало. Тест закрепляет второй путь: долгое зажатие и кнопку «Выбрать».
"""
import unittest

from PySide6.QtWidgets import QAbstractItemView

from .qt_app import qt_app
from app.core.track import Track
from app.ui.track_list import SelectionBar, TrackListWidget


def yt(video_id: str) -> Track:
    return Track(source='youtube', source_id=video_id, youtube_id=video_id,
                 title='Numb', artist='Linkin Park', duration=187,
                 url=f'https://www.youtube.com/watch?v={video_id}')


class SelectionModeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        self.list = TrackListWidget()
        self.list.set_tracks([yt('a'), yt('b'), yt('c')])

    def test_hold_turns_on_selection_and_marks_the_row(self):
        """Долгое зажатие включает режим и отмечает строку, на которой держали."""
        self.list._hold_row = 1
        self.list._on_hold()
        self.assertTrue(self.list.selection_mode)
        self.assertEqual([t.source_id for t in self.list.selected_tracks()], ['b'])

    def test_selection_mode_keeps_marks_without_ctrl(self):
        """В режиме выбора щелчок добавляет строку, а не заменяет предыдущую."""
        self.list.set_selection_mode(True)
        self.assertEqual(self.list.selectionMode(),
                         QAbstractItemView.MultiSelection)
        self.list.item(0).setSelected(True)
        self.list.item(2).setSelected(True)
        self.assertEqual({t.source_id for t in self.list.selected_tracks()},
                         {'a', 'c'})

    def test_leaving_selection_mode_clears_marks(self):
        """Вышли из режима — отметки сняты и выделение снова обычное."""
        self.list.set_selection_mode(True)
        self.list.item(0).setSelected(True)
        self.list.set_selection_mode(False)
        self.assertEqual(self.list.selected_tracks(), [])
        self.assertEqual(self.list.selectionMode(),
                         QAbstractItemView.ExtendedSelection)

    def test_double_click_does_not_play_while_choosing(self):
        """Два щелчка по отмеченной строке не должны запускать трек."""
        played = []
        self.list.play_requested.connect(lambda tracks, row: played.append(row))
        self.list.set_selection_mode(True)
        self.list._on_double_click(self.list.item(0))
        self.assertEqual(played, [])
        self.list.set_selection_mode(False)
        self.list._on_double_click(self.list.item(0))
        self.assertEqual(played, [0])

    def test_bar_shows_actions_only_with_something_marked(self):
        """Полоска включает режим, а «Действия» оживают вместе с отметками."""
        bar = SelectionBar(self.list)
        bar._on_toggle()
        self.assertTrue(self.list.selection_mode)
        self.assertFalse(bar._actions.isEnabled())
        self.list.item(0).setSelected(True)
        self.assertTrue(bar._actions.isEnabled())
        self.assertIn('1', bar._count.text())

    def test_reloading_the_list_resets_the_counter(self):
        """Список перечитали — отметки ушли со строками, и счётчик это показывает."""
        bar = SelectionBar(self.list)
        bar._on_toggle()
        self.list.item(0).setSelected(True)
        self.assertTrue(bar._actions.isEnabled())
        self.list.set_tracks([yt('d')])
        self.assertFalse(bar._actions.isEnabled())


class QueueSelectionTests(unittest.TestCase):
    """В очереди зажатие занято переносом строк, поэтому вход только через кнопку."""

    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def test_queue_list_does_not_react_to_hold_and_stops_dragging(self):
        from app.ui.queue_panel import _QueueList
        widget = _QueueList()
        widget.set_tracks([yt('a'), yt('b')])
        self.assertFalse(widget._hold_enabled)

        widget.set_selection_mode(True)
        # Пока отмечают, тянуть нельзя: одно движение мыши на два действия
        self.assertEqual(widget.dragDropMode(), QAbstractItemView.NoDragDrop)
        widget.set_selection_mode(False)
        self.assertEqual(widget.dragDropMode(), QAbstractItemView.InternalMove)
        self.assertEqual(widget.selectionMode(),
                         QAbstractItemView.SingleSelection)


if __name__ == '__main__':
    unittest.main()
