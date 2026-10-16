# Tesseract: установка и единый конвейер Fast / Standard / Best

Проверено 2026-10-14 в Linux x86_64, Python 3.11.2, 2 логических CPU.

## Что установлено

В локальной папке проекта `Tesseract-OCR/` собраны исходники официальных GitHub-репозиториев:

- **Tesseract 5.5.3**: `Tesseract-OCR/bin/tesseract`;
- Leptonica 1.87.0, libpng 1.6.50, zlib 1.3.1, libjpeg-turbo 3.1.2;
- настоящие `rus.traineddata` и `eng.traineddata` **для каждого из трёх режимов**.

```text
Tesseract-OCR/
  bin/tesseract               # Linux; Windows использует свой tesseract.exe
  lib/                       # зависимости локальной сборки
  tessdata-fast/{rus,eng}.traineddata
  tessdata-medium/{rus,eng}.traineddata
  tessdata-best/{rus,eng}.traineddata
```

Каталог уже включён в `.gitignore`: бинарники, модели (~78 MiB) и результаты сборки не добавляются в Git. Для воспроизведения в Git сохранены скрипт установки и [manifest](../tools/ocr/manifest.json) с версиями исходников, commit ID моделей и SHA-256 каждого архива/модели. Установка выполняется без прав администратора и без изменения системного Tesseract.

**Приложение автоматически находит локальный бинарь** — проверено отдельными CLI-запусками без `--tesseract`.

## Один конвейер, три реальных набора моделей

| Выбор | CLI/JSON | Источник моделей | Особенности |
|---|---|---|---|
| Быстрая | `fast` | `tesseract-ocr/tessdata_fast` | Компактные integer LSTM, основной выбор для массового сканирования |
| Стандартная | `medium` | `tesseract-ocr/tessdata` | Integer LSTM + legacy-компоненты в файлах; приложение использует LSTM |
| Максимальная точность | `best` | `tesseract-ocr/tessdata_best` | Float LSTM, обычно медленнее; преимущество по точности проверять на своём корпусе |

`medium` сохранён как ключ ради совместимости JSON и CLI. Это **не** обозначение стандартной float-модели: upstream `tessdata` содержит integerized LSTM и legacy-компоненты.

Для всех режимов используется **`--oem 1` (LSTM)**. Fast и Best не поддерживают legacy OEM 0/2; явный OEM 1 устраняет зависимость от автоматического выбора движка. Не нужно менять сам бинарь при переключении модели.


Язык независим от модели:

- **Только русский** / `--russian-only`: `rus`. Английские термины могут быть пропущены — на тестовом смешанном скане `contract` действительно не распознан как английское слово.
- **Русский + английский** / `--no-russian-only`: `rus+eng`. Выбирать для смешанных документов и английских поисковых терминов.

Если нужной модели нет (`rus`, а в режиме `rus+eng` — любой из двух), OCR не выполняется и выводится ошибка «Отсутствуют модели OCR»: язык молча не подменяется. Если каталога выбранной градации нет, используются модели из `TESSDATA_PREFIX`, папки `tessdata` рядом с Tesseract или системные. Установщик проверяет каждый запрошенный набор строго, без подмены.

## Повторить установку

### Linux: сборка из GitHub

