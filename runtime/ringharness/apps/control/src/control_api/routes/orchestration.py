"""TEMPORAL 内部 runtime：GET actions / POST admit；不开放 Temporal 管理端口。"""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import (
    ActivityLease,
    BindingStale,
    LeaseRejected,
    RuntimeActionsResult,
    RuntimeAdmitRequest,
    WorkerForbidden,
)
from control_kernel.storage.orchestration import admit_runtime_attempt, get_runtime_actions
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_orchestration_routes(
    app: FastAPI,
    identity,
    store,
    *,
    lease_ttl_seconds: int = 90,
) -> None:
    ttl = timedelta(seconds=lease_ttl_seconds)

    @app.get(
        "/internal/v1/runtime/actions",
        response_model=Envelope[RuntimeActionsResult],
    )
    def list_runtime_actions(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        goal_id: Annotated[UUID, Query()],
        owner_epoch: Annotated[str, Query(min_length=1, max_length=32)],
    ):
        """列出 TEMPORAL Goal 的 READY RuntimeActionRef；Kernel 计算，不派发。"""
        if "worker" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker/admin 查询 runtime actions")
        try:
            result = get_runtime_actions(
                store(request).engine,
                p.sub,
                goal_id=goal_id,
                expected_owner_epoch=owner_epoch,
                project_ids=p.project_ids,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/runtime/admit",
        response_model=Envelope[ActivityLease],
    )
    def admit_runtime(
        body: RuntimeAdmitRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        """具名准入 TEMPORAL READY Activity；镜像 claim 的 worker JWT + Idempotency-Key。"""
        try:
            result = admit_runtime_attempt(
                store(request).engine,
                p.sub,
                idempotency_key,
                body.activity_id,
                lease_ttl=ttl,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或 kinds 越权") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受准入") from exc
        except BindingStale as exc:
            raise ApiError(409, "BINDING_STALE", "活动绑定已过期，请刷新后重试") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
