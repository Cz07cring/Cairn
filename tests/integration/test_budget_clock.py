"""Issue #20：Goal 墙钟预算原子推进（elapsed_wall_seconds / active_seconds）。"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from control_kernel.protocols.runtime import LeaseRejected
from control_kernel.storage.budget_clock import (
    BudgetUsageUnknown,
    advance_goal_budget_clock,
    assert_goal_wall_budget_allows_engineering,
    goal_wall_budget_snapshot,
    init_budget_clock_on_start,
)
from sqlalchemy import create_engine, text
from test_claims import _start_goal


def _engine():
    return create_engine(os.environ["RING_TEST_DATABASE_URL"])


def test_draft_dwell_not_counted_then_start_zeros_clock(api, objects):
    """DRAFT 长时间停留不计入；START 后 elapsed/active 从 0 起。"""
    client, token, auth, goal, _plan = _start_goal(api, objects)
    # _start_goal 已 START；验证锚点已重置
    eng = _engine()
    with eng.begin() as db:
        usage = db.execute(
            text("SELECT budget_usage FROM goals WHERE id=:id"),
            {"id": goal["id"]},
        ).scalar_one()
    eng.dispose()
    assert usage["elapsed_wall_seconds"] == 0
    assert usage["active_seconds"] == 0
    assert client is not None and token and auth


def test_advance_monotonic_and_pause_splits_active(api, objects):
    """推进单调；PAUSED 时 elapsed 继续、active 停表；时钟回拨不回退。"""
    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    t0 = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
    eng = _engine()
    with eng.begin() as db:
        init_budget_clock_on_start(db, goal_id, now=t0)
        u1 = advance_goal_budget_clock(
            db, goal_id, now=t0 + timedelta(seconds=10), goal_status="RUNNING"
        )
        assert u1.elapsed_wall_seconds == 10
        assert u1.active_seconds == 10
        db.execute(
            text(
                """UPDATE goals SET status='PAUSED', previous_status='RUNNING',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": goal_id},
        )
        # 进入暂停态推进：elapsed+5，active 不变
        u2 = advance_goal_budget_clock(
            db, goal_id, now=t0 + timedelta(seconds=15)
        )
        assert u2.elapsed_wall_seconds == 15
        assert u2.active_seconds == 10
        # 时钟回拨：不回退
        u3 = advance_goal_budget_clock(
            db, goal_id, now=t0 + timedelta(seconds=12)
        )
        assert u3.elapsed_wall_seconds == 15
        assert u3.active_seconds == 10
        snap = goal_wall_budget_snapshot(db, goal_id, now=t0 + timedelta(seconds=20))
        assert snap["budget_usage_unknown"] is False
        assert snap["elapsed_wall_seconds"] == 20
        assert snap["active_seconds"] == 10  # 仍 PAUSED
    eng.dispose()


