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
                status TEXT NOT NULL CHECK (status IN ('processing', 'done', 'error')),
                processing_time DOUBLE PRECISION CHECK (processing_time >= 0),
                text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


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


def create_transcription(recording_id: int, model: str) -> int:
    with _connect() as connection:
        row = connection.execute(
            """
            INSERT INTO transcriptions (recording_id, model, status)
            VALUES (%s, %s, 'processing') RETURNING id
            """,
            (recording_id, model),
        ).fetchone()
        return row["id"]


def complete_transcription(
    transcription_id: int, text: str, processing_time: float
) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE transcriptions SET text = %s, processing_time = %s, status = 'done'
            WHERE id = %s
            """,
            (text, processing_time, transcription_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Транскрибация не найдена: {transcription_id}")


def fail_transcription(
    transcription_id: int, processing_time: float | None = None
) -> None:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE transcriptions SET status = 'error', processing_time = %s
            WHERE id = %s
            """,
            (processing_time, transcription_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Транскрибация не найдена: {transcription_id}")


def get_recent_transcriptions(limit: int = 10) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("Количество транскрибаций должно быть положительным")
    with _connect() as connection:
        return connection.execute(
            """
            SELECT t.*, r.filename, r.source_type, r.file_size, r.duration
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            ORDER BY t.created_at DESC, t.id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()


def get_transcription(transcription_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        return connection.execute(
            """
            SELECT t.*, r.filename, r.source_type, r.file_size, r.duration
            FROM transcriptions AS t
            JOIN recordings AS r ON r.id = t.recording_id
            WHERE t.id = %s
            """,
            (transcription_id,),
        ).fetchone()
