"""Task 证据列表：只读 EvidenceEnvelope。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.evidence import EvidenceEnvelopeResource
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.storage.evidence import list_for_task
from control_kernel.storage.policies import ScopeNotFound
from fastapi import Depends, FastAPI, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_evidence_routes(app: FastAPI, identity, store, cursor_key) -> None:
    @app.get(
        "/api/v1/tasks/{task_id}/evidence",
        response_model=Envelope[list[EvidenceEnvelopeResource]],
    )
    def list_task_evidence(
        task_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有证据读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/tasks/{task_id}/evidence",
                str(task_id),
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
            rows = list_for_task(
                store(request).engine, task_id, p.sub, p.project_ids, limit + 1, after
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
