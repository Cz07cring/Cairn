"""Typed VerificationProfile routes, delegating shared protocol behavior."""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope
from control_kernel.protocols.verification import (
    VerificationProfileCreate,
    VerificationProfileResource,
)
from control_kernel.storage.policies import VerificationProfiles
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.identity import Principal

from .configurations import ConfigurationHTTP


def register_verification_routes(app: FastAPI, identity, store, cursor_key) -> None:
    handler = ConfigurationHTTP(
        store,
        cursor_key,
        VerificationProfiles,
        "VerificationProfile",
        "/api/v1/verification-profiles",
    )

    @app.post(
        "/api/v1/verification-profiles",
        status_code=201,
        response_model=Envelope[VerificationProfileResource],
    )
    def create_verification(
        body: VerificationProfileCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return handler.create(body, request, p, idempotency_key)

    @app.get(
        "/api/v1/verification-profiles", response_model=Envelope[list[VerificationProfileResource]]
    )
    def list_verification(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        return handler.list(project_id, request, p, limit, cursor)
