"""Реальный выпуск ДЗ 8+9-10: связка с hw67 (gate) и публикация baseline.

Что делает скрипт (без обучения, без повторного sweep — переиспользуются
готовые training/eval runs ДЗ 6+7):
  1. Замораживает исходные артефакты hw67 (baseline/candidate eval, gate,
     протокол) в artifacts/real/source и фиксирует их SHA-256.
  2. Вызывает правило допуска ДЗ 6+7 (quality_gate из gate/hw67_solution.py)
     на сохраненных отчетах с их исходными хешами. Кандидат честно отклонен
     (behavioral 0/3) — отказ сохраняется как release-report кандидата.
  3. Собирает манифест baseline (Qwen3-0.6B @ закрепленной ревизии): хеши
     реальных файлов артефакта пересчитываются из локального snapshot HF-кеша,
     runtime совпадает с протоколом оценки (dtype float32, max_new_tokens 32,
     greedy, та же system-инструкция).
  4. Формирует связанный release-report baseline и публикует его через
     process.publish_release (валидация + атомарный указатель + одно событие).

ВАЖНО о 'passed: true' в release-report baseline: это НЕ результат сравнения
baseline с самим собой. Полная оценка baseline завершена (0 ошибок генерации),
отчет полный и связан с манифестом; решение « retain» следует из отказа
кандидата гейтом (пороги не ослаблялись). Ссылки на все исходные отчеты и их
хеши включены в отчет.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

import process
import release
from gate import hw67_solution

ROOT = Path(__file__).resolve().parent
HW67 = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "hw67"
OUT = ROOT / "artifacts" / "real"
SOURCE = OUT / "source"

# Закрепленный список файлов артефакта (CONTRACTS.txt: относительный путь ->
# SHA-256 файла, затем SHA-256 канонического JSON словаря).
ARTIFACT_FILES = ["config.json", "generation_config.json", "merges.txt",
                  "model.safetensors", "tokenizer.json", "tokenizer_config.json",
                  "vocab.json"]
TOKENIZER_FILES = ["tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"]
BASELINE_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
EVAL_RUN_ID = "7426f58975a748f88c6863c3701c7561"
CRITICAL_SLICES = ["probability"]

SOURCE_FILES = {
    "baseline-eval.json": HW67 / "metrics" / "baseline-eval.json",
    "candidate-eval.json": HW67 / "metrics" / "candidate-eval.json",
    "quality-gate.json": HW67 / "metrics" / "quality-gate.json",
    "negative-gate.json": HW67 / "metrics" / "negative-gate.json",
    "protocol-params.json": HW67 / "evaluation" / "params.json",
    "baseline-spec.json": HW67 / "evaluation" / "baseline.json",
}


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hf_snapshot_dir() -> Path:
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    snapshot = cache / "models--Qwen--Qwen3-0.6B" / "snapshots" / BASELINE_REVISION
    if not snapshot.is_dir():
        raise SystemExit(f"Не найден snapshot Qwen3-0.6B: {snapshot}")
    return snapshot


def main():
    SOURCE.mkdir(parents=True, exist_ok=True)
    # 1. Заморозка исходных артефактов + хеши.
    source_hashes = {}
    for name, src in SOURCE_FILES.items():
        dst = SOURCE / name
        shutil.copyfile(src, dst)
        source_hashes[name] = sha_file(dst)
    gate_code_hash = sha_file(ROOT / "gate" / "hw67_solution.py")
    (SOURCE / "source-manifest.json").write_text(
        json.dumps({"files": source_hashes,
                    "gate_code_sha256": gate_code_hash,
                    "origin": str(HW67)}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    baseline_eval = json.loads((SOURCE / "baseline-eval.json").read_text(encoding="utf-8"))
    candidate_eval = json.loads((SOURCE / "candidate-eval.json").read_text(encoding="utf-8"))

    # 2. Правило допуска ДЗ 6+7 на сохраненных отчетах (без подмены решения).
    candidate_report = {
        "status": "complete", "passed": False,
        "evaluation_run_id": EVAL_RUN_ID,
        "gate_reason": None, "gate_evidence": "quality_gate ДЗ 6+7 отклонил кандидата",
    }
    try:
        gate_result = hw67_solution.quality_gate(baseline_eval, candidate_eval, CRITICAL_SLICES)
        raise SystemExit("Кандидат внезапно прошел гейт — проверьте исходные отчеты")
    except ValueError as refusal:
        candidate_report["gate_reason"] = str(refusal)
        print("GATE REFUSE (кандидат):", refusal)

    # 3. Манифест baseline по реальным файлам snapshot.
    snapshot = hf_snapshot_dir()
    file_hashes = {name: sha_file(snapshot / name) for name in ARTIFACT_FILES}
    model_sha256 = release.sha256_hex(release.canonical(file_hashes))
    tokenizer_sha256 = release.sha256_hex(release.canonical(
        {name: file_hashes[name] for name in TOKENIZER_FILES}))
    protocol = json.loads((SOURCE / "protocol-params.json").read_text(encoding="utf-8"))
    system = protocol["system"]
    template_sha256 = release.sha256_hex(system.encode("utf-8"))
    runtime = {
        "base_revision": BASELINE_REVISION,
        "tokenizer_sha256": tokenizer_sha256,
        "template_sha256": template_sha256,
        "generation": {
            "max_new_tokens": int(protocol["max_new_tokens"]),
            "do_sample": False,
            "seed": int(protocol["seed"]),
            "enable_thinking": bool(protocol["enable_thinking"]),
            "system": system,
            "system_sha256": template_sha256,
        },
        "dtype": "float32",
    }
    manifest = {
        "schema_version": 1,
        "model_id": "Qwen/Qwen3-0.6B",
        "artifact_uri": f"huggingface:Qwen/Qwen3-0.6B@{BASELINE_REVISION}",
        "model_sha256": model_sha256,
        "protocol_sha256": baseline_eval["protocol_sha256"],
        "golden_sha256": baseline_eval["golden_sha256"],
        "runtime_sha256": release.sha256_hex(release.canonical(runtime)),
        "runtime": runtime,
        "file_hashes": file_hashes,
        "local_path": str(snapshot),
        "kind": "base",
        "source_eval": "artifacts/real/source/baseline-eval.json",
    }

    overall = baseline_eval["overall"]
    baseline_report = {
        "status": "complete",
        "passed": True,  # см. смысл в докстринге: retain после отказа кандидата
        "evaluation_run_id": EVAL_RUN_ID,
        "model_id": manifest["model_id"],
        "model_sha256": manifest["model_sha256"],
        "protocol_sha256": manifest["protocol_sha256"],
        "golden_sha256": manifest["golden_sha256"],
        "runtime_sha256": manifest["runtime_sha256"],
        "metrics": {"accuracy": overall["accuracy"], "format_rate": overall["format_rate"]},
        "decision": "retain-baseline",
        "gate_evidence": {
            "gate_code": "gate/hw67_solution.py (копия evaluation/solution.py ДЗ 6+7)",
            "gate_code_sha256": gate_code_hash,
            "candidate_refused": True,
            "candidate_refusal_reason": candidate_report["gate_reason"],
            "candidate_eval_sha256": source_hashes["candidate-eval.json"],
            "baseline_eval_sha256": source_hashes["baseline-eval.json"],
            "critical_slices": CRITICAL_SLICES,
        },
    }
    # Самопроверка контрактов перед публикацией.
    release.validate_release(manifest, baseline_report)

    # 4. Публикация baseline: валидация, атомарный указатель, одно событие.
    process.write_json(OUT / "manifest-baseline.json", manifest)
    process.write_json(OUT / "release-report-baseline.json", baseline_report)
    process.write_json(OUT / "release-report-candidate-refused.json", candidate_report)
    outcome = process.publish_release(OUT, manifest, baseline_report)
    print(f"PUBLISHED {manifest['model_id']} outcome={outcome}")
    print("model_sha256     :", manifest["model_sha256"])
    print("runtime_sha256   :", manifest["runtime_sha256"])
    print("accepted.json    :", sha_file(OUT / "accepted.json"))
    print("snapshot         :", snapshot)


if __name__ == "__main__":
    main()
