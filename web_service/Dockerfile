FROM node:22-bookworm-slim AS frontend

WORKDIR /build
COPY frontend/ ./frontend/
RUN corepack enable \
    && cd frontend \
    && pnpm install --frozen-lockfile \
    && pnpm build

FROM ghcr.io/astral-sh/uv:0.11.23 AS uv

FROM python:3.11-slim-bookworm AS runtime

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY moshi_data_pipeline/ ./moshi_data_pipeline/
COPY config.example.yaml ./config.example.yaml
COPY --from=frontend /build/moshi_data_pipeline/studio/static/ ./moshi_data_pipeline/studio/static/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MOSHI_WORKSPACE=/data/studio_workspace

VOLUME ["/data/studio_workspace"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"

CMD ["uvicorn", "moshi_data_pipeline.studio.asgi:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--proxy-headers"]
