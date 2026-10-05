"""候选封存 HTTP；快照必须来自已入库工件。"""

from typing import Annotated
from uuid import UUID

from control_kernel.protocols.candidates import (
    CandidateManifestResource,
    CandidateSealRequest,
)
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import LeaseRejected, PlanRejected, WorkerForbidden
from control_kernel.storage.candidates import get_candidate, seal_candidate
from control_kernel.storage.policies import ScopeNotFound
from fastapi import Depends, FastAPI, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_candidate_routes(app: FastAPI, identity, store) -> None:
    @app.post(
        "/internal/v1/candidates/seal",
        status_code=201,
        response_model=Envelope[CandidateManifestResource],
    )
    def post_seal(
        body: CandidateSealRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        objects = request.app.state.objects
        if objects is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "对象仓未配置，无法读取快照")

        def read_bytes(project_id: UUID, digest: str) -> bytes:
            return objects.read(project_id, digest)

        try:
            result = seal_candidate(
                store(request).engine, p.sub, body, read_artifact_bytes=read_bytes
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权封存") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            code = "VALIDATION_ERROR"
            if exc.message.startswith("GOAL_ENGINEERING_CLOSED"):
                code = "GOAL_ENGINEERING_CLOSED"
            elif exc.message.startswith("STOP_UNCONFIRMED"):
                code = "STOP_UNCONFIRMED"
            elif exc.message.startswith("GOAL_REVIEW_BLOCKER"):
                code = "GOAL_REVIEW_BLOCKER"
            elif exc.message.startswith("NO_PROGRESS_"):
                code = exc.message.split(":", 1)[0]
            raise ApiError(422, code, exc.message) from exc
        except FileNotFoundError as exc:
            raise ApiError(404, "NOT_FOUND", "快照对象不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/candidates/{candidate_id}",
        response_model=Envelope[CandidateManifestResource],
    )
    def read_candidate(
        candidate_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not {"viewer", "operator", "admin", "worker"} & set(p.roles):
            raise ApiError(403, "FORBIDDEN", "需要查看权限")
        try:
            result = get_candidate(store(request).engine, candidate_id, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
