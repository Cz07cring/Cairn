"""Issue #22：Goal DONE 与 late Stop/UNKNOWN 的 Goal 级 admission 互斥。"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from control_kernel.protocols.runtime import PlanRejected
from control_kernel.storage.claims import expire_stale_leases
from control_kernel.storage.goals import acquire_goal_admission_lock
from control_kernel.storage.obligations import assert_goal_ready_for_done
from sqlalchemy import create_engine, text
from test_effects import _publish_and_claim_execute


def _engine():
    return create_engine(os.environ["RING_TEST_DATABASE_URL"])


def _seed_expired_execute_with_dispatched(api, objects):
    """RUNNING Goal + EXECUTE ACTIVE 租约已过期 + DISPATCHED effect（供 expire 升 UNKNOWN）。"""
    store, _, _ = objects
    client, _token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = UUID(exec_lease["activity"]["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    project_id = UUID(exec_lease["activity"]["project_id"])
    goal_id = UUID(goal["id"])

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "admission-lock-seed",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    import hashlib
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts

    blob = b'{"path":"src/admission.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:admission",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
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

    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE activity_attempts
                SET lease_expires_at=:past, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )
    eng.dispose()
    return {
        "client": client,
        "auth": auth,
        "goal_id": goal_id,
        "activity_id": activity_id,
        "attempt_id": attempt_id,
        "effect_id": UUID(effect["id"]),
    }


def test_expire_before_done_blocks_assert_goal_ready(api, objects):
    """late Stop/UNKNOWN 先提交 → assert_goal_ready_for_done 失败关闭。"""
    seed = _seed_expired_execute_with_dispatched(api, objects)
    eng = _engine()
    with eng.begin() as db:
        n = expire_stale_leases(db)
    assert n >= 1
    with eng.begin() as db:
        with pytest.raises(PlanRejected, match="STOP_UNCONFIRMED|GOAL_DONE_BLOCKED"):
            assert_goal_ready_for_done(db, seed["goal_id"])
        stop_n = db.execute(
            text(
                """SELECT count(*) FROM stops
                WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
            ),
            {"id": seed["attempt_id"]},
        ).scalar_one()
        effect_status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": seed["effect_id"]},
        ).scalar_one()
    eng.dispose()
    assert stop_n == 1
    assert effect_status == "UNKNOWN"


def test_expire_after_done_writes_no_stop_or_unknown(api, objects):
    """Goal 已 DONE：expire 不得再写 Stop/UNKNOWN（零新增权威事实）。"""
    seed = _seed_expired_execute_with_dispatched(api, objects)
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='DONE', previous_status='RUNNING',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": seed["goal_id"]},
        )
        n = expire_stale_leases(db)
        assert n == 0
        stop_n = db.execute(
            text(
                """SELECT count(*) FROM stops
                WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
            ),
            {"id": seed["attempt_id"]},
        ).scalar_one()
        attempt_status = db.execute(
            text("SELECT status FROM activity_attempts WHERE id=:id"),
            {"id": seed["attempt_id"]},
        ).scalar_one()
        effect_status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": seed["effect_id"]},
        ).scalar_one()
    eng.dispose()
    assert stop_n == 0
    assert attempt_status == "ACTIVE"
    assert effect_status == "DISPATCHED"


def test_admission_lock_blocks_expire_during_done_check_window(api, objects):
    """持 admission 做就绪检查期间 expire 不得插入 Stop；提交 DONE 后 expire 跳过。

    种子：无未决 effect（否则 assert_ready 本身失败）；仅过期 ACTIVE 租约，
    expire 若抢跑会写入 LEASE_EXPIRED Stop。
    """
    client, _token, _auth, goal, _worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    goal_id = UUID(goal["id"])
    attempt_id = UUID(exec_lease["lease"]["attempt_id"])
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE activity_attempts
                SET lease_expires_at=:past, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": attempt_id, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )
    eng.dispose()

    holding = threading.Event()
    errors: list[BaseException] = []
    outcomes: list[str] = []

    def checker() -> None:
        try:
            local = _engine()
            with local.begin() as db:
                acquire_goal_admission_lock(db, goal_id)
                assert_goal_ready_for_done(db, goal_id)
                holding.set()
                # 给 expire 线程时间阻塞在同一 admission 锁上
                time.sleep(0.4)
                assert_goal_ready_for_done(db, goal_id)
                db.execute(
                    text(
                        """UPDATE goals SET status='DONE', previous_status='RUNNING',
                            updated_at=clock_timestamp()
                        WHERE id=:id"""
                    ),
                    {"id": goal_id},
                )
                outcomes.append("checker_done")
            local.dispose()
        except BaseException as exc:  # noqa: BLE001 — 线程内收集
            errors.append(exc)

    def expirer() -> None:
        try:
            assert holding.wait(timeout=5)
            local = _engine()
            with local.begin() as db:
                n = expire_stale_leases(db)
                outcomes.append(f"expire:{n}")
            local.dispose()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=checker)
    t2 = threading.Thread(target=expirer)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not errors, errors
    assert "checker_done" in outcomes
    assert any(o.startswith("expire:") for o in outcomes)

    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        stop_n = db.execute(
            text(
                """SELECT count(*) FROM stops
                WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
            ),
            {"id": attempt_id},
        ).scalar_one()
        attempt_status = db.execute(
            text("SELECT status FROM activity_attempts WHERE id=:id"),
            {"id": attempt_id},
        ).scalar_one()
    eng.dispose()
    assert status == "DONE"
    assert stop_n == 0
    assert attempt_status == "ACTIVE"
    # client 仅保活夹具引用，避免未使用告警
    assert client is not None


