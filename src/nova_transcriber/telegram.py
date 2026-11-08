import asyncio
import logging
import os
import math
import re
import signal
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError

from nova_transcriber import api_client
from loguru import logger

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
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
HISTORY_LOCK = asyncio.Lock()
HISTORY_PANEL = {}


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


def _partial_keyboard(record):
    if not record.get('has_partial_result'):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Скачать неполный TXT', callback_data=f"history:txt:{record['id']}"),
    ]])


def _recording_name(record: dict) -> str:
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
    return filename


def _recording_summary(record: dict) -> str:
    filename = _recording_name(record)
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
    lines = [f"Запись №{record['number']}", f"Файл: {filename}", f"Дата: {date}"]
    for key, label in (("duration", "Длительность записи"),
                       ("processing_time", "Время обработки")):
        value = record.get(key)
        if value is not None and math.isfinite(value) and value >= 0:
            lines.append(f"{label}: {_duration(value)}")
    statuses = {
        "pending": "Ожидает обработки", "processing": "Обрабатывается",
        "completed": "Завершена",
        "failed": "Ошибка",
        "paused": "Приостановлена",
    }
    state = 'Удаляется' if record.get('deleting') else statuses.get(record.get('status'), 'Неизвестно')
    lines.append(f"Состояние: {state}")
    if record.get('status') in {'pending', 'processing', 'failed', 'paused'} and record.get('total_samples'):
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


def _history_label(record):
    status = {
        'completed': '✅ Готово', 'pending': '⏳ В очереди',
        'processing': '⏳ Распознаётся', 'paused': '⏸ Приостановлена',
        'failed': '⚠️ Ошибка',
    }[record['status']]
    if record['status'] == 'processing' and record.get('total_samples'):
        status += f" · {min(100, record['processed_samples'] * 100 // record['total_samples'])}%"
    if record.get('deleting'):
        status = '🗑 Удаляется'
    duration = _duration(record['duration']) if record.get('duration') is not None else 'длительность уточняется'
    return f"№{record['number']}. {_recording_name(record)}\n{status} · {duration}"


