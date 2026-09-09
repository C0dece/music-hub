"""Библиотека - управление уже скачанными файлами."""
import logging
import os
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QInputDialog,
    QLineEdit, QMenu, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..core import library
from ..core.async_task import run_async
from .flow_layout import FlowRow
from .widgets import CheckableTableView, ElidedLabel, EmptyState, is_checked

logger = logging.getLogger(__name__)

COLUMNS = ['', 'Название', 'Откуда', 'Тип', 'Размер', 'Добавлен', 'Папка']
COL_CHECK, COL_NAME, COL_SOURCE, COL_KIND, COL_SIZE, COL_DATE, COL_FOLDER = range(7)

# Откуда файл - знает только история загрузок; у файлов, положенных в папку мимо
# программы, источника нет, и придумывать его не нужно
SOURCE_LABELS = {'youtube': 'YouTube', 'vk_audio': 'VK Музыка', 'vk_video': 'VK Видео'}

SORT_ROLE = Qt.UserRole + 10
KIND_ROLE = Qt.UserRole + 11
SOURCE_ROLE = Qt.UserRole + 12


class LibraryModel(QAbstractTableModel):
    checked_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files: list[library.MediaFile] = []
        # Отметки храним по путям, а не по номерам строк: сортировка и фильтр
        # переставляют строки, и номера тут же перестают что-либо значить
        self._checked: set[str] = set()

    def set_files(self, files) -> None:
        self.beginResetModel()
        self._files = list(files)
        self._checked.clear()
        self.endResetModel()
        self.checked_changed.emit()

    def rowCount(self, parent=QModelIndex()):
        return len(self._files)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        if role == Qt.ToolTipRole and orientation == Qt.Horizontal and section == COL_CHECK:
            return 'Отметьте файлы галочками, действия внизу применятся ко всем сразу'
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.column() == COL_CHECK:
            flags |= Qt.ItemIsUserCheckable
        return flags

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        media = self._files[index.row()]
        col = index.column()

        if role == KIND_ROLE:
            return media.kind
        if role == SOURCE_ROLE:
            return media.source
        if role == Qt.CheckStateRole and col == COL_CHECK:
            return Qt.Checked if media.path in self._checked else Qt.Unchecked
        if role == SORT_ROLE:
            # Сортируем по «сырым» значениям: иначе «9.9 МБ» окажется больше «10.1 МБ»
            if col == COL_SIZE:
                return media.size
            if col == COL_DATE:
                return media.mtime
            if col == COL_CHECK:
                return int(media.path in self._checked)
            return self.data(index, Qt.DisplayRole)
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return media.name
            if col == COL_SOURCE:
                return SOURCE_LABELS.get(media.source, '·')
            if col == COL_KIND:
                return ('Аудио' if media.kind == 'audio' else 'Видео') + f' · {media.ext[1:]}'
            if col == COL_SIZE:
                return media.size_text
            if col == COL_DATE:
                return datetime.fromtimestamp(media.mtime).strftime('%d.%m.%Y %H:%M')
            if col == COL_FOLDER:
                return media.folder
        if role == Qt.ToolTipRole:
            if col == COL_SOURCE and not media.source:
                return 'Файл не из загрузок этой программы, откуда он, неизвестно'
            return media.path
        if role == Qt.TextAlignmentRole and col in (COL_SOURCE, COL_KIND, COL_SIZE, COL_DATE):
            return int(Qt.AlignCenter)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or index.column() != COL_CHECK:
            return False
        path = self._files[index.row()].path
        if is_checked(value):
            self._checked.add(path)
        else:
            self._checked.discard(path)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.checked_changed.emit()
        return True

    def file_at(self, row: int) -> library.MediaFile:
        return self._files[row]

    def is_checked_row(self, row: int) -> bool:
        return self._files[row].path in self._checked

    def set_checked_rows(self, rows, checked: bool) -> None:
        for row in rows:
            path = self._files[row].path
            if checked:
                self._checked.add(path)
            else:
                self._checked.discard(path)
        if self._files:
            top = self.index(0, COL_CHECK)
            bottom = self.index(len(self._files) - 1, COL_CHECK)
            self.dataChanged.emit(top, bottom, [Qt.CheckStateRole])
        self.checked_changed.emit()

    def replace_at(self, row: int, media: library.MediaFile) -> None:
        old = self._files[row]
        if old.path in self._checked:
            self._checked.discard(old.path)
            self._checked.add(media.path)
        self._files[row] = media
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))

    def total_size(self) -> int:
        return sum(f.size for f in self._files)


