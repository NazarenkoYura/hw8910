#!/usr/bin/env bash
# Чистое воспроизведение D5/D6/D7 через питоновский щуп (без кодировочных артефактов curl).
set -u
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8
PY=python
PROTO=hw8910-broken
OUT=evidence/before
mkdir -p "$OUT"

rm -rf .lab-h
$PY $PROTO/lab.py --workspace .lab-h init > "$OUT/h-init.log" 2>&1
$PY $PROTO/service.py --workspace .lab-h --port 8092 --warmup-seconds 10 > "$OUT/h-server.log" 2>&1 &
SRV=$!
sleep 2

echo "== D5: готовность во время прогрева =="
$PY evidence/http_probe.py .lab-h 8092 ready > "$OUT/d5-ready.log" 2>&1

echo "== D6: рапорт об указателе вместо загруженной версии =="
$PY -c "import hashlib;print(hashlib.sha256(open('.lab-h/accepted.json','rb').read()).hexdigest())" > "$OUT/d6-accepted-sha-before.txt"
$PY $PROTO/lab.py --workspace .lab-h run > "$OUT/d6-publish.log" 2>&1
$PY -c "import hashlib;print(hashlib.sha256(open('.lab-h/accepted.json','rb').read()).hexdigest())" > "$OUT/d6-accepted-sha-after.txt"
$PY evidence/http_probe.py .lab-h 8092 mismatch > "$OUT/d6-mismatch.log" 2>&1

echo "== D7: неверные запросы доходят до backend =="
$PY evidence/http_probe.py .lab-h 8092 badrequests > "$OUT/d7-bad-requests.log" 2>&1

kill $SRV 2>/dev/null
sleep 1
cat "$OUT/d5-ready.log" "$OUT/d6-mismatch.log" "$OUT/d7-bad-requests.log"
