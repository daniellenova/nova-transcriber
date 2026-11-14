import logging
import os
import re
import sys
import traceback
from zoneinfo import ZoneInfo

from loguru import logger


def sanitize(value: str) -> str:
    secrets = {
        secret for name, secret in os.environ.items()
        if secret and (any(part in name.upper() for part in (
            "TOKEN", "PASSWORD", "SECRET", "API_KEY", "API_HASH", "DSN", "DATABASE_URL",
            "PRIVATE_KEY", "ACCESS_KEY", "CREDENTIAL", "AUTH",
        )) or name.upper() == "PGPASSFILE")
    }
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", value)
    return re.sub(r"(postgres(?:ql)?://[^:\s/]+:)[^@\s]+@", r"\1[REDACTED]@", value)


def configure_logging() -> None:
    log_timezone = ZoneInfo(os.environ.get("DISPLAY_TIMEZONE") or "Europe/Moscow")

    def patch_record(record):
        record["message"] = sanitize(record["message"])
        record["extra"] = {key: sanitize(str(value)) for key, value in record["extra"].items()}
        record["extra"]["level_ru"] = {"INFO": "ИНФО", "ERROR": "ОШИБКА", "WARNING": "ПРЕДУПРЕЖДЕНИЕ"}.get(record["level"].name, record["level"].name)
        record["extra"]["local_time"] = (
            record["time"].astimezone(log_timezone).strftime("%Y-%m-%d %H:%M:%S")
            + f" {log_timezone.key}"
        )

    logger.remove()
    logger.configure(patcher=patch_record, extra={
        "component": "Приложение", "task_id": "—", "event": "message",
        "duration": "—", "error": "—",
    })
    logger.add(sys.stderr, format=(
        "{extra[local_time]} | {extra[level_ru]} | {extra[component]} | "
        "Задача {extra[task_id]} | {extra[event]} | {message} | "
        "{extra[duration]} | {extra[error]}"
    ), diagnose=False, backtrace=False)

    class LoguruHandler(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + "".join(traceback.format_exception(*record.exc_info))
            logger.bind(component=record.name, event="library_message").log(
                record.levelname, message
            )

    logging.basicConfig(handlers=[LoguruHandler(level=logging.WARNING)], level=logging.WARNING, force=True)
