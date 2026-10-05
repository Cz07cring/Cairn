"""第八十九批：Runner Effect 观察契约 — dispatch 后可读状态，不自动 SUCCEEDED。"""

import hashlib
from io import BytesIO
from uuid import UUID

from control_kernel.storage.artifacts import Artifacts
from test_effects import _publish_and_claim_execute


def test_runner_shaped_dispatch_then_get_stays_dispatched(api, objects):
    """对齐 apps/runner effectObserveHttpPorts：GET 不把 DISPATCHED 冒充终态。"""
    store, _, _ = objects
    client, _token, auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口文件",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]

    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    engine = client.app.state.engine
    project_id = UUID(exec_lease["activity"]["project_id"])
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
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
            "intent_revision": step_data["intent_revision"],
            "tool_ref": "read_file",
            "input_artifact_id": str(artifact.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]

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

    # Runner pollEffectStatus：仅 GET，不得假定已 SUCCEEDED
    got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert got.status_code == 200
    assert got.json()["data"]["status"] == "DISPATCHED"
    assert got.json()["data"]["id"] == effect["id"]
    assert got.json()["data"]["status"] != "SUCCEEDED"
