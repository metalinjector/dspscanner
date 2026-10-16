# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для DSP Scanner — сборка одного exe.

Отслеживается в репозитории (исключение из '*.spec'), чтобы сборка
воспроизводилась из чистого клона. Сборка:

    python -m PyInstaller --clean --noconfirm \
        --upx-dir tools/upx/upx-4.2.4-win64 build_exe.spec

Каталог UPX сам не ищется: PyInstaller берёт его только из PATH или
--upx-dir. Без UPX сборка тоже проходит, exe просто больше.
"""

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/dsp_scanner.ico', 'assets'),
        ('assets/dsp_scanner.png', 'assets'),
        ('assets/dsp_scanner.svg', 'assets'),
    ],
    hiddenimports=[
        # PySide6/Qt
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtSvg',
        # Third-party
        'olefile',
        'striprtf',
        'bs4',
        'lxml',
        'defusedxml',
        'charset_normalizer',
        'pymupdf',
        'openpyxl',
        'docx',
    ] + collect_submodules('app'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'numpy',
        'pandas',
        'PIL',
        'cv2',
        'notebook',
        'jupyter',
        'IPython',
        'setuptools',
        'pip',
        'test',
        'unittest',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DSP_Scanner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # Без консоли — GUI-приложение
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/dsp_scanner.ico',
)