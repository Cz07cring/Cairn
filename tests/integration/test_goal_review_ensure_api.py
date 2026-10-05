"""POST /internal/v1/goals/{id}/goal-reviews：trigger_key 去重 + 复盘预算门禁（≠ DONE）。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from test_claims import _start_goal


def test_internal_ensure_goal_review_dedupes_by_trigger_key(api, objects):
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    first = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:tick-1", "review_seq": 1},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["created"] is True
    assert data["marks_goal_done"] is False
    assert data["reviews_remaining"] is not None
    assert data["max_reviews"] is not None and data["max_reviews"] >= 1
    assert data["min_interval_seconds"] is not None and data["min_interval_seconds"] >= 1
    assert data["stagnation_seconds"] is not None
    assert data["stagnation_seconds"] >= data["min_interval_seconds"]
    assert data["review_snapshot_digest"].startswith("sha256:")
    activity_id = data["activity_id"]

    second = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:tick-1", "review_seq": 1},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert second.status_code == 201, second.text
    again = second.json()["data"]
    assert again["created"] is False
    assert again["activity_id"] == activity_id
    assert again["review_snapshot_digest"] == data["review_snapshot_digest"]
    assert again["marks_goal_done"] is False
    assert again["max_reviews"] == data["max_reviews"]
    assert again["min_interval_seconds"] == data["min_interval_seconds"]
    assert again["stagnation_seconds"] == data["stagnation_seconds"]

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"


def test_ensure_goal_review_rejects_when_max_plan_revisions_exhausted(api, objects):
    """max_plan_revisions=1：第二条不同 trigger 失败关闭；同键去重仍成功。"""
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal["id"]},
            )
            .mappings()
            .one()
        )
        contract = dict(row["contract"])
        contract["retry_policy"] = {
            **contract["retry_policy"],
            "max_plan_revisions": 1,
        }
        contract["budget"] = {**contract["budget"], "wall_clock_seconds": 20}
        db.execute(
            text("UPDATE goals SET contract=CAST(:c AS jsonb) WHERE id=:id"),
            {"id": goal["id"], "c": json.dumps(contract)},
        )

    first = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:a", "review_seq": 1},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["created"] is True
    assert first.json()["data"]["reviews_remaining"] == 0

    blocked = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:b", "review_seq": 2},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "GOAL_REVIEW_BUDGET_EXHAUSTED"
    assert "上限" in blocked.json()["error"]["message"]

    # 同键去重不受上限二次扣减
    again = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:a", "review_seq": 1},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["created"] is False

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"


def test_ensure_goal_review_rejects_short_interval(api, objects):
    """小墙钟 → 短最小间隔；未满间隔拒绝新建。"""
    client, _token, auth, goal, _plan = _start_goal(api, objects)
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal["id"]},
            )
            .mappings()
            .one()
        )
        contract = dict(row["contract"])
        # wall=20, max_rev=2 → min_interval = max(1, 20//4)=5
        contract["retry_policy"] = {
            **contract["retry_policy"],
            "max_plan_revisions": 2,
        }
        contract["budget"] = {**contract["budget"], "wall_clock_seconds": 20}
        db.execute(
            text("UPDATE goals SET contract=CAST(:c AS jsonb) WHERE id=:id"),
            {"id": goal["id"], "c": json.dumps(contract)},
        )

    first = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:t0"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert first.status_code == 201, first.text
    sched = first.json()["data"]
    assert sched["max_reviews"] == 2
    assert sched["min_interval_seconds"] == 5
    assert sched["stagnation_seconds"] == 10

    too_soon = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:t1"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert too_soon.status_code == 422, too_soon.text
    assert too_soon.json()["error"]["code"] == "GOAL_REVIEW_INTERVAL_TOO_SHORT"
    assert "间隔" in too_soon.json()["error"]["message"]

    # 拨回时间后允许下一条
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET created_at=:ts
                WHERE goal_id=:goal AND kind='AUDIT' AND target_type='GOAL_REVIEW'"""
            ),
            {
                "goal": UUID(goal["id"]),
                "ts": datetime.now(UTC) - timedelta(seconds=10),
            },
        )

    second = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:t1"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["created"] is True
    assert second.json()["data"]["marks_goal_done"] is False