def test_invalid_usage_fail_closed(api, objects):
    """非法 budget_usage → UNKNOWN，不得当 0；admit 门拒绝。"""
    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET budget_usage=CAST(:usage AS jsonb)
                WHERE id=:id"""
            ),
            {"id": goal_id, "usage": '{"bad":true}'},
        )
        with pytest.raises(BudgetUsageUnknown):
            advance_goal_budget_clock(db, goal_id)
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(db, goal_id)
        assert ei.value.code == "BUDGET_USAGE_UNKNOWN"
        snap = goal_wall_budget_snapshot(db, goal_id)
        assert snap["budget_usage_unknown"] is True
        assert snap["budget_exhausted"] is True
    eng.dispose()


def test_exhausted_blocks_engineering_admit(api, objects):
    """elapsed 达到 wall_clock_seconds 后 ENGINEERING 准入失败关闭。"""
    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    limit = int(goal["contract"]["budget"]["wall_clock_seconds"])
    t0 = datetime(2026, 9, 12, 13, 0, 0, tzinfo=UTC)
    eng = _engine()
    with eng.begin() as db:
        init_budget_clock_on_start(db, goal_id, now=t0)
        advance_goal_budget_clock(
            db, goal_id, now=t0 + timedelta(seconds=limit), goal_status="RUNNING"
        )
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(
                db, goal_id, now=t0 + timedelta(seconds=limit + 1)
            )
        assert ei.value.code == "BUDGET_EXHAUSTED"
        snap = goal_wall_budget_snapshot(
            db, goal_id, now=t0 + timedelta(seconds=limit + 1)
        )
        assert snap["budget_exhausted"] is True
        assert snap["budget_remaining_wall_seconds"] == 0
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        assert status == "FAILED"
    eng.dispose()


def test_exhausted_marks_goal_failed_and_cancels_ready(api, objects):
    """第141批：耗尽时 Goal→FAILED，READY 工程活动取消（doc/01:112）。"""
    client, _token, auth, goal, plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    plan_id = UUID(plan["id"])
    limit = int(goal["contract"]["budget"]["wall_clock_seconds"])
    t0 = datetime(2026, 9, 12, 16, 0, 0, tzinfo=UTC)
    eng = _engine()
    with eng.begin() as db:
        # 确保有 READY ENGINEERING 可被取消
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": plan_id},
        )
        db.execute(
            text(
                """UPDATE goals SET status='PLANNING', previous_status='DRAFT',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": goal_id},
        )
        init_budget_clock_on_start(db, goal_id, now=t0)
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(
                db, goal_id, now=t0 + timedelta(seconds=limit)
            )
        assert ei.value.code == "BUDGET_EXHAUSTED"
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["block_reason"] and "BUDGET_EXHAUSTED" in g["block_reason"]
        plan_status = db.execute(
            text("SELECT status FROM activities WHERE id=:id"),
            {"id": plan_id},
        ).scalar_one()
        assert plan_status == "CANCELLED"
        ready_n = db.execute(
            text(
                """SELECT count(*) FROM activities
                WHERE goal_id=:goal AND status='READY'
                  AND kind = ANY(:kinds)"""
            ),
            {
                "goal": goal_id,
                "kinds": ["PLAN", "EXECUTE", "AUDIT", "INTEGRATE", "FINALIZE"],
            },
        ).scalar_one()
        assert ready_n == 0
    eng.dispose()
    assert client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"][
        "status"
    ] == "FAILED"


