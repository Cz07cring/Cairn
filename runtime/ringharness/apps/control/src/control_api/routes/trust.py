"""内部信任失效路由：POST /internal/v1/trust/invalidations、GET /trust/jobs。"""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import PlanRejected
from control_kernel.protocols.trust import (
    TrustInvalidationCreate,
    TrustPropagationJobResource,
)
from control_kernel.storage.policies import ScopeNotFound
from control_kernel.storage.trust import (
    TrustRevisionConflict,
    advance_trust_propagation,
    get_trust_propagation_job,
    submit_evidence_invalidation,
)
from fastapi import Depends, FastAPI, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_trust_routes(app: FastAPI, identity, store) -> None:
    @app.post(
        "/internal/v1/trust/invalidations",
        status_code=202,
        response_model=Envelope[TrustPropagationJobResource],
    )
    def post_invalidation(
        body: TrustInvalidationCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        # doc/05：Ledger 运维主体；仓内用 ops 角色，不接受 body authority
        if "ops" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 Ledger 运维权限")
        try:
            job = submit_evidence_invalidation(
                store(request).engine,
                p.sub,
                p.project_ids,
                body,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustRevisionConflict as exc:
            raise ApiError(
                409, "TRUST_REVISION_CONFLICT", "trust_revision 已变更，请重读后再提交"
            ) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", str(exc)) from exc
        return Envelope(data=job, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/internal/v1/trust/jobs/{job_id}",
        response_model=Envelope[TrustPropagationJobResource],
    )
    def get_job(
        job_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if "ops" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要运维权限")
        try:
            job = get_trust_propagation_job(
                store(request).engine, job_id, p.sub, p.project_ids
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=job, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/trust/jobs/{job_id}/advance",
        response_model=Envelope[TrustPropagationJobResource],
    )
    def advance_job(
        job_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """可信维护程序推进传播并在完成后 CAS 解锁；非通用 unlock。"""
        if "ops" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 Ledger 运维权限")
        try:
            job = advance_trust_propagation(
                store(request).engine, job_id, p.sub, p.project_ids
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", str(exc)) from exc
        return Envelope(data=job, meta=Meta(request_id=request.state.request_id))
