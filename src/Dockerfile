# syntax=docker/dockerfile:1.7
# Сборка по шаблону: образ uv → builder с зависимостями → тонкий runtime без root.
# Контекст сборки — корень репозитория (см. docker-compose.yaml).
ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.18

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    HUB_ROOT=/app \
    HOST=0.0.0.0

RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY --link --from=builder /opt/venv /opt/venv
COPY --link src/ /app/src/
COPY --link config.yaml sources.json /app/

# data/ — SQLite и WAL; монтируется томом, но каталог должен принадлежать app.
RUN mkdir -p /app/data && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c 'import os,urllib.request as r; r.urlopen("http://127.0.0.1:8000" + os.environ.get("API_PREFIX", "/api/v1") + "/health/ready", timeout=3)'

CMD ["uvicorn", "src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