def test_effect_receipt_rejected_after_goal_done(api, objects):
    """第138批：Goal DONE 后新 effect 回执零副作用（幂等旧回执除外）。"""
    from datetime import UTC, datetime
    from uuid import uuid4

    from control_kernel.protocols.effects import TrustedReceipt
    from control_kernel.storage.effects import apply_effect_receipt

    seed = _seed_expired_execute_with_dispatched(api, objects)
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE goals SET status='DONE', previous_status='RUNNING',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": seed["goal_id"]},
        )
        subject = db.execute(
            text(
                """SELECT w.subject FROM activity_attempts att
                JOIN workers w ON w.id = att.worker_id
                WHERE att.id=:id"""
            ),
            {"id": seed["attempt_id"]},
        ).scalar_one()
        epoch = db.execute(
            text("SELECT fencing_epoch FROM activity_attempts WHERE id=:id"),
            {"id": seed["attempt_id"]},
        ).scalar_one()

    body = TrustedReceipt(
        receipt_id=uuid4(),
        effect_id=seed["effect_id"],
        producer_activity_id=seed["activity_id"],
        producer_attempt_id=seed["attempt_id"],
        fencing_epoch=str(epoch),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        exit_code=0,
        timed_out=False,
        result_artifact_ids=[],
        observed_outcome="SUCCEEDED",
    )

    with pytest.raises(PlanRejected, match="GOAL_ENGINEERING_CLOSED"):
        apply_effect_receipt(eng, subject, seed["effect_id"], body)

    with eng.begin() as db:
        n = db.execute(
            text("SELECT count(*) FROM effect_receipts WHERE effect_id=:id"),
            {"id": seed["effect_id"]},
        ).scalar_one()
        status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": seed["effect_id"]},
        ).scalar_one()
    eng.dispose()
    assert n == 0
    assert status == "DISPATCHED"