def test_ensure_goal_review_rejects_when_engineering_not_stagnant(api, objects):
    """RUNNING EXECUTE 视为未停滞：拒绝复盘；拨回进展时间后允许。"""
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, auth, goal, _worker_auth, _exec = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal["id"]},
            )
            .mappings()
            .one()
        )
        raw = row["contract"]
        contract = dict(raw) if isinstance(raw, dict) else json.loads(raw)
        # wall=20 → min_interval=5, stagnation=10
        contract["retry_policy"] = {
            **contract["retry_policy"],
            "max_plan_revisions": 2,
        }
        contract["budget"] = {**contract["budget"], "wall_clock_seconds": 20}
        db.execute(
            text("UPDATE goals SET contract=CAST(:c AS jsonb) WHERE id=:id"),
            {"id": goal["id"], "c": json.dumps(contract)},
        )
        # 确认合同已写入
        wall = db.execute(
            text(
                """SELECT (contract->'budget'->>'wall_clock_seconds')::int
                FROM goals WHERE id=:id"""
            ),
            {"id": goal["id"]},
        ).scalar_one()
        assert wall == 20

    blocked = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:stuck-check"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "GOAL_REVIEW_NOT_STAGNANT"
    assert "停滞" in blocked.json()["error"]["message"]
    assert "10" in blocked.json()["error"]["message"]

    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET updated_at=:ts, created_at=:ts
                WHERE goal_id=:goal
                  AND kind IN ('PLAN','EXECUTE','INTEGRATE','AUDIT')
                  AND (target_type IS NULL OR target_type <> 'GOAL_REVIEW')
                  AND status IN ('RUNNING','SUCCEEDED')"""
            ),
            {
                "goal": UUID(goal["id"]),
                "ts": datetime.now(UTC) - timedelta(seconds=30),
            },
        )

    ok = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"wf:{goal['id']}:stuck-check"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["data"]["created"] is True
    assert ok.json()["data"]["marks_goal_done"] is False


def test_request_goal_review_returns_structured_reject(api, objects):
    """编排入口：拒绝时 ok=False + reason_code，不抛异常；≠ DONE。"""
    from control_kernel import request_goal_review
    from test_effects import _publish_and_claim_execute

    _store, _, _ = objects
    _client, _token, _auth, goal, _worker_auth, _exec = _publish_and_claim_execute(
        api, objects
    )
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    rejected = request_goal_review(
        engine,
        UUID(goal["id"]),
        trigger_key=f"orch:{goal['id']}:early",
        review_seq=1,
    )
    assert rejected["ok"] is False
    assert rejected["reason_code"] == "GOAL_REVIEW_NOT_STAGNANT"
    assert rejected["marks_goal_done"] is False
    assert "message" in rejected


def test_http_goal_review_budget_snapshot(api, objects):
    """GET /goals/{id}/goal-review-budget：只读投影；marks_goal_done 恒 false。"""
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, auth, goal, _worker_auth, _exec = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        row = (
            db.execute(
                text("SELECT contract FROM goals WHERE id=:id FOR UPDATE"),
                {"id": goal["id"]},
            )
            .mappings()
            .one()
        )
        raw = row["contract"]
        contract = dict(raw) if isinstance(raw, dict) else json.loads(raw)
        contract["retry_policy"] = {
            **contract["retry_policy"],
            "max_plan_revisions": 2,
        }
        contract["budget"] = {**contract["budget"], "wall_clock_seconds": 20}
        db.execute(
            text("UPDATE goals SET contract=CAST(:c AS jsonb) WHERE id=:id"),
            {"id": goal["id"], "c": json.dumps(contract)},
        )

    res = client.get(
        f"/api/v1/goals/{goal['id']}/goal-review-budget",
        headers=auth,
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["marks_goal_done"] is False
    assert data["max_reviews"] == 2
    assert data["min_interval_seconds"] == 5
    assert data["stagnation_seconds"] == 10
    assert data["reviews_used"] == 0
    assert data["reviews_remaining"] == 2
    assert data["eligible_now"] is False
    assert data["blocking_reason_code"] == "GOAL_REVIEW_NOT_STAGNANT"
