#!/usr/bin/env bash
# Воспроизведение семи дефектов на ИСХОДНОМ прототипе (hw8910-broken).
# Каждый опыт — в отдельном workspace. Логи: evidence/before/
set -u
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8
PY=python
PROTO=hw8910-broken
OUT=evidence/before
mkdir -p "$OUT"

sha() { "$PY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

echo "== D1: кеш подготовки игнорирует содержимое и конфиг =="
rm -rf .lab-d1
$PY $PROTO/lab.py --workspace .lab-d1 init > "$OUT/d1-init.log" 2>&1
$PY $PROTO/lab.py --workspace .lab-d1 run --fail-after-prepare > "$OUT/d1-crash.log" 2>&1
echo "exit_crash=$?" >> "$OUT/d1-crash.log"
$PY $PROTO/lab.py --workspace .lab-d1 run > "$OUT/d1-recover.log" 2>&1
printf 'Изменённые данные\n' > .lab-d1/input.txt
$PY $PROTO/lab.py --workspace .lab-d1 run > "$OUT/d1-stale.log" 2>&1
echo "cache entries:" >> "$OUT/d1-stale.log"; ls .lab-d1/cache >> "$OUT/d1-stale.log"

echo "== D2: неполный отчёт получает зелёный выпуск =="
rm -rf .lab-d2
$PY $PROTO/lab.py --workspace .lab-d2 init > "$OUT/d2-init.log" 2>&1
sha .lab-d2/accepted.json > "$OUT/d2-accepted-sha-before.txt"
cat .lab-d2/report-incomplete.json > "$OUT/d2-report-incomplete.json"
$PY $PROTO/lab.py --workspace .lab-d2 run --report report-incomplete.json > "$OUT/d2-publish.log" 2>&1
echo "exit=$?" >> "$OUT/d2-publish.log"
sha .lab-d2/accepted.json > "$OUT/d2-accepted-sha-after.txt"
"$PY" -c "import json;print(json.load(open('.lab-d2/accepted.json'))['model_id'])" >> "$OUT/d2-publish.log"

echo "== D4: повтор выпуска дублирует публикации =="
rm -rf .lab-d4
$PY $PROTO/lab.py --workspace .lab-d4 init > "$OUT/d4-init.log" 2>&1
$PY $PROTO/lab.py --workspace .lab-d4 run > "$OUT/d4-run1.log" 2>&1
$PY $PROTO/lab.py --workspace .lab-d4 run > "$OUT/d4-run2.log" 2>&1
cp .lab-d4/publications.jsonl "$OUT/d4-publications.jsonl"
"$PY" -c "print('publish_lines=',sum(1 for _ in open('.lab-d4/publications.jsonl')))" >> "$OUT/d4-run2.log"

echo "== D5/D6/D7: HTTP-сервис =="
rm -rf .lab-d5
$PY $PROTO/lab.py --workspace .lab-d5 init > "$OUT/d5-init.log" 2>&1
PORT=8091
$PY $PROTO/service.py --workspace .lab-d5 --port $PORT --warmup-seconds 10 > "$OUT/d5-server.log" 2>&1 &
SRV=$!
sleep 2
echo "-- сразу после старта (прогрев 10с ещё идёт):" > "$OUT/d5-ready.log"
curl -s http://127.0.0.1:$PORT/health >> "$OUT/d5-ready.log"; echo >> "$OUT/d5-ready.log"
echo -n "ready: " >> "$OUT/d5-ready.log"; curl -s http://127.0.0.1:$PORT/ready >> "$OUT/d5-ready.log"; echo >> "$OUT/d5-ready.log"
echo -n "debug: " >> "$OUT/d5-ready.log"; curl -s http://127.0.0.1:$PORT/debug >> "$OUT/d5-ready.log"; echo >> "$OUT/d5-ready.log"
echo -n "generate during warmup: " >> "$OUT/d5-ready.log"
curl -s -X POST http://127.0.0.1:$PORT/generate -H "Content-Type: application/json" -d '{"prompt":"тест","max_new_tokens":8}' >> "$OUT/d5-ready.log"; echo >> "$OUT/d5-ready.log"
echo "exit_generate=$?" >> "$OUT/d5-ready.log"

echo "== D6: сервис рапортует о модели из accepted.json, а не о загруженной =="
sha .lab-d5/accepted.json > "$OUT/d6-accepted-sha-before.txt"
$PY $PROTO/lab.py --workspace .lab-d5 run > "$OUT/d6-publish.log" 2>&1
sha .lab-d5/accepted.json > "$OUT/d6-accepted-sha-after.txt"
echo "-- service WITHOUT restart, after pointer switched to B:" > "$OUT/d6-mismatch.log"
curl -s http://127.0.0.1:$PORT/debug >> "$OUT/d6-mismatch.log"; echo >> "$OUT/d6-mismatch.log"
curl -s -X POST http://127.0.0.1:$PORT/generate -H "Content-Type: application/json" -d '{"prompt":"тест","max_new_tokens":8}' >> "$OUT/d6-mismatch.log"; echo >> "$OUT/d6-mismatch.log"

echo "== D7: неверные запросы доходят до backend =="
curl -s http://127.0.0.1:$PORT/debug > "$OUT/d7-calls-before.json"
curl -s -o /dev/null -w "empty_prompt_http=%{http_code}\n" -X POST http://127.0.0.1:$PORT/generate -H "Content-Type: application/json" -d '{"prompt":"","max_new_tokens":8}' > "$OUT/d7-bad-requests.log"
curl -s -o /dev/null -w "huge_limit_http=%{http_code}\n" -X POST http://127.0.0.1:$PORT/generate -H "Content-Type: application/json" -d '{"prompt":"тест","max_new_tokens":100000}' >> "$OUT/d7-bad-requests.log"
curl -s -o /dev/null -w "string_limit_http=%{http_code}\n" -X POST http://127.0.0.1:$PORT/generate -H "Content-Type: application/json" -d '{"prompt":"тест","max_new_tokens":"1000"}' >> "$OUT/d7-bad-requests.log"
curl -s http://127.0.0.1:$PORT/debug > "$OUT/d7-calls-after.json"
"$PY" -c "import json;b=json.load(open('$OUT/d7-calls-before.json'));a=json.load(open('$OUT/d7-calls-after.json'));print('calls_before=',b['calls'],'calls_after=',a['calls'])" >> "$OUT/d7-bad-requests.log"

kill $SRV 2>/dev/null
sleep 1
echo "=== ГОТОВО, логи в $OUT ==="
ls -la "$OUT"
