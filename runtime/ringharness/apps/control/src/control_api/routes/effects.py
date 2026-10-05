"""步骤与 Effect prepare 内部端口；PLAN → ROLE_TOOL_FORBIDDEN。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.effects import (
    BrokerDispatchableEffect,
    EffectDispatchRequest,
    EffectPrepareRequest,
    EffectResource,
    EffectStatus,
    ReceiptAccepted,
    ReconciliationRequest,
    StepCreate,
    StepResource,
    TrustedReceipt,
)
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import (
    CommandOperation,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from control_kernel.storage.effects import (
    RoleToolForbidden,
    apply_effect_receipt,
    create_step,
    dispatch_effect,
    get_effect,
    list_dispatchable_effects_for_worker,
    list_effects,
    list_steps_for_activity,
    prepare_effect,
)
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict
from control_kernel.storage.reconcile import reconcile_effect
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_effect_routes(app: FastAPI, identity, store, cursor_key=None) -> None:
    @app.get(
        "/internal/v1/broker/effects",
        response_model=Envelope[list[BrokerDispatchableEffect]],
    )
    def list_broker_dispatchable_effects(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 32,
    ):
        """Broker 拉取本 worker 租约下已准备的 effect；不认领活动、不标 Goal DONE。"""
        if "worker" not in p.roles and "broker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 broker/worker 拉取权限")
        try:
            rows = list_dispatchable_effects_for_worker(
                store(request).engine, p.sub, limit=limit
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权拉取 effect 队列") from exc
        return Envelope(data=rows, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/effects", response_model=Envelope[list[EffectResource]])
    def list_project_effects(
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        project_id: Annotated[UUID, Query()],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        goal_id: Annotated[UUID | None, Query()] = None,
        activity_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[EffectStatus | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有 effect 读取权限")
        if cursor_key is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "分页密钥未配置")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/effects",
                str(project_id),
                str(goal_id) if goal_id else None,
                str(activity_id) if activity_id else None,
                status,
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
            rows = list_effects(
                store(request).engine,
                p.sub,
                p.project_ids,
                project_id=project_id,
                goal_id=goal_id,
                activity_id=activity_id,
                status=status,
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
        "/internal/v1/activities/{activity_id}/steps",
        status_code=201,
        response_model=Envelope[StepResource],
    )
    def post_step(
        activity_id: UUID,
        body: StepCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = create_step(store(request).engine, p.sub, activity_id, body)
        except RoleToolForbidden as exc:
            raise ApiError(403, "ROLE_TOOL_FORBIDDEN", "PLAN 禁止登记工具步骤") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权登记步骤") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            code = "STEP_CONFLICT" if exc.message == "STEP_CONFLICT" else "VALIDATION_ERROR"
            if ": " in exc.message and (
                exc.message.startswith("TOOL_")
                or exc.message.startswith("STOP_")
                or exc.message.startswith("GOAL_ENGINEERING_CLOSED")
                or exc.message.startswith("GOAL_REVIEW_BLOCKER")
                or exc.message.startswith("NO_PROGRESS_")
                or exc.message.startswith("SEAL_")
            ):
                code = exc.message.split(":", 1)[0]
            status = 409 if code == "STEP_CONFLICT" else 422
            raise ApiError(status, code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/effects/prepare",
        status_code=201,
        response_model=Envelope[EffectResource],
    )
    def post_prepare(
        body: EffectPrepareRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        objects = request.app.state.objects
        if objects is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "对象仓未配置，无法核验工具输入")

        def read_bytes(project_id: UUID, digest: str) -> bytes:
            return objects.read(project_id, digest)

        try:
            result = prepare_effect(
                store(request).engine,
                p.sub,
                body,
                read_artifact_bytes=read_bytes,
            )
        except RoleToolForbidden as exc:
            raise ApiError(403, "ROLE_TOOL_FORBIDDEN", "PLAN 禁止准备工程 effect") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权准备 effect") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            code = "EFFECT_CONFLICT" if exc.message == "EFFECT_CONFLICT" else "VALIDATION_ERROR"
            # ToolCapability / Stop / Goal 控制态拒绝码透出
            if ": " in exc.message and (
                exc.message.startswith("TOOL_")
                or exc.message.startswith("STOP_")
                or exc.message.startswith("GOAL_ENGINEERING_CLOSED")
                or exc.message.startswith("GOAL_REVIEW_BLOCKER")
                or exc.message.startswith("NO_PROGRESS_")
                or exc.message.startswith("SEAL_")
            ):
                code = exc.message.split(":", 1)[0]
            status = 409 if code == "EFFECT_CONFLICT" else 422
            raise ApiError(status, code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/effects/{effect_id}/dispatch",
        response_model=Envelope[EffectResource],
    )
    def post_dispatch(
        effect_id: UUID,
        body: EffectDispatchRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = dispatch_effect(store(request).engine, p.sub, effect_id, body)
        except RoleToolForbidden as exc:
            raise ApiError(403, "ROLE_TOOL_FORBIDDEN", "PLAN 禁止 dispatch effect") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权 dispatch") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "effect 状态版本不匹配") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            code = "VALIDATION_ERROR"
            if ": " in exc.message and (
                exc.message.startswith("TOOL_") or exc.message.startswith("STOP_")
            ):
                code = exc.message.split(":", 1)[0]
            raise ApiError(422, code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/effects/{effect_id}/receipts",
        status_code=201,
        response_model=Envelope[ReceiptAccepted],
    )
    def post_receipt(
        effect_id: UUID,
        body: TrustedReceipt,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            accepted, _effect = apply_effect_receipt(
                store(request).engine, p.sub, effect_id, body
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权写回执") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            code = "RECEIPT_CONFLICT" if exc.message == "RECEIPT_CONFLICT" else "VALIDATION_ERROR"
            status = 409 if code == "RECEIPT_CONFLICT" else 422
            raise ApiError(status, code, exc.message) from exc
        return Envelope(data=accepted, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/effects/{effect_id}", response_model=Envelope[EffectResource])
    def read_effect(
        effect_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not {"viewer", "operator", "admin", "worker"} & set(p.roles):
            raise ApiError(403, "FORBIDDEN", "需要查看权限")
        try:
            result = get_effect(store(request).engine, effect_id, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/effects/{effect_id}/reconcile",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def post_reconcile(
        effect_id: UUID,
        body: ReconciliationRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if not set(p.roles) & {"approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "需要对账审批权限")
        try:
            result = reconcile_effect(
                store(request).engine, effect_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受对账命令") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "effect 状态版本不匹配") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/activities/{activity_id}/steps",
        response_model=Envelope[list[StepResource]],
    )
    def list_activity_steps(
        activity_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有步骤读取权限")
        if cursor_key is None:
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "分页密钥未配置")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/activities/{activity_id}/steps",
                str(activity_id),
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
            rows = list_steps_for_activity(
                store(request).engine,
                activity_id,
                p.sub,
                p.project_ids,
                limit + 1,
                after,
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
