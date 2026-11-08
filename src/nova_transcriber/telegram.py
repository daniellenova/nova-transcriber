import asyncio
import logging
import os
import math
import re
import signal
from datetime import timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, DecimalException
from pathlib import Path
from time import perf_counter

import psycopg
from loguru import logger

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from nova_transcriber import core, database
from nova_transcriber.errors import MediaError
from nova_transcriber.safe_logging import configure_logging
from nova_transcriber.safe_logging import task_log


FILE_ROOT = Path("/var/lib/telegram-bot-api")


def _read_max_file_size_mb() -> Decimal:
    try:
        value = Decimal(os.environ.get("MAX_FILE_SIZE_MB", "100"))
        if not value.is_finite() or value <= 0:
            raise ValueError
        value * 1_048_576
    except (DecimalException, ValueError):
        raise ValueError("MAX_FILE_SIZE_MB должен быть положительным конечным числом (в МиБ)") from None
    return value


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
        "completed": "Завершена", "done": "Завершена",
        "failed": "Ошибка", "error": "Ошибка",
    }
    lines.append(f"Состояние: {statuses.get(record.get('status'), 'Неизвестно')}")
    if record.get("status") in {"failed", "error"} and record.get("error_reason"):
        reason = record["error_reason"].rstrip(".")
        safe_reasons = {
            "Файл пустой", "Неподдерживаемый формат файла",
            "Не удалось прочитать аудио или видео", "Не удалось обработать файл",
            "Ошибка распознавания аудио или видео",
            "Обработка отменена до начала распознавания",
            "Обработка прервана до начала распознавания",
            database.INTERRUPTED_REASON,
        }
        reasons = {
            "Не удалось сохранить результат в PostgreSQL": "Не удалось сохранить результат",
            "Не удалось начать обработку в PostgreSQL": "Не удалось начать обработку",
        }
        reason = reasons.get(reason, reason if reason in safe_reasons else "Не удалось обработать файл")
        lines.append(f"Причина: {reason}.")
    return "\n".join(lines)


async def history(message: Message) -> None:
    if message.from_user is None:
        return
    try:
        records = await asyncio.to_thread(database.get_recent_transcriptions, 10, message.from_user.id)
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
            reply_markup=keyboard if record["status"] == "done" else None,
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
        record = await asyncio.to_thread(database.get_transcription, int(parts[2]), callback.from_user.id)
    except Exception:
        logging.getLogger(__name__).exception("Не удалось получить транскрибацию")
        await callback.message.answer("Не удалось загрузить результат. Попробуйте позже.")
        return
    if record is None or record["status"] != "done" or record["text"] is None:
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
    message: Message, bot: Bot, max_file_size_mb: Decimal = Decimal("100")
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

    stage = "Запрос getFile"
    stage_started = perf_counter()
    try:
        log.bind(event="file_download_started").info(
            "Начат запрос getFile; размер по данным Telegram: {} байт; "
            "тайм-аут всего HTTP-запроса: 600 с", metadata_size if metadata_size is not None else "неизвестен"
        )
        file = await bot.get_file(media.file_id, request_timeout=600)
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

    task_id = None

    async def update_status(identifier: int, status: str) -> None:
        nonlocal task_id, status_message
        task_id = identifier
        if status == "pending":
            await asyncio.to_thread(
                database.save_status_message, identifier, message.chat.id, status_message.message_id
            )
        text = (f"Файл получен. Задача №{identifier} создана. Ожидает обработки"
                if status == "pending" else f"Задача №{identifier}: распознаю речь…")
        try:
            if status_message is None:
                status_message = await message.answer(text)
            else:
                await status_message.edit_text(text)
        except TelegramAPIError:
            task_log(identifier, "notification_failed", "Не удалось обновить сообщение о состоянии",
                     error="Ошибка отправки сообщения Telegram")

    async def report_error(text: str) -> None:
        text = f"Задача №{task_id}: ошибка. {text}" if task_id is not None else text
        if status_message is not None:
            try:
                await status_message.edit_text(text)
                return
            except TelegramAPIError:
                pass
        await message.answer(text)

    try:
        text = await core.process_recording(
            path=path,
            filename=filename,
            source_type=source_type,
            file_size=file_size,
            duration=getattr(media, "duration", None),
            user_id=message.from_user.id if message.from_user else None,
            on_status=update_status,
        )
    except MediaError as error:
        await report_error(str(error))
        return
    except Exception:
        if task_id is None:
            logging.getLogger(__name__).error("Не удалось зарегистрировать задачу обработки файла")
        await report_error("Не удалось обработать файл. Попробуйте повторить позже.")
        return

    try:
        await status_message.edit_text(f"Задача №{task_id}: речь распознана. Отправляю результат…")
    except TelegramAPIError:
        task_log(task_id, "notification_failed", "Не удалось обновить сообщение о состоянии",
                 error="Ошибка отправки сообщения Telegram")
    output_name = Path(filename.replace("\\", "/")).stem + ".txt"
    try:
        await message.answer_document(
            BufferedInputFile(text.encode("utf-8"), filename=output_name),
            caption=f"Задача №{task_id}: завершена. Результат отправлен",
        )
        task_log(task_id, "txt_sent", "Результат отправлен пользователю")
        final_status = f"Задача №{task_id}: готово. Результат отправлен."
    except TelegramAPIError:
        task_log(task_id, "txt_send_failed", "Результат сохранён, но TXT не отправлен",
                 error="Ошибка отправки документа Telegram")
        final_status = (
            f"Задача №{task_id}: речь распознана, но отправить TXT не удалось. "
            "Результат сохранён и доступен через /history."
        )
    if status_message is not None:
        try:
            await status_message.edit_text(final_status)
        except TelegramAPIError:
            await message.answer(final_status)


