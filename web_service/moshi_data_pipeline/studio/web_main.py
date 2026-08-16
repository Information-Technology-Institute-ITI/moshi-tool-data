from __future__ import annotations

import os


def web_port_from_environment() -> int:
    value = os.environ.get("MOSHI_WEB_PORT", "").strip()
    if not value:
        raise RuntimeError("MOSHI_WEB_PORT is required")
    try:
        port = int(value)
    except ValueError as exc:
        raise RuntimeError("MOSHI_WEB_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise RuntimeError("MOSHI_WEB_PORT must be between 1 and 65535")
    return port


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("The web service requires uvicorn") from exc

    host = os.environ.get("MOSHI_WEB_BIND_ADDRESS", "127.0.0.1").strip()
    if not host:
        raise RuntimeError("MOSHI_WEB_BIND_ADDRESS cannot be empty")
    uvicorn.run(
        "moshi_data_pipeline.studio.asgi:app",
        host=host,
        port=web_port_from_environment(),
        workers=1,
        proxy_headers=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
