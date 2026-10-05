"""E2E-5 注入点 6：Finalization CAS 前断线。

失租后重试：最多一个 ReleaseManifest；Goal 不出现终态复活（DONE→非 DONE）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from e2e3_order_helpers import ALLOW, run_pytest
from e2e4_order_helpers import boot_execute_for_finalize
from execution_broker import (
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)
from sqlalchemy import create_engine, text
from test_claims import _register_worker
from verification_run_helpers import (
    post_verification_run,
    profile_verifier_digest,
    receipt_artifact,
)

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
    _register_worker(probe, kinds=("PLAN", "FINALIZE"))
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


def _release_count(engine, goal_id: str) -> int:
    with engine.begin() as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM release_manifests WHERE goal_id=:g"),
                {"g": goal_id},
            ).scalar_one()
        )


def _finalize_outcome_body(goal, candidate, barrier_id, fin, fin_run_id, fin_assign):
    return {
        "barrier_id": barrier_id,
        "candidate_manifest_id": candidate["id"],
        "global_audits": [
            {
                "subject_candidate_manifest_id": candidate["id"],
                "goal_contract_revision": goal["contract_revision"],
                "task_contract_revision": None,
                "verification_profile_id": fin_assign["verification_profile_id"],
                "layer": "GLOBAL",
                "audit_round": fin_assign["audit_round"],
                "verifier_run_ids": [fin_run_id],
                "verdict": "PASS",
                "criterion_results": [
                    {
                        "criterion_id": "C1",
                        "verdict": "PASS",
                        "evidence_ids": [],
                        "reason": "订单幂等全局验收",
                    }
                ],
                "evidence_ids": [],
                "reason": "e2e5 finalization",
            }
        ],
        "evidence_ids": [],
    }


def test_e2e5_finalization_cas_crash_single_release_x10(api, objects, tmp_path):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_finalize(api, objects, tmp_path, run_id="e2e5-fin-cas")
    run = ctx["run"]
    seed = ctx["seed"]
    client, auth, goal = ctx["client"], ctx["auth"], ctx["goal"]
    token = ctx["token"]
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    client.app.state.objects = ctx["store"]

    seed.apply_reference_fix(run.executor_worktree)
    assert run_pytest(run.executor_worktree, "tests/").returncode == 0

    step = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e5 seal for fin cas",
            "tool_ref": "seal_candidate",
        },
        headers=ctx["exec_auth"],
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": [ctx["mechanical_id"]]},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e5-fin-seal",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "seal_candidate",
            "input_artifact_id": str(input_art.id),
        },
        headers=ctx["exec_auth"],
    )
    assert prepared.status_code == 201, prepared.text
    sealed = run_seal_candidate_effect(
        client,
        worker_auth=ctx["exec_auth"],
        lease=ctx["exec_lease"]["lease"],
        effect=prepared.json()["data"],
        project_id=ctx["project_id"],
        workspace_root=run.executor_worktree,
        allowed_paths=ALLOW,
        input_bytes=input_blob,
    )
    assert sealed.get("seal_error") is None, sealed.get("seal_error")
    candidate = sealed["candidate"]

    mat_root = run.artifacts / "auditor_materialized"
    if mat_root.exists():
        shutil.rmtree(mat_root)

    def read_bytes(digest: str) -> bytes:
        return ctx["store"].read(UUID(str(ctx["project_id"])), digest)

    materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    attach_holdout_tests(mat_root, _HOLDOUT)
    assert run_pytest(mat_root, "tests/", "tests_hidden/").returncode == 0

    activity_now = client.get(
        f"/api/v1/activities/{ctx['exec_lease']['activity']['id']}",
        headers=auth,
    ).json()["data"]
    assert (
        client.post(
            f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/outcomes",
            json={
                "lease": ctx["exec_lease"]["lease"],
                "expected_state_revision": activity_now["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "evidence_ids": sealed["result_artifact_ids"],
                },
            },
            headers=ctx["exec_auth"],
        ).status_code
        == 200
    )
    task_id = ctx["exec_lease"]["activity"]["task_id"]

    audit_subject = str(uuid4())
    _register_worker(audit_subject, kinds=("AUDIT",))
    audit_auth = {"Authorization": "Bearer " + token(audit_subject, ["worker"])}
    audit_lease = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**audit_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    assignment = audit_lease["activity"]["verification_assignments"][0]
    binding = audit_lease["activity"]["binding"]
    receipt_id = receipt_artifact(ctx["engine"], ctx["store"], ctx["project_id"])
    verifier_digest = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), assignment["verification_profile_id"]
    )
    pass_run_id = post_verification_run(
        client,
        audit_auth,
        audit_lease,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=verifier_digest,
        receipt_id=receipt_id,
        engine=ctx["engine"],
    )
    assert (
        client.post(
            f"/internal/v1/activities/{audit_lease['activity']['id']}/outcomes",
            json={
                "lease": audit_lease["lease"],
                "expected_state_revision": audit_lease["activity"]["state_revision"],
                "outcome": {
                    "target_type": "CANDIDATE",
                    "audit": {
                        "subject_candidate_manifest_id": candidate["id"],
                        "goal_contract_revision": binding["goal_contract_revision"],
                        "task_contract_revision": binding["task_contract_revision"],
                        "verification_profile_id": assignment["verification_profile_id"],
                        "layer": assignment["layer"],
                        "audit_round": assignment["audit_round"],
                        "verifier_run_ids": [pass_run_id],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "A1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "ok",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "e2e5 audit pass",
                    },
                },
            },
            headers=audit_auth,
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/tasks/{task_id}", headers=auth).json()["data"][
        "status"
    ] == "DONE"

    integ_subject = str(uuid4())
    _register_worker(integ_subject, kinds=("INTEGRATE", "FINALIZE"))
    integ_auth = {"Authorization": "Bearer " + token(integ_subject, ["worker"])}
    integ = client.post(
        "/internal/v1/claims",
        json={"kinds": ["INTEGRATE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    integration_commit = candidate.get("git_commit") or ("b" * 40)
    assert (
        client.post(
            f"/internal/v1/activities/{integ['activity']['id']}/outcomes",
            json={
                "lease": integ["lease"],
                "expected_state_revision": integ["activity"]["state_revision"],
                "outcome": {
                    "candidate_manifest_id": candidate["id"],
                    "integration_commit": integration_commit,
                    "evidence_ids": [],
                },
            },
            headers=integ_auth,
        ).status_code
        == 200
    )
    goal_verifying = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()[
        "data"
    ]
    assert goal_verifying["status"] == "VERIFYING"
    assert goal_verifying["barrier"]["status"] == "SEALED"
    barrier_id = goal_verifying["barrier"]["id"]

    fin = client.post(
        "/internal/v1/claims",
        json={"kinds": ["FINALIZE"], "capabilities": []},
        headers={**integ_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    assert fin["activity"]["kind"] == "FINALIZE"
    fin_assign = fin["activity"]["verification_assignments"][0]
    fin_receipt = receipt_artifact(ctx["engine"], ctx["store"], ctx["project_id"])
    fin_verifier = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), fin_assign["verification_profile_id"]
    )
    fin_run_id = post_verification_run(
        client,
        integ_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=fin_verifier,
        receipt_id=fin_receipt,
        criterion_id="C1",
        engine=ctx["engine"],
    )
    fin_activity_id = fin["activity"]["id"]
    fin_attempt_id = fin["lease"]["attempt_id"]

    # CAS 前断线：VerificationRun 已登记，FINALIZE outcome 未提交
    _force_expire(engine, fin_attempt_id)
    _trigger_expire_scan(client, token)
    assert _release_count(engine, goal["id"]) == 0
    assert client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
        "status"
    ] == "VERIFYING"

    outcome_body = _finalize_outcome_body(
        goal, candidate, barrier_id, fin, fin_run_id, fin_assign
    )
    for _ in range(_CRASH_ROUNDS):
        forged = client.post(
            f"/internal/v1/activities/{fin_activity_id}/outcomes",
            json={
                "lease": fin["lease"],
                "expected_state_revision": fin["activity"]["state_revision"],
                "outcome": outcome_body,
            },
            headers=integ_auth,
        )
        assert forged.status_code in (400, 409, 422), forged.text
        assert _release_count(engine, goal["id"]) == 0
        st = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"][
            "status"
        ]
        assert st == "VERIFYING"  # 未 DONE，更无「终态复活」

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"id": fin_attempt_id},
            )
            .mappings()
            .one()
        )
    _confirm_stop(client, integ_auth, str(stop["id"]), fin_attempt_id)

    # 重试：唯一一次成功 FINALIZE → 至多一个 ReleaseManifest
    fin2_subject = str(uuid4())
    _register_worker(fin2_subject, kinds=("FINALIZE",))
    fin2_auth = {"Authorization": "Bearer " + token(fin2_subject, ["worker"])}
    fin2 = client.post(
        "/internal/v1/claims",
        json={"kinds": ["FINALIZE"], "capabilities": []},
        headers={**fin2_auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]
    assert fin2["activity"]["id"] == fin_activity_id
    fin2_assign = fin2["activity"]["verification_assignments"][0]
    fin2_receipt = receipt_artifact(ctx["engine"], ctx["store"], ctx["project_id"])
    fin2_verifier = profile_verifier_digest(
        client, auth, str(ctx["project_id"]), fin2_assign["verification_profile_id"]
    )
    fin2_run = post_verification_run(
        client,
        fin2_auth,
        fin2,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=fin2_verifier,
        receipt_id=fin2_receipt,
        criterion_id="C1",
        engine=ctx["engine"],
    )
    ok = client.post(
        f"/internal/v1/activities/{fin2['activity']['id']}/outcomes",
        json={
            "lease": fin2["lease"],
            "expected_state_revision": fin2["activity"]["state_revision"],
            "outcome": _finalize_outcome_body(
                goal, candidate, barrier_id, fin2, fin2_run, fin2_assign
            ),
        },
        headers=fin2_auth,
    )
    assert ok.status_code == 200, ok.text
    goal_done = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_done["status"] == "DONE"
    assert _release_count(engine, goal["id"]) == 1
    release_id = goal_done["release_manifest_id"]
    assert release_id is not None

    # DONE 后 ×10 再冲 outcome / 重领：Release 仍为 1，Goal 保持 DONE（无终态复活）
    for _ in range(_CRASH_ROUNDS):
        again = client.post(
            f"/internal/v1/activities/{fin2['activity']['id']}/outcomes",
            json={
                "lease": fin2["lease"],
                "expected_state_revision": fin2["activity"]["state_revision"],
                "outcome": _finalize_outcome_body(
                    goal, candidate, barrier_id, fin2, fin2_run, fin2_assign
                ),
            },
            headers=fin2_auth,
        )
        assert again.status_code in (200, 400, 409, 422)
        g = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
        assert g["status"] == "DONE"
        assert g["release_manifest_id"] == release_id
        assert _release_count(engine, goal["id"]) == 1

    release = client.get(f"/api/v1/goals/{goal['id']}/release", headers=auth)
    assert release.status_code == 200, release.text
    assert release.json()["data"]["validity"]["status"] == "VALID"
    assert release.json()["data"]["manifest"]["id"] == release_id

    summary = {
        "phase": "E2E-5-finalization-cas-crash",
        "run_id": run.run_id,
        "goal_id": goal["id"],
        "release_manifest_count": 1,
        "release_manifest_id": release_id,
        "crash_rounds": _CRASH_ROUNDS,
        "marks_goal_done": True,
        "done_via": "kernel_finalize_after_cas_crash_retry",
        "non_goals": [
            "official_loop_autonomous",
            "full_profile_layers",
        ],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    engine.dispose()
