"""GoalReview BLOCKER 规则门：挡新 ENGINEERING / 挡屏障 SEAL；≠ Goal BLOCKED / ≠ DONE。"""

from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from control_kernel.storage.audits import (
    ensure_goal_review_activity,
    goal_has_blocking_review,
)
from control_kernel.storage.finalization import open_finalization_barrier
from sqlalchemy import create_engine, text
from test_ab09_stop_barrier import _ensure_global_criterion
from test_claims import _drain_ready, _register_worker
from test_effects import _publish_and_claim_execute


def _submit_review(
    client,
    token,
    *,
    goal: dict,
    trigger_key: str,
    findings: list[dict],
):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    # 测试夹具：拨回既有复盘与工程进展时间，避免间隔/停滞门挡住后继用例
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
    assert binding["subject_digest"] == snap
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


def test_blocker_review_blocks_new_engineering_prepare_not_goal_done(api, objects):
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"blocker:{goal['id']}",
        findings=[
            {
                "code": "STUCK_LOOP",
                "severity": "BLOCKER",
                "evidence_ids": [],
                "recommendation": "停止重复失败工具调用",
            }
        ],
    )

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        assert goal_has_blocking_review(db, UUID(goal["id"])) is True
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal["id"]},
        ).scalar_one()
        assert status != "DONE"
        assert status != "BLOCKED"
    engine.dispose()

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "被 BLOCKER 拒绝",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 422, step.text
    assert "GOAL_REVIEW_BLOCKER" in step.text

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"
    assert goal_after["status"] != "BLOCKED"


def test_warn_review_does_not_block_engineering(api, objects):
    store, _, _ = objects
    client, token, _auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"warn:{goal['id']}",
        findings=[
            {
                "code": "SLOW_PROGRESS",
                "severity": "WARN",
                "evidence_ids": [],
                "recommendation": "关注进度",
            }
        ],
    )

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "WARN 仍可写",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text


def test_blocker_keeps_finalization_barrier_draining(api, objects):
    """无在途/UNKNOWN 时 BLOCKER 仍使屏障 DRAINING，不建 FINALIZE（≠DONE）。"""
    store, _, _ = objects
    client, token, auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    task_id = UUID(exec_lease["activity"]["task_id"])
    candidate_id = uuid4()
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    _ensure_global_criterion(client, auth, engine, str(project_id), str(goal_id))

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"barrier-blocker:{goal['id']}",
        findings=[
            {
                "code": "BASELINE_DRIFT",
                "severity": "BLOCKER",
                "evidence_ids": [],
                "recommendation": "先复盘再开最终屏障",
            }
        ],
    )

    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(b"blocker-barrier").hexdigest(),
        BytesIO(b"blocker-barrier"),
        mime="application/octet-stream",
        producer_identity="test:blocker-barrier",
    )

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='RUNNING', updated_at=clock_timestamp()
                WHERE id=:id AND status NOT IN ('DONE','FAILED','CANCELLED','BLOCKED')"""
            ),
            {"id": goal_id},
        )
        db.execute(
            text(
                """INSERT INTO candidate_manifests(
                  id,project_id,goal_id,task_id,activity_id,attempt_id,
                  protected_baseline_digest,content_digest,content,
                  workspace_snapshot_artifact_id)
                VALUES(
                  :id,:project,:goal,:task,:activity,:attempt,
                  :baseline,:digest,CAST(:content AS jsonb),:snap)"""
            ),
            {
                "id": candidate_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "activity": activity_id,
                "attempt": attempt_id,
                "baseline": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        assert goal_has_blocking_review(db, goal_id) is True
        open_finalization_barrier(db, goal_id, candidate_id)

        barrier = (
            db.execute(
                text(
                    """SELECT status FROM finalization_barriers WHERE goal_id=:goal"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .first()
        )
        assert barrier is not None, "应已创建屏障（Goal 含 GLOBAL 准则）"
        assert barrier["status"] == "DRAINING"
        finalize_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind='FINALIZE'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        assert int(finalize_n) == 0
        goal_row = (
            db.execute(
                text("SELECT status FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert goal_row["status"] != "DONE"
        assert goal_row["status"] != "BLOCKED"

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] != "DONE"
    engine.dispose()


def test_clearing_blocker_promotes_draining_barrier_to_sealed(api, objects):
    """BLOCKER 卡住 DRAINING；后继无 BLOCKER 复盘 → try_seal SEALED+FINALIZE；≠DONE。"""
    store, _, _ = objects
    client, token, auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])
    task_id = UUID(exec_lease["activity"]["task_id"])
    candidate_id = uuid4()
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])

    _ensure_global_criterion(client, auth, engine, str(project_id), str(goal_id))

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"seal-blocker:{goal['id']}:1",
        findings=[
            {
                "code": "BASELINE_DRIFT",
                "severity": "BLOCKER",
                "evidence_ids": [],
                "recommendation": "挡住最终屏障",
            }
        ],
    )

    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(b"blocker-then-seal").hexdigest(),
        BytesIO(b"blocker-then-seal"),
        mime="application/octet-stream",
        producer_identity="test:blocker-seal",
    )

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='RUNNING', updated_at=clock_timestamp()
                WHERE id=:id AND status NOT IN ('DONE','FAILED','CANCELLED','BLOCKED')"""
            ),
            {"id": goal_id},
        )
        db.execute(
            text(
                """INSERT INTO candidate_manifests(
                  id,project_id,goal_id,task_id,activity_id,attempt_id,
                  protected_baseline_digest,content_digest,content,
                  workspace_snapshot_artifact_id)
                VALUES(
                  :id,:project,:goal,:task,:activity,:attempt,
                  :baseline,:digest,CAST(:content AS jsonb),:snap)"""
            ),
            {
                "id": candidate_id,
                "project": project_id,
                "goal": goal_id,
                "task": task_id,
                "activity": activity_id,
                "attempt": attempt_id,
                "baseline": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "digest": "sha256:" + uuid4().hex + uuid4().hex[:32],
                "content": json.dumps({"files": []}),
                "snap": artifact.id,
            },
        )
        assert goal_has_blocking_review(db, goal_id) is True
        open_finalization_barrier(db, goal_id, candidate_id)
        barrier = (
            db.execute(
                text("SELECT status FROM finalization_barriers WHERE goal_id=:goal"),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        assert barrier["status"] == "DRAINING"

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"seal-blocker:{goal['id']}:2",
        findings=[
            {
                "code": "CLEARED",
                "severity": "INFO",
                "evidence_ids": [],
                "recommendation": "偏差已处理，可封存",
            }
        ],
    )

    with engine.connect() as db:
        assert goal_has_blocking_review(db, goal_id) is False
        barrier = (
            db.execute(
                text(
                    """SELECT status, in_flight_engineering, unknown_effects
                    FROM finalization_barriers WHERE goal_id=:goal"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        assert barrier["status"] == "SEALED"
        assert int(barrier["in_flight_engineering"]) == 0
        assert int(barrier["unknown_effects"]) == 0
        finalize_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND kind='FINALIZE' AND status='READY'"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        assert int(finalize_n) == 1
        goal_status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
        assert goal_status == "VERIFYING"
        assert goal_status != "DONE"

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "VERIFYING"
    assert got["status"] != "DONE"
    assert got["barrier"]["status"] == "SEALED"
    engine.dispose()


