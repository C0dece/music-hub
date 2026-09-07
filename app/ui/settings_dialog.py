import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .. import config
from ..core import js_runtime, proxy
from ..core.async_task import run_async
from ..core.browser_cookies import BROWSER_LABELS
from . import player_icons, theme
from .icon import app_icon
from .widgets import Card, StatusChip

PROXY_MODES = [
    ('Автоматически: системный прокси или запущенный VPN-клиент', proxy.MODE_AUTO),
    ('Без прокси: прямое подключение', proxy.MODE_OFF),
    ('Свой адрес', proxy.MODE_MANUAL),
]

PROXY_NOTE = (
    'Чтобы пользоваться своим прокси, впишите адрес с портом, а логин и пароль в поля '
    'ниже, если прокси их требует. Режим переключится сам, дальше приложение будет качать '
    'через этот прокси всё: YouTube, VK и окно входа в VK.\n\n'
    'Схему можно не писать: без неё адрес считается http. Для SOCKS укажите её явно: '
    'socks5://хост:порт.\n\n'
    'Приложение качает само, а не через браузер, поэтому прокси-расширения и VPN, '
    'включённые в браузере, на загрузки не действуют. Системный прокси Windows и режим '
    'TUN/VPN приложение подхватывает само; клиент вроде Clash, v2rayN или Nekoray без '
    'системного прокси оно попробует найти на обычных портах.\n\n'
    '«Проверить» пробует достучаться до YouTube, его видеосерверов и VK по отдельности: '
    'сайт YouTube открывается почти всегда, а файлы качаются с видеосерверов, и обычно '
    'недоступны как раз они.\n\n'
    'Галочка «Обходить блокировку видеосерверов» нужна, когда сайт YouTube открывается, '
    'а видеосерверы нет. Имя сайта едет в первом же пакете открытым текстом, даже через '
    'прокси, и по нему соединение и рвут; с галочкой приложение отправляет этот пакет по '
    'частям, целого имени в нём не видно, и файлы качаются. Работает поверх любого режима '
    'выше, включая «Без прокси». Если не нужна, не включайте: лишний слой ни к чему.\n\n'
    'Пароль хранится в файле настроек открытым текстом, держите папку config при себе.'
)

# Имя вкладки нужно и здесь, и в главном окне (значки шапки открывают её),
# поэтому лежит константой, а не строкой в двух местах
TAB_ACCOUNTS = 'Аккаунты'

COOKIE_SOURCES = [
    ('Не использовать', None),
    *BROWSER_LABELS,
    ('Файл cookies.txt / vk_cookies.txt (вручную)', 'file'),
]

COOKIES_NOTE = (
    'Да, куки для YouTube по-прежнему нужны. Без них YouTube всё чаще отвечает '
    '«Sign in to confirm you’re not a bot» и не отдаёт видео, а с куками '
    'работает как обычно. Заодно они открывают доступ к приватным видео и роликам '
    'с возрастным ограничением.\n\n'
    'Проще всего выбрать браузер, в котором вы уже вошли в YouTube. Есть нюанс: пока '
    'браузер запущен, он держит свой файл кук заблокированным, его нужно полностью '
    'закрыть (включая значок в трее). Если так неудобно, выберите «Файл cookies.txt», '
    f'экспортируйте куки расширением (например, «Get cookies.txt LOCALLY») и положите '
    f'файл рядом с приложением под именем {config.COOKIES_FILE.name}.\n\n'
    'Для VK этот пункт не нужен: кнопка «Войти в VK» выше открывает вход прямо в '
    'приложении и сама сохраняет всё необходимое.'
)

