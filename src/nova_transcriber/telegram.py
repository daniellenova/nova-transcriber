import asyncio
import logging
import os
import math
import re
import signal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from nova_transcriber import api_client
from loguru import logger

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


from nova_transcriber.config import check_disk_space, get_max_file_size_mb, get_owner_id, get_positive_int
from nova_transcriber.errors import MediaError
from nova_transcriber.safe_logging import configure_logging


FILE_ROOT = Path("/var/lib/telegram-bot-api")
DOWNLOAD_LOCK = asyncio.Lock()


async def start(message: Message) -> None:
    await message.answer(
        "Отправьте голосовое сообщение, аудио, видео или документ. "
        "После распознавания русской речи вы получите TXT."
        " История результатов: /history."
    )


def _duration(value: float) -> str:
    hours, seconds = divmod(round(value), 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if seconds or not parts:
        parts.append(f"{seconds} с")
    return " ".join(parts)


def _progress(record: dict) -> str:
    total = record.get('total_samples')
    if not total:
        return record.get('stage') or 'Подготовка аудио'
    processed = record.get('processed_samples', 0)
    percent = min(100, processed * 100 // total)
    return f'{percent}% · {_duration(processed / 16000)} из {_duration(total / 16000)}'


def _recording_summary(record: dict) -> str:
    names = {
        "voice": "Голосовое сообщение", "video_note": "Видеосообщение",
        "audio": "Аудиозапись", "video": "Видеозапись",
    }
    source_type = record.get("source_type")
    filename = record.get("filename") or ""
    if (source_type in {"voice", "video_note"} or not filename
            or "/" in filename or "\\" in filename
            or re.fullmatch(r"file_\d+(?:\.[^.]*)?", filename)):
        filename = names.get(source_type, "Файл без названия")
    filename = " ".join(filename.split())
    if len(filename) > 200:
        filename = filename[:199] + "…"
    created_at = record.get("created_at")
    date = "Дата неизвестна"
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        date = created_at.astimezone(
            ZoneInfo(os.environ.get("DISPLAY_TIMEZONE", "Europe/Moscow"))
        ).strftime("%d.%m.%Y, %H:%M")
    lines = [f"Задача №{record['id']}", f"Файл: {filename}", f"Дата: {date}"]
    for key, label in (("duration", "Длительность записи"),
                       ("processing_time", "Время обработки")):
        value = record.get(key)
        if value is not None and math.isfinite(value) and value >= 0:
            lines.append(f"{label}: {_duration(value)}")
    statuses = {
        "pending": "Ожидает обработки", "processing": "Обрабатывается",
        "completed": "Завершена",
        "failed": "Ошибка",
    }
    lines.append(f"Состояние: {statuses.get(record.get('status'), 'Неизвестно')}")
    if record.get('status') in {'processing', 'failed'} and record.get('total_samples'):
        lines.append(f"Обработано: {_progress(record)}")
    if record.get("status") == "failed" and record.get("error_reason"):
        reason = record["error_reason"].rstrip(".")
        safe_reasons = {
            "Файл пустой", "Неподдерживаемый формат файла",
            "Не удалось прочитать аудио или видео", "Не удалось обработать файл",
            "Не удалось обработать аудио или видео",
            "Не удалось прочитать аудио или видео. Проверьте файл",
            "Ошибка распознавания аудио или видео",
            "Обработка отменена до начала распознавания",
            "Обработка прервана до начала распознавания",
            "Обработка прервана из-за остановки или перезапуска сервиса",
            "Запись превышает допустимую длительность",
            "Файл превышает допустимый размер",
            "Недостаточно свободного места на диске",
            "Превышено число попыток восстановления",
            "Исходный файл недоступен",
            "Длительность исходного файла изменилась",
            "Процесс обработки неожиданно остановился",
        }
        safe_reasons.update(f'Превышено время этапа «{stage}»' for stage in (
            'Подготовка аудио', 'Определение длительности', 'Загрузка модели',
            'Чтение аудио', 'Распознавание',
        ))
        reasons = {
            "Не удалось сохранить результат в PostgreSQL": "Не удалось сохранить результат",
            "Не удалось начать обработку в PostgreSQL": "Не удалось начать обработку",
        }
        reason = reasons.get(reason, reason if reason in safe_reasons else "Не удалось обработать файл")
        lines.append(f"Причина: {reason}.")
    return "\n".join(lines)


async def history(message: Message) -> None:
    try:
        records = await asyncio.to_thread(api_client.request, '/tasks?limit=10')
    except Exception:
        logging.getLogger(__name__).exception("Не удалось получить историю")
        await message.answer("Не удалось загрузить историю. Попробуйте позже.")
        return

    if not records:
        await message.answer("История транскрибаций пока пуста")
        return

    for record in records:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Скачать TXT", callback_data=f"history:txt:{record['id']}"
            ),
            InlineKeyboardButton(
                text="Информация", callback_data=f"history:info:{record['id']}"
            ),
        ]])
        await message.answer(
            _recording_summary(record), parse_mode=None,
            reply_markup=keyboard if record["status"] == "completed" else None,
        )