Нужны Python 3.10+, C/C++ компиляторы и make. CMake можно установить через pip в виртуальное окружение:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements_cli.txt 'cmake>=3.22,<4'
.venv/bin/python tools/install_tesseract.py --build --jobs 2
.venv/bin/python tools/install_tesseract.py --verify-only
```

Скрипт загружает официальные архивы и модели, проверяет SHA-256 **до** замены файла, отвергает небезопасные tar-пути/ссылки. Незавершённая или неверная загрузка не уничтожает установленную модель. Повторная установка не скачивает уже проверенные файлы. `--verify-only` работает без сети и выводит версию бинаря, каталоги, языки и хеши.

Локальная сборка поддерживает PNG/JPEG/PNM — этого достаточно для всех PDF, которые приложение предварительно рендерит через PyMuPDF. Дополнительные декодеры TIFF/GIF/WebP в этой сборке отключены: это не универсальный конвертер любых изображений вне приложения. SIMD-оптимизации Tesseract определяются во время исполнения; `-march=native` не включён. Для другой ОС/архитектуры нужен соответствующий бинарь, а не копия Linux ELF.

### Windows: нативный бинарь и те же модели

1. Проверить доступные файлы [релиза Tesseract 5.5.3](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3). Не предполагать, что официальный релиз обязательно содержит Windows installer: при его отсутствии использовать нативную Windows-сборку из источника, рекомендованного официальной документацией Tesseract, и записать её фактическую версию. Установить в доступную для записи папку `Tesseract-OCR` рядом с DSP Scanner. Сохранить все DLL комплекта, не копировать один EXE отдельно.
2. В корне проекта выполнить:

```powershell
py tools/install_tesseract.py --root .\Tesseract-OCR
py tools/install_tesseract.py --root .\Tesseract-OCR --verify-only
```

3. Если бинарь находится в другой папке, указать её как `--root` и затем выбрать её `tesseract.exe` в настройках DSP Scanner.

Скачивание/проверка моделей — кроссплатформенный Python-код. Автоматическая **сборка** `--build` поддержана только на Linux; установка/запуск Windows EXE здесь не выполнялись. Windows-модели и native binary используют тот же протокол OCR, но выпуск Windows необходимо проверить на Windows. На macOS также нужен собственный установленный бинарь и models-only запуск; macOS не тестировалась.

### Только нужные модели

По умолчанию устанавливаются все три набора и оба языка. Для экономии места:

```bash
python tools/install_tesseract.py --tiers fast --languages rus
```

Такой выбор не включает незагруженные режимы автоматически. Для проверки сокращённой установки использовать те же `--tiers` и `--languages` вместе с `--verify-only`.

## Использование

В GUI: **Параметры → Модель OCR**, затем выбор русского или двух языков. Fast остаётся режимом по умолчанию.

```bash
# Fast, только русский
python main.py --cli --no-config --path /path/to/documents --types pdf \
  --content-terms 'договор' --ocr-model-tier fast --russian-only \
  --format csv --output fast-report.csv

# Стандартные модели, смешанные документы
python main.py --cli --no-config --path /path/to/documents --types pdf \
  --content-terms 'договор,contract' --ocr-model-tier medium --no-russian-only \
  --format csv --output standard-report.csv

# Best; язык выбирается независимо
python main.py --cli --no-config --path /path/to/documents --types pdf \
  --content-terms 'договор' --ocr-model-tier best --russian-only \
  --format csv --output best-report.csv
```

`--workers` управляет файловыми потоками; OCR дополнительно ограничивается CPU/внутренним лимитом. Не выставлять 128 в надежде ускорить один PDF. Общий таймаут файла по-прежнему 60 секунд по умолчанию; для диагностики длинных сканов можно временно использовать `--timeout 300`, но это не ускоряет OCR.

## Источники

- [Tesseract 5.5.3](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3)
- [Leptonica 1.87.0](https://github.com/DanBloomberg/leptonica/releases/tag/1.87.0)
- [tessdata_fast: LSTM integer, OEM 1](https://github.com/tesseract-ocr/tessdata_fast)
- [tessdata: integerized LSTM + legacy](https://github.com/tesseract-ocr/tessdata)
- [tessdata_best](https://github.com/tesseract-ocr/tessdata_best)

Бинарь Tesseract и модели имеют Apache-2.0 лицензию; при распространении комплекта учитывать также лицензии Leptonica, zlib, libpng и libjpeg-turbo, доступные в соответствующих исходниках. В Git проекта сохраняются только manifest и установщик, не переупакованный дистрибутив.
