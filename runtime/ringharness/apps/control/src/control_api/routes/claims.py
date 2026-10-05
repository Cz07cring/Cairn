"""内部 claim / heartbeat / PLAN outcome；不开放公开 worker 自注册。"""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.plans import (
    ActivityOutcomeRequest,
    PlanCreate,
    PlanResource,
    TaskResource,
    TaskStatus,
)
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import (
    ActivationTerminationCreate,
    ActivationTerminationResource,
    ActivityLease,
    ActivityResource,
    BindingStale,
    CheckpointProposal,
    CheckpointResource,
    ClaimRequest,
    CommandOperation,
    ControlRequest,
    GoalBanCallKeyAddRequest,
    GoalBanCallKeysResource,
    GoalNudgeBudgetConsumeRequest,
    GoalNudgeBudgetResource,
    HeartbeatRequest,
    HeartbeatResponse,
    InvalidGoalState,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from control_kernel.storage.activation_terminations import (
    record_activation_termination,
)
from control_kernel.storage.ban_call_keys import (
    add_goal_ban_call_key,
    read_goal_ban_call_keys,
)
from control_kernel.storage.checkpoints import accept_checkpoint
from control_kernel.storage.claims import claim_activity, renew_lease
from control_kernel.storage.effects import RoleToolForbidden
from control_kernel.storage.nudge_budgets import (
    read_goal_nudge_budget,
    try_consume_goal_nudge_budget,
)
from control_kernel.storage.plans import Plans, Tasks
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from control_kernel.storage.probe import submit_activity_outcome
from control_kernel.storage.projects import ProjectConflict
from control_kernel.storage.task_commands import (
    InvalidTaskState,
    cancel_task,
    retry_task,
)
from execution_broker.worktree import (
    WorkspaceUnavailable,
    ensure_attempt_workspace,
    seed_workspace_at_commit,
)
from fastapi import Depends, FastAPI, Header, Query, Request
from sqlalchemy import text

from control_api.errors import ApiError
from control_api.identity import Principal


def _attach_workspace(
    result: ActivityLease,
    engine,
    *,
    repository_paths: dict[str, str],
) -> ActivityLease:
    if (
        result.lease is None
        or result.activity is None
        or result.attempt is None
        or result.activity.kind not in ("EXECUTE", "INTEGRATE")
    ):
        return result
    try:
        path = ensure_attempt_workspace(
            result.activity.project_id, result.attempt.id, required=False
        )
    except WorkspaceUnavailable as exc:
        raise ApiError(503, "WORKSPACE_UNAVAILABLE", exc.message) from exc
    if path is None:
        return result

    # 有映射仓库时按 Goal.base_commit 检出；无映射则保留空工作区（开发过渡）。
    if result.activity.goal_id is not None and repository_paths:
        with engine.connect() as db:
            row = (
                db.execute(
                    text(
                        """SELECT p.repository_ref, g.contract->>'base_commit' AS base_commit
                        FROM goals g JOIN projects p ON p.id=g.project_id
                        WHERE g.id=:goal"""
                    ),
                    {"goal": result.activity.goal_id},
                )
                .mappings()
                .first()
            )
        if row is not None and row["base_commit"]:
            repo = repository_paths.get(row["repository_ref"])
            if repo:
                try:
                    seed_workspace_at_commit(
                        path, Path(repo).expanduser().resolve(), row["base_commit"]
                    )
                except WorkspaceUnavailable as exc:
                    raise ApiError(503, "WORKSPACE_UNAVAILABLE", exc.message) from exc

    return result.model_copy(update={"workspace_root": str(path)})


def register_claim_routes(
    app: FastAPI,
    identity,
    store,
    *,
    lease_ttl_seconds: int = 90,
    repository_paths: dict[str, str] | None = None,
) -> None:
    ttl = timedelta(seconds=lease_ttl_seconds)
    paths = repository_paths or {}

    @app.post("/internal/v1/claims", response_model=Envelope[ActivityLease])
    def claim(
        body: ClaimRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        try:
            result = claim_activity(
                store(request).engine, p.sub, idempotency_key, body, lease_ttl=ttl
            )
            result = _attach_workspace(
                result, store(request).engine, repository_paths=paths
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或 kinds 越权") from exc
        except BindingStale as exc:
            raise ApiError(409, "BINDING_STALE", "活动绑定已过期，请刷新后重试") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except LeaseRejected as exc:
            raise ApiError(409, exc.code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/activities/{activity_id}/heartbeat",
        response_model=Envelope[HeartbeatResponse],
    )
    def heartbeat(
        activity_id: UUID,
        body: HeartbeatRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = renew_lease(store(request).engine, p.sub, activity_id, body, lease_ttl=ttl)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权续约") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/activities/{activity_id}/checkpoints",
        response_model=Envelope[CheckpointResource],
        status_code=201,
    )
    def post_checkpoint(
        activity_id: UUID,
        body: CheckpointProposal,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = accept_checkpoint(store(request).engine, p.sub, activity_id, body)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权写入 checkpoint") from exc
        except RoleToolForbidden as exc:
            raise ApiError(403, "ROLE_TOOL_FORBIDDEN", "当前角色禁止该 checkpoint 操作") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/activities/{activity_id}/outcomes",
        response_model=Envelope[ActivityResource],
    )
    def outcomes(
        activity_id: UUID,
        body: ActivityOutcomeRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = submit_activity_outcome(store(request).engine, p.sub, activity_id, body)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            code = "VALIDATION_ERROR"
            if exc.message.startswith("GOAL_OUTCOME_CLOSED"):
                code = "GOAL_OUTCOME_CLOSED"
            raise ApiError(422, code, exc.message) from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "状态版本冲突") from exc
        except BindingStale as exc:
            raise ApiError(409, "BINDING_STALE", "活动绑定已过期") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/activities/{activity_id}/activation-terminations",
        status_code=201,
        response_model=Envelope[ActivationTerminationResource],
    )
    def post_activation_termination(
        activity_id: UUID,
        body: ActivationTerminationCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """登记 activation 确定性终止；须 attempt 持有者；≠ Goal DONE。"""
        if "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker 身份")
        try:
            row = record_activation_termination(
                store(request).engine,
                subject=p.sub,
                project_ids=p.project_ids,
                activity_id=activity_id,
                attempt_id=body.lease.attempt_id,
                reason=body.reason,
                detail=body.detail,
                summary_artifact_id=body.summary_artifact_id,
                closeout_artifact_id=body.closeout_artifact_id,
                lease=body.lease,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "未登记 worker 或非持有者") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        data = ActivationTerminationResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "activation 终止不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/internal/v1/goals/{goal_id}/nudge-budget",
        response_model=Envelope[GoalNudgeBudgetResource],
    )
    def get_goal_nudge_budget(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        max_budget: Annotated[int, Query(ge=1, le=1000)] = 1,
    ):
        """只读 Goal Nudge 预算；无行视为 consumed=0；≠ DONE。"""
        if "worker" not in p.roles and "operator" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker 或操作员身份")
        try:
            row = read_goal_nudge_budget(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                max_budget=max_budget,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "无权读取 Nudge 预算") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        data = GoalNudgeBudgetResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "Nudge 预算不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/goals/{goal_id}/nudge-budget/consume",
        response_model=Envelope[GoalNudgeBudgetResource],
    )
    def post_goal_nudge_budget_consume(
        goal_id: UUID,
        body: GoalNudgeBudgetConsumeRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """原子消耗 1 次 Nudge；耗尽时 accepted=false；≠ DONE。"""
        if "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker 身份消耗 Nudge 预算")
        try:
            row = try_consume_goal_nudge_budget(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                max_budget=body.max_budget,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "无权消耗 Nudge 预算") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        data = GoalNudgeBudgetResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "Nudge 预算不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/internal/v1/goals/{goal_id}/ban-call-keys",
        response_model=Envelope[GoalBanCallKeysResource],
    )
    def get_goal_ban_call_keys(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """只读 Goal 软禁同参键；≠ DONE。"""
        if "worker" not in p.roles and "operator" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker 或操作员身份")
        try:
            row = read_goal_ban_call_keys(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "无权读取软禁键") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        data = GoalBanCallKeysResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "软禁键不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/goals/{goal_id}/ban-call-keys",
        response_model=Envelope[GoalBanCallKeysResource],
        status_code=201,
    )
    def post_goal_ban_call_key(
        goal_id: UUID,
        body: GoalBanCallKeyAddRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """幂等登记软禁同参键；≠ DONE。"""
        if "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要 worker 身份登记软禁键")
        try:
            row = add_goal_ban_call_key(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                call_key=body.call_key,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "无权登记软禁键") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        data = GoalBanCallKeysResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "软禁键不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))