async def history_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 3
        or parts[1] not in {"txt", "info"}
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or not 0 < int(parts[2]) <= 9223372036854775807
    ):
        await callback.answer("Некорректный идентификатор.", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return

    await callback.answer()
    try:
        record = await asyncio.to_thread(api_client.record, int(parts[2]))
    except Exception:
        logging.getLogger(__name__).exception("Не удалось получить транскрибацию")
        await callback.message.answer("Не удалось загрузить результат. Попробуйте позже.")
        return
    if record["status"] != "completed" or record["text"] is None:
        await callback.message.answer("Готовый результат не найден.")
        return

    if parts[1] == "txt":
        output_name = Path(record["filename"].replace("\\", "/")).stem + ".txt"
        try:
            await callback.message.answer_document(
                BufferedInputFile(record["text"].encode("utf-8"), filename=output_name)
            )
        except TelegramAPIError:
            await callback.message.answer("Не удалось отправить TXT. Попробуйте позже.")
        return

    await callback.message.answer(
        f"{_recording_summary(record)}\nРазмер: {record['file_size']} байт\n"
        f"Модель: {record['model']}\nСимволов: {len(record['text'])}",
        parse_mode=None,
    )


async def receive_file(
    message: Message, bot: Bot, max_file_size_mb: Decimal = Decimal("1024")
) -> None:
    log = logger.bind(component=f"Telegram, сообщение №{message.message_id}")
    received_at = perf_counter()
    log.bind(event="file_received").info("Получено сообщение с медиафайлом")
    try:
        status_message = await message.answer(
            "Получено сообщение с файлом. Загружаю файл для обработки…"
        )
    except TelegramAPIError as error:
        log.bind(event="file_ack_failed", duration=f"{perf_counter() - received_at:.3f} с",
                 error=error.message).error("Не удалось отправить подтверждение получения файла")
        raise
    log.bind(event="file_ack_sent", duration=f"{perf_counter() - received_at:.3f} с").info(
        "Отправлено подтверждение получения файла"
    )
    source_type = message.content_type
    media = getattr(message, source_type)
    max_file_size = max_file_size_mb * 1_048_576
    too_large_message = (
        f"Файл слишком большой. Максимальный размер — {max_file_size_mb.normalize():f} МБ."
    )
    metadata_size = getattr(media, "file_size", None)
    if isinstance(metadata_size, int) and metadata_size > max_file_size:
        await status_message.edit_text(too_large_message)
        return
    duration = getattr(media, 'duration', None)
    if duration is not None and duration > get_positive_int('MAX_DURATION_SECONDS', 43200):
        await status_message.edit_text('Запись превышает допустимую длительность.')
        return

    stage = "Запрос getFile"
    stage_started = perf_counter()
    try:
        log.bind(event="file_download_started").info(
            "Начат запрос getFile; размер по данным Telegram: {} байт; "
            "тайм-аут всего HTTP-запроса: 600 с", metadata_size if metadata_size is not None else "неизвестен"
        )
        async with DOWNLOAD_LOCK:
            check_disk_space(FILE_ROOT, metadata_size if isinstance(metadata_size, int) and metadata_size > 0 else int(max_file_size))
            for attempt in range(3):
                try:
                    file = await bot.get_file(media.file_id, request_timeout=600)
                    break
                except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as error:
                    if attempt == 2:
                        raise
                    delay = error.retry_after if isinstance(error, TelegramRetryAfter) else 2 ** attempt
                    if delay > 30:
                        raise
                    log.bind(event='download_retry').warning('Повтор получения файла, попытка {} из 3', attempt + 2)
                    await asyncio.sleep(delay)
        log.bind(event="get_file_completed", duration=f"{perf_counter() - stage_started:.3f} с").info(
            "Получен ответ getFile"
        )
        stage = "Проверка локального пути"
        stage_started = perf_counter()
        if not file.file_path:
            raise ValueError("Bot API не вернул путь файла")
        path = Path(file.file_path)
        if not path.is_absolute():
            raise ValueError("Bot API не вернул локальный абсолютный путь")
        path = path.resolve()
        if not path.is_relative_to(FILE_ROOT):
            raise ValueError("Путь файла находится вне общего каталога")
        log.bind(event="file_path_checked", duration=f"{perf_counter() - stage_started:.3f} с").info(
            "Локальный путь находится в общем каталоге"
        )
        stage = "Проверка доступности файла"
        stage_started = perf_counter()
        if not path.is_file():
            raise ValueError("Локальный файл недоступен в общем каталоге")
        log.bind(event="file_availability_checked", duration=f"{perf_counter() - stage_started:.3f} с").info(
            "Файл существует; доступ для чтения: {}", "да" if os.access(path, os.R_OK) else "нет"
        )
        stage = "Получение размера файла"
        stage_started = perf_counter()
        filename = getattr(media, "file_name", None) or {
            "voice": "Голосовое сообщение", "video_note": "Видеосообщение",
            "audio": "Аудиозапись", "video": "Видеозапись",
        }.get(source_type, path.name)
        file_size = path.stat().st_size
        log.bind(event="file_size_read", duration=f"{perf_counter() - stage_started:.3f} с").info(
            "Получен размер локального файла: {} байт", file_size
        )
    except MediaError as error:
        await status_message.edit_text(str(error))
        return
    except TelegramNetworkError as error:
        timed_out = isinstance(error.__cause__, asyncio.TimeoutError)
        reason = ("Истекло время ожидания загрузки файла из Telegram"
                  if timed_out else "Нарушено соединение с локальным Telegram Bot API")
        log.bind(event="file_download_failed", duration=f"{perf_counter() - stage_started:.3f} с",
                 error=error.message).error("Этап «{}»: {}", stage, reason)
        await status_message.edit_text(
            f"{reason}. Не удалось получить файл для обработки. Попробуйте отправить его ещё раз."
        )
        return
    except TelegramAPIError as error:
        log.bind(event="file_download_failed", duration=f"{perf_counter() - stage_started:.3f} с",
                 error=error.message).error(
            "Этап «{}»: Telegram Bot API отклонил запрос на получение файла", stage
        )
        await status_message.edit_text(
            "Telegram не смог предоставить файл для обработки. Попробуйте отправить его ещё раз."
        )
        return
    except (OSError, ValueError) as error:
        reason = (str(error) if isinstance(error, ValueError)
                  else f"Ошибка доступа к локальному файлу, код: {error.errno}")
        log.bind(event="file_access_failed", duration=f"{perf_counter() - stage_started:.3f} с").error(
            "Этап «{}»: {}", stage, reason
        )
        await status_message.edit_text(
            "Файл получен из Telegram, но недоступен для чтения. "
            "Проверьте подключение общего каталога файлов локального Bot API."
        )
        return

    log.bind(event="file_download_completed", duration=f"{perf_counter() - received_at:.3f} с").info(
        "Файл получен и доступен для обработки; общее время с получения сообщения"
    )
    if file_size > max_file_size:
        await status_message.edit_text(too_large_message)
        return

    try:
        task = await asyncio.to_thread(api_client.request, '/tasks', {
            'file_key': 'telegram/' + path.relative_to(FILE_ROOT).as_posix(),
            'filename': filename, 'source_type': source_type,
            'duration': getattr(media, 'duration', None),
            'telegram_chat_id': message.chat.id,
            'telegram_message_id': status_message.message_id,
            'request_key': f'{message.chat.id}:{message.message_id}',
        })
    except ValueError as error:
        await status_message.edit_text(str(error))
    except Exception:
        await status_message.edit_text('Не удалось зарегистрировать задачу. Проверьте /history перед повторной отправкой.')
    else:
        log.bind(task_id=task['id'], event='task_registered').info('Задача передана в Core API; отслеживание продолжится через уведомления')


async def notifications(bot):
    delivered = set()
    previous = {}
    updated_at = {}
    while True:
        try:
            records = await asyncio.to_thread(api_client.request, '/notifications')
        except Exception:
            logger.bind(component='Telegram Adapter', event='notification_retry').warning('Не удалось получить уведомления; попытка будет повторена')
            await asyncio.sleep(5)
            continue
        for record in records:
            try:
                identifier = record['id']
                status = record['status']
                chat = record['telegram_chat_id']
                async def edit(text):
                    try:
                        await bot.edit_message_text(text, chat_id=chat, message_id=record['telegram_message_id'])
                    except TelegramAPIError as error:
                        return 'message is not modified' in error.message.lower()
                    return True
                if status in {'pending', 'processing'}:
                    text = (f'Задача №{identifier}: {_progress(record)}' if status == 'processing'
                            else f'Задача №{identifier}: ожидает обработки')
                    if previous.get(identifier) != text and perf_counter() - updated_at.get(identifier, 0) >= 15:
                        if await edit(text):
                            previous[identifier] = text
                            updated_at[identifier] = perf_counter()
                    continue
                previous.pop(identifier, None)
                updated_at.pop(identifier, None)
                final_text = (f'Задача №{identifier}: готово. Результат отправлен.' if status == 'completed'
                              else _recording_summary(record))
                if identifier not in delivered:
                    if status == 'completed':
                        result = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/result')
                        await edit(f'Задача №{identifier}: речь распознана. Отправляю результат…')
                        filename = Path(record['filename'].replace('\\', '/')).stem + '.txt'
                        await bot.send_document(chat, BufferedInputFile(result['text'].encode('utf-8'), filename=filename), caption=f'Задача №{identifier}: завершена. Результат отправлен')
                        delivered.add(identifier)
                    else:
                        if not await edit(final_text):
                            await bot.send_message(chat, final_text)
                        delivered.add(identifier)
                if status == 'completed' and not await edit(final_text):
                    await bot.send_message(chat, final_text)
                    logger.bind(component='Telegram Adapter', task_id=identifier, event='status_edit_failed').warning('Итоговый статус отправлен отдельным сообщением')
                await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/notified', {})
                delivered.discard(identifier)
            except Exception:
                logger.bind(component='Telegram Adapter', task_id=record['id'], event='notification_retry').warning('Не удалось доставить или подтвердить уведомление; попытка будет повторена')
        await asyncio.sleep(5)

async def main() -> None:
    configure_logging()
    max_file_size_mb = get_max_file_size_mb()
    get_positive_int('MAX_DURATION_SECONDS', 43200)
    get_positive_int('MIN_FREE_DISK_MB', 512)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = get_owner_id()

    dispatcher = Dispatcher()
    dispatcher["max_file_size_mb"] = max_file_size_mb
    dispatcher.message.filter(F.from_user.id == allowed_user_id)
    dispatcher.callback_query.filter(F.from_user.id == allowed_user_id)
    dispatcher.message.register(start, CommandStart())
    dispatcher.message.register(history, Command("history"))
    dispatcher.callback_query.register(history_callback, F.data.startswith("history:"))
    dispatcher.message.register(
        receive_file, F.content_type.in_({"voice", "video_note", "audio", "video", "document"})
    )

    session = AiohttpSession(
        api=TelegramAPIServer.from_base("http://telegram-bot-api:8081", is_local=True)
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        if not stop_event.is_set():
            logger.bind(event="stop_signal").info("Получен сигнал остановки приложения")

            stop_event.set()

    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop_signal, request_stop)

    async with Bot(token=token, session=session) as bot:
        notification_task = asyncio.create_task(notifications(bot))
        polling = asyncio.create_task(dispatcher.start_polling(
            bot, allowed_updates=["message", "callback_query"],
            handle_signals=False, close_bot_session=False,
        ))
        stopping = asyncio.create_task(stop_event.wait())
        logger.bind(event="ready").info("Сервис готов к приёму новых файлов")
        try:
            await asyncio.wait((polling, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:

            logger.bind(event="shutdown_started").info("Выполняется завершение работы")
            polling.cancel()
            stopping.cancel()
            notification_task.cancel()
            await asyncio.gather(polling, stopping, notification_task, return_exceptions=True)
            logger.bind(event="shutdown_completed").info("Завершение работы выполнено")
        if not polling.cancelled():
            polling.result()


if __name__ == "__main__":
    asyncio.run(main())

