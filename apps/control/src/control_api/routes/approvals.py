"""公共审批列表、裁决与撤销。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.approvals import ApprovalDecision, ApprovalResource, ApprovalStatus
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import ControlRequest, StateRevisionConflict
from control_kernel.storage.approvals import ApprovalRejected, Approvals
from control_kernel.storage.policies import ScopeNotFound
from fastapi import Depends, FastAPI, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_approval_routes(app: FastAPI, identity, store, cursor_key) -> None:
    @app.get("/api/v1/approvals", response_model=Envelope[list[ApprovalResource]])
    def list_approvals(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        project_id: Annotated[UUID, Query()],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        status: Annotated[ApprovalStatus | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有审批读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/approvals",
                str(project_id),
                p.sub,
                sorted(p.project_ids),
                status,
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
            rows = Approvals(store(request).engine).list_for_project(
                project_id, p.sub, p.project_ids, limit + 1, after, status
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

    @app.post(
        "/api/v1/approvals/{approval_id}/decision",
        response_model=Envelope[ApprovalResource],
    )
    def decide_approval(
        approval_id: UUID,
        body: ApprovalDecision,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "需要审批权限")
        try:
            result = Approvals(store(request).engine).decide(
                approval_id, p.sub, p.project_ids, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "状态版本冲突") from exc
        except ApprovalRejected as exc:
            raise ApiError(409, "INVALID_STATE", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/approvals/{approval_id}/revoke",
        response_model=Envelope[ApprovalResource],
    )
    def revoke_approval(
        approval_id: UUID,
        body: ControlRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "需要审批权限")
        try:
            result = Approvals(store(request).engine).revoke(
                approval_id, p.sub, p.project_ids, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "状态版本冲突") from exc
        except ApprovalRejected as exc:
            raise ApiError(409, "INVALID_STATE", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
