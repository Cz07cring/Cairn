"""Skill 与 SkillSet HTTP 路由；不开放公开创建验证器或伪造验收记录。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import CommandOperation, PlanRejected
from control_kernel.protocols.skills import (
    SkillActivate,
    SkillCreate,
    SkillRevoke,
    SkillSetCreate,
    SkillSetResource,
    SkillValidationRecordResource,
    SkillVersionResource,
)
from control_kernel.storage.policies import InvalidConfiguration, ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict
from control_kernel.storage.skill_validate import start_validate
from control_kernel.storage.skills import Skills, SkillSets
from evidence_ledger.content import encode
from fastapi import Depends, FastAPI, Header, Query, Request
from sqlalchemy import text

from control_api.errors import ApiError
from control_api.identity import Principal

from .configurations import ConfigurationHTTP


def register_skill_routes(app: FastAPI, identity, store, cursor_key) -> None:
    set_http = ConfigurationHTTP(store, cursor_key, SkillSets, "SkillSet", "/api/v1/skill-sets")

    @app.post("/api/v1/skills", status_code=201, response_model=Envelope[SkillVersionResource])
    def create_skill(
        body: SkillCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        engine = store(request).engine
        with engine.connect() as db:
            artifact_digest = db.execute(
                text("SELECT digest FROM artifacts WHERE project_id=:project AND id=:id"),
                {"project": body.project_id, "id": body.content_artifact_id},
            ).scalar_one_or_none()
            profile = (
                db.execute(
                    text(
                        """SELECT content_digest,config FROM verification_profiles
                        WHERE project_id=:project AND id=:id"""
                    ),
                    {"project": body.project_id, "id": body.verification_profile_id},
                )
                .mappings()
                .first()
            )
        if artifact_digest is None or profile is None:
            raise ApiError(422, "VALIDATION_ERROR", "技能引用的工件或验证配置不存在")
        if profile["config"].get("target_scope") != "SKILL":
            raise ApiError(422, "VALIDATION_ERROR", "技能必须绑定 SKILL 验证配置")
        try:
            canonical = encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "SkillVersion",
                        "content": body.model_dump(mode="json"),
                        "reference_bindings": [
                            {
                                "object_type": "Artifact",
                                "object_id": str(body.content_artifact_id),
                                "content_digest": artifact_digest,
                            },
                            {
                                "object_type": "VerificationProfile",
                                "object_id": str(body.verification_profile_id),
                                "content_digest": profile["content_digest"],
                            },
                        ],
                    }
                )
            )
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "配置内容无效") from exc
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        try:
            result = Skills(engine).create(
                p.sub,
                p.project_ids,
                idempotency_key,
                body,
                digest,
                json.loads(canonical)["content"],
            )
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except InvalidConfiguration as exc:
            msg = str(exc)
            if msg.startswith("TOOL_"):
                code = msg.split(":", 1)[0]
                raise ApiError(422, code, msg) from exc
            raise ApiError(422, "VALIDATION_ERROR", "技能配置与批准依赖不匹配") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/skills", response_model=Envelope[list[SkillVersionResource]])
    def list_skills(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        status: Annotated[str | None, Query(max_length=32)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有读取权限")
        if status is not None and status not in {
            "CANDIDATE",
            "VALIDATING",
            "ACTIVE",
            "REVOKED",
            "REJECTED",
        }:
            raise ApiError(422, "VALIDATION_ERROR", "技能状态无效")
        secret = cursor_key()
        scope = json.dumps(
            ["/api/v1/skills", str(project_id), p.sub, sorted(p.project_ids), status],
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
        rows = Skills(store(request).engine).list_versions(
            project_id, p.sub, p.project_ids, limit + 1, after, status
        )
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
        "/api/v1/skills/{version_id}/validate",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def validate_skill(
        version_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        try:
            result = start_validate(
                store(request).engine, version_id, p.sub, p.project_ids, idempotency_key
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受验证命令") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/skills/{version_id}/activate",
        response_model=Envelope[SkillVersionResource],
    )
    def activate_skill(
        version_id: UUID,
        body: SkillActivate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        try:
            result = Skills(store(request).engine).activate(
                version_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except InvalidConfiguration as exc:
            raise ApiError(422, "VALIDATION_ERROR", "激活条件不满足") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/skills/{version_id}/revoke",
        response_model=Envelope[SkillVersionResource],
    )
    def revoke_skill(
        version_id: UUID,
        body: SkillRevoke,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        try:
            result = Skills(store(request).engine).revoke(
                version_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/skills/{version_id}/validations",
        response_model=Envelope[list[SkillValidationRecordResource]],
    )
    def list_validations(
        version_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有读取权限")
        if cursor is not None:
            raise ApiError(400, "INVALID_REQUEST", "验收记录分页尚未开放")
        rows = Skills(store(request).engine).list_validations(
            version_id, p.sub, p.project_ids, limit, None
        )
        return Envelope(data=rows, meta=Meta(request_id=request.state.request_id))

    @app.post("/api/v1/skill-sets", status_code=201, response_model=Envelope[SkillSetResource])
    def create_skill_set(
        body: SkillSetCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        try:
            return set_http.create(body, request, p, idempotency_key)
        except InvalidConfiguration as exc:
            raise ApiError(422, "VALIDATION_ERROR", "技能集只能绑定 ACTIVE 版本") from exc

    @app.get("/api/v1/skill-sets", response_model=Envelope[list[SkillSetResource]])
    def list_skill_sets(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        return set_http.list(project_id, request, p, limit, cursor)
