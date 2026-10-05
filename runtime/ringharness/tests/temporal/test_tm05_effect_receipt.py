"""TM05 部分举证：外部成功且回执丢失 → 原 effect 核对，不盲目重执行。

真实 PG；复用 test_effects / broker_read_file / reconcile 模式。
禁把重复 prepare/dispatch 写成第二次 SUCCEEDED；Goal 不得 DONE。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from sqlalchemy import text
from test_claims import _drain_ready_plans
from test_effects import _publish_and_claim_execute


def test_tm05_lost_receipt_idempotent_no_blind_reexec(api, objects):
    """DISPATCHED 后回执丢失再投：同 receipt 幂等；禁第二 SUCCEEDED 副作用。"""
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    engine = client.app.state.engine
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    lease = exec_lease["lease"]

    try:
        step = client.post(
            f"/internal/v1/activities/{activity_id}/steps",
            json={
                "lease": lease,
                "predecessor_step_id": None,
                "purpose": "读取入口（TM05）",
                "tool_ref": "read_file",
            },
            headers=worker_auth,
        )
        assert step.status_code == 201, step.text
        step_data = step.json()["data"]
        logical_step_id = step_data["logical_step_id"]

        input_blob = b'{"path":"src/main.py"}'
        input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
        input_art = Artifacts(engine, store).ingest_raw(
            project_id,
            input_digest,
            BytesIO(input_blob),
            mime="application/json",
            producer_identity="test:tm05-input",
        )

        prepared = client.post(
            "/internal/v1/effects/prepare",
            json={
                "lease": lease,
                "logical_step_id": logical_step_id,
                "intent_revision": 1,
                "tool_ref": "read_file",
                "input_artifact_id": str(input_art.id),
            },
            headers=worker_auth,
        )
        assert prepared.status_code == 201, prepared.text
        effect = prepared.json()["data"]
        assert effect["status"] == "PREPARED"
        effect_id = effect["id"]

        # 外部已成功窗口：先 dispatch，故意不投回执（回执丢失）
        dispatched = client.post(
            f"/internal/v1/effects/{effect_id}/dispatch",
            json={"lease": lease, "effect_state_revision": effect["state_revision"]},
            headers=worker_auth,
        )
        assert dispatched.status_code == 200, dispatched.text
        assert dispatched.json()["data"]["status"] == "DISPATCHED"
        rev_dispatched = dispatched.json()["data"]["state_revision"]

        # 盲目重 dispatch：幂等 DISPATCHED，不另起 effect
        again_dispatch = client.post(
            f"/internal/v1/effects/{effect_id}/dispatch",
            json={"lease": lease, "effect_state_revision": rev_dispatched},
            headers=worker_auth,
        )
        assert again_dispatch.status_code == 200, again_dispatch.text
        assert again_dispatch.json()["data"]["id"] == effect_id
        assert again_dispatch.json()["data"]["status"] == "DISPATCHED"

        # 模拟外部已成功：投 TrustedReceipt 一次 → SUCCEEDED
        result_blob = b"print('tm05-ok')\n"
        result_digest = "sha256:" + hashlib.sha256(result_blob).hexdigest()
        result_art = Artifacts(engine, store).ingest_raw(
            project_id,
            result_digest,
            BytesIO(result_blob),
            mime="text/x-python",
            producer_identity="test:tm05-result",
        )
        receipt_id = str(uuid4())
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        finished = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        receipt_body = {
            "receipt_id": receipt_id,
            "effect_id": effect_id,
            "producer_activity_id": activity_id,
            "producer_attempt_id": lease["attempt_id"],
            "fencing_epoch": lease["fencing_epoch"],
            "started_at": started,
            "finished_at": finished,
            "exit_code": 0,
            "timed_out": False,
            "result_artifact_ids": [str(result_art.id)],
            "observed_outcome": "SUCCEEDED",
        }
        first = client.post(
            f"/internal/v1/effects/{effect_id}/receipts",
            json=receipt_body,
            headers=worker_auth,
        )
        assert first.status_code == 201, first.text
        assert first.json()["data"]["disposition"] == "APPLIED"
        assert first.json()["data"]["receipt_id"] == receipt_id

        succeeded = client.get(f"/api/v1/effects/{effect_id}", headers=auth)
        assert succeeded.status_code == 200, succeeded.text
        assert succeeded.json()["data"]["status"] == "SUCCEEDED"
        final_rev = succeeded.json()["data"]["state_revision"]

        # 同 receipt_id 再投：幂等（APPLIED 或 DUPLICATE），仍一 SUCCEEDED
        replay = client.post(
            f"/internal/v1/effects/{effect_id}/receipts",
            json=receipt_body,
            headers=worker_auth,
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["data"]["disposition"] in ("APPLIED", "DUPLICATE")
        assert replay.json()["data"]["receipt_id"] == receipt_id

        still = client.get(f"/api/v1/effects/{effect_id}", headers=auth).json()["data"]
        assert still["status"] == "SUCCEEDED"
        assert still["state_revision"] == final_rev

        with engine.connect() as db:
            receipt_rows = db.execute(
                text(
                    """SELECT count(*) FROM effect_receipts
                    WHERE effect_id=:effect AND receipt_id=:receipt"""
                ),
                {"effect": effect_id, "receipt": receipt_id},
            ).scalar_one()
            succeeded_count = db.execute(
                text(
                    """SELECT count(*) FROM effect_intents
                    WHERE activity_id=:activity AND status='SUCCEEDED'"""
                ),
                {"activity": activity_id},
            ).scalar_one()
            same_step_effects = db.execute(
                text(
                    """SELECT count(*) FROM effect_intents
                    WHERE activity_id=:activity AND logical_step_id=:step"""
                ),
                {"activity": activity_id, "step": logical_step_id},
            ).scalar_one()
        assert int(receipt_rows) == 1
        assert int(succeeded_count) == 1
        assert int(same_step_effects) == 1

        # 无 reconcile：同 logical_step 再 prepare → 复用原 effect，不新建
        re_prepare = client.post(
            "/internal/v1/effects/prepare",
            json={
                "lease": lease,
                "logical_step_id": logical_step_id,
                "intent_revision": 1,
                "tool_ref": "read_file",
                "input_artifact_id": str(input_art.id),
            },
            headers=worker_auth,
        )
        assert re_prepare.status_code == 201, re_prepare.text
        assert re_prepare.json()["data"]["id"] == effect_id
        assert re_prepare.json()["data"]["status"] == "SUCCEEDED"

        # 对已 SUCCEEDED 盲目 dispatch → 拒绝，不得二次副作用
        blind = client.post(
            f"/internal/v1/effects/{effect_id}/dispatch",
            json={"lease": lease, "effect_state_revision": final_rev},
            headers=worker_auth,
        )
        assert blind.status_code == 422, blind.text

        with engine.connect() as db:
            after_count = db.execute(
                text(
                    """SELECT count(*) FROM effect_intents
                    WHERE activity_id=:activity AND status='SUCCEEDED'"""
                ),
                {"activity": activity_id},
            ).scalar_one()
        assert int(after_count) == 1

        got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
        assert got.status_code == 200, got.text
        assert got.json()["data"]["status"] != "DONE"
        assert got.json()["data"]["status"] in ("RUNNING", "PLANNING", "VERIFYING")
    finally:
        _drain_ready_plans(client, token)
