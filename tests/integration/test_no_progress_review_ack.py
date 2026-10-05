"""第三百五十批：operator 无进展复盘确认；解除 SEAL/写入闸；≠ DONE。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from control_kernel.storage.activation_terminations import (
    goal_has_unresolved_no_progress_stop,
    record_activation_termination,
)
from control_kernel.storage.stops import assert_goal_allows_new_engineering_writes
from sqlalchemy import create_engine, text
from test_effects import _publish_and_claim_execute


def test_operator_no_progress_review_ack_clears_engineering_gate(api, objects):
    """NO_PROGRESS → 挡写；operator ack → GOAL_REQUIRES_REVIEW → 可写；≠DONE。"""
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    goal_id = UUID(goal["id"])
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    with engine.connect() as db:
        subject = db.execute(
            text(
                """SELECT w.subject FROM workers w
                JOIN activity_attempts a ON a.worker_id=w.id
                WHERE a.id=:id"""
            ),
            {"id": attempt_id},
        ).scalar_one()

    record_activation_termination(
        engine,
        subject=subject,
        project_ids=[],
        activity_id=activity_id,
        attempt_id=attempt_id,
        reason="NO_PROGRESS_STOP",
        detail="need_operator_ack",
    )

    with engine.connect() as db:
        assert goal_has_unresolved_no_progress_stop(db, goal_id) is True

    step_blocked = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "before-ack",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step_blocked.status_code == 422, step_blocked.text
    assert step_blocked.json()["error"]["code"] == "NO_PROGRESS_STOP_UNRESOLVED"

    goal_before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert goal_before["status"] != "DONE"
    rev = goal_before["state_revision"]

    # viewer 禁止
    viewer_auth = {"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])}
    deny = client.post(
        f"/api/v1/goals/{goal_id}/no-progress-review-ack",
        json={"expected_state_revision": rev, "detail": "viewer"},
        headers=viewer_auth,
    )
    assert deny.status_code == 403, deny.text

    ack = client.post(
        f"/api/v1/goals/{goal_id}/no-progress-review-ack",
        json={
            "expected_state_revision": rev,
            "detail": "reviewed_no_progress",
        },
        headers=auth,
    )
    assert ack.status_code == 201, ack.text
    data = ack.json()["data"]
    assert data["reason"] == "GOAL_REQUIRES_REVIEW"
    assert data["marks_goal_done"] is False
    assert "acked_by=" in (data.get("detail") or "")

    # 幂等
    again = client.post(
        f"/api/v1/goals/{goal_id}/no-progress-review-ack",
        json={"expected_state_revision": rev},
        headers=auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == data["id"]

    with engine.connect() as db:
        assert goal_has_unresolved_no_progress_stop(db, goal_id) is False
        assert_goal_allows_new_engineering_writes(db, goal_id)

    step_ok = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "after-ack",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step_ok.status_code == 201, step_ok.text

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["status"] != "DONE"
    engine.dispose()


def test_acknowledge_absent_no_progress_rejected(api, objects):
    client, _token, auth, goal, _worker_auth, _exec = _publish_and_claim_execute(
        api, objects
    )
    goal_id = goal["id"]
    rev = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"][
        "state_revision"
    ]
    missing = client.post(
        f"/api/v1/goals/{goal_id}/no-progress-review-ack",
        json={"expected_state_revision": rev},
        headers=auth,
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "NO_PROGRESS_STOP_ABSENT"
    assert (
        client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]["status"]
        != "DONE"
    )
