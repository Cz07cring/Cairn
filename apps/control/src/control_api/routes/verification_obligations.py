"""VerificationObligation 只读面：QUARANTINED 行即可读 quarantine inbox。

internal 与公开 /api/v1 同语义；公开面供 Web 观察，只读 ≠ DONE。
"""

import base64
import hashlib
import hmac
import json
from typing import Annotated, Literal
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.verification import VerificationObligationResource
from control_kernel.storage.obligations import get_obligation, list_obligations
from control_kernel.storage.policies import ScopeNotFound
from fastapi import Depends, FastAPI, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal

ObligationStatus = Literal["OPEN", "ASSESSED", "QUARANTINED", "SUPERSEDED"]


def register_verification_obligation_routes(
    app: FastAPI, identity, store, cursor_key=None
) -> None:
    @app.get(
        "/internal/v1/verification-obligations",
        response_model=Envelope[list[VerificationObligationResource]],
    )
    @app.get(
        "/api/v1/verification-obligations",
        response_model=Envelope[list[VerificationObligationResource]],
    )
    def list_verification_obligations(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        project_id: Annotated[UUID, Query()],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        goal_id: Annotated[UUID | None, Query()] = None,
        activity_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[ObligationStatus | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin", "worker"}:
            raise ApiError(403, "FORBIDDEN", "没有 VerificationObligation 读取权限")
        if cursor_key is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "分页密钥未配置")
        secret = cursor_key()
        # 路径纳入分页 scope，避免 internal/public cursor 串用
        scope = json.dumps(
            [
                request.url.path,
                str(project_id),
                str(goal_id) if goal_id else None,
                str(activity_id) if activity_id else None,
                status,
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
                expected = hmac.new(
                    secret, (scope + id_value).encode(), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = list_obligations(
                store(request).engine,
                p.sub,
                p.project_ids,
                project_id=project_id,
                goal_id=goal_id,
                activity_id=activity_id,
                status=status,
                limit=limit + 1,
                after=after,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(secret, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode(
                (value + "." + mac).encode()
            ).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.get(
        "/internal/v1/verification-obligations/{obligation_id}",
        response_model=Envelope[VerificationObligationResource],
    )
    @app.get(
        "/api/v1/verification-obligations/{obligation_id}",
        response_model=Envelope[VerificationObligationResource],
    )
    def get_verification_obligation(
        obligation_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin", "worker"}:
            raise ApiError(403, "FORBIDDEN", "没有 VerificationObligation 读取权限")
        try:
            result = get_obligation(
                store(request).engine, obligation_id, p.sub, p.project_ids
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