async def _show_history(message=None, page=0, identifier=None, *, new=False, delete_start=False, delete_event=None):
    async with HISTORY_LOCK:
        deletion = None
        if message is None or delete_event is not None:
            if not HISTORY_PANEL:
                if delete_event is not None:
                    raise ValueError('Выбор устарел. Откройте историю заново')
                return
            if delete_event is not None:
                stored = HISTORY_PANEL.get('deletion')
                previous = HISTORY_PANEL['message']
                if (not stored or stored['token'] != delete_event[0]
                        or (message.chat.id, message.message_id) != (previous.chat.id, previous.message_id)):
                    raise ValueError('Выбор устарел. Откройте историю заново')
            message, page, identifier = (HISTORY_PANEL[key] for key in ('message', 'page', 'identifier'))
            if HISTORY_PANEL.get('deletion'):
                deletion = {**HISTORY_PANEL['deletion'], 'selected': set(HISTORY_PANEL['deletion']['selected'])}
        if delete_start:
            if identifier is not None:
                await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
            deletion = {'token': secrets.token_hex(4), 'selected': {identifier} if identifier is not None else set(),
                        'mode': 'confirm' if identifier is not None else 'select', 'origin': identifier, 'allowed': set()}
        if delete_event is not None:
            _, action, value = delete_event
            if action == 'cancel':
                identifier = deletion['origin']
                deletion = None
            elif action == 'commit':
                if deletion['mode'] != 'confirm' or not deletion['selected']:
                    raise ValueError('Сначала выберите записи и подтвердите удаление')
                await asyncio.to_thread(api_client.request, '/tasks/delete', {'ids': sorted(deletion['selected'])})
                identifier = None
                deletion = None
            elif deletion['mode'] != 'select':
                raise ValueError('Подтвердите удаление или нажмите «Отмена»')
            elif action == 'page':
                page = value
            elif action == 'toggle':
                if value not in deletion['allowed']:
                    raise ValueError('Страница изменилась. Выберите запись ещё раз')
                if value in deletion['selected']:
                    deletion['selected'].remove(value)
                else:
                    if len(deletion['selected']) >= 1000:
                        raise ValueError('Можно выбрать не более 1000 записей за один раз')
                    deletion['selected'].add(value)
            elif action == 'confirm':
                if not deletion['selected']:
                    raise ValueError('Сначала выберите записи')
                deletion['mode'] = 'confirm'
        record = None
        if identifier is not None and deletion is None:
            try:
                record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
            except HTTPError as error:
                if error.code != 404:
                    raise
                identifier = None
        rows = []
        if deletion is not None and deletion['mode'] == 'confirm':
            token = deletion['token']
            title = '🗑 Удалить запись?' if len(deletion['selected']) == 1 else f"🗑 Удалить выбранные записи ({len(deletion['selected'])})?"
            text = (f'{title}\n\n'
                    'Записи исчезнут из истории.\nСохранённые тексты и промежуточные части будут удалены.\n\n'
                    'Исходные аудиофайлы не удаляются. Уже отправленные сообщения и TXT останутся в Telegram.\n\n'
                    'Активные задачи сначала будут остановлены.\nЭто действие нельзя отменить.')
            rows.append([InlineKeyboardButton(text='🗑 Да, удалить', callback_data=f'delete:{token}:commit:0'),
                         InlineKeyboardButton(text='Отмена', callback_data=f'delete:{token}:cancel:0')])
        elif identifier is None or deletion is not None:
            result = await asyncio.to_thread(api_client.request, f'/history?page={page}')
            page = result['page']
            text = f"История транскрибаций\nЗаписей: {result['total']} · Выберите нужную"
            if not result['total']:
                text = 'История транскрибаций пока пуста'
            if deletion is not None:
                token = deletion['token']
                available = [r for r in result['items'] if not r.get('deleting')]
                deletion['allowed'] = {r['id'] for r in available}
                text = f"🗑 Выберите записи для удаления\nВыбрано: {len(deletion['selected'])}"
                text += '\n\n' + '\n\n'.join(
                    f"{'☑' if r['id'] in deletion['selected'] else '☐'} {_history_label(r)}" for r in result['items'])
                if available:
                    rows.append([InlineKeyboardButton(
                        text=f"{'☑ ' if r['id'] in deletion['selected'] else ''}{r['number']}",
                        callback_data=f"delete:{token}:toggle:{r['id']}") for r in available])
            elif result['items']:
                text += '\n\n' + '\n\n'.join(_history_label(record) for record in result['items'])
                rows.append([InlineKeyboardButton(text=str(record['number']),
                    callback_data=f"history:open:{record['id']}:{page}") for record in result['items']])
            navigation = []
            if page:
                navigation.append(InlineKeyboardButton(text='Назад', callback_data=(
                    f'delete:{token}:page:{page - 1}' if deletion is not None else f'history:page:{page - 1}')))
            navigation.append(InlineKeyboardButton(text=f"{page + 1} / {result['pages']}", callback_data='history:noop:0'))
            if page + 1 < result['pages']:
                navigation.append(InlineKeyboardButton(text='Далее', callback_data=(
                    f'delete:{token}:page:{page + 1}' if deletion is not None else f'history:page:{page + 1}')))
            rows.append(navigation)
            if deletion is not None:
                if deletion['selected']:
                    rows.append([InlineKeyboardButton(text=f"🗑 Удалить выбранные ({len(deletion['selected'])})",
                        callback_data=f'delete:{token}:confirm:0')])
                rows.append([InlineKeyboardButton(text='Отмена', callback_data=f'delete:{token}:cancel:0')])
            elif result['total']:
                rows.append([InlineKeyboardButton(text='🗑 Удалить записи', callback_data=f'history:delete:0:{page}')])
        else:
            text = _recording_summary(record)
            if record['status'] == 'completed' or record.get('has_partial_result'):
                rows.append([InlineKeyboardButton(
                    text='📄 Скачать TXT' if record['status'] == 'completed' else '📄 Скачать неполный TXT',
                    callback_data=f'history:txt:{identifier}')])
            names = {'cancel': 'Отменить', 'resume': 'Продолжить', 'retry': 'Повторить'}
            for action in record['actions']:
                rows.append([InlineKeyboardButton(text=names[action], callback_data=f'history:{action}:{identifier}:{page}')])
            if not record.get('deleting'):
                rows.append([InlineKeyboardButton(text='🗑 Удалить запись', callback_data=f'history:delete:{identifier}:{page}')])
            rows.append([InlineKeyboardButton(text='К списку', callback_data=f'history:page:{page}')])
        updated_at = datetime.now(timezone.utc).astimezone(
            ZoneInfo(os.environ.get('DISPLAY_TIMEZONE', 'Europe/Moscow'))
        ).strftime('%d.%m.%Y, %H:%M:%S')
        text += f'\n\n──────────\nОбновлено: {updated_at}'
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        signature = (message.chat.id, message.message_id, text, keyboard.model_dump_json())
        if new:
            message = await message.answer(text, parse_mode=None, reply_markup=keyboard)
            signature = (message.chat.id, message.message_id, text, keyboard.model_dump_json())
        elif HISTORY_PANEL.get('signature') != signature:
            try:
                await message.edit_text(text, parse_mode=None, reply_markup=keyboard)
            except TelegramAPIError as error:
                if 'message is not modified' not in error.message.lower():
                    if isinstance(error, TelegramBadRequest):
                        HISTORY_PANEL.clear()
                    raise
        HISTORY_PANEL.update(message=message, page=page, identifier=identifier, signature=signature, deletion=deletion)


