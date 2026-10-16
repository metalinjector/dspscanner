@echo off
rem DSP Scanner 4.7.0.0 launcher (fix/pdf-ocr-reliability, commit 59be922)
rem Env: Python 3.12 venv (.venv), portable Tesseract from the app folder.
rem Does NOT touch %USERPROFILE%\DSPScanner\settings.json - all overrides
rem are passed via DSP_SCANNER_* environment variables.

set "APP_DIR=%~dp0"

rem Tesseract: portable bundle with tessdata-fast/medium/best tiers
set "DSP_SCANNER_TESSERACT_PATH=%APP_DIR%Tesseract-OCR\tesseract.exe"

rem OCR defaults: fast model, auto worker count (0).
rem DSP_SCANNER_OCR_QUALITY is NOT set here on purpose: the quality mode
rem (adaptive/thorough/fast150) comes from the user's settings.json.
rem Set it in this file only to force a mode for every launch.

rem Russian-only for Russian documents (use 0 for mixed rus+eng)
set "DSP_SCANNER_RUSSIAN_ONLY=1"

rem Launch GUI
"%APP_DIR%.venv\Scripts\python.exe" "%APP_DIR%main.py" %*