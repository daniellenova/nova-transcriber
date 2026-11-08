import json
import os
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from loguru import logger

from nova_transcriber import core, database
from nova_transcriber.config import get_max_file_size_mb, get_owner_id, get_positive_int
from nova_transcriber.safe_logging import configure_logging


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, default=datetime.isoformat).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        if not secrets.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), ("Bearer " + os.environ["CORE_API_TOKEN"]).encode("utf-8")):
            self.respond(401, {"detail": "Доступ запрещён"})
            return
        try:
            user_id = get_owner_id()
            url = urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            if self.command == 'POST' and len(parts) == 3 and parts[0] == 'tasks' and parts[2] == 'delivery':
                identifier = int(parts[1])
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < identifier <= 9223372036854775807 or not 0 < length <= 1024 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Некорректные данные доставки')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or type(data.get('manual', False)) is not bool:
                    raise ValueError('Некорректные данные доставки')
                acquired = database.delivery(identifier, user_id, data.get('action'), data.get('manual', False))
                self.respond(200, {'ok': acquired})
                return
            if self.command == 'POST' and parts == ['tasks', 'delete']:
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('Неподдерживаемый способ передачи')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 32768:
                    raise ValueError('Недопустимый размер запроса')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Ожидается объект с выбранными ID')
                database.delete_tasks(user_id, data.get('ids'))
                self.respond(200, {'ok': True})
                return
            if self.command == 'GET' and parts == ['history']:
                page = int(parse_qs(url.query).get('page', ['0'])[0])
                if not 0 <= page <= 2147483647:
                    raise ValueError('Недопустимая страница')
                result = database.history_page(user_id, page)
                result['items'] = [core.api_record(r) for r in result['items']]
                self.respond(200, result)
                return
            if self.command == 'POST' and len(parts) == 3 and parts[0] == 'tasks' and parts[2] in {'cancel', 'resume', 'retry'}:
                identifier = int(parts[1])
                if not 0 < identifier <= 9223372036854775807:
                    raise ValueError('Недопустимый идентификатор')
                database.change_task(identifier, user_id, parts[2])
                self.respond(200, {'ok': True})
                return
            if self.command == 'GET' and parts == ['notifications']:
                records = database.notifications(user_id)
                self.respond(200, [{**core.api_record(r), 'telegram_chat_id': r['telegram_chat_id'], 'telegram_message_id': r['telegram_message_id']} for r in records])
                return
            if self.command == 'POST' and len(parts) == 3 and parts[0] == 'tasks' and parts[2] == 'notified':
                identifier = int(parts[1])
                if not 0 < identifier <= 9223372036854775807:
                    raise ValueError('Недопустимый идентификатор')
                database.acknowledge(identifier, user_id)
                self.respond(200, {'ok': True})
                return
            if self.command == "POST" and parts == ["tasks"]:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Неподдерживаемый способ передачи")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("Недопустимый размер запроса")
                data = json.loads(self.rfile.read(length))
                self.respond(201, core.create_api_task(data, user_id))
                return
            if self.command == "GET" and parts == ["tasks"]:
                limit = int(parse_qs(url.query).get("limit", ["10"])[0])
                if not 1 <= limit <= 100:
                    raise ValueError("Лимит должен быть от 1 до 100")
                records = database.get_recent_transcriptions(limit, user_id)
                self.respond(200, [core.api_record(r) for r in records])
                return
            if self.command == "GET" and len(parts) in {2, 3} and parts[0] == "tasks" and (len(parts) == 2 or parts[2] == "result"):
                identifier = int(parts[1])
                if not 0 < identifier <= 9223372036854775807:
                    raise ValueError("Недопустимый идентификатор")
                record = database.get_transcription(identifier, user_id, include_text=len(parts) == 3)
                if record is None:
                    self.respond(404, {"detail": "Задача не найдена"})
                elif len(parts) == 3:
                    if record['text'] is None or (record["status"] != "completed" and record["processed_samples"] == 0):
                        self.respond(409, {"detail": "Результат ещё не готов"})
                    else:
                        self.respond(200, {**core.api_record(record), "text": record["text"],
                                           "partial": record["status"] != "completed"})
                else:
                    self.respond(200, core.api_record(record))
                return
            self.respond(404, {"detail": "Маршрут не найден"})
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, KeyError, OSError, OverflowError):
            self.respond(400, {"detail": "Некорректные данные или недоступный файл"})
        except ValueError as error:
            self.respond(400, {"detail": str(error)})
        except Exception:
            logger.bind(component="Core API", event="request_failed").error("Не удалось выполнить запрос")
            self.respond(503, {"detail": "Сервис временно недоступен"})


def main():
    configure_logging()
    if len(os.environ.get("CORE_API_TOKEN", "")) < 32:
        raise ValueError("CORE_API_TOKEN должен содержать не менее 32 символов")
    get_owner_id()
    get_max_file_size_mb()
    get_positive_int('MAX_DURATION_SECONDS', 43200)
    if not Path(os.environ["CORE_MEDIA_ROOT"]).is_dir():
        raise ValueError("CORE_MEDIA_ROOT должен указывать на доступный каталог")
    database.init_db()
    server = ThreadingHTTPServer((os.environ.get("CORE_API_HOST", "127.0.0.1"), 8000), Handler)
    logger.bind(component="Core API", event="ready").info("Внутренний API готов к работе")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
