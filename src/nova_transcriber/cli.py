import argparse
from pathlib import Path

from nova_transcriber.asr import transcribe


def main() -> None:
    parser = argparse.ArgumentParser(description="Транскрибация аудиофайла в TXT.")
    parser.add_argument("path", type=Path, help="Путь к аудиофайлу")
    args = parser.parse_args()
    output = args.path.with_suffix(".txt")
    if output == args.path:
        parser.error("Входной файл не должен иметь расширение .txt")

    try:
        text = transcribe(args.path, on_status=lambda message: print(message, flush=True))
        output.write_text(text, encoding="utf-8")
    except Exception as error:
        parser.exit(1, f"Ошибка транскрибации: {error}\n")

    print(f"Готово. Текст сохранён: {output}")
