import json
import os
from http.client import HTTPException
from time import sleep
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def request(path, data=None):
    req = Request(os.environ.get('CORE_API_URL', 'http://core-api:8000') + path,
                  data=None if data is None else json.dumps(data).encode(),
                  headers={'Authorization': 'Bearer ' + os.environ['CORE_API_TOKEN'],
                           'Content-Type': 'application/json'})
    can_retry = data is None or path.endswith('/notified') or (
        path == '/tasks' and bool(data.get('request_key'))
    )
    for attempt in range(3):
        try:
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                if error.code == 400:
                    detail = json.load(error).get('detail', 'Некорректные данные задачи')
                    raise ValueError(detail) from None
            if not can_retry or attempt == 2 or error.code not in {429, 500, 502, 503, 504}:
                raise
        except (URLError, OSError, HTTPException):
            if not can_retry or attempt == 2:
                raise
        sleep(2 ** attempt)


def record(identifier):
    result = request(f'/tasks/{identifier}')
    if result['status'] == 'completed' or result.get('has_partial_result'):
        result.update(request(f'/tasks/{identifier}/result'))
    return result
