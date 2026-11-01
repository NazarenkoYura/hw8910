"""Собственные интеграционные тесты ДЗ 8+9-10 (сверх приложенных 13).

Запуск против исправленной версии (по умолчанию — корень репозитория):
    python -m unittest tests.test_integration -v
Красный прогон против ИСХОДНОГО прототипа (доказательство дефекта):
    HW8910_TARGET=hw8910-broken python -m unittest tests.test_integration -v

Тесты работают с обоими вариантами через совместимые интерфейсы: CLI
(lab.py init/run), HTTP-маршруты (/health /ready /debug /generate) и модуль
release.py. Границы настоящие: subprocess с отдельными рабочими каталогами,
локальный HTTP-сервер, файловая система. Модель заменена stub.

Покрытие обязательных сценариев:
- отказ и несовместимый отчет ........ test_incomplete_report_refused,
                                        test_report_binds_to_its_model, test_reject_report_keeps_bytes
- сбой/восстановление ................ test_crash_recovery_and_cache_invalidation
- изменение входа .................... test_crash_recovery_and_cache_invalidation
- повтор выпуска, дубликаты .......... test_repeat_release_no_duplicate_events,
                                        test_journal_tolerates_torn_tail
- сбой между указателем и журналом ... test_crash_between_pointer_and_journal_recovers
- откат к ранее принятой ............. test_rollback_to_previous_accepted
- готовность ......................... test_ready_requires_warmup
- фактически загруженная версия ...... test_response_reports_loaded_version
- неверный запрос без вызова backend . test_invalid_requests_do_not_reach_backend
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TARGET = Path(os.environ.get("HW8910_TARGET", Path(__file__).resolve().parent.parent))
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def load_target_module(name):
    spec = importlib.util.spec_from_file_location(f"target_{name}_{id(TARGET)}", TARGET / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_lab(workspace: Path, *args, timeout=60):
    return subprocess.run(
        [sys.executable, "lab.py", "--workspace", str(workspace), *args],
        cwd=TARGET, env=CHILD_ENV, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode())
        except Exception:
            payload = {}
        return error.code, payload


def stop_service(proc):
    """Аккуратное завершение сервисного subprocess (без ResourceWarning)."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def start_service(workspace: Path, warmup_seconds=1.0, backend=None):
    """Запуск HTTP-сервиса цели с флагами, совместимыми с прототипом.
    --backend не передается по умолчанию: у исходного прототипа такого флага
    нет, в обеих версиях по умолчанию stub."""
    port = free_port()
    args = [sys.executable, "service.py", "--workspace", str(workspace),
            "--port", str(port), "--warmup-seconds", str(warmup_seconds)]
    if backend is not None:
        args += ["--backend", backend]
    proc = subprocess.Popen(
        args,
        cwd=TARGET, env=CHILD_ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 30
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError(f"Сервис завершился до готовности:\n{out}")
        try:
            status, last = http(port, "GET", "/health", timeout=2)
            if status == 200:
                return proc, port
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError):
            time.sleep(0.15)
    proc.terminate()
    raise TimeoutError(f"Сервис не поднялся за 30 с: {last}")


