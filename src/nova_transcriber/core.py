import math
import os
from pathlib import Path

from loguru import logger

from nova_transcriber import database
from nova_transcriber.config import get_max_file_size_mb
from nova_transcriber.errors import EmptyFileError, UnsupportedFormatError


SUPPORTED_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".aiff", ".aif", ".wma", ".mp4", ".m4v", ".mov", ".mkv", ".webm",
    ".avi", ".mpeg", ".mpg", ".ts", ".3gp", ".3g2", ".amr",
}


def api_record(record):
    fields = ("id", "filename", "source_type", "file_size", "duration", "model",
              "created_at", "started_at", "finished_at", "processing_time", "error_reason", "status")
    return {key: record[key] for key in fields}


def create_api_task(data, user_id):
    if not isinstance(data, dict):
        raise ValueError("Ожидается объект")
    filename = data.get("filename")
    source_type = data.get("source_type")
    key = data.get("file_key")
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 255 or any(c in filename for c in ("/", "\\", "\x00")):
        raise ValueError("Недопустимое имя файла")
    if source_type not in {"voice", "audio", "video", "video_note", "document"}:
        raise ValueError("Недопустимый тип файла")
    if not isinstance(key, str) or not key or "\\" in key or Path(key).is_absolute() or ".." in Path(key).parts:
        raise ValueError("Недопустимая ссылка на файл")
    root = Path(os.environ["CORE_MEDIA_ROOT"]).resolve(strict=True)
    path = (root / key).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Файл вне хранилища")
    size = path.stat().st_size
    if size > get_max_file_size_mb() * 1048576:
        raise ValueError("Недопустимый размер файла")
    if not size:
        raise EmptyFileError()
    if source_type == "document" and Path(filename).suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise UnsupportedFormatError()
    duration = data.get("duration")
    if duration is not None and (type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0):
        raise ValueError("Недопустимая длительность")
    for field in ('telegram_chat_id', 'telegram_message_id'):
        if data.get(field) is not None and (type(data[field]) is not int or not -9223372036854775808 <= data[field] <= 9223372036854775807):
            raise ValueError('Недопустимые метаданные уведомления')
    if (data.get('telegram_chat_id') is None) != (data.get('telegram_message_id') is None):
        raise ValueError('Нужны оба идентификатора Telegram-сообщения')
    if data.get('request_key') is not None and (not isinstance(data['request_key'], str) or not 1 <= len(data['request_key']) <= 200):
        raise ValueError('Недопустимый ключ запроса')
    task = database.create_task(filename, source_type, size, duration, user_id, path.relative_to(root).as_posix(), data)
    logger.bind(component="Core", task_id=task['id'], event="created").info("Задача зарегистрирована через внутренний API")
    return task



