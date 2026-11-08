from functools import cache
from pathlib import Path
from collections.abc import Callable
from itertools import chain
import gc

import av
import numpy as np
from faster_whisper import WhisperModel
from av.error import FFmpegError

from nova_transcriber.config import check_disk_space, get_max_file_size_mb, get_positive_int
from nova_transcriber.errors import EmptyFileError, MediaError, UnreadableMediaError


SAMPLE_RATE = 16000
CHUNK_SAMPLES = 300 * SAMPLE_RATE


@cache
def _get_model() -> WhisperModel:
    model_path = Path("models/large-v3").resolve()
    if not model_path.is_dir():
        raise FileNotFoundError(f"Каталог модели не найден: {model_path}")
    return WhisperModel(
        str(model_path), device="cpu", compute_type="int8", local_files_only=True
    )


def _audio_frames(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Аудиофайл не найден: {path}")
    if path.stat().st_size == 0:
        raise EmptyFileError()

    if path.stat().st_size > get_max_file_size_mb() * 1_048_576:
        raise MediaError("Файл превышает допустимый размер")
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
    try:
        with av.open(str(path), metadata_errors="ignore") as container:
            if not container.streams.audio:
                raise UnreadableMediaError()
            for frame in chain(container.decode(audio=0), [None]):
                if frame is not None:
                    frame.pts = None
                for output in resampler.resample(frame):
                    yield output.to_ndarray().reshape(-1)
    except (FFmpegError, ValueError, EOFError, IndexError) as error:
        if isinstance(error, MediaError):
            raise
        raise UnreadableMediaError() from error
    finally:
        del resampler
        gc.collect()


def audio_samples(path) -> int:
    limit = get_positive_int("MAX_DURATION_SECONDS", 43200) * SAMPLE_RATE
    total = 0
    for samples in _audio_frames(path):
        total += len(samples)
        if total > limit:
            raise MediaError("Запись превышает допустимую длительность")
    if not total:
        raise UnreadableMediaError()
    return total


def _audio_chunks(path, start_sample):
    buffer = np.empty(CHUNK_SAMPLES, dtype=np.float32)
    used = position = 0
    limit = get_positive_int("MAX_DURATION_SECONDS", 43200) * SAMPLE_RATE
    for samples in _audio_frames(path):
        end = position + len(samples)
        if end > limit:
            raise MediaError("Запись превышает допустимую длительность")
        offset = min(len(samples), max(0, start_sample - position))
        position = end
        while offset < len(samples):
            count = min(CHUNK_SAMPLES - used, len(samples) - offset)
            buffer[used:used + count] = samples[offset:offset + count]
            used += count
            offset += count
            if used == CHUNK_SAMPLES:
                yield buffer
                used = 0
    if used:
        yield buffer[:used]


def transcribe_chunks(path, *, start_sample=0, on_status=None):
    def status(text):
        if on_status is not None:
            on_status(text)

    status("Загрузка модели")
    model = _get_model()
    status("Чтение аудио")
    position = start_sample
    for audio in _audio_chunks(path, start_sample):
        check_disk_space(Path(path).parent)
        status("Распознавание")
        segments, _ = model.transcribe(
            audio, language="ru", task="transcribe", vad_filter=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        position += len(audio)
        yield position, text
        del segments
        gc.collect()
        status("Чтение аудио")


def transcribe(path: str | Path, *, on_status: Callable[[str], None] | None = None) -> str:
    audio_samples(path)
    return " ".join(text for _, text in transcribe_chunks(path, on_status=on_status) if text)
