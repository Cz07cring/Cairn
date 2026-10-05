"""E2E-5 注入点 3：run_tests 已跑完、Evidence/回执前提交失败 → UNKNOWN。

不伪造测试结果为 SUCCEEDED；本地可重跑只读 pytest；写操作不盲重试。≠ DONE。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import RUN_TESTS_SCHEMA_DIGEST
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
from e2e_tool_helpers import prepare_tool_effect
from execution_broker.run_tests import execute_run_tests
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


def test_e2e5_order_run_tests_before_evidence_crash_unknown_x10(api, objects, tmp_path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    import seed_business_e2e as seed

    ctx = boot_execute_for_seal(
        api,
        objects,
        tmp_path,
        run_id="e2e5-run-tests-evidence",
        allowed_tools=["read_file", "write_file", "run_tests", "seal_candidate"],
    )
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

    # fixture 自检：先落好修复，使 public suite 为绿（本缝焦点是 evidence 前崩溃，非修 bug）
    seed.apply_reference_fix(ws)
    pre = run_pytest(ws, "tests/")
    assert pre.returncode == 0, pre.stdout + pre.stderr

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
        tool_ref="run_tests",
        purpose="e2e5 run_tests before evidence crash",
        parameters={"suite": "public"},
        schema_digest=RUN_TESTS_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e5-run-tests",
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

    # 外部测试已跑完，但 Evidence PUT / receipt 尚未提交
    ran = execute_run_tests(ws, blob, timeout_seconds=120)
    assert ran.observed_outcome == "SUCCEEDED", ran.error
    assert ran.exit_code == 0
    assert ran.content is not None  # 本地证据体存在，但未入库

    _force_expire(engine, attempt_id)
    _trigger_expire_scan(client, token)

    unknown = client.get(f"/api/v1/effects/{effect_id}", headers=auth).json()["data"]
    assert unknown["status"] == "UNKNOWN"
    # Kernel 不得把未入库的本地跑测结果写成 SUCCEEDED
    assert unknown["status"] != "SUCCEEDED"
    producer = client.get(f"/api/v1/activities/{activity_id}", headers=auth).json()[
        "data"
    ]
    assert producer["status"] == "RECOVERING"

    with engine.begin() as db:
        effect_n = db.execute(
            text("SELECT count(*) FROM effect_intents WHERE activity_id=:a"),
            {"a": activity_id},
        ).scalar_one()
    assert effect_n == 1

    for _ in range(_CRASH_ROUNDS):
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
        still = client.get(f"/api/v1/effects/{effect_id}", headers=auth).json()["data"]
        assert still["status"] == "UNKNOWN"

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

    # 可重跑只读验证（本地 pytest），不经新 write_file effect
    reread = run_pytest(ws, "tests/")
    assert reread.returncode == 0, reread.stdout + reread.stderr

    # 写操作盲重试关闭
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
        "phase": "E2E-5-run-tests-before-evidence-unknown",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "effect_id": effect_id,
        "crash_rounds": _CRASH_ROUNDS,
        "effect_count": 1,
        "local_pytest_rerun_ok": True,
        "marks_goal_done": False,
        "allowed_paths": ALLOW,
        "non_goals": [
            "e2e5_other_inject_points",
            "reconcile_to_succeeded",
            "fake_test_evidence_as_succeeded",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    engine.dispose()
