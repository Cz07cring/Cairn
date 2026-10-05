"""PLANNER context-compile 绑定 GoalReview 事实（EVIDENCE）；≠ PlanningFeedback。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from control_kernel.storage.audits import ensure_goal_review_activity
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker, _start_goal
from test_effects import _publish_and_claim_execute


def _submit_goal_review(client, token, *, goal: dict, trigger_key: str, findings: list):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET created_at=created_at - interval '1 day',
                    updated_at=updated_at - interval '1 day'
                WHERE goal_id=:goal AND (
                  (kind='AUDIT' AND target_type='GOAL_REVIEW')
                  OR (kind IN ('PLAN','EXECUTE','INTEGRATE','AUDIT')
                      AND (target_type IS NULL OR target_type <> 'GOAL_REVIEW')
                      AND status IN ('RUNNING','SUCCEEDED'))
                )"""
            ),
            {"goal": goal["id"]},
        )
    activity_id, snap, _created, _rem, *_ = ensure_goal_review_activity(
        engine,
        goal_id=UUID(goal["id"]),
        trigger_key=trigger_key,
    )
    _drain_ready(client, token, kinds=("AUDIT",))
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": activity_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='AUDIT' AND id<>:id"""
            ),
            {"id": activity_id},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    binding = lease["activity"]["binding"]
    outcome = client.post(
        f"/internal/v1/activities/{activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "GOAL_REVIEW",
                "review": {
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "plan_revision": binding["plan_revision"],
                    "review_snapshot_digest": snap,
                    "findings": findings,
                },
            },
        },
        headers=worker_auth,
    )
    assert outcome.status_code == 200, outcome.text
    return snap


def test_planner_context_compile_binds_goal_review_as_evidence_not_feedback(
    api, objects
):
    client, token, _auth, goal, _plan = _start_goal(api, objects)
    _submit_goal_review(
        client,
        token,
        goal=goal,
        trigger_key=f"compile:{goal['id']}",
        findings=[
            {
                "code": "NO_PROGRESS",
                "severity": "WARN",
                "evidence_ids": [],
                "recommendation": "检查停滞",
            }
        ],
    )

    # 新 PLAN Activity：避免已 claim 的旧 PLAN；直接 ensure 后 claim 新 PLAN 较难，
    # 恢复本 Goal 的 PLAN 为 READY 再 claim。
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        plan_id = db.execute(
            text(
                """SELECT id FROM activities
                WHERE goal_id=:g AND kind='PLAN'
                ORDER BY created_at LIMIT 1"""
            ),
            {"g": goal["id"]},
        ).scalar_one()
        db.execute(
            text(
                """UPDATE activity_attempts SET status='CANCELLED',
                  finished_at=clock_timestamp(), updated_at=clock_timestamp()
                WHERE activity_id=:id AND status='ACTIVE'"""
            ),
            {"id": plan_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', current_attempt_id=NULL,
                  state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": plan_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='PLAN' AND id<>:id"""
            ),
            {"id": plan_id},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == str(plan_id)

    compiled = client.post(
        f"/internal/v1/activities/{plan_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    content = compiled.json()["data"]["content"]
    assert content["role"] == "PLANNER"
    assert content["planning_feedback_ids"] == []
    evidence = [b for b in content["input_bindings"] if b["classification"] == "EVIDENCE"]
    assert len(evidence) >= 1
    assert all(b["digest"].startswith("sha256:") for b in evidence)
    # 不得把 GoalReview 塞进反馈 uuid 列表
    assert all(not str(x).startswith("sha256:") for x in content["planning_feedback_ids"])


def test_executor_context_compile_excludes_goal_review_evidence(api, objects):
    store, _, _ = objects
    client, token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    _submit_goal_review(
        client,
        token,
        goal=goal,
        trigger_key=f"exec-compile:{goal['id']}",
        findings=[
            {
                "code": "HINT",
                "severity": "INFO",
                "evidence_ids": [],
                "recommendation": "仅 Planner 可见",
            }
        ],
    )

    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": exec_lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    content = compiled.json()["data"]["content"]
    assert content["role"] == "EXECUTOR"
    evidence = [b for b in content["input_bindings"] if b["classification"] == "EVIDENCE"]
    assert evidence == []
    assert content["planning_feedback_ids"] == []
