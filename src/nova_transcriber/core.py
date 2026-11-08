import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from threading import Event

from nova_transcriber import asr, database
from nova_transcriber.errors import EmptyFileError, UnsupportedFormatError, MediaError
from nova_transcriber.safe_logging import task_log


_semaphore = asyncio.Semaphore(1)
_stopping = Event()
_active_tasks: set[asyncio.Task] = set()


def stop_accepting() -> None:
    _stopping.set()


async def wait_for_processing() -> None:
    if _active_tasks:
        await asyncio.wait(tuple(_active_tasks), timeout=3)


SUPPORTED_MEDIA_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".aiff", ".aif", ".wma", ".mp4", ".m4v", ".mov", ".mkv", ".webm",
    ".avi", ".mpeg", ".mpg", ".ts", ".3gp", ".3g2", ".amr",
}


def _register_recording(path, filename, source_type, file_size, duration, user_id=None):
    if _stopping.is_set():
        raise RuntimeError("Сервис завершает работу")
    if path.stat().st_size == 0:
        raise EmptyFileError()
    if source_type == "document" and Path(filename).suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise UnsupportedFormatError()
    recording_id = database.create_recording(filename, source_type, file_size, duration)
    task_id = database.create_transcription(recording_id, "large-v3", user_id)
    if _stopping.is_set():
        _fail(task_id, database.INTERRUPTED_REASON)
        raise RuntimeError("Сервис завершает работу")
    task_log(task_id, "created", "Задача создана. Ожидает обработки")
    return task_id


def _fail(task_id, reason, duration=None):
    task_log(task_id, "failed", "Обработка завершилась ошибкой", duration=duration, error=reason)
    try:
        database.fail_transcription(task_id, duration, reason)
    except Exception:
        task_log(task_id, "failure_save_failed", "Не удалось сохранить состояние задачи",
                 error="Ошибка записи в PostgreSQL")


def _process_recording(path, filename, source_type, file_size, duration, task_id=None):
    if task_id is None:
        task_id = _register_recording(path, filename, source_type, file_size, duration)
    started = None
    stage = "start"
    try:
        if _stopping.is_set():
            raise RuntimeError("Сервис завершает работу")
        database.start_transcription(task_id)
        started = perf_counter()
        task_log(task_id, "processing", "Начата транскрибация")
        stage = "recognize"
        text = asr.transcribe(path)
        stage = "save"
        processing_time = perf_counter() - started
        database.complete_transcription(task_id, text, processing_time)
        task_log(task_id, "completed", "Результат сохранён. Транскрибация завершена",
                 duration=processing_time)
    except Exception as error:
        reason = (database.INTERRUPTED_REASON if _stopping.is_set()
                  else str(error) if type(error) in {EmptyFileError, UnsupportedFormatError}
                  else "Не удалось прочитать аудио или видео" if isinstance(error, MediaError)
                  else "Не удалось сохранить результат в PostgreSQL" if stage == "save"
                  else "Не удалось начать обработку в PostgreSQL" if stage == "start"
                  else "Ошибка распознавания аудио или видео")
        _fail(task_id, reason, None if started is None else perf_counter() - started)
        raise
    return text


async def process_recording(
    path: str | Path,
    filename: str,
    source_type: str,
    file_size: int,
    duration: float | None = None,
    *,
    user_id: int | None = None,
    on_status: Callable[[int, str], Awaitable[None]] | None = None,
) -> str:
    # Регистрация завершается даже при отмене запроса, чтобы не потерять ID задачи.
    registration = asyncio.create_task(asyncio.to_thread(
        _register_recording, Path(path), filename, source_type, file_size, duration, user_id
    ))
    _active_tasks.add(registration)
    registration.add_done_callback(_active_tasks.discard)
    try:
        task_id = await asyncio.shield(registration)
    except asyncio.CancelledError:
        task_id = await registration
        await asyncio.to_thread(
            _fail, task_id, database.INTERRUPTED_REASON if _stopping.is_set()
            else "Обработка отменена до начала распознавания",
        )
        raise
    processing_started = False
    try:
        if on_status:
            await on_status(task_id, "pending")
        async with _semaphore:
            if _stopping.is_set():
                raise RuntimeError("Сервис завершает работу")
            if on_status:
                await on_status(task_id, "processing")
            task = asyncio.create_task(asyncio.to_thread(
                _process_recording, Path(path), filename, source_type, file_size, duration, task_id
            ))
            _active_tasks.add(task)
            task.add_done_callback(_active_tasks.discard)
            processing_started = True
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Поток продолжает работу: удерживаем семафор до его завершения.
                try:
                    await task
                finally:
                    raise
    except (Exception, asyncio.CancelledError):
        if not processing_started:
            await asyncio.to_thread(
                _fail, task_id, database.INTERRUPTED_REASON if _stopping.is_set()
                else "Обработка прервана до начала распознавания",
            )
        raise