def register_plan_routes(app: FastAPI, identity, store, cursor_key) -> None:
    @app.post(
        "/api/v1/goals/{goal_id}/plans",
        status_code=201,
        response_model=Envelope[PlanResource],
    )
    def post_plan_candidate(
        goal_id: UUID,
        body: PlanCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = Plans(store(request).engine).submit_candidate(
                goal_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受候选计划") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法提交候选计划") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/goals/{goal_id}/plans", response_model=Envelope[list[PlanResource]])
    def list_plans(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有计划读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/goals/{goal_id}/plans",
                str(goal_id),
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
                expected = hmac.new(
                    secret, (scope + id_value).encode(), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = Plans(store(request).engine).list_for_goal(
                goal_id, p.sub, p.project_ids, limit + 1, after
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

    @app.get("/api/v1/goals/{goal_id}/tasks", response_model=Envelope[list[TaskResource]])
    def list_tasks(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        status: Annotated[TaskStatus | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有任务读取权限")
        secret = cursor_key()
        scope = json.dumps(
            [
                "/api/v1/goals/{goal_id}/tasks",
                str(goal_id),
                p.sub,
                sorted(p.project_ids),
                status,
            ],
            separators=(",", ":"),
        )
        after = None
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor).decode()
                id_value, mac = raw.split(".", 1)
                expected = hmac.new(
                    secret, (scope + id_value).encode(), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    raise ValueError("cursor")
                after = UUID(id_value)
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "INVALID_REQUEST", "分页标识无效") from exc
        try:
            rows = Tasks(store(request).engine).list_for_goal(
                goal_id, p.sub, p.project_ids, limit + 1, status, after
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

    @app.get("/api/v1/tasks/{task_id}", response_model=Envelope[TaskResource])
    def get_task(
        task_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        human = set(p.roles) & {"viewer", "operator", "approver", "admin"}
        if not human and "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有任务读取权限")
        try:
            if human:
                result = Tasks(store(request).engine).get(
                    task_id, p.sub, p.project_ids
                )
            else:
                from control_kernel.protocols.runtime import WorkerForbidden

                try:
                    result = Tasks(store(request).engine).get_for_worker(
                        task_id, p.sub
                    )
                except WorkerForbidden as exc:
                    raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/tasks/{task_id}/cancel",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def cancel_task_route(
        task_id: UUID,
        body: ControlRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = cancel_task(
                store(request).engine, task_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受控制命令") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法取消任务") from exc
        except InvalidTaskState as exc:
            raise ApiError(409, "INVALID_STATE", f"任务状态为 {exc.status}，无法取消") from exc
        except StateRevisionConflict as exc:
            raise ApiError(
                409, "STATE_REVISION_CONFLICT", "任务状态版本不匹配，请刷新后重试"
            ) from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/tasks/{task_id}/retry",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def retry_task_route(
        task_id: UUID,
        body: ControlRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = retry_task(
                store(request).engine, task_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受控制命令") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法重试任务") from exc
        except InvalidTaskState as exc:
            raise ApiError(409, "INVALID_STATE", f"任务状态为 {exc.status}，无法重试") from exc
        except StateRevisionConflict as exc:
            raise ApiError(
                409, "STATE_REVISION_CONFLICT", "任务状态版本不匹配，请刷新后重试"
            ) from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
