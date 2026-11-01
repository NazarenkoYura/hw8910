"""Scorer и quality gate для части B. Контракт: EVALUATION.md и tests/test_eval.py."""
import json
import math


def _parse_answer(raw, choices):
    """Строгий разбор: JSON-объект с единственным полем answer, значение — строка из choices."""
    if not isinstance(raw, str) or not raw.strip():
        return None, False
    try:
        value = json.loads(raw)
    except ValueError:
        return None, False
    if not isinstance(value, dict) or set(value) != {"answer"}:
        return None, False
    answer = value["answer"]
    if not isinstance(answer, str):
        return None, False
    return answer, answer in choices


def _counts(items):
    """n, correct, valid, errors и доли. Пустые/битые ответы остаются в знаменателе."""
    n = len(items)
    if n == 0:
        return {"n": 0, "correct": 0, "valid": 0, "errors": 0,
                "accuracy": 0.0, "format_rate": 0.0}
    correct = sum(1 for i in items if i["correct"])
    valid = sum(1 for i in items if i["valid"])
    errors = sum(1 for i in items if i.get("error"))
    return {"n": n, "correct": correct, "valid": valid, "errors": errors,
            "accuracy": correct / n, "format_rate": valid / n}


def score(rows, outputs):
    """overall, slices и per_item. Ошибки и пустые ответы не удаляются из знаменателя.

    outputs: [{id, raw, error, latency_seconds}, ...]; матчинг по id, порядок не важен.
    Несовпадающие/дублирующиеся id и пустой набор — ValueError (ошибка контракта).
    """
    if not rows or not outputs:
        raise ValueError("Пустой набор вопросов или ответов")
    row_ids = []
    for row in rows:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id.strip():
            raise ValueError("Вопросы должны иметь непустой строковый id")
        row_ids.append(row_id)
    out_ids = []
    for out in outputs:
        out_id = out.get("id")
        if not isinstance(out_id, str) or not out_id.strip():
            raise ValueError("Ответы должны иметь непустой строковый id")
        out_ids.append(out_id)
    if len(row_ids) != len(set(row_ids)):
        raise ValueError("Дублирующиеся id в вопросах")
    if len(out_ids) != len(set(out_ids)):
        raise ValueError("Дублирующиеся id в ответах")
    if set(out_ids) != set(row_ids):
        raise ValueError("Ответы не совпадают с вопросами по id")
    by_id = {o["id"]: o for o in outputs}
    per_item = []
    for row in rows:
        out = by_id[row["id"]]
        parsed, valid = _parse_answer(out.get("raw"), row.get("choices"))
        per_item.append({"id": row["id"], "raw": out.get("raw"), "parsed": parsed,
                         "valid": valid, "correct": valid and parsed == row["expected"],
                         "error": out.get("error"), "slices": list(row.get("slices", []))})
    slices = {name: _counts([i for i in per_item if name in i["slices"]])
              for name in {s for i in per_item for s in i["slices"]}}
    return {"overall": _counts(per_item), "slices": slices, "per_item": per_item}