class _LibraryFilter(QSortFilterProxyModel):
    """Поиск по названию плюс фильтры «аудио/видео» и «откуда скачано»."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(COL_NAME)
        self._kind = ''
        self._source = ''

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.invalidateFilter()

    def set_source(self, source: str) -> None:
        # 'vk' ловит и музыку, и видео VK - одним пунктом списка
        self._source = source
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        index = self.sourceModel().index(row, 0, parent)
        if self._kind and self.sourceModel().data(index, KIND_ROLE) != self._kind:
            return False
        if self._source:
            source = self.sourceModel().data(index, SOURCE_ROLE) or ''
            if self._source == 'unknown':
                if source:
                    return False
            elif not source.startswith(self._source):
                return False
        return super().filterAcceptsRow(row, parent)


class LibraryPage(QWidget):
    """Список скачанного: послушать, открыть, показать в папке, переименовать, удалить."""

    count_changed = Signal(int)
    play_files_requested = Signal(list)     # пути к файлам - для общего плеера
    enqueue_files_requested = Signal(list)

    def __init__(self, settings_provider, parent=None):
        super().__init__(parent)
        self._settings_provider = settings_provider
        self._loading = False
        # Очередь заливки в музыку VK - общая с главным окном, ставится снаружи
        self._uploader = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = FlowRow(spacing=8)
        self._search = QLineEdit()
        self._search.setPlaceholderText('Поиск по названию…')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        top.add(self._search)

        self._kind_combo = QComboBox()
        self._kind_combo.addItem('Все файлы', '')
        self._kind_combo.addItem('Только аудио', 'audio')
        self._kind_combo.addItem('Только видео', 'video')
        self._kind_combo.currentIndexChanged.connect(self._apply_filter)
        top.add(self._kind_combo)

        self._source_combo = QComboBox()
        self._source_combo.addItem('Откуда угодно', '')
        self._source_combo.addItem('С YouTube', 'youtube')
        self._source_combo.addItem('Из VK', 'vk')
        self._source_combo.addItem('Не из загрузок', 'unknown')
        self._source_combo.currentIndexChanged.connect(self._apply_filter)
        top.add(self._source_combo)

        self._refresh_btn = QPushButton('Обновить')
        self._refresh_btn.setObjectName('secondary')
        self._refresh_btn.clicked.connect(self.reload)
        top.add(self._refresh_btn)
        layout.addWidget(top)

        self._model = LibraryModel(self)
        self._model.checked_changed.connect(self._on_checked_changed)
        self._proxy = _LibraryFilter(self)
        self._proxy.setSourceModel(self._model)

        self._table = CheckableTableView()
        self._table.CHECK_COLUMN = COL_CHECK
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(COL_DATE, Qt.DescendingOrder)
        self._table.setAlternatingRowColors(True)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_menu)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(30)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        # Растягиваемой колонке Qt отдаёт то, что осталось от остальных, и в узком
        # окне не осталось ничего: пять колонок «по содержимому» забирали всю
        # ширину, название сжималось до 40 px, а его заголовок обрезался слева
        # до «Іазвани». Минимум по названию держит колонку читаемой, а лишнее
        # Qt тогда убирает в горизонтальную прокрутку - это честнее, чем колонка,
        # в которой не видно ни одного файла. Минимум секции здесь общий на весь
        # заголовок, поэтому им не обойтись: он поднял бы и колонку галочек с её
        # 36 px. Вместо этого убираем из борьбы за ширину самих соперников -
        # колонки ниже получают фиксированную ширину по своему содержимому
        # Папка делила ширину с названием поровну: у всех файлов путь один и тот
        # же, а названия резались многоточием на середине. По содержимому её
        # тоже нельзя: длинный путь снова съедал бы название целиком, поэтому
        # фиксированная ширина - вручную её всё равно можно растянуть
        header.setSectionResizeMode(COL_FOLDER, QHeaderView.Interactive)
        self._table.setColumnWidth(COL_FOLDER, 140)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        self._table.setColumnWidth(COL_CHECK, 36)
        # «По содержимому» эти четыре растут от самой длинной строки и вместе
        # съедали всё место у названия. Ширины ниже подобраны под их формат:
        # источник, «Аудио · mp3», «12.3 МБ» и дата со временем - длиннее не
        # бывает, а лишнего они больше не занимают
        for col, width in ((COL_SOURCE, 92), (COL_KIND, 96),
                           (COL_SIZE, 84), (COL_DATE, 124)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self._table.setColumnWidth(col, width)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.selectionModel().selectionChanged.connect(self._update_buttons)
        layout.addWidget(self._table, 1)

        # Пустую таблицу с шапкой читать не в чем: вместо неё показываем, что
        # делать дальше
        self._empty = EmptyState(
            'download', 'Загрузок пока нет',
            'Скачанные треки и видео появятся здесь. Папку можно поменять '
            'в настройках, а список обновить.')
        self._empty.add_action('Проверить папку', self.reload)
        self._empty.hide()
        layout.addWidget(self._empty, 1)

        bottom = FlowRow(spacing=8, align_right=True, overflow=True)
        self._summary = ElidedLabel()
        self._summary.setObjectName('hint')
        bottom.add(self._summary)

        self._check_all_btn = QPushButton('Отметить все')
        self._check_all_btn.setObjectName('link')
        self._check_all_btn.clicked.connect(self._toggle_all)
        bottom.add(self._check_all_btn)
        bottom.add_stretch()

        self._play_btn = self._action_button(bottom, 'Слушать', self._play_selected)
        self._play_btn.setObjectName('')  # единственная основная кнопка - она заметнее прочих
        self._open_btn = self._action_button(bottom, 'Открыть', self._open_selected)
        self._reveal_btn = self._action_button(bottom, 'Показать в папке', self._reveal_selected)
        self._rename_btn = self._action_button(bottom, 'Переименовать', self._rename_selected)
        self._vk_btn = self._action_button(bottom, 'В музыку VK', self._upload_to_vk)
        self._delete_btn = self._action_button(bottom, 'Удалить', self._delete_selected, danger=True)
        layout.addWidget(bottom)

        self._update_summary()
        self._update_buttons()

    def _action_button(self, row, text, slot, danger=False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName('danger' if danger else 'secondary')
        button.clicked.connect(slot)
        row.add(button)
        return button

    def set_uploader(self, uploader) -> None:
        """Очередь заливки в музыку VK. Она общая с загрузками, поэтому приходит снаружи."""
        self._uploader = uploader
        uploader.changed.connect(self._update_buttons)
        self._update_buttons()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_columns()

    def _sync_columns(self) -> None:
        """В узком окне прятать то, без чего список читается.

        Ширины колонок больше не растут от содержимого, но в 664 px их всё равно
        семь, и названию - единственному, ради чего в этот список смотрят -
        остаётся меньше места, чем колонке с датой. Папка у всех файлов одна и
        та же, размер интересен редко: без них название дышит, а вернутся они
        сами, как только окно снова станет широким.

        Порог папки высокий намеренно: при 1280 с открытой очередью таблице
        достаётся 726 px, и 140 из них под одинаковый у всех путь - размен не в
        пользу названия. Сам путь никуда не делся: он в подсказке строки и в
        кнопке «Показать в папке».

        Порог размера считается по той же ширине, что и порог папки, а папка к
        этому моменту уже скрыта - поэтому 520 не срабатывали никогда: в окне
        664 таблице остаётся 556. При них названию доставалось 124 px, ровно
        столько же, сколько колонке с датой, и имя файла обрывалось на третьем
        слове. 600 - это «папки нет и место всё равно кончилось»: размер уходит,
        и его 84 px достаются названию."""
        width = self._table.viewport().width()
        self._table.setColumnHidden(COL_FOLDER, width < 700)
        self._table.setColumnHidden(COL_SIZE, width < 600)

    # ---------- данные ----------
    def reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText('Читаю…')
        settings = self._settings_provider()
        dirs = [settings.get('music_dir'), settings.get('video_dir')]

        def on_done(files, error):
            self._loading = False
            self._refresh_btn.setEnabled(True)
            self._refresh_btn.setText('Обновить')
            if error:
                QMessageBox.warning(self, 'Библиотека', f'Не удалось прочитать папки:\n{error}')
                return
            self._model.set_files(files)
            self._apply_filter()
            self._update_buttons()
            self.count_changed.emit(len(files))

        run_async(library.scan, on_done, dirs)

    def _apply_filter(self) -> None:
        self._proxy.set_kind(self._kind_combo.currentData())
        self._proxy.set_source(self._source_combo.currentData())
        self._update_summary()
        self._update_buttons()

    def _update_summary(self) -> None:
        shown = self._proxy.rowCount()
        total = self._model.rowCount()
        self._empty.setVisible(not total)
        self._table.setVisible(bool(total))
        if not total:
            self._summary.setText('Пока ничего не скачано')
            return
        text = f'Файлов: {total}' if shown == total else f'Показано: {shown} из {total}'
        parts = [text, library.format_size(self._model.total_size())]
        checked = self._checked_files()
        if checked:
            size = library.format_size(sum(f.size for f in checked))
            parts.append(f'отмечено: {len(checked)} · {size}')
        self._summary.setText(' · '.join(parts))

    def _on_checked_changed(self) -> None:
        self._update_summary()
        self._update_buttons()

    # ---------- выбор ----------
    def _visible_rows(self) -> list[int]:
        """Строки исходной модели, которые сейчас видно сквозь фильтр."""
        return [self._proxy.mapToSource(self._proxy.index(row, 0)).row()
                for row in range(self._proxy.rowCount())]

    def _selected_rows(self) -> list[int]:
        return [
            self._proxy.mapToSource(index).row()
            for index in self._table.selectionModel().selectedRows()
        ]

    def _checked_files(self) -> list[library.MediaFile]:
        """Отмеченные - но только среди видимых.

        Иначе отметка, спрятанная поиском, попала бы под «Удалить» вместе с тем,
        что человек видит на экране."""
        return [self._model.file_at(row) for row in self._visible_rows()
                if self._model.is_checked_row(row)]

    def _target_files(self) -> list[library.MediaFile]:
        """К чему применяются действия: к отмеченному, а если галочек нет - к выделенному.

        Ставить галочку ради одного файла незачем, а при разборе десятков - наоборот,
        удобно видеть выбор явно. Работают оба способа."""
        checked = self._checked_files()
        if checked:
            return checked
        return [self._model.file_at(row) for row in self._selected_rows()]

    def _toggle_all(self) -> None:
        rows = self._visible_rows()
        # Отмечаем всё видимое, а если оно уже отмечено целиком - снимаем
        checked = len(self._checked_files()) < len(rows)
        self._model.set_checked_rows(rows, checked)

    def _update_buttons(self, *_args) -> None:
        files = self._target_files()
        one = len(files) == 1
        for button in (self._open_btn, self._reveal_btn, self._rename_btn):
            button.setEnabled(one)
        self._play_btn.setEnabled(bool(files))
        self._delete_btn.setEnabled(bool(files))
        self._update_vk_button(files)
        self._play_btn.setText(
            'Смотреть' if one and files[0].kind == 'video' else 'Слушать')
        rows = self._visible_rows()
        self._check_all_btn.setEnabled(bool(rows))
        self._check_all_btn.setText(
            'Снять отметки' if rows and len(self._checked_files()) == len(rows)
            else 'Отметить все')

    def _update_vk_button(self, files) -> None:
        # Видео в музыку не положишь, поэтому кнопка живёт только на аудио
        audio = bool(files) and all(f.kind == 'audio' for f in files)
        ready = self._uploader is not None and self._uploader.ready
        pending = self._uploader.pending if self._uploader else 0
        self._vk_btn.setEnabled(audio and ready)
        self._vk_btn.setText(f'В музыку VK ({pending})' if pending else 'В музыку VK')
        self._vk_btn.setToolTip(
            'Отправить в «Мою музыку» вашего аккаунта VK' if ready
            else 'Нужен вход в VK, вкладка «Музыка VK»')

    def _upload_to_vk(self, *_args) -> None:
        files = [f for f in self._target_files() if f.kind == 'audio']
        if not files or self._uploader is None:
            return
        if not self._uploader.add([f.path for f in files]):
            QMessageBox.information(self, 'Музыка VK',
                                    'Эти файлы уже стоят в очереди на отправку.')

    # ---------- действия ----------
    def _show_menu(self, pos) -> None:
        files = self._target_files()
        menu = QMenu(self)
        play = menu.addAction('Воспроизвести в приложении')
        play.triggered.connect(self._play_selected)
        enqueue = menu.addAction('Добавить в очередь')
        enqueue.triggered.connect(self._enqueue_selected)
        open_action = menu.addAction('Открыть другой программой')
        open_action.triggered.connect(self._open_selected)
        reveal = menu.addAction('Показать в папке')
        reveal.triggered.connect(self._reveal_selected)
        copy = menu.addAction('Скопировать путь')
        copy.triggered.connect(self._copy_paths)
        menu.addSeparator()
        rename = menu.addAction('Переименовать')
        rename.triggered.connect(self._rename_selected)
        to_vk = menu.addAction('Отправить в музыку VK')
        to_vk.triggered.connect(self._upload_to_vk)
        menu.addSeparator()
        delete = menu.addAction('Удалить в корзину')
        delete.triggered.connect(self._delete_selected)

        for action in (play, copy, delete):
            action.setEnabled(bool(files))
        enqueue.setEnabled(bool(files))
        for action in (open_action, reveal, rename):
            action.setEnabled(len(files) == 1)
        to_vk.setEnabled(self._vk_btn.isEnabled())
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_double_click(self, index) -> None:
        # По галочке двойной клик - это две попытки её поставить, а не «открыть файл»
        if index.column() != COL_CHECK:
            self._play_selected()

    def _play_selected(self, *_args) -> None:
        """Слушать и смотреть скачанное - в общем плеере приложения.

        Раньше ролики открывались отдельным окном: два плеера, два звука и своя
        очередь у каждого. Теперь видео показывает та же область над списком."""
        paths = [f.path for f in self._target_files()]
        if paths:
            self.play_files_requested.emit(paths)

    def _enqueue_selected(self, *_args) -> None:
        paths = [f.path for f in self._target_files()]
        if paths:
            self.enqueue_files_requested.emit(paths)

    def _open_selected(self, *_args) -> None:
        files = self._target_files()
        if not files:
            return
        try:
            library.open_file(files[0].path)
        except OSError as exc:
            QMessageBox.warning(self, 'Библиотека', f'Не удалось открыть файл:\n{exc}')

    def _reveal_selected(self) -> None:
        files = self._target_files()
        if files:
            library.reveal(files[0].path)

    def _copy_paths(self) -> None:
        files = self._target_files()
        if files:
            QGuiApplication.clipboard().setText('\n'.join(f.path for f in files))

    def _rename_selected(self) -> None:
        files = self._target_files()
        if len(files) != 1:
            return
        media = files[0]
        row = next((r for r in range(self._model.rowCount())
                    if self._model.file_at(r).path == media.path), None)
        if row is None:
            return
        new_name, ok = QInputDialog.getText(self, 'Переименовать', 'Новое имя:', text=media.name)
        if not ok:
            return
        try:
            new_path = library.rename(media.path, new_name)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Переименование', str(exc))
            return
        self._model.replace_at(row, library.MediaFile(
            path=new_path,
            name=os.path.splitext(os.path.basename(new_path))[0],
            ext=media.ext, kind=media.kind, size=media.size,
            mtime=media.mtime, folder=media.folder, source=media.source,
        ))

    def _delete_selected(self) -> None:
        files = self._target_files()
        if not files:
            return
        if len(files) == 1:
            question = f'Удалить файл «{files[0].name}{files[0].ext}»?'
        else:
            question = f'Удалить выбранные файлы ({len(files)} шт.)?'
        answer = QMessageBox.question(
            self, 'Удаление', f'{question}\n\nФайлы отправятся в корзину.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            library.delete([f.path for f in files])
        except OSError as exc:
            QMessageBox.warning(self, 'Удаление', f'Не удалось удалить:\n{exc}')
        self.reload()
