"""Effect 审批绑定、裁决、消费与列表。"""

import hashlib
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from test_claims import _drain_ready_plans
from test_effects import _publish_and_claim_execute


def test_effect_approval_bind_decide_dispatch(api, objects):
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    project_id = goal["project_id"]

    # 新策略版本：read_file 需人工审批
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": project_id,
            "name": "approval-required",
            "allowed_tools": ["read_file"],
            "allowed_paths": ["src/**"],
            "protected_paths": [],
            "network_allowlist": [],
            "external_actions": [{"action": "read_file", "mode": "APPROVAL"}],
            "secret_scope_refs": [],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert policy.status_code == 201, policy.text
    # Goal 合同绑定固定 policy_id；测试将合同切到需审批策略版本。
    import os

    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET contract = jsonb_set(
                  contract, '{policy_id}', to_jsonb(CAST(:policy AS text)), false)
                WHERE id=:id"""
            ),
            {"policy": str(policy.json()["data"]["id"]), "id": goal["id"]},
        )
    engine.dispose()

    activity_id = exec_lease["activity"]["id"]
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]

    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        UUID(project_id),
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity=f"test:{exec_lease['attempt']['worker_id']}",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["approval_id"] is not None
    assert effect["status"] == "PREPARED"

    listed = client.get(
        "/api/v1/approvals",
        params={"project_id": project_id, "status": "PENDING"},
        headers=auth,
    )
    assert listed.status_code == 200, listed.text
    approvals = listed.json()["data"]
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["id"] == effect["approval_id"]
    assert approval["subject"] == {"type": "EFFECT", "id": effect["id"]}

    # 未裁决不可 dispatch
    denied = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert denied.status_code == 422, denied.text

    decided = client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={
            "expected_state_revision": approval["state_revision"],
            "decision": "APPROVE",
            "subject": approval["subject"],
            "payload_digest": approval["payload_digest"],
            "reason": "允许只读探测",
        },
        headers=auth,
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "APPROVED"

    effect = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert effect["status"] == "AUTHORIZED"

    authorized_snap = client.get(f"/api/v1/goals/{goal['id']}/snapshot", headers=auth)
    assert authorized_snap.status_code == 200, authorized_snap.text
    authorized_data = authorized_snap.json()["data"]
    assert [item["id"] for item in authorized_data["effects"]] == [effect["id"]]
    assert [item["id"] for item in authorized_data["approvals"]] == [approval["id"]]

    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["data"]["status"] == "DISPATCHED"

    dispatched_snap = client.get(f"/api/v1/goals/{goal['id']}/snapshot", headers=auth)
    assert dispatched_snap.status_code == 200, dispatched_snap.text
    dispatched_data = dispatched_snap.json()["data"]
    assert [item["id"] for item in dispatched_data["effects"]] == [effect["id"]]
    assert dispatched_data["approvals"] == []

    # 撤销已消费审批仍可标记，但不抹除已 DISPATCHED
    rev = client.post(
        f"/api/v1/approvals/{approval['id']}/revoke",
        json={
            "expected_state_revision": decided.json()["data"]["state_revision"],
            "reason": "事后收回",
        },
        headers=auth,
    )
    assert rev.status_code == 200, rev.text
    assert rev.json()["data"]["status"] == "REVOKED"
    still = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert still["status"] == "DISPATCHED"

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": got["state_revision"], "reason": "cleanup"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)
