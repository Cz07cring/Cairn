"""内部停止证明路由（05§3.6）：请求 / 回执 / 查询。"""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import (
    PlanRejected,
    StopReceipt,
    StopReceiptAccepted,
    StopRequest,
    StopResource,
    WorkerForbidden,
)
from control_kernel.storage.policies import ScopeNotFound
from control_kernel.storage.stops import apply_stop_receipt, get_stop, request_stop
from fastapi import Depends, FastAPI, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def _ensure_internal_actor(p: Principal) -> None:
    """Kernel/controller/admin 或 worker；与其他 internal 端口一致。"""
    if not set(p.roles) & {"admin", "worker"}:
        raise ApiError(403, "FORBIDDEN", "需要 admin 或 worker 身份")


def register_stop_routes(app: FastAPI, identity, store) -> None:
    @app.post(
        "/internal/v1/activations/{activation_id}/stop",
        status_code=202,
        response_model=Envelope[StopResource],
    )
    def post_stop(
        activation_id: UUID,
        body: StopRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        _ensure_internal_actor(p)
        try:
            result = request_stop(
                store(request).engine,
                p.sub,
                roles=set(p.roles),
                project_ids=p.project_ids,
                activation_id=activation_id,
                body=body,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权请求停止") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except PlanRejected as exc:
            if exc.message == "STOP_REQUEST_CONFLICT":
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "同一 request_id 已用于不同停止参数"
                ) from exc
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/stops/{stop_id}/receipts",
        status_code=201,
        response_model=Envelope[StopReceiptAccepted],
    )
    def post_stop_receipt(
        stop_id: UUID,
        body: StopReceipt,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        _ensure_internal_actor(p)
        try:
            result = apply_stop_receipt(
                store(request).engine,
                p.sub,
                roles=set(p.roles),
                project_ids=p.project_ids,
                stop_id=stop_id,
                body=body,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权提交停止回执") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/internal/v1/stops/{stop_id}",
        response_model=Envelope[StopResource],
    )
    def read_stop(
        stop_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        _ensure_internal_actor(p)
        try:
            result = get_stop(
                store(request).engine,
                p.sub,
                roles=set(p.roles),
                project_ids=p.project_ids,
                stop_id=stop_id,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权读取停止资源") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
