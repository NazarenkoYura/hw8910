"""HTTP-сервис выбранной модели: FastAPI, внедряемый backend, /health /ready /generate.

Исправления относительно дефектного прототипа:
- /ready отвечает 200 только после фактической загрузки весов и прогрева;
  до этого 503, и /generate не обслуживает запросы;
- в ответе /generate — фактически загруженный model_id (backend.loaded_id),
  а не модель из свежепрочитанного accepted.json;
- неверный запрос (пустой prompt, max_new_tokens вне поддерживаемого диапазона,
  нечисловой лимит) получает 4xx ДО вызова backend; /debug.calls не меняется;
- манифест читается из принятого указателя workspace, файлы артефакта
  пересчитываются до загрузки (TransformersBackend), лимиты зафиксированы.

CLI совместим с прототипом:
  python3 service.py --port 8091 --warmup-seconds 10 [--workspace .lab] [--backend stub]
"""
import argparse
import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import backends

# Поддерживаемый диапазон генерации для стенда (зафиксирован в INCIDENTS.txt п.7
# и в runtime.generation.max_new_tokens манифеста).
MAX_NEW_TOKENS_LIMIT = 32
MAX_PROMPT_CHARS = 2048
MAX_BODY_BYTES = 65536


class ServiceState:
    """Состояние сервиса: backend, готовность, лимиты, статистика вызовов."""

    def __init__(self, root: Path, warmup_seconds: float, backend_name: str = "stub",
                 device: str = "cpu", max_parallel: int | None = None):
        self.root = Path(root)
        self.backend_name = backend_name
        self.device = device
        self.ready = False
        self.warmup_finished = threading.Event()
        self.started_at = time.time()
        self.load_error = None
        self.max_new_tokens_limit = MAX_NEW_TOKENS_LIMIT
        # Для реальных весов последовательное вычисление; stub допускает 2.
        self.max_parallel = max_parallel if max_parallel is not None else (
            1 if backend_name == "transformers" else 2)
        self.semaphore = threading.Semaphore(self.max_parallel)
        self.backend = None
        self.warmup_seconds = warmup_seconds
        self._load_thread = threading.Thread(target=self._load_and_warm, daemon=True)
        self._load_thread.start()

    def _load_and_warm(self):
        try:
            manifest = json.loads((self.root / "accepted.json").read_text(encoding="utf-8"))
            self.manifest = manifest
            runtime_limit = manifest.get("runtime", {}).get("generation", {}).get("max_new_tokens")
            if isinstance(runtime_limit, int) and 0 < runtime_limit <= MAX_NEW_TOKENS_LIMIT:
                self.max_new_tokens_limit = runtime_limit
            model_dir = None
            if self.backend_name == "transformers":
                # Локальный каталог артефакта: local_path из манифеста, иначе
                # artifact_uri как каталог. При отсутствии — загрузка по URI
                # с закрепленной ревизией (без локальной сверки хешей файлов).
                local = manifest.get("local_path") or manifest.get("artifact_uri", "")
                candidate = Path(local)
                if candidate.is_dir():
                    model_dir = candidate
            self.backend = backends.create_backend(
                self.backend_name, manifest, model_dir=model_dir, device=self.device)
            # Прогрев: один реальный вызов генерации до перевода /ready в 200.
            self.backend.generate("warmup", 1)
            # warmup_seconds имитирует длительную загрузку/прогрев: до его конца
            # сервис жив (/health), но не готов (/ready 503, генерация 503).
            time.sleep(self.warmup_seconds)
            self.warmup_finished.set()
            self.ready = True
        except BaseException as error:  # загрузка не удалась: сервис жив, но не готов
            self.load_error = f"{type(error).__name__}: {error}"
            self.warmup_finished.set()

    def generate(self, request: dict) -> dict:
        """Валидация + генерация. Вызывается из threadpool (см. /generate):
        блокирующие операции не держат event loop uvicorn."""
        prompt = request.get("prompt")
        limit = request.get("max_new_tokens", self.max_new_tokens_limit)
        # Валидация ДО вызова backend: ни один неверный запрос не запускает вычисление.
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt: нужна непустая строка")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt: превышен лимит {MAX_PROMPT_CHARS} символов")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("max_new_tokens: нужно целое число")
        if not 1 <= limit <= self.max_new_tokens_limit:
            raise ValueError(
                f"max_new_tokens: поддерживается диапазон 1..{self.max_new_tokens_limit}")
        queue_started = time.perf_counter()
        acquired = self.semaphore.acquire(timeout=30)
        if not acquired:
            raise TimeoutError("очередь генерации переполнена")
        queue_seconds = time.perf_counter() - queue_started
        try:
            started = time.perf_counter()
            result = self.backend.generate(prompt, limit)
            result.setdefault("output_tokens", 0)
            result.update(
                model_id=self.backend.loaded_id,            # фактически загруженная версия
                runtime_sha256=self.backend.runtime_sha256,
                request_id=str(uuid.uuid4()),
                ready=self.ready,
                queue_seconds=queue_seconds,
                generation_seconds=time.perf_counter() - started,
            )
            return result
        finally:
            self.semaphore.release()


