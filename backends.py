"""Бэкенды сервиса через общий интерфейс: StubBackend и TransformersBackend.

Общий интерфейс (инвариант для тестов управления):
  backend.loaded_id        — фактически загруженная версия (model_id);
  backend.runtime_sha256   — runtime загруженного артефакта;
  backend.calls            — число фактических вызовов генерации;
  backend.generate(prompt, limit) -> {'answer': str, 'output_tokens': int}

TransformersBackend загружает базу/адаптер из принятого манифеста: сверяет
SHA-256 закрепленных файлов артефакта (manifest.file_hashes) до загрузки и
использует dtype/генерационные параметры из manifest.runtime — те же условия,
что при оценке в ДЗ 6+7.
"""
import hashlib
import json
from pathlib import Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_artifact_files(manifest: dict, model_dir: Path) -> None:
    """Пересчет хешей реальных файлов до загрузки (validate_release метаданные
    не проверяет веса — это отдельная обязательная проверка)."""
    file_hashes = manifest.get("file_hashes")
    if not isinstance(file_hashes, dict) or not file_hashes:
        raise ValueError("В манифесте нет закрепленного списка file_hashes")
    for name, expected in file_hashes.items():
        path = model_dir / name
        if not path.is_file():
            raise ValueError(f"Файл артефакта отсутствует: {name}")
        actual = _sha256_file(path)
        if actual != expected:
            raise ValueError(f"Хеш файла {name}: {actual} != {expected}")


class StubBackend:
    """Легкий имитатор: явно помеченные ответы STUB ONLY, без весов и сети."""

    def __init__(self, manifest: dict, **_):
        self.loaded_id = manifest["model_id"]
        self.calls = 0
        self.runtime_sha256 = manifest["runtime_sha256"]
        self.kind = "STUB ONLY"

    def generate(self, prompt, limit):
        self.calls += 1
        import time

        time.sleep(0.01)
        return {"answer": f"STUB ONLY: {self.loaded_id}", "output_tokens": 1}


def parse_artifact_uri(uri: str):
    """'huggingface:repo@revision' -> (repo, revision). Иначе (uri, None)."""
    if isinstance(uri, str) and uri.startswith("huggingface:"):
        rest = uri[len("huggingface:"):]
        repo, _, revision = rest.partition("@")
        return repo, revision or None
    return uri, None


class TransformersBackend:
    """Загрузчик модели ДЗ 6+7 (Transformers). model_dir — локальный каталог
    артефакта (например snapshot HF-кеша), закрепленный манифестом; при его
    наличии хеши файлов сверяются до загрузки. Иначе URI вида
    huggingface:repo@revision загружается с закрепленной ревизией."""

    def __init__(self, manifest: dict, model_dir=None, device: str = "cpu", **_):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.kind = "transformers"
        self.calls = 0
        self.device = device
        dir_path = Path(model_dir) if model_dir else None
        if dir_path is not None:
            verify_artifact_files(manifest, dir_path)
            source, revision = str(dir_path), None
        else:
            source, revision = parse_artifact_uri(manifest["artifact_uri"])
        runtime = manifest.get("runtime", {})
        generation = runtime.get("generation", {})
        dtype_name = runtime.get("dtype", "float32")
        dtype = getattr(torch, dtype_name, None) if isinstance(dtype_name, str) else None
        if dtype is None:
            raise ValueError(f"runtime.dtype {dtype_name!r} не распознан torch")
        self.tokenizer = AutoTokenizer.from_pretrained(source, revision=revision)
        self.model = AutoModelForCausalLM.from_pretrained(source, revision=revision,
                                                          torch_dtype=dtype)
        self.model.to(device)
        self.model.eval()
        self.loaded_id = manifest["model_id"]
        self.runtime_sha256 = manifest["runtime_sha256"]
        self.generation_defaults = {
            "do_sample": bool(generation.get("do_sample", False)),
            "max_new_tokens": int(generation.get("max_new_tokens", 32)),
            "enable_thinking": bool(generation.get("enable_thinking", False)),
        }

    def generate(self, prompt, limit):
        import time

        import torch

        self.calls += 1
        started = time.perf_counter()
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        with torch.no_grad():
            output = self.model.generate(
                **inputs,
                max_new_tokens=int(limit),
                do_sample=self.generation_defaults["do_sample"],
                pad_token_id=self.tokenizer.eos_token_id,
            )
        new_tokens = output[0][inputs["input_ids"].shape[1]:]
        answer = self.tokenizer.decode(new_tokens, skip_special_tokens=True)
        return {
            "answer": answer,
            "output_tokens": int(new_tokens.shape[0]),
            "generation_seconds_backend": time.perf_counter() - started,
        }


def create_backend(name: str, manifest: dict, **kwargs):
    if name == "stub":
        return StubBackend(manifest, **kwargs)
    if name == "transformers":
        return TransformersBackend(manifest, **kwargs)
    raise ValueError(f"Неизвестный backend: {name}")
