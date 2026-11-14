import os

import psycopg
from psycopg.rows import dict_row

from nova_transcriber.stages import LABELS


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


def _renumber(connection):
    connection.execute("""WITH numbered AS (
        SELECT id, row_number() OVER (PARTITION BY user_id ORDER BY created_at, id) AS number
        FROM transcriptions
    ) UPDATE transcriptions t SET number = n.number FROM numbered n
        WHERE t.id = n.id AND t.number IS DISTINCT FROM n.number""")


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
                ADD COLUMN IF NOT EXISTS progress_samples BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS total_samples BIGINT,
                ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS stage TEXT,
                ADD COLUMN IF NOT EXISTS delete_requested BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS number BIGINT,
                ADD COLUMN IF NOT EXISTS delivery_state TEXT
        """)
        connection.execute("""UPDATE transcriptions SET delivery_state = CASE
            WHEN status = 'completed' AND delivery_notified THEN 'sent' ELSE 'pending' END
            WHERE delivery_state IS NULL""")
        connection.execute("""ALTER TABLE transcriptions
            ALTER COLUMN delivery_state SET DEFAULT 'pending',
            ALTER COLUMN delivery_state SET NOT NULL""")
        for code, label in LABELS.items():
            connection.execute('UPDATE transcriptions SET stage = %s WHERE stage = %s', (code, label))
        connection.execute('DROP INDEX IF EXISTS transcriptions_user_number')
        connection.execute('DROP TABLE IF EXISTS transcription_counters')
        _renumber(connection)
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
                CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'paused'))
        """)
        connection.execute("""
            CREATE OR REPLACE FUNCTION guard_transcription_status() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.id IS DISTINCT FROM OLD.id THEN
                    RAISE EXCEPTION 'Идентификатор задачи нельзя изменить';
                END IF;
                IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                    (OLD.status = 'pending' AND NEW.status IN ('processing', 'failed', 'paused')) OR
                    (OLD.status = 'processing' AND NEW.status IN ('pending', 'completed', 'failed', 'paused')) OR
                    (OLD.status IN ('paused', 'failed') AND NEW.status = 'pending')
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

