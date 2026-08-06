"""Диалог настройки SMTP и автоматической отправки отчётов."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.email_sender import send_test_email
from app.email_settings import EMAIL_PASSWORD_ENV, EmailSettings


class EmailSettingsDialog(QDialog):
    """Редактирует переносимую секцию ``email`` в search_settings.json."""

    def __init__(self, settings: EmailSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Почта и автоматическая отправка отчётов")
        self.resize(760, 720)
        self.setMinimumSize(680, 560)
        self._initial = settings.sanitized()
        self.result_settings = self._initial

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)
        content_layout.setSpacing(14)

        automation_box = QGroupBox("Автоматическая отправка")
        automation_layout = QVBoxLayout(automation_box)
        automation_layout.setContentsMargins(18, 36, 18, 14)
        self.auto_send = QCheckBox("Автоматически отправлять отчёт после успешного сканирования")
        self.auto_send.setChecked(self._initial.auto_send)
        self.send_without_matches = QCheckBox("Отправлять отчёт, даже если совпадений не найдено")
        self.send_without_matches.setChecked(self._initial.send_when_no_matches)
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("Формат вложения:"))
        self.report_format = QComboBox()
        self.report_format.addItem("Excel (*.xlsx)", "xlsx")
        self.report_format.addItem("CSV (*.csv)", "csv")
        self.report_format.addItem("Markdown (*.md)", "md")
        index = self.report_format.findData(self._initial.report_format)
        self.report_format.setCurrentIndex(max(0, index))
        format_row.addWidget(self.report_format)
        format_row.addStretch(1)
        automation_layout.addWidget(self.auto_send)
        automation_layout.addWidget(self.send_without_matches)
        automation_layout.addLayout(format_row)
        content_layout.addWidget(automation_box)

        smtp_box = QGroupBox("SMTP-сервер")
        smtp_layout = QFormLayout(smtp_box)
        smtp_layout.setContentsMargins(18, 38, 18, 16)
        smtp_layout.setVerticalSpacing(10)

        self.smtp_host = QLineEdit(self._initial.smtp_host)
        self.smtp_host.setPlaceholderText("smtp.example.com")
        self.smtp_port = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(self._initial.smtp_port)
        self.security = QComboBox()
        self.security.addItem("STARTTLS (обычно порт 587)", "starttls")
        self.security.addItem("SSL/TLS с момента подключения (обычно порт 465)", "ssl")
        self.security.addItem("Без шифрования", "none")
        index = self.security.findData(self._initial.security)
        self.security.setCurrentIndex(max(0, index))
        self.security.currentIndexChanged.connect(self._suggest_port)

        self.username = QLineEdit(self._initial.username)
        self.password = QLineEdit(self._initial.password)
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText(
            f"пароль из JSON или переменной {EMAIL_PASSWORD_ENV}"
        )
        self.show_password = QCheckBox("Показать")
        self.show_password.toggled.connect(
            lambda checked: self.password.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        password_row.addWidget(self.show_password)

        self.save_password = QCheckBox("Сохранять пароль в переносимом JSON открытым текстом")
        self.save_password.setChecked(self._initial.save_password)
        self.save_password.setToolTip(
            "Для автоматического серверного запуска пароль можно не сохранять, "
            f"а задать в переменной окружения {EMAIL_PASSWORD_ENV}."
        )
        warning = QLabel(
            f"Безопаснее оставить пароль пустым и задать системную переменную "
            f"<b>{EMAIL_PASSWORD_ENV}</b>."
        )
        warning.setWordWrap(True)
        warning.setTextFormat(Qt.RichText)
        warning.setStyleSheet("color: #F2B84B;")

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setValue(self._initial.timeout_seconds)
        self.timeout.setSuffix(" с")

        smtp_layout.addRow("SMTP-сервер:", self.smtp_host)
        smtp_layout.addRow("Порт:", self.smtp_port)
        smtp_layout.addRow("Защита соединения:", self.security)
        smtp_layout.addRow("Имя пользователя:", self.username)
        smtp_layout.addRow("Пароль:", password_row)
        smtp_layout.addRow("", self.save_password)
        smtp_layout.addRow("", warning)
        smtp_layout.addRow("Таймаут:", self.timeout)
        content_layout.addWidget(smtp_box)

        address_box = QGroupBox("Отправитель и получатели")
        address_layout = QFormLayout(address_box)
        address_layout.setContentsMargins(18, 38, 18, 16)
        address_layout.setVerticalSpacing(10)
        self.from_address = QLineEdit(self._initial.from_address)
        self.to_addresses = QLineEdit(self._initial.to_addresses)
        self.cc_addresses = QLineEdit(self._initial.cc_addresses)
        self.bcc_addresses = QLineEdit(self._initial.bcc_addresses)
        for widget in (self.to_addresses, self.cc_addresses, self.bcc_addresses):
            widget.setPlaceholderText("Адреса через запятую или точку с запятой")
        address_layout.addRow("Отправитель (From):", self.from_address)
        address_layout.addRow("Получатели (To):", self.to_addresses)
        address_layout.addRow("Копия (Cc):", self.cc_addresses)
        address_layout.addRow("Скрытая копия (Bcc):", self.bcc_addresses)
        content_layout.addWidget(address_box)

        templates_box = QGroupBox("Тема и текст письма")
        templates_layout = QFormLayout(templates_box)
        templates_layout.setContentsMargins(18, 38, 18, 16)
        self.subject = QLineEdit(self._initial.subject_template)
        self.body = QTextEdit()
        self.body.setPlainText(self._initial.body_template)
        self.body.setMinimumHeight(125)
        templates_layout.addRow("Тема:", self.subject)
        templates_layout.addRow("Текст:", self.body)
        placeholders = QLabel(
            "Доступные подстановки: {date}, {matches}, {processed}, {errors}, "
            "{elapsed}, {host}, {status}, {report_format}."
        )
        placeholders.setWordWrap(True)
        placeholders.setStyleSheet("color: #9AA3B2;")
        templates_layout.addRow("", placeholders)
        content_layout.addWidget(templates_box)

        content_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 6, 0, 3)
        controls.setSpacing(8)
        test_button = QPushButton("Отправить тестовое письмо")
        test_button.clicked.connect(self._send_test)
        controls.addWidget(test_button)
        controls.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        controls.addWidget(buttons)
        root.addLayout(controls)

    def _suggest_port(self) -> None:
        security = self.security.currentData()
        current = self.smtp_port.value()
        if security == "ssl" and current in {25, 587}:
            self.smtp_port.setValue(465)
        elif security == "starttls" and current in {25, 465}:
            self.smtp_port.setValue(587)

    def _collect(self) -> EmailSettings:
        return EmailSettings(
            auto_send=self.auto_send.isChecked(),
            send_when_no_matches=self.send_without_matches.isChecked(),
            smtp_host=self.smtp_host.text(),
            smtp_port=self.smtp_port.value(),
            security=str(self.security.currentData()),
            username=self.username.text(),
            password=self.password.text(),
            save_password=self.save_password.isChecked(),
            from_address=self.from_address.text(),
            to_addresses=self.to_addresses.text(),
            cc_addresses=self.cc_addresses.text(),
            bcc_addresses=self.bcc_addresses.text(),
            subject_template=self.subject.text(),
            body_template=self.body.toPlainText(),
            report_format=str(self.report_format.currentData()),
            timeout_seconds=self.timeout.value(),
        ).sanitized()

    def _send_test(self) -> None:
        settings = self._collect()
        errors = settings.validation_errors(require_delivery=True)
        if errors:
            QMessageBox.warning(self, "Настройки почты", "\n".join(errors))
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = send_test_email(settings)
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.critical(self, "Ошибка SMTP", str(exc))
        else:
            QMessageBox.information(
                self,
                "Тестовое письмо отправлено",
                "Получатели:\n" + "\n".join(result.recipients),
            )
        finally:
            QApplication.restoreOverrideCursor()

    def _accept(self) -> None:
        settings = self._collect()
        if settings.auto_send:
            errors = settings.validation_errors(require_delivery=True)
            if errors:
                QMessageBox.warning(self, "Настройки почты", "\n".join(errors))
                return
        if settings.save_password and settings.password:
            answer = QMessageBox.question(
                self,
                "Сохранение пароля",
                "Пароль будет записан в переносимый JSON открытым текстом. Продолжить?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.result_settings = settings
        self.accept()
