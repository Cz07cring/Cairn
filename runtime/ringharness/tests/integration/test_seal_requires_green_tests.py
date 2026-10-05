"""write_file 成功后 seal_candidate 须有绿测回执（SEAL_REQUIRES_GREEN_TESTS）。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.domain.tool_capability_manifest import (
    RUN_TESTS_SCHEMA_DIGEST,
    SEAL_CANDIDATE_SCHEMA_DIGEST,
    WRITE_FILE_SCHEMA_DIGEST,
)
from control_kernel.storage.artifacts import Artifacts
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body


def _complete_effect(client, worker_auth, exec_lease, effect, *, exit_code: int) -> None:
    activity_id = exec_lease["activity"]["id"]
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    effect = dispatched.json()["data"]
    outcome = "SUCCEEDED" if exit_code == 0 else "FAILED"
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": exec_lease["lease"]["attempt_id"],
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "exit_code": exit_code,
            "timed_out": False,
            "result_artifact_ids": [effect["input_artifact_id"]],
            "observed_outcome": outcome,
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text


def _prepare(
    client,
    store,
    worker_auth,
    exec_lease,
    *,
    tool_ref: str,
    schema_digest: str,
    parameters: dict,
    predecessor_step_id: str | None,
):
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": predecessor_step_id,
            "purpose": f"tool:{tool_ref}",
            "tool_ref": tool_ref,
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    blob = json.dumps(
        {
            "tool_ref": tool_ref,
            "tool_schema_digest": schema_digest,
            "parameters": parameters,
        },
        separators=(",", ":"),
    ).encode()
    art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(blob).hexdigest(),
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:seal-green:{tool_ref}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": tool_ref,
            "input_artifact_id": str(art.id),
        },
        headers=worker_auth,
    )
    return step_data, prepared


def _publish_execute_with_tools(api, objects, tools: list[str]):
    """对齐 test_effects._publish_and_claim_execute，但策略含 seal 相关工具。"""
    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": f"seal-green-{uuid4().hex[:8]}",
            "allowed_tools": tools,
            "allowed_paths": ["src/**", "order_service/**"],
            "protected_paths": [],
            "network_allowlist": [],
            "external_actions": [],
            "secret_scope_refs": [],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert policy.status_code == 201, policy.text
    goal_body = {**goal_body, "policy_id": policy.json()["data"]["id"]}
    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN"))
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
    plan_activity = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
              WHERE status='READY' AND kind='PLAN' AND id<>:id"""),
            {"id": plan_activity["id"]},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('CANCELLED','READY')"""
            ),
            {"id": plan_activity["id"]},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == plan_activity["id"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
    plan["tasks"][0]["contract"]["allowed_paths"] = ["src/**", "order_service/**"]
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    engine.dispose()

    exec_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert exec_claim.status_code == 200, exec_claim.text
    exec_lease = exec_claim.json()["data"]
    assert exec_lease["lease"] is not None
    assert exec_lease["activity"]["kind"] == "EXECUTE"
    assert exec_lease["activity"]["goal_id"] == goal["id"]
    return client, token, auth, goal, worker_auth, exec_lease, profile_id


def test_seal_prepare_blocked_until_green_after_write(api, objects):
    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease, profile_id = (
        _publish_execute_with_tools(
            api,
            objects,
            ["read_file", "write_file", "run_tests", "seal_candidate"],
        )
    )
    client.app.state.objects = store

    write_step, write_prep = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="write_file",
        schema_digest=WRITE_FILE_SCHEMA_DIGEST,
        parameters={"path": "src/main.py", "content": "x=1\n"},
        predecessor_step_id=None,
    )
    assert write_prep.status_code == 201, write_prep.text
    _complete_effect(
        client, worker_auth, exec_lease, write_prep.json()["data"], exit_code=0
    )

    _seal_step, seal_blocked = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="seal_candidate",
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        parameters={"verification_profile_ids": [profile_id]},
        predecessor_step_id=write_step["id"],
    )
    assert seal_blocked.status_code == 422, seal_blocked.text
    assert seal_blocked.json()["error"]["code"] == "SEAL_REQUIRES_GREEN_TESTS"

    red_step, red_prep = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="run_tests",
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        parameters={"suite": "public"},
        predecessor_step_id=write_step["id"],
    )
    assert red_prep.status_code == 201, red_prep.text
    _complete_effect(
        client, worker_auth, exec_lease, red_prep.json()["data"], exit_code=1
    )

    _s2, seal_still = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="seal_candidate",
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        parameters={"verification_profile_ids": [profile_id]},
        predecessor_step_id=red_step["id"],
    )
    assert seal_still.status_code == 422, seal_still.text
    assert seal_still.json()["error"]["code"] == "SEAL_REQUIRES_GREEN_TESTS"

    green_step, green_prep = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="run_tests",
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        parameters={"suite": "public"},
        predecessor_step_id=red_step["id"],
    )
    assert green_prep.status_code == 201, green_prep.text
    _complete_effect(
        client, worker_auth, exec_lease, green_prep.json()["data"], exit_code=0
    )

    _s3, seal_ok = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="seal_candidate",
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        parameters={"verification_profile_ids": [profile_id]},
        predecessor_step_id=green_step["id"],
    )
    assert seal_ok.status_code == 201, seal_ok.text
    assert seal_ok.json()["data"]["tool_ref"] == "seal_candidate"
    assert (
        client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]["status"]
        != "DONE"
    )


def test_seal_without_write_still_allowed(api, objects):
    """无 write_file 时不触发绿测闸（兼容纯封存路径）。"""
    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease, profile_id = (
        _publish_execute_with_tools(api, objects, ["read_file", "seal_candidate"])
    )
    client.app.state.objects = store
    _step, prepared = _prepare(
        client,
        store,
        worker_auth,
        exec_lease,
        tool_ref="seal_candidate",
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        parameters={"verification_profile_ids": [profile_id]},
        predecessor_step_id=None,
    )
    assert prepared.status_code == 201, prepared.text
    assert (
        client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]["status"]
        != "DONE"
    )
