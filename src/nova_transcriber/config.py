import os
from decimal import Decimal, DecimalException


def get_max_file_size_mb() -> Decimal:
    try:
        value = Decimal(os.environ.get("MAX_FILE_SIZE_MB", "100"))
        if not value.is_finite() or value <= 0:
            raise ValueError
        value * 1_048_576
    except (DecimalException, ValueError):
        raise ValueError("MAX_FILE_SIZE_MB должен быть положительным конечным числом (в МиБ)") from None
    return value


def get_owner_id() -> int:
    try:
        value = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
        if not 0 < value <= 9223372036854775807:
            raise ValueError
    except (KeyError, ValueError):
        raise ValueError("TELEGRAM_ALLOWED_USER_ID должен быть положительным целым числом в диапазоне BIGINT") from None
    return value
