.PHONY: install test lint format seed collect watch

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