def test_concurrent_advance_no_lost_update(api, objects):
    """两连接并发推进不丢秒（行锁串行）。"""
    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    t0 = datetime(2026, 9, 12, 14, 0, 0, tzinfo=UTC)
    eng = _engine()
    with eng.begin() as db:
        init_budget_clock_on_start(db, goal_id, now=t0)
    eng.dispose()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            local = _engine()
            with local.begin() as db:
                barrier.wait(timeout=5)
                advance_goal_budget_clock(
                    db,
                    goal_id,
                    now=t0 + timedelta(seconds=offset),
                    goal_status="RUNNING",
                )
            local.dispose()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(5,))
    t2 = threading.Thread(target=worker, args=(8,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not errors, errors

    eng = _engine()
    with eng.begin() as db:
        usage = db.execute(
            text("SELECT budget_usage FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
    eng.dispose()
    # 串行后最终至少覆盖到较大偏移
    assert usage["elapsed_wall_seconds"] == 8
    assert usage["active_seconds"] == 8


def test_restart_persists_elapsed(api, objects):
    """推进提交后新连接读到累计值（模拟进程重启）。"""
    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    t0 = datetime(2026, 9, 12, 15, 0, 0, tzinfo=UTC)
    eng = _engine()
    with eng.begin() as db:
        init_budget_clock_on_start(db, goal_id, now=t0)
        advance_goal_budget_clock(
            db, goal_id, now=t0 + timedelta(seconds=42), goal_status="RUNNING"
        )
    eng.dispose()

    eng2 = _engine()
    with eng2.begin() as db:
        usage = db.execute(
            text("SELECT budget_usage FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
        assert usage["elapsed_wall_seconds"] == 42
        u = advance_goal_budget_clock(
            db, goal_id, now=t0 + timedelta(seconds=50), goal_status="RUNNING"
        )
        assert u.elapsed_wall_seconds == 50
    eng2.dispose()


def test_http_wall_budget_snapshot(api, objects):
    """GET /goals/{id}/wall-budget：推进后投影；marks_goal_done 恒 false。

    锚点必须贴近真实墙钟：HTTP 快照用 datetime.now() 再推进。
    若用固定历史 observed_at，跨日会一次累加到耗尽（假阳性 / 时间炸弹）。
    """
    client, token, auth, goal, _plan = _start_goal(api, objects)
    goal_id = goal["id"]
    # 起点必须相对真实时钟：路由内部 now=None → datetime.now(UTC) 再推进。
    # 固定绝对 t0 的存活期只有 wall_clock_seconds（合同常 3600s），到期必红。
    t0 = datetime.now(UTC)
    eng = _engine()
    with eng.begin() as db:
        init_budget_clock_on_start(db, UUID(goal_id), now=t0)
        advance_goal_budget_clock(
            db,
            UUID(goal_id),
            now=t0 + timedelta(seconds=7),
            goal_status="RUNNING",
        )
    eng.dispose()

    res = client.get(f"/api/v1/goals/{goal_id}/wall-budget", headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["budget_usage_unknown"] is False
    assert body["elapsed_wall_seconds"] >= 7
    # 合同默认 wall_clock_seconds=3600；HTTP 再推进仅毫秒级，不得误报耗尽
    assert body["elapsed_wall_seconds"] < 60
    assert body["budget_exhausted"] is False
    assert body["budget_remaining_wall_seconds"] is not None
    assert body["budget_remaining_wall_seconds"] > 3500
    assert body["marks_goal_done"] is False
    assert token


def test_http_wall_budget_readable_by_active_worker(api, objects):
    """第196批：ACTIVE worker 可读 wall-budget（编排消费）；≠ DONE。"""
    from uuid import uuid4

    from test_claims import _register_worker

    client, token, auth, goal, _plan = _start_goal(api, objects)
    goal_id = goal["id"]
    worker = str(uuid4())
    _register_worker(worker, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(worker, ["worker"])}

    res = client.get(f"/api/v1/goals/{goal_id}/wall-budget", headers=worker_auth)
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["marks_goal_done"] is False
    assert body["budget_usage_unknown"] is False
    assert "budget_remaining_wall_seconds" in body

    stranger = str(uuid4())
    # 未登记 worker → 403
    denied = client.get(
        f"/api/v1/goals/{goal_id}/wall-budget",
        headers={"Authorization": "Bearer " + token(stranger, ["worker"])},
    )
    assert denied.status_code == 403, denied.text
    assert auth


def test_heartbeat_advances_wall_budget_clock(api, objects):
    """第三百一十一批：成功 heartbeat 后独立推进 elapsed（长跑钟）；≠ Goal DONE。"""
    from uuid import uuid4

    from test_claims import _register_worker

    client, token, auth, goal, plan = _start_goal(api, objects)
    worker = str(uuid4())
    worker_id = _register_worker(worker, kinds=("PLAN",))
    worker_auth = {"Authorization": "Bearer " + token(worker, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None, claimed.text
    assert lease["activity"]["id"] == plan["id"]
    goal_id = UUID(goal["id"])

    t0 = datetime.now(UTC) - timedelta(seconds=22)
    eng = _engine()
    with eng.begin() as db:
        init_budget_clock_on_start(db, goal_id, now=t0)
        before = db.execute(
            text("SELECT budget_usage FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).scalar_one()
    eng.dispose()
    assert before["elapsed_wall_seconds"] == 0

    beat = client.post(
        f"/internal/v1/activities/{lease['lease']['activity_id']}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["data"]["renewal_seq"] == 1

    eng = _engine()
    with eng.begin() as db:
        after = db.execute(
            text("SELECT budget_usage, status FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).mappings().one()
    eng.dispose()
    assert after["status"] != "DONE"
    assert after["budget_usage"]["elapsed_wall_seconds"] >= 18
    assert after["budget_usage"]["active_seconds"] >= 18
    assert auth and token and worker_id


def test_heartbeat_budget_exhaustion_fails_goal_not_done(api, objects):
    """heartbeat tick 耗尽墙钟 → Goal FAILED，续约仍成功；≠ DONE。"""
    from uuid import uuid4

    from test_claims import _register_worker

    client, token, auth, goal, _plan = _start_goal(api, objects)
    worker = str(uuid4())
    _register_worker(worker, kinds=("PLAN",))
    worker_auth = {"Authorization": "Bearer " + token(worker, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None, claimed.text
    goal_id = UUID(goal["id"])

    # 合同墙钟压到 5s，并把锚点拨到 10s 前 → tick 必耗尽
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET contract = jsonb_set(
                    contract, '{budget,wall_clock_seconds}', '5', true
                ) WHERE id=:id"""
            ),
            {"id": goal_id},
        )
        init_budget_clock_on_start(
            db, goal_id, now=datetime.now(UTC) - timedelta(seconds=10)
        )
    eng.dispose()

    beat = client.post(
        f"/internal/v1/activities/{lease['lease']['activity_id']}/heartbeat",
        json={"lease": lease["lease"], "renewal_seq": 1},
        headers=worker_auth,
    )
    assert beat.status_code == 200, beat.text

    eng = _engine()
    with eng.begin() as db:
        row = db.execute(
            text("SELECT status, block_reason, budget_usage FROM goals WHERE id=:id"),
            {"id": goal_id},
        ).mappings().one()
    eng.dispose()
    assert row["status"] == "FAILED"
    assert row["status"] != "DONE"
    assert "BUDGET_EXHAUSTED" in (row["block_reason"] or "")
    assert row["budget_usage"]["elapsed_wall_seconds"] >= 5
    assert auth and token


def test_record_budget_meters_tokens_and_tool_calls_monotonic(api, objects):
    """token/tool_calls 有写入方且单调；≠ DONE。"""
    from control_kernel.storage.budget_clock import record_goal_budget_meters

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    eng = _engine()
    with eng.begin() as db:
        u1 = record_goal_budget_meters(
            db, goal_id, consumed_tokens_delta=100, tool_calls_delta=1
        )
        assert u1.consumed_tokens == 100
        assert u1.tool_calls == 1
        u2 = record_goal_budget_meters(
            db, goal_id, consumed_tokens_delta=50, tool_calls_delta=2
        )
        assert u2.consumed_tokens == 150
        assert u2.tool_calls == 3
        # 零增量可读
        u3 = record_goal_budget_meters(db, goal_id)
        assert u3.consumed_tokens == 150
        assert u3.tool_calls == 3
    eng.dispose()
    got = _client.get(f"/api/v1/goals/{goal_id}", headers=_auth).json()["data"]
    assert got["status"] != "DONE"
    assert got["budget_usage"]["consumed_tokens"] == 150
    assert got["budget_usage"]["tool_calls"] == 3


def test_effect_succeeded_receipt_increments_tool_calls(api, objects):
    """Effect SUCCEEDED 回执推进 BudgetUsage.tool_calls；幂等旧回执不双计。"""
    import hashlib
    from datetime import UTC, datetime
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    goal_id = goal["id"]

    before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert before["budget_usage"]["tool_calls"] == 0

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "tool:read_file:budget-meter",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        UUID(exec_lease["activity"]["project_id"]),
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
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    receipt_id = str(uuid4())
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    receipt_body = {
        "receipt_id": receipt_id,
        "effect_id": effect["id"],
        "producer_activity_id": activity_id,
        "producer_attempt_id": exec_lease["lease"]["attempt_id"],
        "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
        "started_at": ts,
        "finished_at": ts,
        "exit_code": 0,
        "timed_out": False,
        "result_artifact_ids": [str(artifact.id)],
        "observed_outcome": "SUCCEEDED",
    }
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json=receipt_body,
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "APPLIED"

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["tool_calls"] == 1
    assert after["status"] != "DONE"

    # 幂等重传（同 content_digest）不双计
    again = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json=receipt_body,
        headers=worker_auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["disposition"] == "APPLIED"
    again_goal = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert again_goal["budget_usage"]["tool_calls"] == 1


def _shrink_goal_meter_limits(
    goal_id: UUID,
    *,
    max_tokens: int | None = None,
    max_tool_calls: int | None = None,
    max_cost_usd: str | None = None,
    max_network_calls: int | None = None,
    max_disk_bytes: int | None = None,
    max_gpu_seconds: str | None = None,
) -> None:
    """测试专用：收紧合同 meter/cost 上限（不经 API 版本行）。"""
    import json

    eng = _engine()
    with eng.begin() as db:
        contract = db.execute(
            text("SELECT contract FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        if isinstance(contract, str):
            contract = json.loads(contract)
        budget = dict(contract["budget"])
        if max_tokens is not None:
            budget["max_tokens"] = max_tokens
        if max_tool_calls is not None:
            budget["max_tool_calls"] = max_tool_calls
        if max_cost_usd is not None:
            budget["max_cost_usd"] = max_cost_usd
        if max_network_calls is not None:
            budget["max_network_calls"] = max_network_calls
        if max_disk_bytes is not None:
            budget["max_disk_bytes"] = max_disk_bytes
        if max_gpu_seconds is not None:
            budget["max_gpu_seconds"] = max_gpu_seconds
        contract = dict(contract)
        contract["budget"] = budget
        db.execute(
            text(
                """UPDATE goals SET contract=CAST(:c AS jsonb),
                updated_at=clock_timestamp() WHERE id=:id"""
            ),
            {"id": goal_id, "c": json.dumps(contract)},
        )
    eng.dispose()


def test_tool_calls_exhaustion_fails_goal_not_done(api, objects):
    """tool_calls 达 max_tool_calls → FAILED；≠ DONE；admit 拒绝。"""
    from control_kernel.storage.budget_clock import record_goal_budget_meters

    client, _token, auth, goal, plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    plan_id = UUID(plan["id"])
    _shrink_goal_meter_limits(goal_id, max_tool_calls=2)
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": plan_id},
        )
        u = record_goal_budget_meters(db, goal_id, tool_calls_delta=2)
        assert u.tool_calls == 2
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "tool_calls" in (g["block_reason"] or "")
        assert "BUDGET_EXHAUSTED" in (g["block_reason"] or "")
        plan_status = db.execute(
            text("SELECT status FROM activities WHERE id=:id"), {"id": plan_id}
        ).scalar_one()
        assert plan_status == "CANCELLED"
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(db, goal_id)
        assert ei.value.code == "BUDGET_EXHAUSTED"
    eng.dispose()
    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "FAILED"
    assert got["budget_usage"]["tool_calls"] == 2


def test_consumed_tokens_exhaustion_fails_goal_not_done(api, objects):
    """consumed_tokens 达 max_tokens → FAILED；≠ DONE。"""
    from control_kernel.storage.budget_clock import record_goal_budget_meters

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    _shrink_goal_meter_limits(goal_id, max_tokens=50)
    eng = _engine()
    with eng.begin() as db:
        u = record_goal_budget_meters(db, goal_id, consumed_tokens_delta=50)
        assert u.consumed_tokens == 50
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "consumed_tokens" in (g["block_reason"] or "")
        assert "BUDGET_EXHAUSTED" in (g["block_reason"] or "")
    eng.dispose()


def test_cost_zero_budget_allows_zero_spend_but_fails_on_positive(api, objects):
    """max_cost_usd=0：consumed=0 可 admit；正花费 → FAILED ≠ DONE。"""
    from control_kernel.storage.budget_clock import record_goal_budget_meters

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    # 测试夹具默认 max_cost_usd 已是 "0"
    eng = _engine()
    with eng.begin() as db:
        assert_goal_wall_budget_allows_engineering(db, goal_id)
        u = record_goal_budget_meters(
            db, goal_id, consumed_cost_usd_delta="0.01"
        )
        assert u.consumed_cost_usd == "0.01"
        assert u.cost_status == "CONFIRMED"
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "consumed_cost_usd" in (g["block_reason"] or "")
        assert "BUDGET_EXHAUSTED" in (g["block_reason"] or "")
    eng.dispose()


def test_cost_limit_exhaustion_at_boundary(api, objects):
    """正 max_cost_usd 边界含等值耗尽。"""
    from control_kernel.storage.budget_clock import record_goal_budget_meters

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    _shrink_goal_meter_limits(goal_id, max_cost_usd="1.5")
    eng = _engine()
    with eng.begin() as db:
        mid = record_goal_budget_meters(db, goal_id, consumed_cost_usd_delta="1")
        assert mid.consumed_cost_usd == "1"
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        assert status != "FAILED"
        assert_goal_wall_budget_allows_engineering(db, goal_id)
        u = record_goal_budget_meters(db, goal_id, consumed_cost_usd_delta="0.5")
        assert u.consumed_cost_usd == "1.5"
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert "BUDGET_EXHAUSTED" in (g["block_reason"] or "")
    eng.dispose()


def test_network_and_disk_meters_exhaustion(api, objects):
    """network_calls / disk_bytes 达上限 → FAILED；≠ DONE。"""
    from control_kernel.storage.budget_clock import record_goal_budget_meters

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    _shrink_goal_meter_limits(goal_id, max_network_calls=2, max_disk_bytes=100)
    eng = _engine()
    with eng.begin() as db:
        u = record_goal_budget_meters(db, goal_id, network_calls_delta=2)
        assert u.network_calls == 2
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "network_calls" in (g["block_reason"] or "")
    eng.dispose()

    _client2, _token2, _auth2, goal2, _plan2 = _start_goal(api, objects)
    goal2_id = UUID(goal2["id"])
    _shrink_goal_meter_limits(goal2_id, max_disk_bytes=50)
    eng = _engine()
    with eng.begin() as db:
        u = record_goal_budget_meters(db, goal2_id, disk_bytes_delta=50)
        assert u.disk_bytes == 50
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal2_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert "disk_bytes" in (g["block_reason"] or "")
        assert g["status"] != "DONE"
    eng.dispose()


def test_reserved_tokens_adjust_and_release_clamp(api, objects):
    """预留写入/释放：登记+、释放-钳制到 0；预留计入耗尽；≠ DONE。"""
    from control_kernel.storage.budget_clock import (
        adjust_goal_budget_reservations,
        assert_goal_wall_budget_allows_engineering,
    )

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    _shrink_goal_meter_limits(goal_id, max_tokens=100)
    eng = _engine()
    with eng.begin() as db:
        u = adjust_goal_budget_reservations(
            db, goal_id, reserved_tokens_delta=40, reserved_cost_usd_delta="0"
        )
        assert u.reserved_tokens == 40
        assert_goal_wall_budget_allows_engineering(db, goal_id)
        # 预留再占 60 → 100 达上限 FAILED
        adjust_goal_budget_reservations(db, goal_id, reserved_tokens_delta=60)
        g = (
            db.execute(
                text("SELECT status, block_reason, budget_usage FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "reserved_tokens" in (g["block_reason"] or "")
        assert g["budget_usage"]["reserved_tokens"] == 100
    eng.dispose()

    _client2, _token2, _auth2, goal2, _plan2 = _start_goal(api, objects)
    goal2_id = UUID(goal2["id"])
    eng = _engine()
    with eng.begin() as db:
        adjust_goal_budget_reservations(db, goal2_id, reserved_tokens_delta=25)
        released = adjust_goal_budget_reservations(
            db, goal2_id, reserved_tokens_delta=-100
        )
        assert released.reserved_tokens == 0
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal2_id}
        ).scalar_one()
        assert status != "DONE"
        assert status != "FAILED"
    eng.dispose()


def test_effect_succeeded_receipt_increments_disk_bytes(api, objects):
    """Effect SUCCEEDED 回执按结果工件 size_bytes 累加 disk_bytes。"""
    import hashlib
    from datetime import UTC, datetime
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from test_effects import _publish_and_claim_execute

    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    goal_id = goal["id"]
    before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert before["budget_usage"]["disk_bytes"] == 0

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "tool:read_file:disk-meter",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]
    blob = b'{"path":"src/main.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        UUID(exec_lease["activity"]["project_id"]),
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
    dispatched = client.post(
        f"/internal/v1/effects/{effect['id']}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=worker_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    receipt = client.post(
        f"/internal/v1/effects/{effect['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "effect_id": effect["id"],
            "producer_activity_id": activity_id,
            "producer_attempt_id": exec_lease["lease"]["attempt_id"],
            "fencing_epoch": exec_lease["lease"]["fencing_epoch"],
            "started_at": ts,
            "finished_at": ts,
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "SUCCEEDED",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["tool_calls"] == 1
    assert after["budget_usage"]["disk_bytes"] == len(blob)
    assert after["budget_usage"]["network_calls"] == 0  # read_file 无 URL
    assert after["status"] != "DONE"


def test_gpu_seconds_null_limit_still_records_usage(api, objects):
    """max_gpu_seconds=null：仍记录可得用量，不因 GPU 硬预算 FAILED。"""
    from control_kernel.storage.budget_clock import (
        assert_goal_wall_budget_allows_engineering,
        record_goal_budget_meters,
    )

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    # 夹具默认 max_gpu_seconds=null
    eng = _engine()
    with eng.begin() as db:
        u = record_goal_budget_meters(db, goal_id, gpu_seconds_delta="1.5")
        assert u.gpu_seconds == "1.5"
        assert_goal_wall_budget_allows_engineering(db, goal_id)
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        assert status != "FAILED"
        assert status != "DONE"
    eng.dispose()


def test_gpu_seconds_exhaustion_fails_goal_not_done(api, objects):
    """正 max_gpu_seconds 边界含等值耗尽 → FAILED；admit 拒绝；≠ DONE。"""
    from control_kernel.storage.budget_clock import (
        assert_goal_wall_budget_allows_engineering,
        record_goal_budget_meters,
    )

    client, _token, auth, goal, plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    plan_id = UUID(plan["id"])
    _shrink_goal_meter_limits(goal_id, max_gpu_seconds="2")
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": plan_id},
        )
        mid = record_goal_budget_meters(db, goal_id, gpu_seconds_delta="1")
        assert mid.gpu_seconds == "1"
        assert_goal_wall_budget_allows_engineering(db, goal_id)
        u = record_goal_budget_meters(db, goal_id, gpu_seconds_delta="1")
        assert u.gpu_seconds == "2"
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "gpu_seconds" in (g["block_reason"] or "")
        assert "BUDGET_EXHAUSTED" in (g["block_reason"] or "")
        plan_status = db.execute(
            text("SELECT status FROM activities WHERE id=:id"), {"id": plan_id}
        ).scalar_one()
        assert plan_status == "CANCELLED"
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(db, goal_id)
        assert ei.value.code == "BUDGET_EXHAUSTED"
    eng.dispose()
    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert got["status"] == "FAILED"
    assert got["budget_usage"]["gpu_seconds"] == "2"


def test_gpu_zero_budget_allows_zero_but_fails_on_positive(api, objects):
    """max_gpu_seconds=0：gpu=0 可 admit；正用量 → FAILED ≠ DONE。"""
    from control_kernel.storage.budget_clock import (
        assert_goal_wall_budget_allows_engineering,
        record_goal_budget_meters,
    )

    _client, _token, _auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    _shrink_goal_meter_limits(goal_id, max_gpu_seconds="0")
    eng = _engine()
    with eng.begin() as db:
        assert_goal_wall_budget_allows_engineering(db, goal_id)
        u = record_goal_budget_meters(db, goal_id, gpu_seconds_delta="0.1")
        assert u.gpu_seconds == "0.1"
        g = (
            db.execute(
                text("SELECT status, block_reason FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert g["status"] == "FAILED"
        assert g["status"] != "DONE"
        assert "gpu_seconds" in (g["block_reason"] or "")
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(db, goal_id)
        assert ei.value.code == "BUDGET_EXHAUSTED"
    eng.dispose()
