.PHONY: install test lint format seed collect watch tg-login tg-status process quality serve docker-up happy-pr happy-gr

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

serve:
	uv run python -m src serve

docker-up:
	docker compose up --build
