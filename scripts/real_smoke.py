"""Реальный smoke-проверка сервиса на модели ДЗ 6+7 (Qwen3-0.6B baseline).

Поднимает service.py --backend transformers на artifacts/real, ждет /ready,
делает два реальных запроса (из протокола оценки), один неверный запрос и
сверяет рапорт версии с манифестом. Лог: evidence/after/real-smoke.log
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_integration import http, stop_service  # noqa: E402


def main():
    workspace = ROOT / "artifacts" / "real"
    manifest = json.loads((workspace / "manifest-baseline.json").read_text(encoding="utf-8"))
    protocol = json.loads((workspace / "source" / "protocol-params.json").read_text(encoding="utf-8"))
    port = 8093
    print(f"== Запуск сервиса на {manifest['model_id']} "
          f"(загрузка реальных весов на CPU занимает минуты) ==")
    proc = subprocess.Popen(
        [sys.executable, "service.py", "--workspace", str(workspace), "--port", str(port),
         "--warmup-seconds", "0", "--backend", "transformers", "--max-parallel", "1"],
        cwd=ROOT, env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PATH": "", "SYSTEMROOT": __import__("os").environ["SYSTEMROOT"]},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                status, body = http(port, "GET", "/health", timeout=2)
                if status == 200:
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                print(proc.stdout.read())
                raise SystemExit("Сервис упал при старте")
            time.sleep(1)
        print("health:", json.dumps(body, ensure_ascii=False))

        print("== Ожидание /ready (проверка хешей 1.5 ГБ + загрузка весов) ==")
        deadline = time.time() + 420
        while time.time() < deadline:
            status, ready_body = http(port, "GET", "/ready", timeout=5)
            if status == 200:
                break
            time.sleep(5)
        print(f"ready: http={status} {json.dumps(ready_body, ensure_ascii=False)}")
        if status != 200:
            raise SystemExit("Сервис не стал готовым")

        status, debug = http(port, "GET", "/debug")
        print("debug:", json.dumps(debug, ensure_ascii=False))
        assert debug["loaded_id"] == manifest["model_id"], "загружена не манифестная версия"
        assert debug["runtime_sha256"] == manifest["runtime_sha256"], "runtime не совпал"

        prompts = [
            protocol.get("_smoke_prompt") or
            "Which measure of central tendency is most robust to outliers? A - mean, B - median, C - mode, D - range. choices: A, B, C, D.",
        ]
        for prompt in prompts:
            status, gen = http(port, "POST", "/generate",
                               {"prompt": prompt,
                                "max_new_tokens": manifest["runtime"]["generation"]["max_new_tokens"]},
                               timeout=180)
            print(f"REAL ANSWER http={status}: {json.dumps(gen, ensure_ascii=False)}")
            assert status == 200 and gen["model_id"] == manifest["model_id"]

        status, bad = http(port, "POST", "/generate",
                           {"prompt": "probe", "max_new_tokens": 100000}, timeout=30)
        _, after = http(port, "GET", "/debug")
        print(f"invalid request: http={status} body={json.dumps(bad, ensure_ascii=False)}")
        print(f"calls_after_invalid={after['calls']} "
              f"(до неверного запроса было {debug['calls'] + 1})")
        assert 400 <= status < 500 and after["calls"] == debug["calls"] + 1, \
            "неверный запрос не должен вызывать backend"
        print("== REAL SMOKE OK ==")
    finally:
        stop_service(proc)


if __name__ == "__main__":
    main()
