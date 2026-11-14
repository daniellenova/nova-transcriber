from contextlib import contextmanager

from loguru import logger

from nova_transcriber.db_schema import _connect
from nova_transcriber.stages import PREPARING


INTERRUPTED_REASON = "Обработка прервана из-за остановки или перезапуска сервиса"


def claim_task(connection):
    # Сеансовая блокировка остаётся после фиксации processing и освобождается
    # PostgreSQL при разрыве соединения. Все записи Worker идут через эту сессию.
    with connection.transaction():
        after_id = 0
        while True:
            row = connection.execute("""
                SELECT id, status, file_key, processed_samples, total_samples,
                    retry_count, COALESCE(processing_time, 0) AS processing_time
                FROM transcriptions
                WHERE file_key IS NOT NULL AND NOT delete_requested
                    AND status IN ('pending', 'processing') AND id > %s
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            """, (after_id,)).fetchone()
            if row is None:
                return None
            after_id = row['id']
            locked = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (-row['id'],)
            ).fetchone()['acquired']
            if not locked:
                continue
            if row['status'] == 'processing':
                if row['retry_count'] >= 3:
                    connection.execute("""
                        UPDATE transcriptions SET status = 'failed', error_reason = %s,
                            finished_at = clock_timestamp() WHERE id = %s
                    """, ("Превышено число попыток восстановления", row['id']))
                    connection.execute("SELECT pg_advisory_unlock(%s)", (-row['id'],))
                    continue
                connection.execute("""
                    UPDATE transcriptions SET retry_count = retry_count + 1 WHERE id = %s
                """, (row['id'],))
                logger.bind(component="Очередь", task_id=row['id'], event="interrupted",
                            error=INTERRUPTED_REASON).warning("Продолжение с сохранённого фрагмента")
            connection.execute("""
                UPDATE transcriptions SET status = 'processing',
                    started_at = COALESCE(started_at, clock_timestamp()), stage = %s,
                    progress_samples = processed_samples
                WHERE id = %s
            """, (PREPARING, row['id']))
            return row


def set_audio_duration(connection, identifier, total_samples):
    with connection.transaction():
        if connection.execute('SELECT status FROM transcriptions WHERE id = %s FOR UPDATE', (identifier,)).fetchone()['status'] != 'processing':
            return False
        connection.execute("""UPDATE transcriptions SET total_samples = %s,
            retry_count = 0 WHERE id = %s""", (total_samples, identifier))
        connection.execute("""UPDATE recordings SET duration = %s WHERE id =
            (SELECT recording_id FROM transcriptions WHERE id = %s)""",
            (total_samples / 16000, identifier))
        return True


def update_progress(connection, identifier, samples):
    connection.execute("""UPDATE transcriptions SET progress_samples =
        LEAST(total_samples, GREATEST(processed_samples, progress_samples, %s))
        WHERE id = %s AND status = 'processing'""", (samples, identifier))


def save_chunk(connection, identifier, start_sample, end_sample, text, processing_time):
    with connection.transaction():
        if connection.execute('SELECT status FROM transcriptions WHERE id = %s FOR UPDATE', (identifier,)).fetchone()['status'] != 'processing':
            return False
        connection.execute("""INSERT INTO transcription_chunks
            (transcription_id, start_sample, end_sample, text) VALUES (%s, %s, %s, %s)""",
            (identifier, start_sample, end_sample, text))
        connection.execute("""UPDATE transcriptions SET processed_samples = %s, progress_samples = %s,
            processing_time = %s, retry_count = 0 WHERE id = %s AND status = 'processing'""",
            (end_sample, end_sample, processing_time, identifier))
        return True


def finish_chunks(connection, identifier, processing_time):
    with connection.transaction():
        if connection.execute('SELECT status FROM transcriptions WHERE id = %s FOR UPDATE', (identifier,)).fetchone()['status'] != 'processing':
            return False
        cursor = connection.execute("""UPDATE transcriptions SET
            text = (SELECT COALESCE(string_agg(text, ' ' ORDER BY start_sample)
                FILTER (WHERE text <> ''), '') FROM transcription_chunks
                WHERE transcription_id = %s),
            status = 'completed', stage = NULL, error_reason = NULL,
            processing_time = %s, finished_at = clock_timestamp()
            WHERE id = %s AND status = 'processing' AND total_samples = processed_samples""",
            (identifier, processing_time, identifier))
        if cursor.rowcount != 1:
            raise ValueError("Не все фрагменты сохранены")
        connection.execute("DELETE FROM transcription_chunks WHERE transcription_id = %s", (identifier,))
        return True


def fail_task(connection, identifier, reason, processing_time):
    connection.execute("""UPDATE transcriptions SET status = 'failed', stage = NULL,
        error_reason = %s, processing_time = %s, finished_at = clock_timestamp()
        WHERE id = %s AND status = 'processing'""", (reason, processing_time, identifier))


@contextmanager
def worker_session():
    with _connect() as connection:
        connection.autocommit = True
        yield connection


def reset_progress(connection, identifier):
    connection.execute("UPDATE transcriptions SET progress_samples = processed_samples WHERE id = %s AND status = 'processing'", (identifier,))


def task_status(connection, identifier):
    return connection.execute('SELECT status FROM transcriptions WHERE id = %s', (identifier,)).fetchone()['status']


def set_stage(connection, identifier, stage):
    connection.execute("UPDATE transcriptions SET stage = %s WHERE id = %s AND status = 'processing'", (stage, identifier))


def save_paused_time(connection, identifier, elapsed):
    connection.execute("UPDATE transcriptions SET processing_time = %s WHERE id = %s AND status = 'paused'", (elapsed, identifier))


def requeue_task(connection, identifier, elapsed):
    connection.execute("""UPDATE transcriptions SET processing_time = %s,
        status = 'pending', stage = NULL WHERE id = %s AND status = 'processing'""", (elapsed, identifier))


def task_retry_count(connection, identifier):
    return connection.execute('SELECT retry_count FROM transcriptions WHERE id = %s', (identifier,)).fetchone()['retry_count']


def record_retry(connection, identifier, elapsed):
    connection.execute('UPDATE transcriptions SET retry_count = retry_count + 1, processing_time = %s WHERE id = %s', (elapsed, identifier))
