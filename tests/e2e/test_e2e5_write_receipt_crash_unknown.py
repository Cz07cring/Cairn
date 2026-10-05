"""E2E-5 注入点 2：订单 write 已落盘、回执前提交失败/失租 → UNKNOWN。

外部写入已发生；对账前不得盲重试新写入、不得 Goal DONE。
对齐 AB03；落到订单 fixture 路径。≠ 其余注入点 / 自主 Loop。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import WRITE_FILE_SCHEMA_DIGEST
from e2e3_order_helpers import ALLOW, boot_execute_for_seal
from e2e_tool_helpers import load_reference_fix_store, prepare_tool_effect
from execution_broker.write_file import execute_write_file
from sqlalchemy import create_engine, text
from test_claims import _register_worker

_CRASH_ROUNDS = 10


def _force_expire(engine, attempt_id: str) -> None:
    with engine.begin() as db:
        db.execute(
            text("""UPDATE activity_attempts
              SET lease_expires_at=:past, updated_at=clock_timestamp()
              WHERE id=:id"""),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )


def _trigger_expire_scan(client, token) -> None:
    probe = str(uuid4())
    _register_worker(probe, kinds=("PLAN", "EXECUTE"))
    client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={
            "Authorization": "Bearer " + token(probe, ["worker"]),
            "Idempotency-Key": str(uuid4()),
        },
    )


def test_e2e5_order_write_before_receipt_crash_unknown_x10(api, objects, tmp_path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(api, objects, tmp_path, run_id="e2e5-write-receipt")
    client = ctx["client"]
    token = ctx["token"]
    auth = ctx["auth"]
    goal = ctx["goal"]
    exec_auth = ctx["exec_auth"]
    exec_lease = ctx["exec_lease"]
    project_id = ctx["project_id"]
    store = ctx["store"]
    run = ctx["run"]
    ws = run.executor_worktree
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    client.app.state.objects = store

    buggy = (ws / "order_service" / "store.py").read_text(encoding="utf-8")
    assert "_by_key" not in buggy
    fix_body = load_reference_fix_store()
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]
    old_epoch = exec_lease["lease"]["fencing_epoch"]

    step, effect, blob = prepare_tool_effect(
        {
            "client": client,
            "engine": engine,
            "store": store,
            "project_id": project_id,
            "exec_lease": exec_lease,
            "exec_auth": exec_auth,
        },
        tool_ref="write_file",
        purpose="e2e5 write before receipt crash",
        parameters={"path": "order_service/store.py", "content": fix_body},
        schema_digest=WRITE_FILE_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e5-receipt",
    )
    effect_id = effect["id"]
    input_artifact_id = effect["input_artifact_id"]
    assert effect["status"] == "PREPARED"

    dispatched = client.post(
        f"/internal/v1/effects/{effect_id}/dispatch",
        json={
            "lease": exec_lease["lease"],
            "effect_state_revision": effect["state_revision"],
        },
        headers=exec_auth,
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["data"]["status"] == "DISPATCHED"

    # 外部副作用已发生：Broker 写盘成功，但尚未提交 receipt
    wrote = execute_write_file(
        ws,
        blob,
        allowed_paths=["order_service/**"],
    )
    assert wrote.observed_outcome == "SUCCEEDED", wrote.error
    assert (ws / "order_service" / "store.py").read_text(encoding="utf-8") == fix_body

    _force_expire(engine, attempt_id)
    _trigger_expire_scan(client, token)

    unknown = client.get(f"/api/v1/effects/{effect_id}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"
    producer = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()[
        "data"
    ]
    assert producer["status"] == "RECOVERING"

    with engine.begin() as db:
        effect_n = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
        stop = (
            db.execute(
                text(
                    """SELECT id, status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"id": attempt_id},
            )
            .mappings()
            .one()
        )
    assert effect_n == 1
    assert stop["status"] == "REQUESTED"

    late_dispositions: list[str] = []
    for _ in range(_CRASH_ROUNDS):
        # 未对账：不得重领本 EXECUTE 继续写工具
        other = str(uuid4())
        _register_worker(other, kinds=("EXECUTE",))
        other_auth = {"Authorization": "Bearer " + token(other, ["worker"])}
        reclaim = client.post(
            "/internal/v1/claims",
            json={"kinds": ["EXECUTE"], "capabilities": []},
            headers={**other_auth, "Idempotency-Key": str(uuid4())},
        )
        if reclaim.status_code == 200 and reclaim.json()["data"].get("lease"):
            assert reclaim.json()["data"]["activity"]["id"] != activity_id

        late = client.post(
            f"/internal/v1/effects/{effect_id}/receipts",
            json={
                "receipt_id": str(uuid4()),
                "effect_id": effect_id,
                "producer_activity_id": activity_id,
                "producer_attempt_id": attempt_id,
                "fencing_epoch": old_epoch,
                "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z",
                "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z",
                "exit_code": 0,
                "timed_out": False,
                "result_artifact_ids": [input_artifact_id],
                "observed_outcome": "SUCCEEDED",
            },
            headers=exec_auth,
        )
        assert late.status_code == 201, late.text
        assert late.json()["data"]["disposition"] == "PENDING_RECONCILIATION"
        late_dispositions.append(late.json()["data"]["disposition"])

        still = client.get(f"/api/v1/effects/{effect_id}", headers=auth).json()["data"]
        assert still["status"] == "UNKNOWN"
        assert (ws / "order_service" / "store.py").read_text(encoding="utf-8") == fix_body

        with engine.begin() as db:
            n = db.execute(
                text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
                {"a": activity_id},
            ).scalar_one()
            statuses = list(
                db.execute(
                    text(
                        """SELECT status FROM effect_intents
                        WHERE activity_id=:a ORDER BY created_at"""
                    ),
                    {"a": activity_id},
                ).scalars()
            )
        assert n == 1
        assert statuses == ["UNKNOWN"]

    assert late_dispositions == ["PENDING_RECONCILIATION"] * _CRASH_ROUNDS

    # 旧租约不得再开新 ENGINEERING step（盲重试关闭）
    step_retry = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": step["id"],
            "purpose": "e2e5-blind-retry-write",
            "tool_ref": "write_file",
        },
        headers=exec_auth,
    )
    assert step_retry.status_code in (400, 409), step_retry.text

    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"

    summary = {
        "phase": "E2E-5-write-before-receipt-unknown",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "effect_id": effect_id,
        "crash_rounds": _CRASH_ROUNDS,
        "effect_count": 1,
        "disk_fixed_before_receipt": True,
        "marks_goal_done": False,
        "allowed_paths": ALLOW,
        "non_goals": [
            "e2e5_other_inject_points",
            "reconcile_to_succeeded",
            "official_loop_autonomous",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    engine.dispose()
