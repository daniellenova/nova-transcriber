import math
import os
from pathlib import Path

from loguru import logger

from nova_transcriber import database
from nova_transcriber.config import get_max_file_size_mb, get_positive_int
from nova_transcriber.errors import EmptyFileError, UnsupportedFormatError


SUPPORTED_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".aiff", ".aif", ".wma", ".mp4", ".m4v", ".mov", ".mkv", ".webm",
    ".avi", ".mpeg", ".mpg", ".ts", ".3gp", ".3g2", ".amr",
}


def api_record(record):
    fields = ("id", "filename", "source_type", "file_size", "duration", "model",
              "created_at", "started_at", "finished_at", "processing_time", "error_reason", "status",
              "processed_samples", "progress_samples", "total_samples", "stage", "delivery_state")
    return {**{key: record[key] for key in fields},
            "number": record.get('number'),
            "deleting": record['delete_requested'],
            "actions": ([] if record['delete_requested'] else
                        ['cancel'] if record['status'] in {'pending', 'processing'} else
                        ['retry'] if record['status'] == 'failed' and record.get('file_key') else
                        ['resume'] if record['status'] == 'paused' and record.get('file_key') else []),
            "has_partial_result": record["status"] != "completed" and record["processed_samples"] > 0}


def create_api_task(*, user_id, file_path, source, filename, duration=None,
                    request_key=None, notification=None):
    key = file_path
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 255 or any(c in filename for c in ("/", "\\", "\x00")):
        raise ValueError("Недопустимое имя файла")
    if source not in {"voice", "audio", "video", "video_note", "document"}:
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
    if source == "document" and Path(filename).suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise UnsupportedFormatError()
    if duration is not None and (type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0):
        raise ValueError("Недопустимая длительность")
    if duration is not None and duration > get_positive_int("MAX_DURATION_SECONDS", 43200):
        raise ValueError("Запись превышает допустимую длительность")
    if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 200):
        raise ValueError('Недопустимый ключ запроса')
    task = database.create_task(
        filename, source, size, duration, user_id, path.relative_to(root).as_posix(),
        {'request_key': request_key, 'notification': notification},
    )
    logger.bind(component="Core", task_id=task['id'], event="created").info("Задача зарегистрирована через внутренний API")
    return task



