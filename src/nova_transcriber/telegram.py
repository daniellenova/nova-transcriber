import asyncio
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import Message


FILE_ROOT = Path("/var/lib/telegram-bot-api")


async def start(message: Message) -> None:
    await message.answer(
        "Отправьте голосовое сообщение, аудио, видео или документ. "
        "Пока бот только принимает файлы; обработка ещё не подключена."
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

    await message.answer(
        f"Файл получен: {filename}\n"
        f"Тип: {source_type}\n"
        f"Размер: {file_size} байт\n"
        f"Локальный путь: {path}\n"
        "Обработка ещё не подключена."
    )


async def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    if allowed_user_id <= 0:
        raise ValueError("TELEGRAM_ALLOWED_USER_ID должен быть положительным")

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
