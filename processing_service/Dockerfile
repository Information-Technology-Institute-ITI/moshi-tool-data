FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04

COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /usr/local/bin/

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    MOSHI_WORKER_CACHE=/cache \
    HF_HOME=/cache/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && uv python install 3.11

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra ml --extra separation --extra review --no-dev --no-install-project --python 3.11
COPY moshi_data_pipeline/ ./moshi_data_pipeline/
COPY config.example.yaml ./config.example.yaml
RUN uv sync --frozen --extra ml --extra separation --extra review --no-dev --python 3.11

ENV PATH="/app/.venv/bin:${PATH}"
VOLUME ["/cache"]

CMD ["python", "-m", "moshi_data_pipeline.remote_worker_main"]
