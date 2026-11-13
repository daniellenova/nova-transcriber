import asyncio
import os
import signal

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand
from loguru import logger

from nova_transcriber import api_client
from nova_transcriber.config import get_max_file_size_mb, get_owner_id, get_positive_int
from nova_transcriber.safe_logging import configure_logging
from nova_transcriber.telegram_downloader import receive_file, _send_result
from nova_transcriber.telegram_handlers import start, history, history_callback, delete_callback, callback_guard, _show_history
from nova_transcriber.telegram_state import TelegramState, _recording_summary, _task_keyboard


async def notifications(bot, app_state):
    previous = {}
    while True:
        try:
            records = await asyncio.to_thread(api_client.request, '/notifications')
        except Exception:
            logger.bind(component='Telegram Adapter', event='notification_retry').warning('Не удалось получить уведомления')
            await asyncio.sleep(5)
            continue
        active = {record['id'] for record in records}
        app_state.confirmed_sends.intersection_update(active)
        previous = {key: value for key, value in previous.items() if key in active}
        for record in records:
            identifier = record['id']
            try:
                if identifier in app_state.confirmed_sends:
                    await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/delivery', {'action': 'sent'})
                    app_state.confirmed_sends.discard(identifier)
                    record.update(await asyncio.to_thread(api_client.request, f'/tasks/{identifier}'))
                if record['status'] == 'completed' and record['delivery_state'] == 'pending':
                    try:
                        await _send_result(app_state, bot, record['telegram_chat_id'], identifier)
                    except TelegramAPIError:
                        logger.bind(component='Telegram Adapter', task_id=identifier, event='delivery_failed').warning('Отправка TXT не подтверждена')
                    record.update(await asyncio.to_thread(api_client.request, f'/tasks/{identifier}'))
                text = _recording_summary(record, app_state, progress_bar=True)
                keyboard = _task_keyboard(record)
                signature = (text, keyboard.model_dump_json())
                if previous.get(identifier) != signature:
                    try:
                        await bot.edit_message_text(text, chat_id=record['telegram_chat_id'],
                            message_id=record['telegram_message_id'], reply_markup=keyboard, parse_mode='HTML')
                    except TelegramBadRequest as error:
                        if 'message is not modified' not in error.message.lower():
                            logger.bind(task_id=identifier, event='card_unavailable').warning('Карточка недоступна; результат остаётся в истории')
                    previous[identifier] = signature
                if record['status'] in {'completed', 'failed', 'paused'} and identifier not in app_state.send_busy:
                    await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/notified', {})
            except Exception:
                logger.bind(component='Telegram Adapter', task_id=identifier, event='notification_retry').warning('Не удалось обновить карточку')
        try:
            await _show_history(app_state)
        except Exception:
            logger.bind(component='Telegram Adapter', event='history_refresh_failed').warning('Не удалось обновить историю')
        await asyncio.sleep(5)

async def main() -> None:
    configure_logging()
    max_file_size_mb = get_max_file_size_mb()
    get_positive_int('MAX_DURATION_SECONDS', 43200)
    get_positive_int('MIN_FREE_DISK_MB', 512)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = get_owner_id()

    app_state = TelegramState()
    dispatcher = Dispatcher()
    dispatcher["app_state"] = app_state
    dispatcher["max_file_size_mb"] = max_file_size_mb
    dispatcher.message.filter(F.from_user.id == allowed_user_id)
    dispatcher.callback_query.filter(F.from_user.id == allowed_user_id)
    dispatcher.callback_query.outer_middleware(callback_guard)
    dispatcher.message.register(start, CommandStart())
    dispatcher.message.register(history, Command("history"))
    dispatcher.callback_query.register(history_callback, F.data.startswith("history:"))
    dispatcher.callback_query.register(delete_callback, F.data.startswith('delete:'))
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

    async with Bot(token=token, session=session, default=DefaultBotProperties(parse_mode='HTML')) as bot:
        try:
            await bot.set_my_commands([
                BotCommand(command='start', description='Отправить аудио'),
                BotCommand(command='history', description='📂 История'),
            ])
        except Exception:
            logger.bind(event='menu_update_failed').warning('Не удалось обновить меню команд; бот продолжает запуск')
        notification_task = asyncio.create_task(notifications(bot, app_state))
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


if __name__ == '__main__':
    asyncio.run(main())
