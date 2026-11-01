"""Prefect-flow процесса выпуска: отдельные задачи, устойчивый кеш, идемпотентная публикация.

Стадии (один flow, отдельные задачи):
  prepare_stage          — подготовка входа; кешируется Prefect по содержимому
                           (release.cache_key: {data_sha256, config}), результат
                           сохраняется на диск и читается после завершения процесса;
  resolve_candidate_stage— чтение манифеста и отчета допуска (только описания);
  gate_stage             — связанная проверка допуска (release.validate_release);
  publish_stage          — атомарный указатель + ровно одно событие на выпуск
                           (process.publish_release).

Между задачами передаются небольшие описания (пути, id, хеши); веса и тензоры
как результаты задач не передаются. Чтение готового training run ДЗ 6+7 — это
чтение результата, а не повторное обучение; здесь работает тот же принцип:
повторный sweep не выполняется, подготовка переиспользуется через кеш.

CLI совместим с lab.py:
  python3 flow.py --workspace .lab-flow init
  python3 flow.py --workspace .lab-flow run [--report report-B.json] [--fail-after-prepare]
"""
import argparse
import json
import sys
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.filesystems import LocalFileSystem

import lab
import process
import release

# Счетчик исполнений тела prepare_stage: нужен тестам, чтобы отличить
# фактическую подготовку от попадания в кеш Prefect.
PREPARE_EXECUTIONS = 0

REQUIRED_CONFIG_FIELDS = process.REQUIRED_CONFIG_FIELDS


def prepare_cache_key(context, parameters) -> str:
    """Ключ кеша стадии подготовки: SHA-256 канонического JSON
    {data_sha256, config}. Содержимое входа, ревизия токенизатора, шаблон,
    max_length и версия кода подготовки входят в ключ; путь и mtime — нет."""
    workspace = Path(parameters["workspace"])
    config = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    missing = [f for f in REQUIRED_CONFIG_FIELDS if f not in config]
    if missing:
        raise ValueError(f"config.json: нет обязательных полей {missing}")
    return release.cache_key(workspace / "input.txt", config)


@task(name="prepare-input", cache_key_fn=prepare_cache_key, persist_result=True,
      description="Подготовка входа с кешем по содержимому")
def prepare_stage(workspace: str) -> dict:
    """Подготовка/токенизация входа. Результат — небольшой словарь с хешами."""
    global PREPARE_EXECUTIONS
    PREPARE_EXECUTIONS += 1
    logger = get_run_logger()
    root = Path(workspace)
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    payload = (root / "input.txt").read_bytes()
    result = {
        "input_sha256": release.sha256_hex(payload),
        "rows": len(payload.splitlines()),
        "config": config,
        "cache_key": prepare_cache_key(None, {"workspace": workspace}),
    }
    logger.info("PREPARED %s (rows=%s)", result["input_sha256"], result["rows"])
    return result


@task(name="resolve-candidate", description="Манифест и отчет допуска кандидата")
def resolve_candidate_stage(workspace: str, report_name: str,
                            manifest_name: str | None = None) -> dict:
    manifest, report, resolved_manifest_name = process.resolve_candidate(
        Path(workspace), report_name, manifest_name)
    return {
        "manifest_path": str(Path(workspace) / resolved_manifest_name),
        "report_path": str(Path(workspace) / report_name),
        "model_id": manifest["model_id"],
        "artifact_uri": manifest["artifact_uri"],
        "model_sha256": manifest["model_sha256"],
        "runtime_sha256": manifest["runtime_sha256"],
        "evaluation_run_id": report["evaluation_run_id"],
    }


@task(name="gate-decision", description="Связанная проверка допуска")
def gate_stage(candidate: dict) -> dict:
    logger = get_run_logger()
    manifest = json.loads(Path(candidate["manifest_path"]).read_text(encoding="utf-8"))
    report = json.loads(Path(candidate["report_path"]).read_text(encoding="utf-8"))
    # Отказ по качеству/контракту здесь поднимает ValueError — flow падает,
    # указатель и журнал событий не меняются.
    release.validate_release(manifest, report)
    decision = {
        "decision": "publish",
        "release_id": process.release_id(manifest, report),
        "manifest_release_id": process.manifest_release_id(manifest),
        "model_id": manifest["model_id"],
        "evaluation_run_id": report["evaluation_run_id"],
    }
    logger.info("GATE PASS %s release_id=%s", decision["model_id"], decision["release_id"])
    return decision


@task(name="publish-release", description="Атомарный указатель + идемпотентный журнал событий")
def publish_stage(workspace: str, decision: dict, manifest_path: str, report_path: str) -> dict:
    """Публикация перечитывает связанные файлы по путям из решения гейта:
    через задачи идут только описания, не сами артефакты."""
    logger = get_run_logger()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    outcome = process.publish_release(Path(workspace), manifest, report)
    logger.info("PUBLISHED %s outcome=%s", decision["model_id"], outcome)
    return {"model_id": decision["model_id"], "outcome": outcome,
            "release_id": decision["release_id"]}


def _workspace_storage(root: Path) -> LocalFileSystem:
    """Локальное хранилище результатов Prefect внутри рабочего каталога:
    результаты стадии подготовки читаются после завершения процесса и видны
    при разборе инцидента."""
    import hashlib

    basepath = str(root / "prefect-results")
    name = "hw8910-" + hashlib.sha256(basepath.encode()).hexdigest()[:12]
    try:
        return LocalFileSystem.load(name)
    except Exception:
        storage = LocalFileSystem(basepath=basepath)
        storage.save(name, overwrite=True)
        return storage


@flow(name="release-flow", log_prints=True)
def release_flow(workspace: str, report_name: str = "report-B.json",
                 manifest_name: str | None = None,
                 fail_after_prepare: bool = False) -> dict:
    logger = get_run_logger()
    root = Path(workspace)
    executions_before = PREPARE_EXECUTIONS
    prepared = prepare_stage.with_options(result_storage=_workspace_storage(root))(workspace)
    if fail_after_prepare:
        raise RuntimeError("Инъекция сбоя после подготовки")
    candidate = resolve_candidate_stage(workspace, report_name, manifest_name)
    decision = gate_stage(candidate)
    published = publish_stage(workspace, decision, candidate["manifest_path"],
                              candidate["report_path"])
    return {"model_id": published["model_id"], "outcome": published["outcome"],
            "release_id": published["release_id"],
            "prepared_input_sha256": prepared["input_sha256"],
            "prepared_from_cache": PREPARE_EXECUTIONS == executions_before}


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
        lab.initialize(args.workspace)
    else:
        try:
            release_flow(workspace=str(args.workspace), report_name=args.report,
                         manifest_name=args.manifest,
                         fail_after_prepare=args.fail_after_prepare)
        except ValueError as error:
            print(f"REFUSED {error}", file=sys.stderr, flush=True)
            sys.exit(2)


if __name__ == "__main__":
    main()
