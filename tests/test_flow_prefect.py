"""Тесты Prefect-flow (зеленая сторона; требуют prefect, пропускаются без него).

Доказывают свойства целевой архитектуры: устойчивый кеш стадии подготовки
между процессами, идемпотентная публикация, отказ по гейту без изменения
указателя. Красные доказательства дефектов — в test_integration.py (через
совместимые CLI/HTTP интерфейсы); у исходного прототипа Prefect-flow нет,
поэтому здесь красный прогон не требуется.
"""
import json
import tempfile
import unittest
from pathlib import Path

try:
    import flow as flow_module
    HAS_PREFECT = True
except ImportError:  # prefect не установлен: быстрые проверки остаются рабочими
    HAS_PREFECT = False


@unittest.skipUnless(HAS_PREFECT, "prefect не установлен")
class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        flow_module.lab.initialize(self.ws)

    def test_prepare_cache_and_idempotent_publish(self):
        """Первый прогон исполняет подготовку и публикует; повтор в том же
        процессе берет кеш Prefect и не создает второе событие."""
        first = flow_module.release_flow(workspace=str(self.ws))
        self.assertFalse(first["prepared_from_cache"])
        self.assertEqual(first["outcome"], "published")
        executions_after_first = flow_module.PREPARE_EXECUTIONS

        second = flow_module.release_flow(workspace=str(self.ws))
        self.assertTrue(second["prepared_from_cache"],
                        "повтор должен взять кешированный результат подготовки")
        self.assertEqual(flow_module.PREPARE_EXECUTIONS, executions_after_first,
                         "тело подготовки не должно исполняться при попадании в кеш")
        self.assertEqual(second["outcome"], "already-published")
        self.assertEqual(second["prepared_input_sha256"], first["prepared_input_sha256"])
        events = [json.loads(l) for l in
                  (self.ws / "publications.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(events), 1, "повтор не создает дубликат события")

    def test_input_change_invalidates_prefect_cache(self):
        first = flow_module.release_flow(workspace=str(self.ws))
        (self.ws / "input.txt").write_bytes(b"new content for flow cache test\n")
        second = flow_module.release_flow(workspace=str(self.ws))
        self.assertFalse(second["prepared_from_cache"],
                         "изменение входа должно исполнить подготовку заново")
        self.assertNotEqual(second["prepared_input_sha256"], first["prepared_input_sha256"])

    def test_gate_refusal_leaves_pointer(self):
        import hashlib

        before = hashlib.sha256((self.ws / "accepted.json").read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            flow_module.release_flow(workspace=str(self.ws), report_name="report-incomplete.json")
        after = hashlib.sha256((self.ws / "accepted.json").read_bytes()).hexdigest()
        self.assertEqual(before, after, "отказ гейта не меняет принятый указатель")
        self.assertFalse((self.ws / "publications.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
