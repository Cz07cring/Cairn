"""AB01 真实 Control+Broker：ToolPayload prepare → run_read_file_effect → evidence 可引用。

不写 Goal DONE；不宣称官方 AgentLoop。live 第二轮需 RING_LOCAL_QWEN_*。
"""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from control_kernel.domain.tool_capability_manifest import READ_FILE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_read_file_effect
from test_effects import _publish_and_claim_execute

MARKER = "RING_AB01_REAL_BROKER_a91e"
REL_PATH = "src/ab01.txt"

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取工作区文本文件",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _tool_payload_bytes(path: str) -> bytes:
    return json.dumps(
        {
            "tool_ref": "read_file",
            "tool_schema_digest": READ_FILE_SCHEMA_DIGEST,
            "parameters": {"path": path},
        },
        separators=(",", ":"),
    ).encode()


def _chat_env_ready() -> bool:
    # 与 probe / serve_local 同名变量；测试进程可能已由 conftest/runtime 注入
    base = (os.environ.get("RING_LOCAL_QWEN_BASE") or "").strip()
    key = (os.environ.get("RING_LOCAL_QWEN_API_KEY") or "").strip()
    model = (os.environ.get("RING_LOCAL_QWEN_MODEL") or "").strip()
    return bool(base and key and model)


def _load_runtime_chat_env() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("chat.env", "qwen.env"):
        path = root / ".runtime" / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
                continue
            k, _, v = trimmed.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and not (os.environ.get(k) or "").strip():
                os.environ[k] = v


def _assistant_cites(text: str, tool_result: str) -> bool:
    needles = [
        ln.strip()
        for ln in tool_result.splitlines()
        if ln.strip()
        and not ln.strip().startswith("[tool_result_spilled]")
        and len(ln.strip()) >= 6
    ]
    return any(n in text for n in needles)


def _prepare_and_run_read_file(api, objects, tmp_path: Path, *, file_body: bytes):
    store, _, _ = objects
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "ab01.txt").write_bytes(file_body)

    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "ab01 real read_file",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text

    input_blob = _tool_payload_bytes(REL_PATH)
    input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        input_digest,
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:ab01",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["status"] == "PREPARED"

    ran = run_read_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED"
    assert ran["result"].content == file_body
    assert ran["receipt"]["disposition"] == "APPLIED"

    effect_got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert effect_got.status_code == 200
    assert effect_got.json()["data"]["status"] == "SUCCEEDED"
    evidence_ids = effect_got.json()["data"].get("evidence_ids") or ran["result_artifact_ids"]
    assert evidence_ids

    art_id = evidence_ids[0]
    content = client.get(f"/api/v1/artifacts/{art_id}/content", headers=auth)
    assert content.status_code == 200
    assert content.content == file_body
    assert MARKER.encode() in content.content

    # 业务 Goal 不得因 effect SUCCEEDED 变 DONE
    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.status_code == 200
    assert goal_got.json()["data"]["status"] != "DONE"

    return {
        "client": client,
        "auth": auth,
        "goal": goal,
        "effect_id": effect["id"],
        "evidence_text": content.content.decode("utf-8"),
        "marks_goal_done": False,
    }


def test_ab01_real_broker_prepare_dispatch_succeeded_evidence(api, objects, tmp_path: Path):
    """真实 PREPARED→dispatch→SUCCEEDED+evidence；内容含 AB01 标记且 ≠ Goal DONE。"""
    body = f"ab01 workspace\n{MARKER}\nend\n".encode()
    out = _prepare_and_run_read_file(api, objects, tmp_path, file_body=body)
    assert out["marks_goal_done"] is False
    assert MARKER in out["evidence_text"]
    assert _assistant_cites(f"标记是 {MARKER}", out["evidence_text"]) is True


