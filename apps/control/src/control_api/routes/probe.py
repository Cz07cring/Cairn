"""模型探测与 ModelInvocation 内部端口。"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from context_compiler import CompileRejected, TokenBudget
from control_kernel.domain.cloud_endpoint import CloudModeDenied
from control_kernel.protocols.models import (
    ContextBindRequest,
    ContextBindResponse,
    ModelDispatchRequest,
    ModelInvocationCreate,
    ModelInvocationResource,
    ModelReceipt,
    ModelReceiptResponse,
)
from control_kernel.protocols.projects import Contract, Envelope, Meta
from control_kernel.protocols.runtime import (
    CommandOperation,
    LeaseIdentity,
    LeaseRejected,
    PlanRejected,
    StateRevisionConflict,
    WorkerForbidden,
)
from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.context_compile import compile_and_persist_context
from control_kernel.storage.policies import ScopeNotFound, TrustBlocked
from control_kernel.storage.probe import (
    apply_receipt,
    bind_context,
    create_context_bundle,
    create_invocation,
    mark_dispatched,
    start_probe,
)
from control_kernel.storage.projects import ProjectConflict
from fastapi import Depends, FastAPI, Header, Request
from pydantic import Field
from sqlalchemy import text

from control_api.connectors.local_qwen import (
    LocalQwenUnavailable,
    chat_completion,
    format_gpu_seconds,
    is_loopback_inference_base,
)
from control_api.errors import ApiError
from control_api.identity import Principal
from control_api.settings import Settings

logger = logging.getLogger(__name__)


class ContextBundleCreateBody(Contract):
    lease: LeaseIdentity
    content: dict = Field(default_factory=dict)


class ContextCompileBody(Contract):
    lease: LeaseIdentity
    max_input_tokens: int = Field(default=8192, ge=1, le=9007199254740991)


def register_probe_routes(app: FastAPI, identity, store, settings: Settings) -> None:
    @app.post(
        "/api/v1/model-profiles/{profile_id}/probe",
        status_code=202,
        response_model=Envelope[CommandOperation],
    )
    def probe_model(
        profile_id: UUID,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        if "admin" not in p.roles:
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        try:
            result = start_probe(
                store(request).engine, profile_id, p.sub, p.project_ids, idempotency_key
            )
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受探测") from exc
        except ProjectConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于其他内容") from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/activities/{activity_id}/context-bundles",
        status_code=201,
        response_model=Envelope[dict],
    )
    def post_context_bundle(
        activity_id: UUID,
        body: ContextBundleCreateBody,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            bundle_id, digest = create_context_bundle(
                store(request).engine,
                p.sub,
                activity_id,
                body.lease.activity_id,
                body.lease.attempt_id,
                body.lease.fencing_epoch,
                body.content,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受上下文绑定") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "上下文内容无效") from exc
        return Envelope(
            data={"id": str(bundle_id), "content_digest": digest},
            meta=Meta(request_id=request.state.request_id),
        )

    @app.post(
        "/internal/v1/activities/{activity_id}/context-compile",
        status_code=201,
        response_model=Envelope[dict],
    )
    def post_context_compile(
        activity_id: UUID,
        body: ContextCompileBody,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        objects = request.app.state.objects
        if objects is None:
            raise ApiError(503, "OBJECT_STORE_UNAVAILABLE", "对象仓未配置")
        try:
            bundle_id, digest, content = compile_and_persist_context(
                store(request).engine,
                objects,
                p.sub,
                activity_id,
                body.lease.activity_id,
                body.lease.attempt_id,
                body.lease.fencing_epoch,
                TokenBudget(max_input_tokens=body.max_input_tokens),
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except TrustBlocked as exc:
            raise ApiError(409, "INVALID_STATE", "项目信任核对中，暂不接受上下文编译") from exc
        except PlanRejected as exc:
            code = "VALIDATION_ERROR"
            if exc.message.startswith("GOAL_INFERENCE_CLOSED"):
                code = "GOAL_INFERENCE_CLOSED"
            raise ApiError(422, code, exc.message) from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except CompileRejected as exc:
            raise ApiError(422, "VALIDATION_ERROR", exc.message) from exc
        except RuntimeError as exc:
            # 仅「对象仓未配置」映射 503；其它 RuntimeError 暴露原文，避免误诊
            if "object storage not configured" in str(exc):
                raise ApiError(503, "OBJECT_STORE_UNAVAILABLE", "对象仓未配置") from exc
            raise ApiError(500, "INTERNAL_ERROR", f"上下文编译失败: {exc}") from exc
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "上下文内容无效") from exc
        return Envelope(
            data={
                "id": str(bundle_id),
                "content_digest": digest,
                "content": content,
            },
            meta=Meta(request_id=request.state.request_id),
        )

    @app.post(
        "/internal/v1/activities/{activity_id}/context",
        response_model=Envelope[ContextBindResponse],
    )
    def post_context(
        activity_id: UUID,
        body: ContextBindRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = bind_context(store(request).engine, p.sub, activity_id, body)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            raise ApiError(409, "BINDING_STALE", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/model-invocations",
        status_code=201,
        response_model=Envelope[ModelInvocationResource],
    )
    def post_invocation(
        body: ModelInvocationCreate,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        try:
            result = create_invocation(store(request).engine, p.sub, body)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            if exc.message == "MODEL_INPUT_CONFLICT":
                code = "MODEL_INPUT_CONFLICT"
                status = 409
            elif exc.message.startswith("GOAL_INFERENCE_CLOSED"):
                code = "GOAL_INFERENCE_CLOSED"
                status = 422
            else:
                code = "VALIDATION_ERROR"
                status = 422
            raise ApiError(status, code, exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))

    @app.post(
        "/internal/v1/model-invocations/{invocation_id}/dispatch",
        response_model=Envelope[ModelInvocationResource],
    )
    def dispatch_invocation(
        invocation_id: UUID,
        body: ModelDispatchRequest,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        engine = store(request).engine
        cfg: Settings = request.app.state.settings
        try:
            dispatched = mark_dispatched(
                engine,
                p.sub,
                invocation_id,
                body,
                endpoint_base=cfg.local_qwen_base,
                deployment_cloud_mode=cfg.cloud_mode,
            )
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except StateRevisionConflict as exc:
            raise ApiError(409, "STATE_REVISION_CONFLICT", "调用状态版本不匹配") from exc
        except LeaseRejected as exc:
            status = 409 if exc.code != "INVALID_REQUEST" else 400
            raise ApiError(status, exc.code, exc.message) from exc
        except PlanRejected as exc:
            if exc.message.startswith("GOAL_INFERENCE_CLOSED"):
                raise ApiError(422, "GOAL_INFERENCE_CLOSED", exc.message) from exc
            raise ApiError(409, "INVALID_STATE", exc.message) from exc
        except CloudModeDenied as exc:
            # 未进入 DISPATCHED；禁止写成「已派发后失败」
            raise ApiError(403, "CLOUD_MODE_DENIED", exc.message) from exc

        # 官方 AgentLoop：Runner 自持多轮 chat，Control 只迁 DISPATCHED；须随后 receipts
        if body.runner_owned_completion:
            return Envelope(
                data=dispatched, meta=Meta(request_id=request.state.request_id)
            )

        # 事务外真实调用；失败记 UNKNOWN/FAILED 回执，不假装成功。
        api_key = (
            cfg.local_qwen_api_key.get_secret_value() if cfg.local_qwen_api_key else None
        )
        base = cfg.local_qwen_base
        try:
            if not api_key:
                raise LocalQwenUnavailable("本地模型 API key 未配置")
            # kind-aware：PLAN 零工具权威提示；EXECUTE 暴露登记工具并解析 tool_calls
            criterion_id = "C1"
            activity_kind = "PLAN"
            with engine.connect() as db:
                row = db.execute(
                    text(
                        """SELECT a.kind AS activity_kind,
                                  g.contract->'success_criteria'->0->>'id' AS cid
                        FROM model_invocations mi
                        JOIN activities a ON a.id = mi.activity_id
                        LEFT JOIN goals g ON g.id = a.goal_id
                        WHERE mi.id=:id"""
                    ),
                    {"id": invocation_id},
                ).mappings().first()
                if row and row.get("cid"):
                    criterion_id = str(row["cid"])
                if row and row.get("activity_kind"):
                    activity_kind = str(row["activity_kind"])

            from control_kernel.domain.tool_capability_manifest import (
                READ_FILE_PARAM_SCHEMA,
            )
            from control_kernel.protocols.models import ModelToolCall

            chat_tools: list[dict] | None = None
            if activity_kind == "EXECUTE":
                exposed = list(dispatched.exposed_tools or [])
                chat_tools = []
                for name in exposed:
                    if name == "read_file":
                        chat_tools.append(
                            {
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "description": "读取工作区内只读文件",
                                    "parameters": READ_FILE_PARAM_SCHEMA,
                                },
                            }
                        )
                    else:
                        raise LocalQwenUnavailable(
                            f"连接器尚未支持工具 schema: {name}"
                        )
                if not chat_tools:
                    chat_tools = None
                execute_prompt = "\n".join(
                    [
                        f"EXECUTE context_digest={dispatched.context_digest}",
                        "你是受控 Executor。需要读文件时调用 read_file；不要编造文件内容。",
                        "若无需工具，用一句话说明原因。",
                    ]
                )
                messages = [{"role": "user", "content": execute_prompt}]
            else:
                # PLAN / PROBE 等：零工具；Kernel 权威提示
                plan_prompt = "\n".join(
                    [
                        f"PLAN context_digest={dispatched.context_digest}",
                        "零工具 Planner。禁止解释与 markdown。只输出一行 JSON。",
                        '格式严格为：{"reason":"...","objective":"...","acceptance_description":"..."}',
                        "三个字符串均非空，每个不超过40个字。",
                        f"参考 goal_criterion_id={criterion_id}",
                    ]
                )
                messages = [{"role": "user", "content": plan_prompt}]

            # 取调用方登记值与「推理模型下限」的较大者：登记值通常来自 Runner 的固定
            # 预算，推理模型会把它全吃在 reasoning 上 ⇒ content 为空。下限可经
            # RING_MODEL_MIN_OUTPUT_TOKENS 调，避免再次硬编码。
            max_tokens = max(dispatched.max_output_tokens, cfg.model_min_output_tokens)
            call = chat_completion(
                base_url=base,
                api_key=api_key,
                model_id=dispatched.model_id,
                max_tokens=max_tokens,
                messages=messages,
                tools=chat_tools,
            )
            response = call["response"]
            returned_model = response.get("model") or dispatched.model_id
            usage = response.get("usage") or {}
            # 本机 loopback 推理墙钟计入 gpu_seconds；远程 API 不计本机 GPU
            gpu_seconds = None
            if is_loopback_inference_base(base):
                gpu_seconds = format_gpu_seconds(
                    float(call.get("elapsed_wall_seconds") or 0)
                )
            choices = response.get("choices") or []
            assistant_text = ""
            parsed_tool_calls: list[ModelToolCall] = []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                if isinstance(message, dict):
                    assistant_text = str(message.get("content") or "").strip()
                    raw_calls = message.get("tool_calls") or []
                    if isinstance(raw_calls, list):
                        for raw in raw_calls:
                            if not isinstance(raw, dict):
                                continue
                            fn = raw.get("function") or {}
                            if not isinstance(fn, dict):
                                continue
                            name = str(fn.get("name") or "").strip()
                            if not name:
                                continue
                            parsed_tool_calls.append(
                                ModelToolCall(
                                    id=str(raw.get("id") or uuid4()),
                                    name=name,
                                    arguments=str(fn.get("arguments") or "{}"),
                                )
                            )
            finish_reason = (
                str((choices[0] if choices and isinstance(choices[0], dict) else {}).get(
                    "finish_reason"
                ) or "")
            )
            reasoning_tokens = (
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                if isinstance(usage, dict)
                else None
            )
            if not assistant_text and not parsed_tool_calls:
                # 让「预算被推理吃光」与「模型确实没产出」在日志里可区分。
                # 二者此前只表现为 Runner 侧的 MODEL_INVOCATION_LIVE_EMPTY，排查绕远。
                reason = (
                    "推理 token 已吃满上限（finish_reason=length），需提高 "
                    "RING_MODEL_MIN_OUTPUT_TOKENS"
                    if finish_reason == "length" and reasoning_tokens
                    else f"模型未产出正文或工具调用（finish_reason={finish_reason or '未知'}）"
                )
                logger.warning(
                    "live dispatch 空产出：kind=%s model=%s max_tokens=%s finish_reason=%s "
                    "reasoning_tokens=%s 原因=%s",
                    activity_kind,
                    dispatched.model_id,
                    max_tokens,
                    finish_reason or "未知",
                    reasoning_tokens,
                    reason,
                )
            evidence = {
                "provider_ref": dispatched.provider_ref,
                "requested_model_id": dispatched.model_id,
                "returned_model_id": returned_model,
                "id": response.get("id"),
                "usage": usage,
                "activity_kind": activity_kind,
                "exposed_tools": list(dispatched.exposed_tools or []),
                "assistant_text_preview": assistant_text[:500],
                "tool_call_names": [c.name for c in parsed_tool_calls],
                "elapsed_wall_seconds": call.get("elapsed_wall_seconds"),
                "gpu_seconds_recorded": gpu_seconds,
                "max_tokens_used": max_tokens,
                "finish_reason": finish_reason,
            }
            blob = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
            digest = "sha256:" + hashlib.sha256(blob).hexdigest()
            objects = request.app.state.objects
            if objects is None:
                raise LocalQwenUnavailable("对象仓未配置，无法封存探测证据")
            from io import BytesIO

            artifact = Artifacts(engine, objects).ingest_raw(
                dispatched.project_id,
                digest,
                BytesIO(blob),
                mime="application/json",
                producer_identity=f"connector:{p.sub}",
            )
            model_ok = returned_model == dispatched.model_id or dispatched.model_id in str(
                returned_model
            )
            observed = "SUCCEEDED" if model_ok else "FAILED"
            receipt = ModelReceipt(
                receipt_id=uuid4(),
                invocation_id=invocation_id,
                producer_attempt_id=dispatched.producer_attempt_id,
                observed_result=observed,
                usage_status="CONFIRMED" if usage else "UNKNOWN",
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                cost_usd="0",
                gpu_seconds=gpu_seconds,
                result_artifact_id=artifact.id,
                observed_at=datetime.now(UTC),
            )
            apply_receipt(engine, p.sub, receipt)
            with engine.connect() as db:
                row = (
                    db.execute(
                        text("SELECT * FROM model_invocations WHERE id=:id"),
                        {"id": invocation_id},
                    )
                    .mappings()
                    .one()
                )
            from control_kernel.storage.probe import _invocation_from_row

            resource = _invocation_from_row(row)
            # 响应附加模型正文 / tool_calls；不写入库行
            resource = resource.model_copy(
                update={
                    "assistant_text": assistant_text or None,
                    "tool_calls": parsed_tool_calls or None,
                }
            )
            return Envelope(
                data=resource,
                meta=Meta(request_id=request.state.request_id),
            )
        except LocalQwenUnavailable as exc:
            receipt = ModelReceipt(
                receipt_id=uuid4(),
                invocation_id=invocation_id,
                producer_attempt_id=dispatched.producer_attempt_id,
                observed_result="UNKNOWN",
                usage_status="UNKNOWN",
                observed_at=datetime.now(UTC),
            )
            apply_receipt(engine, p.sub, receipt)
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", exc.message) from exc

    @app.post(
        "/internal/v1/model-invocations/{invocation_id}/receipts",
        status_code=201,
        response_model=Envelope[ModelReceiptResponse],
    )
    def post_receipt(
        invocation_id: UUID,
        body: ModelReceipt,
        request: Request,
        p: Annotated[Principal, Depends(identity)],
    ):
        if body.invocation_id != invocation_id:
            raise ApiError(400, "INVALID_REQUEST", "回执与路径调用不一致")
        try:
            result = apply_receipt(store(request).engine, p.sub, body)
        except WorkerForbidden as exc:
            raise ApiError(403, "FORBIDDEN", "未登记 worker") from exc
        except ScopeNotFound as exc:
            raise ApiError(404, "NOT_FOUND", "资源不存在") from exc
        except PlanRejected as exc:
            raise ApiError(409, "INVALID_STATE", exc.message) from exc
        return Envelope(data=result, meta=Meta(request_id=request.state.request_id))
