from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nova_transcriber import asr, core, database, telegram


class FileSizeConfigurationTests(unittest.TestCase):
    def test_default_is_100_mib(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(telegram._read_max_file_size_mb() * 1_048_576, 104_857_600)

    def test_positive_integer_and_fraction(self):
        for value in ("1", "100", "0.5", " 25.25 "):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"MAX_FILE_SIZE_MB": value}):
                    self.assertEqual(telegram._read_max_file_size_mb(), Decimal(value))

    def test_invalid_configuration(self):
        for value in ("", "abc", "0", "-1", "NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"MAX_FILE_SIZE_MB": value}):
                    with self.assertRaisesRegex(ValueError, "MAX_FILE_SIZE_MB.*положительным"):
                        telegram._read_max_file_size_mb()


class FileSizeHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "recording.wav"
        self.path.write_bytes(b"1234567890")
        self.limit = Decimal(10) / 1_048_576
        self.root_patch = patch.object(telegram, "FILE_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def message_and_bot(self, source_type="document", size=10):
        media = SimpleNamespace(file_id="id", file_name=self.path.name, file_size=size)
        message = SimpleNamespace(
            content_type=source_type,
            **{source_type: media},
            answer=AsyncMock(), answer_document=AsyncMock(),
        )
        bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path=str(self.path))))
        return message, bot

    def assert_rejected(self, message, process, transcribe):
        process.assert_not_awaited()
        transcribe.assert_not_called()
        message.answer_document.assert_not_awaited()
        message.answer.assert_awaited_once_with(
            f"Файл слишком большой. Максимальный размер — {self.limit.normalize():f} МБ."
        )
        self.assertTrue(self.path.exists())

    async def test_below_and_at_limit_for_all_supported_types(self):
        for source_type in ("voice", "audio", "video", "document"):
            for size in (9, 10):
                with self.subTest(source_type=source_type, size=size):
                    self.path.write_bytes(b"x" * size)
                    message, bot = self.message_and_bot(source_type, size)
                    with patch.object(core, "process_recording", return_value="Речь") as process:
                        await telegram.receive_file(message, bot, self.limit)
                    process.assert_awaited_once()
                    self.assertEqual(process.call_args.kwargs["file_size"], size)
                    message.answer_document.assert_awaited_once()

    async def test_metadata_rejection_precedes_get_file_for_all_types(self):
        for source_type in ("voice", "audio", "video", "document"):
            with self.subTest(source_type=source_type):
                message, bot = self.message_and_bot(source_type, 11)
                with (
                    patch.object(core, "process_recording", new_callable=AsyncMock) as process,
                    patch.object(asr, "transcribe") as transcribe,
                ):
                    await telegram.receive_file(message, bot, self.limit)
                bot.get_file.assert_not_awaited()
                self.assert_rejected(message, process, transcribe)

    async def test_unknown_invalid_and_underreported_metadata_use_actual_size(self):
        for source_type in ("voice", "audio", "video", "document"):
            for size in (None, -1, "invalid", 1):
                for actual_size in (10, 11):
                    with self.subTest(source_type=source_type, size=size, actual_size=actual_size):
                        self.path.write_bytes(b"x" * actual_size)
                        message, bot = self.message_and_bot(source_type, size)
                        with (
                            patch.object(core, "process_recording", return_value="Речь") as process,
                            patch.object(asr, "transcribe") as transcribe,
                        ):
                            await telegram.receive_file(message, bot, self.limit)
                        bot.get_file.assert_awaited_once()
                        if actual_size > 10:
                            self.assert_rejected(message, process, transcribe)
                        else:
                            process.assert_awaited_once()
                            self.assertEqual(process.call_args.kwargs["file_size"], actual_size)
                            message.answer_document.assert_awaited_once()

    async def test_default_limit_message(self):
        message, bot = self.message_and_bot(size=104_857_601)
        await telegram.receive_file(message, bot)
        bot.get_file.assert_not_awaited()
        message.answer.assert_awaited_once_with(
            "Файл слишком большой. Максимальный размер — 100 МБ."
        )

    async def test_invalid_setting_stops_startup_before_database(self):
        with (
            patch.dict(os.environ, {"MAX_FILE_SIZE_MB": "invalid"}),
            patch.object(telegram, "configure_logging"),
            patch.object(database, "init_db") as init_db,
        ):
            with self.assertRaisesRegex(ValueError, "MAX_FILE_SIZE_MB"):
                await telegram.main()
        init_db.assert_not_called()

    async def test_valid_file_is_saved_and_returned_after_rejection(self):
        with (
            patch.object(database, "create_recording", return_value=1) as create,
            patch.object(database, "create_transcription", return_value=2),
            patch.object(database, "complete_transcription") as complete,
            patch.object(database, "fail_transcription") as fail,
            patch.object(asr, "transcribe", return_value="Привет") as transcribe,
        ):
            rejected, rejected_bot = self.message_and_bot(size=11)
            await telegram.receive_file(rejected, rejected_bot, self.limit)
            create.assert_not_called()
            complete.assert_not_called()
            transcribe.assert_not_called()
            message, bot = self.message_and_bot()
            await telegram.receive_file(message, bot, self.limit)
        create.assert_called_once_with(self.path.name, "document", 10, None)
        self.assertEqual(complete.call_args.args[:2], (2, "Привет"))
        fail.assert_not_called()
        document = message.answer_document.call_args.args[0]
        self.assertEqual(document.filename, "recording.txt")
        self.assertEqual(document.data, "Привет".encode("utf-8"))
