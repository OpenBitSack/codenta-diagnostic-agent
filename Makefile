PY ?= .venv/bin/python

.PHONY: install test demo reports load serve worker compose-up compose-down clean

install:
	uv venv --python 3.12 .venv && uv pip install -e ".[dev,llm,postgres]"

test:
	$(PY) -m pytest

demo:
	$(PY) scripts/demo.py

reports:
	$(PY) scripts/demo.py --only a,b --report-dir docs/generated

load:
	$(PY) scripts/load_test.py --tickets 1000 --unique-runs 60 --workers 4

serve:
	CODENTA_RUN_WORKERS=1 $(PY) -m uvicorn codenta_diag.main:app --reload --port 8000

worker:
	$(PY) -m codenta_diag.worker

compose-up:
	docker compose up --build

compose-down:
	docker compose down -v

clean:
	rm -f codenta_diag.db codenta_diag.db-wal codenta_diag.db-shm
