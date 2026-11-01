"""Отдельный опыт ДЗ 8+9-10: сервис на A -> публикация B -> откат к A (STUB версии).

Проверяется фактическая загрузка, а не указатель: до перезапуска сервис
обслуживает и рапортует прежнюю версию; после управляемого перезапуска —
новую; после отката и перезапуска — подтверждаем возврат A ответом.
Лог: evidence/after/aba-experience.log
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_integration import http, run_lab, stop_service  # noqa: E402

WS = ROOT / ".lab-aba"
PORT = 8095


def start():
    proc, port = None, None
    from tests.test_integration import start_service
    proc, port = start_service(WS, warmup_seconds=0.2)
    deadline = time.time() + 30
    while time.time() < deadline:
        if http(port, "GET", "/ready")[0] == 200:
            return proc, port
        time.sleep(0.2)
    stop_service(proc)
    raise SystemExit("Сервис не готов")


def snapshot(port, label):
    _, debug = http(port, "GET", "/debug")
    status, gen = http(port, "POST", "/generate", {"prompt": "probe", "max_new_tokens": 8})
    pointer = json.loads((WS / "accepted.json").read_text(encoding="utf-8"))["model_id"]
    print(f"[{label}] pointer={pointer} loaded={debug['loaded_id']} "
          f"response_model_id={gen.get('model_id')} calls={debug['calls']}")
    return debug, gen, pointer


def main():
    import shutil
    if WS.exists():
        shutil.rmtree(WS)
    init = run_lab(WS, "init")
    assert init.returncode == 0, init.stderr
    print("== Опыт A -> B -> A на двух STUB-версиях (это инженерная проверка, не eval) ==")

    proc, port = start()
    try:
        snapshot(port, "1. сервис запущен на A")
        publish = run_lab(WS, "run")
        assert publish.returncode == 0, publish.stderr
        snapshot(port, "2. опубликован B, БЕЗ перезапуска: ответ и loaded остаются A")

        stop_service(proc)
        proc, port = start()
        snapshot(port, "3. после управляемого перезапуска: загружен и рапортуется B")

        rollback = run_lab(WS, "run", "--report", "report-A.json")
        assert rollback.returncode == 0, rollback.stderr
        stop_service(proc)
        proc, port = start()
        debug, gen, pointer = snapshot(port, "4. откат к ранее принятой A + перезапуск")
        assert gen["model_id"] == "fixture-A" and debug["loaded_id"] == "fixture-A" \
            and pointer == "fixture-A", "версия A должна быть подтверждена ответом"
        events = (WS / "publications.jsonl").read_text(encoding="utf-8").splitlines()
        print(f"journal событий: {len(events)} "
              f"(B и A, по одному на выпуск — без дубликатов и повторов)")
        print("== ОПЫТ A->B->A OK ==")
    finally:
        stop_service(proc)


if __name__ == "__main__":
    main()
