"""GET /api/v1/memories 与 POST /internal/v1/memories（PROPOSED）。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated, Literal
from uuid import UUID

from control_kernel.protocols.memory import MemoryCreate, MemoryResource
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import CommandOperation, PlanRejected
from control_kernel.storage.memories import create_memory, list_memories
from control_kernel.storage.memory_index import start_index_memory
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal

MemoryKindQuery = Literal["fact", "decision", "failure", "hypothesis", "question"]


def register_memory_routes(app: FastAPI, identity, store, cursor_key) -> None:
    @app.get("/api/v1/memories", response_model=Envelope[list[MemoryResource]])
    def list_memory_resources(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        project_id: Annotated[UUID, Query()],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        kind: Annotated[MemoryKindQuery | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=10000)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有记忆读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/memories",
                str(project_id),
                kind,
                q,
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
            rows = list_memories(
                store(request).engine,
                p.sub,
                p.project_ids,
                project_id=project_id,
                kind=kind,
                q=q,
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

    @app.post(
        "/internal/v1/memories",
        status_code=201,
        response_model=Envelope[MemoryResource],
    )
    def post_memory(
        body: MemoryCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        # 临时 ACL：冻结规格为「记忆服务」身份；专用主体未落地前仅 admin 可写，operator 只读。
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有记忆写入权限（暂限 admin；记忆服务身份未落地）")
        try:
            result = create_memory(
                store(request).engine,
                p.sub,
                p.project_ids,
                idempotency_key,
                body,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受新记忆") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", str(exc) or "记忆请求无效") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/projects/{project_id}/memory-index",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def post_memory_index(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        """排队 INDEX_MEMORY；有 PROPOSED 记忆时才接受。"""
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        try:
            result = start_index_memory(
                store(request).engine,
                project_id,
                p.sub,
                p.project_ids,
                idempotency_key,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受索引命令") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
