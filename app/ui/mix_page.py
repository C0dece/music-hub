"""Раздел «Микс»: слушать всё сразу, не выбирая источник.

Здесь человек один раз говорит, чего и сколько он хочет — половину из своей
музыки VK, половину с YouTube, немного своих файлов; знакомое, новое или и то и
другое, — и получает одну общую волну. Настройки запоминаются в settings.json,
так что в следующий раз достаточно нажать «Слушать».

Вторая кнопка — «На усмотрение программы»: доли расставляет `Mixer.auto_config`
по тому, что сейчас работает (есть вход в VK, отвечает ли YouTube). Это для
случая «просто включи музыку».

Сборка идёт в фоне: и фонотека VK, и лента YouTube — это сеть. Замечания
источников («вход в VK не выполнен», «лента недоступна, взят поиск») показываются
как есть: подменять один источник другим молча нельзя.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from ..core import moods
from ..core.async_task import run_async
from ..core.mixer import (DEFAULT_LIMIT, MODE_DISCOVER, MODE_KNOWN, MODE_LABELS,
                          MODE_MIXED, SOURCE_ORDER, SOURCE_TITLES, MixConfig)
from ..core.track import SOURCE_VK
from . import player_icons
from .flow_layout import FlowRow
from .track_list import ROW_HEIGHT, SelectionBar, TrackListWidget
from .widgets import Card, ElidedLabel, Skeleton

# Список получается длинный, а высота строки известна — показываем часть,
# остальное прокручивается вместе со страницей
PREVIEW_ROWS = 14

MODE_ORDER = (MODE_KNOWN, MODE_MIXED, MODE_DISCOVER)
MODE_HINTS = {
    MODE_KNOWN: 'Своя фонотека VK, свои файлы и то, что уже слушали',
    MODE_MIXED: 'Половина знакомого, половина того, чего ещё не слышали',
    MODE_DISCOVER: 'Лента YouTube Music и поиск по любимым исполнителям',
}


class _SourceRow:
    """Одна строка источника: включён ли он и какая у него доля."""

    def __init__(self, source: str, on_toggle, on_drag) -> None:
        self.source = source
        self.check = QCheckBox(SOURCE_TITLES[source])
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(10)
        self.slider.setMinimumWidth(140)
        self.share = QLabel('0 %')
        self.share.setObjectName('hint')
        self.share.setMinimumWidth(46)
        self.share.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.note = ElidedLabel('')
        self.note.setObjectName('hint')
        self.check.toggled.connect(lambda _v: on_toggle())
        self.slider.valueChanged.connect(lambda value: on_drag(self, value))

    @property
    def weight(self) -> int:
        return self.slider.value() if self.check.isChecked() else 0

    def set_weight(self, weight: int) -> None:
        self.check.setChecked(weight > 0)
        self.set_value(weight if weight > 0 else 50)

    def set_value(self, value: int) -> None:
        """Подвинуть ползунок, не поднимая волну пересчёта."""
        value = max(0, min(int(round(value)), 100))
        if self.slider.value() == value:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)

    def set_available(self, available: bool, note: str = '') -> None:
        self.check.setEnabled(available)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        if not available:
            self.check.setChecked(False)
        self.slider.setEnabled(available and self.check.isChecked())


class MixPage(QWidget):
    """Настройки общей волны и её предварительный список."""

    play_requested = Signal(object, int)      # треки и с какого начинать
    enqueue_requested = Signal(object, bool)  # треки, ставить ли следующими
    config_changed = Signal(dict)             # настройки для settings.json
    status_message = Signal(str)

    def __init__(self, mixer, config: MixConfig | None = None, parent=None):
        super().__init__(parent)
        self._mixer = mixer
        self._busy = False
        # Размер порции больше не спрашиваем у человека, но настройкам он нужен:
        # им меряется и первая выдача, и каждое продолжение бесконечной волны
        self._limit_value = DEFAULT_LIMIT
        # Номер сборки: пришедший позже старый ответ не должен затирать новый список
        self._gen = 0
        self._build_ui()
        self.set_config(config or MixConfig())
        self.refresh_sources()

    # ---------- сборка интерфейса ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)

        box.addWidget(self._build_moods_card())
        box.addWidget(self._build_sources_card())
        box.addWidget(self._build_shape_card())
        box.addWidget(self._build_actions_card())

        self._status = ElidedLabel('Микс ещё не собран')
        self._status_text = self._status.text()
        self._status.setObjectName('hint')
        box.addWidget(self._status)
        self._notes = QLabel('')
        self._notes.setObjectName('hint')
        self._notes.setWordWrap(True)
        self._notes.hide()
        box.addWidget(self._notes)

        self._skeleton = Skeleton(4)
        self._skeleton.hide()
        box.addWidget(self._skeleton)

        self.list = TrackListWidget(self)
        self.list.hide()
        box.addWidget(SelectionBar(self.list))
        box.addWidget(self.list)

        box.addStretch(1)
        area.setWidget(inner)
        outer.addWidget(area, 1)

    def _build_moods_card(self) -> Card:
        """Настроения: включить музыку, не настраивая её.

        Плитка не отдельный режим, а те же настройки ниже: после нажатия все
        ползунки видно, и их можно докрутить, — иначе пресет был бы чёрным ящиком.
        """
        card = Card()
        title = QLabel('Настроение')
        title.setObjectName('h2')
        card.layout().addWidget(title)
        hint = ElidedLabel('Нажмите, и микс соберётся сам. Настройки ниже '
                           'подстроятся, их можно поправить.')
        hint.setObjectName('hint')
        card.layout().addWidget(hint)

        tiles = FlowRow(spacing=8)
        for mood in moods.MOODS:
            button = QPushButton(mood.title)
            button.setObjectName('secondary')
            button.setIcon(player_icons.draw(mood.icon))
            button.setToolTip(mood.hint)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, m=mood: self.start_mood(m))
            tiles.add(button)

        # Та же плитка, только выбор за программой: место ей здесь, рядом с
        # остальными «включить, не настраивая», а не среди кнопок запуска
        self._auto_btn = QPushButton('На усмотрение программы')
        self._auto_btn.setObjectName('secondary')
        self._auto_btn.setIcon(player_icons.draw('autoplay'))
        self._auto_btn.setToolTip('Расставить доли по тому, что сейчас работает, и включить')
        self._auto_btn.setCursor(Qt.PointingHandCursor)
        self._auto_btn.clicked.connect(self.start_auto)
        tiles.add(self._auto_btn)

        tiles.add_stretch()
        card.layout().addWidget(tiles)
        return card

    def _build_sources_card(self) -> Card:
        card = Card()
        title = QLabel('Откуда брать музыку')
        title.setObjectName('h2')
        card.layout().addWidget(title)
        hint = ElidedLabel('Ползунок задаёт долю источника. Остальные подстраиваются, '
                           'в сумме всегда 100 %.')
        hint.setObjectName('hint')
        card.layout().addWidget(hint)

        self._rows: dict[str, _SourceRow] = {}
        for source in SOURCE_ORDER:
            row = _SourceRow(source, self._on_sources_toggled, self._on_slider_moved)
            line = QHBoxLayout()
            line.setSpacing(10)
            row.check.setMinimumWidth(130)
            line.addWidget(row.check)
            line.addWidget(row.slider, 1)
            line.addWidget(row.share)
            holder = QWidget()
            holder.setLayout(line)
            card.layout().addWidget(holder)
            card.layout().addWidget(row.note)
            self._rows[source] = row
            if source == SOURCE_VK:
                card.layout().addWidget(self._build_vk_extras())
        return card

    def _build_vk_extras(self) -> QWidget:
        """Уточнение к доле VK: чем её наполнять.

        Это не источник и не доля, а настройка одного источника, поэтому она
        стоит под его ползунком с отступом, а не отдельным флагом в конце
        карточки, где читалась как четвёртый источник."""
        holder = QWidget()
        line = QHBoxLayout(holder)
        # Отступ ровно под подписью источника: видно, что настройка вложена
        line.setContentsMargins(24, 0, 0, 0)
        line.setSpacing(8)
        self._vk_recoms = QCheckBox('Волны и рекомендации VK')
        self._vk_recoms.setToolTip(
            'Брать подборки самого VK. Если их нет, новое подбирается поиском, '
            'об этом будет сказано под списком')
        self._vk_recoms.toggled.connect(lambda _v: self._save())
        line.addWidget(self._vk_recoms)
        line.addStretch(1)
        self._vk_extras = holder
        return holder

    def _build_shape_card(self) -> Card:
        card = Card()
        title = QLabel('Какой должна быть волна')
        title.setObjectName('h2')
        card.layout().addWidget(title)

        mode_row = FlowRow(spacing=8)
        mode_row.add(QLabel('Настроение'))
        self._mode = QComboBox()
        for mode in MODE_ORDER:
            self._mode.addItem(MODE_LABELS[mode], mode)
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.add(self._mode)
        mode_row.add_stretch()
        card.layout().addWidget(mode_row)

        self._mode_hint = ElidedLabel('')
        self._mode_hint.setObjectName('hint')
        card.layout().addWidget(self._mode_hint)

        query_row = QHBoxLayout()
        query_row.setSpacing(8)
        query_row.addWidget(QLabel('Про что'))
        self._query = QLineEdit()
        self._query.setPlaceholderText('исполнитель, жанр или настроение, необязательно')
        self._query.setClearButtonEnabled(True)
        self._query.editingFinished.connect(self._save)
        self._query.returnPressed.connect(lambda: self.start(play=True))
        query_row.addWidget(self._query, 1)
        holder = QWidget()
        holder.setLayout(query_row)
        card.layout().addWidget(holder)

        flags = FlowRow(spacing=12)
        self._shuffle = self._flag(flags, 'Перемешивать',
                                   'Иначе источники отдают треки в своём порядке')
        self._skip_recent = self._flag(flags, 'Не повторять недавнее',
                                       'Пропускать то, что играло только что')
        self._unique = self._flag(flags, 'Без повторов между источниками',
                                  'Одну песню берём один раз, даже если она есть и в VK, и на YouTube')
        self._autoplay = self._flag(flags, 'Продолжать бесконечно',
                                    'Когда очередь подойдёт к концу, плеер сам добавит похожее')
        flags.add_stretch()
        card.layout().addWidget(flags)

        # Раньше здесь стоял счётчик треков, и микс упирался в его число.
        # Длину волны задаёт не он, а этот флаг — о чём и сказано вслух.
        self._endless_hint = ElidedLabel('')
        self._endless_hint.setObjectName('hint')
        card.layout().addWidget(self._endless_hint)
        self._autoplay.toggled.connect(self._update_endless_hint)
        self._update_endless_hint()
        return card

    def _update_endless_hint(self, *_args) -> None:
        self._endless_hint.setText(
            'Микс не кончается: когда очередь подходит к концу, добавляются новые треки.'
            if self._autoplay.isChecked() else
            'Микс сыграет одну порцию и остановится. Включите «Продолжать бесконечно».')

    def _flag(self, row: FlowRow, text: str, tooltip: str) -> QCheckBox:
        box = QCheckBox(text)
        box.setToolTip(tooltip)
        box.toggled.connect(lambda _v: self._save())
        row.add(box)
        return box

    def _build_actions_card(self) -> Card:
        card = Card()
        actions = FlowRow(spacing=8)
        self._play_btn = QPushButton('Слушать')
        self._play_btn.clicked.connect(lambda: self.start(play=True))
        actions.add(self._play_btn)

        self._build_btn = QPushButton('Собрать, но не включать')
        self._build_btn.setObjectName('secondary')
        self._build_btn.clicked.connect(lambda: self.start(play=False))
        actions.add(self._build_btn)

        self._queue_btn = QPushButton('Добавить в очередь')
        self._queue_btn.setObjectName('secondary')
        self._queue_btn.setEnabled(False)
        self._queue_btn.clicked.connect(self._enqueue_current)
        actions.add(self._queue_btn)
        actions.add_stretch()
        card.layout().addWidget(actions)
        return card

    # ---------- настройки ----------
    @property
    def lists(self) -> list[TrackListWidget]:
        """Списки раздела — для общей проводки действий в главном окне."""
        return [self.list]

    def config(self) -> MixConfig:
        """Настройки как их выставил человек."""
        return MixConfig(weights={source: row.weight for source, row in self._rows.items()},
                         mode=self._mode.currentData(),
                         query=self._query.text(),
                         limit=self._limit_value,
                         shuffle=self._shuffle.isChecked(),
                         skip_recent=self._skip_recent.isChecked(),
                         unique_songs=self._unique.isChecked(),
                         autoplay=self._autoplay.isChecked(),
                         vk_recoms=self._vk_recoms.isChecked())

    def set_config(self, config: MixConfig) -> None:
        """Разложить настройки по элементам, не сохраняя их обратно по кругу."""
        self._loading = True
        try:
            for source, row in self._rows.items():
                row.set_weight(config.weights.get(source, 0))
            index = self._mode.findData(config.mode)
            self._mode.setCurrentIndex(max(0, index))
            self._query.setText(config.query)
            self._limit_value = config.limit
            self._shuffle.setChecked(config.shuffle)
            self._skip_recent.setChecked(config.skip_recent)
            self._unique.setChecked(config.unique_songs)
            self._autoplay.setChecked(config.autoplay)
            self._vk_recoms.setChecked(config.vk_recoms)
        finally:
            self._loading = False
        # Настройки из файла или из пресета могут не давать в сумме сотню —
        # приводим к тому же виду, в каком их правят ползунками
        self._balance()
        self._update_shares()
        self._update_vk_extras()
        self._update_mode_hint()

    def refresh_sources(self) -> None:
        """Показать, какие источники сейчас доступны. Зовётся при показе раздела:
        вход в VK мог появиться уже после запуска."""
        available = self._mixer.available() if self._mixer is not None else {}
        notes = {'vk': 'Вход в VK не выполнен, раздел «Музыка VK»',
                 'youtube': 'Рекомендации YouTube недоступны',
                 'local': 'Папка с музыкой пуста'}
        for source, row in self._rows.items():
            ok = bool(available.get(source))
            row.set_available(ok, '' if ok else notes.get(source, ''))
        self._update_shares()

    def _save(self) -> None:
        if getattr(self, '_loading', False):
            return
        self.config_changed.emit(self.config().to_dict())

    def _on_sources_toggled(self) -> None:
        """Источник включили или выключили: доли делим заново между оставшимися."""
        for row in self._rows.values():
            row.slider.setEnabled(row.check.isEnabled() and row.check.isChecked())
        self._update_vk_extras()
        if not getattr(self, '_loading', False):
            self._balance()
        self._update_shares()
        self._save()

    def _on_slider_moved(self, moved: '_SourceRow', value: int) -> None:
        """Один ползунок ведут рукой — остальные расходятся сами.

        Без этого цифры менялись, а ползунки соседей стояли на месте: доля
        считалась как вес относительно суммы, и та же «50» значила то 50 %,
        то 33 %. Теперь ползунок и есть процент, а сумма держится равной 100."""
        if getattr(self, '_loading', False) or getattr(self, '_balancing', False):
            return
        self._balance(moved, value)
        self._update_shares()
        self._save()

    def _balance(self, moved: '_SourceRow | None' = None,
                 value: int | None = None) -> None:
        """Свести включённые доли к сотне, оставив нетронутой ту, что ведут рукой."""
        active = [row for row in self._rows.values() if row.check.isChecked()]
        if not active:
            return
        self._balancing = True
        try:
            if moved is not None and moved in active:
                fixed = max(0, min(int(value if value is not None else moved.slider.value()), 100))
                others = [row for row in active if row is not moved]
                if not others:
                    moved.set_value(100)
                    return
                moved.set_value(fixed)
                self._spread(others, 100 - fixed)
            else:
                self._spread(active, 100)
        finally:
            self._balancing = False

    @staticmethod
    def _spread(rows: list, total: int) -> None:
        """Разложить `total` по строкам, сохранив их нынешнее соотношение."""
        before = [max(row.slider.value(), 0) for row in rows]
        base = sum(before)
        if base <= 0:                       # все по нулям — делим поровну
            before = [1] * len(rows)
            base = len(rows)
        values, left = [], total
        for index, weight in enumerate(before):
            share = total - sum(values) if index == len(rows) - 1 else round(weight * total / base)
            values.append(max(0, share))
        # После округлений сумма может разойтись на единицу — сносим её на самую крупную долю
        drift = total - sum(values)
        if drift and values:
            top = values.index(max(values))
            values[top] = max(0, values[top] + drift)
        for row, share in zip(rows, values):
            row.set_value(share)

    def _on_mode_changed(self) -> None:
        self._update_mode_hint()
        self._save()

    def _update_vk_extras(self) -> None:
        """Настройка VK живёт, только пока сам VK в волне."""
        row = self._rows.get(SOURCE_VK)
        self._vk_recoms.setEnabled(bool(row is not None and row.check.isChecked()))

    def _update_mode_hint(self) -> None:
        self._mode_hint.setText(MODE_HINTS.get(self._mode.currentData(), ''))

    def _update_shares(self) -> None:
        config = self.config()
        for source, row in self._rows.items():
            share = config.share(source)
            row.share.setText(f'{share} %' if row.check.isChecked() else '·')
        if not config.sources:
            self._play_btn.setEnabled(False)
            self._build_btn.setEnabled(False)
            self._status.setText('Выберите хотя бы один источник')
        elif not self._busy:
            self._play_btn.setEnabled(True)
            self._build_btn.setEnabled(True)
            # Источник выбран — предупреждение отжило своё, и на его месте снова
            # то, что рассказывает про сам микс
            self._status.setText(self._status_text)

    def _set_status(self, text: str) -> None:
        """Показать и запомнить строку о состоянии микса.

        Предупреждение о невыбранных источниках подменяет её на время, поэтому
        последний осмысленный текст надо где-то хранить."""
        self._status_text = text
        self._status.setText(text)

    # ---------- сборка микса ----------
    def start(self, play: bool = True, config: MixConfig | None = None,
              title: str = '') -> None:
        if self._busy or self._mixer is None:
            return
        config = config or self.config()
        if not config.sources:
            self.status_message.emit('Микс: не выбрано ни одного источника')
            return
        self._set_busy(True)
        # Сборка занимает время: если волну заказало настроение, его имя должно
        # быть видно всё ожидание, а не мелькнуть и исчезнуть
        self._set_status(f'Собираю волну «{title}»…' if title else 'Собираю волну…')
        self._notes.hide()
        self._gen += 1
        gen = self._gen

        def on_done(result, error):
            if gen != self._gen:
                return   # пока ждали, человек собрал микс заново
            self._set_busy(False)
            if error:
                self._set_status(f'Не собралось: {error}')
                self.status_message.emit(f'Микс: {error}')
                return
            self._show_result(result, play, config)

        run_async(self._mixer.build, on_done, config)

    def start_mood(self, mood) -> None:
        """Пресет настроения: разложить его по ручкам и включить.

        Порядок тот же, что у «на усмотрение программы»: сперва показать выбор,
        потом играть, — человек должен видеть, что именно включилось.
        """
        if self._mixer is None:
            return
        # Размер порции и автопродолжение — не дело настроения: их человек уже
        # задал, и пресет их не сбрасывает
        config = mood.config(limit=self._limit_value,
                             autoplay=self._autoplay.isChecked())
        self.set_config(config)
        self._save()
        self.start(play=True, config=config, title=mood.title)

    def start_config(self, config: MixConfig, title: str = '') -> None:
        """Готовая конфигурация со стороны — кнопка запуска с главной.

        Третий вход в ту же дверь, что `start_mood` и `start_auto`: показать
        выбор, запомнить его и играть. Порядок именно такой — иначе музыка
        зазвучит раньше, чем человек увидит, из чего она собралась.
        """
        if self._mixer is None:
            return
        # Размер порции и автопродолжение — прежние: кнопка с главной про то,
        # откуда брать музыку, а не сколько её отмерить
        config.limit = self._limit_value
        config.autoplay = self._autoplay.isChecked()
        self.set_config(config)
        self._save()
        self.start(play=True, config=config, title=title)

    def start_auto(self) -> None:
        """«На усмотрение программы»: доли расставляет сам микшер."""
        if self._mixer is None:
            return
        config = self._mixer.auto_config()
        # Показываем, что именно программа выбрала, — иначе кнопка выглядит
        # непредсказуемой, а в следующий раз человек хочет это подправить
        self.set_config(config)
        self._save()
        self.start(play=True, config=config)

    def _show_result(self, result, play: bool, config: MixConfig) -> None:
        tracks = list(result.tracks) if result is not None else []
        self.list.set_tracks(tracks)
        self.list.setVisible(bool(tracks))
        if tracks:
            rows = min(len(tracks), PREVIEW_ROWS)
            self.list.setFixedHeight(rows * ROW_HEIGHT + 4)
        self._queue_btn.setEnabled(bool(tracks))
        self._set_status(result.summary if result is not None else 'Пусто')
        notes = list(result.notes) if result is not None else []
        self._notes.setText(' · '.join(notes))
        self._notes.setVisible(bool(notes))
        if not tracks:
            self.status_message.emit('Микс: подходящих треков не нашлось')
            return
        if play:
            # Дальше главное окно само решит, чем продолжать очередь: «бесконечно»
            # для микса значит «продолжать тем же миксом»
            self.play_requested.emit(tracks, 0)
            self.status_message.emit(f'Микс: {result.summary}')

    def _enqueue_current(self) -> None:
        tracks = self.list.tracks()
        if tracks:
            self.enqueue_requested.emit(tracks, False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button in (self._play_btn, self._auto_btn, self._build_btn):
            button.setEnabled(not busy)
        self._skeleton.setVisible(busy)
        if busy:
            self.list.hide()
        elif self.list.tracks():
            self.list.show()
        if not busy:
            self._update_shares()
