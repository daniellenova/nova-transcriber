import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message

from nova_transcriber import core, database


FILE_ROOT = Path("/var/lib/telegram-bot-api")


async def start(message: Message) -> None:
    await message.answer(
        "Отправьте голосовое сообщение, аудио, видео или документ. "
        "После распознавания русской речи вы получите TXT."
    )


async def receive_file(message: Message, bot: Bot) -> None:
    source_type = message.content_type
    media = getattr(message, source_type)

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
        file_size = media.file_size
        if file_size is None:
            file_size = file.file_size
        if file_size is None:
            file_size = path.stat().st_size
    except (TelegramAPIError, OSError, ValueError):
        await message.answer(
            "Не удалось получить доступ к файлу. Проверьте локальный Bot API "
            "и подключение общего каталога файлов."
        )
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
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    if allowed_user_id <= 0:
        raise ValueError("TELEGRAM_ALLOWED_USER_ID должен быть положительным")

    await asyncio.to_thread(database.init_db)

    dispatcher = Dispatcher()
    dispatcher.message.filter(F.from_user.id == allowed_user_id)
    dispatcher.message.register(start, CommandStart())
    dispatcher.message.register(
        receive_file, F.content_type.in_({"voice", "audio", "video", "document"})
    )

    session = AiohttpSession(
        api=TelegramAPIServer.from_base("http://telegram-bot-api:8081", is_local=True)
    )
    async with Bot(token=token, session=session) as bot:
        await dispatcher.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
