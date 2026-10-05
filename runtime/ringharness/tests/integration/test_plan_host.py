"""Fake PLAN 宿主路径：claim→context→ModelInvocation→PlanCreate；零工具。"""

import hashlib
import json
from uuid import uuid4

from control_kernel.storage.claims import binding_digest_of
from test_claims import _register_worker, _start_goal
from test_plans import _plan_body


def test_plan_host_path_with_model_invocation_then_outcome(api, objects):
    client, token, auth, goal, _plan_activity = _start_goal(api, objects)
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
    assert lease["activity"]["kind"] == "PLAN"
    activity_id = lease["activity"]["id"]

    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    bundle_id = compiled.json()["data"]["id"]
    assert compiled.json()["data"]["content"]["role"] == "PLANNER"
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": bundle_id,
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]

    # 宿主模型回合：工具集为空；仅登记调用，不经 steps/effects。
    prompt = json.dumps(
        {"role": "PLANNER", "tools": [], "instruction": "emit PlanCreate only"},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    # 冻结 Profile 即权威（test_goals._ready_project 装配）：调用方不得自报 provider/model。
    # provider_ref 用 Profile 的 local_provider_ref，model_id 必须与 Profile.model_id 一致，
    # 否则 create 会被 assert_provider_model_matches_profile 拒绝（422）。
    provider_ref = "pm2:omlx-flashnext"
    model_id = "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx"
    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": provider_ref,
            "model_id": model_id,
            "max_output_tokens": 256,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert created.status_code == 201, created.text
    invocation = created.json()["data"]
    assert invocation["status"] == "AUTHORIZED"

    listed = client.get(
        "/api/v1/model-invocations",
        params={
            "project_id": goal["project_id"],
            "goal_id": goal["id"],
            "activity_id": activity_id,
        },
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert any(row["id"] == invocation["id"] for row in rows)
    assert all("input_digest" in row and "messages" not in row for row in rows)

    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    done = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": _plan_body(goal, profile_id)},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "SUCCEEDED"
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert got["status"] == "RUNNING"
    assert got["plan_revision"] == 1
