.PHONY: install test lint format seed collect watch happy-pr happy-gr

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

seed:
	uv run python -m src sources seed

collect:
	uv run python -m src collect

watch:
	uv run python -m src collect --watch

happy-pr:
	bash scripts/happy_path_pr.sh

happy-gr:
	bash scripts/happy_path_gr.sh
