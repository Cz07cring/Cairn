"""Typed Policy routes, delegating shared protocol behavior."""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.policies import PolicyCreate, PolicyResource
from control_kernel.protocols.projects import Envelope
from control_kernel.storage.policies import Policies
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.identity import Principal

from .configurations import ConfigurationHTTP


def register_policy_routes(app: FastAPI, identity, store, cursor_key) -> None:
    handler = ConfigurationHTTP(store, cursor_key, Policies, "Policy", "/api/v1/policies")

    @app.post("/api/v1/policies", status_code=201, response_model=Envelope[PolicyResource])
    def create_policy(
        body: PolicyCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return handler.create(body, request, p, idempotency_key)

    @app.get("/api/v1/policies", response_model=Envelope[list[PolicyResource]])
    def list_policy(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        return handler.list(project_id, request, p, limit, cursor)
