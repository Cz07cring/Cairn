"""受信 collector 上传工件：必须携带有效租约 fencing。"""

import hashlib
from io import BytesIO
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.artifacts import ArtifactResource
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import LeaseRejected, WorkerForbidden
from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from evidence_ledger.objects import IntegrityError, ObjectTooLarge
from fastapi import Depends, FastAPI, Header, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_collector_routes(app: FastAPI, identity, store) -> None:
    @app.put(
        "/internal/v1/artifacts/{digest}/content",
        status_code=201,
        response_model=Envelope[ArtifactResource],
    )
    async def put_collector_content(
        digest: str,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        project_id: Annotated[UUID, Header(alias="X-Ring-Project-Id")],
        activity_id: Annotated[UUID, Header(alias="X-Ring-Activity-Id")],
        attempt_id: Annotated[UUID, Header(alias="X-Ring-Attempt-Id")],
        fencing_epoch: Annotated[str, Header(alias="X-Ring-Fencing-Epoch")],
        content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
    ):
        if "worker" not in p.roles and "broker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 broker/worker 上传权限")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ApiError(422, "VALIDATION_ERROR", "digest 非法")
        body = await request.body()
        expected = "sha256:" + hashlib.sha256(body).hexdigest()
        if expected != digest:
            raise ApiError(422, "EVIDENCE_INVALID", "内容与 digest 不一致")
        objects = request.app.state.objects
        if objects is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "对象仓未配置")
        mime = content_type or "application/octet-stream"
        try:
            result = Artifacts(store(request).engine, objects).ingest_collector(
                project_id,
                digest,
                BytesIO(body),
                mime=mime,
                producer_identity=f"broker:{p.sub}",
                subject=p.sub,
                activity_id=activity_id,
                attempt_id=attempt_id,
                fencing_epoch=fencing_epoch,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权上传") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except IntegrityError as exc:
            raise ApiError(422, "EVIDENCE_INVALID", "证据完整性校验失败") from exc
        except ObjectTooLarge as exc:
            raise ApiError(422, "VALIDATION_ERROR", "对象过大") from exc
        except RuntimeError as exc:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", str(exc)) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
