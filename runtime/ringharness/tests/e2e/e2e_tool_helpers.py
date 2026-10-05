"""E2E 共用：prepare step/effect 与订单参考修复正文。"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

from control_kernel.storage.artifacts import Artifacts

_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE_STORE = (
    _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "reference_fix" / "store.py"
)


def load_reference_fix_store() -> str:
    """读取 fixture 参考修复正文（真实业务代码），供 write_file 写入 Executor 树。"""
    return _REFERENCE_STORE.read_text(encoding="utf-8")


def prepare_tool_effect(
    ctx: dict,
    *,
    tool_ref: str,
    purpose: str,
    parameters: dict,
    schema_digest: str,
    predecessor_step_id: str | None,
    producer: str = "e2e-tool",
    lease_key: str = "exec_lease",
    auth_key: str = "exec_auth",
) -> tuple[dict, dict, bytes]:
    """创建 step → 入库 ToolPayload → prepare effect。返回 (step, effect, input_bytes)。"""
    client = ctx["client"]
    worker_auth = ctx[auth_key]
    lease_bundle = ctx[lease_key]
    activity_id = lease_bundle["activity"]["id"]
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": lease_bundle["lease"],
            "predecessor_step_id": predecessor_step_id,
            "purpose": purpose,
            "tool_ref": tool_ref,
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    input_blob = json.dumps(
        {
            "tool_ref": tool_ref,
            "tool_schema_digest": schema_digest,
            "parameters": parameters,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity=f"test:{producer}-{tool_ref}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": lease_bundle["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": tool_ref,
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    return step_data, prepared.json()["data"], input_blob