def test_ab01_real_broker_then_live_chat_cites_evidence(api, objects, tmp_path: Path):
    """真实 Broker evidence 按 tool_call_id 回灌第二轮 live chat（缺 env 则 skip）。"""
    _load_runtime_chat_env()
    if not _chat_env_ready():
        pytest.skip("缺 RING_LOCAL_QWEN_BASE/API_KEY/MODEL")

    from control_api.connectors.local_qwen import LocalQwenUnavailable, chat_completion

    body = f"live cite\n{MARKER}\n".encode()
    out = _prepare_and_run_read_file(api, objects, tmp_path, file_body=body)
    tool_call_id = "call_ab01_real_fixture"

    messages = [
        {
            "role": "system",
            "content": "根据工具返回原文回答；必须照抄以 RING_ 开头的标记。",
        },
        {"role": "user", "content": f"请调用 read_file 读取 {REL_PATH}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": REL_PATH}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": out["evidence_text"],
        },
        {
            "role": "user",
            "content": "原样复述工具返回中以 RING_ 开头的标记字符串。",
        },
    ]
    try:
        done = chat_completion(
            base_url=os.environ["RING_LOCAL_QWEN_BASE"],
            api_key=os.environ["RING_LOCAL_QWEN_API_KEY"],
            model_id=os.environ["RING_LOCAL_QWEN_MODEL"],
            max_tokens=128,
            messages=messages,
        )
    except LocalQwenUnavailable as exc:
        pytest.skip(f"chat 不可用: {exc.message}")

    choice = (done["response"].get("choices") or [{}])[0]
    assistant = ((choice.get("message") or {}).get("content") or "").strip()
    assert assistant, done["response"]
    assert MARKER in assistant
    assert _assistant_cites(assistant, out["evidence_text"])
    assert out["marks_goal_done"] is False


def test_ab01_live_model_tool_call_then_real_broker(api, objects, tmp_path: Path):
    """真实模型发 read_file → 真实 Broker → 第二轮引用（完整 AB01 缝）。"""
    _load_runtime_chat_env()
    if not _chat_env_ready():
        pytest.skip("缺 RING_LOCAL_QWEN_BASE/API_KEY/MODEL")

    from control_api.connectors.local_qwen import LocalQwenUnavailable, chat_completion

    body = f"model-driven\n{MARKER}\n".encode()
    # 先准备工作区与 lease，再让模型发 tool_call，再 prepare/run
    store, _, _ = objects
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "ab01.txt").write_bytes(body)

    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    round1_msgs = [
        {
            "role": "system",
            "content": "必须通过工具 read_file 读文件；不要臆造内容。",
        },
        {
            "role": "user",
            "content": f"你必须调用工具 read_file，参数 path 恰好为 {REL_PATH}。",
        },
    ]
    try:
        round1 = chat_completion(
            base_url=os.environ["RING_LOCAL_QWEN_BASE"],
            api_key=os.environ["RING_LOCAL_QWEN_API_KEY"],
            model_id=os.environ["RING_LOCAL_QWEN_MODEL"],
            max_tokens=256,
            messages=round1_msgs,
            tools=[READ_FILE_TOOL],
        )
    except LocalQwenUnavailable as exc:
        pytest.skip(f"chat 不可用: {exc.message}")

    message = ((round1["response"].get("choices") or [{}])[0].get("message")) or {}
    tool_calls = message.get("tool_calls") or []
    read_calls = [
        c
        for c in tool_calls
        if (c.get("function") or {}).get("name") == "read_file"
    ]
    assert len(read_calls) == 1, message
    call = read_calls[0]
    call_id = call["id"]
    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
    assert args.get("path") == REL_PATH

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": f"tool:{call_id}",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = _tool_payload_bytes(REL_PATH)
    input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        input_digest,
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:ab01-live",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    ran = run_read_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=prepared.json()["data"],
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED"
    evidence_text = ran["result"].content.decode("utf-8")

    round2_msgs = [
        *round1_msgs,
        {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        },
        {"role": "tool", "tool_call_id": call_id, "content": evidence_text},
        {
            "role": "user",
            "content": "原样复述工具返回中以 RING_ 开头的标记字符串。",
        },
    ]
    round2 = chat_completion(
        base_url=os.environ["RING_LOCAL_QWEN_BASE"],
        api_key=os.environ["RING_LOCAL_QWEN_API_KEY"],
        model_id=os.environ["RING_LOCAL_QWEN_MODEL"],
        max_tokens=128,
        messages=round2_msgs,
    )
    assistant = (
        ((round2["response"].get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        )
        or ""
    ).strip()
    assert MARKER in assistant
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"
