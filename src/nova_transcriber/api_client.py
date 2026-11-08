import json
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def request(path, data=None):
    req = Request(os.environ.get('CORE_API_URL', 'http://core-api:8000') + path,
                  data=None if data is None else json.dumps(data).encode(),
                  headers={'Authorization': 'Bearer ' + os.environ['CORE_API_TOKEN'],
                           'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 400:
            with error:
                detail = json.load(error).get('detail', 'Некорректные данные задачи')
            raise ValueError(detail) from None
        raise


def record(identifier):
    result = request(f'/tasks/{identifier}')
    if result['status'] == 'completed':
        result.update(request(f'/tasks/{identifier}/result'))
    return result