def test_stop_receipt_rejected_after_goal_done(api, objects):
    """第188批：Goal DONE 后新 Stop 回执零副作用（不得 CONFIRMED/释放隔离）；≠ 业务 DONE。"""
    from control_kernel.protocols.runtime import StopReceipt
    from control_kernel.storage.stops import apply_stop_receipt

    seed = _seed_expired_execute_with_dispatched(api, objects)
    eng = _engine()
    with eng.begin() as db:
        n = expire_stale_leases(db)
        assert n >= 1
        stop_id = db.execute(
            text(
                """SELECT id FROM stops
                WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
            ),
            {"id": seed["attempt_id"]},
        ).scalar_one()
        subject = db.execute(
            text(
                """SELECT w.subject FROM activity_attempts att
                JOIN workers w ON w.id = att.worker_id
                WHERE att.id=:id"""
            ),
            {"id": seed["attempt_id"]},
        ).scalar_one()
        # 夹具：业务已终态后迟到 EXITED 不得再推进 Stop / 释放资源
        db.execute(
            text(
                """UPDATE goals SET status='DONE', previous_status='RUNNING',
                    updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": seed["goal_id"]},
        )
        db.execute(
            text(
                """UPDATE resource_reservations
                SET status='QUARANTINED', updated_at=clock_timestamp()
                WHERE attempt_id=:id AND status='HELD'"""
            ),
            {"id": seed["attempt_id"]},
        )

    body = StopReceipt(
        receipt_id=uuid4(),
        stop_id=stop_id,
        activation_id=seed["attempt_id"],
        attempt_id=seed["attempt_id"],
        resource_instance_id=seed["attempt_id"],
        observed_at=datetime.now(UTC),
        observation="EXITED",
        compute_released=True,
        write_capability_revoked=True,
        proof_artifact_ids=[],
    )
    with pytest.raises(PlanRejected, match="GOAL_ENGINEERING_CLOSED|GOAL_STOP_CLOSED"):
        apply_stop_receipt(
            eng,
            subject,
            roles={"worker"},
            project_ids=[],
            stop_id=stop_id,
            body=body,
        )

    with eng.begin() as db:
        stop_status = db.execute(
            text("SELECT status FROM stops WHERE id=:id"),
            {"id": stop_id},
        ).scalar_one()
        receipt_n = db.execute(
            text("SELECT count(*) FROM stop_receipts WHERE stop_id=:id"),
            {"id": stop_id},
        ).scalar_one()
        res_status = db.execute(
            text(
                """SELECT status FROM resource_reservations
                WHERE attempt_id=:id ORDER BY updated_at DESC LIMIT 1"""
            ),
            {"id": seed["attempt_id"]},
        ).scalar()
    eng.dispose()
    assert stop_status == "REQUESTED"
    assert receipt_n == 0
    assert res_status in (None, "QUARANTINED", "HELD")
    assert res_status != "RELEASED"


def test_admission_lock_stress_100x_finalize_vs_expire(api, objects):
    """第144批：Issue #22 — finalize 窗口 × expire 并发至少 100 次，错误 DONE=0、死锁=0。

    每次独立 Goal：持 admission 就绪检查期间 expire 阻塞；合法串行后要么
    Goal DONE 且无 LEASE_EXPIRED Stop，要么（若本轮未走到 DONE）不得出现
    「已 DONE 却仍写入 Stop/UNKNOWN」的权威冲突。
    """
    iters = int(os.environ.get("RING_ADMISSION_STRESS_ITERS", "100"))
    bad_done = 0
    deadlocks = 0
    other_errors: list[str] = []

    for i in range(iters):
        client, _token, _auth, goal, _worker_auth, exec_lease = (
            _publish_and_claim_execute(api, objects)
        )
        goal_id = UUID(goal["id"])
        attempt_id = UUID(exec_lease["lease"]["attempt_id"])
        eng = _engine()
        with eng.begin() as db:
            db.execute(
                text(
                    """UPDATE activity_attempts
                    SET lease_expires_at=:past, updated_at=clock_timestamp()
                    WHERE id=:id"""
                ),
                {
                    "id": attempt_id,
                    "past": datetime.now(UTC) - timedelta(seconds=5),
                },
            )
        eng.dispose()

        holding = threading.Event()
        errors: list[BaseException] = []
        outcomes: list[str] = []

        def checker(
            *,
            gid: UUID = goal_id,
            hold: threading.Event = holding,
            outs: list[str] = outcomes,
            errs: list[BaseException] = errors,
        ) -> None:
            try:
                local = _engine()
                with local.begin() as db:
                    acquire_goal_admission_lock(db, gid)
                    assert_goal_ready_for_done(db, gid)
                    hold.set()
                    time.sleep(0.05)
                    assert_goal_ready_for_done(db, gid)
                    db.execute(
                        text(
                            """UPDATE goals SET status='DONE', previous_status='RUNNING',
                                updated_at=clock_timestamp()
                            WHERE id=:id"""
                        ),
                        {"id": gid},
                    )
                    outs.append("checker_done")
                local.dispose()
            except BaseException as caught:  # noqa: BLE001
                errs.append(caught)

        def expirer(
            *,
            hold: threading.Event = holding,
            outs: list[str] = outcomes,
            errs: list[BaseException] = errors,
        ) -> None:
            try:
                assert hold.wait(timeout=10)
                local = _engine()
                with local.begin() as db:
                    n = expire_stale_leases(db)
                    outs.append(f"expire:{n}")
                local.dispose()
            except BaseException as caught:  # noqa: BLE001
                errs.append(caught)

        t1 = threading.Thread(target=checker, name=f"admit-check-{i}")
        t2 = threading.Thread(target=expirer, name=f"admit-expire-{i}")
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        if t1.is_alive() or t2.is_alive():
            deadlocks += 1
            other_errors.append(f"iter={i} thread hang")
            continue

        for exc in errors:
            msg = str(exc).lower()
            if "deadlock" in msg:
                deadlocks += 1
            else:
                other_errors.append(f"iter={i} {type(exc).__name__}: {exc}")

        eng = _engine()
        with eng.begin() as db:
            status = db.execute(
                text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
            ).scalar_one()
            stop_n = int(
                db.execute(
                    text(
                        """SELECT count(*) FROM stops
                        WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                    ),
                    {"id": attempt_id},
                ).scalar_one()
            )
            attempt_status = db.execute(
                text("SELECT status FROM activity_attempts WHERE id=:id"),
                {"id": attempt_id},
            ).scalar_one()
        eng.dispose()

        # 安全串行：DONE ⇒ 不得有 late Stop；非 DONE 时不得假装成功关闭
        if status == "DONE":
            if stop_n != 0 or attempt_status != "ACTIVE":
                bad_done += 1
                other_errors.append(
                    f"iter={i} DONE with stop_n={stop_n} attempt={attempt_status}"
                )
        elif "checker_done" in outcomes:
            bad_done += 1
            other_errors.append(f"iter={i} checker_done but status={status}")

        assert client is not None

    assert deadlocks == 0, f"deadlocks={deadlocks} detail={other_errors[:5]}"
    assert bad_done == 0, f"bad_done={bad_done} detail={other_errors[:5]}"
    assert not other_errors, other_errors[:8]


def test_submit_finalize_outcome_races_expire_under_admission(api, objects):
    """第145批：真实 submit_finalize_outcome × expire 在 admission 下安全串行。

    同 Goal 另挂一条已过期 ACTIVE attempt：若 expire 先入锁则写入 Stop 并挡 DONE；
    若 finalize 先合法 DONE，则 expire 对终态 Goal 零新增 Stop。
    """
    from control_kernel.protocols.plans import ActivityOutcomeRequest
    from control_kernel.protocols.runtime import PlanRejected
    from control_kernel.storage.claims import binding_digest_of
    from control_kernel.storage.finalization import submit_finalize_outcome
    from test_finalization import _claim_finalize_lease
    from verification_run_helpers import (
        post_verification_run,
        profile_verifier_digest,
        receipt_artifact,
    )

    client, _token, auth, goal, candidate, worker_auth, fin, barrier_id, store = (
        _claim_finalize_lease(api, objects)
    )
    fin_assign = fin["activity"]["verification_assignments"][0]
    fin_project_id = UUID(fin["activity"]["project_id"])
    client.app.state.objects = store
    fin_receipt_id = receipt_artifact(client.app.state.engine, store, fin_project_id)
    fin_verifier_digest = profile_verifier_digest(
        client, auth, str(fin_project_id), fin_assign["verification_profile_id"]
    )
    fin_run_id = post_verification_run(
        client,
        worker_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=fin_verifier_digest,
        receipt_id=fin_receipt_id,
        criterion_id="C1",
    )

    goal_id = UUID(goal["id"])
    project_id = fin_project_id
    stale_activity_id = uuid4()
    stale_attempt_id = uuid4()

    eng = _engine()
    with eng.begin() as db:
        exe = (
            db.execute(
                text(
                    """SELECT * FROM activities
                    WHERE goal_id=:goal AND kind='EXECUTE'
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .first()
        )
        assert exe is not None
        worker_id = db.execute(
            text("SELECT worker_id FROM activity_attempts WHERE id=:id"),
            {"id": UUID(fin["lease"]["attempt_id"])},
        ).scalar_one()
        subject = db.execute(
            text("SELECT subject FROM workers WHERE id=:id"),
            {"id": worker_id},
        ).scalar_one()
        binding = exe["binding"]
        if isinstance(binding, str):
            binding = json.loads(binding)
        resources = exe["resources"]
        if not isinstance(resources, str):
            resources = json.dumps(resources)
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:scope,'EXECUTE',:ttype,:tid,
                  CAST(:binding AS jsonb),'[]'::jsonb,'RUNNING',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": stale_activity_id,
                "project": project_id,
                "goal": goal_id,
                "task": exe["task_id"],
                "scope": exe["budget_scope_id"],
                "ttype": exe["target_type"],
                "tid": exe["target_id"],
                "binding": json.dumps(binding),
                "resources": resources,
            },
        )
        db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,project_id,activity_id,worker_id,binding_digest,fencing_epoch,status,
                  lease_expires_at,renewal_seq,skill_versions,started_at)
                VALUES(
                  :id,:project,:activity,:worker,:binding,1,'ACTIVE',
                  :past,0,'[]'::jsonb,clock_timestamp())"""
            ),
            {
                "id": stale_attempt_id,
                "project": project_id,
                "activity": stale_activity_id,
                "worker": worker_id,
                "binding": binding_digest_of(binding or {}),
                "past": datetime.now(UTC) - timedelta(seconds=5),
            },
        )
    eng.dispose()

    body = ActivityOutcomeRequest.model_validate(
        {
            "lease": fin["lease"],
            "expected_state_revision": fin["activity"]["state_revision"],
            "outcome": {
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
                                "reason": "全局通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "最终验收通过",
                    }
                ],
                "evidence_ids": [],
            },
        }
    )

    holding = threading.Event()
    errors: list[BaseException] = []
    outcomes: list[str] = []
    engine = client.app.state.engine
    activity_id = UUID(fin["activity"]["id"])

    def finalizer(
        *,
        hold: threading.Event = holding,
        outs: list[str] = outcomes,
        errs: list[BaseException] = errors,
    ) -> None:
        try:
            # 先占 admission：在 storage 入口内锁；此处用显式锁模拟「已进入就绪检查窗口」
            # 真实路径由 submit_finalize_outcome 自行取锁——与 expire 对撞。
            submit_finalize_outcome(engine, subject, activity_id, body)
            outs.append("finalize_ok")
            hold.set()
        except BaseException as caught:  # noqa: BLE001
            outs.append(f"finalize_err:{type(caught).__name__}")
            errs.append(caught)
            hold.set()

    def expirer(
        *,
        hold: threading.Event = holding,
        outs: list[str] = outcomes,
        errs: list[BaseException] = errors,
    ) -> None:
        try:
            # 与 finalizer 几乎同时启动；admission 串行化两者
            local = _engine()
            with local.begin() as db:
                n = expire_stale_leases(db)
                outs.append(f"expire:{n}")
            local.dispose()
            hold.set()
        except BaseException as caught:  # noqa: BLE001
            errs.append(caught)
            hold.set()

    t1 = threading.Thread(target=finalizer, name="finalize-real")
    t2 = threading.Thread(target=expirer, name="expire-vs-finalize")
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), outcomes
    assert any(o.startswith("expire:") for o in outcomes), outcomes

    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        stop_n = int(
            db.execute(
                text(
                    """SELECT count(*) FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": stale_attempt_id},
            ).scalar_one()
        )
        barrier_status = db.execute(
            text(
                """SELECT status FROM finalization_barriers
                WHERE goal_id=:goal ORDER BY created_at DESC LIMIT 1"""
            ),
            {"goal": goal_id},
        ).scalar_one()
    eng.dispose()

    if "finalize_ok" in outcomes:
        assert status == "DONE"
        assert barrier_status == "RELEASED"
        # finalize 先提交：终态后 expire 不得对 stale attempt 写 Stop
        assert stop_n == 0
        assert not any(isinstance(e, PlanRejected) for e in errors)
    else:
        # expire 先写入 Stop → finalize 失败关闭，Goal 非 DONE、屏障未 RELEASED
        assert status != "DONE"
        assert barrier_status == "SEALED"
        assert stop_n == 1
        assert any(
            isinstance(e, PlanRejected) or "STOP_UNCONFIRMED" in str(e) or "GOAL_DONE" in str(e)
            for e in errors
        ), (outcomes, errors)


