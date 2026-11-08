import asyncio
import logging
import os
import math
import re
from datetime import timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, DecimalException
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
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
    source_type = message.content_type
    media = getattr(message, source_type)
    max_file_size = max_file_size_mb * 1_048_576
    too_large_message = (
        f"Файл слишком большой. Максимальный размер — {max_file_size_mb.normalize():f} МБ."
    )
    metadata_size = getattr(media, "file_size", None)
    if isinstance(metadata_size, int) and metadata_size > max_file_size:
        await message.answer(too_large_message)
        return

    try:
        file = await bot.get_file(media.file_id)
        if not file.file_path:
            raise ValueError("Bot API не вернул путь файла")
        path = Path(file.file_path)
        if not path.is_absolute():
            raise ValueError("Bot API не вернул локальный абсолютный путь")
        path = path.resolve()
        if not path.is_relative_to(FILE_ROOT) or not path.is_file():
            raise ValueError("Локальный файл недоступен в общем каталоге")
        filename = getattr(media, "file_name", None) or {
            "voice": "Голосовое сообщение", "video_note": "Видеосообщение",
            "audio": "Аудиозапись", "video": "Видеозапись",
        }.get(source_type, path.name)
        file_size = path.stat().st_size
    except (TelegramAPIError, OSError, ValueError):
        await message.answer(
            "Не удалось получить доступ к файлу. Проверьте локальный Bot API "
            "и подключение общего каталога файлов."
        )
        return

    if file_size > max_file_size:
        await message.answer(too_large_message)
        return

    task_id = None
    status_message = None

    async def update_status(identifier: int, status: str) -> None:
        nonlocal task_id, status_message
        task_id = identifier
        text = (f"Файл получен. Задача №{identifier} создана. Ожидает обработки"
                if status == "pending" else f"Задача №{identifier}: обрабатывается")
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

    output_name = Path(filename.replace("\\", "/")).stem + ".txt"
    try:
        await message.answer_document(
            BufferedInputFile(text.encode("utf-8"), filename=output_name),
            caption=f"Задача №{task_id}: завершена. Результат отправлен",
        )
        task_log(task_id, "txt_sent", "Результат отправлен пользователю")
    except TelegramAPIError:
        task_log(task_id, "txt_send_failed", "Результат сохранён, но TXT не отправлен",
                 error="Ошибка отправки документа Telegram")
        await message.answer(
            f"Задача №{task_id}: завершена. Текст сохранён в базе, но отправить TXT не удалось."
        )
    if status_message is not None:
        try:
            await status_message.edit_text(f"Задача №{task_id}: завершена")
        except TelegramAPIError:
            pass


async def main() -> None:
    configure_logging()
    max_file_size_mb = _read_max_file_size_mb()
    ZoneInfo(os.environ.get("DISPLAY_TIMEZONE", "Europe/Moscow"))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    if allowed_user_id <= 0:
        raise ValueError("TELEGRAM_ALLOWED_USER_ID должен быть положительным")

    await asyncio.to_thread(database.init_db)

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
    async with Bot(token=token, session=session) as bot:
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
