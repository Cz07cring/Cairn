"""真实 GoalReview 快照 × Runner Critic 规则：open_effect_ids → BLOCKER → 工程门控。

parity：apps/runner/src/harness/goalReviewCritic.ts::findingsFromReviewSnapshot
≠ criterion PASS；≠ Goal DONE。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_effects import _publish_and_claim_execute


def findings_from_review_snapshot(snapshot: dict) -> list[dict]:
    """与 Runner goalReviewCritic.ts 对齐的确定性 findings（测试/parity 用）。"""
    findings: list[dict] = []
    open_ids = snapshot.get("open_effect_ids") or []
    if open_ids:
        findings.append(
            {
                "code": "OPEN_EFFECTS",
                "severity": "BLOCKER",
                "evidence_ids": [],
                "recommendation": (
                    f"存在 {len(open_ids)} 个未决 effect，禁止最终屏障 SEAL / 新 ENGINEERING"
                ),
            }
        )
    failed = [
        a
        for a in (snapshot.get("recent_activities") or [])
        if isinstance(a, dict) and a.get("status") == "FAILED"
    ]
    if failed:
        findings.append(
            {
                "code": "RECENT_ACTIVITY_FAILED",
                "severity": "WARN",
                "evidence_ids": [],
                "recommendation": (
                    f"近期有 {len(failed)} 个 FAILED 活动，建议复盘后重规划"
                ),
            }
        )
    if not findings:
        findings.append(
            {
                "code": "NO_BLOCKER",
                "severity": "INFO",
                "evidence_ids": [],
                "recommendation": "快照未见未决副作用或近期失败",
            }
        )
    return findings


def _prepare_dispatched_effect(client, store, worker_auth, exec_lease):
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "critic-snapshot-open-effect",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    blob = b'{"path":"src/critic_open.py"}'
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    artifact = Artifacts(client.app.state.engine, store).ingest_raw(
        project_id,
        digest,
        BytesIO(blob),
        mime="application/json",
        producer_identity="test:critic-snapshot",
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
    return effect["id"]


def _claim_goal_review_activity(client, token, activity_id: str):
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    _drain_ready(client, token, kinds=("AUDIT",))
    with engine.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='READY', updated_at=clock_timestamp()
                WHERE id=:id"""
            ),
            {"id": activity_id},
        )
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='AUDIT' AND id<>:id"""
            ),
            {"id": activity_id},
        )
    engine.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("AUDIT",))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["AUDIT"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    assert lease["activity"]["id"] == activity_id
    assert lease["activity"]["target"]["type"] == "GOAL_REVIEW"
    return worker_auth, lease


def test_open_effect_snapshot_drives_critic_blocker_gate(api, objects):
    """ensure 钉扎含 DISPATCHED effect 的快照 → Critic OPEN_EFFECTS → steps 422。"""
    store, _, _ = objects
    client, token, auth, goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    effect_id = _prepare_dispatched_effect(client, store, worker_auth, exec_lease)

    # 必须先越过 Kernel 的**停滞门**：ensure 只在「≥stagnation_seconds 无工程进展」时才创建
    # （默认合同 wall=3600 → min_interval=180、stagnation=360；见 _goal_review_budget_limits）。
    # 本用例刚建 Goal 就请求，会被 422 GOAL_REVIEW_NOT_STAGNANT 拒（实测 CI：
    # 「工程尚未停滞（需 ≥360s 无进展，已空闲 0s）」）。
    # 同款前置写法见 tests/integration/test_goal_review_ensure_api.py 的
    # test_ensure_goal_review_rejects_when_engineering_not_stagnant（那边用 wall=20 → 拨 30s）。
    # 拨旧**工程**活动的 updated_at/created_at（口径与 _latest_engineering_progress_at 一致），
    # 仅改时间戳、不动状态，故 snapshot 的 open_effect_ids 断言不受影响。
    engine_bootstrap = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine_bootstrap.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET updated_at=:ts, created_at=:ts
                WHERE goal_id=:goal
                  AND kind IN ('PLAN','EXECUTE','INTEGRATE','AUDIT')
                  AND (target_type IS NULL OR target_type <> 'GOAL_REVIEW')
                  AND status IN ('RUNNING','SUCCEEDED')"""
            ),
            {
                "goal": UUID(goal["id"]),
                "ts": datetime.now(UTC) - timedelta(seconds=400),
            },
        )
    engine_bootstrap.dispose()

    ensure = client.post(
        f"/internal/v1/goals/{goal['id']}/goal-reviews",
        json={"trigger_key": f"critic-open:{goal['id']}"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert ensure.status_code == 201, ensure.text
    ensured = ensure.json()["data"]
    review_activity_id = ensured["activity_id"]
    snap = ensured["review_snapshot_digest"]
    assert snap.startswith("sha256:")

    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.connect() as db:
        row = (
            db.execute(
                text(
                    """SELECT payload FROM goal_review_snapshots
                    WHERE content_digest=:d"""
                ),
                {"d": snap},
            )
            .mappings()
            .one()
        )
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        open_ids = payload.get("open_effect_ids") or []
        assert str(effect_id) in open_ids, (
            f"快照应含未决 effect，got={open_ids!r} expected={effect_id}"
        )
    engine.dispose()

    findings = findings_from_review_snapshot(payload)
    assert any(
        f["code"] == "OPEN_EFFECTS" and f["severity"] == "BLOCKER" for f in findings
    )

    audit_auth, lease = _claim_goal_review_activity(client, token, review_activity_id)
    binding = lease["activity"]["binding"]
    assert binding["subject_digest"] == snap

    # 可选：compile 绑定权威快照 EVIDENCE（Runner Critic 路径）
    compiled = client.post(
        f"/internal/v1/activities/{review_activity_id}/context-compile",
        json={"lease": lease["lease"], "max_input_tokens": 8192},
        headers=audit_auth,
    )
    assert compiled.status_code == 201, compiled.text
    content = compiled.json()["data"]["content"]
    evidence = [
        b
        for b in content["input_bindings"]
        if b["classification"] == "EVIDENCE" and b["digest"] == snap
    ]
    assert evidence, "AUDITOR compile 须绑定权威快照 EVIDENCE"
    art_id = evidence[0]["artifact_id"]
    art = client.get(f"/api/v1/artifacts/{art_id}/content", headers=audit_auth)
    assert art.status_code == 200, art.text
    artifact_payload = json.loads(art.text)
    assert str(effect_id) in (artifact_payload.get("open_effect_ids") or [])

    outcome = client.post(
        f"/internal/v1/activities/{review_activity_id}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {
                "target_type": "GOAL_REVIEW",
                "review": {
                    "goal_contract_revision": binding["goal_contract_revision"],
                    "plan_revision": binding["plan_revision"],
                    "review_snapshot_digest": snap,
                    "findings": findings,
                },
            },
        },
        headers=audit_auth,
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["data"]["status"] == "SUCCEEDED"

    blocked = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "OPEN_EFFECTS BLOCKER 应拒绝",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert blocked.status_code == 422, blocked.text
    assert "GOAL_REVIEW_BLOCKER" in blocked.text

    goal_after = client.get(f"/api/v1/goals/{goal['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"
    assert goal_after["status"] != "BLOCKED"


def test_findings_parity_no_open_effects_is_info():
    findings = findings_from_review_snapshot(
        {
            "open_effect_ids": [],
            "recent_activities": [{"kind": "EXECUTE", "status": "SUCCEEDED"}],
        }
    )
    assert findings == [
        {
            "code": "NO_BLOCKER",
            "severity": "INFO",
            "evidence_ids": [],
            "recommendation": "快照未见未决副作用或近期失败",
        }
    ]
