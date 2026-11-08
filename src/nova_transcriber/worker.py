import os
import signal
from pathlib import Path
from threading import Event
from time import perf_counter

from loguru import logger

from nova_transcriber import asr, database
from nova_transcriber.errors import MediaError
from nova_transcriber.safe_logging import configure_logging


def main():
    configure_logging()
    stopping = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    root = Path(os.environ['CORE_MEDIA_ROOT']).resolve(strict=True)
    log = logger.bind(component='Whisper Worker')
    database.init_db()
    log.bind(event='ready').info('Обработчик готов к получению задач')
    while not stopping.is_set():
        try:
            with database._connect() as connection:
                task = database.claim_task(connection)
                if task is None:
                    stopping.wait(3)
                    continue
                identifier = task['id']
                task_log = log.bind(task_id=identifier)
                started = perf_counter()
                task_log.bind(event='processing').info('Начата транскрибация')
                try:
                    key = Path(task['file_key'])
                    path = (root / key).resolve(strict=True)
                    if key.is_absolute() or not path.is_relative_to(root) or not path.is_file():
                        raise ValueError('Недопустимая ссылка на файл')
                    text = asr.transcribe(path)
                    database.complete_transcription(identifier, text, perf_counter() - started)
                    task_log.bind(event='completed', duration=f'{perf_counter() - started:.3f} с').info('Результат сохранён. Транскрибация завершена')
                except Exception as error:
                    reason = str(error) if isinstance(error, MediaError) else 'Не удалось обработать аудио или видео'
                    database.fail_transcription(identifier, perf_counter() - started, reason)
                    task_log.bind(event='failed', error=reason, duration=f'{perf_counter() - started:.3f} с').error('Обработка завершилась ошибкой')
        except Exception:
            log.bind(event='worker_error', error='Ошибка подключения или сохранения состояния').error('Не удалось обработать очередь; повтор через 3 секунды')
            stopping.wait(3)
    log.bind(event='stopped').info('Обработчик остановлен')


if __name__ == '__main__':
    main()
