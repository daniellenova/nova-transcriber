import io
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from nova_transcriber import asr, core, telegram
from nova_transcriber.errors import (
    EmptyFileError, UnsupportedFormatError, UnreadableMediaError,
)
from nova_transcriber.safe_logging import configure_logging


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "recording.wav"
        self.path.write_bytes(b"broken media")

    def process(self, filename="recording.wav"):
        return core._process_recording(self.path, filename, "document", 12, None)

    def test_empty_file_uses_actual_size(self):
        self.path.write_bytes(b"")
        with patch.object(core.database, "create_recording") as create:
            with self.assertRaisesRegex(EmptyFileError, "Файл пустой"):
                self.process()
            create.assert_not_called()
        with self.assertRaises(EmptyFileError):
            asr.transcribe(self.path)

    def test_unsupported_extension_never_reaches_asr(self):
        with patch.object(core.asr, "transcribe") as transcribe:
            with patch.object(core.database, "create_recording") as create:
                with self.assertRaises(UnsupportedFormatError):
                    self.process("unsupported.xyz")
                transcribe.assert_not_called()
                create.assert_not_called()

    def test_corrupt_audio_does_not_load_model(self):
        with patch.object(asr, "_get_model") as get_model:
            with self.assertRaises(UnreadableMediaError):
                asr.transcribe(self.path)
            get_model.assert_not_called()

    def test_decodable_audio_and_mocked_speech(self):
        with wave.open(str(self.path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 1600)
        model = Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(text=" Привет, "), SimpleNamespace(text="мир! ")]),
            None,
        )
        with patch.object(asr, "_get_model", return_value=model):
            self.assertEqual(asr.transcribe(self.path), "Привет, мир!")
        self.assertEqual(model.transcribe.call_args.args[0].size, 1600)
        self.assertEqual(model.transcribe.call_args.kwargs["language"], "ru")

    def test_media_without_audio_stream_or_samples(self):
        with patch.object(asr, "_get_model") as get_model:
            with patch.object(asr, "decode_audio", side_effect=IndexError("audio stream missing")):
                with self.assertRaises(UnreadableMediaError):
                    asr.transcribe(self.path)
            with patch.object(asr, "decode_audio", return_value=SimpleNamespace(size=0)):
                with self.assertRaises(UnreadableMediaError):
                    asr.transcribe(self.path)
            get_model.assert_not_called()

    def test_failure_is_error_and_never_done(self):
        for error in (UnreadableMediaError(), RuntimeError("internal failure")):
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(core.database, "create_recording", return_value=1),
                    patch.object(core.database, "create_transcription", return_value=2),
                    patch.object(core.database, "complete_transcription") as complete,
                    patch.object(core.database, "fail_transcription") as fail,
                    patch.object(core.asr, "transcribe", side_effect=error),
                ):
                    with self.assertRaises(type(error)):
                        self.process()
                    complete.assert_not_called()
                    self.assertEqual(fail.call_args.args[0], 2)

    def test_success_is_saved(self):
        with (
            patch.object(core.database, "create_recording", return_value=1),
            patch.object(core.database, "create_transcription", return_value=2),
            patch.object(core.database, "complete_transcription") as complete,
            patch.object(core.database, "fail_transcription") as fail,
            patch.object(core.asr, "transcribe", return_value="Привет"),
        ):
            self.assertEqual(self.process("RECORDING.WAV"), "Привет")
            self.assertEqual(complete.call_args.args[:2], (2, "Привет"))
            fail.assert_not_called()

    def test_logging_redacts_exception_chain_and_keeps_traceback(self):
        token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456789"
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("test.safe_logging")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, factory)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token}):
            configure_logging()
            try:
                try:
                    raise ValueError(f"/var/lib/telegram-bot-api/{token}/music.mp3")
                except ValueError as error:
                    raise RuntimeError("decode failed") from error
            except RuntimeError:
                logger.exception("Failed: %s", token)
        logged = output.getvalue()
        self.assertNotIn(token, logged)
        self.assertIn("Traceback", logged)
        self.assertIn("ValueError", logged)
        self.assertIn("RuntimeError", logged)
        self.assertIn("[REDACTED]", logged)


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_errors_and_successful_txt(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "recording.mp3"
            path.write_bytes(b"audio")
            for error in (EmptyFileError(), UnsupportedFormatError(), UnreadableMediaError(), RuntimeError()):
                with self.subTest(error=type(error).__name__):
                    message = SimpleNamespace(
                        content_type="document",
                        document=SimpleNamespace(file_id="id", file_name=path.name, file_size=5),
                        answer=AsyncMock(), answer_document=AsyncMock(),
                    )
                    bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path=str(path))))
                    with (
                        patch.object(telegram, "FILE_ROOT", Path(directory).resolve()),
                        patch.object(core, "process_recording", side_effect=error),
                        patch.object(telegram.logging, "getLogger"),
                    ):
                        await telegram.receive_file(message, bot)
                    expected = str(error) if not isinstance(error, RuntimeError) else "Не удалось обработать файл. Попробуйте повторить позже."
                    self.assertEqual(message.answer.call_args.args[0], expected)
                    message.answer_document.assert_not_called()
            with (
                patch.object(telegram, "FILE_ROOT", Path(directory).resolve()),
                patch.object(core, "process_recording", return_value="Привет"),
            ):
                await telegram.receive_file(message, bot)
            document = message.answer_document.call_args.args[0]
            self.assertEqual(document.filename, "recording.txt")
            self.assertEqual(document.data, "Привет".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
