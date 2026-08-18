from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from moshi_data_pipeline.studio.catalog import StudioCatalog


@dataclass(frozen=True)
class RequestPrincipal:
    user_id: str
    email: str
    role: str
    status: str

    @classmethod
    def from_catalog_user(cls, user: dict[str, object]) -> RequestPrincipal:
        return cls(
            user_id=str(user["id"]),
            email=str(user["email"]),
            role=str(user["role"]),
            status=str(user["status"]),
        )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def require_principal(request: Request) -> RequestPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, RequestPrincipal) or principal.status != "active":
        raise HTTPException(status_code=401, detail="Sign in is required")
    return principal


def require_admin(principal: RequestPrincipal) -> RequestPrincipal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access is required")
    return principal


class CatalogAuthorization:
    def __init__(self, catalog: StudioCatalog):
        self.catalog = catalog

    def authorize_project(
        self,
        principal: RequestPrincipal,
        project_id: str,
    ) -> dict[str, Any]:
        owner_user_id = self.catalog.get_project_owner_id(project_id)
        if not principal.is_admin and owner_user_id != principal.user_id:
            raise KeyError(project_id)
        return self.catalog.get_project(project_id)

    def authorize_source(
        self,
        principal: RequestPrincipal,
        source_id: str,
    ) -> dict[str, Any]:
        try:
            project_id = self.catalog.get_source_project_id(source_id)
            self.authorize_project(principal, project_id)
        except KeyError:
            raise KeyError(source_id) from None
        return self.catalog.get_source(source_id)

    def authorize_job(
        self,
        principal: RequestPrincipal,
        job_id: str,
    ) -> dict[str, Any]:
        try:
            project_id = self.catalog.get_job_project_id(job_id)
            self.authorize_project(principal, project_id)
        except KeyError:
            raise KeyError(job_id) from None
        return self.catalog.get_job(job_id)

    def authorize_export(
        self,
        principal: RequestPrincipal,
        export_id: str,
    ) -> dict[str, Any]:
        try:
            project_id = self.catalog.get_export_project_id(export_id)
            self.authorize_project(principal, project_id)
        except KeyError:
            raise KeyError(export_id) from None
        return self.catalog.get_export(export_id)
