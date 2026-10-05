"""Goal HTTP：operator 创建 DRAFT；start 同事务建 PLAN Activity（无工具）。"""

import base64
import hashlib
import hmac
import json
from typing import Annotated
from uuid import UUID

from control_kernel.protocols.audits import GoalReviewBudgetSnapshot
from control_kernel.protocols.finalization import (
    EvidenceExportRequest,
    FinalizationRecoveryRequest,
    ReleaseView,
)
from control_kernel.protocols.goals import (
    GoalContractUpdate,
    GoalCreate,
    GoalResource,
    GoalStatus,
    GoalWallBudgetSnapshot,
    OrchestrationAbandonmentCreate,
    OrchestrationAbandonmentResource,
)
from control_kernel.protocols.projects import Envelope, Meta
from control_kernel.protocols.runtime import (
    ActivationTerminationResource,
    CommandOperation,
    ControlRequest,
    InvalidGoalState,
    NoProgressReviewAckRequest,
    PlanRejected,
    ReplanRequest,
    StateRevisionConflict,
    WorkerForbidden,
)
from control_kernel.storage.abandonments import (
    list_orchestration_abandonments,
    record_orchestration_abandonment,
    release_orchestration_abandonment_block,
)
from control_kernel.storage.activation_terminations import (
    acknowledge_no_progress_review,
    list_activation_terminations,
)
from control_kernel.storage.activities import start_goal
from control_kernel.storage.audits import goal_review_budget_snapshot
from control_kernel.storage.budget_clock import goal_wall_budget_snapshot
from control_kernel.storage.evidence_export import SigningUnavailable, start_evidence_export
from control_kernel.storage.finalization import get_release, recover_finalization
from control_kernel.storage.goals import (
    Goals,
    LegacyOrchestrationForbidden,
    content_digest_for,
    lookup_config_digests,
)
from control_kernel.storage.policies import InvalidConfiguration, ScopeNotFound, TrustBlocked
from control_kernel.storage.projects import ProjectConflict
from control_kernel.storage.replan import replan_goal
from fastapi import Depends, FastAPI, Header, Query, Request

from control_api.errors import ApiError
from control_api.identity import Principal


