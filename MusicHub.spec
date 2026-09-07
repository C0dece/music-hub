# -*- mode: python ; coding: utf-8 -*-
"""Сборка переносимой версии: dist/MusicHub/MusicHub.exe.

Папкой, а не одним файлом: onefile каждый запуск распаковывал бы во временную
папку полторы сотни мегабайт ffmpeg — это секунды ожидания на пустом месте.
Папку достаточно распаковать один раз; музыка, настройки и журналы ложатся
рядом с .exe, поэтому её можно унести на флешке вместе со всем нажитым.

Сборка: .venv\Scripts\python.exe -m PyInstaller MusicHub.spec --noconfirm
"""
from pathlib import Path

ROOT = Path(SPECPATH)

# ffmpeg берём только сам ffmpeg.exe: ffplay и ffprobe рядом с ним не нужны
# никому — теги читает mutagen, а проигрываем мы средствами Qt (≈280 МБ мимо)
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

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='MusicHub',
    icon=str(ROOT / 'assets' / 'icon.ico'),
    console=False,          # обычное оконное приложение, чёрное окно ни к чему
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name='MusicHub')
