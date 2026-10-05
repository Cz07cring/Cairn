"""云闸门负向：策略拒绝时不得进入 DISPATCHED，且不调用连接器。"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import patch
from uuid import uuid4

from control_api.settings import Settings
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from test_goals import _ready_project


def test_deny_remote_base_keeps_authorized_and_skips_connector(api, objects):
    client, token = api
    app = client.app
    old: Settings = app.state.settings
    app.state.settings = old.model_copy(
        update={
            "local_qwen_base": "https://api.deepseek.com",
            "cloud_mode": "DENY",
        }
    )
    try:
        _client, _tok, auth, _project, goal_body = _ready_project(api, objects)
        created = client.post(
            "/api/v1/goals",
            json=goal_body,
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        goal = created.json()["data"]
        started = client.post(
            f"/api/v1/goals/{goal['id']}/start",
            json={"expected_state_revision": 1, "reason": "plan"},
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )
        assert started.status_code == 202, started.text

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
        from control_kernel.storage.claims import binding_digest_of

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

        # Harness 服务名应被解析为 local_provider_ref 后落库
        created_inv = client.post(
            "/internal/v1/model-invocations",
            json={
                "lease": lease["lease"],
                "invocation_seq": 1,
                "context_digest": context_digest,
                "input_digest": input_digest,
                "provider_ref": "ring-kernel",
                "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
                "max_output_tokens": 32,
                "max_cost_usd": "0",
                "data_categories": [],
            },
            headers=worker_auth,
        )
        assert created_inv.status_code == 201, created_inv.text
        invocation = created_inv.json()["data"]
        assert invocation["status"] == "AUTHORIZED"
        assert invocation["provider_ref"] == "pm2:omlx-flashnext"

        with patch("control_api.routes.probe.chat_completion") as mocked:
            denied = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "CLOUD_MODE_DENIED"
            mocked.assert_not_called()

        engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
        with engine.connect() as db:
            status = db.execute(
                text("SELECT status FROM model_invocations WHERE id=:id"),
                {"id": invocation["id"]},
            ).scalar_one()
        engine.dispose()
        assert status == "AUTHORIZED"
    finally:
        app.state.settings = old


def test_model_mismatch_rejected_on_create(api, objects):
    client, token = api
    _client, _tok, auth, _project, goal_body = _ready_project(api, objects)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
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
    from control_kernel.storage.claims import binding_digest_of

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
    prompt = b'{"messages":[{"role":"user","content":"x"}]}'
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    bad = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "not-the-frozen-model",
            "max_output_tokens": 32,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert bad.status_code == 422, bad.text
    assert "model_id" in bad.json()["error"]["message"]


def test_stale_authorized_wrong_model_cannot_dispatch(api, objects):
    """历史错绑 AUTHORIZED 行在 dispatch 前被冻结 Profile 拒绝，不得 DISPATCHED。"""
    client, token = api
    _client, _tok, auth, _project, goal_body = _ready_project(api, objects)
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    lease = claimed.json()["data"]
    activity_id = lease["activity"]["id"]
    attempt_id = lease["lease"]["attempt_id"]
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
    from control_kernel.storage.claims import binding_digest_of

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
    prompt = b'{"messages":[{"role":"user","content":"stale"}]}'
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    inv_id = uuid4()
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """INSERT INTO model_invocations(
                  id,project_id,goal_id,activity_id,producer_attempt_id,invocation_seq,
                  payload_digest,binding_digest,context_digest,input_digest,provider_ref,model_id,
                  max_output_tokens,max_cost_usd,data_categories,status,state_revision,
                  reservation_id,usage_status)
                VALUES(
                  :id,:project,:goal,:activity,:attempt,1,
                  :payload,:binding,:context,:input,'pm2:omlx-flashnext','wrong-stale-model',
                  32,'0','{}','AUTHORIZED',1,:reservation,'UNKNOWN')"""
            ),
            {
                "id": inv_id,
                "project": goal["project_id"],
                "goal": goal["id"],
                "activity": activity_id,
                "attempt": attempt_id,
                "payload": "sha256:" + "a" * 64,
                "binding": binding_digest_of(lease["activity"]["binding"]),
                "context": context_digest,
                "input": input_digest,
                "reservation": uuid4(),
            },
        )
    with patch("control_api.routes.probe.chat_completion") as mocked:
        denied = client.post(
            f"/internal/v1/model-invocations/{inv_id}/dispatch",
            json={
                "lease": lease["lease"],
                "expected_state_revision": 1,
            },
            headers=worker_auth,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "CLOUD_MODE_DENIED"
        mocked.assert_not_called()
    with engine.connect() as db:
        status = db.execute(
            text("SELECT status FROM model_invocations WHERE id=:id"),
            {"id": inv_id},
        ).scalar_one()
    engine.dispose()
    assert status == "AUTHORIZED"
