"""StopRequest / StopReceipt 内部路由：幂等、观察推进与过期 UNCONFIRMED。"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text
from test_claims import _register_worker, _start_goal


def _claim_plan(client, token, auth):
    subject = str(uuid4())
    _register_worker(subject)
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["lease"] is not None
    return lease, worker_auth, auth


def _stop_body(lease, *, reason="CANCEL", deadline=None, request_id=None):
    attempt_id = lease["attempt"]["id"]
    return {
        "request_id": request_id or str(uuid4()),
        "activation_id": attempt_id,
        "activity_id": lease["lease"]["activity_id"],
        "attempt_id": attempt_id,
        "fencing_epoch": lease["lease"]["fencing_epoch"],
        "reason": reason,
        "deadline_at": (deadline or (datetime.now(UTC) + timedelta(hours=1)))
        .isoformat()
        .replace("+00:00", "Z"),
    }


def test_stop_request_receipt_confirm_and_guards(api, objects):
    client, token, auth, _goal, _plan = _start_goal(api, objects)
    lease, worker_auth, admin_auth = _claim_plan(client, token, auth)
    attempt_id = lease["attempt"]["id"]
    body = _stop_body(lease)

    # viewer 拒绝
    denied = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=body,
        headers={"Authorization": "Bearer " + token(str(uuid4()), ["viewer"])},
    )
    assert denied.status_code == 403

    requested = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=body,
        headers=admin_auth,
    )
    assert requested.status_code == 202, requested.text
    stop = requested.json()["data"]
    assert stop["status"] == "REQUESTED"
    assert stop["receipt_ids"] == []
    assert stop["request"]["activation_id"] == attempt_id
    assert stop["request"]["attempt_id"] == attempt_id

    # 同 request_id + 同参数幂等
    again = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=body,
        headers=admin_auth,
    )
    assert again.status_code == 202
    assert again.json()["data"]["id"] == stop["id"]
    assert again.json()["data"]["status"] == "REQUESTED"

    # 同 request_id 不同参数 → 冲突
    conflict = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json={**body, "reason": "PAUSE"},
        headers=admin_auth,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # RUNNING 观察：仍 REQUESTED，不假 CONFIRMED
    running_rid = str(uuid4())
    running = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": running_rid,
            "stop_id": stop["id"],
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "RUNNING",
            "compute_released": False,
            "write_capability_revoked": False,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert running.status_code == 201, running.text
    assert running.json()["data"]["disposition"] == "APPLIED"
    mid = client.get(f"/internal/v1/stops/{stop['id']}", headers=admin_auth)
    assert mid.status_code == 200
    assert mid.json()["data"]["status"] == "REQUESTED"
    assert running_rid in mid.json()["data"]["receipt_ids"]

    # ISOLATED + 撤写：仍不 CONFIRMED
    isolated = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop["id"],
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "ISOLATED",
            "compute_released": False,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert isolated.status_code == 201
    assert isolated.json()["data"]["disposition"] == "APPLIED"
    still = client.get(f"/internal/v1/stops/{stop['id']}", headers=admin_auth)
    assert still.json()["data"]["status"] == "REQUESTED"

    # EXITED 但未释放计算：仍不 CONFIRMED
    exited_held = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop["id"],
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": False,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert exited_held.status_code == 201
    assert (
        client.get(f"/internal/v1/stops/{stop['id']}", headers=admin_auth).json()["data"][
            "status"
        ]
        == "REQUESTED"
    )

    # EXITED ∧ compute_released → CONFIRMED
    confirm_rid = str(uuid4())
    confirmed = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": confirm_rid,
            "stop_id": stop["id"],
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
    assert confirmed.status_code == 201
    assert confirmed.json()["data"]["disposition"] == "APPLIED"
    final = client.get(f"/internal/v1/stops/{stop['id']}", headers=admin_auth)
    assert final.json()["data"]["status"] == "CONFIRMED"
    assert confirm_rid in final.json()["data"]["receipt_ids"]

    # receipt_id 去重
    dup = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": confirm_rid,
            "stop_id": stop["id"],
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
    assert dup.status_code == 201
    assert dup.json()["data"]["disposition"] == "DUPLICATE"


def test_stop_mismatched_receipt_pending_and_deadline_unconfirmed(api, objects):
    client, token, auth, _goal, _plan = _start_goal(api, objects)
    lease, worker_auth, admin_auth = _claim_plan(client, token, auth)
    attempt_id = lease["attempt"]["id"]
    past = datetime.now(UTC) - timedelta(seconds=5)
    body = _stop_body(lease, deadline=past)

    stop = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=body,
        headers=admin_auth,
    ).json()["data"]
    assert stop["status"] == "REQUESTED"

    # 正确 Stop owner 提交参数矛盾回执 → PENDING_RECONCILIATION（落库对账，不推进 CONFIRMED）
    other = str(uuid4())
    pending = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop["id"],
            "activation_id": other,
            "attempt_id": other,
            "resource_instance_id": other,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=worker_auth,
    )
    assert pending.status_code == 201, pending.text
    assert pending.json()["data"]["disposition"] == "PENDING_RECONCILIATION"

    got = client.get(f"/internal/v1/stops/{stop['id']}", headers=admin_auth)
    assert got.status_code == 200
    # deadline 已过：仍 UNCONFIRMED；矛盾回执不得推进 CONFIRMED / 写入 receipt_ids 权威链
    assert got.json()["data"]["status"] == "UNCONFIRMED"
    assert got.json()["data"]["receipt_ids"] == []

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        row = (
            db.execute(
                text(
                    """SELECT disposition, observation FROM stop_receipts
                    WHERE stop_id=:id"""
                ),
                {"id": stop["id"]},
            )
            .mappings()
            .one()
        )
    engine.dispose()
    assert row["disposition"] == "PENDING_RECONCILIATION"
    assert row["observation"] == "EXITED"

    # activation_id != attempt_id 的请求体拒绝
    bad = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json={
            **_stop_body(lease),
            "activation_id": attempt_id,
            "attempt_id": str(uuid4()),
        },
        headers=admin_auth,
    )
    assert bad.status_code == 422


def test_stop_request_replay_after_fencing_epoch_bump(api, objects):
    """同 request_id 重传：attempt fencing 前进后仍返回首次 StopResource。"""
    client, token, auth, _goal, _plan = _start_goal(api, objects)
    lease, _worker_auth, admin_auth = _claim_plan(client, token, auth)
    attempt_id = lease["attempt"]["id"]
    body = _stop_body(lease)
    original_epoch = int(body["fencing_epoch"])

    first = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=body,
        headers=admin_auth,
    )
    assert first.status_code == 202, first.text
    stop = first.json()["data"]

    # 模拟 fencing 前进（不改 request 正文，仅推进 attempt 当前 epoch）
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activity_attempts
                  SET fencing_epoch=:epoch, updated_at=clock_timestamp()
                  WHERE id=:id"""
            ),
            {"id": attempt_id, "epoch": original_epoch + 1},
        )
    engine.dispose()

    replay = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=body,
        headers=admin_auth,
    )
    assert replay.status_code == 202, replay.text
    again = replay.json()["data"]
    assert again["id"] == stop["id"]
    assert again["status"] == stop["status"]
    assert again["request"]["request_id"] == body["request_id"]
    assert again["request"]["fencing_epoch"] == body["fencing_epoch"]


