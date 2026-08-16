"""Local, human-in-the-loop dataset studio."""

from typing import Any

__all__ = ["create_studio_app", "serve_studio"]


def __getattr__(name: str) -> Any:
    """Load the web server lazily so protocol-only clients stay dependency-light."""
    if name in __all__:
        from moshi_data_pipeline.studio import server

        return getattr(server, name)
    raise AttributeError(name)
