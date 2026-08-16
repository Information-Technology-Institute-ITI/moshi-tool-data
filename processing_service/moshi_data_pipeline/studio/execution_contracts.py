from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from moshi_data_pipeline.studio.domain import AnnotationDocument, ClipPlanDocument


class ProcessingPaths(Protocol):
    """Filesystem surface available to processing code.

    Implementations may point at the local Studio workspace or at an isolated
    per-attempt directory materialized by the GPU processing service.
    """

    root: Path
    exports: Path

    def resolve_relative(self, value: str) -> Path: ...

    def relative(self, path: Path) -> str: ...

    def source_root(self, source_id: str) -> Path: ...

    def canonical_audio(self, source_id: str) -> Path: ...

    def canonical_channels(self, source_id: str) -> Path: ...

    def video_proxy(self, source_id: str) -> Path: ...

    def peaks(self, source_id: str) -> Path: ...

    def artifact(self, source_id: str, name: str) -> Path: ...


class ProcessingState(Protocol):
    """Typed state operations needed by processing algorithms.

    The GPU implementation is in-memory and cannot access the web catalog.
    """

    def get_project(self, project_id: str) -> dict[str, Any]: ...

    def get_source(self, source_id: str) -> dict[str, Any]: ...

    def list_sources(self, project_id: str) -> list[dict[str, Any]]: ...

    def latest_annotation(self, source_id: str) -> AnnotationDocument: ...

    def save_annotation(
        self,
        source_id: str,
        expected_version: int,
        annotation: AnnotationDocument,
    ) -> AnnotationDocument: ...

    def replace_initial_annotation(
        self,
        source_id: str,
        annotation: AnnotationDocument,
    ) -> AnnotationDocument: ...

    def update_source(self, source_id: str, **values: Any) -> dict[str, Any]: ...

    def get_clip_plan(self, source_id: str) -> ClipPlanDocument | None: ...

    def clip_decisions(self, source_id: str) -> dict[str, dict[str, Any]]: ...

    def overlap_recoveries(self, source_id: str) -> list[dict[str, Any]]: ...

    def replace_overlap_recoveries(
        self,
        source_id: str,
        annotation_version: int,
        records: list[dict[str, Any]],
    ) -> None: ...

    def update_overlap_details(
        self,
        source_id: str,
        region_id: str,
        details: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_export(self, export_id: str) -> dict[str, Any]: ...
