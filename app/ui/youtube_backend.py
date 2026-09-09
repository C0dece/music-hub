"""Воспроизведение YouTube для общего плеера - через встроенный плеер сайта.

Это надстройка над тем, что уже работает в `web_preview.py`: та же страница с
YouTube IFrame API на своём origin, тот же профиль Chromium с куками и прокси, тот
же обход проверки «вы не робот». Разница в том, что здесь это не отдельное окно
предпросмотра, а движок для PlayerController: страницу опрашиваем и ей управляем.

Прямые ссылки googlevideo сознательно не трогаем - они не проходят через
настройки прокси приложения (см. шапку web_preview.py).

Страницу поднимаем один раз: следующий трек включается через loadVideoById, а не
перезагрузкой всего документа. Разница заметная - нет повторной загрузки iframe
API, нет мигания и нет паузы между треками."""
from __future__ import annotations

import json
import logging
from dataclasses import replace

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core.player_controller import (STATE_BUFFERING, STATE_ERROR, STATE_LOADING,
                                      STATE_PAUSED, STATE_PLAYING, STATE_STOPPED,
                                      PlaybackBackend)
from ..core.track import SOURCE_YOUTUBE, Track
from .web_preview import _LOCAL_BASE, _make_page

logger = logging.getLogger(__name__)

