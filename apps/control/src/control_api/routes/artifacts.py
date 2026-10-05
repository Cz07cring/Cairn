"""Project-authorized metadata and downloads. No client-controlled object-store keys."""

import re
from typing import Annotated
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from control_kernel.protocols.artifacts import ArtifactResource
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import WorkerForbidden
from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.policies import ScopeNotFound
from evidence_ledger.objects import IntegrityError, ObjectTooLarge
from fastapi import Depends, FastAPI, Header, Request, Response

from control_api.errors import ApiError
from control_api.identity import Principal


def register_artifact_routes(app: FastAPI, identity, store) -> None:
    def catalog(request: Request, principal: Principal) -> Artifacts:
        if not set(principal.roles) & {"viewer", "operator", "approver", "admin", "worker"}:
            raise ApiError(403, "FORBIDDEN", "没有工件读取权限")
        return Artifacts(store(request).engine, request.app.state.objects)

    def load_artifact(
        artifacts: Artifacts, artifact_id: UUID, principal: Principal
    ) -> ArtifactResource:
        # 人机角色走项目范围；worker JWT 常无 project_ids，须已登记 ACTIVE。
        human = set(principal.roles) & {"viewer", "operator", "approver", "admin"}
        if human:
            return artifacts.get(artifact_id, principal.sub, principal.project_ids)
        return artifacts.get_for_worker(artifact_id, principal.sub)

    @app.get("/api/v1/artifacts/{artifact_id}", response_model=Envelope[ArtifactResource])
    def metadata(artifact_id: UUID, request: Request, p: Annotated[Principal, Depends(identity)]):
        try:
            record = load_artifact(catalog(request, p), artifact_id, p)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权读取工件") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=record, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/artifacts/{artifact_id}/content",
        response_class=Response,
        responses={
            200: {"content": {"application/octet-stream": {}}},
            206: {"description": "Single byte range", "content": {"application/octet-stream": {}}},
            416: {"description": "Unsatisfiable byte range"},
        },
    )
    def content(
        artifact_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        range_header: Annotated[str | None, Header(alias="Range", max_length=200)] = None,
        if_range: Annotated[str | None, Header(alias="If-Range", max_length=200)] = None,
    ):
        artifacts = catalog(request, p)
        try:
            record = load_artifact(artifacts, artifact_id, p)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权读取工件") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        if artifacts.objects is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "证据存储尚未配置")
        try:
            data = artifacts.content(record)
        except (IntegrityError, ObjectTooLarge) as exc:
            raise ApiError(422, "EVIDENCE_INVALID", "证据完整性校验失败") from exc
        except (FileNotFoundError, BotoCoreError, ClientError) as exc:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "证据内容暂不可用") from exc
        headers = {
            "ETag": '"' + record.digest + '"',
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{record.id}"',
        }
        status = 200
        headers["Accept-Ranges"] = "bytes"
        # Verify the WHOLE object first, then slice. Partial bytes alone cannot prove its digest.
        if range_header and (if_range is None or if_range == headers["ETag"]):
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_header)
            if not match or not any(match.groups()) or not data:
                raise ApiError(
                    416,
                    "INVALID_REQUEST",
                    "请求的内容范围无效",
                    headers={"Content-Range": f"bytes */{len(data)}"},
                )
            first, last = match.groups()
            start = int(first) if first else max(0, len(data) - int(last))
            end = min(int(last), len(data) - 1) if first and last else len(data) - 1
            if start > end:
                raise ApiError(
                    416,
                    "INVALID_REQUEST",
                    "请求的内容范围无效",
                    headers={"Content-Range": f"bytes */{len(data)}"},
                )
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            data = data[start : end + 1]
            status = 206
        return Response(
            data, status_code=status, media_type="application/octet-stream", headers=headers
        )