JS_NOTE = (
    'YouTube подписывает ссылки на потоки JavaScript-кодом из своего плеера. Чтобы '
    'их расшифровать, yt-dlp запускает этот код во внешнем движке. Без движка '
    'скачивание с YouTube не работает вообще: ошибка «The page needs to be reloaded».\n\n'
    'Достаточно файла QuickJS весом около 2 МБ рядом с приложением. Подойдут и уже '
    'установленные в системе Deno или Node.js 22+.'
)


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, vk_logged_in: bool, parent=None,
                 start_tab: str = ''):
        super().__init__(parent)
        self.setWindowTitle('Настройки')
        self.setWindowIcon(app_icon())
        self.resize(720, 600)
        self._settings = dict(settings)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        tabs = QTabWidget()
        tabs.addTab(self._scrollable(self._build_downloads_tab()), 'Загрузки')
        tabs.addTab(self._scrollable(self._build_music_tab()), 'Музыка')
        tabs.addTab(self._scrollable(self._build_app_tab()), 'Программа')
        tabs.addTab(self._scrollable(self._build_access_tab(vk_logged_in)), TAB_ACCOUNTS)
        # Значок VK или YouTube в шапке — это и есть кнопка «разобраться со
        # входом»: открываем сразу нужную вкладку, чтобы человек не искал её сам
        if start_tab:
            for i in range(tabs.count()):
                if tabs.tabText(i) == start_tab:
                    tabs.setCurrentIndex(i)
                    break
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('Сохранить')
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText('Отмена')
        cancel.setObjectName('secondary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------- вкладка «Загрузки» ----------
    def _build_downloads_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(4, 12, 4, 4)
        box.setSpacing(14)

        folders = Card()
        folders.layout().addWidget(self._title('Куда сохранять'))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._music_dir = QLineEdit(self._settings['music_dir'])
        self._video_dir = QLineEdit(self._settings['video_dir'])
        form.addRow('Папка для музыки:', self._dir_row(self._music_dir))
        form.addRow('Папка для видео:', self._dir_row(self._video_dir))

        folders.layout().addLayout(form)
        box.addWidget(folders)

        # Скорость вынесена из «Куда сохранять»: там были папки, и счётчик
        # загрузок посреди них читался как ещё одна настройка хранения
        speed = Card()
        speed.layout().addWidget(self._title('Скорость'))
        speed_form = QFormLayout()
        speed_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._concurrency = QSpinBox()
        self._concurrency.setRange(1, 8)
        self._concurrency.setValue(self._settings.get('concurrency', 3))
        self._concurrency.setSuffix('  шт.')
        self._concurrency.setToolTip('Сколько файлов из очереди качать одновременно')
        speed_form.addRow('Одновременных загрузок:',
                          self._narrow(self._concurrency))
        self._fragment_concurrency = QSpinBox()
        self._fragment_concurrency.setRange(1, 16)
        self._fragment_concurrency.setValue(
            self._settings.get('fragment_concurrency', 4))
        self._fragment_concurrency.setSuffix('  шт.')
        self._fragment_concurrency.setToolTip(
            'На сколько потоков делить один файл')
        speed_form.addRow('Потоков на файл:',
                          self._narrow(self._fragment_concurrency))
        speed.layout().addLayout(speed_form)
        speed.layout().addWidget(self._hint(
            'Скорость YouTube режет каждому соединению отдельно, поэтому несколько '
            'потоков на файл обычно быстрее одного. Если загрузка стала рваться, '
            'поставьте 1: некоторые провайдеры и прокси не любят много соединений.'))
        box.addWidget(speed)

        behaviour = Card()
        behaviour.layout().addWidget(self._title('Повторные загрузки'))
        self._skip_downloaded = QCheckBox('Пропускать то, что уже скачивалось')
        self._skip_downloaded.setChecked(bool(self._settings.get('skip_downloaded', True)))
        self._skip_downloaded.setToolTip(
            'Программа помнит скачанное и не ставит эти файлы в очередь повторно')
        behaviour.layout().addWidget(self._skip_downloaded)
        behaviour.layout().addWidget(self._hint(
            'Если файл удалить вручную, а галочку оставить, он не скачается заново. '
            'Снимите её, чтобы качать всё подряд.'))
        box.addWidget(behaviour)
        box.addWidget(self._build_proxy_card())

        box.addStretch(1)
        return page

    # ---------- вкладка «Музыка» ----------
    def _build_music_tab(self) -> QWidget:
        """Только про саму музыку. Раньше эта вкладка звалась «Музыка и связь»
        и держала заодно значок у часов, горячие клавиши и мост с браузером —
        найти там что-то по названию было невозможно."""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(4, 12, 4, 4)
        box.setSpacing(14)
        box.addWidget(self._build_playback_card())
        box.addWidget(self._build_my_music_card())
        box.addWidget(self._build_vk_button_card())
        box.addStretch(1)
        return page

    # ---------- вкладка «Программа» ----------
    def _build_app_tab(self) -> QWidget:
        """Настройки самого приложения, а не того, что оно играет."""
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(4, 12, 4, 4)
        box.setSpacing(14)
        box.addWidget(self._build_tray_card())
        box.addWidget(self._build_hotkeys_card())
        box.addWidget(self._build_bridge_card())
        box.addStretch(1)
        return page

    def _build_playback_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Проигрывание'))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._volume = QSpinBox()
        self._volume.setRange(0, 100)
        self._volume.setSuffix('  %')
        self._volume.setValue(int(self._settings.get('volume', 80)))
        form.addRow('Громкость:', self._narrow(self._volume))
        card.layout().addLayout(form)

        self._remember_volume = QCheckBox('Запоминать громкость между запусками')
        self._remember_volume.setChecked(bool(self._settings.get('remember_volume', True)))
        card.layout().addWidget(self._remember_volume)

        self._autoplay_next = QCheckBox('Доиграл трек: включать следующий из очереди')
        self._autoplay_next.setChecked(bool(self._settings.get('autoplay_next', True)))
        card.layout().addWidget(self._autoplay_next)
        return card

    def _build_my_music_card(self) -> Card:
        """Своя музыка и офлайн — одна карточка: это одна и та же фонотека,
        просто с двух сторон — что в неё добавляют и что из неё держат на диске."""
        card = Card()
        card.layout().addWidget(self._title('Моя музыка'))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._offline_dir = QLineEdit(
            str(self._settings.get('offline_dir') or config.OFFLINE_DIR))
        form.addRow('Папка офлайна:', self._dir_row(self._offline_dir))

        self._offline_limit = QDoubleSpinBox()
        self._offline_limit.setRange(0, 500)
        self._offline_limit.setDecimals(1)
        self._offline_limit.setSingleStep(0.5)
        self._offline_limit.setSuffix('  ГБ')
        # 0 — это «сколько влезет», и написать это словами понятнее, чем нулём
        self._offline_limit.setSpecialValueText('без ограничения')
        self._offline_limit.setValue(float(self._settings.get('offline_limit_gb') or 0))
        form.addRow('Занимать не больше:', self._narrow(self._offline_limit, 190))
        card.layout().addLayout(form)

        self._offline_favorites = QCheckBox(
            'Сохранять офлайн всё, что попадает в «Любимое»')
        self._offline_favorites.setChecked(
            bool(self._settings.get('offline_favorites', True)))
        card.layout().addWidget(self._offline_favorites)
        card.layout().addWidget(self._hint(
            'Копии лежат отдельно от скачанного вручную: содержимое папки офлайна '
            'программа считает своим и при нехватке места убирает самые давние копии. '
            '«Любимое» при этом не трогается.'))

        card.layout().addWidget(self._title('Папки со своей музыкой'))
        self._local_dirs = QListWidget()
        self._local_dirs.setMaximumHeight(96)
        for path in self._settings.get('local_dirs') or []:
            self._local_dirs.addItem(str(path))
        card.layout().addWidget(self._local_dirs)
        # Пустая рамка ничего не объясняет — вместо неё строка о том, что делать
        self._local_dirs_empty = self._hint(
            'Пока ни одной папки. Добавьте свою, и её музыка появится в «Треках».')
        card.layout().addWidget(self._local_dirs_empty)

        buttons = QHBoxLayout()
        add = QPushButton('Добавить папку…')
        add.setObjectName('secondary')
        add.clicked.connect(self._add_local_dir)
        remove = self._local_dirs_remove = QPushButton('Убрать из списка')
        remove.setObjectName('secondary')
        remove.clicked.connect(self._remove_local_dir)
        # Убирать нечего, пока в списке ничего не выбрано
        self._local_dirs.itemSelectionChanged.connect(self._sync_local_dirs)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        card.layout().addLayout(buttons)
        self._sync_local_dirs()
        card.layout().addWidget(self._hint(
            'Музыка из этих папок попадает в раздел «Моя музыка» → «Треки». Сами файлы '
            'программа не переносит и не удаляет, только читает.'))
        return card

    def _add_local_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, 'Папка со своей музыкой',
                                                self._settings.get('music_dir', ''))
        if not path:
            return
        existing = [self._local_dirs.item(row).text()
                    for row in range(self._local_dirs.count())]
        if not any(os.path.normcase(item) == os.path.normcase(path) for item in existing):
            self._local_dirs.addItem(path)
        self._sync_local_dirs()

    def _remove_local_dir(self) -> None:
        for item in self._local_dirs.selectedItems():
            self._local_dirs.takeItem(self._local_dirs.row(item))
        self._sync_local_dirs()

    def _sync_local_dirs(self) -> None:
        has_rows = self._local_dirs.count() > 0
        self._local_dirs.setVisible(has_rows)
        self._local_dirs_empty.setVisible(not has_rows)
        self._local_dirs_remove.setEnabled(bool(self._local_dirs.selectedItems()))

    def _build_vk_button_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Кнопка «+ VK»'))

        self._vk_match_first = QCheckBox('Сначала искать готовую запись в VK')
        self._vk_match_first.setChecked(bool(self._settings.get('vk_match_first', True)))
        card.layout().addWidget(self._vk_match_first)

        self._vk_ask_ambiguous = QCheckBox('Спрашивать, когда подходящих записей несколько')
        self._vk_ask_ambiguous.setChecked(
            bool(self._settings.get('vk_ask_on_ambiguous', True)))
        card.layout().addWidget(self._vk_ask_ambiguous)

        self._vk_upload_fallback = QCheckBox('Не нашли в VK: скачать и залить своим файлом')
        self._vk_upload_fallback.setChecked(
            bool(self._settings.get('vk_upload_fallback', True)))
        card.layout().addWidget(self._vk_upload_fallback)

        card.layout().addWidget(self._hint(
            'Добавить готовую запись быстрее и не занимает место: файл заливается, '
            'только если ничего похожего в VK не нашлось.'))
        return card

    def _build_tray_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Значок у часов'))

        self._tray_enabled = QCheckBox('Показывать значок в области уведомлений')
        self._tray_enabled.setChecked(bool(self._settings.get('tray_enabled', True)))
        card.layout().addWidget(self._tray_enabled)

        self._minimize_to_tray = QCheckBox('Сворачивать окно к значку')
        self._minimize_to_tray.setChecked(bool(self._settings.get('minimize_to_tray', False)))
        card.layout().addWidget(self._minimize_to_tray)

        self._close_to_tray = QCheckBox('Крестик прячет окно, а не закрывает приложение')
        self._close_to_tray.setChecked(bool(self._settings.get('close_to_tray', False)))
        card.layout().addWidget(self._close_to_tray)

        self._tray_notifications = QCheckBox('Показывать короткие сообщения у часов')
        self._tray_notifications.setChecked(
            bool(self._settings.get('tray_notifications', True)))
        card.layout().addWidget(self._tray_notifications)

        card.layout().addWidget(self._hint(
            'Выйти из спрятанного приложения можно через меню значка, пункт «Выход».'))
        # Прятать окно некуда, если значка нет
        self._tray_enabled.toggled.connect(self._sync_tray_controls)
        self._sync_tray_controls(self._tray_enabled.isChecked())
        return card

    def _sync_tray_controls(self, enabled: bool) -> None:
        for check in (self._minimize_to_tray, self._close_to_tray, self._tray_notifications):
            check.setEnabled(enabled)

    def _build_hotkeys_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Горячие клавиши'))

        self._hotkeys_enabled = QCheckBox('Управлять музыкой из любой программы')
        self._hotkeys_enabled.setChecked(bool(self._settings.get('hotkeys_enabled', True)))
        card.layout().addWidget(self._hotkeys_enabled)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._hotkey_edits = {}
        rows = [
            ('hotkey_play_pause', 'Играть / пауза:'),
            ('hotkey_next', 'Следующий трек:'),
            ('hotkey_prev', 'Предыдущий трек:'),
            ('hotkey_add_vk', 'Добавить в VK:'),
            ('hotkey_favorite', 'В избранное:'),
            ('hotkey_show', 'Показать окно:'),
        ]
        for key, label in rows:
            edit = QLineEdit(str(self._settings.get(key, '')))
            edit.setPlaceholderText('например, Ctrl+Alt+Space')
            self._hotkey_edits[key] = edit
            form.addRow(label, edit)
        card.layout().addLayout(form)

        self._hotkeys_media = QCheckBox('Слушать мультимедийные клавиши клавиатуры')
        self._hotkeys_media.setChecked(bool(self._settings.get('hotkeys_media_keys', True)))
        card.layout().addWidget(self._hotkeys_media)

        card.layout().addWidget(self._hint(
            'Пишите сочетания как Ctrl+Alt+Space: буква или цифра, F1–F12, стрелки, '
            'Space, Enter. Занятое другой программой сочетание просто не сработает, '
            'остальные при этом продолжат работать. Пустое поле выключает действие.'))
        self._hotkeys_enabled.toggled.connect(self._sync_hotkey_controls)
        self._sync_hotkey_controls(self._hotkeys_enabled.isChecked())
        return card

    def _sync_hotkey_controls(self, enabled: bool) -> None:
        for edit in self._hotkey_edits.values():
            edit.setEnabled(enabled)
        self._hotkeys_media.setEnabled(enabled)

    def _build_bridge_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Связь с браузером'))

        self._bridge_enabled = QCheckBox('Принимать ссылки от расширения')
        self._bridge_enabled.setChecked(bool(self._settings.get('bridge_enabled', True)))
        card.layout().addWidget(self._bridge_enabled)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._bridge_port = QSpinBox()
        self._bridge_port.setRange(1025, 65535)
        self._bridge_port.setValue(int(self._settings.get('bridge_port') or 48211))
        form.addRow('Порт:', self._narrow(self._bridge_port))

        self._bridge_token = QLineEdit(str(self._settings.get('bridge_token', '')))
        self._bridge_token.setReadOnly(True)
        self._bridge_token.setPlaceholderText('появится после запуска связи')
        row = QWidget()
        row_box = QHBoxLayout(row)
        row_box.setContentsMargins(0, 0, 0, 0)
        row_box.addWidget(self._bridge_token)
        show = QPushButton('Показать')
        show.setObjectName('secondary')
        show.setCheckable(True)
        show.toggled.connect(self._toggle_bridge_token)
        row_box.addWidget(show)
        self._bridge_token.setEchoMode(QLineEdit.Password)
        form.addRow('Ключ доступа:', row)
        card.layout().addLayout(form)

        card.layout().addWidget(self._hint(
            'Ключ вставляется в настройки расширения. Это секрет: он не даёт доступа '
            'к вашей учётной записи VK, но позволяет управлять проигрывателем. '
            'Приложение слушает только 127.0.0.1, из сети снаружи к нему не достучаться.'))
        self._bridge_enabled.toggled.connect(self._sync_bridge_controls)
        self._sync_bridge_controls(self._bridge_enabled.isChecked())
        return card

    def _toggle_bridge_token(self, shown: bool) -> None:
        self._bridge_token.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)

    def _sync_bridge_controls(self, enabled: bool) -> None:
        self._bridge_port.setEnabled(enabled)
        self._bridge_token.setEnabled(enabled)

    def _build_proxy_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Подключение к интернету'))

        row = QHBoxLayout()
        row.addWidget(QLabel('Прокси:'))
        self._proxy_mode = QComboBox()
        for label, value in PROXY_MODES:
            self._proxy_mode.addItem(label, value)
        idx = self._proxy_mode.findData(self._settings.get('proxy_mode', proxy.MODE_AUTO))
        self._proxy_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)
        row.addWidget(self._proxy_mode, 1)
        card.layout().addLayout(row)

        addr_row = QHBoxLayout()
        self._proxy_url = QLineEdit(self._settings.get('proxy_url', ''))
        self._proxy_url.setPlaceholderText('адрес:порт, например 45.12.34.56:8000 '
                                           'или socks5://127.0.0.1:1080')
        # Начали печатать адрес — сразу включаем «Свой адрес»: иначе введённые данные
        # молча не применяются, и это выглядит как «настройка не работает»
        self._proxy_url.textEdited.connect(self._switch_to_manual_proxy)
        addr_row.addWidget(self._proxy_url, 1)
        self._proxy_check_btn = QPushButton('Проверить')
        self._proxy_check_btn.setObjectName('secondary')
        self._proxy_check_btn.clicked.connect(self._check_proxy)
        addr_row.addWidget(self._proxy_check_btn)
        card.layout().addLayout(addr_row)

        auth_row = QHBoxLayout()
        self._proxy_user = QLineEdit(self._settings.get('proxy_user', ''))
        self._proxy_user.setPlaceholderText('Логин (если прокси его требует)')
        self._proxy_user.textEdited.connect(self._switch_to_manual_proxy)
        auth_row.addWidget(self._proxy_user, 1)
        self._proxy_pass = QLineEdit(self._settings.get('proxy_pass', ''))
        self._proxy_pass.setPlaceholderText('Пароль')
        self._proxy_pass.setEchoMode(QLineEdit.Password)
        self._proxy_pass.textEdited.connect(self._switch_to_manual_proxy)
        auth_row.addWidget(self._proxy_pass, 1)
        card.layout().addLayout(auth_row)

        self._proxy_fragment = QCheckBox('Обходить блокировку видеосерверов')
        self._proxy_fragment.setChecked(bool(self._settings.get('proxy_fragment', False)))
        card.layout().addWidget(self._proxy_fragment)

        self._proxy_status = self._hint(proxy.describe())
        card.layout().addWidget(self._proxy_status)
        card.layout().addWidget(self._note(
            PROXY_NOTE, 'Свой прокси, VPN и обход блокировки: подробно'))
        self._on_proxy_mode_changed()
        return card

    def _on_proxy_mode_changed(self) -> None:
        manual = self._proxy_mode.currentData() == proxy.MODE_MANUAL
        for field in (self._proxy_url, self._proxy_user, self._proxy_pass):
            field.setEnabled(manual)

    def _switch_to_manual_proxy(self, _text: str = '') -> None:
        if self._proxy_mode.currentData() != proxy.MODE_MANUAL:
            self._proxy_mode.setCurrentIndex(self._proxy_mode.findData(proxy.MODE_MANUAL))

    def _check_proxy(self) -> None:
        self._proxy_check_btn.setEnabled(False)
        self._proxy_status.setText('Проверяю соединение…')

        def on_done(result, error):
            self._proxy_check_btn.setEnabled(True)
            if error:
                self._proxy_status.setText(f'Проверка не удалась: {error}')
                return
            _ok, text = result
            self._proxy_status.setText(text)

        # Пробелы обрезаем так же, как при сохранении: иначе проверка и работа
        # приложения пользовались бы разными паролями.
        run_async(proxy.check, on_done, self._proxy_mode.currentData(), self._proxy_url.text(),
                  self._proxy_user.text().strip(), self._proxy_pass.text().strip(),
                  self._proxy_fragment.isChecked())

    # ---------- вкладка «Доступ и вход» ----------
    def _build_access_tab(self, vk_logged_in: bool) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(4, 12, 4, 4)
        box.setSpacing(14)

        box.addWidget(self._build_vk_card(vk_logged_in))
        box.addWidget(self._build_js_card())
        box.addWidget(self._build_cookies_card())
        box.addStretch(1)
        return page

    def _build_vk_card(self, vk_logged_in: bool) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Аккаунт VK'))

        row = QHBoxLayout()
        self._vk_chip = StatusChip('vk', 'off')
        row.addWidget(self._vk_chip)
        # Значок показывает состояние цветом, но в настройках рядом есть место
        # под слово: тут человек разбирается, а не скользит взглядом
        self._vk_label = QLabel()
        self._vk_label.setObjectName('hint')
        row.addWidget(self._vk_label)
        row.addStretch(1)
        self.login_button = QPushButton('Войти в VK')
        self.logout_button = QPushButton('Выйти')
        self.logout_button.setObjectName('secondary')
        row.addWidget(self.login_button)
        row.addWidget(self.logout_button)
        card.layout().addLayout(row)

        card.layout().addWidget(self._hint(
            'Музыка VK скачивается через ваш аккаунт: официального API для этого у VK нет. '
            'Вход открывается прямо в приложении, пароль никуда не передаётся и не хранится. '
            'Если VK изменит правила, функция может временно перестать работать.'))
        self.set_vk_status(vk_logged_in)
        return card

    def _build_js_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Движок JavaScript (нужен для YouTube)'))

        row = QHBoxLayout()
        self._js_chip = StatusChip('youtube', 'off')
        row.addWidget(self._js_chip)
        self._js_label = QLabel()
        self._js_label.setObjectName('hint')
        row.addWidget(self._js_label)
        row.addStretch(1)
        self._js_button = QPushButton('Скачать движок')
        self._js_button.clicked.connect(self._download_js_runtime)
        row.addWidget(self._js_button)
        card.layout().addLayout(row)

        card.layout().addWidget(self._note(JS_NOTE, 'Зачем он нужен и что подойдёт'))
        self._refresh_js_status()
        return card

    def _build_cookies_card(self) -> Card:
        card = Card()
        card.layout().addWidget(self._title('Куки для YouTube'))

        row = QHBoxLayout()
        row.addWidget(QLabel('Источник:'))
        self._cookies_browser = QComboBox()
        for label, value in COOKIE_SOURCES:
            self._cookies_browser.addItem(label, value)
        idx = self._cookies_browser.findData(self._settings.get('cookies_browser', 'chrome'))
        self._cookies_browser.setCurrentIndex(idx if idx >= 0 else 0)
        row.addWidget(self._cookies_browser, 1)
        card.layout().addLayout(row)

        card.layout().addWidget(self._note(
            COOKIES_NOTE, 'Зачем нужны куки и как их передать'))
        return card

    # ---------- мелочи ----------
    @staticmethod
    def _title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName('h2')
        return label

    @staticmethod
    def _narrow(widget: QWidget, width: int = 150) -> QWidget:
        """Поле по размеру содержимого, а не во всю строку формы."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        widget.setFixedWidth(width)
        row.addWidget(widget)
        row.addStretch(1)
        return box

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName('hint')
        label.setWordWrap(True)
        return label

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        """Вкладка с прокруткой вместо вкладки во весь рост.

        Развёрнутое пояснение тянуло окно вниз, и настройки не помещались на экран.
        Теперь высоту задаём мы, а разрастается содержимое внутрь полосы прокрутки."""
        area = QScrollArea()
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return area

    def _note(self, text: str, summary: str) -> QWidget:
        """Длинное пояснение, свёрнутое до одной строки.

        Эти тексты читают один раз, а место занимали всегда: три подробных рассказа
        внизу карточек и делали окно настроек огромным по высоте."""
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)

        body = self._hint(text)
        body.setVisible(False)

        toggle = QPushButton(summary)
        toggle.setObjectName('link')
        toggle.setCheckable(True)
        toggle.setCursor(Qt.PointingHandCursor)
        arrow = theme.color('accent_text')
        toggle.setIcon(player_icons.draw('chevron_right', arrow, 14))
        toggle.toggled.connect(
            lambda on: (body.setVisible(on),
                        toggle.setIcon(player_icons.draw(
                            'chevron_down' if on else 'chevron_right', arrow, 14))))

        col.addWidget(toggle)
        col.addWidget(body)
        return box

    def _dir_row(self, line_edit: QLineEdit) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(line_edit)
        browse = QPushButton('Обзор…')
        browse.setObjectName('secondary')
        browse.clicked.connect(lambda: self._browse(line_edit))
        row.addWidget(browse)
        return container

    def _browse(self, line_edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, 'Выберите папку', line_edit.text())
        if path:
            line_edit.setText(path)

    def set_vk_status(self, logged_in: bool) -> None:
        if logged_in:
            self._vk_chip.update_chip('Вход выполнен', 'ok', 'Аккаунт VK подключён')
            self._vk_label.setText('вход выполнен')
            self.login_button.setText('Сменить аккаунт')
            self.logout_button.setEnabled(True)
        else:
            self._vk_chip.update_chip('Вход не выполнен', 'off', 'Музыка VK недоступна')
            self._vk_label.setText('без входа музыка VK недоступна')
            self.login_button.setText('Войти в VK')
            self.logout_button.setEnabled(False)

    # ---------- JS-движок ----------
    def _refresh_js_status(self) -> None:
        found = js_runtime.find()
        if found:
            self._js_chip.update_chip('Готов', 'ok', 'YouTube будет скачиваться')
            self._js_label.setText(js_runtime.describe())
            self._js_button.setText('Скачать заново')
        else:
            self._js_chip.update_chip('Не найден', 'warn', 'YouTube скачиваться не будет')
            self._js_label.setText('без него YouTube не работает')
            self._js_button.setText('Скачать движок')

    def _download_js_runtime(self) -> None:
        self._js_button.setEnabled(False)
        self._js_button.setText('Скачиваю…')

        def on_done(_result, error):
            self._js_button.setEnabled(True)
            if error:
                self._js_chip.update_chip('Ошибка', 'warn', str(error))
                self._js_label.setText(f'не удалось скачать: {error}')
                self._js_button.setText('Повторить')
                return
            self._refresh_js_status()

        run_async(js_runtime.download_bundled, on_done)

    def result_settings(self) -> dict:
        self._settings['music_dir'] = self._music_dir.text()
        self._settings['video_dir'] = self._video_dir.text()
        self._settings['concurrency'] = self._concurrency.value()
        self._settings['fragment_concurrency'] = self._fragment_concurrency.value()
        self._settings['cookies_browser'] = self._cookies_browser.currentData()
        self._settings['skip_downloaded'] = self._skip_downloaded.isChecked()
        self._settings['proxy_mode'] = self._proxy_mode.currentData()
        self._settings['proxy_url'] = self._proxy_url.text().strip()
        self._settings['proxy_user'] = self._proxy_user.text().strip()
        # Пароль из буфера часто приезжает с пробелом или переводом строки на конце.
        # Прокси такой не примет, а за звёздочками в поле этого не видно — отсюда 407
        # при «всё введено верно».
        self._settings['proxy_pass'] = self._proxy_pass.text().strip()
        self._settings['proxy_fragment'] = self._proxy_fragment.isChecked()

        self._settings['volume'] = self._volume.value()
        self._settings['remember_volume'] = self._remember_volume.isChecked()
        self._settings['autoplay_next'] = self._autoplay_next.isChecked()
        self._settings['offline_dir'] = self._offline_dir.text().strip()
        self._settings['offline_limit_gb'] = self._offline_limit.value()
        self._settings['offline_favorites'] = self._offline_favorites.isChecked()
        self._settings['local_dirs'] = [self._local_dirs.item(row).text()
                                        for row in range(self._local_dirs.count())]
        self._settings['vk_match_first'] = self._vk_match_first.isChecked()
        self._settings['vk_ask_on_ambiguous'] = self._vk_ask_ambiguous.isChecked()
        self._settings['vk_upload_fallback'] = self._vk_upload_fallback.isChecked()

        self._settings['tray_enabled'] = self._tray_enabled.isChecked()
        self._settings['minimize_to_tray'] = self._minimize_to_tray.isChecked()
        self._settings['close_to_tray'] = self._close_to_tray.isChecked()
        self._settings['tray_notifications'] = self._tray_notifications.isChecked()

        self._settings['hotkeys_enabled'] = self._hotkeys_enabled.isChecked()
        for key, edit in self._hotkey_edits.items():
            self._settings[key] = edit.text().strip()
        self._settings['hotkeys_media_keys'] = self._hotkeys_media.isChecked()

        self._settings['bridge_enabled'] = self._bridge_enabled.isChecked()
        self._settings['bridge_port'] = self._bridge_port.value()
        return self._settings