def test_stop_receipt_id_conflict_rejects(api, objects):
    """同 receipt_id 参数不一致时 422，不得静默 DUPLICATE。"""
    client, token, auth, _goal, _plan = _start_goal(api, objects)
    lease, worker_auth, admin_auth = _claim_plan(client, token, auth)
    attempt_id = lease["attempt"]["id"]

    stop_a = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=_stop_body(lease),
        headers=admin_auth,
    ).json()["data"]

    client_b, token_b, auth_b, _g2, _p2 = _start_goal(api, objects)
    lease_b, worker_auth_b, admin_auth_b = _claim_plan(client_b, token_b, auth_b)
    attempt_b = lease_b["attempt"]["id"]
    stop_b = client_b.post(
        f"/internal/v1/activations/{attempt_b}/stop",
        json=_stop_body(lease_b),
        headers=admin_auth_b,
    ).json()["data"]

    receipt_id = str(uuid4())
    base = {
        "receipt_id": receipt_id,
        "stop_id": stop_a["id"],
        "activation_id": attempt_id,
        "attempt_id": attempt_id,
        "resource_instance_id": attempt_id,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "observation": "RUNNING",
        "compute_released": False,
        "write_capability_revoked": False,
        "proof_artifact_ids": [],
    }
    applied = client.post(
        f"/internal/v1/stops/{stop_a['id']}/receipts",
        json=base,
        headers=worker_auth,
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["data"]["disposition"] == "APPLIED"

    # 同 receipt_id、不同 observation → 422
    obs_conflict = client.post(
        f"/internal/v1/stops/{stop_a['id']}/receipts",
        json={**base, "observation": "EXITED", "compute_released": True},
        headers=worker_auth,
    )
    assert obs_conflict.status_code == 422
    assert obs_conflict.json()["error"]["message"] == "STOP_RECEIPT_CONFLICT"

    # 同 receipt_id、不同 stop_id → 422（不得 DUPLICATE）
    stop_conflict = client_b.post(
        f"/internal/v1/stops/{stop_b['id']}/receipts",
        json={
            **base,
            "stop_id": stop_b["id"],
            "activation_id": attempt_b,
            "attempt_id": attempt_b,
            "resource_instance_id": attempt_b,
        },
        headers=worker_auth_b,
    )
    assert stop_conflict.status_code == 422
    assert stop_conflict.json()["error"]["message"] == "STOP_RECEIPT_CONFLICT"


def test_stop_receipt_rejects_non_owner_worker(api, objects):
    """Issue #17：非持有者不得伪造 Stop 回执提前释放 QUARANTINED。"""
    client, token, auth, _goal, _plan = _start_goal(api, objects)
    lease, _worker_auth, admin_auth = _claim_plan(client, token, auth)
    attempt_id = lease["attempt"]["id"]
    stop = client.post(
        f"/internal/v1/activations/{attempt_id}/stop",
        json=_stop_body(lease),
        headers=admin_auth,
    ).json()["data"]

    # 隔离资源，确认伪造 CONFIRMED 不会释放
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE resource_reservations
                  SET status='QUARANTINED', updated_at=clock_timestamp()
                  WHERE attempt_id=:id AND status='HELD'"""
            ),
            {"id": attempt_id},
        )
    engine.dispose()

    impostor = str(uuid4())
    _register_worker(impostor)
    impostor_auth = {"Authorization": "Bearer " + token(impostor, ["worker"])}
    forged = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": stop["id"],
            "activation_id": attempt_id,
            "attempt_id": attempt_id,
            "resource_instance_id": attempt_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=impostor_auth,
    )
    assert forged.status_code == 403, forged.text

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        stop_status = db.execute(
            text("SELECT status FROM stops WHERE id=:id"), {"id": stop["id"]}
        ).scalar_one()
        receipt_n = db.execute(
            text("SELECT count(*) FROM stop_receipts WHERE stop_id=:id"),
            {"id": stop["id"]},
        ).scalar_one()
        q = db.execute(
            text(
                """SELECT count(*) FROM resource_reservations
                WHERE attempt_id=:id AND status='QUARANTINED'"""
            ),
            {"id": attempt_id},
        ).scalar_one()
    engine.dispose()
    assert stop_status == "REQUESTED"
    assert receipt_n == 0
    assert q >= 1


def test_stop_receipt_rejects_successor_attempt_holder(api, objects):
    """Issue #17 残差：同 Activity 后继 attempt 持有者不得向旧 Stop 写入回执。

    权威 owner 取自 stops.attempt_id，不得信请求体自报 attempt。
    """
    client, token, _auth, _goal, _plan = _start_goal(api, objects)
    subject_a = str(uuid4())
    _register_worker(subject_a)
    auth_a = {"Authorization": "Bearer " + token(subject_a, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth_a, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease_a = claimed.json()["data"]
    attempt_a = lease_a["attempt"]["id"]
    activity_id = lease_a["lease"]["activity_id"]

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activity_attempts
                  SET lease_expires_at=:past, updated_at=clock_timestamp()
                  WHERE id=:id"""
            ),
            {"id": attempt_a, "past": datetime.now(UTC) - timedelta(seconds=5)},
        )
    # 旧心跳失败；随后 claim 触发 expire → LEASE_EXPIRED Stop（owner=attempt_a）
    heart = client.post(
        f"/internal/v1/activities/{activity_id}/heartbeat",
        json={
            "lease": lease_a["lease"],
            "renewal_seq": lease_a["attempt"]["renewal_seq"] + 1,
        },
        headers=auth_a,
    )
    assert heart.status_code in (409, 400), heart.text

    subject_b = str(uuid4())
    _register_worker(subject_b)
    auth_b = {"Authorization": "Bearer " + token(subject_b, ["worker"])}
    reclaim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**auth_b, "Idempotency-Key": str(uuid4())},
    )
    assert reclaim.status_code == 200, reclaim.text
    lease_b = reclaim.json()["data"]
    assert lease_b["lease"] is not None
    attempt_b = lease_b["attempt"]["id"]
    assert attempt_b != attempt_a

    with engine.begin() as db:
        stop = (
            db.execute(
                text(
                    """SELECT id, attempt_id, status FROM stops
                    WHERE attempt_id=:id AND reason='LEASE_EXPIRED'"""
                ),
                {"id": attempt_a},
            )
            .mappings()
            .first()
        )
        db.execute(
            text(
                """UPDATE resource_reservations
                  SET status='QUARANTINED', updated_at=clock_timestamp()
                  WHERE attempt_id=:id AND status IN ('HELD','QUARANTINED')"""
            ),
            {"id": attempt_a},
        )
    assert stop is not None
    assert str(stop["attempt_id"]) == attempt_a
    assert stop["status"] == "REQUESTED"

    # 后继 holder 用自报 attempt_b 向旧 Stop 投 EXITED——须 403 零写入
    forged = client.post(
        f"/internal/v1/stops/{stop['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "stop_id": str(stop["id"]),
            "activation_id": attempt_b,
            "attempt_id": attempt_b,
            "resource_instance_id": attempt_b,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "observation": "EXITED",
            "compute_released": True,
            "write_capability_revoked": True,
            "proof_artifact_ids": [],
        },
        headers=auth_b,
    )
    assert forged.status_code == 403, forged.text

    with engine.begin() as db:
        stop_status = db.execute(
            text("SELECT status FROM stops WHERE id=:id"), {"id": stop["id"]}
        ).scalar_one()
        receipt_n = db.execute(
            text("SELECT count(*) FROM stop_receipts WHERE stop_id=:id"),
            {"id": stop["id"]},
        ).scalar_one()
        q = db.execute(
            text(
                """SELECT count(*) FROM resource_reservations
                WHERE attempt_id=:id AND status='QUARANTINED'"""
            ),
            {"id": attempt_a},
        ).scalar_one()
    engine.dispose()
    assert stop_status == "REQUESTED"
    assert receipt_n == 0
    assert q >= 1
