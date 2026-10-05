"""VerificationRun 内部登记：POST 宿主写入 + GET 按项目 scope 读取。"""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import LeaseRejected, PlanRejected, WorkerForbidden
from control_kernel.protocols.verification import (
    VerificationRunCreateRequest,
    VerificationRunResource,
)
from control_kernel.storage.policies import ScopeNotFound
from control_kernel.storage.verification_runs import (
    VerificationRunConflict,
    create_run,
    get_run,
)
from fastapi import Depends, FastAPI, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_verification_run_routes(app: FastAPI, identity, store) -> None:
    @app.post(
        "/internal/v1/verification-runs",
        status_code=201,
        response_model=Envelope[VerificationRunResource],
    )
    def post_verification_run(
        body: VerificationRunCreateRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker 权限")
        try:
            result = create_run(store(request).engine, p.sub, body)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权写入 VerificationRun") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except VerificationRunConflict as exc:
            raise ApiError(
                409, "VERIFICATION_RUN_CONFLICT", "同键 VerificationRun 内容冲突"
            ) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/internal/v1/verification-runs/{run_id}",
        response_model=Envelope[VerificationRunResource],
    )
    def get_verification_run(
        run_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin", "worker"}:
            raise ApiError(403, "FORBIDDEN", "没有 VerificationRun 读取权限")
        try:
            result = get_run(store(request).engine, run_id, p.sub, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
