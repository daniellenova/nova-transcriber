from functools import cache
from pathlib import Path
from collections.abc import Callable

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from av.error import FFmpegError

from nova_transcriber.errors import EmptyFileError, UnreadableMediaError


@cache
def _get_model() -> WhisperModel:
    model_path = Path("models/large-v3").resolve()
    if not model_path.is_dir():
        raise FileNotFoundError(f"Каталог модели не найден: {model_path}")
    return WhisperModel(
        str(model_path), device="cpu", compute_type="int8", local_files_only=True
    )


def transcribe(path: str | Path, *, on_status: Callable[[str], None] | None = None) -> str:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Аудиофайл не найден: {path}")
    if path.stat().st_size == 0:
        raise EmptyFileError()

    with path.open("rb") as source:
        try:
            audio = decode_audio(source, sampling_rate=16000)
        except (FFmpegError, ValueError, EOFError, IndexError) as error:
            raise UnreadableMediaError() from error
    if audio.size == 0:
        raise UnreadableMediaError()

    if on_status is not None:
        on_status("Загрузка модели…")
    model = _get_model()
    if on_status is not None:
        on_status("Распознавание…")

    segments, _ = model.transcribe(
        audio,
        language="ru",
        task="transcribe",
        vad_filter=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()
