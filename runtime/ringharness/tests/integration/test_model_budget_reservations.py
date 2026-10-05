"""ModelInvocation HTTP 咽喉：create 占预留、dispatch 回执释放/计入 consumed；≠ DONE。"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from control_api.connectors.local_qwen import LocalQwenUnavailable
from control_kernel.protocols.runtime import LeaseRejected
from control_kernel.storage.budget_clock import (
    adjust_goal_budget_reservations,
    assert_goal_wall_budget_allows_engineering,
)
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from test_claims import _register_worker, _start_goal


def _plan_context_and_create_invocation(client, worker_auth, lease, *, max_output_tokens=128):
    """PLAN 宿主最小 context + create_invocation。"""
    from control_kernel.storage.claims import binding_digest_of

    activity_id = lease["activity"]["id"]
    compiled = client.post(
        f"/internal/v1/activities/{activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=worker_auth,
    )
    assert compiled.status_code == 201, compiled.text
    bound = client.post(
        f"/internal/v1/activities/{activity_id}/context",
        json={
            "lease": lease["lease"],
            "binding_digest": binding_digest_of(lease["activity"]["binding"]),
            "context_bundle_id": compiled.json()["data"]["id"],
        },
        headers=worker_auth,
    )
    assert bound.status_code == 200, bound.text
    context_digest = bound.json()["data"]["context_digest"]
    prompt = json.dumps(
        {"role": "PLANNER", "tools": [], "instruction": "emit PlanCreate only"},
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(prompt).hexdigest()
    created = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": max_output_tokens,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    return created, context_digest, input_digest


def test_create_invocation_reserves_tokens_dispatch_releases_and_counts(api, objects):
    """HTTP create→reserved；mock dispatch→自动回执释放并计入 consumed；幂等 create 不双计。"""
    client, token, auth, goal, _plan = _start_goal(api, objects)
    goal_id = goal["id"]
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

    before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert before["budget_usage"]["reserved_tokens"] == 0
    assert before["budget_usage"]["consumed_tokens"] == 0
    assert before["status"] != "DONE"

    created, context_digest, input_digest = _plan_context_and_create_invocation(
        client, worker_auth, lease, max_output_tokens=128
    )
    assert created.status_code == 201, created.text
    invocation = created.json()["data"]
    after_create = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after_create["budget_usage"]["reserved_tokens"] == 128
    assert after_create["budget_usage"]["consumed_tokens"] == 0
    assert after_create["status"] != "DONE"

    # 幂等重传同一 seq 不双计预留
    again = client.post(
        "/internal/v1/model-invocations",
        json={
            "lease": lease["lease"],
            "invocation_seq": 1,
            "context_digest": context_digest,
            "input_digest": input_digest,
            "provider_ref": "pm2:omlx-flashnext",
            "model_id": "Qwen3.8-Flash-Next-Uncensored-Mixed-omlx",
            "max_output_tokens": 128,
            "max_cost_usd": "0",
            "data_categories": [],
        },
        headers=worker_auth,
    )
    assert again.status_code == 201, again.text
    assert again.json()["data"]["id"] == invocation["id"]
    dup_goal = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert dup_goal["budget_usage"]["reserved_tokens"] == 128

    fake_response = {
        "model": invocation["model_id"],
        "id": "chatcmpl-budget-reserve",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "choices": [{"message": {"content": "plan stub"}}],
    }
    app = client.app
    old = app.state.settings
    # DENY 云闸门只允许 loopback；本机 chat.env 可能指向 DeepSeek，须强制回环
    app.state.settings = old.model_copy(
        update={
            "local_qwen_api_key": SecretStr("test-key"),
            "local_qwen_base": "http://127.0.0.1:8001",
            "cloud_mode": "DENY",
        }
    )
    try:
        with patch("control_api.routes.probe.chat_completion") as mocked:
            mocked.return_value = {"response": fake_response}
            dispatched = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
    finally:
        app.state.settings = old
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["data"]["status"] == "SUCCEEDED"

    after_dispatch = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after_dispatch["budget_usage"]["reserved_tokens"] == 0
    assert after_dispatch["budget_usage"]["consumed_tokens"] == 30
    assert after_dispatch["status"] != "DONE"


def test_dispatch_loopback_records_gpu_seconds_from_elapsed(api, objects):
    """loopback dispatch：连接器墙钟写入 BudgetUsage.gpu_seconds；≠ DONE。"""
    client, auth, goal, worker_auth, lease, invocation = _claim_plan_and_create(
        api, objects, max_output_tokens=64
    )
    goal_id = goal["id"]
    fake_response = {
        "model": invocation["model_id"],
        "id": "chatcmpl-gpu-loopback",
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        "choices": [{"message": {"content": "plan stub"}}],
    }
    app = client.app
    old = app.state.settings
    app.state.settings = old.model_copy(
        update={
            "local_qwen_api_key": SecretStr("test-key"),
            "local_qwen_base": "http://127.0.0.1:8001",
            "cloud_mode": "DENY",
        }
    )
    try:
        with patch("control_api.routes.probe.chat_completion") as mocked:
            mocked.return_value = {
                "response": fake_response,
                "elapsed_wall_seconds": 1.25,
            }
            dispatched = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
    finally:
        app.state.settings = old
    assert dispatched.status_code == 200, dispatched.text
    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["gpu_seconds"] == "1.25"
    assert after["budget_usage"]["consumed_tokens"] == 5
    assert after["status"] != "DONE"
    assert after["status"] != "FAILED"


def test_dispatch_non_loopback_skips_gpu_seconds(api, objects):
    """非 loopback：即使有 elapsed 也不写入 gpu_seconds。"""
    client, auth, goal, worker_auth, lease, invocation = _claim_plan_and_create(
        api, objects, max_output_tokens=32
    )
    goal_id = goal["id"]
    fake_response = {
        "model": invocation["model_id"],
        "id": "chatcmpl-gpu-remote",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "choices": [{"message": {"content": "ok"}}],
    }
    app = client.app
    old = app.state.settings
    app.state.settings = old.model_copy(
        update={
            "local_qwen_api_key": SecretStr("test-key"),
            "local_qwen_base": "http://127.0.0.1:8001",
            "cloud_mode": "DENY",
        }
    )
    try:
        with (
            patch(
                "control_api.routes.probe.is_loopback_inference_base",
                return_value=False,
            ),
            patch("control_api.routes.probe.chat_completion") as mocked,
        ):
            mocked.return_value = {
                "response": fake_response,
                "elapsed_wall_seconds": 9.99,
            }
            dispatched = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
    finally:
        app.state.settings = old
    assert dispatched.status_code == 200, dispatched.text
    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["gpu_seconds"] is None
    assert after["status"] != "DONE"


def test_create_invocation_budget_exhausted_rejects_without_side_effect(api, objects):
    """预留空间不足时 create 422；不落 invocation；事务回滚不写 FAILED。"""
    client, token, auth, goal, _plan = _start_goal(api, objects)
    goal_id = UUID(goal["id"])
    max_tokens = int(goal["contract"]["budget"]["max_tokens"])
    # 不改合同（会破坏 contract_digest）；先占满预留，再 create 必超限
    prefill = max_tokens - 10
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        adjust_goal_budget_reservations(db, goal_id, reserved_tokens_delta=prefill)
    eng.dispose()

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
    activity_id = lease["activity"]["id"]

    denied, _ctx, _inp = _plan_context_and_create_invocation(
        client, worker_auth, lease, max_output_tokens=128
    )
    assert denied.status_code == 422, denied.text
    assert "BUDGET_EXHAUSTED" in denied.json()["error"]["message"]

    got = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    # create 事务回滚：预留仍为 prefill，不因失败登记而 FAILED
    assert got["budget_usage"]["reserved_tokens"] == prefill
    assert got["status"] not in ("DONE", "FAILED")

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.connect() as db:
        n = db.execute(
            text(
                """SELECT count(*) FROM model_invocations
                WHERE activity_id=:aid AND invocation_seq=1"""
            ),
            {"aid": activity_id},
        ).scalar_one()
    eng.dispose()
    assert n == 0


def _claim_plan_and_create(api, objects, *, max_output_tokens=64):
    client, token, auth, goal, _plan = _start_goal(api, objects)
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
    created, _ctx, _inp = _plan_context_and_create_invocation(
        client, worker_auth, lease, max_output_tokens=max_output_tokens
    )
    assert created.status_code == 201, created.text
    return client, auth, goal, worker_auth, lease, created.json()["data"]


def test_unknown_dispatch_keeps_reservation_no_consumed(api, objects):
    """dispatch 依赖不可用 → UNKNOWN 回执：预留保留、不计入 consumed；≠ DONE。"""
    client, auth, goal, worker_auth, lease, invocation = _claim_plan_and_create(
        api, objects, max_output_tokens=64
    )
    goal_id = goal["id"]
    reserved_before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()[
        "data"
    ]["budget_usage"]["reserved_tokens"]
    assert reserved_before == 64

    app = client.app
    old = app.state.settings
    app.state.settings = old.model_copy(
        update={
            "local_qwen_api_key": SecretStr("test-key"),
            "local_qwen_base": "http://127.0.0.1:8001",
            "cloud_mode": "DENY",
        }
    )
    try:
        with patch("control_api.routes.probe.chat_completion") as mocked:
            mocked.side_effect = LocalQwenUnavailable("模拟连接器不可用")
            denied = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
    finally:
        app.state.settings = old
    assert denied.status_code == 503, denied.text

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.connect() as db:
        inv = (
            db.execute(
                text("SELECT status, usage_status FROM model_invocations WHERE id=:id"),
                {"id": invocation["id"]},
            )
            .mappings()
            .one()
        )
    eng.dispose()
    assert inv["status"] == "UNKNOWN"
    assert inv["usage_status"] == "UNKNOWN"

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["reserved_tokens"] == 64
    assert after["budget_usage"]["consumed_tokens"] == 0
    assert after["status"] != "DONE"


def test_failed_receipt_releases_reservation_no_consumed(api, objects):
    """FAILED 回执释放预留，但不计入 consumed（仅 SUCCEEDED+CONFIRMED）；≠ DONE。"""
    from datetime import UTC, datetime

    client, auth, goal, worker_auth, lease, invocation = _claim_plan_and_create(
        api, objects, max_output_tokens=48
    )
    goal_id = goal["id"]
    assert (
        client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"][
            "budget_usage"
        ]["reserved_tokens"]
        == 48
    )

    # 直接置 DISPATCHED，绕过连接器（本缝只验 receipt 预留语义）
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE model_invocations SET status='DISPATCHED',
                state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": invocation["id"]},
        )
    eng.dispose()

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    receipt = client.post(
        f"/internal/v1/model-invocations/{invocation['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "invocation_id": invocation["id"],
            "producer_attempt_id": lease["lease"]["attempt_id"],
            "observed_result": "FAILED",
            "usage_status": "CONFIRMED",
            "input_tokens": 5,
            "output_tokens": 7,
            "cost_usd": "0.01",
            "observed_at": ts,
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "APPLIED"

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["reserved_tokens"] == 0
    assert after["budget_usage"]["consumed_tokens"] == 0
    assert after["budget_usage"]["consumed_cost_usd"] in ("0", "0.0", "0.00")
    assert after["status"] != "DONE"


def test_succeeded_receipt_records_gpu_seconds(api, objects):
    """SUCCEEDED+CONFIRMED 回执携带 gpu_seconds → BudgetUsage 写入；max=null 不 FAILED；≠ DONE。"""
    from datetime import UTC, datetime

    client, auth, goal, worker_auth, lease, invocation = _claim_plan_and_create(
        api, objects, max_output_tokens=32
    )
    goal_id = goal["id"]
    before = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert before["budget_usage"]["gpu_seconds"] is None
    assert before["contract"]["budget"]["max_gpu_seconds"] is None

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE model_invocations SET status='DISPATCHED',
                state_revision=state_revision+1, updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": invocation["id"]},
        )
    eng.dispose()

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    receipt = client.post(
        f"/internal/v1/model-invocations/{invocation['id']}/receipts",
        json={
            "receipt_id": str(uuid4()),
            "invocation_id": invocation["id"],
            "producer_attempt_id": lease["lease"]["attempt_id"],
            "observed_result": "SUCCEEDED",
            "usage_status": "CONFIRMED",
            "input_tokens": 3,
            "output_tokens": 5,
            "gpu_seconds": "1.25",
            "observed_at": ts,
        },
        headers=worker_auth,
    )
    assert receipt.status_code == 201, receipt.text
    assert receipt.json()["data"]["disposition"] == "APPLIED"

    after = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert after["budget_usage"]["gpu_seconds"] == "1.25"
    assert after["budget_usage"]["consumed_tokens"] == 8
    assert after["budget_usage"]["reserved_tokens"] == 0
    assert after["status"] != "DONE"
    assert after["status"] != "FAILED"


def test_unknown_held_reservation_blocks_engineering_admit(api, objects):
    """UNKNOWN 保留的预留仍计入上限；再占满 → FAILED + admit BUDGET_EXHAUSTED；≠ DONE。"""
    client, auth, goal, worker_auth, lease, invocation = _claim_plan_and_create(
        api, objects, max_output_tokens=64
    )
    goal_id = UUID(goal["id"])
    max_tokens = int(goal["contract"]["budget"]["max_tokens"])

    app = client.app
    old = app.state.settings
    app.state.settings = old.model_copy(
        update={
            "local_qwen_api_key": SecretStr("test-key"),
            "local_qwen_base": "http://127.0.0.1:8001",
            "cloud_mode": "DENY",
        }
    )
    try:
        with patch("control_api.routes.probe.chat_completion") as mocked:
            mocked.side_effect = LocalQwenUnavailable("模拟连接器不可用")
            denied = client.post(
                f"/internal/v1/model-invocations/{invocation['id']}/dispatch",
                json={
                    "lease": lease["lease"],
                    "expected_state_revision": invocation["state_revision"],
                },
                headers=worker_auth,
            )
    finally:
        app.state.settings = old
    assert denied.status_code == 503, denied.text

    mid = client.get(f"/api/v1/goals/{goal_id}", headers=auth).json()["data"]
    assert mid["budget_usage"]["reserved_tokens"] == 64
    assert mid["status"] not in ("DONE", "FAILED")

    # 独立事务占满：UNKNOWN 留下的 64 计入，再加满即触顶
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        adjust_goal_budget_reservations(
            db, goal_id, reserved_tokens_delta=max_tokens - 64
        )
        row = (
            db.execute(
                text("SELECT status, block_reason, budget_usage FROM goals WHERE id=:id"),
                {"id": goal_id},
            )
            .mappings()
            .one()
        )
        assert row["budget_usage"]["reserved_tokens"] == max_tokens
        assert row["status"] == "FAILED"
        assert row["status"] != "DONE"
        assert "reserved_tokens" in (row["block_reason"] or "")
        with pytest.raises(LeaseRejected) as ei:
            assert_goal_wall_budget_allows_engineering(db, goal_id)
        assert ei.value.code == "BUDGET_EXHAUSTED"
    eng.dispose()