def test_expire_before_submit_finalize_blocks_done(api, objects):
    """第145批：late Stop 先落盘时真实 finalize 不得写 DONE / RELEASED。"""
    from control_kernel.protocols.plans import ActivityOutcomeRequest
    from control_kernel.protocols.runtime import PlanRejected
    from control_kernel.storage.claims import binding_digest_of
    from control_kernel.storage.finalization import submit_finalize_outcome
    from test_finalization import _claim_finalize_lease
    from verification_run_helpers import (
        post_verification_run,
        profile_verifier_digest,
        receipt_artifact,
    )

    client, _token, auth, goal, candidate, worker_auth, fin, barrier_id, store = (
        _claim_finalize_lease(api, objects)
    )
    fin_assign = fin["activity"]["verification_assignments"][0]
    fin_project_id = UUID(fin["activity"]["project_id"])
    client.app.state.objects = store
    fin_run_id = post_verification_run(
        client,
        worker_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=profile_verifier_digest(
            client, auth, str(fin_project_id), fin_assign["verification_profile_id"]
        ),
        receipt_id=receipt_artifact(client.app.state.engine, store, fin_project_id),
        criterion_id="C1",
    )

    goal_id = UUID(goal["id"])
    stale_activity_id = uuid4()
    stale_attempt_id = uuid4()
    eng = _engine()
    with eng.begin() as db:
        exe = (
            db.execute(
                text(
                    """SELECT * FROM activities
                    WHERE goal_id=:goal AND kind='EXECUTE'
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        worker_id = db.execute(
            text("SELECT worker_id FROM activity_attempts WHERE id=:id"),
            {"id": UUID(fin["lease"]["attempt_id"])},
        ).scalar_one()
        subject = db.execute(
            text("SELECT subject FROM workers WHERE id=:id"), {"id": worker_id}
        ).scalar_one()
        binding = exe["binding"]
        if isinstance(binding, str):
            binding = json.loads(binding)
        resources = exe["resources"]
        if not isinstance(resources, str):
            resources = json.dumps(resources)
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:scope,'EXECUTE',:ttype,:tid,
                  CAST(:binding AS jsonb),'[]'::jsonb,'RUNNING',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": stale_activity_id,
                "project": fin_project_id,
                "goal": goal_id,
                "task": exe["task_id"],
                "scope": exe["budget_scope_id"],
                "ttype": exe["target_type"],
                "tid": exe["target_id"],
                "binding": json.dumps(binding),
                "resources": resources,
            },
        )
        db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,project_id,activity_id,worker_id,binding_digest,fencing_epoch,status,
                  lease_expires_at,renewal_seq,skill_versions,started_at)
                VALUES(
                  :id,:project,:activity,:worker,:binding,1,'ACTIVE',
                  :past,0,'[]'::jsonb,clock_timestamp())"""
            ),
            {
                "id": stale_attempt_id,
                "project": fin_project_id,
                "activity": stale_activity_id,
                "worker": worker_id,
                "binding": binding_digest_of(binding or {}),
                "past": datetime.now(UTC) - timedelta(seconds=5),
            },
        )
        n = expire_stale_leases(db)
        assert n >= 1
    eng.dispose()

    body = ActivityOutcomeRequest.model_validate(
        {
            "lease": fin["lease"],
            "expected_state_revision": fin["activity"]["state_revision"],
            "outcome": {
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
                                "reason": "全局通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "最终验收通过",
                    }
                ],
                "evidence_ids": [],
            },
        }
    )
    with pytest.raises(PlanRejected, match="STOP_UNCONFIRMED|GOAL_DONE_BLOCKED"):
        submit_finalize_outcome(
            client.app.state.engine,
            subject,
            UUID(fin["activity"]["id"]),
            body,
        )

    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        barrier_status = db.execute(
            text(
                """SELECT status FROM finalization_barriers
                WHERE goal_id=:goal ORDER BY created_at DESC LIMIT 1"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        release_n = int(
            db.execute(
                text("SELECT count(*) FROM release_manifests WHERE goal_id=:goal"),
                {"goal": goal_id},
            ).scalar_one()
        )
    eng.dispose()
    assert status == "VERIFYING"
    assert barrier_status == "SEALED"
    assert release_n == 0


def _finalize_ready_ctx(api, objects):
    """FINALIZE 已 claim + VerificationRun 已登记，尚未 submit outcome。"""
    from test_finalization import _claim_finalize_lease
    from verification_run_helpers import (
        post_verification_run,
        profile_verifier_digest,
        receipt_artifact,
    )

    client, token, auth, goal, candidate, worker_auth, fin, barrier_id, store = (
        _claim_finalize_lease(api, objects)
    )
    fin_assign = fin["activity"]["verification_assignments"][0]
    fin_project_id = UUID(fin["activity"]["project_id"])
    client.app.state.objects = store
    fin_run_id = post_verification_run(
        client,
        worker_auth,
        fin,
        subject_id=candidate["id"],
        subject_digest=candidate["content_digest"],
        verifier_digest=profile_verifier_digest(
            client, auth, str(fin_project_id), fin_assign["verification_profile_id"]
        ),
        receipt_id=receipt_artifact(client.app.state.engine, store, fin_project_id),
        criterion_id="C1",
    )
    return {
        "client": client,
        "token": token,
        "auth": auth,
        "goal": goal,
        "candidate": candidate,
        "worker_auth": worker_auth,
        "fin": fin,
        "barrier_id": barrier_id,
        "store": store,
        "fin_assign": fin_assign,
        "fin_project_id": fin_project_id,
        "fin_run_id": fin_run_id,
    }


def _finalize_outcome_body(ctx) -> object:
    from control_kernel.protocols.plans import ActivityOutcomeRequest

    goal = ctx["goal"]
    fin = ctx["fin"]
    fin_assign = ctx["fin_assign"]
    return ActivityOutcomeRequest.model_validate(
        {
            "lease": fin["lease"],
            "expected_state_revision": fin["activity"]["state_revision"],
            "outcome": {
                "barrier_id": ctx["barrier_id"],
                "candidate_manifest_id": ctx["candidate"]["id"],
                "global_audits": [
                    {
                        "subject_candidate_manifest_id": ctx["candidate"]["id"],
                        "goal_contract_revision": goal["contract_revision"],
                        "task_contract_revision": None,
                        "verification_profile_id": fin_assign["verification_profile_id"],
                        "layer": "GLOBAL",
                        "audit_round": fin_assign["audit_round"],
                        "verifier_run_ids": [ctx["fin_run_id"]],
                        "verdict": "PASS",
                        "criterion_results": [
                            {
                                "criterion_id": "C1",
                                "verdict": "PASS",
                                "evidence_ids": [],
                                "reason": "全局通过",
                            }
                        ],
                        "evidence_ids": [],
                        "reason": "最终验收通过",
                    }
                ],
                "evidence_ids": [],
            },
        }
    )


def _insert_dispatched_effect_under_goal(ctx) -> dict:
    """同 Goal 再挂 RUNNING EXECUTE + DISPATCHED effect（挡 DONE，可供 receipt）。"""
    import hashlib
    from io import BytesIO

    from control_kernel.storage.artifacts import Artifacts
    from control_kernel.storage.claims import binding_digest_of

    goal_id = UUID(ctx["goal"]["id"])
    project_id = ctx["fin_project_id"]
    fin = ctx["fin"]
    store = ctx["store"]
    engine = ctx["client"].app.state.engine
    stale_activity_id = uuid4()
    stale_attempt_id = uuid4()
    effect_id = uuid4()
    step_id = uuid4()

    blob = b'{"path":"src/finalize-race.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:finalize-receipt-race",
    )

    eng = _engine()
    with eng.begin() as db:
        exe = (
            db.execute(
                text(
                    """SELECT * FROM activities
                    WHERE goal_id=:goal AND kind='EXECUTE'
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        worker_id = db.execute(
            text("SELECT worker_id FROM activity_attempts WHERE id=:id"),
            {"id": UUID(fin["lease"]["attempt_id"])},
        ).scalar_one()
        subject = db.execute(
            text("SELECT subject FROM workers WHERE id=:id"), {"id": worker_id}
        ).scalar_one()
        binding = exe["binding"]
        if isinstance(binding, str):
            binding = json.loads(binding)
        resources = exe["resources"]
        if not isinstance(resources, str):
            resources = json.dumps(resources)
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:scope,'EXECUTE',:ttype,:tid,
                  CAST(:binding AS jsonb),'[]'::jsonb,'RUNNING',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": stale_activity_id,
                "project": project_id,
                "goal": goal_id,
                "task": exe["task_id"],
                "scope": exe["budget_scope_id"],
                "ttype": exe["target_type"],
                "tid": exe["target_id"],
                "binding": json.dumps(binding),
                "resources": resources,
            },
        )
        db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,project_id,activity_id,worker_id,binding_digest,fencing_epoch,status,
                  lease_expires_at,renewal_seq,skill_versions,started_at)
                VALUES(
                  :id,:project,:activity,:worker,:binding,1,'ACTIVE',
                  clock_timestamp() + interval '1 hour',0,'[]'::jsonb,clock_timestamp())"""
            ),
            {
                "id": stale_attempt_id,
                "project": project_id,
                "activity": stale_activity_id,
                "worker": worker_id,
                "binding": binding_digest_of(binding or {}),
            },
        )
        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,status,
                  input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,1,
                  :digest,'read_file','READ_ONLY','ENGINEERING','DISPATCHED',
                  :artifact,:attempt)"""
            ),
            {
                "id": effect_id,
                "project": project_id,
                "goal": goal_id,
                "activity": stale_activity_id,
                "step": step_id,
                "digest": "sha256:" + "a" * 64,
                "artifact": artifact.id,
                "attempt": stale_attempt_id,
            },
        )
    eng.dispose()
    return {
        "subject": subject,
        "activity_id": stale_activity_id,
        "attempt_id": stale_attempt_id,
        "effect_id": effect_id,
        "epoch": "1",
    }


