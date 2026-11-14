from typing import Any

from nova_transcriber.db_schema import _connect, _renumber, init_db
from nova_transcriber.db_worker import (
    claim_task,
    set_audio_duration,
    update_progress,
    save_chunk,
    finish_chunks,
    fail_task,
    worker_session,
    reset_progress,
    task_status,
    set_stage,
    save_paused_time,
    requeue_task,
    task_retry_count,
    record_retry,
)


_TASK_COLUMNS = """t.id, t.model, t.status, t.processing_time, t.created_at,
    t.started_at, t.finished_at, t.error_reason, t.telegram_chat_id, t.telegram_message_id,
    t.processed_samples, t.progress_samples, t.total_samples, t.stage, t.file_key, t.delete_requested, t.number, t.delivery_state,
    r.filename, r.source_type, r.file_size, r.duration"""


def change_task(identifier, user_id, action):
    if action not in {'cancel', 'resume', 'retry'}:
        raise ValueError('Неизвестное действие')
    with _connect() as connection:
        row = connection.execute('SELECT status, file_key, delete_requested FROM transcriptions WHERE id = %s AND user_id = %s FOR UPDATE',
                                 (identifier, user_id)).fetchone()
        if row is None:
            raise ValueError('Задача не найдена')
        if row['delete_requested']:
            raise ValueError('Запись уже удаляется')
        if action == 'cancel':
            if row['status'] == 'paused':
                return
            if row['status'] not in {'pending', 'processing'}:
                raise ValueError('Запись уже завершена')
            connection.execute("""UPDATE transcriptions SET status = 'paused', stage = NULL,
                delivery_notified = FALSE WHERE id = %s""", (identifier,))
            return
        expected = 'failed' if action == 'retry' else 'paused'
        if row['status'] in {'pending', 'processing'}:
            return
        if row['status'] != expected:
            raise ValueError('Состояние записи изменилось. Это действие уже недоступно')
        if not row['file_key']:
            raise ValueError('Исходный файл недоступен. Отправьте запись заново')
        acquired = connection.execute('SELECT pg_try_advisory_xact_lock(%s) AS acquired', (-identifier,)).fetchone()['acquired']
        if not acquired:
            raise ValueError('Обработчик ещё останавливается. Повторите через несколько секунд')
        connection.execute("""UPDATE transcriptions SET status = 'pending', retry_count = 0,
            error_reason = NULL, finished_at = NULL, stage = NULL, delivery_notified = FALSE,
            delivery_state = 'pending'
            WHERE id = %s""", (identifier,))


def history_page(user_id, page):
    with _connect() as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        total = connection.execute('SELECT count(*) AS total FROM transcriptions WHERE user_id = %s', (user_id,)).fetchone()['total']
        pages = max(1, (total + 4) // 5)
        page = min(page, pages - 1)
        records = connection.execute(f"""SELECT {_TASK_COLUMNS} FROM transcriptions t
            JOIN recordings r ON r.id = t.recording_id WHERE t.user_id = %s
            ORDER BY t.number DESC, t.id DESC LIMIT 5 OFFSET %s""", (user_id, page * 5)).fetchall()
        return {'total': total, 'page': page, 'pages': pages, 'items': records}


def delete_tasks(user_id, identifiers):
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 1000
            or any(type(value) is not int or not 0 < value <= 9223372036854775807 for value in identifiers)):
        raise ValueError('Выберите от 1 до 1000 записей с корректными ID')
    identifiers = sorted(set(identifiers))
    with _connect() as connection:
        records = connection.execute('SELECT id, user_id FROM transcriptions WHERE id = ANY(%s) ORDER BY id FOR UPDATE',
                                     (identifiers,)).fetchall()
        if any(record['user_id'] != user_id for record in records):
            raise ValueError('Нельзя удалить чужие записи')
        connection.execute("""UPDATE transcriptions SET delete_requested = TRUE,
            delivery_notified = TRUE, stage = NULL,
            status = CASE WHEN status IN ('pending', 'processing') THEN 'paused' ELSE status END
            WHERE id = ANY(%s) AND user_id = %s""", (identifiers, user_id))
    cleanup_deleted_tasks()


def cleanup_deleted_tasks():
    with _connect() as connection:
        # Создание и удаление не должны одновременно менять пользовательские номера.
        connection.execute('SELECT pg_advisory_xact_lock(19790520, 2)')
        records = connection.execute("""SELECT id, recording_id FROM transcriptions
            WHERE delete_requested ORDER BY id FOR UPDATE SKIP LOCKED""").fetchall()
        ready = []
        for record in records:
            acquired = connection.execute('SELECT pg_try_advisory_xact_lock(%s) AS acquired', (-record['id'],)).fetchone()['acquired']
            if acquired:
                ready.append(record)
        if not ready:
            return
        identifiers = [record['id'] for record in ready]
        recording_ids = [record['recording_id'] for record in ready]
        connection.execute('DELETE FROM transcription_chunks WHERE transcription_id = ANY(%s)', (identifiers,))
        connection.execute('DELETE FROM transcriptions WHERE id = ANY(%s)', (identifiers,))
        _renumber(connection)
        connection.execute('SELECT id FROM recordings WHERE id = ANY(%s) ORDER BY id FOR UPDATE', (recording_ids,)).fetchall()
        connection.execute('''DELETE FROM recordings r WHERE r.id = ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM transcriptions t WHERE t.recording_id = r.id)''', (recording_ids,))


