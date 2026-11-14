import asyncio
import re
from dataclasses import dataclass, field
from html import escape

from nova_transcriber.stages import TRANSCRIBING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass
class TelegramState:
    download_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history_panel: dict = field(default_factory=dict)
    send_busy: set = field(default_factory=set)
    confirmed_sends: set = field(default_factory=set)
    callback_busy: set = field(default_factory=set)
    callback_last: dict = field(default_factory=dict)


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
        return '🎙️ Распознаю речь' if record.get('stage') == TRANSCRIBING else '⚙️ Подготавливаю'
    return {'pending': '⏳ В очереди', 'completed': '✅ Готово',
            'failed': '⚠️ Не удалось обработать', 'paused': '🚫 Обработка отменена'}[record['status']]


def _recording_summary(record, app_state, *, progress_bar=False):
    title = _status(record)
    if record['status'] == 'completed' and record['delivery_state'] in {'failed', 'sending'} and record['id'] not in app_state.send_busy:
        title = '⚠️ Не удалось отправить TXT'
    lines = [f"<b>№{record['number']} · {title}</b>", '', f"<u>{_recording_name(record)}</u>", '']
    if record['status'] in {'pending', 'processing', 'paused', 'failed'} and record.get('total_samples'):
        lines.append(_progress(record, bar=progress_bar, compact=True))
    elif record.get('duration') is not None:
        lines.append(f"⏱ Длительность: {_duration(record['duration'])}")
    if record['status'] == 'processing' and record.get('stage') != TRANSCRIBING:
        lines.append('\nПроверяю аудио перед распознаванием…')
    if record['status'] == 'completed':
        if record['delivery_state'] == 'sent':
            lines.append('\n📄 TXT отправлен и доступен в истории')
        elif record['id'] in app_state.send_busy or record['delivery_state'] == 'pending':
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
    return f"<b>№{record['number']}</b> · <u>{_recording_name(record)}</u>\n{status} · {duration}"


