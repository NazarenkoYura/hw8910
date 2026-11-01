"""Исправленные контракты выпуска: cache_key, validate_release, publish.

Контракты — CONTRACTS.txt (исходного прототипа). Отличия от дефектной версии:
- cache_key учитывает SHA-256 байтов входа и все поля config (канонический JSON),
  а не путь к файлу;
- validate_release проверяет полный манифест и отчёт (схема, хеши, связность,
  конечные метрики), а не только правдивость поля passed;
- publish атомарен: временный файл -> flush/fsync -> os.replace, при отказе
  прежний файл побайтно неизменен и временный файл удалён.

Только стандартная библиотека, Python 3.12.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

HEX64 = set("0123456789abcdef")
HASH_FIELDS = ("model_sha256", "protocol_sha256", "golden_sha256", "runtime_sha256")


def canonical(value) -> bytes:
    """Канонический JSON: sort_keys, компактные разделители, UTF-8, без NaN/Inf."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_hex64(value, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= HEX64:
        raise ValueError(f"{label}: должен быть SHA-256 (64 строчных hex-символа)")


def _require_nonempty_str(value, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: нужна непустая строка")


def _is_real_number(value) -> bool:
    # bool — подкласс int, для метрик он запрещён явно.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def cache_key(data_path: Path, config: dict) -> str:
    """SHA-256 канонического JSON {data_sha256, config}.

    Байты входа читаются по data_path, имя файла и mtime не участвуют.
    Порядок ключей config значения не имеет. Любое изменение байтов данных,
    ревизии токенизатора, шаблона, max_length или версии кода подготовки
    меняет ключ.
    """
    if not isinstance(config, dict):
        raise ValueError("config должен быть словарем")
    data = Path(data_path).read_bytes()
    payload = {"data_sha256": sha256_hex(data), "config": config}
    return sha256_hex(canonical(payload))


def validate_release(manifest: dict, report: dict) -> None:
    """Полная проверка пары (manifest, report). Нарушение контракта — ValueError."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest: должен быть словарем")
    if not isinstance(report, dict):
        raise ValueError("report: должен быть словарем")

    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ValueError("manifest.schema_version: разрешен только int 1")
    _require_nonempty_str(manifest.get("model_id"), "manifest.model_id")
    _require_nonempty_str(manifest.get("artifact_uri"), "manifest.artifact_uri")
    for field in HASH_FIELDS:
        _require_hex64(manifest.get(field), f"manifest.{field}")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        raise ValueError("manifest.runtime: нужен непустой словарь")
    if manifest["runtime_sha256"] != sha256_hex(canonical(runtime)):
        raise ValueError("manifest.runtime_sha256 не совпадает с каноническим runtime")

    if report.get("status") != "complete":
        raise ValueError(f"report.status: ожидается 'complete', получено {report.get('status')!r}")
    # passed обязан быть настоящим boolean True: строка 'true'/'false' или 1 не подходят.
    if report.get("passed") is not True:
        raise ValueError("report.passed: разрешен только настоящий boolean True")
    _require_nonempty_str(report.get("evaluation_run_id"), "report.evaluation_run_id")

    if report.get("model_id") != manifest["model_id"]:
        raise ValueError("report.model_id не совпадает с manifest.model_id")
    for field in HASH_FIELDS:
        if report.get(field) != manifest[field]:
            raise ValueError(f"report.{field} не совпадает с manifest.{field}")

    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("report.metrics: нужен словарь")
    for name in ("accuracy", "format_rate"):
        value = metrics.get(name)
        if not _is_real_number(value):
            raise ValueError(f"report.metrics.{name}: число (не bool/строка) обязательно")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"report.metrics.{name}: NaN/Inf запрещены")
        if not 0 <= value <= 1:
            raise ValueError(f"report.metrics.{name}: значение вне [0, 1]")


def publish(manifest: dict, report: dict, target: Path) -> None:
    """Валидация, затем атомарная запись manifest в target.

    Запись: временный файл рядом с целью -> flush + fsync -> os.replace.
    При отказе валидации или ошибке записи прежний файл остается побайтно
    неизменным, временный файл удаляется, исключение выходит наружу.
    Повтор одинакового выпуска записывает те же байты.
    """
    validate_release(manifest, report)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
