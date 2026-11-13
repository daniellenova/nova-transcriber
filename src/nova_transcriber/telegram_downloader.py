import asyncio
import os
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import BufferedInputFile, Message
from loguru import logger

from nova_transcriber import api_client
from nova_transcriber.config import check_disk_space, get_positive_int
from nova_transcriber.errors import MediaError
from nova_transcriber.telegram_state import TelegramState, _duration, _progress, _recording_name


FILE_ROOT = Path('/var/lib/telegram-bot-api')


async def receive_file(
    message: Message, bot: Bot, app_state: TelegramState, max_file_size_mb: Decimal = Decimal("1024")
) -> None:
    log = logger.bind(component=f"Telegram, сообщение №{message.message_id}")
    received_at = perf_counter()
    log.bind(event="file_received").info("Получено сообщение с медиафайлом")
    source_type = message.content_type
    media = getattr(message, source_type)
    received_name = _recording_name({'filename': getattr(media, 'file_name', None), 'source_type': source_type})
    received_text = f'<b>📥 Аудио получено</b>\n\n{received_name}'
    if getattr(media, 'duration', None) is not None:
        received_text += f'\n\n⏱ Длительность: {_duration(media.duration)}'
    received_text += '\n\nДобавляю запись в обработку…'
    try:
        status_message = await message.answer(
            received_text
        )
    except TelegramAPIError as error:
        log.bind(event="file_ack_failed", duration=f"{perf_counter() - received_at:.3f} с",
                 error=error.message).error("Не удалось отправить подтверждение получения файла")
        raise
    log.bind(event="file_ack_sent", duration=f"{perf_counter() - received_at:.3f} с").info(
        "Отправлено подтверждение получения файла"
    )
    max_file_size = max_file_size_mb * 1_048_576
    too_large_message = (
        f"<b>⚠️ Файл слишком большой</b>\n\nМаксимальный размер: {max_file_size_mb.normalize():f} МБ.\n\nОтправьте файл меньшего размера."
    )
    metadata_size = getattr(media, "file_size", None)
    if isinstance(metadata_size, int) and metadata_size > max_file_size:
        await status_message.edit_text(too_large_message)
        return
    duration = getattr(media, 'duration', None)
    if duration is not None and duration > get_positive_int('MAX_DURATION_SECONDS', 43200):
        await status_message.edit_text('<b>⚠️ Не удалось принять файл</b>\n\nЗапись превышает допустимую длительность.')
        return

    stage = "Запрос getFile"
    stage_started = perf_counter()
    try:
        log.bind(event="file_download_started").info(
            "Начат запрос getFile; размер по данным Telegram: {} байт; "
            "тайм-аут всего HTTP-запроса: 600 с", metadata_size if metadata_size is not None else "неизвестен"
        )
        async with app_state.download_lock:
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
    except MediaError:
        await status_message.edit_text('<b>⚠️ Не удалось подготовить запись</b>\n\nПопробуйте позже.')
        return
    except TelegramNetworkError as error:
        timed_out = isinstance(error.__cause__, asyncio.TimeoutError)
        reason = ("Истекло время ожидания загрузки файла из Telegram"
                  if timed_out else "Нарушено соединение с локальным Telegram Bot API")
        log.bind(event="file_download_failed", duration=f"{perf_counter() - stage_started:.3f} с",
                 error=error.message).error("Этап «{}»: {}", stage, reason)
        await status_message.edit_text(
            '<b>⚠️ Не удалось загрузить запись</b>\n\nОтправьте файл ещё раз.'
        )
        return
    except TelegramAPIError as error:
        log.bind(event="file_download_failed", duration=f"{perf_counter() - stage_started:.3f} с",
                 error=error.message).error(
            "Этап «{}»: Telegram Bot API отклонил запрос на получение файла", stage
        )
        await status_message.edit_text(
            '<b>⚠️ Не удалось загрузить запись</b>\n\nОтправьте файл ещё раз.'
        )
        return
    except (OSError, ValueError) as error:
        reason = (str(error) if isinstance(error, ValueError)
                  else f"Ошибка доступа к локальному файлу, код: {error.errno}")
        log.bind(event="file_access_failed", duration=f"{perf_counter() - stage_started:.3f} с").error(
            "Этап «{}»: {}", stage, reason
        )
        await status_message.edit_text(
            '<b>⚠️ Не удалось подготовить запись</b>\n\nОтправьте файл ещё раз.'
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
    except ValueError:
        await status_message.edit_text('<b>⚠️ Не удалось принять файл</b>\n\nОтправьте аудио или видео в поддерживаемом формате и допустимого размера.')
    except Exception:
        await status_message.edit_text('<b>⚠️ Не удалось добавить запись</b>\n\nПроверьте историю перед повторной отправкой.')
    else:
        log.bind(task_id=task['id'], event='task_registered').info('Задача передана в Core API; отслеживание продолжится через уведомления')


async def _send_result(app_state, bot, chat, identifier, *, manual=False):
    if identifier in app_state.send_busy:
        return False
    app_state.send_busy.add(identifier)
    tracked = False
    try:
        record = await asyncio.to_thread(api_client.record, identifier)
        if record.get('text') is None or record.get('deleting'):
            raise ValueError('Результат пока недоступен')
        if record['status'] == 'completed' and record['delivery_state'] != 'sent':
            claimed = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/delivery',
                                            {'action': 'start', 'manual': manual})
            if not claimed['ok']:
                return False
            tracked = True
        elif record['status'] == 'completed' and not manual:
            return False
        text = record['text']
        filename = Path(record['filename'].replace('\\', '/')).stem
        if record.get('partial'):
            filename += '_неполный'
            text = f"Неполный результат\nОбработано: {_progress(record, saved=True)}\n\n{text}"
        try:
            await bot.send_document(chat, BufferedInputFile(text.encode('utf-8'), filename=filename + '.txt'),
                                    caption=(f"<b>📄 Неполный результат · №{record['number']}</b>" if record.get('partial') else
                                             f"<b>📄 Расшифровка · №{record['number']}</b>") + '\n\n' + _recording_name(record),
                                    request_timeout=60)
        except TelegramAPIError:
            if tracked:
                await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/delivery', {'action': 'failed'})
            raise
        if tracked:
            app_state.confirmed_sends.add(identifier)
            await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/delivery', {'action': 'sent'})
            app_state.confirmed_sends.discard(identifier)
        return True
    finally:
        app_state.send_busy.discard(identifier)


