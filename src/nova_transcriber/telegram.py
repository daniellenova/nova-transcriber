import asyncio
import logging
import os
import re
import signal
import secrets
from html import escape
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError

from nova_transcriber import api_client
from loguru import logger

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    BotCommand,
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
SEND_BUSY = set()
CONFIRMED_SENDS = set()
CALLBACK_BUSY = set()
CALLBACK_LAST = {}


async def start(message: Message) -> None:
    await message.answer(
        '<b>🎙️ Nova Transcriber</b>\n\nОтправьте аудио — я превращу речь в текст.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='📂 История', callback_data='history:page:0'),
        ]]),
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


def _clock(value):
    hours, seconds = divmod(round(value), 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def _progress(record: dict, *, saved=False, bar=False, compact=False) -> str:
    total = record.get('total_samples')
    if not total:
        return 'Подготавливаю'
    processed = record.get('processed_samples', 0)
    if not saved and record['status'] == 'processing':
        processed = max(processed, record.get('progress_samples', 0))
    processed = min(processed, total)
    percent = min(99.9 if not saved and record['status'] == 'processing' else 100, processed * 100 / total)
    text = f'{percent:.1f}% · {_duration(processed / 16000)} из {_duration(total / 16000)}'
    if compact:
        filled = int(percent / 10)
        meter = '█' * filled + '░' * (10 - filled) + ' ' if bar else ''
        text = f'{meter}<b>{percent:.1f}%</b>\n\n⏱ Обработано: {_clock(processed / 16000)} из {_clock(total / 16000)}'
    return text


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
    # Разрываем автоматическое распознавание ссылок в отображаемом имени.
    return escape(filename.translate(str.maketrans({symbol: symbol + '\u200b' for symbol in '.:@'})))


def _status(record):
    if record.get('deleting'):
        return '🗑️ Удаляется'
    if record['status'] == 'processing':
        return '🎙️ Распознаю речь' if record.get('stage') == 'Распознавание' else '⚙️ Подготавливаю'
    return {'pending': '⏳ В очереди', 'completed': '✅ Готово',
            'failed': '⚠️ Не удалось обработать', 'paused': '🚫 Обработка отменена'}[record['status']]


def _recording_summary(record, *, progress_bar=False):
    title = _status(record)
    if record['status'] == 'completed' and record['delivery_state'] in {'failed', 'sending'} and record['id'] not in SEND_BUSY:
        title = '⚠️ Не удалось отправить TXT'
    lines = [f"<b>{title} · №{record['number']}</b>", '', _recording_name(record), '']
    if record['status'] in {'pending', 'processing', 'paused', 'failed'} and record.get('total_samples'):
        lines.append(_progress(record, bar=progress_bar, compact=True))
    elif record.get('duration') is not None:
        lines.append(f"⏱ Длительность: {_duration(record['duration'])}")
    if record['status'] == 'processing' and record.get('stage') != 'Распознавание':
        lines.append('\nПроверяю аудио перед распознаванием…')
    if record['status'] == 'completed':
        if record['delivery_state'] == 'sent':
            lines.append('\n📄 TXT отправлен и доступен в истории.')
        elif record['id'] in SEND_BUSY or record['delivery_state'] == 'pending':
            lines.append('\nTXT готов. Отправляю…')
        else:
            lines.append('\nРасшифровка сохранена. Попробуйте отправить TXT ещё раз.')
    elif record['status'] == 'failed':
        lines.append('\nВозникла ошибка при распознавании. Попробуйте ещё раз.')
    return '\n'.join(lines)


def _task_keyboard(record, *, history=True):
    rows = []
    identifier = record['id']
    if not record.get('deleting'):
        if record['status'] == 'completed':
            label = '📄 Скачать TXT' if record['delivery_state'] == 'sent' else '📄 Отправить TXT'
            rows.append([InlineKeyboardButton(text=label, callback_data=f'history:txt:{identifier}')])
        elif record.get('has_partial_result'):
            rows.append([InlineKeyboardButton(text='📄 Скачать неполный TXT', callback_data=f'history:txt:{identifier}')])
        names = {'cancel': '✖️ Отменить', 'resume': '▶️ Продолжить', 'retry': '🔄 Повторить'}
        for action in record['actions']:
            rows.append([InlineKeyboardButton(text=names[action], callback_data=f'history:{action}:{identifier}')])
    if history:
        rows.append([InlineKeyboardButton(text='📂 История', callback_data='history:show:0')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _history_label(record):
    status = _status(record)
    if record['status'] == 'failed' and not record.get('deleting'):
        status = '⚠️ Ошибка'
    if record['status'] == 'processing' and record.get('total_samples'):
        status = '🎙️ ' + _progress(record).split(' · ', 1)[0]
    duration = _duration(record['duration']) if record.get('duration') is not None else 'длительность уточняется'
    return f"<b>№{record['number']}</b> · {status} · {duration}\n{_recording_name(record)}"


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
            title = '🗑️ Удалить запись?' if len(deletion['selected']) == 1 else f"🗑️ Удалить выбранные записи ({len(deletion['selected'])})?"
            text = (f'<b>{title}</b>\n\n'
                    'Из истории будут удалены записи, их тексты и сохранённые части распознавания.\n\n'
                    'Исходные аудиофайлы и ранее отправленные TXT останутся.\n\n'
                    '⏳ Активная обработка будет остановлена перед удалением.\n\n<b>Это действие нельзя отменить.</b>')
            rows.append([InlineKeyboardButton(text='🗑️ Да, удалить', callback_data=f'delete:{token}:commit:0'),
                         InlineKeyboardButton(text='Отмена', callback_data=f'delete:{token}:cancel:0')])
        elif identifier is None or deletion is not None:
            result = await asyncio.to_thread(api_client.request, f'/history?page={page}')
            page = result['page']
            text = f"<b>📂 История</b>\n\nВсего записей: <b>{result['total']}</b>"
            if not result['total']:
                text = '<b>📂 История</b>\n\nЗдесь пока нет записей.\nОтправьте аудио, чтобы создать первую расшифровку.'
            if deletion is not None:
                token = deletion['token']
                available = [r for r in result['items'] if not r.get('deleting')]
                deletion['allowed'] = {r['id'] for r in available}
                text = f"<b>🗑️ Удаление записей</b>\n\nВыбрано: <b>{len(deletion['selected'])}</b>"
                text += '\n\n' + '\n\n'.join(
                    f"{'☑️' if r['id'] in deletion['selected'] else '☐'} <b>№{r['number']}</b> · {_recording_name(r)}" for r in result['items'])
                if available:
                    rows.append([InlineKeyboardButton(
                        text=f"{'☑️ ' if r['id'] in deletion['selected'] else ''}№{r['number']}",
                        callback_data=f"delete:{token}:toggle:{r['id']}") for r in available])
            elif result['items']:
                text += '\n\n' + '\n\n'.join(_history_label(record) for record in result['items'])
                rows.append([InlineKeyboardButton(text=f"№{record['number']}",
                    callback_data=f"history:open:{record['id']}:{page}") for record in result['items']])
            navigation = []
            if page:
                navigation.append(InlineKeyboardButton(text='◀️ Назад', callback_data=(
                    f'delete:{token}:page:{page - 1}' if deletion is not None else f'history:page:{page - 1}')))
            navigation.append(InlineKeyboardButton(text=f"{page + 1} / {result['pages']}", callback_data='history:noop:0'))
            if page + 1 < result['pages']:
                navigation.append(InlineKeyboardButton(text='Далее ▶️', callback_data=(
                    f'delete:{token}:page:{page + 1}' if deletion is not None else f'history:page:{page + 1}')))
            rows.append(navigation)
            if deletion is not None:
                if deletion['selected']:
                    rows.append([InlineKeyboardButton(text=f"🗑️ Удалить выбранные ({len(deletion['selected'])})",
                        callback_data=f'delete:{token}:confirm:0')])
                rows.append([InlineKeyboardButton(text='✖️ Отмена', callback_data=f'delete:{token}:cancel:0')])
            elif result['total']:
                rows.append([InlineKeyboardButton(text='🗑️ Выбрать для удаления', callback_data=f'history:delete:0:{page}')])
        else:
            text = _recording_summary(record)
            rows.extend(_task_keyboard(record, history=False).inline_keyboard)
            if not record.get('deleting'):
                rows.append([InlineKeyboardButton(text='🗑️ Удалить запись', callback_data=f'history:delete:{identifier}:{page}')])
            rows.append([InlineKeyboardButton(text='◀️ К истории', callback_data=f'history:page:{page}')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        signature = (message.chat.id, message.message_id, text, keyboard.model_dump_json())
        if new:
            message = await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
            signature = (message.chat.id, message.message_id, text, keyboard.model_dump_json())
        elif HISTORY_PANEL.get('signature') != signature:
            try:
                await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
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
        or parts[1] not in {'txt', 'info', 'page', 'show', 'open', 'cancel', 'resume', 'retry', 'noop', 'delete'}
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or not 0 <= int(parts[2]) <= 9223372036854775807
        or (parts[1] not in {'page', 'show', 'noop', 'delete'} and int(parts[2]) == 0)
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
        if action == 'show':
            await _show_history(callback.message, new=True)
            return
        if action == 'delete':
            await _show_history(callback.message, page, identifier or None, delete_start=True)
            return
        if action == 'page':
            await _show_history(callback.message, identifier)
            return
        if action in {'cancel', 'resume', 'retry'}:
            await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/{action}', {})
            panel_message = HISTORY_PANEL.get('message')
            if panel_message is not None and (panel_message.chat.id, panel_message.message_id) == (callback.message.chat.id, callback.message.message_id):
                await _show_history(callback.message, HISTORY_PANEL['page'], identifier)
            else:
                record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
                await callback.message.edit_text(_recording_summary(record, progress_bar=True), reply_markup=_task_keyboard(record), parse_mode='HTML')
            return
        if action != 'txt':
            await _show_history(callback.message, page, identifier)
            return
        record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        try:
            panel_message = HISTORY_PANEL.get('message')
            if panel_message is not None and (panel_message.chat.id, panel_message.message_id) == (callback.message.chat.id, callback.message.message_id):
                await _show_history(callback.message, page, identifier)
        except Exception:
            logging.getLogger(__name__).exception('Не удалось обновить карточку')
        return
    except Exception:
        logging.getLogger(__name__).exception("Не удалось получить транскрибацию")
        await callback.answer('Не удалось загрузить данные. Попробуйте позже.', show_alert=True)
        return
    if record['status'] != 'completed' and not record.get('has_partial_result'):
        await callback.answer("Сохранённых частей пока нет.", show_alert=True)
        return

    try:
        await _send_result(callback.bot, callback.message.chat.id, identifier, manual=True)
    except (TelegramAPIError, ValueError):
        await callback.answer('Не удалось отправить TXT. Попробуйте ещё раз.', show_alert=True)
    await _show_history()


async def receive_file(
    message: Message, bot: Bot, max_file_size_mb: Decimal = Decimal("1024")
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


async def _send_result(bot, chat, identifier, *, manual=False):
    if identifier in SEND_BUSY:
        return False
    SEND_BUSY.add(identifier)
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
            CONFIRMED_SENDS.add(identifier)
            await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/delivery', {'action': 'sent'})
            CONFIRMED_SENDS.discard(identifier)
        return True
    finally:
        SEND_BUSY.discard(identifier)


async def callback_guard(handler, event, data):
    key = (event.from_user.id, event.data)
    now = perf_counter()
    if key in CALLBACK_BUSY or now - CALLBACK_LAST.get(key, 0) < 2:
        await event.answer('Действие уже выполнено или выполняется')
        return
    CALLBACK_BUSY.add(key)
    try:
        return await handler(event, data)
    finally:
        CALLBACK_BUSY.discard(key)
        CALLBACK_LAST[key] = perf_counter()
        if len(CALLBACK_LAST) > 256:
            CALLBACK_LAST.pop(min(CALLBACK_LAST, key=CALLBACK_LAST.get))


async def notifications(bot):
    previous = {}
    while True:
        try:
            records = await asyncio.to_thread(api_client.request, '/notifications')
        except Exception:
            logger.bind(component='Telegram Adapter', event='notification_retry').warning('Не удалось получить уведомления')
            await asyncio.sleep(5)
            continue
        active = {record['id'] for record in records}
        CONFIRMED_SENDS.intersection_update(active)
        previous = {key: value for key, value in previous.items() if key in active}
        for record in records:
            identifier = record['id']
            try:
                if identifier in CONFIRMED_SENDS:
                    await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/delivery', {'action': 'sent'})
                    CONFIRMED_SENDS.discard(identifier)
                    record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
                if record['status'] == 'completed' and record['delivery_state'] == 'pending':
                    try:
                        await _send_result(bot, record['telegram_chat_id'], identifier)
                    except TelegramAPIError:
                        logger.bind(component='Telegram Adapter', task_id=identifier, event='delivery_failed').warning('Отправка TXT не подтверждена')
                    record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
                text = _recording_summary(record, progress_bar=True)
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
                if record['status'] in {'completed', 'failed', 'paused'} and identifier not in SEND_BUSY:
                    await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/notified', {})
            except Exception:
                logger.bind(component='Telegram Adapter', task_id=identifier, event='notification_retry').warning('Не удалось обновить карточку')
        try:
            await _show_history()
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

    dispatcher = Dispatcher()
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
        await bot.set_my_commands([
            BotCommand(command='start', description='Отправить аудио'),
            BotCommand(command='history', description='📂 История'),
        ])
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

