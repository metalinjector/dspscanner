"""DSP Scanner — пакет приложения.

Структура:
    app.config       — константы и датаклассы конфигурации/результатов
    app.logging_utils— настройка логирования (файл + GUI)
    app.readers      — модули чтения docx/doc/txt/pdf
    app.scanning     — поиск файлов и текстовый поисковый движок, оркестратор
    app.fileops      — копирование, безопасное удаление/перемещение файлов
    app.export       — экспорт в Excel/CSV/Markdown
    app.gui          — графический интерфейс на PySide6
"""

__version__ = "2.9.9"
