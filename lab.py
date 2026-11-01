"""CLI процесса выпуска (совместим с прототипом): init, run.

Тот же интерфейс команд, что у исходного прототипа:
  python3 lab.py init
  python3 lab.py run [--report report-B.json] [--fail-after-prepare]
  python3 lab.py --workspace .lab-second init
Дополнения: --manifest (явный манифест), иначе манифест разрешается по
model_id отчета. Стадии исполняются process.py; Prefect-обертка — flow.py
(те же стадии как задачи с устойчивым кешем).

Без модели, без дополнительных пакетов. Фикстуры — синтетические STUB ONLY.
"""
import argparse
import sys
from pathlib import Path

import process
import release


def canonical(value):
    return release.canonical(value)


def digest(data):
    return release.sha256_hex(data)


def read(path):
    return process.read(path)


def write(path, value):
    process.write_json(path, value)


def initialize(root: Path):
    """Синтетический стенд: две версии A/B, полный и дефектные отчеты, вход."""
    if root.exists():
        raise FileExistsError("Рабочий каталог уже существует. Выберите новый --workspace.")
    root.mkdir(parents=True)
    runtime = {"base_revision": "fixture-only", "tokenizer_sha256": "a" * 64,
               "template_sha256": "b" * 64,
               "generation": {"max_new_tokens": 32, "do_sample": False},
               "dtype": "float32"}
    for version in ["A", "B"]:
        artifact = root / f"model-{version}.txt"
        artifact.write_text(f"STUB ONLY — version {version}\n", encoding="utf-8")
        files = {artifact.name: digest(artifact.read_bytes())}
        manifest = {"schema_version": 1, "model_id": f"fixture-{version}",
                    "artifact_uri": artifact.name,
                    "model_sha256": digest(canonical(files)),
                    "protocol_sha256": "d" * 64, "golden_sha256": "e" * 64,
                    "runtime_sha256": digest(canonical(runtime)),
                    "runtime": runtime, "file_hashes": files}
        report = {k: manifest[k] for k in
                  ["model_id", "model_sha256", "protocol_sha256", "golden_sha256", "runtime_sha256"]}
        report.update(status="complete", passed=True,
                      evaluation_run_id=f"fixture-eval-{version}",
                      metrics={"accuracy": 0.8, "format_rate": 1.0}, synthetic=True)
        write(root / f"manifest-{version}.json", manifest)
        write(root / f"report-{version}.json", report)
    write(root / "accepted.json", read(root / "manifest-A.json"))
    report = read(root / "report-B.json")
    report["status"] = "running"
    report["passed"] = "false"
    write(root / "report-incomplete.json", report)
    report = read(root / "report-B.json")
    report["passed"] = False
    write(root / "report-reject.json", report)
    write(root / "config.json", {"tokenizer_revision": "r1", "template_sha256": "b" * 64,
                                 "max_length": 128, "code_revision": "prepare-v1"})
    (root / "input.txt").write_text("Первый пример\nВторой пример\n", encoding="utf-8")
    print("Создан синтетический стенд:", root)


def run(root: Path, report_name: str, fail_after_prepare: bool = False,
        manifest_name: str | None = None) -> str:
    return process.run(root, report_name, fail_after_prepare, manifest_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(".lab"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--report", default="report-B.json")
    run_parser.add_argument("--manifest", default=None)
    run_parser.add_argument("--fail-after-prepare", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.workspace)
    else:
        try:
            run(args.workspace, args.report, args.fail_after_prepare, args.manifest)
        except ValueError as error:
            sys.exit(2)


if __name__ == "__main__":
    main()
