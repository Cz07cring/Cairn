"""TM12 部分举证：秘密/长日志不进 Temporal history payload。

静态扫描 GoalWorkflow / RealTemporalClient 源码与 RunActivation 入参键；
另用 time-skipping 导出 history JSON，断言无 JWT/token/长日志。
禁 LEGACY fallback；不标 Goal DONE。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import json
import re
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from orchestration.client import (
    _GOAL_WORKFLOW_OPTIONAL_KEYS,
    DEFAULT_TASK_QUEUE,
    RUNNER_TASK_QUEUE,
    RealTemporalClient,
    build_goal_workflow_payload,
)
from orchestration.temporal_workflows import GoalWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Workflow / Real 入参允许的键（ids + 测试用观察轮次）
_GOAL_WORKFLOW_PAYLOAD_KEYS = frozenset(
    {"command_id", "goal_id", "owner_epoch", *_GOAL_WORKFLOW_OPTIONAL_KEYS}
)
_RUN_ACTIVATION_INPUT_KEYS = frozenset(
    {
        "activity_id",
        "attempt_id",
        "goal_id",
        "kind",
        "fencing_epoch",
        "owner_epoch",
    }
)
# history / 源码中禁止出现的秘密或大上下文字段名
_FORBIDDEN_FIELD_NAMES = frozenset(
    {"token", "jwt", "prompt", "messages", "authorization", "api_key", "password"}
)
_FORBIDDEN_SUBSTRINGS = (
    "Bearer ",
    "RING_WORKER_JWT",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_source(rel: str) -> str:
    return (_repo_root() / rel).read_text(encoding="utf-8")


def test_tm12_goal_workflow_source_has_no_secret_fields():
    """GoalWorkflow 源码：RunActivation 字面量仅 ids；无 token/jwt/prompt/messages。"""
    src = _read_source("packages/orchestration/src/orchestration/temporal_workflows.py")
    tree = ast.parse(src)

    # 收集 execute_activity("RunActivation", …) 的 dict 字面量键
    run_keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # workflow.execute_activity("RunActivation", args=[{...}])
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "execute_activity"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "RunActivation":
            continue
        for kw in node.keywords:
            if kw.arg != "args" or not isinstance(kw.value, ast.List):
                continue
            for elt in kw.value.elts:
                if isinstance(elt, ast.Dict):
                    for key in elt.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            run_keys.add(key.value)

    assert run_keys, "未在 GoalWorkflow 中找到 RunActivation 入参 dict"
    assert run_keys <= _RUN_ACTIVATION_INPUT_KEYS
    assert run_keys.isdisjoint(_FORBIDDEN_FIELD_NAMES)

    # 顶层 payload 读取仅 id 类字段
    for forbidden in _FORBIDDEN_FIELD_NAMES:
        # 避免误伤注释里的英文；只查 .get("…") / ["…"] 形态
        assert not re.search(rf'\.get\(\s*["\']{forbidden}["\']', src)
        assert not re.search(rf'\[["\']{forbidden}["\']\]', src)

    assert "marks_goal_done" in src
    assert "False" in src  # 返回不得 DONE


def test_tm12_real_temporal_client_payload_only_ids():
    """RealTemporalClient 启动 payload 仅 id 类键，且体积极小（无 multi-KB 日志）。

    原实现用**源码文本**断言（在方法体里 grep `"command_id"`）。该写法在把 payload
    构造提为纯函数 `build_goal_workflow_payload` 后失效 —— 但**意图未变**：
    「真实客户端启动时的载荷不得含大对象/禁用字段」。
    改为**行为断言**：直接调用生产用的同一构造器，断言其输出的键集合与体积；
    另保留一条源码检查确认客户端确实调用它（防「测的不是生产路径」）。
    """
    # 行为：直接调用生产构造器（不连 Server）
    payload = build_goal_workflow_payload(
        {"command_id": str(uuid4()), "goal_id": str(uuid4()), "owner_epoch": "1"}, {}
    )
    assert {"command_id", "goal_id", "owner_epoch"} <= set(payload)
    assert set(payload) <= _GOAL_WORKFLOW_PAYLOAD_KEYS
    assert set(payload).isdisjoint(_FORBIDDEN_FIELD_NAMES)
    # 体积极小：无 multi-KB 日志
    assert len(json.dumps(payload)) < 512

    # 链接：客户端启动路径确实使用该构造器（否则本用例测的不是生产路径）
    src = inspect.getsource(RealTemporalClient._ensure_started_async)
    assert "build_goal_workflow_payload(" in src
    for forbidden in _FORBIDDEN_FIELD_NAMES:
        assert f'"{forbidden}"' not in src
        assert f"'{forbidden}'" not in src


def test_tm12_run_activation_ts_input_type_has_no_secrets():
    """TS RunActivationInput 类型字段不得含 token/jwt/prompt/messages。"""
    ts = _read_source("apps/runner/src/temporal/runActivation.ts")
    # 提取 `export type RunActivationInput = { ... };`
    m = re.search(
        r"export type RunActivationInput\s*=\s*\{([^}]+)\}",
        ts,
        flags=re.DOTALL,
    )
    assert m is not None, "找不到 RunActivationInput 类型"
    body = m.group(1)
    fields = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[?:]", body, flags=re.MULTILINE))
    assert fields == {
        "activity_id",
        "attempt_id",
        "goal_id",
        "kind",
        "fencing_epoch",
        "owner_epoch",
    }
    assert fields.isdisjoint(_FORBIDDEN_FIELD_NAMES)
    assert fields <= _RUN_ACTIVATION_INPUT_KEYS


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-tm12",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict:
    aid = str(uuid4())
    return {
        "actions": [
            {
                "activity_id": aid,
                "action_id": aid,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    return {
        "activity_id": activity_id,
        "attempt_id": str(uuid4()),
        "fencing_epoch": "1",
    }


@activity.defn(name="RunActivation")
async def _stub_run(payload: dict) -> dict:
    # 若 Workflow 误传秘密字段，本 stub 也会在 history 入参里留下痕迹
    assert set(payload) <= _RUN_ACTIVATION_INPUT_KEYS
    assert set(payload).isdisjoint(_FORBIDDEN_FIELD_NAMES)
    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "Harness RunActivation 未接线",
        "kind": payload["kind"],
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe(activity_id: str) -> dict[str, str]:
    return {"activity_id": activity_id, "status": "SUCCEEDED", "kind": "PLAN"}


def test_tm12_exported_history_payloads_have_no_secrets_or_long_logs():
    """导出 history JSON：无 JWT/Bearer、无 multi-KB 单 payload、无禁止字段名。"""

    async def _run() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            goal_id = str(uuid4())
            command_id = str(uuid4())
            async with (
                Worker(
                    env.client,
                    task_queue=DEFAULT_TASK_QUEUE,
                    workflows=[GoalWorkflow],
                    activities=[_stub_ensure, _stub_list, _stub_admit, _stub_observe],
                ),
                Worker(
                    env.client,
                    task_queue=RUNNER_TASK_QUEUE,
                    activities=[_stub_run],
                ),
            ):
                handle = await env.client.start_workflow(
                    GoalWorkflow.run,
                    {
                        "command_id": command_id,
                        "goal_id": goal_id,
                        "owner_epoch": "1",
                        "observe_max_ticks": 1,
                    },
                    id=f"goal-{goal_id}",
                    task_queue=DEFAULT_TASK_QUEUE,
                    execution_timeout=timedelta(seconds=30),
                )
                result = await handle.result()
                assert result["marks_goal_done"] is False
                history = await handle.fetch_history()

        history_json = history.to_json()
        assert len(history_json) < 200_000  # 整段历史不该塞满日志
        for needle in _FORBIDDEN_SUBSTRINGS:
            assert needle not in history_json, f"history 含禁止片段: {needle!r}"

        # 逐 payload：解码 base64 JSON（to_json 形态）后检查键与体积
        parsed = json.loads(history_json)
        for event in parsed.get("events") or []:
            for blob in _iter_json_history_payload_blobs(event):
                assert len(blob) < 4096, "单条 history payload 过大（疑似长日志）"
                # 真 JWT：三段 base64url 点分；普通 JSON base64 也会以 eyJ 开头，不能靠前缀
                if blob.count(".") == 2 and all(
                    part and re.fullmatch(r"[A-Za-z0-9_-]+", part)
                    for part in blob.split(".")
                ):
                    raise AssertionError("history payload 疑似 JWT 三段串")
                try:
                    obj = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    keys_lower = {str(k).lower() for k in obj}
                    assert keys_lower.isdisjoint(_FORBIDDEN_FIELD_NAMES)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict):
                            assert {str(k).lower() for k in item}.isdisjoint(
                                _FORBIDDEN_FIELD_NAMES
                            )

    asyncio.run(_run())


def _iter_json_history_payload_blobs(event: dict) -> list[str]:
    """从 history.to_json() 的 event dict 抽出已解码的 payload 字符串。"""
    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            # Temporal JSON 导出：{"metadata": {...}, "data": "<base64>"}
            if "data" in node and isinstance(node.get("data"), str):
                raw_b64 = node["data"]
                try:
                    decoded = base64.b64decode(raw_b64).decode("utf-8")
                    out.append(decoded)
                except (ValueError, UnicodeDecodeError):
                    out.append(raw_b64)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(event)
    return out
