"""Broker write_file：沙箱写入 → collector 证据 → TrustedReceipt；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.domain.tool_capability_manifest import WRITE_FILE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_write_file_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body


def test_broker_write_file_real_bytes_and_not_done(api, objects, tmp_path: Path):
    store, _, _ = objects
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    target = workspace / "src" / "main.py"
    target.write_bytes(b"old\n")

    atomic_target = workspace / "src" / "atomic.py"
    atomic_original = b"o" * 8192
    atomic_target.write_bytes(atomic_original)
    atomic_input = json.dumps(
        {"path": "src/atomic.py", "content": "n" * 8192},
        separators=(",", ":"),
    ).encode()
    failure_probe = textwrap.dedent(
        f"""
        import resource
        import signal
        from pathlib import Path

        from execution_broker.write_file import execute_write_file

        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
        result = execute_write_file(
            Path({str(workspace)!r}),
            {atomic_input!r},
            allowed_paths=["src/**"],
            max_bytes=16384,
        )
        if result.observed_outcome != "FAILED":
            raise RuntimeError(f"write unexpectedly succeeded: {{result}}")
        """
    )
    failed_write = subprocess.run(
        [sys.executable, "-c", failure_probe],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert failed_write.returncode == 0, failed_write.stderr
    assert atomic_target.read_bytes() == atomic_original
    assert list((workspace / "src").glob(".ring-write-*")) == []

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "write-e2e",
            "allowed_tools": ["read_file", "write_file"],
            "allowed_paths": ["src/**"],
            "protected_paths": [],
            "network_allowlist": [],
            "external_actions": [],
            "secret_scope_refs": [],
        },
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert policy.status_code == 201, policy.text
    goal_body = {**goal_body, "policy_id": policy.json()["data"]["id"]}
    _drain_ready(client, token, kinds=("EXECUTE", "AUDIT", "FINALIZE", "INTEGRATE", "PLAN"))
    created = client.post(
        "/api/v1/goals",
        json=goal_body,
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201, created.text
    goal = created.json()["data"]
    started = client.post(
        f"/api/v1/goals/{goal['id']}/start",
        json={"expected_state_revision": 1, "reason": "plan"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    plan_activity = client.get(
        f"/api/v1/goals/{goal['id']}/activities",
        params={"kind": "PLAN"},
        headers=auth,
    ).json()["data"][0]

    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
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
    eng.dispose()

    subject = str(uuid4())
    _register_worker(subject, kinds=("PLAN", "EXECUTE"))
    worker_auth = {"Authorization": "Bearer " + token(subject, ["worker"])}
    claimed = client.post(
        "/internal/v1/claims",
        json={"kinds": ["PLAN"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["data"]
    profile_id = goal["contract"]["success_criteria"][0]["verification_profile_id"]
    plan = _plan_body(goal, profile_id)
    done = client.post(
        f"/internal/v1/activities/{lease['activity']['id']}/outcomes",
        json={
            "lease": lease["lease"],
            "expected_state_revision": lease["activity"]["state_revision"],
            "outcome": {"plan": plan},
        },
        headers=worker_auth,
    )
    assert done.status_code == 200, done.text
    eng = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with eng.begin() as db:
        db.execute(
            text(
                """UPDATE activities SET status='CANCELLED', updated_at=clock_timestamp()
                WHERE status='READY' AND kind='EXECUTE' AND goal_id<>:goal"""
            ),
            {"goal": goal["id"]},
        )
    eng.dispose()
    exec_claim = client.post(
        "/internal/v1/claims",
        json={"kinds": ["EXECUTE"], "capabilities": []},
        headers={**worker_auth, "Idempotency-Key": str(uuid4())},
    )
    assert exec_claim.status_code == 200, exec_claim.text
    exec_lease = exec_claim.json()["data"]
    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "write_file 修复",
            "tool_ref": "write_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    step_data = step.json()["data"]

    input_blob = json.dumps(
        {
            "tool_ref": "write_file",
            "tool_schema_digest": WRITE_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/main.py", "content": "new\n"},
        },
        separators=(",", ":"),
    ).encode()
    input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        input_digest,
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:write-input",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step_data["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "write_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["tool_ref"] == "write_file"
    assert effect["replay_class"] == "IDEMPOTENT"

    ran = run_write_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED"
    assert target.read_bytes() == b"new\n"
    assert ran["result"].before_digest != ran["result"].after_digest
    assert ran["result_artifact_ids"]

    got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert got.json()["data"]["status"] == "SUCCEEDED"
    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"

    host_file = tmp_path / "host.py"
    host_file.write_bytes(b"host-original\n")
    os.link(host_file, workspace / "src" / "host.py")
    blocked_step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": step_data["logical_step_id"],
            "purpose": "拒绝 hardlink 写入",
            "tool_ref": "write_file",
        },
        headers=worker_auth,
    )
    assert blocked_step.status_code == 201, blocked_step.text
    blocked_blob = json.dumps(
        {
            "tool_ref": "write_file",
            "tool_schema_digest": WRITE_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/host.py", "content": "overwritten\n"},
        },
        separators=(",", ":"),
    ).encode()
    blocked_digest = "sha256:" + hashlib.sha256(blocked_blob).hexdigest()
    blocked_art = Artifacts(engine, store).ingest_raw(
        project_id,
        blocked_digest,
        BytesIO(blocked_blob),
        mime="application/json",
        producer_identity="test:write-hardlink-input",
    )
    blocked_prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": blocked_step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "write_file",
            "input_artifact_id": str(blocked_art.id),
        },
        headers=worker_auth,
    )
    assert blocked_prepared.status_code == 201, blocked_prepared.text
    blocked_effect = blocked_prepared.json()["data"]
    blocked_run = run_write_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=blocked_effect,
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=blocked_blob,
    )
    assert blocked_run["result"].observed_outcome == "FAILED"
    assert "硬链接" in (blocked_run["result"].error or "")
    assert host_file.read_bytes() == b"host-original\n"

    real_dir = workspace / "src" / "real-write"
    real_dir.mkdir()
    linked_target = real_dir / "target.py"
    linked_target.write_bytes(b"linked-original\n")
    (workspace / "src" / "linked-write").symlink_to(real_dir, target_is_directory=True)
    symlink_step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": blocked_step.json()["data"]["logical_step_id"],
            "purpose": "拒绝父目录 symlink 写入",
            "tool_ref": "write_file",
        },
        headers=worker_auth,
    )
    assert symlink_step.status_code == 201, symlink_step.text
    symlink_blob = json.dumps(
        {
            "tool_ref": "write_file",
            "tool_schema_digest": WRITE_FILE_SCHEMA_DIGEST,
            "parameters": {
                "path": "src/linked-write/target.py",
                "content": "linked-overwritten\n",
            },
        },
        separators=(",", ":"),
    ).encode()
    symlink_digest = "sha256:" + hashlib.sha256(symlink_blob).hexdigest()
    symlink_art = Artifacts(engine, store).ingest_raw(
        project_id,
        symlink_digest,
        BytesIO(symlink_blob),
        mime="application/json",
        producer_identity="test:write-parent-symlink-input",
    )
    symlink_prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": symlink_step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "write_file",
            "input_artifact_id": str(symlink_art.id),
        },
        headers=worker_auth,
    )
    assert symlink_prepared.status_code == 201, symlink_prepared.text
    symlink_run = run_write_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=symlink_prepared.json()["data"],
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=symlink_blob,
    )
    assert symlink_run["result"].observed_outcome == "FAILED"
    assert "符号链接" in (symlink_run["result"].error or "")
    assert linked_target.read_bytes() == b"linked-original\n"

    large_target = workspace / "src" / "large.py"
    large_bytes = b"x" * (1024 * 1024 + 1)
    large_target.write_bytes(large_bytes)
    large_step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": symlink_step.json()["data"]["logical_step_id"],
            "purpose": "拒绝无界旧文件读取",
            "tool_ref": "write_file",
        },
        headers=worker_auth,
    )
    assert large_step.status_code == 201, large_step.text
    large_blob = json.dumps(
        {
            "tool_ref": "write_file",
            "tool_schema_digest": WRITE_FILE_SCHEMA_DIGEST,
            "parameters": {"path": "src/large.py", "content": "small\n"},
        },
        separators=(",", ":"),
    ).encode()
    large_digest = "sha256:" + hashlib.sha256(large_blob).hexdigest()
    large_art = Artifacts(engine, store).ingest_raw(
        project_id,
        large_digest,
        BytesIO(large_blob),
        mime="application/json",
        producer_identity="test:write-large-existing-input",
    )
    large_prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": large_step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "write_file",
            "input_artifact_id": str(large_art.id),
        },
        headers=worker_auth,
    )
    assert large_prepared.status_code == 201, large_prepared.text
    large_run = run_write_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=large_prepared.json()["data"],
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=large_blob,
    )
    assert large_run["result"].observed_outcome == "FAILED"
    assert "上限" in (large_run["result"].error or "")
    assert large_target.read_bytes() == large_bytes
