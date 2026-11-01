"""HTTP-щуп для дефектов 5/6/7 на исходном или исправленном сервисе.

Использование: python evidence/http_probe.py <workspace> <port> <probe> [warmup]
probe: ready | mismatch | badrequests
Печатает человекочитаемые строки-свидетельства. Совместим с прототипом
(http.server) и FastAPI-версией: использует одинаковые маршруты и поля.
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


def call(port, method, path, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {}
        return e.code, payload


def main():
    workspace, port, probe = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    accepted = json.loads((Path(workspace) / "accepted.json").read_text(encoding="utf-8"))
    if probe == "ready":
        for path in ("/health", "/ready", "/debug"):
            status, payload = call(port, "GET", path)
            print(f"{path}: http={status} body={json.dumps(payload, ensure_ascii=False)}")
        status, payload = call(port, "POST", "/generate",
                               {"prompt": "probe prompt", "max_new_tokens": 8})
        print(f"generate-during-warmup: http={status} body={json.dumps(payload, ensure_ascii=False)}")
    elif probe == "mismatch":
        print(f"accepted.json model_id (pointer): {accepted['model_id']}")
        for path in ("/debug",):
            status, payload = call(port, "GET", path)
            print(f"{path}: http={status} body={json.dumps(payload, ensure_ascii=False)}")
        status, payload = call(port, "POST", "/generate",
                               {"prompt": "probe prompt", "max_new_tokens": 8})
        served = payload.get("model_id")
        print(f"generate-response-model_id: http={status} model_id={served} "
              f"loaded_must_match={served == accepted['model_id']}")
    elif probe == "badrequests":
        _, before = call(port, "GET", "/debug")
        cases = [
            ("empty prompt", {"prompt": "", "max_new_tokens": 8}),
            ("max_new_tokens=0", {"prompt": "probe prompt", "max_new_tokens": 0}),
            ("max_new_tokens=100000", {"prompt": "probe prompt", "max_new_tokens": 100000}),
            ("max_new_tokens string", {"prompt": "probe prompt", "max_new_tokens": "1000"}),
            ("prompt not a string", {"prompt": 123, "max_new_tokens": 8}),
        ]
        for name, body in cases:
            status, payload = call(port, "POST", "/generate", body)
            print(f"{name}: http={status} body={json.dumps(payload, ensure_ascii=False)}")
        status, payload = call(port, "POST", "/generate",
                               {"prompt": "valid probe", "max_new_tokens": 8})
        print(f"valid request: http={status} model_id={payload.get('model_id')}")
        _, after = call(port, "GET", "/debug")
        bad = len(cases)
        print(f"calls_before={before['calls']} calls_after={after['calls']} "
              f"backend_calls_added_by_bad_requests={after['calls'] - before['calls'] - 1}")


if __name__ == "__main__":
    main()