def create_task(filename, source_type, file_size, duration, user_id, file_key, metadata=None, *, model):
    with _connect() as connection:
        connection.execute('SELECT pg_advisory_xact_lock(19790520, 2)')
        metadata = metadata or {}
        key = metadata.get('request_key')
        if key:
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (key,))
            existing = connection.execute('SELECT id, status FROM transcriptions WHERE user_id = %s AND request_key = %s', (user_id, key)).fetchone()
            if existing:
                return existing
        recording = connection.execute(
            """INSERT INTO recordings (filename, source_type, file_size, duration)
            VALUES (%s, %s, %s, %s) RETURNING id""",
            (filename, source_type, file_size, duration),
        ).fetchone()
        number = connection.execute('SELECT COALESCE(max(number), 0) + 1 AS number FROM transcriptions WHERE user_id = %s',
                                    (user_id,)).fetchone()['number']
        return connection.execute(
            """INSERT INTO transcriptions (recording_id, model, status, user_id, file_key, request_key, telegram_chat_id, telegram_message_id, number, created_at)
            VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, clock_timestamp()) RETURNING id, status""",
            (recording["id"], model, user_id, file_key, key, (metadata.get('notification') or {}).get('chat_id'), (metadata.get('notification') or {}).get('message_id'), number),
        ).fetchone()


def notifications(user_id):
    with _connect() as connection:
        return connection.execute(f'''SELECT {_TASK_COLUMNS} FROM transcriptions t
            JOIN recordings r ON r.id=t.recording_id
            WHERE t.user_id=%s AND t.telegram_chat_id IS NOT NULL
            AND NOT (t.file_key IS NULL AND t.status = 'completed')
            AND NOT t.delivery_notified AND NOT t.delete_requested ORDER BY t.id''', (user_id,)).fetchall()


def acknowledge(identifier, user_id):
    with _connect() as connection:
        connection.execute("UPDATE transcriptions SET delivery_notified=TRUE WHERE id=%s AND user_id=%s AND status IN ('completed','failed','paused')", (identifier, user_id))


def delivery(identifier, user_id, action, manual=False):
    with _connect() as connection:
        row = connection.execute('SELECT status, delivery_state, delete_requested FROM transcriptions WHERE id = %s AND user_id = %s FOR UPDATE',
                                 (identifier, user_id)).fetchone()
        if row is None or row['status'] != 'completed' or row['delete_requested']:
            raise ValueError('Готовый результат недоступен')
        if action == 'start':
            if row['delivery_state'] == 'sent' or (not manual and row['delivery_state'] != 'pending'):
                return False
            state = 'sending'
        elif action in {'sent', 'failed'}:
            if row['delivery_state'] != 'sending':
                return False
            state = action
        else:
            raise ValueError('Недопустимое действие')
        connection.execute('UPDATE transcriptions SET delivery_state = %s, delivery_notified = FALSE WHERE id = %s', (state, identifier))
        return True


def complete_transcription(
    transcription_id: int, text: str, processing_time: float
) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE transcriptions SET text = %s, processing_time = %s,
                status = 'completed', finished_at = clock_timestamp()
            WHERE id = %s AND status = 'processing'
            """,
            (text, processing_time, transcription_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Транскрибация не найдена: {transcription_id}")


def fail_transcription(
    transcription_id: int, processing_time: float | None = None,
    error_reason: str = "Не удалось обработать файл",
) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE transcriptions SET status = 'failed', processing_time = %s,
                finished_at = clock_timestamp(), error_reason = %s, text = NULL
            WHERE id = %s AND status IN ('pending', 'processing')
            """,
            (processing_time, error_reason, transcription_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Транскрибация не найдена: {transcription_id}")


def get_recent_transcriptions(limit: int = 10, user_id: int | None = None) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("Количество транскрибаций должно быть положительным")
    with _connect() as connection:
        return connection.execute(
            f"""
            SELECT {_TASK_COLUMNS}
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            WHERE t.user_id = %s
            ORDER BY t.created_at DESC, t.id DESC LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()


def get_transcription(transcription_id: int, user_id: int | None = None, *, include_text=False) -> dict[str, Any] | None:
    text_column = """, CASE WHEN t.status = 'completed' THEN t.text ELSE COALESCE((
        SELECT string_agg(c.text, ' ' ORDER BY c.start_sample) FILTER (WHERE c.text <> '')
        FROM transcription_chunks c WHERE c.transcription_id = t.id
    ), '') END AS text""" if include_text else ''
    with _connect() as connection:
        return connection.execute(
            f"""
            SELECT {_TASK_COLUMNS}{text_column}
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            WHERE t.id = %s AND t.user_id = %s
            """,
            (transcription_id, user_id),
        ).fetchone()
