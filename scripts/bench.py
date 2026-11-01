"""Небольшой замер ДЗ 8+9-10 (часть D).

Один и тот же набор из 8 запросов при параллельности 1 и еще 8 при
параллельности 2 против одного сервиса (backend Transformers, один писатель
весов: server semaphore = 1). Каждая попытка сохраняется в JSONL: id, режим,
статус/ошибка, клиентская задержка, server-времена (queue/generation),
model_id, фактическое число выходных токенов. Ошибки из журнала не удаляются.

Итоговая таблица: n, успехи/ошибки, длительность всего пакета, успешные
запросы/с, средняя и максимальная задержка успешных. На такой выборке
устойчивый p95 не вычисляется (см. REPORT.txt).
"""
import argparse
import concurrent.futures
import csv
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_integration import http, start_service, stop_service  # noqa: E402

# Один и тот же набор из 8 запросов для обоих режимов (ASCII — единые условия
# с протоколом оценки ДЗ 6+7; лимит ответа одинаковый: max_new_tokens=32).
REQUESTS = [
    "Which measure of central tendency is most robust to outliers? A - mean, B - median, C - mode, D - range. choices: A, B, C, D.",
    "Which method reduces overfitting by averaging trees grown on bootstrap samples? A - k-means, B - PCA, C - random forest, D - z-test. choices: A, B, C, D.",
    "Which test compares means of two independent groups? A - t-test, B - chi-square, C - ANOVA post-hoc, D - bootstrap CI. choices: A, B, C, D.",
    "What does a p-value below 0.05 typically indicate? A - effect size, B - statistical significance, C - causation, D - power. choices: A, B, C, D.",
    "Which distribution models the number of successes in n trials? A - normal, B - binomial, C - exponential, D - uniform. choices: A, B, C, D.",
    "Which plot shows the median and quartiles? A - histogram, B - boxplot, C - scatter, D - pie. choices: A, B, C, D.",
    "What is the expected value of a fair six-sided die? A - 3, B - 3.5, C - 4, D - 6. choices: A, B, C, D.",
    "Which method estimates model error on unseen data? A - cross-validation, B - gradient descent, C - tokenization, D - pruning. choices: A, B, C, D.",
]
MAX_NEW_TOKENS = 32


def run_mode(port, mode, parallelism, log, lock):
    records = []
    started = time.perf_counter()

    def one(index):
        prompt = REQUESTS[index % len(REQUESTS)]
        t0 = time.perf_counter()
        status, body = http(port, "POST", "/generate",
                            {"prompt": prompt, "max_new_tokens": MAX_NEW_TOKENS},
                            timeout=600)
        client_latency = time.perf_counter() - t0
        record = {
            "attempt_id": str(uuid.uuid4()),
            "utc_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "mode": mode,
            "parallelism": parallelism,
            "request_index": index,
            "status": status,
            "error": None if status == 200 else json.dumps(body, ensure_ascii=False),
            "client_latency_seconds": round(client_latency, 4),
            "queue_seconds": body.get("queue_seconds"),
            "generation_seconds": body.get("generation_seconds"),
            "model_id": body.get("model_id"),
            "output_tokens": body.get("output_tokens"),
            "request_id": body.get("request_id"),
        }
        with lock:
            log.append(record)
            print(f"[{mode}] {index + 1}/8 http={status} "
                  f"client={record['client_latency_seconds']}s "
                  f"queue={record['queue_seconds']}s "
                  f"gen={record['generation_seconds']}s tokens={record['output_tokens']}",
                  flush=True)
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
        list(pool.map(one, range(len(REQUESTS))))
    package_seconds = time.perf_counter() - started
    return records, package_seconds


def summarize(mode, records, package_seconds):
    ok = [r for r in records if r["status"] == 200]
    failed = [r for r in records if r["status"] != 200]
    latencies = [r["client_latency_seconds"] for r in ok]
    return {
        "mode": mode,
        "n": len(records),
        "successes": len(ok),
        "errors": len(failed),
        "package_seconds": round(package_seconds, 3),
        "success_per_second": round(len(ok) / package_seconds, 3) if package_seconds else 0,
        "avg_latency_success": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "max_latency_success": round(max(latencies), 3) if latencies else None,
        "avg_generation_success": round(
            sum(r["generation_seconds"] for r in ok if r["generation_seconds"]) / len(ok), 3)
        if ok else None,
        "total_output_tokens": sum(r["output_tokens"] or 0 for r in ok),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(ROOT / "artifacts" / "real"))
    parser.add_argument("--backend", default="transformers")
    parser.add_argument("--port", type=int, default=8094)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--out", default=str(ROOT / "measurements"))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = out_dir / f"bench-{stamp}.jsonl"
    csv_path = out_dir / f"bench-{stamp}.csv"
    log = []
    lock = threading.Lock()

    print(f"== Запуск сервиса backend={args.backend} (прогрев реальных весов) ==")
    proc, port = start_service(Path(args.workspace), warmup_seconds=0, backend=args.backend)
    try:
        deadline = time.time() + 420
        while time.time() < deadline:
            if http(port, "GET", "/ready", timeout=5)[0] == 200:
                break
            time.sleep(5)
        print("== Сервис готов ==")
        for _ in range(args.warmup):
            status, _body = http(port, "POST", "/generate",
                                 {"prompt": "warmup request", "max_new_tokens": MAX_NEW_TOKENS},
                                 timeout=600)
            print(f"warmup: http={status}", flush=True)

        _, package1 = run_mode(port, "conc1", 1, log, lock)
        _, package2 = run_mode(port, "conc2", 2, log, lock)
    finally:
        stop_service(proc)

    summaries = [summarize("conc1", [r for r in log if r["mode"] == "conc1"], package1),
                 summarize("conc2", [r for r in log if r["mode"] == "conc2"], package2)]

    with jsonl_path.open("w", encoding="utf-8") as f:
        for record in log:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        writer.writeheader()
        writer.writerows(log)

    print("\n== Сводная таблица ==")
    header = ["mode", "n", "ok/err", "package_s", "req/s", "avg_lat_s", "max_lat_s", "avg_gen_s", "tokens"]
    print("{:<7} {:>3} {:>7} {:>10} {:>6} {:>9} {:>9} {:>9} {:>7}".format(*header))
    for s in summaries:
        print("{mode:<7} {n:>3} {successes}/{errors:<5} {package_seconds:>10} "
              "{success_per_second:>6} {avg_latency_success:>9} {max_latency_success:>9} "
              "{avg_generation_success:>9} {total_output_tokens:>7}".format(**s))
    (out_dir / f"bench-{stamp}-summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSONL: {jsonl_path}\nCSV:   {csv_path}")


if __name__ == "__main__":
    main()
