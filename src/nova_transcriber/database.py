import os
from typing import Any

import psycopg
from loguru import logger
from psycopg.rows import dict_row


_TASK_COLUMNS = """t.id, t.model, t.status, t.processing_time, t.created_at,
    t.started_at, t.finished_at, t.error_reason, t.telegram_chat_id, t.telegram_message_id,
    t.processed_samples, t.total_samples, t.stage,
    r.filename, r.source_type, r.file_size, r.duration"""


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        row_factory=dict_row,
        options="-c timezone=UTC -c statement_timeout=15000 -c lock_timeout=10000",
        connect_timeout=5,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=3,
        tcp_user_timeout=30000,
    )


def init_db() -> None:
    with _connect() as connection:
        # Core и Worker могут одновременно запускаться с пустой базой.
        connection.execute("SELECT pg_advisory_xact_lock(19790520, 1)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                filename TEXT NOT NULL,
                source_type TEXT NOT NULL,
                file_size BIGINT NOT NULL CHECK (file_size >= 0),
                duration DOUBLE PRECISION CHECK (duration >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS transcriptions (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                recording_id BIGINT NOT NULL REFERENCES recordings(id),
                model TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
                processing_time DOUBLE PRECISION CHECK (processing_time >= 0),
                text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("LOCK TABLE transcriptions IN ACCESS EXCLUSIVE MODE")
        connection.execute("""
            ALTER TABLE transcriptions
                ADD COLUMN IF NOT EXISTS file_key TEXT,
                ADD COLUMN IF NOT EXISTS request_key TEXT,
                ADD COLUMN IF NOT EXISTS delivery_notified BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS error_reason TEXT,
                ADD COLUMN IF NOT EXISTS user_id BIGINT,
                ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT,
                ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT,
                ADD COLUMN IF NOT EXISTS processed_samples BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS total_samples BIGINT,
                ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS stage TEXT
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS transcription_chunks (
                transcription_id BIGINT NOT NULL REFERENCES transcriptions(id),
                start_sample BIGINT NOT NULL CHECK (start_sample >= 0),
                end_sample BIGINT NOT NULL CHECK (end_sample > start_sample),
                text TEXT NOT NULL,
                PRIMARY KEY (transcription_id, start_sample)
            )
        """)
        connection.execute("""
            ALTER TABLE transcriptions
                DROP CONSTRAINT IF EXISTS transcriptions_status_check
        """)
        connection.execute("""
            UPDATE transcriptions SET status = CASE status
                WHEN 'done' THEN 'completed' WHEN 'error' THEN 'failed' ELSE status END
            WHERE status IN ('done', 'error')
        """)
        connection.execute("""
            ALTER TABLE transcriptions ADD CONSTRAINT transcriptions_status_check
                CHECK (status IN ('pending', 'processing', 'completed', 'failed'))
        """)
        connection.execute("""
            CREATE OR REPLACE FUNCTION guard_transcription_status() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.id IS DISTINCT FROM OLD.id THEN
                    RAISE EXCEPTION 'Идентификатор задачи нельзя изменить';
                END IF;
                IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                    (OLD.status = 'pending' AND NEW.status IN ('processing', 'failed')) OR
                    (OLD.status = 'processing' AND NEW.status IN ('pending', 'completed', 'failed'))
                ) THEN
                    RAISE EXCEPTION 'Недопустимый переход состояния задачи';
                END IF;
                IF NEW.status = 'completed' AND NEW.text IS NULL AND (
                    OLD.status IS DISTINCT FROM NEW.status OR OLD.text IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'Результат задачи не сохранён';
                END IF;
                RETURN NEW;
            END; $$
        """)
        connection.execute("""
            CREATE OR REPLACE TRIGGER transcription_status_guard
            BEFORE UPDATE ON transcriptions FOR EACH ROW
            EXECUTE FUNCTION guard_transcription_status()
        """)


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
                WHERE file_key IS NOT NULL AND status IN ('pending', 'processing') AND id > %s
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
                    started_at = COALESCE(started_at, clock_timestamp()), stage = 'Подготовка аудио'
                WHERE id = %s
            """, (row['id'],))
            return row


def set_audio_duration(connection, identifier, total_samples):
    with connection.transaction():
        connection.execute("""UPDATE transcriptions SET total_samples = %s,
            retry_count = 0 WHERE id = %s""", (total_samples, identifier))
        connection.execute("""UPDATE recordings SET duration = %s WHERE id =
            (SELECT recording_id FROM transcriptions WHERE id = %s)""",
            (total_samples / 16000, identifier))


def save_chunk(connection, identifier, start_sample, end_sample, text, processing_time):
    with connection.transaction():
        connection.execute("""INSERT INTO transcription_chunks
            (transcription_id, start_sample, end_sample, text) VALUES (%s, %s, %s, %s)""",
            (identifier, start_sample, end_sample, text))
        connection.execute("""UPDATE transcriptions SET processed_samples = %s,
            processing_time = %s, retry_count = 0 WHERE id = %s AND status = 'processing'""",
            (end_sample, processing_time, identifier))


def finish_chunks(connection, identifier, processing_time):
    with connection.transaction():
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


def fail_task(connection, identifier, reason, processing_time):
    connection.execute("""UPDATE transcriptions SET status = 'failed', stage = NULL,
        error_reason = %s, processing_time = %s, finished_at = clock_timestamp()
        WHERE id = %s AND status = 'processing'""", (reason, processing_time, identifier))


def create_task(filename, source_type, file_size, duration, user_id, file_key, metadata=None):
    with _connect() as connection:
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
        return connection.execute(
            """INSERT INTO transcriptions (recording_id, model, status, user_id, file_key, request_key, telegram_chat_id, telegram_message_id)
            VALUES (%s, 'large-v3', 'pending', %s, %s, %s, %s, %s) RETURNING id, status""",
            (recording["id"], user_id, file_key, key, metadata.get('telegram_chat_id'), metadata.get('telegram_message_id')),
        ).fetchone()


def notifications(user_id):
    with _connect() as connection:
        return connection.execute(f'''SELECT {_TASK_COLUMNS} FROM transcriptions t
            JOIN recordings r ON r.id=t.recording_id
            WHERE t.user_id=%s AND t.telegram_chat_id IS NOT NULL
            AND NOT (t.file_key IS NULL AND t.status = 'completed')
            AND NOT t.delivery_notified ORDER BY t.id''', (user_id,)).fetchall()


def acknowledge(identifier, user_id):
    with _connect() as connection:
        connection.execute("UPDATE transcriptions SET delivery_notified=TRUE WHERE id=%s AND user_id=%s AND status IN ('completed','failed')", (identifier, user_id))


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
    with _connect() as connection:
        return connection.execute(
            f"""
            SELECT {_TASK_COLUMNS}{', t.text' if include_text else ''}
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            WHERE t.id = %s AND t.user_id = %s
            """,
            (transcription_id, user_id),
        ).fetchone()

