import os
import shutil
from decimal import Decimal, DecimalException

from nova_transcriber.errors import MediaError


def get_max_file_size_mb() -> Decimal:
    try:
        value = Decimal(os.environ.get("MAX_FILE_SIZE_MB", "1024"))
        if not value.is_finite() or value <= 0:
            raise ValueError
        value * 1_048_576
    except (DecimalException, ValueError):
        raise ValueError("MAX_FILE_SIZE_MB должен быть положительным конечным числом (в МиБ)") from None
    return value


def get_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if value <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} должен быть положительным целым числом") from None
    return value


def check_disk_space(path, required_bytes: int = 0) -> None:
    reserve = get_positive_int("MIN_FREE_DISK_MB", 512) * 1_048_576
    if shutil.disk_usage(path).free < reserve + required_bytes:
        raise MediaError("Недостаточно свободного места на диске")


def get_owner_id(name="TELEGRAM_ALLOWED_USER_ID", default=None) -> int:
    try:
        value = int(os.environ.get(name, default))
        if not 0 < value <= 9223372036854775807:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{name} должен быть положительным целым числом в диапазоне BIGINT") from None
    return value
