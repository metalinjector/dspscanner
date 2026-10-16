# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для DSP Scanner — сборка одного exe.

Отслеживается в репозитории (исключение из '*.spec'), чтобы сборка
воспроизводилась из чистого клона. Скрытые импорты сверены со структурой main
(линия 4.7.6.2026, включая app.readers.ole_doc и app.scanning.pdf_cache).
При добавлении новых модулей app.* дополняйте hiddenimports.
"""

BLOCK_CIPHER = None

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
        # Readers
        'app.readers',
        'app.readers.base',
        'app.readers.doc_formats',
        'app.readers.doc_reader',
        'app.readers.docx_reader',
        'app.readers.ole_doc',
        'app.readers.pdf_reader',
        'app.readers.txt_reader',
        # Scanning
        'app.scanning',
        'app.scanning.file_finder',
        'app.scanning.scanner',
        'app.scanning.search_engine',
        'app.scanning.term_parser',
        'app.scanning.pdf_cache',
        # Export
        'app.export',
        'app.export.excel_export',
        'app.export.safety',
        'app.export.text_exports',
        # GUI
        'app.gui',
        'app.gui.main_window',
        'app.gui.results_model',
        'app.gui.settings_dialog',
        'app.gui.email_settings_dialog',
        'app.gui.result_contexts_dialog',
        'app.gui.unreadable_files_dialog',
        'app.gui.terms_field',
        'app.gui.theme',
        'app.gui.workers',
        # File ops
        'app.fileops',
        'app.fileops.file_operations',
        # Other
        'app.config',
        'app.logging_utils',
        'app.reporting',
        'app.settings_store',
        'app.scan_config_store',
        'app.email_settings',
        'app.email_sender',
        'app.default_paths',
        'app.cli',
        # Third-party
        'olefile',
        'striprtf',
        'bs4',
        'lxml',
        'defusedxml',
        'charset_normalizer',
        'pymupdf',
        'openpyxl',
        'python_docx',
    ],
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

pyz = PYZ(a.pure, a.zipped_data, cipher=BLOCK_CIPHER)

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