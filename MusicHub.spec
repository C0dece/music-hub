# -*- mode: python ; coding: utf-8 -*-
"""Сборка переносимой версии: dist/MusicHub.exe - один файл.

Одним файлом, потому что так его и просили: скачал, запустил, никаких папок.
Плата за это - первый запуск: Windows распаковывает содержимое во временную
папку, и с ffmpeg внутри это несколько секунд. Дальше распакованное остаётся
в кэше, и повторные запуски быстрые.

Музыка, настройки и журналы всё равно ложатся рядом с .exe, а не во временную
папку: за это отвечает config.BASE_DIR, и переносимость сохраняется - .exe
можно унести на флешке вместе со всем нажитым.

Сборка: .venv\Scripts\python.exe -m PyInstaller MusicHub.spec --noconfirm
"""
from pathlib import Path

ROOT = Path(SPECPATH)

# ffmpeg берём только сам ffmpeg.exe: ffplay и ffprobe рядом с ним не нужны
# никому - теги читает mutagen, а проигрываем мы средствами Qt (≈280 МБ мимо)
resources = [
    (str(ROOT / 'assets'), 'assets'),
    (str(ROOT / 'app' / 'ui' / 'styles.qss'), 'app/ui'),
]
for src, dest in ((ROOT / 'ffmpeg' / 'bin' / 'ffmpeg.exe', 'ffmpeg/bin'),
                  (ROOT / 'runtime' / 'qjs.exe', 'runtime')):
    if src.exists():
        resources.append((str(src), dest))

a = Analysis(
    ['run_app.py'],
    pathex=[str(ROOT)],
    datas=resources,
    hiddenimports=['yt_dlp_ejs'],
    excludes=['tkinter', 'unittest', 'pytest', 'PyInstaller'],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Всё в одном файле: binaries и datas уезжают в сам .exe, COLLECT не нужен
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='MusicHub',
    icon=str(ROOT / 'assets' / 'icon.ico'),
    console=False,          # обычное оконное приложение, чёрное окно ни к чему
    upx=False,              # сжатие ломает Qt-библиотеки и злит антивирусы
)
