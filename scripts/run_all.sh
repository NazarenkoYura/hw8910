#!/usr/bin/env bash
# Полный прогон ДЗ 8+9-10 одним запуском (Git Bash, Windows).
#
# ВАЖНО: шаги 1-2 КРАСНЫЕ — тесты специально запускаются против ИСХОДНОГО
# сломанного прототипа (hw8910-broken) и обязаны падать: это доказательства
# дефектов. Шаги 7-8 грузят реальные веса Qwen3-0.6B — это минуты на CPU.
# Общая длительность ~6-8 минут.
set -u
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
PYABS="$(pwd)/.venv/Scripts/python.exe"

step() { echo; echo "============================================================"; echo "== $*"; echo "============================================================"; }

step "1/9 КРАСНЫЙ: 11 интеграционных тестов на ИСХОДНОМ прототипе"
echo "   Ожидание: FAILED (failures=10) — так и надо"
HW8910_TARGET=hw8910-broken "$PYABS" -m unittest tests.test_integration 2>&1 | tail -3

step "2/9 КРАСНЫЙ: 13 приложенных тестов на ИСХОДНОМ прототипе"
echo "   Ожидание: FAILED (failures=54) — так и надо"
( cd hw8910-broken && "$PYABS" -m unittest discover -s tests 2>&1 | tail -3 )

step "3/9 ЗЕЛЁНЫЙ: все проверки на исправленной версии (make check)"
echo "   Ожидание: Ran 27 tests ... OK"
make check 2>&1 | grep -E "^(Ran|OK|FAILED)"

step "4/9 Prefect-тесты: кеш, идемпотентность, отказ гейта (make check-flow)"
echo "   Ожидание: Ran 3 tests ... OK"
make check-flow 2>&1 | grep -E "^(Ran|OK|FAILED)"

step "5/9 Prefect-flow: сбой -> восстановление из кеша -> инвалидация входа"
rm -rf .lab-flow
"$PYABS" flow.py --workspace .lab-flow init > /dev/null
echo "-- run --fail-after-prepare (ожидаемо падает, exit_code=1):"
"$PYABS" flow.py --workspace .lab-flow run --fail-after-prepare 2>&1 | grep -E "PREPARED|Finished in state|RuntimeError" | tail -4
echo "exit_code=${PIPESTATUS[0]}"
echo "-- повтор без сбоя (ожидание: prepare-input Cached, PUBLISHED):"
"$PYABS" flow.py --workspace .lab-flow run 2>&1 | grep -E "Cached|PUBLISHED" | tail -3
echo "-- меняем байты input.txt (ожидание: PREPARED с НОВЫМ хешем, не Cached):"
printf 'Другие данные для инвалидации кеша\n' > .lab-flow/input.txt
"$PYABS" flow.py --workspace .lab-flow run 2>&1 | grep -E "PREPARED|Cached|PUBLISHED" | tail -3
echo "-- событий фактического выпуска после всех прогонов (ожидание: 1):"
wc -l < .lab-flow/publications.jsonl

step "6/9 Реальный выпуск baseline из ДЗ 6+7: gate REFUSE кандидату -> публикация"
"$PYABS" make_real_release.py

step "7/9 Реальный smoke: сервис на Qwen3-0.6B (проверка хешей 1.5 ГБ + загрузка, минуты...)"
"$PYABS" scripts/real_smoke.py

step "8/9 Замер: одинаковые 8 запросов при параллельности 1 и 2 (минуты...)"
"$PYABS" scripts/bench.py 2>&1 | tail -8

step "9/9 STUB-опыт A -> B -> A: версия в памяти, перезапуск, откат"
"$PYABS" scripts/aba_experience.py

echo
echo "============================================================"
echo "== ГОТОВО. Доказательства: evidence/, замеры: measurements/"
echo "== Для видео: docs/video-script.md"
echo "============================================================"
