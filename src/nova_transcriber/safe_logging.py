import logging
import os
import re
import sys
from datetime import timezone

from loguru import logger


def sanitize(value: str) -> str:
    for name, secret in os.environ.items():
        if secret and (any(part in name.upper() for part in (
            "TOKEN", "PASSWORD", "SECRET", "API_KEY", "API_HASH", "DSN", "DATABASE_URL",
            "PRIVATE_KEY", "ACCESS_KEY", "CREDENTIAL", "AUTH",
        )) or name.upper() == "PGPASSFILE"):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", value)
    return re.sub(r"(postgres(?:ql)?://[^:\s/]+:)[^@\s]+@", r"\1[REDACTED]@", value)


def task_log(task_id: int, event: str, message: str, *,
             duration: float | None = None, error: str | None = None) -> None:
    logger.bind(component="Core", task_id=task_id, event=event,
                duration="—" if duration is None else f"{duration:.3f} с",
                error=error or "—").log("ERROR" if error else "INFO", message)


def configure_logging() -> None:
    original_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = original_factory(*args, **kwargs)
        record.msg = sanitize(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = sanitize(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = sanitize(record.stack_info)
        return record

    logging.setLogRecordFactory(record_factory)

    def patch_record(record):
        record["message"] = sanitize(record["message"])
        record["extra"] = {key: sanitize(str(value)) for key, value in record["extra"].items()}
        record["extra"]["level_ru"] = {"INFO": "ИНФО", "ERROR": "ОШИБКА", "WARNING": "ПРЕДУПРЕЖДЕНИЕ"}.get(record["level"].name, record["level"].name)
        record["extra"]["utc_time"] = record["time"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    logger.remove()
    logger.configure(patcher=patch_record, extra={
        "component": "Приложение", "task_id": "—", "event": "message",
        "duration": "—", "error": "—",
    })
    logger.add(sys.stderr, format=(
        "{extra[utc_time]} | {extra[level_ru]} | {extra[component]} | "
        "Задача {extra[task_id]} | {extra[event]} | {message} | "
        "{extra[duration]} | {extra[error]}"
    ), diagnose=False, backtrace=False)

    class LoguruHandler(logging.Handler):
        def emit(self, record):
            logger.bind(component=record.name, event="library_message").log(
                record.levelname, sanitize(record.getMessage())
            )

    logging.basicConfig(handlers=[LoguruHandler()], level=logging.INFO, force=True)
