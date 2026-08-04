from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler


def configure_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [
        RichHandler(rich_tracebacks=True, show_path=verbose, markup=False)
    ]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def redact_secret(value: str | None) -> str:
    return "<configured>" if value else "<missing>"