def _check_structure(report, label):
    """Полнота отчёта, конечность метрик и соответствие overall/per_item."""
    if not isinstance(report, dict):
        raise ValueError(f"{label}: отчёт отсутствует")
    for key in ("overall", "slices", "per_item", "behavioral", "status", "model",
                "protocol_sha256", "golden_sha256", "baseline_spec_sha256"):
        if key not in report:
            raise ValueError(f"{label}: неполный отчёт, нет {key}")
    if report["status"] != "complete":
        raise ValueError(f"{label}: оценка не завершена (status={report['status']!r})")
    if not isinstance(report["model"], dict) or not report["model"]:
        raise ValueError(f"{label}: в отчёте нет конкретной модели")
    overall = report["overall"]
    if not isinstance(overall, dict):
        raise ValueError(f"{label}: overall сломан")
    for key in ("n", "correct", "valid", "errors", "accuracy", "format_rate"):
        if key not in overall:
            raise ValueError(f"{label}.overall: нет {key}")
    if overall["n"] < 50:
        raise ValueError(f"{label}: golden меньше 50 примеров (n={overall['n']})")
    if overall["errors"]:
        raise ValueError(f"{label}: в generation есть ошибки ({overall['errors']})")
    for key in ("accuracy", "format_rate"):
        value = overall[key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{label}.overall.{key}: неконечное или вне [0,1] значение")
    per = report["per_item"]
    if not isinstance(per, list) or len(per) != overall["n"] or len({i["id"] for i in per}) != len(per):
        raise ValueError(f"{label}: per_item не соответствует n")
    if not isinstance(report["slices"], dict) or len(report["slices"]) < 3:
        raise ValueError(f"{label}: нужно минимум три среза")
    for name, s in report["slices"].items():
        for key in ("n", "correct", "valid", "errors", "accuracy", "format_rate"):
            if key not in s:
                raise ValueError(f"{label}.slices.{name}: нет {key}")
        if any(not isinstance(s[k], (int, float)) or not math.isfinite(s[k]) for k in ("accuracy", "format_rate")):
            raise ValueError(f"{label}.slices.{name}: нечисловая метрика")
        if not math.isclose(s["accuracy"], s["correct"] / s["n"], rel_tol=1e-9):
            raise ValueError(f"{label}.slices.{name}: accuracy не соответствует counts")
    recalculated = _counts(per)
    for key in ("n", "correct", "valid", "errors"):
        if overall[key] != recalculated[key]:
            raise ValueError(f"{label}.overall.{key} не соответствует per_item")
    for key in ("accuracy", "format_rate"):
        if not math.isclose(overall[key], recalculated[key], rel_tol=1e-9):
            raise ValueError(f"{label}.overall.{key} не соответствует per_item")


def _check_behavioral(report, label):
    """Обязательные behavioral-кейсы: минимум три и все выполнены."""
    behavioral = report["behavioral"]
    if not isinstance(behavioral, dict) or not isinstance(behavioral.get("overall"), dict):
        raise ValueError(f"{label}: нет behavioural-отчёта")
    overall = behavioral["overall"]
    if overall.get("n", 0) < 3:
        raise ValueError(f"{label}: behavioral меньше трёх кейсов")
    if overall.get("errors"):
        raise ValueError(f"{label}: ошибки в behavioral")
    if overall.get("correct") != overall.get("n") or overall.get("accuracy") != 1.0:
        raise ValueError(f"{label}: пройдены не все behavioral-кейсы "
                         f"({overall.get('correct')}/{overall.get('n')})")


def quality_gate(baseline, candidate, critical_slices):
    """{passed: True, ...} или ValueError. Допуск регрессии — ноль, порог до измерения."""
    if not isinstance(critical_slices, list) or not critical_slices:
        raise ValueError("Нужен непустой список критических срезов")
    if any(not isinstance(name, str) or not name.strip() for name in critical_slices):
        raise ValueError("Критический срез должен быть непустой строкой")
    _check_structure(baseline, "baseline")
    _check_structure(candidate, "candidate")
    for key in ("protocol_sha256", "golden_sha256", "baseline_spec_sha256"):
        if baseline[key] != candidate[key]:
            raise ValueError(f"Отчёты несовместимы: различается {key}")
    base_ids = [i["id"] for i in baseline["per_item"]]
    cand_ids = [i["id"] for i in candidate["per_item"]]
    if base_ids != cand_ids:
        raise ValueError("Разные id вопросов в отчётах")
    if set(baseline["slices"]) != set(candidate["slices"]):
        raise ValueError("Разные имена срезов")
    for name, s in baseline["slices"].items():
        if candidate["slices"][name]["n"] != s["n"]:
            raise ValueError(f"Срез {name}: разные размеры групп")
    missing = [s for s in critical_slices if s not in baseline["slices"]]
    if missing:
        raise ValueError(f"Критические срезы отсутствуют: {missing}")
    b, c = baseline["overall"], candidate["overall"]
    if c["accuracy"] < b["accuracy"]:
        raise ValueError(f"Общая accuracy ниже baseline: {c['accuracy']:.4f} < {b['accuracy']:.4f}")
    if c["format_rate"] < b["format_rate"]:
        raise ValueError(f"Format rate ниже baseline: {c['format_rate']:.4f} < {b['format_rate']:.4f}")
    for name in critical_slices:
        bs, cs = baseline["slices"][name], candidate["slices"][name]
        if cs["accuracy"] < bs["accuracy"]:
            raise ValueError(f"Регрессия критического среза {name}: "
                             f"{bs['accuracy']:.4f} -> {cs['accuracy']:.4f}")
    _check_behavioral(candidate, "candidate")
    return {"passed": True,
            "baseline_accuracy": b["accuracy"], "candidate_accuracy": c["accuracy"],
            "baseline_format_rate": b["format_rate"], "candidate_format_rate": c["format_rate"],
            "critical_slices": {name: {"baseline": baseline["slices"][name]["accuracy"],
                                        "candidate": candidate["slices"][name]["accuracy"]}
                                for name in critical_slices},
            "behavioral": {"n": candidate["behavioral"]["overall"]["n"],
                           "correct": candidate["behavioral"]["overall"]["correct"]},
            "decision": "accept"}