async def history(message: Message) -> None:
    try:
        await _show_history(message, new=True)
    except Exception:
        logging.getLogger(__name__).exception('Не удалось получить историю')
        await message.answer('Не удалось загрузить историю. Попробуйте позже.')


async def delete_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or '').split(':')
    if (len(parts) != 4 or not re.fullmatch(r'[0-9a-f]{8}', parts[1])
            or parts[2] not in {'toggle', 'page', 'confirm', 'commit', 'cancel'}
            or not parts[3].isascii() or not parts[3].isdigit()
            or not 0 <= int(parts[3]) <= 9223372036854775807
            or (parts[2] == 'toggle' and int(parts[3]) == 0)
            or (parts[2] == 'page' and int(parts[3]) > 2147483647)):
        await callback.answer('Некорректный выбор', show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer('Сообщение недоступно', show_alert=True)
        return
    await callback.answer()
    try:
        await _show_history(callback.message, delete_event=(parts[1], parts[2], int(parts[3])))
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
    except Exception:
        logging.getLogger(__name__).exception('Не удалось выполнить удаление')
        await callback.answer('Не удалось выполнить действие. Попробуйте позже.', show_alert=True)


async def history_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) not in {3, 4}
        or parts[1] not in {'txt', 'info', 'page', 'open', 'cancel', 'resume', 'retry', 'noop', 'delete'}
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or not 0 <= int(parts[2]) <= 9223372036854775807
        or (parts[1] not in {'page', 'noop', 'delete'} and int(parts[2]) == 0)
        or (parts[1] == 'page' and int(parts[2]) > 2147483647)
        or (len(parts) == 4 and (not parts[3].isascii() or not parts[3].isdigit() or not 0 <= int(parts[3]) <= 2147483647))
    ):
        await callback.answer("Некорректный идентификатор.", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return

    await callback.answer()
    action = parts[1]
    if action == 'noop':
        return
    identifier = int(parts[2])
    page = int(parts[3]) if len(parts) == 4 else 0
    try:
        if action == 'delete':
            await _show_history(callback.message, page, identifier or None, delete_start=True)
            return
        if action == 'page':
            await _show_history(callback.message, identifier)
            return
        if action in {'cancel', 'resume', 'retry'}:
            await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/{action}', {})
        if action != 'txt':
            await _show_history(callback.message, page, identifier)
            return
        record = await asyncio.to_thread(api_client.record, identifier)
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        try:
            await _show_history(callback.message, page, identifier)
        except Exception:
            logging.getLogger(__name__).exception('Не удалось обновить карточку')
        return
    except Exception:
        logging.getLogger(__name__).exception("Не удалось получить транскрибацию")
        await callback.answer('Не удалось загрузить данные. Попробуйте позже.', show_alert=True)
        return
    if record.get("text") is None:
        await callback.answer("Сохранённых частей пока нет.", show_alert=True)
        return

    if parts[1] == "txt":
        output_name = Path(record["filename"].replace("\\", "/")).stem + ".txt"
        text = record['text']
        partial = record.get('partial', False)
        if partial:
            output_name = Path(output_name).stem + '_неполный.txt'
            text = f"Неполный результат\nОбработано: {_progress(record)}\n\n{text}"
        try:
            await callback.message.answer_document(
                BufferedInputFile(text.encode("utf-8"), filename=output_name),
                caption='Неполный результат' if partial else None,
            )
        except TelegramAPIError:
            await callback.answer("Не удалось отправить TXT. Попробуйте позже.", show_alert=True)
        return


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
                number = record['number']
                status = record['status']
                chat = record['telegram_chat_id']
                async def edit(text):
                    try:
                        await bot.edit_message_text(text, chat_id=chat, message_id=record['telegram_message_id'],
                                                    reply_markup=_partial_keyboard(record))
                    except TelegramAPIError as error:
                        return 'message is not modified' in error.message.lower()
                    return True
                if status in {'pending', 'processing'}:
                    text = (f'Задача №{number}: {_progress(record)}' if status == 'processing'
                            else f'Задача №{number}: ожидает обработки')
                    if previous.get(identifier) != text and perf_counter() - updated_at.get(identifier, 0) >= 15:
                        if await edit(text):
                            previous[identifier] = text
                            updated_at[identifier] = perf_counter()
                    continue
                previous.pop(identifier, None)
                updated_at.pop(identifier, None)
                final_text = (f'Задача №{number}: готово. Результат отправлен.' if status == 'completed'
                              else _recording_summary(record))
                if identifier not in delivered:
                    if status == 'completed':
                        result = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/result')
                        await edit(f'Задача №{number}: речь распознана. Отправляю результат…')
                        filename = Path(record['filename'].replace('\\', '/')).stem + '.txt'
                        await bot.send_document(chat, BufferedInputFile(result['text'].encode('utf-8'), filename=filename), caption=f'Задача №{number}: завершена. Результат отправлен')
                        delivered.add(identifier)
                    else:
                        if not await edit(final_text):
                            await bot.send_message(chat, final_text, reply_markup=_partial_keyboard(record))
                        delivered.add(identifier)
                if status == 'completed' and not await edit(final_text):
                    await bot.send_message(chat, final_text)
                    logger.bind(component='Telegram Adapter', task_id=identifier, event='status_edit_failed').warning('Итоговый статус отправлен отдельным сообщением')
                await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/notified', {})
                delivered.discard(identifier)
            except Exception:
                logger.bind(component='Telegram Adapter', task_id=record['id'], event='notification_retry').warning('Не удалось доставить или подтвердить уведомление; попытка будет повторена')
        try:
            await _show_history()
        except Exception:
            logger.bind(component='Telegram Adapter', event='history_refresh_failed').warning('Не удалось обновить открытую историю')
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