def test_successor_non_blocker_review_clears_engineering_gate(api, objects):
    """最新 GoalReview 无 BLOCKER 时解除门控；历史 BLOCKER 不永久锁死；≠ DONE。"""
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"blocker-then-clear:{goal['id']}:1",
        findings=[
            {
                "code": "STUCK_LOOP",
                "severity": "BLOCKER",
                "evidence_ids": [],
                "recommendation": "先停再复审",
            }
        ],
    )

    blocked = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "BLOCKER 期间应拒绝",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert blocked.status_code == 422, blocked.text
    assert "GOAL_REVIEW_BLOCKER" in blocked.text

    _submit_review(
        client,
        token,
        goal=goal,
        trigger_key=f"blocker-then-clear:{goal['id']}:2",
        findings=[
            {
                "code": "CLEARED",
                "severity": "INFO",
                "evidence_ids": [],
                "recommendation": "偏差已处理，可继续工程写入",
            }
        ],
    )

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        assert goal_has_blocking_review(db, UUID(goal["id"])) is False
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": goal["id"]},
        ).scalar_one()
        assert status != "DONE"
        assert status != "BLOCKED"
        n = db.execute(
            text("SELECT count(*) FROM goal_reviews WHERE goal_id=:g"),
            {"g": goal["id"]},
        ).scalar_one()
        assert int(n) == 2
    engine.dispose()

    allowed = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "后继无 BLOCKER 应允许",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert allowed.status_code == 201, allowed.text

    listed = client.get(f"/api/v1/goals/{goal['id']}/audits", headers=auth)
    assert listed.status_code == 200, listed.text
    reviews = [i for i in listed.json()["data"] if i["record_type"] == "GOAL_REVIEW"]
    assert len(reviews) >= 2
    # 联合游标 ORDER BY created_at ASC；最新在末尾
    latest = reviews[-1]["review"]
    assert latest["findings"][0]["code"] == "CLEARED"
    assert all(f["severity"] != "BLOCKER" for f in latest["findings"])

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"
    assert goal_after["status"] != "BLOCKED"
