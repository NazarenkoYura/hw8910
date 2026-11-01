# Триггеры: событие → действие (команды этого проекта)

| Событие | Ожидаемое действие | Команды проекта |
|---|---|---|
| Изменился код (процесс/сервис) | Быстрые тесты без весов; при изменении поведения генерации — повтор полной оценки (не требовалось: генерация не менялась) | `make check` (compileall + 27 тестов); при поломке `git diff 1-й-коммит -- release.py` для разбора |
| Изменились данные оценки (`data/eval/*` ДЗ 6+7) | Проверка схемы/версии, инвалидация зависимого кеша (ключ включает SHA-256 байтов входа и все поля config), объяснение необходимости нового обучения по роли данных | `python make_real_release.py` пересобирает release-report из замороженных отчетов; кеш подготовки: `flow.py run` после изменения `input.txt`/`config.json` → `PREPARED` (не `CACHE_HIT`) |
| Новый адаптер (кандидат) | Проверка загрузки, полная оценка тем же протоколом, правило допуска ДЗ 6+7 перед обновлением принятой версии; отказ сохраняется, baseline продолжает обслуживаться | `gate/hw67_solution.py::quality_gate(baseline_eval, candidate_eval, ["probability"])`; сейчас: REFUSE (behavioral 0/3) → `artifacts/real/release-report-candidate-refused.json` |
| Изменился runtime (dtype/template/лимит генерации) | Новый eval-отчет, новый runtime_sha256 — правка хеша запрещена | `release.validate_release` отклоняет расхождение `runtime_sha256` (публичный тест `test_runtime_change_with_stale_hash_rejected`) |

## Команды проверки (локальный CI)

```bash
make check          # 13 приложенных + 11 интеграционных + 3 Prefect-теста
make check-fast     # только контрактные тесты release.py
make check-flow     # Prefect: кеш/восстановление/идемпотентность
```

Демонстрация красного прогона: `evidence/after/check-broken.log` (намеренная
поломка `release.py`, exit 1) → восстановление → `evidence/after/check-green.log`
(exit 0). GitHub Actions (ci.yml) при пуше выполнит те же команды; удалённый run
не настроен, поэтому защита merge НЕ заявляется — локальный эквивалент полностью
засчитывается по условию.