def register_goal_routes(app: FastAPI, identity, store, cursor_key) -> None:
    @app.post("/api/v1/goals", status_code=201, response_model=Envelope[GoalResource])
    def create_goal(
        body: GoalCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        engine = store(request).engine
        try:
            bindings = lookup_config_digests(engine, body.project_id, body)
            digest, canonical = content_digest_for(body, bindings)
            result = Goals(engine).create(
                p.sub, p.project_ids, idempotency_key, body, digest, canonical, bindings
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except InvalidConfiguration as exc:
            detail = str(exc).strip() or "目标合同引用的配置无效"
            raise ApiError(422, "VALIDATION_ERROR", detail) from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "目标合同内容无效") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.put(
        "/api/v1/goals/{goal_id}/contract",
        response_model=Envelope[GoalResource],
    )
    def put_contract(
        goal_id: UUID,
        body: GoalContractUpdate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        engine = store(request).engine
        try:
            bindings = lookup_config_digests(engine, body.contract.project_id, body.contract)
            digest, canonical = content_digest_for(body.contract, bindings)
            result = Goals(engine).update_contract(
                goal_id,
                p.sub,
                p.project_ids,
                idempotency_key,
                body,
                digest,
                canonical,
                bindings,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受合同更新") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法更新合同") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "目标状态版本不匹配") from exc
        except InvalidConfiguration as exc:
            detail = str(exc).strip() or "目标合同引用的配置无效"
            raise ApiError(422, "VALIDATION_ERROR", detail) from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "目标合同内容无效") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/start",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def start(
        goal_id: UUID,
        body: ControlRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = start_goal(
                store(request).engine, goal_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受控制命令") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法启动") from exc
        except StateRevisionConflict as exc:
            raise ApiError(
                409, "STATE_REVISION_CONFLICT", "目标状态版本不匹配，请刷新后重试"
            ) from exc
        except LegacyOrchestrationForbidden as exc:
            raise ApiError(409, "LEGACY_ORCHESTRATION_FORBIDDEN", exc.message) from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/replan",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def replan(
        goal_id: UUID,
        body: ReplanRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = replan_goal(
                store(request).engine, goal_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受控制命令") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法重规划") from exc
        except StateRevisionConflict as exc:
            raise ApiError(
                409, "STATE_REVISION_CONFLICT", "目标状态或计划版本不匹配，请刷新后重试"
            ) from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/finalization-recovery",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def finalization_recovery(
        goal_id: UUID,
        body: FinalizationRecoveryRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = recover_finalization(
                store(request).engine, goal_id, p.sub, p.project_ids, idempotency_key, body
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受控制命令") from exc
        except InvalidGoalState as exc:
            raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法恢复验收") from exc
        except StateRevisionConflict as exc:
            raise ApiError(
                409, "STATE_REVISION_CONFLICT", "目标状态、计划或 epoch 不匹配，请刷新后重试"
            ) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", str(exc) or "恢复验收请求无效") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    def _control_command(kind: str, handler):
        @app.post(
            f"/api/v1/goals/{{goal_id}}/{kind}",
            status_code=202,
            response_model=Envelope[CommandOperation],
        )
        def _route(
            goal_id: UUID,
            body: ControlRequest,
            request: Request,
            p: Annotated[Principal, Depends(identity)],
            idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
        ):
            if "operator" not in p.roles:
                raise ApiError(403, "FORBIDDEN", "需要操作员权限")
            try:
                result = handler(
                    store(request).engine, goal_id, p.sub, p.project_ids, idempotency_key, body
                )
            except ScopeNotFound as exc:
                raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
            except TrustBlocked as exc:
                raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受控制命令") from exc
            except InvalidGoalState as exc:
                raise ApiError(409, "INVALID_STATE", f"目标状态为 {exc.status}，无法执行该命令") from exc
            except StateRevisionConflict as exc:
                raise ApiError(
                    409, "STATE_REVISION_CONFLICT", "目标状态版本不匹配，请刷新后重试"
                ) from exc
            except ProjectConflict as exc:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
            return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

        _route.__name__ = f"goal_{kind}"
        return _route

    from control_kernel.storage.control_commands import cancel_goal, pause_goal, resume_goal

    _control_command("pause", pause_goal)
    _control_command("resume", resume_goal)
    _control_command("cancel", cancel_goal)

    @app.get("/api/v1/goals/{goal_id}", response_model=Envelope[GoalResource])
    def get_goal(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        human = set(p.roles) & {"viewer", "operator", "approver", "admin"}
        if not human and "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        try:
            if human:
                result = Goals(store(request).engine).get(goal_id, p.sub, p.project_ids)
            else:
                from control_kernel.protocols.runtime import WorkerForbidden

                try:
                    result = Goals(store(request).engine).get_for_worker(goal_id, p.sub)
                except WorkerForbidden as exc:
                    raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/goals/{goal_id}/wall-budget",
        response_model=Envelope[GoalWallBudgetSnapshot],
    )
    def read_goal_wall_budget(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """推进并返回墙钟预算快照（编排/worker 只读消费）；绝不写 Goal DONE。

        ACTIVE worker 在 admit 前也需读剩余墙钟，故不要求已持有 ACTIVE attempt
        （与 GET Goal 的 get_for_worker 门不同）。
        """
        from sqlalchemy import text

        human = set(p.roles) & {"viewer", "operator", "approver", "admin"}
        if not human and "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        engine = store(request).engine
        try:
            if human:
                Goals(engine).get(goal_id, p.sub, p.project_ids)
            else:
                with engine.connect() as db:
                    worker = (
                        db.execute(
                            text(
                                """SELECT id FROM workers
                                WHERE subject=:subject AND status='ACTIVE'"""
                            ),
                            {"subject": p.sub},
                        )
                        .mappings()
                        .first()
                    )
                    if worker is None:
                        raise WorkerForbidden()
                    exists = db.execute(
                        text("SELECT 1 FROM goals WHERE id=:id"),
                        {"id": goal_id},
                    ).first()
                    if exists is None:
                        raise ScopeNotFound()
            with engine.begin() as db:
                snap = goal_wall_budget_snapshot(db, goal_id)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        data = GoalWallBudgetSnapshot.model_validate(
            {**snap, "marks_goal_done": False}
        )
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/goals/{goal_id}/goal-review-budget",
        response_model=Envelope[GoalReviewBudgetSnapshot],
    )
    def read_goal_review_budget(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """只读 GoalReview 复盘预算快照；不创建 Activity、绝不写 DONE。

        权限同 wall-budget：人类项目 scope，或 ACTIVE 登记 worker。
        """
        from sqlalchemy import text

        human = set(p.roles) & {"viewer", "operator", "approver", "admin"}
        if not human and "worker" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        engine = store(request).engine
        try:
            if human:
                Goals(engine).get(goal_id, p.sub, p.project_ids)
            else:
                with engine.connect() as db:
                    worker = (
                        db.execute(
                            text(
                                """SELECT id FROM workers
                                WHERE subject=:subject AND status='ACTIVE'"""
                            ),
                            {"subject": p.sub},
                        )
                        .mappings()
                        .first()
                    )
                    if worker is None:
                        raise WorkerForbidden()
                    exists = db.execute(
                        text("SELECT 1 FROM goals WHERE id=:id"),
                        {"id": goal_id},
                    ).first()
                    if exists is None:
                        raise ScopeNotFound()
            with engine.connect() as db:
                snap = goal_review_budget_snapshot(db, goal_id)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except PlanRejected as exc:
            raise ApiError(
                422,
                exc.code or "VALIDATION_ERROR",
                str(exc),
            ) from exc
        data = GoalReviewBudgetSnapshot.model_validate(
            {**snap, "marks_goal_done": False}
        )
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/orchestration-abandonments",
        response_model=Envelope[OrchestrationAbandonmentResource],
        status_code=201,
    )
    def create_goal_orchestration_abandonment(
        goal_id: UUID,
        body: OrchestrationAbandonmentCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """登记编排放弃 → Goal BLOCKED；幂等 (goal_id, generation)；≠ DONE。"""
        if "worker" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要已登记 worker 提交编排放弃")
        engine = store(request).engine
        try:
            row = record_orchestration_abandonment(
                engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                reason=body.reason,
                generation=body.generation,
                prior_run_id=body.prior_run_id,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "未登记 worker 或无权操作") from exc
        except PlanRejected as exc:
            raise ApiError(409, "INVALID_STATE", str(exc) or "无法登记编排放弃") from exc
        data = OrchestrationAbandonmentResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "编排放弃不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/goals/{goal_id}/orchestration-abandonments",
        response_model=Envelope[list[OrchestrationAbandonmentResource]],
    )
    def list_goal_orchestration_abandonments(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """只读列出编排放弃事实；marks_goal_done 恒 false。"""
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        try:
            rows = list_orchestration_abandonments(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        data = [OrchestrationAbandonmentResource.model_validate(r) for r in rows]
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.get(
        "/api/v1/goals/{goal_id}/activation-terminations",
        response_model=Envelope[list[ActivationTerminationResource]],
    )
    def list_goal_activation_terminations(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """只读列出 activation 终止事实；marks_goal_done 恒 false；≠ Goal DONE。"""
        if not set(p.roles) & {"viewer", "operator", "approver", "admin", "worker"}:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        try:
            rows = list_activation_terminations(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        data = [ActivationTerminationResource.model_validate(r) for r in rows]
        if any(item.marks_goal_done for item in data):
            raise ApiError(500, "INTERNAL", "activation 终止不得写 Goal DONE")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/no-progress-review-ack",
        response_model=Envelope[ActivationTerminationResource],
        status_code=201,
    )
    def post_no_progress_review_ack(
        goal_id: UUID,
        body: NoProgressReviewAckRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """Operator 确认无进展复盘（GOAL_REQUIRES_REVIEW）；可解除 SEAL/写入闸；≠ DONE。"""
        if "operator" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限确认无进展复盘")
        try:
            row = acknowledge_no_progress_review(
                store(request).engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                expected_state_revision=body.expected_state_revision,
                detail=body.detail,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", str(exc) or "无权确认无进展复盘") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "状态版本冲突") from exc
        except PlanRejected as exc:
            code = "VALIDATION_ERROR"
            msg = exc.message if hasattr(exc, "message") else str(exc)
            if msg.startswith("NO_PROGRESS_"):
                code = msg.split(":", 1)[0]
            raise ApiError(422, code, msg) from exc
        data = ActivationTerminationResource.model_validate(row)
        if data.marks_goal_done:
            raise ApiError(500, "INTERNAL", "复盘确认不得写 Goal DONE")
        if data.reason != "GOAL_REQUIRES_REVIEW":
            raise ApiError(500, "INTERNAL", "复盘确认必须落 GOAL_REQUIRES_REVIEW")
        return Envelope(data=data, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/orchestration-abandonment-release",
        response_model=Envelope[GoalResource],
    )
    def release_goal_orchestration_abandonment(
        goal_id: UUID,
        body: ControlRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        """人工解除 ORCHESTRATION_ABANDONED BLOCKED；恢复 previous_status；≠ DONE。"""
        if "operator" not in p.roles and "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        engine = store(request).engine
        try:
            release_orchestration_abandonment_block(
                engine,
                goal_id,
                subject=p.sub,
                project_ids=p.project_ids,
                expected_state_revision=body.expected_state_revision,
            )
            result = Goals(engine).get(goal_id, p.sub, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker 或无权操作") from exc
        except StateRevisionConflict as exc:
            raise ApiError(
                409, "STATE_REVISION_CONFLICT", "目标状态版本不匹配，请刷新后重试"
            ) from exc
        except PlanRejected as exc:
            raise ApiError(409, "INVALID_STATE", str(exc) or "无法解除编排放弃封锁") from exc
        if result.status == "DONE":
            raise ApiError(500, "INTERNAL", "解除放弃不得写 Goal DONE")
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/goals/{goal_id}/release", response_model=Envelope[ReleaseView])
    def read_release(
        goal_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        try:
            result = get_release(store(request).engine, goal_id, p.project_ids)
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "尚无发布清单") from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/api/v1/goals/{goal_id}/evidence-exports",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def export_evidence(
        goal_id: UUID,
        body: EvidenceExportRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "operator" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要操作员权限")
        try:
            result = start_evidence_export(
                store(request).engine,
                goal_id,
                p.sub,
                p.project_ids,
                idempotency_key,
                body,
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受导出命令") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except SigningUnavailable as exc:
            raise ApiError(
                503,
                "SIGNING_UNAVAILABLE",
                "未配置签名提供方与信任根，无法生成离线可验证导出",
            ) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.get("/api/v1/goals", response_model=Envelope[list[GoalResource]])
    def list_goals(
        project_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        status: Annotated[GoalStatus | None, Query()] = None,
    ):
        if not set(p.roles) & {"viewer", "operator", "approver", "admin"}:
            raise ApiError(403, "FORBIDDEN", "没有目标读取权限")
        secret = cursor_key()
        scope = json.dumps(
            ["/api/v1/goals", str(project_id), p.sub, sorted(p.project_ids), status],
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
            rows = Goals(store(request).engine).list_goals(
                project_id, p.sub, p.project_ids, limit + 1, after, status
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
