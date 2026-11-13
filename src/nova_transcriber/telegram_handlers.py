import asyncio
import logging
import re
import secrets
from time import perf_counter
from urllib.error import HTTPError

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from nova_transcriber import api_client
from nova_transcriber.telegram_downloader import _send_result
from nova_transcriber.telegram_state import (
    TelegramState, _history_label, _recording_name, _recording_summary, _task_keyboard,
)


async def start(message: Message) -> None:
    await message.answer(
        '<b>🎙️ Nova Transcriber</b>\n\nОтправьте аудио — я превращу речь в текст.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='📂 История', callback_data='history:page:0'),
        ]]),
    )


async def _show_history(app_state, message=None, page=0, identifier=None, *, new=False, delete_start=False, delete_event=None):
    async with app_state.history_lock:
        deletion = None
        if message is None or delete_event is not None:
            if not app_state.history_panel:
                if delete_event is not None:
                    raise ValueError('Выбор устарел. Откройте историю заново')
                return
            if delete_event is not None:
                stored = app_state.history_panel.get('deletion')
                previous = app_state.history_panel['message']
                if (not stored or stored['token'] != delete_event[0]
                        or (message.chat.id, message.message_id) != (previous.chat.id, previous.message_id)):
                    raise ValueError('Выбор устарел. Откройте историю заново')
            message, page, identifier = (app_state.history_panel[key] for key in ('message', 'page', 'identifier'))
            if app_state.history_panel.get('deletion'):
                deletion = {**app_state.history_panel['deletion'], 'selected': set(app_state.history_panel['deletion']['selected'])}
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
            text = _recording_summary(record, app_state)
            rows.extend(_task_keyboard(record, history=False).inline_keyboard)
            if not record.get('deleting'):
                rows.append([InlineKeyboardButton(text='🗑️ Удалить запись', callback_data=f'history:delete:{identifier}:{page}')])
            rows.append([InlineKeyboardButton(text='◀️ К истории', callback_data=f'history:page:{page}')])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        signature = (message.chat.id, message.message_id, text, keyboard.model_dump_json())
        if new:
            message = await message.answer(text, parse_mode='HTML', reply_markup=keyboard)
            signature = (message.chat.id, message.message_id, text, keyboard.model_dump_json())
        elif app_state.history_panel.get('signature') != signature:
            try:
                await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
            except TelegramAPIError as error:
                if 'message is not modified' not in error.message.lower():
                    if isinstance(error, TelegramBadRequest):
                        app_state.history_panel.clear()
                    raise
        app_state.history_panel.update(message=message, page=page, identifier=identifier, signature=signature, deletion=deletion)


async def history(message: Message, app_state: TelegramState) -> None:
    try:
        await _show_history(app_state, message, new=True)
    except Exception:
        logging.getLogger(__name__).exception('Не удалось получить историю')
        await message.answer('Не удалось загрузить историю. Попробуйте позже.')


async def delete_callback(callback: CallbackQuery, app_state: TelegramState) -> None:
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
        await _show_history(app_state, callback.message, delete_event=(parts[1], parts[2], int(parts[3])))
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
    except Exception:
        logging.getLogger(__name__).exception('Не удалось выполнить удаление')
        await callback.answer('Не удалось выполнить действие. Попробуйте позже.', show_alert=True)


async def history_callback(callback: CallbackQuery, app_state: TelegramState) -> None:
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
            await _show_history(app_state, callback.message, new=True)
            return
        if action == 'delete':
            await _show_history(app_state, callback.message, page, identifier or None, delete_start=True)
            return
        if action == 'page':
            await _show_history(app_state, callback.message, identifier)
            return
        if action in {'cancel', 'resume', 'retry'}:
            await asyncio.to_thread(api_client.request, f'/tasks/{identifier}/{action}', {})
            panel_message = app_state.history_panel.get('message')
            if panel_message is not None and (panel_message.chat.id, panel_message.message_id) == (callback.message.chat.id, callback.message.message_id):
                await _show_history(app_state, callback.message, app_state.history_panel['page'], identifier)
            else:
                record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
                await callback.message.edit_text(_recording_summary(record, app_state, progress_bar=True), reply_markup=_task_keyboard(record), parse_mode='HTML')
            return
        if action != 'txt':
            await _show_history(app_state, callback.message, page, identifier)
            return
        record = await asyncio.to_thread(api_client.request, f'/tasks/{identifier}')
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        try:
            panel_message = app_state.history_panel.get('message')
            if panel_message is not None and (panel_message.chat.id, panel_message.message_id) == (callback.message.chat.id, callback.message.message_id):
                await _show_history(app_state, callback.message, page, identifier)
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
        await _send_result(app_state, callback.bot, callback.message.chat.id, identifier, manual=True)
    except (TelegramAPIError, ValueError):
        await callback.answer('Не удалось отправить TXT. Попробуйте ещё раз.', show_alert=True)
    await _show_history(app_state)


async def callback_guard(handler, event, data):
    app_state = data['app_state']
    key = (event.from_user.id, event.data)
    now = perf_counter()
    if key in app_state.callback_busy or now - app_state.callback_last.get(key, 0) < 2:
        await event.answer('Действие уже выполнено или выполняется')
        return
    app_state.callback_busy.add(key)
    try:
        return await handler(event, data)
    finally:
        app_state.callback_busy.discard(key)
        app_state.callback_last[key] = perf_counter()
        if len(app_state.callback_last) > 256:
            app_state.callback_last.pop(min(app_state.callback_last, key=app_state.callback_last.get))


