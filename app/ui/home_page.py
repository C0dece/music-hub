"""Главная: что играет, что слушали и что можно послушать дальше.

Страница нужна, чтобы за один взгляд вернуться к прерванному — продолжить
очередь, включить недавнее. Списка разделов здесь нет: он и так стоит слева, а
главная повторяла его целиком. Ниже — подборки: настоящая
лента YouTube Music, если вход есть, и честно подписанные подборки по поиску,
если её нет (выдавать поиск за рекомендации нельзя).

Подборки грузятся в фоне и заранее занимают постоянные места (слоты). Так их
списки можно один раз подключить к главному окну: если бы блоки создавались на
каждый ответ, каждый новый список остался бы без действий по правой кнопке."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ..core.async_task import run_async
from ..core.mixer import MODE_DISCOVER, MODE_KNOWN, MODE_MIXED, MixConfig
from ..core.player_controller import PlayerController
from ..core.track import SOURCE_LOCAL, SOURCE_VK, SOURCE_YOUTUBE
from . import player_icons
from .flow_layout import FlowRow
from .track_list import ROW_HEIGHT, TrackListWidget
from .widgets import Card, ElidedLabel, Skeleton

# Главная — витрина, а не архив: за длинными списками есть «История» и «Микс»
RECENT_LIMIT = 8
SECTION_LIMIT = 12          # столько треков показываем в подборке
SECTION_SLOTS = 5           # столько подборок помещается на главную
SECTIONS_TTL = 600          # секунд: лента YouTube меняется не каждую минуту


class _Launch:
    """Плитка «включить музыку»: куда ведёт и какой микс приносит."""

    def __init__(self, page: str, title: str, hint: str, icon: str,
                 mode: str = MODE_MIXED, weights: dict | None = None,
                 vk_recoms: bool = True):
        self.page = page
        self.title = title
        self.hint = hint
        self.icon = icon
        self._mode = mode
        self._weights = weights
        self._vk_recoms = vk_recoms

    def config(self) -> MixConfig:
        """Свежая конфигурация: её будут править ползунками, общая не годится.

        Размер порции и автопродолжение остаются по умолчанию — кнопка про то,
        откуда брать музыку, а не про то, сколько её отмерить.
        """
        return MixConfig(weights=dict(self._weights) if self._weights else None,
                         mode=self._mode, vk_recoms=self._vk_recoms)


# «Общий микс» без долей — берёт то, что уже настроено на странице микса;
# остальные плитки сдвигают вес к своему источнику, а не отключают прочие
# начисто: волна из одного колодца быстро повторяется
LAUNCHES: tuple[_Launch, ...] = (
    _Launch('mix', 'Общий микс', 'Всё сразу: VK, YouTube и свои файлы',
            'shuffle', MODE_MIXED),
    _Launch('vk', 'Волна VK', 'Рекомендации VK и своя фонотека', 'radio',
            MODE_MIXED, {SOURCE_VK: 80, SOURCE_YOUTUBE: 20, SOURCE_LOCAL: 0}),
    _Launch('youtube', 'Микс YouTube', 'Лента YouTube Music и новое',
            'headphones', MODE_DISCOVER,
            {SOURCE_VK: 20, SOURCE_YOUTUBE: 80, SOURCE_LOCAL: 0}),
    _Launch('local', 'Из своих файлов', 'Только музыка с компьютера', 'audio',
            MODE_KNOWN, {SOURCE_VK: 0, SOURCE_YOUTUBE: 0, SOURCE_LOCAL: 100},
            vk_recoms=False),
)


class HomePage(QWidget):
    """Сводка, быстрые переходы и подборки. Данные читаются при показе страницы."""

    # Кнопка запуска: куда перейти и с какой конфигурацией включить микс.
    # Словарь, а не MixConfig, — то же, что уходит в settings.json (`to_dict`)
    mix_requested = Signal(str, dict)

    def __init__(self, player: PlayerController, store, recommender=None, parent=None):
        super().__init__(parent)
        self._player = player
        self._store = store
        self._recommender = recommender
        self._sections_at = 0.0
        self._sections_busy = False
        self._build_ui()
        player.track_changed.connect(self.set_current)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Всё содержимое в прокрутке: на узком окне подборки просто уезжают вниз,
        # а не сжимаются в нечитаемые полоски
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)

        # Карточки «сейчас играет» здесь нет: то же самое во всю ширину
        # показывает полоса плеера внизу, и главная повторяла её слово в слово
        box.addWidget(self._build_launch_card())

        self.recent, self._recent_label = self._block(box, 'Недавно слушали')
        self.imported, self._imported_label = self._block(box, 'Недавно добавлено в VK')

        status = FlowRow(spacing=8)
        self._sections_hint = ElidedLabel('Собираю подборки…')
        self._sections_hint.setObjectName('hint')
        status.add(self._sections_hint)
        self._sections_retry = QPushButton('Повторить')
        self._sections_retry.setObjectName('secondary')
        self._sections_retry.clicked.connect(self._retry_sections)
        self._sections_retry.hide()
        status.add(self._sections_retry)
        status.add_stretch()
        box.addWidget(status)

        # Пока подборки едут, на их месте видно, что что-то грузится
        self._sections_skeleton = Skeleton(3)
        self._sections_skeleton.hide()
        box.addWidget(self._sections_skeleton)

        # Постоянные места под подборки: заполняются, когда придёт ответ
        self._slots: list[tuple[QLabel, TrackListWidget]] = []
        for _index in range(SECTION_SLOTS):
            widget, label = self._block(box, '')
            label.hide()
            widget.hide()
            self._slots.append((label, widget))

        box.addStretch(1)
        area.setWidget(inner)
        outer.addWidget(area, 1)

    def _build_launch_card(self) -> Card:
        """Включить музыку одной кнопкой, не открывая настройки.

        Каждая плитка ведёт в свой раздел и приносит туда готовую конфигурацию:
        «Общий микс» — на страницу микса как есть, остальные — с долями под свой
        источник. Настройки при этом видны и правятся дальше руками, как и у
        настроений: кнопка предзаполняет, а не подменяет.
        """
        card = Card()
        title = QLabel('Включить музыку')
        title.setObjectName('h2')
        card.layout().addWidget(title)
        # Не ElidedLabel: это не статус в одну строку, а объяснение, зачем плитки
        # вообще нужны. В узком окне обрезка съедала вторую половину фразы — ровно
        # ту, где сказано про бесконечную очередь, — хотя место под вторую строку
        # в карточке есть. Переносим по словам, ширину при этом не требуем
        hint = QLabel('Один выбор, и волна собирается сама. '
                      'Играет без конца: в конце очереди добавляются новые треки.')
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        hint.setMinimumWidth(1)
        card.layout().addWidget(hint)

        tiles = FlowRow(spacing=8)
        for launch in LAUNCHES:
            button = QPushButton(launch.title)
            button.setObjectName('secondary')
            button.setIcon(player_icons.draw(launch.icon))
            button.setToolTip(launch.hint)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, item=launch:
                                   self._start_launch(item))
            tiles.add(button)
        tiles.add_stretch()
        card.layout().addWidget(tiles)
        return card

    def _start_launch(self, launch: '_Launch') -> None:
        """Плитка нажата: переход и запуск — одним сигналом.

        Разделять их нельзя: главное окно должно сперва открыть нужную вкладку,
        а потом уже включать волну, иначе человек услышит музыку, не увидев, где
        она собралась.
        """
        self.mix_requested.emit(launch.page, launch.config().to_dict())

    def _block(self, box: QVBoxLayout, title: str) -> tuple[TrackListWidget, QLabel]:
        label = QLabel(title)
        label.setObjectName('h2')
        box.addWidget(label)
        widget = TrackListWidget(self)
        box.addWidget(widget)
        return widget, label

    @property
    def lists(self) -> list[TrackListWidget]:
        """Все списки страницы — для общей проводки действий в главном окне."""
        return [self.recent, self.imported] + [widget for _label, widget in self._slots]

    # ---------- обновление ----------
    def reload(self) -> None:
        """Перечитать списки — вызывается при переходе на страницу."""
        if self._store is not None:
            self._fill(self.recent, self._recent_label, 'Недавно слушали',
                       self._store.recent_plays(RECENT_LIMIT))
            self._fill(self.imported, self._imported_label, 'Недавно добавлено в VK',
                       self._store.recent_imports(RECENT_LIMIT))
        self.set_current(self._player.current)
        self._load_sections()

    def set_current(self, track) -> None:
        for widget in self.lists:
            widget.set_current(track)

    def _fill(self, widget: TrackListWidget, label: QLabel, title: str, tracks) -> None:
        tracks = list(tracks or ())
        widget.set_tracks(tracks)
        label.setText(title)
        label.setVisible(bool(tracks))
        widget.setVisible(bool(tracks))
        if tracks:
            # Высота ровно по содержимому: страница и так прокручивается целиком,
            # вложенная полоса прокрутки в каждом блоке только мешала бы
            widget.setFixedHeight(len(tracks) * ROW_HEIGHT + 4)

    # ---------- подборки ----------
    def _load_sections(self) -> None:
        if self._recommender is None or self._sections_busy:
            return
        fresh = time.monotonic() - self._sections_at < SECTIONS_TTL
        if fresh and any(label.isVisible() for label, _w in self._slots):
            return
        self._sections_busy = True
        self._sections_hint.setText('Собираю подборки…')
        self._sections_hint.show()
        self._sections_retry.hide()
        if not any(label.isVisible() for label, _w in self._slots):
            self._sections_skeleton.show()

        def on_done(sections, error):
            self._sections_busy = False
            self._sections_skeleton.hide()
            if error:
                self._sections_hint.setText('Подборки не загрузились, попробуйте позже')
                self._sections_retry.show()
                return
            self._sections_at = time.monotonic()
            self._show_sections(sections or [])

        run_async(self._recommender.sections, on_done, SECTION_LIMIT)

    def _retry_sections(self) -> None:
        """Повторить вручную: срок годности прошлого ответа сбрасываем сами."""
        self._sections_at = 0.0
        self._load_sections()

    def _show_sections(self, sections) -> None:
        self._sections_retry.hide()
        self._sections_skeleton.hide()
        current = self._player.current
        for index, (label, widget) in enumerate(self._slots):
            section = sections[index] if index < len(sections) else None
            if section is None or not section.tracks:
                label.hide()
                widget.hide()
                widget.set_tracks([])
                continue
            title = section.title
            if not section.genuine:
                # Честно: это не рекомендация, а поиск вместо неё
                title = f'{title} · {section.label}'
            self._fill(widget, label, title, section.tracks[:SECTION_LIMIT])
            widget.setToolTip(section.label)
            widget.set_current(current)
        shown = sum(1 for label, _w in self._slots if label.isVisible())
        self._sections_hint.setVisible(not shown)
        if not shown:
            self._sections_hint.setText('Подборок пока нет: послушайте что-нибудь, и они появятся')

