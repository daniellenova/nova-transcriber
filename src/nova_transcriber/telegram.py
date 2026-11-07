import asyncio
import logging
import os
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


def _recording_summary(record: dict) -> str:
    created_at = record["created_at"].strftime("%d.%m.%Y %H:%M:%S %z")
    summary = f"Файл: {record['filename'][:1024]}\nДата: {created_at}"
    if record["duration"] is not None:
        summary += f"\nДлительность: {record['duration']:g} с"
    return summary


async def history(message: Message) -> None:
    try:
        records = await asyncio.to_thread(database.get_recent_transcriptions, 10)
    except Exception:
        logging.getLogger(__name__).exception("Не удалось получить историю")
        await message.answer("Не удалось загрузить историю. Попробуйте позже.")
        return

    successful = [record for record in records if record["status"] == "done"]
    if not successful:
        await message.answer("Среди последних 10 транскрибаций нет готовых результатов.")
        return

    for record in successful:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Скачать TXT", callback_data=f"history:txt:{record['id']}"
            ),
            InlineKeyboardButton(
                text="Информация", callback_data=f"history:info:{record['id']}"
            ),
        ]])
        await message.answer(_recording_summary(record), reply_markup=keyboard)


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
        record = await asyncio.to_thread(database.get_transcription, int(parts[2]))
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

    processing_time = record["processing_time"]
    processing = f"{processing_time:g} с" if processing_time is not None else "неизвестно"
    summary = _recording_summary(record)
    if record["duration"] is None:
        summary += "\nДлительность: неизвестна"
    await callback.message.answer(
        f"{summary}\nРазмер: {record['file_size']} байт\n"
        f"Модель: {record['model']}\nВремя обработки: {processing}\n"
        f"Символов: {len(record['text'])}"
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
        filename = getattr(media, "file_name", None) or path.name
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

    await message.answer("Файл получен. Начинаю обработку; файлы обрабатываются по одному.")
    try:
        text = await core.process_recording(
            path=path,
            filename=filename,
            source_type=source_type,
            file_size=file_size,
            duration=getattr(media, "duration", None),
        )
    except MediaError as error:
        await message.answer(str(error))
        return
    except Exception:
        logging.getLogger(__name__).exception("Ошибка обработки файла")
        await message.answer("Не удалось обработать файл. Попробуйте повторить позже.")
        return

    output_name = Path(filename.replace("\\", "/")).stem + ".txt"
    try:
        await message.answer_document(
            BufferedInputFile(text.encode("utf-8"), filename=output_name),
            caption="Готово.",
        )
    except TelegramAPIError:
        await message.answer(
            "Текст сохранён в базе, но отправить TXT не удалось."
        )


async def main() -> None:
    configure_logging()
    max_file_size_mb = _read_max_file_size_mb()
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
        receive_file, F.content_type.in_({"voice", "audio", "video", "document"})
    )

    session = AiohttpSession(
        api=TelegramAPIServer.from_base("http://telegram-bot-api:8081", is_local=True)
    )
    async with Bot(token=token, session=session) as bot:
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
