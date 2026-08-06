"""Безопасная отправка отчётов DSP Scanner через SMTP."""
from __future__ import annotations

import mimetypes
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from string import Formatter
from typing import Mapping, Sequence

from app.email_settings import EmailSettings, parse_address_list, safe_header


@dataclass(frozen=True)
class EmailSendResult:
    recipients: tuple[str, ...]
    attachments: tuple[str, ...]
    message_id: str = ""


class _SafeTemplateDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, values: Mapping[str, object]) -> str:
    """Подставляет только именованные поля и не падает на неизвестных ключах."""
    # Ограничиваем потенциально патологические шаблоны из повреждённого JSON.
    text = str(template or "")[:100_000]
    # Formatter.parse выявляет синтаксически некорректные фигурные скобки.
    try:
        list(Formatter().parse(text))
        return text.format_map(_SafeTemplateDict({key: str(value) for key, value in values.items()}))
    except (ValueError, KeyError, IndexError):
        return text


def _add_attachment(message: EmailMessage, file_path: Path) -> None:
    if not file_path.is_file():
        raise FileNotFoundError(f"Файл вложения не найден: {file_path}")
    mime_type, encoding = mimetypes.guess_type(file_path.name)
    if mime_type is None or encoding is not None:
        maintype, subtype = "application", "octet-stream"
    else:
        maintype, subtype = mime_type.split("/", 1)
    # Отчёты невелики; чтение целиком упрощает корректное MIME-кодирование.
    data = file_path.read_bytes()
    message.add_attachment(data, maintype=maintype, subtype=subtype, filename=file_path.name)


def build_email_message(
    settings: EmailSettings,
    *,
    template_values: Mapping[str, object],
    attachments: Sequence[Path | str] = (),
) -> tuple[EmailMessage, list[str]]:
    settings = settings.sanitized()
    errors = settings.validation_errors(require_delivery=True)
    if errors:
        raise ValueError(" ".join(errors))

    to_addresses, cc_addresses, bcc_addresses = settings.recipients()
    envelope_recipients = [*to_addresses, *cc_addresses, *bcc_addresses]

    message = EmailMessage()
    message["From"] = safe_header(settings.from_address)
    if to_addresses:
        message["To"] = ", ".join(to_addresses)
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    message["Subject"] = safe_header(
        render_template(settings.subject_template, template_values),
        "Отчёт DSP Scanner",
    )
    body = render_template(settings.body_template, template_values)
    message.set_content(body or "Отчёт DSP Scanner во вложении.")

    for attachment in attachments:
        _add_attachment(message, Path(attachment))
    return message, envelope_recipients


def _open_smtp(settings: EmailSettings):
    timeout = settings.timeout_seconds
    if settings.security == "ssl":
        return smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )

    client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    client.ehlo()
    if settings.security == "starttls":
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
    return client


def send_email(
    settings: EmailSettings,
    *,
    template_values: Mapping[str, object],
    attachments: Sequence[Path | str] = (),
) -> EmailSendResult:
    """Формирует и отправляет письмо, не записывая пароль в журналы."""
    settings = settings.sanitized()
    message, recipients = build_email_message(
        settings,
        template_values=template_values,
        attachments=attachments,
    )
    password = settings.effective_password()

    try:
        with _open_smtp(settings) as client:
            if settings.username:
                client.login(settings.username, password)
            sender = parse_address_list(settings.from_address)[0]
            client.send_message(
                message,
                from_addr=sender,
                to_addrs=recipients,
            )
    except (smtplib.SMTPException, OSError, socket.timeout) as exc:
        raise RuntimeError(f"Ошибка отправки SMTP: {exc}") from exc

    return EmailSendResult(
        recipients=tuple(recipients),
        attachments=tuple(str(Path(item)) for item in attachments),
        message_id=str(message.get("Message-ID", "")),
    )


def send_test_email(settings: EmailSettings) -> EmailSendResult:
    values = {
        "date": "тест",
        "matches": 0,
        "processed": 0,
        "errors": 0,
        "elapsed": "0.0",
        "host": socket.gethostname(),
        "status": "тестовое письмо",
        "report_format": settings.report_format,
    }
    return send_email(settings, template_values=values, attachments=())
