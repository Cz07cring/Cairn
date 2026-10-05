"""Project API foundation. No background scheduler and no model/tool execution at startup."""

import base64
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import boto3
import jwt
from botocore.config import Config
from control_kernel.protocols.projects import Envelope, Meta, ProjectCreate, ProjectResource
from control_kernel.storage.policies import InvalidConfiguration, ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict, Projects
from evidence_ledger.content import parse as parse_protocol_json
from evidence_ledger.objects import S3Objects
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from control_api.errors import ApiError
from control_api.identity import Principal
from control_api.routes.activities import register_activity_routes
from control_api.routes.approvals import register_approval_routes
from control_api.routes.artifacts import register_artifact_routes
from control_api.routes.audits import register_audit_routes
from control_api.routes.auth import register_auth_routes
from control_api.routes.candidates import register_candidate_routes
from control_api.routes.claims import register_claim_routes, register_plan_routes
from control_api.routes.collector import register_collector_routes
from control_api.routes.effects import register_effect_routes
from control_api.routes.evidence import register_evidence_routes
from control_api.routes.goals import register_goal_routes
from control_api.routes.memories import register_memory_routes
from control_api.routes.models import register_model_routes
from control_api.routes.orchestration import register_orchestration_routes
from control_api.routes.plan_inputs import register_plan_input_routes
from control_api.routes.policies import register_policy_routes
from control_api.routes.probe import register_probe_routes
from control_api.routes.read_model import register_read_model_routes
from control_api.routes.skills import register_skill_routes
from control_api.routes.stops import register_stop_routes
from control_api.routes.trust import register_trust_routes
from control_api.routes.verification import register_verification_routes
from control_api.routes.verification_obligations import (
    register_verification_obligation_routes,
)
from control_api.routes.verification_runs import register_verification_run_routes
from control_api.session_cookie import COOKIE_NAME, CSRF_HEADER, principal_from_session
from control_api.settings import Settings