def events(workspace: Path):
    path = workspace / "publications.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha_of(path: Path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class IntegrationTests(unittest.TestCase):
    """Каждый опыт — в отдельном рабочем каталоге, чтобы предыдущий не скрывал ошибку."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        init = run_lab(self.ws, "init")
        self.assertEqual(init.returncode, 0, f"init упал:\n{init.stderr}")

    # --- subprocess: кеш, сбой/восстановление, изменение входа (дефект 1) ---

    def test_crash_recovery_and_cache_invalidation(self):
        """Сбой после подготовки; повтор в новом процессе берет кеш; изменение
        байтов входа или конфига выполняет подготовку заново."""
        crash = run_lab(self.ws, "run", "--fail-after-prepare")
        self.assertNotEqual(crash.returncode, 0, "сбой после подготовки должен падать")
        self.assertIn("PREPARED", crash.stdout)
        prepared_sha = crash.stdout.split("PREPARED")[1].split()[0]

        recover = run_lab(self.ws, "run")
        self.assertEqual(recover.returncode, 0, recover.stderr)
        self.assertIn("CACHE_HIT", recover.stdout, "повтор должен взять кеш подготовки")
        self.assertIn(prepared_sha, recover.stdout, "из кеша должен вернуться тот же результат")

        (self.ws / "input.txt").write_bytes(b"changed bytes for invalidation\n")
        changed = run_lab(self.ws, "run")
        self.assertIn("PREPARED", changed.stdout,
                      "после изменения байтов входа кеш не может быть валиден")
        new_sha = changed.stdout.split("PREPARED")[1].split()[0]
        self.assertNotEqual(new_sha, prepared_sha, "новые байты должны дать другой результат")

        config = json.loads((self.ws / "config.json").read_text(encoding="utf-8"))
        config["max_length"] = config["max_length"] + 64
        (self.ws / "config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8")
        reconfig = run_lab(self.ws, "run")
        self.assertIn("PREPARED", reconfig.stdout,
                      "изменение max_length (конфиг подготовки) должно инвалидировать кеш")

    # --- subprocess: отказ и несовместимый отчет (дефект 2) ---

    def test_incomplete_report_refused(self):
        """Неполный отчет (status=running, passed='false' строкой) не получает выпуск."""
        before = sha_of(self.ws / "accepted.json")
        result = run_lab(self.ws, "run", "--report", "report-incomplete.json")
        self.assertNotEqual(result.returncode, 0,
                            "неполный отчет обязан отказать с ненулевым кодом")
        self.assertEqual(sha_of(self.ws / "accepted.json"), before,
                         "принятая версия не должна измениться при отказе")
        pointer = json.loads((self.ws / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["model_id"], "fixture-A")

    def test_report_binds_to_its_model(self):
        """Отчет допуска применяется к своему кандидату: отчет A не может
        выпустить кандидата B (у исходного прототипа манифест захардкожен)."""
        result = run_lab(self.ws, "run", "--report", "report-A.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        pointer = json.loads((self.ws / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["model_id"], "fixture-A",
                         "отчет fixture-A должен выпускать модель fixture-A")

    def test_reject_report_keeps_bytes(self):
        """Отказ по качеству сохраняет прежний accepted.json побайтно и не пишет событие."""
        self.assertEqual(run_lab(self.ws, "run").returncode, 0)
        before = sha_of(self.ws / "accepted.json")
        count = len(events(self.ws))
        result = run_lab(self.ws, "run", "--report", "report-reject.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sha_of(self.ws / "accepted.json"), before)
        self.assertEqual(len(events(self.ws)), count, "отказ не создает событие выпуска")

    # --- subprocess: повтор выпуска и журнал событий (дефект 4) ---

    def test_repeat_release_no_duplicate_events(self):
        """Два одинаковых выпуска B: журнал попыток — две попытки, журнал
        событий фактического выпуска — ровно одна запись, байты указателя те же."""
        first = run_lab(self.ws, "run")
        self.assertEqual(first.returncode, 0, first.stderr)
        pointer_bytes = (self.ws / "accepted.json").read_bytes()
        self.assertEqual(len(events(self.ws)), 1)

        second = run_lab(self.ws, "run")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(events(self.ws)), 1,
                         f"повтор того же выпуска не должен создавать событие: {events(self.ws)}")
        self.assertEqual((self.ws / "accepted.json").read_bytes(), pointer_bytes,
                         "чистый повтор не меняет принятый указатель")
        attempts_path = self.ws / "attempts.jsonl"
        if attempts_path.exists():  # журнал попыток отделен от событий
            attempts = [l for l in attempts_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertGreaterEqual(len(attempts), 2)

    def test_journal_tolerates_torn_tail(self):
        """Оборванная строка журнала (сбой во время записи) не ломает журнал:
        после повтора все строки читаются, событие ровно одно."""
        self.assertEqual(run_lab(self.ws, "run").returncode, 0)
        with (self.ws / "publications.jsonl").open("ab") as f:
            f.write(b'{"event": "publi')  # оборванная запись без перевода строки
        result = run_lab(self.ws, "run")
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = (self.ws / "publications.jsonl").read_text(encoding="utf-8")
        lines = [line for line in raw.splitlines() if line.strip()]
        try:
            parsed = [json.loads(line) for line in lines]
        except ValueError as error:
            self.fail(f"журнал событий нечитаем после повтора (сбояная строка не отброшена): {error}")
        self.assertEqual(len(parsed), 1, f"событие выпуска должно остаться одним: {parsed}")

    def test_crash_between_pointer_and_journal_recovers(self):
        """Сбой между обновлением указателя и записью журнала: повтор
        восстанавливает согласованность без двойного события."""
        release = load_target_module("release")
        manifest = json.loads((self.ws / "manifest-B.json").read_text(encoding="utf-8"))
        report = json.loads((self.ws / "report-B.json").read_text(encoding="utf-8"))
        # Имитация аварии: указатель заменен, журнал событий не записан.
        release.publish(manifest, report, self.ws / "accepted.json")
        self.assertFalse((self.ws / "publications.jsonl").exists())

        result = run_lab(self.ws, "run")
        self.assertEqual(result.returncode, 0, result.stderr)
        pointer = json.loads((self.ws / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["model_id"], "fixture-B")
        self.assertEqual(len(events(self.ws)), 1,
                         f"после восстановления событие ровно одно: {events(self.ws)}")
        again = run_lab(self.ws, "run")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(events(self.ws)), 1, "повтор после восстановления не дублирует событие")

    # --- subprocess: откат к ранее принятой версии ---

    def test_rollback_to_previous_accepted(self):
        """A -> B -> откат к A: указатель возвращается, у каждого выпуска ровно
        одно событие, повторы не создают новых событий."""
        self.assertEqual(run_lab(self.ws, "run").returncode, 0)  # B
        rollback = run_lab(self.ws, "run", "--report", "report-A.json")  # откат к A
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        pointer = json.loads((self.ws / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["model_id"], "fixture-A", "откат должен вернуть принятую A")
        ids = sorted(e["model_id"] for e in events(self.ws))
        self.assertEqual(ids, ["fixture-A", "fixture-B"], f"по одному событию на выпуск: {ids}")

        repeat = run_lab(self.ws, "run")  # повтор B после отката
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(len(events(self.ws)), 2, "повтор уже публиковавшегося B: без нового события")
        again = run_lab(self.ws, "run", "--report", "report-A.json")  # возврат к A
        self.assertEqual(again.returncode, 0, again.stderr)
        pointer = json.loads((self.ws / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["model_id"], "fixture-A")
        self.assertEqual(len(events(self.ws)), 2)

    # --- HTTP: готовность (дефект 5) ---

    def test_ready_requires_warmup(self):
        """Сразу после старта /ready и /generate не обслуживают до конца прогрева."""
        proc, port = start_service(self.ws, warmup_seconds=4)
        self.addCleanup(stop_service, proc)
        status, ready_body = http(port, "GET", "/ready")
        self.assertEqual(status, 503, f"во время прогрева ready должен быть 503: {ready_body}")
        status, gen_body = http(port, "POST", "/generate",
                                {"prompt": "probe", "max_new_tokens": 8})
        self.assertEqual(status, 503, f"генерация до готовности не обслуживается: {gen_body}")
        _, debug = http(port, "GET", "/debug")
        self.assertFalse(debug["warmup_finished"])
        deadline = time.time() + 30
        while time.time() < deadline:
            status, ready_body = http(port, "GET", "/ready")
            if status == 200:
                break
            time.sleep(0.2)
        self.assertEqual(status, 200, "после прогрева ready должен стать 200")
        status, gen_body = http(port, "POST", "/generate",
                                {"prompt": "probe", "max_new_tokens": 8})
        self.assertEqual(status, 200, gen_body)

    # --- HTTP: фактически загруженная версия (дефект 6) ---

    def test_response_reports_loaded_version(self):
        """Сервис на A, в другом процессе опубликован B: до перезапуска ответ
        рапортует о фактически загруженной A, а не об указателе B."""
        proc, port = start_service(self.ws, warmup_seconds=0.2)
        self.addCleanup(stop_service, proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            status, ready_body = http(port, "GET", "/ready")
            if status == 200:
                break
            time.sleep(0.2)
        self.assertEqual(status, 200, ready_body)
        _, debug = http(port, "GET", "/debug")
        self.assertEqual(debug["loaded_id"], "fixture-A")

        publish = run_lab(self.ws, "run")
        self.assertEqual(publish.returncode, 0, publish.stderr)
        pointer = json.loads((self.ws / "accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["model_id"], "fixture-B", "указатель должен переключиться на B")

        status, gen = http(port, "POST", "/generate",
                           {"prompt": "probe", "max_new_tokens": 8})
        self.assertEqual(status, 200, gen)
        _, debug = http(port, "GET", "/debug")
        self.assertEqual(debug["loaded_id"], "fixture-A",
                         "без перезапуска веса A все еще загружены")
        self.assertEqual(gen["model_id"], debug["loaded_id"],
                         f"ответ должен рапортовать о загруженной версии, не об указателе: {gen}")

    # --- HTTP: неверный запрос не доходит до backend (дефект 7) ---

    def test_invalid_requests_do_not_reach_backend(self):
        """Пустой prompt, max_new_tokens=0/100000/строка, не-строка prompt:
        4xx до генерации, число вызовов backend не меняется. Максимум стенда — 32."""
        proc, port = start_service(self.ws, warmup_seconds=0.2)
        self.addCleanup(stop_service, proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            if http(port, "GET", "/ready")[0] == 200:
                break
            time.sleep(0.2)
        _, before = http(port, "GET", "/debug")
        bad = [
            {"prompt": "", "max_new_tokens": 8},
            {"prompt": "probe", "max_new_tokens": 0},
            {"prompt": "probe", "max_new_tokens": 100000},
            {"prompt": "probe", "max_new_tokens": "8"},
            {"prompt": 123, "max_new_tokens": 8},
        ]
        for case in bad:
            status, body = http(port, "POST", "/generate", case)
            self.assertTrue(400 <= status < 500,
                            msg=f"{case}: ожидается 4xx, получено {status} {body}")
        _, after = http(port, "GET", "/debug")
        self.assertEqual(after["calls"], before["calls"],
                         "неверные запросы не должны вызывать backend")
        status, gen = http(port, "POST", "/generate",
                           {"prompt": "probe", "max_new_tokens": 8})
        self.assertEqual(status, 200, gen)
        _, final = http(port, "GET", "/debug")
        self.assertEqual(final["calls"], before["calls"] + 1,
                         "ровно один валидный запрос дошел до backend")

if __name__ == "__main__":
    unittest.main(verbosity=2)
