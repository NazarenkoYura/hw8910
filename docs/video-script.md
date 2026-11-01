# Сценарий видео 5–8 минут (для записи)

Тяжёлые шаги (загрузка весов, полный замер) показываются по сохранённым
журналам — это разрешено условием. Все команды выполняются из корня репозитория
в `.venv` (Python 3.12): `source .venv/Scripts/activate`.

## 1. Исходная версия и расследование (~1 мин)
- `git log --oneline` — два коммита: исходный прототип и исправленный;
  `git diff <commit1> <commit2> --stat`.
- Открыть `defects.csv` — семь причин, воспроизведение, тесты, логи до/после.
- `evidence/before/public-tests-red.log` — 54 падения приложенных тестов
  на исходном коде; `evidence/before/integration-red.log` — 10 красных
  собственных интеграционных тестов.

## 2. Красный/зелёный (~1 мин)
- `HW8910_TARGET=hw8910-broken python -m unittest tests.test_integration -v`
  (кратко: падает на дубликатах публикаций, неполном отчёте, готовности).
- `make check` — 27 тестов зелёные (`evidence/after/all-tests-green.log`).
- Показать `evidence/after/check-broken.log` (намеренная поломка, exit 1)
  → `make check` снова зеленый.

## 3. Prefect: сбой → восстановление → кеш (~1.5 мин)
- `python flow.py --workspace .lab-flow init`
- `python flow.py --workspace .lab-flow run --fail-after-prepare` — сбой
  после подготовки (`evidence/after/flow-crash.log`, задача prepare Completed,
  результат сохранен в `.lab-flow/prefect-results/`).
- Повтор без сбоя — задача `prepare-input` в состоянии **Cached**,
  подготовка не повторяется (`evidence/after/flow-recover.log`).
- Изменить `input.txt` → новый прогон — `PREPARED` с новым ключом
  (`evidence/after/flow-invalidate.log`); журнал публикаций — по одному
  событию на выпуск.

## 4. Gate и реальный выпуск (~1 мин)
- `artifacts/real/release-report-candidate-refused.json` — честный отказ
  кандидата гейтом ДЗ 6+7 (behavioral 0/3), порог не ослаблялся.
- `artifacts/real/manifest-baseline.json` — model_sha256 по реальным файлам,
  runtime = протокол оценки; `accepted.json` + `publications.jsonl` (одно
  событие). Публикация: `make release-real`.

## 5. Реальный сервис (~1.5 мин)
- `make smoke` (по журналу `evidence/after/real-smoke.log`): /ready после
  проверки хешей и загрузки Qwen3-0.6B; реальный ответ с model_id,
  runtime_sha256, request_id, output_tokens, длительностями; неверный лимит
  — 400 до генерации, calls не растет.
- STUB-опыт A→B→A: `make aba` (`evidence/after/aba-experience.log`) —
  указатель сменился, ответ остался A; после перезапуска — B; откат — снова A.

## 6. Замер (~1 мин)
- `make bench` (`evidence/after/bench-real.log`, `measurements/bench-*.csv`):
  conc1 avg 2.7s против conc2 avg 5.5s при том же времени пакета — очередь.
- Объяснить: prefill/decode, почему последовательный Transformers-сервис не
  доказывает скорость SGLang/vLLM; p95 на 8 запросах не вычисляется.
