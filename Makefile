.PHONY: install test lint verify format seed collect watch tg-login tg-status process quality happy-pr happy-gr web demo prepare-pilot pilot-a pilot-b pilot-report

RUN_PY := $(shell if command -v uv >/dev/null 2>&1; then printf 'uv run python'; elif [ -x .venv/bin/python ]; then printf '.venv/bin/python'; else printf 'python3'; fi)

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check .

verify: test lint

format:
	uv run ruff format src tests

seed:
	uv run python -m src sources seed

collect:
	uv run python -m src collect

watch:
	uv run python -m src collect --watch

tg-login:
	uv run python -m src telegram login

tg-status:
	uv run python -m src telegram status

happy-pr:
	bash scripts/happy_path_pr.sh

happy-gr:
	bash scripts/happy_path_gr.sh

process:
	uv run python -m src process

quality:
	uv run python -m src quality --gold

web:
	uv run python -m src.product.web --host 127.0.0.1 --port 3002

demo:
	uv run python -m src.product.web --host 127.0.0.1 --port 3002 --db artifacts/demo/gs_labs_demo.db

prepare-pilot:
	$(RUN_PY) benchmarks/b3_e2e/prepare_pilot.py

pilot-a:
	$(RUN_PY) -m src.product.web --host 127.0.0.1 --port 3002 --db artifacts/b3/pilot/product_A.db

pilot-b:
	$(RUN_PY) -m src.product.web --host 127.0.0.1 --port 3002 --db artifacts/b3/pilot/product_B.db

pilot-report:
	$(RUN_PY) benchmarks/b3_e2e/import_pilot_workbook.py artifacts/b3/PILOT_EXCEL_BASELINE_V1.xlsx --output artifacts/b3/human_observations.jsonl
	$(RUN_PY) benchmarks/b3_e2e/evaluate_human.py artifacts/b3/human_observations.jsonl --value-scope post_collection --report artifacts/b3/HUMAN_PILOT_RESULT.json