# 就绪检查要求的迁移版本（单一事实来源）。
#
# **为什么不是裸字面量**：该值原先直接写死在 `ready()` 里（`!= "0033_trust_invalidation"`）。
# 每当新增迁移、`alembic upgrade head` 推进版本号后，就绪端点便恒返回 503，而
# 破坏性只在无关测试（`/health/ready` 的 `["scope"]`）上以 `KeyError` 暴露 ——
# main 因此确定性地红了一次（见 Issue #12 通报）。裸字面量缺少**漂移护栏**。
#
# **护栏**：`tests/unit/test_expected_db_head.py` 断言本常量 ==
# `alembic ScriptDirectory.get_heads()`。新增迁移却忘了同步此处时，CI 会以
# 明确信息失败（“请同步 EXPECTED_DB_HEAD”），而不是让就绪端点在运行时静默降级。
EXPECTED_DB_HEAD = "0044_plan_input_binding"
def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        if cfg.database_url:
            url = cfg.database_url.get_secret_value()
            if not url.startswith("postgresql+psycopg://"):
                raise RuntimeError("PostgreSQL psycopg URL required")
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
                connect_args={
                    "connect_timeout": 5,
                    # 本机联调/compile 可能超过 10s；可用 RING_DB_STATEMENT_TIMEOUT_MS 覆盖
                    "options": (
                        f"-c statement_timeout={os.environ.get('RING_DB_STATEMENT_TIMEOUT_MS', '60000')}"
                        f" -c lock_timeout={os.environ.get('RING_DB_LOCK_TIMEOUT_MS', '15000')}"
                    ),
                },
            )
        app.state.objects = None
        if cfg.s3_endpoint and cfg.s3_bucket and cfg.s3_access_key and cfg.s3_secret_key:
            client = boto3.client(
                "s3",
                endpoint_url=cfg.s3_endpoint,
                aws_access_key_id=cfg.s3_access_key.get_secret_value(),
                aws_secret_access_key=cfg.s3_secret_key.get_secret_value(),
                region_name=cfg.s3_region,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"max_attempts": 0},
                ),
            )
            app.state.objects = S3Objects(client, cfg.s3_bucket, max_bytes=cfg.artifact_max_bytes)
        app.state.engine = engine
        yield
        if app.state.objects is not None:
            app.state.objects.client.close()
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title="Ringharness Control API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = cfg

    @app.middleware("http")
    async def envelope_errors(request: Request, call_next):
        # 测试/联调可挂 app.state._force_objects，避免 lifespan 与外部注入竞态清空对象仓
        forced = getattr(request.app.state, "_force_objects", None)
        if forced is not None:
            request.app.state.objects = forced
        request.state.request_id = uuid4()
        # Enforce actual bytes even if Content-Length is missing or dishonest.
        size = 0
        chunks = []
        path = request.url.path
        is_collector_put = path.startswith("/internal/v1/artifacts/") and path.endswith(
            "/content"
        )
        max_bytes = 8 * 1024 * 1024 if is_collector_put else 1024 * 1024
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                return error(request, 413, "PAYLOAD_TOO_LARGE", "请求超过大小限制")
            chunks.append(chunk)
        request._body = b"".join(chunks)
        if request._body and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            # collector 上传允许二进制；其余写请求仍强制 JSON。
            if is_collector_put:
                if media_type not in ("application/octet-stream", "application/json"):
                    return error(request, 415, "INVALID_REQUEST", "collector 上传需要 octet-stream")
            elif media_type != "application/json":
                return error(request, 415, "INVALID_REQUEST", "写请求需要 JSON")
            else:
                try:
                    parse_protocol_json(request._body.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    return error(request, 422, "VALIDATION_ERROR", "JSON 内容存在歧义或格式无效")
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        response.headers["Cache-Control"] = "no-store"
        return response

    def error(request: Request, status: int, code: str, message: str):
        return JSONResponse(
            status_code=status,
            content={
                "data": None,
                "error": {
                    "code": code,
                    "message": message,
                    "details": {},
                    "retryable": status == 503,
                },
                "meta": {"request_id": str(request.state.request_id)},
            },
        )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        response = error(request, exc.status, exc.code, exc.message)
        response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo potentially secret input or raw exception contents.
        return error(request, 422, "VALIDATION_ERROR", "请求字段无效")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # 暴露异常类型便于联调（不回显 SQL 文本/参数）
        kind = type(exc).__name__
        return error(
            request,
            503,
            "DEPENDENCY_UNAVAILABLE",
            f"数据服务暂不可用（{kind}）",
        )

    def identity(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    ) -> Principal:
        if authorization and authorization.startswith("Bearer "):
            if not cfg.jwt_public_key:
                raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "身份验证尚未配置")
            try:
                claims = jwt.decode(
                    authorization[7:],
                    cfg.jwt_public_key,
                    algorithms=["RS256"],
                    issuer=cfg.jwt_issuer,
                    audience=cfg.jwt_audience,
                    options={"require": ["exp", "iat", "sub", "iss", "aud"]},
                )
                principal = Principal.model_validate(claims)
                if claims["exp"] - claims["iat"] > 3600:
                    raise ValueError("token lifetime")
                for project in principal.project_ids:
                    UUID(project)
                return principal
            except (jwt.PyJWTError, ValidationError, ValueError, TypeError) as exc:
                raise ApiError(401, "UNAUTHENTICATED", "登录凭据无效或已过期") from exc

        raw = request.cookies.get(COOKIE_NAME)
        if raw and cfg.session_secret and len(cfg.session_secret.get_secret_value()) >= 32:
            try:
                principal, csrf, _expires = principal_from_session(
                    cfg.session_secret.get_secret_value().encode(), raw
                )
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                raise ApiError(401, "UNAUTHENTICATED", "登录凭据无效或已过期") from exc
            for project in principal.project_ids:
                UUID(project)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                if not x_csrf_token or not hmac.compare_digest(x_csrf_token, csrf):
                    raise ApiError(403, "FORBIDDEN", "CSRF 校验失败")
                origin = request.headers.get("origin")
                if (
                    origin
                    and cfg.auth_return_to_origins
                    and origin not in cfg.auth_return_to_origins
                ):
                    raise ApiError(403, "FORBIDDEN", "Origin 不在允许列表")
            return principal

        raise ApiError(401, "UNAUTHENTICATED", "请先登录")

    def store(request: Request) -> Projects:
        if request.app.state.engine is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "数据库尚未配置")
        return Projects(request.app.state.engine)

    def cursor_key() -> bytes:
        if cfg.cursor_secret is None or len(cfg.cursor_secret.get_secret_value()) < 32:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "分页签名尚未配置")
        return cfg.cursor_secret.get_secret_value().encode()

    @app.get("/health/live", response_model=dict[str, bool])
    def live():
        return {"alive": True}

    @app.get("/health/ready", response_model=dict[str, str])
    def ready(request: Request, p: Annotated[Principal, Depends(identity)]):
        if "ops" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要运维权限")
        if not cfg.repository_refs:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "仓库引用尚未配置")
        cursor_key()
        with store(request).engine.connect() as db:
            if (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                != EXPECTED_DB_HEAD
            ):
                raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "数据库版本不匹配")
        return {"status": "ready", "scope": "project-config-api-only"}

    @app.post("/api/v1/projects", status_code=201, response_model=Envelope[ProjectResource])
    def create_project(
        body: ProjectCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        if body.repository_ref not in cfg.repository_refs:
            raise ApiError(422, "VALIDATION_ERROR", "仓库引用未登记")
        try:
            result = store(request).create(p.sub, idempotency_key, body)
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/projects", response_model=Envelope[list[ProjectResource]])
    def list_projects(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有项目读取权限")
        key = cursor_key()
        after = None
        scope = json.dumps([p.sub, sorted(p.project_ids)], separators=(",", ":"))
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(key, (scope + id_value).encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor signature")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        rows = store(request).list(p.sub, [UUID(i) for i in p.project_ids], limit + 1, after)
        next_cursor = None
        if len(rows) > limit:
            value = str(rows[limit - 1].id)
            mac = hmac.new(key, (scope + value).encode(), hashlib.sha256).hexdigest()
            next_cursor = base64.urlsafe_b64encode((value + "." + mac).encode()).decode()
        return Envelope(
            data=rows[:limit],
            meta=Meta(request_id=request.state.request_id, next_cursor=next_cursor),
        )

    @app.exception_handler(ScopeNotFound)
    async def scope_error(request: Request, exc: ScopeNotFound):
        return error(request, 404, "NOT_FOUND", "资源不存在")

    @app.exception_handler(InvalidConfiguration)
    async def config_error(request: Request, exc: InvalidConfiguration):
        return error(request, 422, "VERIFICATION_PROFILE_MISMATCH", "验证配置与批准定义不匹配")

    @app.exception_handler(TrustBlocked)
    async def trust_error(request: Request, exc: TrustBlocked):
        return error(request, 409, "INVALID_STATE", "项目信任核对中，暂不接受新配置")

    register_artifact_routes(app, identity, store)
    register_collector_routes(app, identity, store)
    register_policy_routes(app, identity, store, cursor_key)
    register_model_routes(app, identity, store, cursor_key)
    register_verification_routes(app, identity, store, cursor_key)
    register_verification_run_routes(app, identity, store)
    register_verification_obligation_routes(app, identity, store, cursor_key)
    register_skill_routes(app, identity, store, cursor_key)
    register_goal_routes(app, identity, store, cursor_key)
    register_activity_routes(app, identity, store, cursor_key)
    register_approval_routes(app, identity, store, cursor_key)
    register_claim_routes(
        app,
        identity,
        store,
        lease_ttl_seconds=cfg.lease_ttl_seconds,
        repository_paths=cfg.repository_paths,
    )
    register_orchestration_routes(
        app,
        identity,
        store,
        lease_ttl_seconds=cfg.lease_ttl_seconds,
    )
    register_plan_routes(app, identity, store, cursor_key)
    register_probe_routes(app, identity, store, cfg)
    register_read_model_routes(app, identity, store)
    register_effect_routes(app, identity, store, cursor_key)
    register_evidence_routes(app, identity, store, cursor_key)
    register_memory_routes(app, identity, store, cursor_key)
    register_stop_routes(app, identity, store)
    register_trust_routes(app, identity, store)
    register_candidate_routes(app, identity, store)
    register_audit_routes(app, identity, store, cursor_key)
    register_plan_input_routes(app, identity, store)
    register_auth_routes(app, identity, cfg)

    return app
