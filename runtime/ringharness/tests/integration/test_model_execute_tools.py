"""kind-aware ModelInvocation：PLAN 拒工具；EXECUTE dispatch 回传 tool_calls。"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch
from uuid import uuid4

from control_kernel.storage.claims import binding_digest_of
from test_claims import _register_worker, _start_goal
from test_effects import _publish_and_claim_execute


def test_plan_model_invocation_rejects_exposed_tools(api, objects):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    activity_id = lease["activity"]["id"]

    context_content = {
        "project_id": goal["project_id"],
        "goal_id": goal["id"],
        "activity_id": activity_id,
        "role": "PLANNER",
        "goal_contract_revision": None,
        "task_contract_revision": None,
        "plan_revision": None,
        "input_bindings": [],
        "excluded_refs": [],
        "session_generation": 0,
        "compaction_source_digest": None,
        "planning_feedback_ids": [],
    }
    bundle = client.post(
        f"/internal/v1/activities/{activity_id}/context-bundles",
        json={"lease": lease["lease"], "content": context_content},
        headers=worker_auth,
    )
    assert bundle.status_code == 201, bundle.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": bundle.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"messages": [{"role": "user", "content": "ping"}]},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()

    denied = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 32,
            "max_cost_usd": "0",
            "data_categories": [],
            "exposed_tools": ["read_file"],
        },
        headers=worker_auth,
    )
    assert denied.status_code == 422, denied.text
    assert "PLAN" in denied.json()["error"]["message"]


def test_execute_dispatch_returns_tool_calls(api, objects):
    """第167批：EXECUTE live dispatch 解析 tool_calls，连接器收到 tools。"""
    store, _, _ = objects
    client, _token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    # 绑定最小 context
    from control_kernel.storage.claims import binding_digest_of as bdigest

    context_content = {
        "project_id": goal["project_id"],
        "goal_id": goal["id"],
        "activity_id": activity_id,
        "role": "EXECUTOR",
        "goal_contract_revision": None,
        "task_contract_revision": None,
        "plan_revision": None,
        "input_bindings": [],
        "excluded_refs": [],
        "session_generation": 0,
        "compaction_source_digest": None,
        "planning_feedback_ids": [],
    }
    bundle = client.post(
        f"/internal/v1/activities/{activity_id}/context-bundles",
        json={"lease": exec_lease["lease"], "content": context_content},
        headers=worker_auth,
    )
    assert bundle.status_code == 201, bundle.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": exec_lease["lease"],
            "binding_digest": bdigest(exec_lease["activity"]["binding"]),
            "context_bundle_id": bundle.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"messages": [{"role": "user", "content": "read it"}]},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()

    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": exec_lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 64,
            "max_cost_usd": "0",
            "data_categories": [],
            "exposed_tools": ["read_file"],
        },
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    invocation = created.json()["data"]
    assert invocation["exposed_tools"] == ["read_file"]
    assert invocation["status"] == "AUTHORIZED"

    fake_response = {
        "model": invocation["model_id"],
        "id": "chatcmpl-test",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"src/a.ts"}',
                            },
                        }
                    ],
                }
            }
        ],
    }

    with patch("control_api.routes.probe.chat_completion") as mocked:
        mocked.return_value = {"response": fake_response}
        # 注入假 API key，避免 503
        app = client.app
        old = app.state.settings
        from pydantic import SecretStr

        app.state.settings = old.model_copy(
            update={
                "local_qwen_api_key": SecretStr("test-key"),
                "local_qwen_base": "http://127.0.0.1:8001",
            }
        )
        try:
            dispatched = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": exec_lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
        finally:
            app.state.settings = old

        assert dispatched.status_code == 200, dispatched.text
        data = dispatched.json()["data"]
        assert data["tool_calls"] is not None
        assert data["tool_calls"][0]["name"] == "read_file"
        assert "src/a.ts" in data["tool_calls"][0]["arguments"]
        kwargs = mocked.call_args.kwargs
        assert kwargs.get("tools")
        assert kwargs["tools"][0]["function"]["name"] == "read_file"


def test_execute_runner_owned_dispatch_skips_control_chat(api, objects):
    """第三百五十五批：runner_owned_completion 只迁 DISPATCHED，不代调模型；≠ DONE。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    client, _token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    activity_id = exec_lease["activity"]["id"]
    from control_kernel.storage.claims import binding_digest_of as bdigest

    context_content = {
        "project_id": goal["project_id"],
        "goal_id": goal["id"],
        "activity_id": activity_id,
        "role": "EXECUTOR",
        "goal_contract_revision": None,
        "task_contract_revision": None,
        "plan_revision": None,
        "input_bindings": [],
        "excluded_refs": [],
        "session_generation": 0,
        "compaction_source_digest": None,
        "planning_feedback_ids": [],
    }
    bundle = client.post(
        f"/internal/v1/activities/{activity_id}/context-bundles",
        json={"lease": exec_lease["lease"], "content": context_content},
        headers=worker_auth,
    )
    assert bundle.status_code == 201, bundle.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": exec_lease["lease"],
            "binding_digest": bdigest(exec_lease["activity"]["binding"]),
            "context_bundle_id": bundle.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"messages": [{"role": "user", "content": "runner owned"}]},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()

    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": exec_lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 64,
            "max_cost_usd": "0",
            "data_categories": [],
            "exposed_tools": ["read_file"],
        },
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    invocation = created.json()["data"]

    with patch("control_api.routes.probe.chat_completion") as mocked:
        mocked.side_effect = AssertionError("runner_owned 不得代调 chat_completion")
        dispatched = client.post(
            f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
            json={
                "lease": exec_lease["lease"],
                "expected_state_revision": invocation["state_revision"],
                "runner_owned_completion": True,
            },
            headers=worker_auth,
        )
        assert dispatched.status_code == 200, dispatched.text
        assert mocked.call_count == 0

    data = dispatched.json()["data"]
    assert data["status"] == "DISPATCHED"
    assert data.get("assistant_text") in (None, "")
    assert data.get("tool_calls") in (None, [])

    receipt = client.post(
        f"/internal/v1/model-invocations/{invocation['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "invocation_id": invocation["id"],
            "producer_attempt_id": exec_lease["lease"]["attempt_id"],
            "observed_result": "SUCCEEDED",
            "usage_status": "CONFIRMED",
            "input_tokens": 11,
            "output_tokens": 7,
            "cost_usd": None,
            "result_artifact_id": None,
            "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "APPLIED"

    # 已 SUCCEEDED：再次 dispatch 须拒绝（证明账本已终态）
    again = client.post(
        f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "expected_state_revision": data["state_revision"],
            "runner_owned_completion": True,
        },
        headers=worker_auth,
    )
    assert again.status_code == 409, again.text
