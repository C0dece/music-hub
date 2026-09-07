"""Действия над треком — одним списком для всех мест программы.

Раньше меню собиралось отдельно в списке треков, в панели плеера и в очереди, и
списки разошлись: где-то не было «добавить в плейлист», где-то «скопировать
ссылку», порядок пунктов везде свой. Здесь одно описание, а вызывающая сторона
передаёт только те обработчики, которые у неё осмысленны: пункт без обработчика
в меню не попадает.

Порядок пунктов постоянный — привыкнув к меню в одном разделе, пользователь
находит тот же пункт на том же месте в любом другом."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtWidgets import QApplication, QMenu

from ..core.track import SOURCE_LOCAL, SOURCE_VK, Track

# id, которого не бывает у настоящего плейлиста: «создать новый»
NEW_PLAYLIST = 0


@dataclass
class TrackActions:
    """Обработчики пунктов меню. Ни один не обязателен."""

    play: Optional[Callable[[list], None]] = None            # список треков
    enqueue: Optional[Callable[[list, bool], None]] = None    # треки, «следующим»
    add_vk: Optional[Callable[[list], None]] = None
    favorite: Optional[Callable[[list], None]] = None         # переключает
    library: Optional[Callable[[list], None]] = None          # «Моя музыка», переключает
    offline: Optional[Callable[[list], None]] = None          # офлайн-копия, переключает
    playlist: Optional[Callable[[list, int], None]] = None     # треки, id плейлиста
    download: Optional[Callable[[list], None]] = None
    remove: Optional[Callable[[list], None]] = None            # номера строк
    radio: Optional[Callable[[Track], None]] = None
    artist: Optional[Callable[[str], None]] = None
    hide: Optional[Callable[[list], None]] = None
    hide_artist: Optional[Callable[[str], None]] = None
    open_source: Optional[Callable[[Track], None]] = None


def copy_link(track: Track) -> None:
    clipboard = QApplication.clipboard()
    if clipboard is not None and track is not None and track.url:
        clipboard.setText(track.url)


def fill_menu(menu: QMenu, tracks, actions: TrackActions, *, store=None,
              rows=None) -> QMenu:
    """Дописать в меню стандартный набор действий над `tracks`."""
    tracks = [t for t in (tracks or []) if t is not None]
    if not tracks:
        return menu
    single = tracks[0] if len(tracks) == 1 else None

    if actions.play is not None:
        menu.addAction('Играть', lambda: actions.play(tracks))
    if actions.enqueue is not None:
        menu.addAction('Играть следующим', lambda: actions.enqueue(tracks, True))
        menu.addAction('В очередь', lambda: actions.enqueue(tracks, False))

    menu.addSeparator()
    if actions.add_vk is not None:
        # Запись VK из поиска или подборки в VK есть, но не у вас: её тоже можно
        # добавить к себе. Свои от чужих отличает сервис переноса, не меню
        targets = [t for t in tracks if t.source == SOURCE_VK or not t.in_vk]
        if targets:
            title = ('Добавить в VK' if len(targets) == 1
                     else f'Добавить в VK ({len(targets)})')
            menu.addAction(title, lambda: actions.add_vk(targets))
    if actions.favorite is not None:
        known = store is not None and all(store.is_favorite(t.uid) for t in tracks)
        menu.addAction('Убрать из избранного' if known else 'В избранное',
                       lambda: actions.favorite(tracks))
    if actions.library is not None:
        # Состояние берём по всем выделенным сразу: пункт должен обещать одно
        # действие, а не «половину добавить, половину убрать»
        saved = store is not None and all(store.is_saved(t.uid) for t in tracks)
        menu.addAction('Убрать из моей музыки' if saved else 'В мою музыку',
                       lambda: actions.library(tracks))
    if actions.playlist is not None:
        _fill_playlists(menu, tracks, actions, store)

    menu.addSeparator()
    if single is not None and actions.radio is not None:
        menu.addAction('Радио по треку', lambda: actions.radio(single))
    if single is not None and single.artist and actions.artist is not None:
        menu.addAction(f'Перейти к «{single.artist}»',
                       lambda: actions.artist(single.artist))
    if actions.download is not None and any(not t.cached for t in tracks):
        menu.addAction('Скачать', lambda: actions.download(tracks))
    if actions.offline is not None:
        # Свой файл уже и так на диске — предлагать «сохранить офлайн» незачем
        targets = [t for t in tracks if t.source != SOURCE_LOCAL]
        if targets:
            uids = {t.uid for t in targets}
            cached = store is not None and store.cached_uids(uids) >= uids
            menu.addAction('Убрать из офлайна' if cached else 'Сохранить офлайн',
                           lambda: actions.offline(targets))
    if actions.remove is not None and rows:
        menu.addAction('Убрать', lambda: actions.remove(list(rows)))

    if actions.hide is not None:
        menu.addSeparator()
        menu.addAction('Не нравится', lambda: actions.hide(tracks))
        if single is not None and single.artist and actions.hide_artist is not None:
            menu.addAction(f'Не рекомендовать «{single.artist}»',
                           lambda: actions.hide_artist(single.artist))

    if single is not None and single.url:
        menu.addSeparator()
        if actions.open_source is not None:
            menu.addAction('Открыть источник в браузере',
                           lambda: actions.open_source(single))
        menu.addAction('Скопировать ссылку', lambda: copy_link(single))
    return menu


def _fill_playlists(menu: QMenu, tracks, actions: TrackActions, store) -> None:
    """Подменю «Добавить в плейлист». Системные списки сюда не попадают:
    избранное добавляется отдельным пунктом выше."""
    submenu = menu.addMenu('Добавить в плейлист')
    playlists = []
    if store is not None:
        try:
            playlists = [p for p in store.playlists(include_system=False)
                         if (p.get('source') or 'hub') == 'hub']
        except Exception:  # база могла быть закрыта — меню важнее
            playlists = []
    for playlist in playlists:
        playlist_id = int(playlist['id'])
        submenu.addAction(playlist['title'] or 'Без названия',
                          lambda _=False, pid=playlist_id: actions.playlist(tracks, pid))
    if playlists:
        submenu.addSeparator()
    submenu.addAction('Новый плейлист…', lambda: actions.playlist(tracks, NEW_PLAYLIST))


def build_menu(parent, tracks, actions: TrackActions, *, store=None, rows=None) -> QMenu:
    return fill_menu(QMenu(parent), tracks, actions, store=store, rows=rows)