async def notify_interrupted(bot: Bot, user_id: int) -> None:
    records = await asyncio.to_thread(database.get_interrupted_notifications, user_id)
    for record in records:
        identifier = record["id"]
        text = (
            f"Задача №{identifier}: ошибка. {database.INTERRUPTED_REASON}. "
            "Распознавание остановлено. Для повторной обработки отправьте файл ещё раз."
        )
        chat_id = record["telegram_chat_id"] or record["user_id"]
        if record["telegram_message_id"] is not None:
            try:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=record["telegram_message_id"], parse_mode=None
                )
            except TelegramAPIError:
                task_log(identifier, "status_edit_failed", "Не удалось обновить прежнее сообщение",
                         error="Будет отправлено отдельное уведомление о прерывании")
        try:
            # Отдельное сообщение уведомляет пользователя, даже если он не открыл старый статус.
            await bot.send_message(chat_id, text, parse_mode=None)
        except TelegramAPIError:
            task_log(identifier, "interruption_notification_failed",
                     "Не удалось сообщить о прерывании; отправка будет повторена",
                     error="Ошибка отправки сообщения Telegram")
            continue
        await asyncio.to_thread(database.mark_interruption_notified, identifier)
        task_log(identifier, "interruption_notified", "Пользователь уведомлён о прерывании обработки")


async def main() -> None:
    configure_logging()
    max_file_size_mb = _read_max_file_size_mb()
    ZoneInfo(os.environ.get("DISPLAY_TIMEZONE", "Europe/Moscow"))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    if allowed_user_id <= 0:
        raise ValueError("TELEGRAM_ALLOWED_USER_ID должен быть положительным")

    async def recover() -> None:
        logger.bind(event="recovery_started").info("Запущена проверка незавершённых задач")
        identifiers = await asyncio.to_thread(database.recover_transcriptions, allowed_user_id)
        for identifier in identifiers:
            logger.bind(task_id=identifier, event="interrupted").warning(
                "Задача прервана остановкой сервиса"
            )
            task_log(identifier, "recovered", "Задача переведена в состояние «Ошибка»")
        logger.bind(event="recovery_completed").info(
            "Проверка завершена. Восстановлено состояний: {}", len(identifiers)
        )

    for attempt in range(1, 11):
        try:
            await asyncio.to_thread(database.init_db)
            await recover()
            break
        except psycopg.OperationalError:
            if attempt == 10:
                logger.bind(event="database_unavailable").error(
                    "Не удалось подключиться к PostgreSQL после 10 попыток. Сервис остановлен"
                )
                raise SystemExit(1) from None
            logger.bind(event="database_retry").warning(
                "PostgreSQL недоступен. Попытка {} из 10; повтор через 2 секунды", attempt
            )
            await asyncio.sleep(2)

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
            core.stop_accepting()
            stop_event.set()

    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop_signal, request_stop)

    async with Bot(token=token, session=session) as bot:
        async def notifications() -> None:
            while True:
                try:
                    await notify_interrupted(bot, allowed_user_id)
                except psycopg.Error:
                    logger.bind(event="notification_database_error").error(
                        "Не удалось прочитать или сохранить уведомления о прерывании; повтор через 30 секунд"
                    )
                await asyncio.sleep(30)

        notification_task = asyncio.create_task(notifications())
        polling = asyncio.create_task(dispatcher.start_polling(
            bot, allowed_updates=["message", "callback_query"],
            handle_signals=False, close_bot_session=False,
        ))
        stopping = asyncio.create_task(stop_event.wait())
        logger.bind(event="ready").info("Сервис готов к приёму новых файлов")
        try:
            await asyncio.wait((polling, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:
            core.stop_accepting()
            logger.bind(event="shutdown_started").info("Выполняется завершение работы")
            polling.cancel()
            stopping.cancel()
            notification_task.cancel()
            await asyncio.gather(polling, stopping, notification_task, return_exceptions=True)
            await core.wait_for_processing()
            try:
                await recover()
                try:
                    await asyncio.wait_for(notify_interrupted(bot, allowed_user_id), timeout=3)
                except asyncio.TimeoutError:
                    logger.bind(event="shutdown_notification_timeout").warning(
                        "Не все уведомления отправлены при остановке; отправка продолжится после запуска"
                    )
            except psycopg.Error:
                logger.bind(event="shutdown_database_error").error(
                    "Не удалось обновить задачи при остановке. Восстановление выполнится при следующем запуске"
                )
            logger.bind(event="shutdown_completed").info("Завершение работы выполнено")
        if not polling.cancelled():
            polling.result()


if __name__ == "__main__":
    asyncio.run(main())
