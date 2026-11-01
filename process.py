"""Процесс выпуска: подготовка с кешем, связка кандидат/отчет, идемпотентная публикация.

Отличия от дефектного lab.py:
- ключ кеша считается по содержимому входа и конфигу (release.cache_key);
- кандидат разрешается по model_id отчета, а не жестко manifest-B;
- публикация разделена на журнал попыток (attempts.jsonl, пишется каждый запуск)
  и журнал событий фактического выпуска (publications.jsonl, уникальный
  release_id ровно один раз);
- повтор успешного выпуска, в том числе в другом процессе, не создает дубликатов
  и не меняет принятый указатель; сбой между обновлением указателя и записью
  журнала восстанавливается повтором без двойного события;
- указатель обновляется только атомарной заменой (release.publish).

Только стандартная библиотека, Python 3.12. Один писатель.
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import release

REQUIRED_CONFIG_FIELDS = ("tokenizer_revision", "template_sha256", "max_length", "code_revision")
POINTER_NAME = "accepted.json"
EVENTS_NAME = "publications.jsonl"
ATTEMPTS_NAME = "attempts.jsonl"


def canonical(value) -> bytes:
    return release.canonical(value)


def digest(data: bytes) -> str:
    return release.sha256_hex(data)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def manifest_release_id(manifest: dict) -> str:
    """Идентификатор выпуска по манифесту: модель + runtime. Восстановим из указателя."""
    payload = {
        "model_id": manifest["model_id"],
        "model_sha256": manifest["model_sha256"],
        "runtime_sha256": manifest["runtime_sha256"],
    }
    return digest(canonical(payload))


def release_id(manifest: dict, report: dict) -> str:
    """Идентификатор выпуска: модель, runtime и связанное решение допуска.

    Не сводится к model_id: изменение runtime или повторная оценка с другим
    evaluation_run_id/отчетом дают другой выпуск.
    """
    payload = {
        "model_id": manifest["model_id"],
        "model_sha256": manifest["model_sha256"],
        "runtime_sha256": manifest["runtime_sha256"],
        "evaluation_run_id": report["evaluation_run_id"],
        "report_sha256": digest(canonical(report)),
    }
    return digest(canonical(payload))


# --- журнал попыток и журнал событий -----------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_attempt(root: Path, record: dict) -> None:
    record = {"attempt_id": str(uuid.uuid4()), "utc_time": _utc_now(), **record}
    with (Path(root) / ATTEMPTS_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_events(root: Path):
    """События фактического выпуска. Оборванная последняя строка (сбой во время
    записи) отбрасывается и файл переписывается без нее — журнал остается
    построчно читаемым. Один писатель, поэтому переписывание безопасно."""
    path = Path(root) / EVENTS_NAME
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    complete, torn = [], False
    if text and not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1]
        torn = True
    for line in text.splitlines():
        if line.strip():
            complete.append(json.loads(line))
    if torn:
        _rewrite_events(path, complete)
    return complete


def _rewrite_events(path: Path, events) -> None:
    import tempfile, os

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_name, path)


def _append_event(root: Path, event: dict) -> None:
    with (Path(root) / EVENTS_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        import os as _os

        _os.fsync(f.fileno())


# --- стадии процесса ---------------------------------------------------------------

def prepare(root: Path) -> dict:
    """Подготовка входа с кешем по содержимому. Возвращает небольшой результат
    (хеши и счетчики), читаемый и после завершения процесса."""
    root = Path(root)
    config = read(root / "config.json")
    missing = [f for f in REQUIRED_CONFIG_FIELDS if f not in config]
    if missing:
        raise ValueError(f"config.json: нет обязательных полей {missing}")
    key = release.cache_key(root / "input.txt", config)
    cache = root / "cache"
    cache.mkdir(exist_ok=True)
    entry = cache / (key + ".json")
    if entry.exists():
        result = read(entry)
        print("CACHE_HIT", result["input_sha256"], flush=True)
        return result
    payload = (root / "input.txt").read_bytes()
    result = {
        "input_sha256": digest(payload),
        "rows": len(payload.splitlines()),
        "config": config,
        "cache_key": key,
    }
    write_json(entry, result)
    print("PREPARED", result["input_sha256"], flush=True)
    return result


def resolve_candidate(root: Path, report_name: str, manifest_name: str | None = None):
    """Манифест разрешается по model_id отчета допуска (не жесткой константой).
    Возвращает (manifest, report, manifest_name). Передаются только описания:
    пути, id, хеши."""
    root = Path(root)
    report = read(root / report_name)
    model_id = report.get("model_id")
    if manifest_name is None:
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("В отчете нет model_id — манифест не разрешить")
        candidates = sorted((root.glob("manifest-*.json")))
        matches = []
        for path in candidates:
            try:
                manifest = read(path)
            except ValueError:
                continue
            if manifest.get("model_id") == model_id:
                matches.append((path, manifest))
        if len(matches) != 1:
            raise ValueError(
                f"Для model_id={model_id!r} ожидается ровно один манифест, найдено {len(matches)}"
            )
        manifest_name, manifest = matches[0][0].name, matches[0][1]
    else:
        manifest = read(root / manifest_name)
    return manifest, report, manifest_name


def publish_release(root: Path, manifest: dict, report: dict) -> str:
    """Связанный gate + атомарный указатель + ровно одно событие на выпуск.

    Возвращает исход попытки: published | already-published | recovered
    | published-new-decision. Отказ валидации не трогает ни указатель, ни журнал.
    """
    root = Path(root)
    release.validate_release(manifest, report)  # отказ до любых изменений
    rid = release_id(manifest, report)
    rid_manifest = manifest_release_id(manifest)
    pointer = root / POINTER_NAME
    events = read_events(root)
    event_base = {
        "event": "published",
        "release_id": rid,
        "manifest_release_id": rid_manifest,
        "model_id": manifest["model_id"],
        "runtime_sha256": manifest["runtime_sha256"],
        "evaluation_run_id": report["evaluation_run_id"],
        "report_sha256": digest(canonical(report)),
        "manifest_sha256": digest(canonical(manifest)),
        "utc_time": _utc_now(),
    }

    current = None
    if pointer.exists():
        current = json.loads(pointer.read_text(encoding="utf-8"))

    existing = [e for e in events if e["release_id"] == rid]
    if existing:
        # Этот выпуск уже публиковался ранее: повтор (в том числе возврат к
        # ранее принятой версии) не создает второго события.
        if current is not None and manifest_release_id(current) == rid_manifest:
            return "already-published"
        release.publish(manifest, report, pointer)
        return "already-published-repointed"

    if current is not None and manifest_release_id(current) == rid_manifest:
        same_release = [e for e in events if e["release_id"] == rid]
        if same_release:
            # Чистый повтор: указатель уже согласован с журналом, ничего не пишем.
            return "already-published"
        same_model = [e for e in events if e.get("manifest_release_id") == rid_manifest]
        if same_model and same_model[-1]["release_id"] != rid:
            # Та же модель+runtime, но другое решение допуска — новый выпуск.
            _append_event(root, event_base)
            return "published-new-decision"
        # Указатель обновлен, событие потеряно (сбой между заменой и записью
        # журнала): восстановим согласованность ровно одним событием.
        event_base["recovered"] = True
        _append_event(root, event_base)
        return "recovered"

    release.publish(manifest, report, pointer)
    event_base["accepted_sha256"] = digest(pointer.read_bytes())
    _append_event(root, event_base)
    return "published"


def run(root: Path, report_name: str, fail_after_prepare: bool = False,
        manifest_name: str | None = None) -> str:
    """Одна попытка выпуска: подготовка -> (сбой) -> gate -> публикация."""
    root = Path(root)
    outcome = None
    try:
        prepared = prepare(root)
        if fail_after_prepare:
            raise RuntimeError("Инъекция сбоя после подготовки")
        manifest, report, _ = resolve_candidate(root, report_name, manifest_name)
        outcome = publish_release(root, manifest, report)
        append_attempt(root, {
            "release_id": release_id(manifest, report),
            "model_id": manifest["model_id"],
            "report": report_name,
            "prepared_input_sha256": prepared["input_sha256"],
            "outcome": outcome,
        })
        print(f"PUBLISHED {manifest['model_id']} outcome={outcome}", flush=True)
        return outcome
    except ValueError as error:
        append_attempt(root, {
            "report": report_name,
            "outcome": "refused",
            "error": str(error),
        })
        print(f"REFUSED {error}", file=sys.stderr, flush=True)
        raise
    except Exception as error:
        append_attempt(root, {
            "report": report_name,
            "outcome": "error",
            "error": f"{type(error).__name__}: {error}",
        })
        raise
