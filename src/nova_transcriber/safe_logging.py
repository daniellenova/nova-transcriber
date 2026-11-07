import logging
import os
import re


def sanitize(value: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        value = value.replace(token, "[REDACTED]")
    return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", value)


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
