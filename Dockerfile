FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HUB_ROOT=/app

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY config.yaml sources.json ./
COPY src ./src
COPY MVP ./MVP

RUN mkdir -p /app/data /app/MVP/data

EXPOSE 3002

HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
  CMD [".venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3002/api/pilot/status', timeout=3).read()"]

CMD [".venv/bin/python", "-m", "MVP.backend.app", "--host", "0.0.0.0", "--port", "3002", "--live", "--interval", "600", "--news-active-days", "30", "--db", "/app/MVP/data/pilot.db"]
