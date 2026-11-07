import asyncio
import logging
from pathlib import Path
from time import perf_counter

from nova_transcriber import asr, database
from nova_transcriber.errors import EmptyFileError, UnsupportedFormatError


_semaphore = asyncio.Semaphore(1)
_logger = logging.getLogger(__name__)
SUPPORTED_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".aiff", ".aif", ".wma", ".mp4", ".m4v", ".mov", ".mkv", ".webm",
    ".avi", ".mpeg", ".mpg", ".ts", ".3gp", ".3g2", ".amr",
}


def _process_recording(
    path: Path,
    filename: str,
    source_type: str,
    file_size: int,
    duration: float | None,
) -> str:
    if path.stat().st_size == 0:
        raise EmptyFileError()
    if source_type == "document" and Path(filename).suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise UnsupportedFormatError()
    recording_id = database.create_recording(filename, source_type, file_size, duration)
    transcription_id = database.create_transcription(recording_id, "large-v3")
    started = perf_counter()
    try:
        text = asr.transcribe(path)
        processing_time = perf_counter() - started
        database.complete_transcription(transcription_id, text, processing_time)
    except Exception:
        try:
            database.fail_transcription(transcription_id, perf_counter() - started)
        except Exception:
            _logger.exception("Не удалось сохранить статус error")
        raise
    return text


async def process_recording(
    path: str | Path,
    filename: str,
    source_type: str,
    file_size: int,
    duration: float | None = None,
) -> str:
    async with _semaphore:
        task = asyncio.create_task(
            asyncio.to_thread(
                _process_recording, Path(path), filename, source_type, file_size, duration
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Поток продолжает работу после отмены: удерживаем семафор до его завершения.
            try:
                await task
            finally:
                raise
