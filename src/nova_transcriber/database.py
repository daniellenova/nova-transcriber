import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        row_factory=dict_row,
        options="-c timezone=UTC",
        connect_timeout=5,
    )


def init_db() -> None:
    with _connect() as connection:
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
                ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS error_reason TEXT,
                ADD COLUMN IF NOT EXISTS user_id BIGINT,
                ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT,
                ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT,
                ADD COLUMN IF NOT EXISTS interruption_notified BOOLEAN NOT NULL DEFAULT FALSE
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
                    (OLD.status = 'processing' AND NEW.status IN ('completed', 'failed'))
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


def recover_transcriptions(user_id: int) -> list[int]:
    with _connect() as connection:
        rows = connection.execute(
            """
            UPDATE transcriptions SET status = 'failed',
                finished_at = clock_timestamp(), error_reason = %s, text = NULL
            WHERE user_id = %s AND status IN ('pending', 'processing')
            RETURNING id
            """,
            (INTERRUPTED_REASON, user_id),
        ).fetchall()
    return [row["id"] for row in rows]


def save_status_message(transcription_id: int, chat_id: int, message_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            """UPDATE transcriptions SET telegram_chat_id = %s, telegram_message_id = %s
            WHERE id = %s""", (chat_id, message_id, transcription_id),
        )


def get_interrupted_notifications(user_id: int) -> list[dict[str, Any]]:
    with _connect() as connection:
        return connection.execute(
            """SELECT id, user_id, telegram_chat_id, telegram_message_id
            FROM transcriptions
            WHERE user_id = %s AND status = 'failed' AND error_reason = %s
                AND NOT interruption_notified
            ORDER BY id""", (user_id, INTERRUPTED_REASON),
        ).fetchall()


def mark_interruption_notified(transcription_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE transcriptions SET interruption_notified = TRUE WHERE id = %s",
            (transcription_id,),
        )


def create_recording(
    filename: str, source_type: str, file_size: int, duration: float | None = None
) -> int:
    with _connect() as connection:
        row = connection.execute(
            """
            INSERT INTO recordings (filename, source_type, file_size, duration)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (filename, source_type, file_size, duration),
        ).fetchone()
        return row["id"]


def create_transcription(recording_id: int, model: str, user_id: int | None = None) -> int:
    with _connect() as connection:
        row = connection.execute(
            """
            INSERT INTO transcriptions (recording_id, model, status, user_id)
            VALUES (%s, %s, 'pending', %s) RETURNING id
            """,
            (recording_id, model, user_id),
        ).fetchone()
        return row["id"]


def start_transcription(transcription_id: int) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            """UPDATE transcriptions SET status = 'processing', started_at = clock_timestamp()
            WHERE id = %s AND status = 'pending'""", (transcription_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("Задача не найдена или переход состояния запрещён")


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
            """
            SELECT t.*, CASE t.status WHEN 'completed' THEN 'done'
                WHEN 'failed' THEN 'error' ELSE t.status END AS status,
                r.filename, r.source_type, r.file_size, r.duration
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            WHERE t.user_id = %s
            ORDER BY t.created_at DESC, t.id DESC LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()


def get_transcription(transcription_id: int, user_id: int | None = None) -> dict[str, Any] | None:
    with _connect() as connection:
        return connection.execute(
            """
            SELECT t.*, CASE t.status WHEN 'completed' THEN 'done'
                WHEN 'failed' THEN 'error' ELSE t.status END AS status,
                r.filename, r.source_type, r.file_size, r.duration
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            WHERE t.id = %s AND t.user_id = %s
            """,
            (transcription_id, user_id),
        ).fetchone()
