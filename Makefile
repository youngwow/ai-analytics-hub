.PHONY: install test lint verify format seed collect watch tg-login tg-status process quality happy-pr happy-gr web demo docker-up docker-down docker-logs docker-status prepare-pilot pilot-a pilot-b pilot-report

RUN_PY := $(shell if command -v uv >/dev/null 2>&1; then printf 'uv run python'; elif [ -x .venv/bin/python ]; then printf '.venv/bin/python'; else printf 'python3'; fi)

install:
	uv sync

test:
	$(RUN_PY) -m pytest -q

lint:
	$(RUN_PY) -m ruff check .

verify: test lint

format:
	$(RUN_PY) -m ruff format src tests MVP

seed:
	$(RUN_PY) -m src sources seed

collect:
	$(RUN_PY) -m src collect

watch:
	$(RUN_PY) -m src collect --watch

tg-login:
	$(RUN_PY) -m src telegram login

tg-status:
	$(RUN_PY) -m src telegram status

happy-pr:
	bash scripts/happy_path_pr.sh

happy-gr:
	bash scripts/happy_path_gr.sh

process:
	$(RUN_PY) -m src process

quality:
	$(RUN_PY) -m src quality --gold

web:
	$(RUN_PY) -m MVP.backend.app --host 127.0.0.1 --port 3002

demo:
	$(RUN_PY) -m MVP.backend.app --host 127.0.0.1 --port 3002

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f radar

docker-status:
	docker compose ps

prepare-pilot:
	$(RUN_PY) benchmarks/b3_e2e/prepare_pilot.py

pilot-a:
	$(RUN_PY) -m MVP.backend.app --host 127.0.0.1 --port 3002 --db artifacts/b3/pilot/product_A.db

pilot-b:
	$(RUN_PY) -m MVP.backend.app --host 127.0.0.1 --port 3002 --db artifacts/b3/pilot/product_B.db

pilot-report:
	$(RUN_PY) benchmarks/b3_e2e/import_pilot_workbook.py artifacts/b3/PILOT_EXCEL_BASELINE_V1.xlsx --output artifacts/b3/human_observations.jsonl
	$(RUN_PY) benchmarks/b3_e2e/evaluate_human.py artifacts/b3/human_observations.jsonl --value-scope post_collection --report artifacts/b3/HUMAN_PILOT_RESULT.json
