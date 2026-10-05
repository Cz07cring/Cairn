"""E2E-5 注入点 4：CandidateManifest 封存后杀 Executor。

密封候选不可变；Executor 树被破坏/重写不影响已封存 digest；Auditor 可物化续跑。≠ DONE。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
from e2e_tool_helpers import prepare_tool_effect
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)
from sqlalchemy import create_engine, text
from test_claims import _register_worker

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"
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


def _confirm_stop(client, worker_auth, stop_id: str, attempt_id: str) -> None:
    confirm = client.post(
        f"/internal/v1/stops/{stop_id}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop_id),
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert confirm.status_code == 201, confirm.text


def test_e2e5_seal_then_kill_executor_auditor_continues_x10(api, objects, tmp_path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(
        api,
        objects,
        tmp_path,
        run_id="e2e5-seal-kill-exec",
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
    seed = ctx["seed"]
    ws = run.executor_worktree
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    client.app.state.objects = store

    seed.apply_reference_fix(ws)
    assert (ws / "tests_hidden").exists() is False
    sealed_fix = (ws / "order_service" / "store.py").read_text(encoding="utf-8")
    assert "_by_key" in sealed_fix

    _step, effect, blob = prepare_tool_effect(
        {
            "client": client,
            "engine": engine,
            "store": store,
            "project_id": project_id,
            "exec_lease": exec_lease,
            "exec_auth": exec_auth,
        },
        tool_ref="seal_candidate",
        purpose="e2e5 seal before kill executor",
        parameters={"verification_profile_ids": [ctx["profile_id"]]},
        schema_digest=SEAL_CANDIDATE_SCHEMA_DIGEST,
        predecessor_step_id=None,
        producer="e2e5-seal",
    )
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=exec_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=ws,
        allowed_paths=ALLOW,
        input_bytes=blob,
    )
    assert sealed.get("seal_error") is None, sealed.get("seal_error")
    candidate = sealed["candidate"]
    assert candidate is not None
    sealed_digest = candidate["content_digest"]
    seal_effect_id = effect["id"]
    activity_id = exec_lease["activity"]["id"]
    attempt_id = exec_lease["lease"]["attempt_id"]

    # 封存后杀 Executor（失租）
    _force_expire(engine, attempt_id)
    _trigger_expire_scan(client, token)
    with engine.begin() as db:
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
        seal_status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": seal_effect_id},
        ).scalar_one()
    assert stop["status"] == "REQUESTED"
    assert seal_status == "SUCCEEDED"  # 封存已成功，不因失租变 UNKNOWN

    got = client.get(
        f"/api/v1/candidates/{candidate['id']}", headers=auth
    ).json()["data"]
    assert got["content_digest"] == sealed_digest

    def read_bytes(digest: str) -> bytes:
        return store.read(UUID(str(project_id)), digest)

    # ×10：破坏 Executor 树并尝试重开写入；密封候选不变；Auditor 物化仍可用
    for round_i in range(_CRASH_ROUNDS):
        (ws / "order_service" / "store.py").write_text(
            f"# corrupted by dead executor round {round_i}\n",
            encoding="utf-8",
        )

        # 旧租约不得再写
        step_blocked = client.post(
            f"/internal/v1/activities/{activity_id}/steps",
            json={
                "lease": exec_lease["lease"],
                "predecessor_step_id": None,
                "purpose": f"e2e5 reopen write {round_i}",
                "tool_ref": "write_file",
            },
            headers=exec_auth,
        )
        assert step_blocked.status_code in (400, 409), step_blocked.text

        # 密封候选字节不变
        again = client.get(
            f"/api/v1/candidates/{candidate['id']}", headers=auth
        ).json()["data"]
        assert again["content_digest"] == sealed_digest
        assert again["id"] == candidate["id"]

        mat_root = run.artifacts / f"auditor_mat_{round_i}"
        if mat_root.exists():
            shutil.rmtree(mat_root)
        n = materialize_candidate_worktree(
            mat_root, again["files"], read_bytes=read_bytes, read_only=True
        )
        assert n == len(again["files"])
        mat_store = (mat_root / "order_service" / "store.py").read_text(encoding="utf-8")
        assert mat_store == sealed_fix
        assert mat_store != (ws / "order_service" / "store.py").read_text(encoding="utf-8")
        attach_holdout_tests(mat_root, _HOLDOUT)
        green = run_pytest(mat_root, "tests/", "tests_hidden/")
        assert green.returncode == 0, green.stdout + green.stderr

    # Stop 确认后若重领：不得再开 ENGINEERING write（STEP_CONFLICT = 不得重开候选写入）
    _confirm_stop(client, exec_auth, str(stop["id"]), attempt_id)
    other = str(uuid4())
    _register_worker(other, kinds=("EXECUTE",))
    other_auth = {"Authorization": "Bearer " + token(other, ["worker"])}
    reclaimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**other_auth, "Idempotency-Key": str(uuid4())},
    )
    if reclaimed.status_code == 200 and reclaimed.json()["data"].get("lease"):
        new_lease = reclaimed.json()["data"]
        if new_lease["activity"]["id"] == activity_id:
            reopen = client.post(
                f"/internal/v1/activities/{activity_id}/steps",
                json={
                    "lease": new_lease["lease"],
                    "predecessor_step_id": None,
                    "purpose": "e2e5 reopen write after reclaim",
                    "tool_ref": "write_file",
                },
                headers=other_auth,
            )
            assert reopen.status_code in (400, 409), reopen.text

    final_cand = client.get(
        f"/api/v1/candidates/{candidate['id']}", headers=auth
    ).json()["data"]
    assert final_cand["content_digest"] == sealed_digest
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] != "DONE"

    summary = {
        "phase": "E2E-5-seal-then-kill-executor",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "candidate_id": candidate["id"],
        "sealed_content_digest": sealed_digest,
        "crash_rounds": _CRASH_ROUNDS,
        "auditor_materialize_ok": True,
        "marks_goal_done": False,
        "non_goals": [
            "e2e5_other_inject_points",
            "audit_pass",
            "goal_done",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    engine.dispose()