def advertised(state: ServiceState):
    """Модель по принятому указателю (может отличаться от загруженной до перезапуска)."""
    try:
        return json.loads((state.root / "accepted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def create_app(root: Path, warmup_seconds: float = 2.0, backend_name: str = "stub",
               device: str = "cpu", max_parallel: int | None = None) -> FastAPI:
    state = ServiceState(root, warmup_seconds, backend_name, device, max_parallel)

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app = FastAPI(title="mlops26 hw8-9-10 model service", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"alive": True, "backend": getattr(state.backend, "kind", state.backend_name),
                "ready": state.ready}

    @app.get("/ready")
    def ready():
        if state.ready:
            return JSONResponse(status_code=200, content={
                "ready": True, "model_id": state.backend.loaded_id,
                "runtime_sha256": state.backend.runtime_sha256})
        body = {"ready": False, "warmup_finished": state.warmup_finished.is_set()}
        if state.load_error:
            body["load_error"] = state.load_error
        return JSONResponse(status_code=503, content=body)

    @app.get("/debug")
    def debug():
        """Тестовый маршрут: показывает факт загрузки и вызовы backend.
        В реальном публичном API не выставляется (раскрывает внутренности)."""
        adv = advertised(state)
        return {
            "loaded_id": getattr(state.backend, "loaded_id", None),
            "runtime_sha256": getattr(state.backend, "runtime_sha256", None),
            "advertised_id": adv.get("model_id") if adv else None,
            "pointer_matches_loaded": bool(
                adv and state.backend and adv.get("model_id") == state.backend.loaded_id),
            "calls": getattr(state.backend, "calls", 0),
            "warmup_finished": state.warmup_finished.is_set(),
            "ready": state.ready,
            "backend": getattr(state.backend, "kind", state.backend_name),
        }

    @app.post("/generate")
    async def generate(request: Request):
        if not state.ready:
            return JSONResponse(status_code=503, content={
                "error": "сервис еще не готов: загрузка/прогрев не завершены"})
        size = int(request.headers.get("Content-Length", "0") or 0)
        if size < 0 or size > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "body too large"})
        raw = await request.body()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("тело запроса должно быть JSON-объектом")
        except (ValueError, UnicodeDecodeError) as error:
            return JSONResponse(status_code=400, content={"error": str(error)})
        # Валидация и генерация блокирующие: выполняем в threadpool, чтобы не
        # блокировать event loop (иначе очередь не видна и параллельные
        # запросы монополизируют цикл событий).
        from starlette.concurrency import run_in_threadpool

        try:
            result = await run_in_threadpool(state.generate, payload)
        except (ValueError, TypeError) as error:
            return JSONResponse(status_code=400, content={"error": str(error)})
        except TimeoutError as error:
            return JSONResponse(status_code=503, content={"error": str(error)})
        return JSONResponse(status_code=200, content=result)

    app.state.service_state = state
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path(".lab"))
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--warmup-seconds", type=float, default=5)
    parser.add_argument("--backend", choices=["stub", "transformers"], default="stub")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--max-parallel", type=int, default=None)
    args = parser.parse_args()

    import uvicorn

    app = create_app(args.workspace, args.warmup_seconds, args.backend,
                     args.device, args.max_parallel)
    print(f"SERVE http://127.0.0.1:{args.port} workspace={args.workspace} "
          f"backend={args.backend}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
