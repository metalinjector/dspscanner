@echo off
rem DSP Scanner 4.7.0.0 launcher (fix/pdf-ocr-reliability, commit 59be922)
rem Env: Python 3.12 venv (.venv), portable Tesseract from the app folder.
rem Does NOT touch %USERPROFILE%\DSPScanner\settings.json - all overrides
rem are passed via DSP_SCANNER_* environment variables.

set "APP_DIR=%~dp0"

rem Tesseract: portable bundle with tessdata-fast/medium/best tiers
set "DSP_SCANNER_TESSERACT_PATH=%APP_DIR%Tesseract-OCR\tesseract.exe"

rem OCR defaults: fast model, adaptive quality, auto worker count (0)
set "DSP_SCANNER_OCR_MODEL_TIER=fast"
set "DSP_SCANNER_OCR_QUALITY=adaptive"
set "DSP_SCANNER_OCR_WORKERS=0"

rem Russian-only for Russian documents (use 0 for mixed rus+eng)
set "DSP_SCANNER_RUSSIAN_ONLY=1"

rem Launch GUI
"%APP_DIR%.venv\Scripts\python.exe" "%APP_DIR%main.py" %*