def test_dispatched_effect_blocks_real_finalize(api, objects):
    """第146批：未决 DISPATCHED 存在时真实 finalize 不得 DONE。"""
    from control_kernel.storage.finalization import submit_finalize_outcome

    ctx = _finalize_ready_ctx(api, objects)
    planted = _insert_dispatched_effect_under_goal(ctx)
    body = _finalize_outcome_body(ctx)
    with pytest.raises(PlanRejected, match="GOAL_DONE_BLOCKED_UNSETTLED"):
        submit_finalize_outcome(
            ctx["client"].app.state.engine,
            planted["subject"],
            UUID(ctx["fin"]["activity"]["id"]),
            body,
        )
    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": UUID(ctx["goal"]["id"])},
        ).scalar_one()
        release_n = int(
            db.execute(
                text("SELECT count(*) FROM release_manifests WHERE goal_id=:goal"),
                {"goal": UUID(ctx["goal"]["id"])},
            ).scalar_one()
        )
    eng.dispose()
    assert status == "VERIFYING"
    assert release_n == 0


def test_submit_finalize_races_effect_receipt_under_admission(api, objects):
    """第146批：finalize × effect receipt 在 admission 下安全串行。

    DISPATCHED 未决时 finalize 必失败；receipt 先 SUCCEEDED 后 finalize 可 DONE。
    并发时由 admission 串行，不得出现 DONE 且 effect 仍 DISPATCHED。
    """
    from control_kernel.protocols.effects import TrustedReceipt
    from control_kernel.storage.effects import apply_effect_receipt
    from control_kernel.storage.finalization import submit_finalize_outcome

    ctx = _finalize_ready_ctx(api, objects)
    planted = _insert_dispatched_effect_under_goal(ctx)
    body = _finalize_outcome_body(ctx)
    engine = ctx["client"].app.state.engine
    fin_activity_id = UUID(ctx["fin"]["activity"]["id"])
    goal_id = UUID(ctx["goal"]["id"])

    receipt = TrustedReceipt(
        receipt_id=uuid4(),
        effect_id=planted["effect_id"],
        producer_activity_id=planted["activity_id"],
        producer_attempt_id=planted["attempt_id"],
        fencing_epoch=planted["epoch"],
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        exit_code=0,
        timed_out=False,
        result_artifact_ids=[],
        observed_outcome="SUCCEEDED",
    )

    outcomes: list[str] = []
    errors: list[BaseException] = []

    def finalizer(
        *,
        outs: list[str] = outcomes,
        errs: list[BaseException] = errors,
    ) -> None:
        try:
            submit_finalize_outcome(
                engine, planted["subject"], fin_activity_id, body
            )
            outs.append("finalize_ok")
        except BaseException as caught:  # noqa: BLE001
            outs.append(f"finalize_err:{type(caught).__name__}")
            errs.append(caught)

    def receiver(
        *,
        outs: list[str] = outcomes,
        errs: list[BaseException] = errors,
    ) -> None:
        try:
            apply_effect_receipt(
                engine, planted["subject"], planted["effect_id"], receipt
            )
            outs.append("receipt_ok")
        except BaseException as caught:  # noqa: BLE001
            outs.append(f"receipt_err:{type(caught).__name__}")
            errs.append(caught)

    t1 = threading.Thread(target=finalizer, name="finalize-vs-receipt")
    t2 = threading.Thread(target=receiver, name="receipt-vs-finalize")
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), outcomes

    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        effect_status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": planted["effect_id"]},
        ).scalar_one()
        release_n = int(
            db.execute(
                text("SELECT count(*) FROM release_manifests WHERE goal_id=:goal"),
                {"goal": goal_id},
            ).scalar_one()
        )
    eng.dispose()

    # 禁止：Goal DONE 却仍留下未决 DISPATCHED（错误 DONE）
    if status == "DONE":
        assert effect_status == "SUCCEEDED"
        assert release_n == 1
        assert "finalize_ok" in outcomes
        assert "receipt_ok" in outcomes
    else:
        assert status == "VERIFYING"
        assert release_n == 0
        # finalize 未成功时，effect 要么仍 DISPATCHED，要么已 SUCCEEDED（receipt 先到但 finalize 仍失败的其它原因）
        assert effect_status in ("DISPATCHED", "SUCCEEDED")
        assert "finalize_ok" not in outcomes


