.PHONY: install test lint verify format seed collect watch tg-login tg-status process quality happy-pr happy-gr web demo

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
