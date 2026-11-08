import multiprocessing
import os
import signal
from pathlib import Path
from threading import Event
from time import perf_counter

from loguru import logger

from nova_transcriber import database
from nova_transcriber.config import check_disk_space, get_max_file_size_mb, get_positive_int
from nova_transcriber.errors import MediaError
from nova_transcriber.safe_logging import configure_logging


def _transcribe(pipe, path, processed_samples, total_samples):
    try:
        from nova_transcriber import asr

        if total_samples is None:
            pipe.send(('stage', 'Определение длительности'))
            total_samples = asr.audio_samples(path)
            pipe.send(('duration', total_samples))
            pipe.recv()
        for end_sample, text in asr.transcribe_chunks(
            path, start_sample=processed_samples,
            on_status=lambda stage: pipe.send(('stage', stage)),
        ):
            pipe.send(('chunk', end_sample, text))
            pipe.recv()
        pipe.send(('finished',))
    except Exception as error:
        reason = str(error) if isinstance(error, MediaError) else 'Не удалось обработать аудио или видео'
        retryable = isinstance(error, (OSError, RuntimeError)) and not isinstance(error, (FileNotFoundError, PermissionError))
        pipe.send(('error', reason, retryable))
    finally:
        pipe.close()


def _run_task(connection, task, path, stopping, timeout):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(
        target=_transcribe,
        args=(child, path, task['processed_samples'], task['total_samples']),
        daemon=True,
    )
    started = perf_counter()
    stage_started = heartbeat = started
    stage = 'Подготовка аудио'
    identifier = task['id']
    def elapsed():
        return task['processing_time'] + perf_counter() - started

    try:
        process.start()
        child.close()
        while not stopping.is_set():
            now = perf_counter()
            if now - heartbeat >= 5:
                connection.execute('SELECT 1')
                heartbeat = now
            if now - stage_started > timeout:
                return f'Превышено время этапа «{stage}»', True, elapsed()
            if not parent.poll(0.5):
                if not process.is_alive():
                    return 'Процесс обработки неожиданно остановился', True, elapsed()
                continue
            try:
                message = parent.recv()
            except EOFError:
                return 'Процесс обработки неожиданно остановился', True, elapsed()
            if message[0] == 'stage':
                stage = message[1]
                stage_started = perf_counter()
                connection.execute('UPDATE transcriptions SET stage = %s WHERE id = %s', (stage, identifier))
            elif message[0] == 'duration':
                database.set_audio_duration(connection, identifier, message[1])
                task['total_samples'] = message[1]
                parent.send(True)
                stage_started = perf_counter()
            elif message[0] == 'chunk':
                end_sample, text = message[1:]
                if not task['processed_samples'] < end_sample <= task['total_samples']:
                    return 'Длительность исходного файла изменилась', False, elapsed()
                database.save_chunk(connection, identifier, task['processed_samples'], end_sample, text, elapsed())
                task['processed_samples'] = end_sample
                parent.send(True)
                stage_started = perf_counter()
            elif message[0] == 'finished':
                if task['processed_samples'] != task['total_samples']:
                    return 'Длительность исходного файла изменилась', False, elapsed()
                database.finish_chunks(connection, identifier, elapsed())
                return None, False, elapsed()
            elif message[0] == 'error':
                return message[1], message[2], elapsed()
        return None, True, elapsed()
    finally:
        child.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            process.close()
        parent.close()


def main():
    configure_logging()
    stopping = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    root = Path(os.environ['CORE_MEDIA_ROOT']).resolve(strict=True)
    timeout = get_positive_int('STAGE_TIMEOUT_SECONDS', 3600)
    get_max_file_size_mb()
    get_positive_int('MAX_DURATION_SECONDS', 43200)
    get_positive_int('MIN_FREE_DISK_MB', 512)
    log = logger.bind(component='Whisper Worker')
    database.init_db()
    log.bind(event='ready').info('Обработчик готов к получению задач')
    while not stopping.is_set():
        try:
            with database._connect() as connection:
                connection.autocommit = True
                task = database.claim_task(connection)
                if task is None:
                    stopping.wait(3)
                    continue
                identifier = task['id']
                task_log = log.bind(task_id=identifier)
                try:
                    key = Path(task['file_key'])
                    path = (root / key).resolve(strict=True)
                    if key.is_absolute() or not path.is_relative_to(root) or not path.is_file():
                        raise MediaError('Исходный файл недоступен')
                    check_disk_space(path.parent)
                except (OSError, ValueError) as error:
                    reason = str(error) if isinstance(error, MediaError) else 'Исходный файл недоступен'
                    database.fail_task(connection, identifier, reason, task['processing_time'])
                    continue
                task_log.bind(event='processing').info('Продолжение с {} с', task['processed_samples'] / 16000)
                if task['total_samples'] is not None and task['processed_samples'] == task['total_samples']:
                    database.finish_chunks(connection, identifier, task['processing_time'])
                    continue
                while not stopping.is_set():
                    reason, retryable, elapsed = _run_task(connection, task, path, stopping, timeout)
                    if stopping.is_set():
                        connection.execute("""UPDATE transcriptions SET processing_time = %s,
                            status = 'pending', stage = NULL WHERE id = %s AND status = 'processing'""",
                            (elapsed, identifier))
                        break
                    if reason is None:
                        task_log.bind(event='completed').info('Результат сохранён. Транскрибация завершена')
                        break
                    row = connection.execute('SELECT retry_count FROM transcriptions WHERE id = %s', (identifier,)).fetchone()
                    if not retryable or row['retry_count'] >= 3:
                        database.fail_task(connection, identifier, reason, elapsed)
                        task_log.bind(event='failed', error=reason).error('Обработка завершилась ошибкой')
                        break
                    connection.execute('UPDATE transcriptions SET retry_count = retry_count + 1, processing_time = %s WHERE id = %s', (elapsed, identifier))
                    task['processing_time'] = elapsed
                    task_log.bind(event='retry', error=reason).warning('Повтор с последнего сохранённого фрагмента')
                    stopping.wait(2 ** (row['retry_count'] + 1))
        except Exception:
            log.bind(event='worker_error', error='Ошибка подключения или сохранения состояния').error('Не удалось обработать очередь; повтор через 3 секунды')
            stopping.wait(3)
    log.bind(event='stopped').info('Обработчик остановлен')


if __name__ == '__main__':
    main()