def test_unknown_effect_blocks_real_finalize(api, objects):
    """第147批：未决 UNKNOWN 存在时真实 finalize 不得 DONE。"""
    from control_kernel.storage.finalization import submit_finalize_outcome

    ctx = _finalize_ready_ctx(api, objects)
    planted = _insert_dispatched_effect_under_goal(ctx)
    eng = _engine()
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE effect_intents SET status='UNKNOWN', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": planted["effect_id"]},
        )
    eng.dispose()

    body = _finalize_outcome_body(ctx)
    with pytest.raises(PlanRejected, match="GOAL_DONE_BLOCKED_UNSETTLED"):
        submit_finalize_outcome(
            ctx["client"].app.state.engine,
            planted["subject"],
            UUID(ctx["fin"]["activity"]["id"]),
            body,
        )
    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"),
            {"id": UUID(ctx["goal"]["id"])},
        ).scalar_one()
        release_n = int(
            db.execute(
                text("SELECT count(*) FROM release_manifests WHERE goal_id=:goal"),
                {"goal": UUID(ctx["goal"]["id"])},
            ).scalar_one()
        )
    eng.dispose()
    assert status == "VERIFYING"
    assert release_n == 0


def test_reconcile_rejected_after_real_finalize_done(api, objects):
    """第147批：真实 finalize DONE 后禁止新对账命令（零新增 RECONCILE）。"""
    from control_kernel.storage.claims import binding_digest_of
    from test_finalization import _goal_done_with_release

    client, _token, auth, goal_done, _release_id, _candidate, _worker_auth = (
        _goal_done_with_release(api, objects)
    )
    goal_id = UUID(goal_done["id"])
    project_id = UUID(goal_done["project_id"])
    client.app.state.objects = objects[0]

    eng = _engine()
    with eng.begin() as db:
        exe = (
            db.execute(
                text(
                    """SELECT * FROM activities
                    WHERE goal_id=:goal AND kind='EXECUTE'
                    ORDER BY created_at DESC LIMIT 1"""
                ),
                {"goal": goal_id},
            )
            .mappings()
            .one()
        )
        worker_id = db.execute(
            text(
                """SELECT worker_id FROM activity_attempts
                WHERE activity_id=:aid ORDER BY created_at DESC LIMIT 1"""
            ),
            {"aid": exe["id"]},
        ).scalar_one()
        binding = exe["binding"]
        if isinstance(binding, str):
            binding = json.loads(binding)
        resources = exe["resources"]
        if not isinstance(resources, str):
            resources = json.dumps(resources)
        act_id = uuid4()
        att_id = uuid4()
        effect_id = uuid4()
        artifact_id = db.execute(
            text(
                """SELECT input_artifact_id FROM effect_intents
                WHERE goal_id=:goal LIMIT 1"""
            ),
            {"goal": goal_id},
        ).scalar_one()
        db.execute(
            text(
                """INSERT INTO activities(
                  id,project_id,goal_id,task_id,budget_scope_id,kind,target_type,target_id,
                  binding,verification_assignments,status,state_revision,depends_on_activity_ids,
                  retry_count,resources)
                VALUES(
                  :id,:project,:goal,:task,:scope,'EXECUTE',:ttype,:tid,
                  CAST(:binding AS jsonb),'[]'::jsonb,'RECOVERING',1,'{}',0,
                  CAST(:resources AS jsonb))"""
            ),
            {
                "id": act_id,
                "project": project_id,
                "goal": goal_id,
                "task": exe["task_id"],
                "scope": exe["budget_scope_id"],
                "ttype": exe["target_type"],
                "tid": exe["target_id"],
                "binding": json.dumps(binding),
                "resources": resources,
            },
        )
        db.execute(
            text(
                """INSERT INTO activity_attempts(
                  id,project_id,activity_id,worker_id,binding_digest,fencing_epoch,status,
                  lease_expires_at,renewal_seq,skill_versions,started_at,finished_at)
                VALUES(
                  :id,:project,:activity,:worker,:binding,1,'EXPIRED',
                  clock_timestamp() - interval '1 hour',0,'[]'::jsonb,
                  clock_timestamp() - interval '2 hour', clock_timestamp())"""
            ),
            {
                "id": att_id,
                "project": project_id,
                "activity": act_id,
                "worker": worker_id,
                "binding": binding_digest_of(binding or {}),
            },
        )
        db.execute(
            text(
                """INSERT INTO effect_intents(
                  id,project_id,goal_id,activity_id,logical_step_id,intent_revision,
                  payload_digest,tool_ref,replay_class,scope,status,
                  input_artifact_id,producer_attempt_id)
                VALUES(
                  :id,:project,:goal,:activity,:step,1,
                  :digest,'read_file','READ_ONLY','ENGINEERING','UNKNOWN',
                  :artifact,:attempt)"""
            ),
            {
                "id": effect_id,
                "project": project_id,
                "goal": goal_id,
                "activity": act_id,
                "step": uuid4(),
                "digest": "sha256:" + "b" * 64,
                "artifact": artifact_id,
                "attempt": att_id,
            },
        )
        rev = db.execute(
            text("SELECT state_revision FROM effect_intents WHERE id=:id"),
            {"id": effect_id},
        ).scalar_one()
    eng.dispose()

    denied = client.post(
        f"/api/v1/effects/{effect_id}/reconcile",
        json={
            "expected_state_revision": rev,
            "observed_result": "SUCCEEDED",
            "external_ref": "after-done-blocked",
            "evidence_ids": [str(uuid4())],
            "reason": "终态后不得对账",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 422, denied.text
    assert "GOAL_ENGINEERING_CLOSED" in denied.json()["error"]["message"]

    eng = _engine()
    with eng.begin() as db:
        rec_n = int(
            db.execute(
                text(
                    """SELECT count(*) FROM activities
                    WHERE goal_id=:goal AND kind='RECONCILE'"""
                ),
                {"goal": goal_id},
            ).scalar_one()
        )
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
    eng.dispose()
    assert status == "DONE"
    assert rec_n == 0


def test_submit_finalize_races_reconcile_outcome_under_admission(api, objects):
    """第147批：finalize × submit_reconcile_outcome 在 admission 下安全串行。

    禁止 Goal DONE 且 effect 仍为 UNKNOWN。
    """
    from control_kernel.protocols.effects import TrustedReceipt
    from control_kernel.protocols.plans import ActivityOutcomeRequest
    from control_kernel.storage.effects import apply_effect_receipt
    from control_kernel.storage.finalization import submit_finalize_outcome
    from control_kernel.storage.reconcile import submit_reconcile_outcome
    from test_claims import _register_worker

    ctx = _finalize_ready_ctx(api, objects)
    planted = _insert_dispatched_effect_under_goal(ctx)
    engine = ctx["client"].app.state.engine
    client = ctx["client"]
    auth = ctx["auth"]
    token = ctx["token"]

    # UNKNOWN 回执 → 生成 EvidenceEnvelope，供对账
    receipt = TrustedReceipt(
        receipt_id=uuid4(),
        effect_id=planted["effect_id"],
        producer_activity_id=planted["activity_id"],
        producer_attempt_id=planted["attempt_id"],
        fencing_epoch=planted["epoch"],
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        exit_code=None,
        timed_out=True,
        result_artifact_ids=[],
        observed_outcome="UNKNOWN",
    )
    apply_effect_receipt(engine, planted["subject"], planted["effect_id"], receipt)

    eng = _engine()
    with eng.begin() as db:
        effect = (
            db.execute(
                text("SELECT status, state_revision FROM effect_intents WHERE id=:id"),
                {"id": planted["effect_id"]},
            )
            .mappings()
            .one()
        )
        assert effect["status"] == "UNKNOWN"
        env_id = db.execute(
            text(
                """SELECT id FROM evidence_envelopes WHERE effect_id=:id
                ORDER BY created_at DESC LIMIT 1"""
            ),
            {"id": planted["effect_id"]},
        ).scalar_one()
    eng.dispose()

    created = client.post(
        f"/api/v1/effects/{planted['effect_id']}/reconcile",
        json={
            "expected_state_revision": effect["state_revision"],
            "observed_result": "SUCCEEDED",
            "external_ref": "finalize-race-reconcile",
            "evidence_ids": [str(env_id)],
            "reason": "对账核实成功",
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 202, created.text
    reconcile_activity_id = UUID(created.json()["data"]["result"]["activity_id"])

    eng = _engine()
    with eng.begin() as db:
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
                WHERE id=:id"""
            ),
            {"id": reconcile_activity_id},
        )
    eng.dispose()

    rec_subject = str(uuid4())
    _register_worker(rec_subject, kinds=("RECONCILE",))
    rec_auth = {"Authorization": "Bearer " + token(rec_subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["RECONCILE"], "capabilities": []},
        headers={**rec_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert UUID(lease["activity"]["id"]) == reconcile_activity_id

    fin_body = _finalize_outcome_body(ctx)
    rec_body = ActivityOutcomeRequest.model_validate(
        {
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "effect_id": str(planted["effect_id"]),
                "observed_status": "SUCCEEDED",
                "evidence_ids": [str(env_id)],
            },
        }
    )

    outcomes: list[str] = []
    errors: list[BaseException] = []
    fin_activity_id = UUID(ctx["fin"]["activity"]["id"])
    goal_id = UUID(ctx["goal"]["id"])

    def finalizer(
        *,
        outs: list[str] = outcomes,
        errs: list[BaseException] = errors,
    ) -> None:
        try:
            submit_finalize_outcome(
                engine, planted["subject"], fin_activity_id, fin_body
            )
            outs.append("finalize_ok")
        except BaseException as caught:  # noqa: BLE001
            outs.append(f"finalize_err:{type(caught).__name__}")
            errs.append(caught)

    def reconciler(
        *,
        outs: list[str] = outcomes,
        errs: list[BaseException] = errors,
    ) -> None:
        try:
            submit_reconcile_outcome(
                engine, rec_subject, reconcile_activity_id, rec_body
            )
            outs.append("reconcile_ok")
        except BaseException as caught:  # noqa: BLE001
            outs.append(f"reconcile_err:{type(caught).__name__}")
            errs.append(caught)

    t1 = threading.Thread(target=finalizer, name="finalize-vs-reconcile")
    t2 = threading.Thread(target=reconciler, name="reconcile-vs-finalize")
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), outcomes

    eng = _engine()
    with eng.begin() as db:
        status = db.execute(
            text("SELECT status FROM goals WHERE id=:id"), {"id": goal_id}
        ).scalar_one()
        effect_status = db.execute(
            text("SELECT status FROM effect_intents WHERE id=:id"),
            {"id": planted["effect_id"]},
        ).scalar_one()
        release_n = int(
            db.execute(
                text("SELECT count(*) FROM release_manifests WHERE goal_id=:goal"),
                {"goal": goal_id},
            ).scalar_one()
        )
    eng.dispose()

    if status == "DONE":
        assert effect_status == "SUCCEEDED"
        assert release_n == 1
        assert "finalize_ok" in outcomes
        assert "reconcile_ok" in outcomes
    else:
        assert status == "VERIFYING"
        assert release_n == 0
        assert effect_status in ("UNKNOWN", "SUCCEEDED")
        assert "finalize_ok" not in outcomes
        # 若对账先成功，effect SUCCEEDED 但 finalize 仍可能因其它门失败；不得 DONE+UNKNOWN
        assert not (status == "DONE" and effect_status == "UNKNOWN")
