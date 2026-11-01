# Ветка выбирается до первого присваивания: подряд два `?=` не работают
# (первое уже определяет переменную, второе игнорируется), из-за этого на
# Windows вызывался несуществующий python3.
ifeq ($(OS),Windows_NT)
  PYTHON ?= .venv/Scripts/python.exe
else
  PYTHON ?= python3
endif

.PHONY: check check-fast check-flow release-real smoke bench aba clean
# Быстрые проверки без весов: компиляция + все юнит/интеграционные тесты
# (13 приложенных + собственные; stub-бэкенд, без Prefect, без модели).
check:
	$(PYTHON) -m compileall -q release.py process.py lab.py service.py backends.py flow.py tests
	$(PYTHON) -m unittest discover -s tests -v

# То же, но только тесты контрактов релиза (мгновенно, чистая stdlib).
check-fast:
	$(PYTHON) -m unittest discover -s tests -p "test_release.py" -v

# Prefect-flow: кеш, восстановление, идемпотентность (требует prefect).
check-flow:
	$(PYTHON) -m unittest tests.test_flow_prefect -v

# Реальный выпуск baseline из ДЗ 6+7 (нужен ../hw67 и HF-кеш Qwen3-0.6B).
release-real:
	$(PYTHON) make_real_release.py

# Реальный smoke: сервис на Qwen3-0.6B из artifacts/real.
smoke:
	$(PYTHON) scripts/real_smoke.py

# Замер: 8 запросов при параллельности 1 и 2 (реальный сервис).
bench:
	$(PYTHON) scripts/bench.py

# STUB-опыт A -> B -> A с перезапусками и откатом.
aba:
	$(PYTHON) scripts/aba_experience.py

clean:
	rm -rf .lab .lab-*
