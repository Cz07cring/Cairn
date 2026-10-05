"""Typed ModelProfile routes, delegating shared protocol behavior."""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.models import (
    ModelInvocationResource,
    ModelProfileCreate,
    ModelProfileResource,
)
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.storage.policies import ModelProfiles, ScopeNotFound
from control_kernel.storage.probe import list_invocations
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal

from .configurations import ConfigurationHTTP


def register_model_routes(app: FastAPI, identity, store, cursor_key) -> None:
    handler = ConfigurationHTTP(
        store, cursor_key, ModelProfiles, "ModelProfile", "/api/v1/model-profiles"
    )

    @app.post(
        "/api/v1/model-profiles", status_code=201, response_model=Envelope[ModelProfileResource]
    )
    def create_model(
        body: ModelProfileCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return handler.create(body, request, p, idempotency_key)

    @app.get("/api/v1/model-profiles", response_model=Envelope[list[ModelProfileResource]])
    def list_model(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        return handler.list(project_id, request, p, limit, cursor)

    @app.get(
        "/api/v1/model-invocations",
        response_model=Envelope[list[ModelInvocationResource]],
    )
    def list_model_invocations(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        project_id: Annotated[UUID, Query()],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        goal_id: Annotated[UUID | None, Query()] = None,
        activity_id: Annotated[UUID | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有模型调用读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/model-invocations",
                str(project_id),
                str(goal_id) if goal_id else None,
                str(activity_id) if activity_id else None,
                p.sub,
                sorted(p.project_ids),
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(secret, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = list_invocations(
                store(request).engine,
                p.sub,
                p.project_ids,
                project_id=project_id,
                goal_id=goal_id,
                activity_id=activity_id,
                limit=limit + 1,
                after=after,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )
