"""Модель и валидация настроек электронной почты DSP Scanner.

Настройки сохраняются внутри переносимого ``search_settings.json`` в секции
``email``. Пароль записывается только при явно включённом ``save_password``;
для серверного запуска предпочтительнее переменная окружения
``DSP_SCANNER_SMTP_PASSWORD``.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, replace
from email.utils import getaddresses
from typing import Any, Mapping

from app.scan_config_store import load_scan_config

EMAIL_PASSWORD_ENV = "DSP_SCANNER_SMTP_PASSWORD"
_ALLOWED_SECURITY = {"starttls", "ssl", "none"}
_ALLOWED_FORMATS = {"xlsx", "csv", "md"}
_HEADER_NEWLINE_RE = re.compile(r"[\r\n]+")


@dataclass
class EmailSettings:
    """Параметры SMTP и автоматической отправки отчётов."""

    auto_send: bool = False
    send_when_no_matches: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str = ""
    save_password: bool = False
    from_address: str = ""
    to_addresses: str = ""
    cc_addresses: str = ""
    bcc_addresses: str = ""
    subject_template: str = "DSP Scanner: {matches} совпадений — {date}"
    body_template: str = (
        "Сканирование завершено.\n\n"
        "Обработано файлов: {processed}\n"
        "Найдено совпадений: {matches}\n"
        "Ошибок и предупреждений: {errors}\n"
        "Время выполнения: {elapsed} с\n"
        "Компьютер: {host}\n"
    )
    report_format: str = "xlsx"
    timeout_seconds: int = 30

    def sanitized(self) -> "EmailSettings":
        def clean_text(value: Any, max_length: int = 10_000) -> str:
            try:
                text = str(value or "").strip()
            except Exception:
                return ""
            return text[:max_length]

        try:
            port = int(self.smtp_port)
        except (TypeError, ValueError, OverflowError):
            port = 587
        port = min(65535, max(1, port))

        try:
            timeout = int(self.timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            timeout = 30
        timeout = min(300, max(5, timeout))

        security = clean_text(self.security, 32).lower()
        if security not in _ALLOWED_SECURITY:
            security = "starttls"
        report_format = clean_text(self.report_format, 16).lower().lstrip(".")
        if report_format not in _ALLOWED_FORMATS:
            report_format = "xlsx"

        return EmailSettings(
            auto_send=bool(self.auto_send),
            send_when_no_matches=bool(self.send_when_no_matches),
            smtp_host=clean_text(self.smtp_host, 512),
            smtp_port=port,
            security=security,
            username=clean_text(self.username, 512),
            password=clean_text(self.password, 4096),
            save_password=bool(self.save_password),
            from_address=clean_text(self.from_address, 512),
            to_addresses=clean_text(self.to_addresses, 10_000),
            cc_addresses=clean_text(self.cc_addresses, 10_000),
            bcc_addresses=clean_text(self.bcc_addresses, 10_000),
            subject_template=clean_text(self.subject_template, 998)
            or EmailSettings.subject_template,
            body_template=clean_text(self.body_template, 100_000)
            or EmailSettings.body_template,
            report_format=report_format,
            timeout_seconds=timeout,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "EmailSettings":
        if not isinstance(value, Mapping):
            return cls()
        allowed = cls.__dataclass_fields__
        return cls(**{key: val for key, val in value.items() if key in allowed}).sanitized()

    def to_mapping(self) -> dict[str, Any]:
        """Возвращает JSON-совместимое представление.

        Пароль намеренно исключается, если пользователь не разрешил его
        сохранение. Это предотвращает случайное попадание секрета в переносимый
        JSON при обычной работе GUI.
        """
        value = asdict(self.sanitized())
        if not value.get("save_password"):
            value["password"] = ""
        return value

    def effective_password(self, env: Mapping[str, str] | None = None) -> str:
        source = os.environ if env is None else env
        return str(source.get(EMAIL_PASSWORD_ENV, "") or self.password or "")

    def recipients(self) -> tuple[list[str], list[str], list[str]]:
        return (
            parse_address_list(self.to_addresses),
            parse_address_list(self.cc_addresses),
            parse_address_list(self.bcc_addresses),
        )

    def validation_errors(self, *, require_delivery: bool = True) -> list[str]:
        settings = self.sanitized()
        errors: list[str] = []
        if not settings.smtp_host:
            errors.append("Не указан SMTP-сервер.")
        if not 1 <= settings.smtp_port <= 65535:
            errors.append("Порт SMTP должен быть от 1 до 65535.")
        if settings.security not in _ALLOWED_SECURITY:
            errors.append("Неизвестный режим защиты SMTP.")
        if require_delivery:
            sender_addresses = parse_address_list(settings.from_address)
            if not settings.from_address:
                errors.append("Не указан адрес отправителя.")
            elif len(sender_addresses) != 1:
                errors.append("В поле отправителя должен быть ровно один email-адрес.")
            to, cc, bcc = settings.recipients()
            if not (to or cc or bcc):
                errors.append("Не указан ни один получатель.")
            invalid = invalid_addresses(
                settings.from_address,
                settings.to_addresses,
                settings.cc_addresses,
                settings.bcc_addresses,
            )
            if invalid:
                errors.append("Некорректные email-адреса: " + ", ".join(invalid))
        if settings.username and not settings.effective_password():
            errors.append(
                "Указано имя пользователя SMTP, но пароль отсутствует. "
                f"Сохраните пароль или задайте переменную {EMAIL_PASSWORD_ENV}."
            )
        return errors


def parse_address_list(value: str) -> list[str]:
    """Разбирает адреса, разделённые запятыми, точками с запятой или строками."""
    normalized = str(value or "").replace(";", ",").replace("\n", ",")
    addresses: list[str] = []
    seen: set[str] = set()
    for _display_name, address in getaddresses([normalized]):
        address = address.strip()
        key = address.casefold()
        if address and key not in seen:
            addresses.append(address)
            seen.add(key)
    return addresses


def invalid_addresses(*values: str) -> list[str]:
    invalid: list[str] = []
    for value in values:
        normalized = str(value or "").replace(";", ",").replace("\n", ",")
        for _display_name, address in getaddresses([normalized]):
            address = address.strip()
            if not address:
                continue
            # Практичная, намеренно не чрезмерно строгая проверка. Полную
            # валидацию всё равно выполняет SMTP-сервер.
            if (
                "@" not in address
                or address.startswith("@")
                or address.endswith("@")
                or any(char.isspace() for char in address)
                or "\r" in address
                or "\n" in address
            ):
                invalid.append(address)
    return invalid


def safe_header(value: str, fallback: str = "") -> str:
    """Удаляет CR/LF, исключая SMTP header injection."""
    cleaned = _HEADER_NEWLINE_RE.sub(" ", str(value or "")).strip()
    return cleaned or fallback


def load_email_settings(config: Mapping[str, Any] | None = None) -> EmailSettings:
    if config is None:
        try:
            config = load_scan_config() or {}
        except (OSError, ValueError, TypeError):
            config = {}
    section = config.get("email") if isinstance(config, Mapping) else None
    return EmailSettings.from_mapping(section if isinstance(section, Mapping) else None)


def with_password(settings: EmailSettings, password: str) -> EmailSettings:
    """Возвращает копию с временным паролем без изменения флага сохранения."""
    return replace(settings, password=str(password or ""))
