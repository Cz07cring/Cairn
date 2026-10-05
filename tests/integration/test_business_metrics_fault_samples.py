"""Codex P1-4：故障注入样本须进入业务指标分母（UNKNOWN 恢复 / 重试）。

顺利路径无法证明 `unknown_recovery` / `retry_success` 采集器正确。
本文件用真实 Kernel 路径制造：
  · DISPATCHED → UNKNOWN 回执 → reconcile → SUCCEEDED（恢复样本）
  · EXECUTE 失租后二次 claim → 终态 SUCCEEDED（重试样本）
再对单 Goal 跑 `report_business_metrics.collect`，断言分母 > 0。

≠ Goal DONE；不对共享库全窗做验收裁定。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _drain_ready_plans, _register_worker
from test_reconcile import _prepare_dispatched_effect

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "report_business_metrics",
    _ROOT / "scripts" / "report_business_metrics.py",
)
assert _spec and _spec.loader
_metrics = importlib.util.module_from_spec(_spec)
sys.modules["report_business_metrics"] = _metrics
_spec.loader.exec_module(_metrics)


def _metrics_engine():
    url = (os.environ.get("RING_TEST_DATABASE_URL") or "").strip()
    assert url, "RING_TEST_DATABASE_URL required"
    return create_engine(url)


def _reconcile_unknown_to_succeeded(
    client,
    token,
    auth,
    worker_auth,
    exec_lease,
    effect,
    artifact,
):
    """活租约 UNKNOWN 回执 → RECONCILE outcome → effect SUCCEEDED。"""
    activity_id = exec_lease["activity"]["id"]
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
            "exit_code": None,
            "timed_out": True,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "UNKNOWN",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    unknown = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"

    task_id = exec_lease["activity"]["task_id"]
    evidence = client.get(f"/api/v1/tasks/{task_id}/evidence", headers=auth)
    assert evidence.status_code == 200, evidence.text
    env_id = evidence.json()["data"][0]["id"]

    created = client.post(
        f"/api/v1/effects/{effect['id']}/reconcile",
        json={
            "expected_state_revision": unknown["state_revision"],
            "observed_result": "SUCCEEDED",
            "external_ref": f"metrics-fault-{uuid4().hex[:8]}",
            "evidence_ids": [env_id],
            "reason": "P1-4 指标故障注入：核实远端已成功",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 202, created.text
    reconcile_activity_id = created.json()["data"]["result"]["activity_id"]

    engine = _metrics_engine()
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status IN ('READY','RUNNING','RECOVERING')
                  AND kind='RECONCILE' AND id<>:id"""
            ),
            {"id": reconcile_activity_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('READY','CANCELLED')"""
            ),
            {"id": reconcile_activity_id},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("RECONCILE",))
    rec_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["RECONCILE"], "capabilities": []},
        headers={**rec_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == reconcile_activity_id

    done = client.post(
        f"/internal/v1/activities/{reconcile_activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "effect_id": effect["id"],
                "observed_status": "SUCCEEDED",
                "evidence_ids": [env_id],
            },
        },
        headers=rec_auth,
    )
    assert done.status_code == 200, done.text
    final = client.get(f"/api/v1/effects/{effect['id']}", headers=auth).json()["data"]
    assert final["status"] == "SUCCEEDED"
    return final


def test_metrics_unknown_recovery_denominator_after_reconcile(api, objects):
    """P1-4：UNKNOWN→SUCCEEDED 后，单 Goal 窗 unknown_recovery 分母≥1、分子≥1。"""
    (
        client,
        token,
        auth,
        goal,
        worker_auth,
        exec_lease,
        effect,
        artifact,
    ) = _prepare_dispatched_effect(api, objects)

    _reconcile_unknown_to_succeeded(
        client, token, auth, worker_auth, exec_lease, effect, artifact
    )

    db = _metrics_engine()
    report = _metrics.collect(db, goal_id=str(goal["id"]))
    recovery = report.as_dict()["metrics"]["unknown_recovery"]["recovery"]
    assert recovery["denominator"] >= 1, recovery
    assert recovery["numerator"] >= 1, recovery
    assert recovery["numerator"] <= recovery["denominator"]

    # 清理：取消 Goal，避免污染后续窗口（仍非 DONE 宣称）
    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": got["state_revision"], "reason": "metrics-fault-cleanup"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)


def test_metrics_unknown_outstanding_while_unreconciled(api, objects):
    """P1-4：未对账 UNKNOWN 须出现在 outstanding（证明采集器看见卡住样本）。"""
    (
        client,
        token,
        auth,
        goal,
        worker_auth,
        exec_lease,
        effect,
        artifact,
    ) = _prepare_dispatched_effect(api, objects)
    activity_id = exec_lease["activity"]["id"]

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
            "exit_code": None,
            "timed_out": True,
            "result_artifact_ids": [str(artifact.id)],
            "observed_outcome": "UNKNOWN",
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text

    db = _metrics_engine()
    report = _metrics.collect(db, goal_id=str(goal["id"]))
    data = report.as_dict()["metrics"]["unknown_recovery"]
    assert data["recovery"]["denominator"] >= 1
    assert data["outstanding"]["still_unknown"] >= 1
    assert data["recovery"]["numerator"] == 0

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={"expected_state_revision": got["state_revision"], "reason": "metrics-outstanding-cleanup"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)


def test_metrics_retry_success_after_plan_lease_expire_reclaim(api, objects):
    """P1-4：PLAN 失租后二次 claim 再提交 plan → retry_success 分母≥1。"""
    from test_claims import _start_goal
    from test_plans import _plan_body

    client, token, auth, goal, plan_activity = _start_goal(api, objects)
    engine = _metrics_engine()
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

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease1 = claimed.json()["data"]
    assert lease1["activity"]["id"] == plan_activity["id"]
    attempt1 = lease1["lease"]["attempt_id"]

    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt1, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )

    # 触发失租扫描
    probe = str(uuid4())
    _register_worker(probe, kinds=("EXECUTE",))
    probe_auth = {"Authorization": "Bearer " + token(probe, ["worker"])}
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**probe_auth, "Idempotency-Key": str(uuid4())},
    )

    with engine.begin() as db:
        db.execute(
            text("""UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
              WHERE status='READY' AND kind='PLAN' AND id<>:id"""),
            {"id": plan_activity["id"]},
        )
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id AND status IN ('READY','RECOVERING','CANCELLED')"""
            ),
            {"id": plan_activity["id"]},
        )

    subject2 = str(uuid4())
    _register_worker(subject2, kinds=("PLAN",))
    auth2 = {"Authorization": "Bearer " + token(subject2, ["worker"])}
    claimed2 = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth2, "Idempotency-Key": str(uuid4())},
    )
    assert claimed2.status_code == 200, claimed2.text
    lease2 = claimed2.json()["data"]
    assert lease2["activity"]["id"] == plan_activity["id"]
    assert lease2["lease"]["attempt_id"] != attempt1

    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    done = client.post(
        f"/internal/v1/activities/{plan_activity['id']}/outcomes",
        json={
            "lease": lease2["lease"],
            "expected_state_revision": lease2["activity"]["state_revision"],
            "outcome": {"plan": _plan_body(goal, profile_id)},
        },
        headers=auth2,
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "SUCCEEDED"

    with engine.begin() as db:
        n_attempts = db.execute(
            text("SELECT count(*) FROM activity_attempts WHERE activity_id=:id"),
            {"id": plan_activity["id"]},
        ).scalar_one()
    assert n_attempts >= 2, n_attempts

    report = _metrics.collect(engine, goal_id=str(goal["id"]))
    retry = report.as_dict()["metrics"]["retry_success"]
    assert retry["denominator"] >= 1, retry
    assert retry["numerator"] >= 1, retry

    got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    client.post(
        f"/api/v1/goals/{goal['id']}/cancel",
        json={
            "expected_state_revision": got["state_revision"],
            "reason": "metrics-retry-cleanup",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    _drain_ready_plans(client, token)
    engine.dispose()