# Та же страница, что и в предпросмотре, но без ухода на youtube.com своими силами:
# решение о запасном пути принимает Python, иначе он потеряет управление плеером.
_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
 html,body{margin:0;height:100%;background:#0d1015;overflow:hidden}
 #note{position:absolute;left:0;right:0;top:50%;margin-top:-12px;text-align:center;
       color:#8b93a7;font:14px "Segoe UI",Arial,sans-serif}
 #player,#player iframe{position:absolute;left:0;top:0;width:100%;height:100%;border:0}
</style></head><body>
<div id="note">Загружаю плеер…</div><div id="player"></div>
<script>
var ID = '__ID__';
window.hub = {error: '', ready: false, gen: 0};
// Проверку «подтвердите, что вы не робот» плеер ошибкой не считает: он просто не
// начинает играть. Состояния -1 и 5 через восемь секунд после запуска - это она.
window.hub.watch = function (player) {
  var mark = ++window.hub.gen;
  setTimeout(function () {
    if (mark !== window.hub.gen) { return; }
    var state = player.getPlayerState();
    if (state === -1 || state === 5) { window.hub.error = 'blocked'; }
  }, 8000);
};
// Включить другой ролик в уже поднятом плеере - без перезагрузки страницы
window.hub.load = function (id) {
  window.hub.error = '';
  if (window.ytPlayer && window.ytPlayer.loadVideoById) {
    window.ytPlayer.loadVideoById(id);
    window.hub.watch(window.ytPlayer);
    return true;
  }
  return false;
};
function onYouTubeIframeAPIReady() {
  window.ytPlayer = new YT.Player('player', {
    videoId: ID,
    playerVars: {autoplay: 1, rel: 0, modestbranding: 1, playsinline: 1},
    events: {
      onReady: function (e) {
        document.getElementById('note').style.display = 'none';
        window.hub.ready = true;
        e.target.setVolume(__VOL__);
        e.target.playVideo();
        window.hub.watch(e.target);
      },
      // Ролик, запрещённый к встраиванию, плеер не покажет
      onError: function () { window.hub.error = 'embed'; }
    }
  });
}
var s = document.createElement('script');
s.src = 'https://www.youtube.com/iframe_api';
s.onerror = function () { window.hub.error = 'api'; };
document.head.appendChild(s);
</script></body></html>"""

# Один опрос на оба случая: своя страница с IFrame API и обычная страница ролика,
# куда уходим, если встраивание запрещено. На обычной странице цепляемся за сам
# элемент <video> - он есть всегда, в отличие от классов вёрстки YouTube.
_POLL_JS = """(function () {
  var p = window.ytPlayer;
  if (p && p.getPlayerState) {
    return JSON.stringify({m: 'api', s: p.getPlayerState(), t: p.getCurrentTime() || 0,
                           d: p.getDuration() || 0, e: (window.hub || {}).error || ''});
  }
  var v = document.querySelector('video');
  if (v) {
    return JSON.stringify({m: 'dom', s: v.ended ? 0 : (v.paused ? 2 : 1),
                           t: v.currentTime || 0, d: v.duration || 0, e: ''});
  }
  return JSON.stringify({m: 'none', s: -1, t: 0, d: 0, e: (window.hub || {}).error || ''});
})()"""

_CONTROL_JS = """(function () {
  var p = window.ytPlayer, v = document.querySelector('video');
  %s
})()"""

_LOAD_JS = "window.hub && window.hub.load ? window.hub.load('%s') : false"

# Состояния YouTube IFrame API
_YT_ENDED, _YT_PLAYING, _YT_PAUSED, _YT_BUFFERING, _YT_CUED = 0, 1, 2, 3, 5

# Сколько пустых опросов терпим, прежде чем признать, что страница не поднялась.
# Полсекунды на опрос - значит около двадцати секунд ожидания.
_STALL_LIMIT = 40


class YouTubeWebBackend(PlaybackBackend):
    """Движок для PlayerController: страница с плеером YouTube внутри приложения.

    Виджет `view` нужно куда-нибудь положить в окне - иначе Chromium нечего
    показывать (звук при этом идёт и когда область видео скрыта)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # Место в окне занимаем сразу, а сам Chromium поднимаем только при первом
        # воспроизведении. Движок читает настройки прокси, когда создаётся первый
        # QWebEngineView (проверено), а на старте приложения адрес прокси ещё ищется
        # в фоне: подними мы движок вместе с окном - он остался бы без прокси и
        # показывал «нет интернет-соединения».
        # Высоту здесь не задаём: пропорции 16:9 держит контейнер в окне.
        self._holder = QWidget(parent)
        box = QVBoxLayout(self._holder)
        box.setContentsMargins(0, 0, 0, 0)
        self._view: QWebEngineView | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)

        self._track: Track | None = None
        self._volume = 80
        self._state = STATE_STOPPED
        self._degraded = False   # ушли на обычную страницу ролика
        self._ended = False
        self._busy = False       # предыдущий опрос ещё не вернулся
        self._live = False       # страница с IFrame API поднята и отвечает
        self._stall = 0

    @property
    def view(self) -> QWidget:
        """Виджет для окна. Внутри пусто, пока ничего не играло."""
        return self._holder

    def _ensure_view(self) -> QWebEngineView:
        if self._view is None:
            self._view = QWebEngineView(self._holder)
            self._view.setPage(_make_page(self._view))
            self._holder.layout().addWidget(self._view)
        return self._view

    @property
    def limited(self) -> bool:
        """Идёт обычная страница YouTube: реклама и лишнее вокруг, но звук есть."""
        return self._degraded

    # ---------- PlaybackBackend ----------
    def can_play(self, track: Track) -> bool:
        return track.source == SOURCE_YOUTUBE and bool(track.youtube_id)

    def play(self, track: Track) -> None:
        self._track = track
        self._ended = False
        self._stall = 0
        self._set_state(STATE_LOADING)
        if self._live and not self._degraded and self._view is not None:
            # Страница уже поднята: меняем ролик внутри неё, а не грузим заново
            self._view.page().runJavaScript(_LOAD_JS % track.youtube_id, self._on_reload)
            self._timer.start()
            return
        self._start_page(track)

    def _start_page(self, track: Track) -> None:
        self._degraded = False
        self._live = False
        html = _PAGE.replace('__ID__', track.youtube_id).replace('__VOL__', str(self._volume))
        self._ensure_view().setHtml(html, QUrl(_LOCAL_BASE))
        self._timer.start()

    def _on_reload(self, ok) -> None:
        """Плеер на странице не отозвался - поднимаем страницу заново."""
        if ok or self._track is None:
            return
        self._start_page(self._track)

    def swap_clip(self, video_id: str, position_ms: int = 0) -> bool:
        """Продолжить ту же песню другим роликом - с того же места.

        Для видеорежима: в YouTube Music песня обычно лежит «art track» -
        роликом, где вместо картинки одна обложка альбома. Найденный клип
        подставляем сюда, звук при этом тот же.

        Меняем только ролик внутри уже поднятой страницы: перезагрузка вернула бы
        паузу и чёрный кадр на секунду. Страница ещё не отвечает или её увели на
        запасной путь - отказываемся, наверху есть что показать и без клипа."""
        video_id = (video_id or '').strip()
        if (not video_id or self._track is None or not self._live
                or self._degraded or self._view is None):
            return False
        if video_id == self._track.youtube_id:
            return False
        # Трек в очереди остаётся прежним: подменять его номер нельзя, иначе
        # «добавить в избранное» и «открыть источник» уведут на клип, а
        # следующий запуск этой же песни начнётся не с той записи
        self._track = replace(self._track, youtube_id=video_id)
        self._ended = False
        self._stall = 0
        self._view.page().runJavaScript(_LOAD_JS % video_id, self._on_reload)
        if position_ms > 0:
            self.seek(position_ms)
        return True

    def pause(self) -> None:
        self._run('if (p && p.pauseVideo) { p.pauseVideo(); } else if (v) { v.pause(); }')

    def resume(self) -> None:
        self._run('if (p && p.playVideo) { p.playVideo(); } else if (v) { v.play(); }')

    def stop(self) -> None:
        self._timer.stop()
        self._track = None
        self._blank()
        self._set_state(STATE_STOPPED)

    def seek(self, position_ms: int) -> None:
        seconds = max(0, int(position_ms)) / 1000
        self._run(f'if (p && p.seekTo) {{ p.seekTo({seconds}, true); }}'
                  f' else if (v) {{ v.currentTime = {seconds}; }}')

    def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, int(volume)))
        self._run(f'if (p && p.setVolume) {{ p.setVolume({self._volume}); }}'
                  f' else if (v) {{ v.volume = {self._volume / 100}; }}')

    def shutdown(self) -> None:
        self._timer.stop()
        self._blank()

    def _blank(self) -> None:
        self._live = False
        if self._view is not None:
            self._view.stop()
            self._view.setUrl(QUrl('about:blank'))

    # ---------- внутреннее ----------
    def _run(self, body: str) -> None:
        if self._track is None or self._view is None:
            return
        self._view.page().runJavaScript(_CONTROL_JS % body)

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def _poll(self) -> None:
        if self._busy or self._track is None or self._view is None:
            return
        self._busy = True
        self._view.page().runJavaScript(_POLL_JS, self._on_poll)

    def _on_poll(self, raw) -> None:
        self._busy = False
        if self._track is None:
            return
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return

        error = data.get('e') or ''
        if error and not self._degraded:
            self._fallback(error)
            return
        mode = data.get('m')
        if mode == 'none':
            # Страница ещё грузится. Но если она не оживает совсем, честно говорим
            # об ошибке: иначе плеер вечно висел бы на «Загрузка…».
            self._stall += 1
            if self._stall >= _STALL_LIMIT:
                self._timer.stop()
                self._set_state(STATE_ERROR)
                self.failed.emit('YouTube не открыл плеер')
            return
        self._stall = 0
        if mode == 'api':
            self._live = True

        state = int(data.get('s', -1))
        position = int(float(data.get('t') or 0) * 1000)
        duration = int(float(data.get('d') or 0) * 1000)
        if duration <= 0 and self._track is not None:
            duration = self._track.duration * 1000
        self.position_changed.emit(position, duration)

        if state == _YT_PLAYING:
            self._ended = False
            self._set_state(STATE_PLAYING)
        elif state == _YT_PAUSED:
            self._set_state(STATE_PAUSED)
        elif state == _YT_BUFFERING:
            self._set_state(STATE_BUFFERING)
        elif state == _YT_ENDED and not self._ended:
            self._ended = True
            self._timer.stop()
            self._set_state(STATE_STOPPED)
            self.ended.emit()

    def _fallback(self, error: str) -> None:
        """Встраивание не вышло - открываем обычную страницу ролика.

        Там ролик играет всегда, а управлять им можно через сам элемент <video>.
        Очередь при этом не рвётся: следующий трек включится как обычно. Но
        состояние оттуда приходит куцее, и наружу это видно как `limited`."""
        track = self._track
        if track is None:
            return
        logger.info('YouTube: встроенный плеер не пошёл (%s), открываю страницу ролика', error)
        self._degraded = True
        self._live = False
        self._stall = 0
        self._set_state(STATE_LOADING)
        url = track.url or f'https://www.youtube.com/watch?v={track.youtube_id}'
        self._ensure_view().load(QUrl(url))
