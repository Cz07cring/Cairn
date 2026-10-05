"""Broker git_diff：固定 git argv → 证据工件 → TrustedReceipt；≠ Goal DONE。"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from control_kernel.domain.tool_capability_manifest import GIT_DIFF_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_git_diff_effect
from sqlalchemy import create_engine, text
from test_claims import _drain_ready, _register_worker
from test_goals import _ready_project
from test_plans import _plan_body


def _git_init_with_commit(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@ring.local"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ring-test"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x=1\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/main.py"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_broker_git_diff_dirty_and_not_done(api, objects, tmp_path: Path, monkeypatch):
    store, _, _ = objects
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git_init_with_commit(workspace)
    (workspace / "src" / "main.py").write_text(
        'x="' + ("z" * 300_000) + '"\n', encoding="utf-8"
    )
    expected_diff = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
    ).stdout
    assert len(expected_diff) > 200_000
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    real_git = shutil.which("git")
    assert real_git is not None
    path_git_sentinel = tmp_path / "path-git-ran"
    fake_bin = tmp_path / "candidate-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\nprintf ran > "
        + shlex.quote(str(path_git_sentinel))
        + "\nexec "
        + shlex.quote(real_git)
        + ' "$@"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    external_diff_sentinel = tmp_path / "external-diff-ran"
    external_diff = tmp_path / "external-diff"
    external_diff.write_text(
        "#!/bin/sh\nprintf ran > "
        + shlex.quote(str(external_diff_sentinel))
        + "\nexit 0\n",
        encoding="utf-8",
    )
    external_diff.chmod(0o755)
    (workspace / ".gitattributes").write_text("src/*.py diff=ringevil\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.ringevil.command", str(external_diff)],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    attacker_repo = tmp_path / "attacker-repo"
    attacker_repo.mkdir()
    _git_init_with_commit(attacker_repo)
    (attacker_repo / "src" / "main.py").write_text("attacker=1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "src/main.py"],
        cwd=attacker_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "attacker"],
        cwd=attacker_repo,
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GIT_DIR", str(attacker_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(workspace))

    client, token, auth, _project, goal_body = _ready_project(api, objects)
    policy = client.post(
        "/api/v1/policies",
        json={
            "project_id": goal_body["project_id"],
            "name": "git-diff-e2e",
            "allowed_tools": ["read_file", "git_diff"],
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
            "purpose": "git_diff dirty",
            "tool_ref": "git_diff",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text

    input_blob = json.dumps(
        {
            "tool_ref": "git_diff",
            "tool_schema_digest": GIT_DIFF_SCHEMA_DIGEST,
            "parameters": {},
        },
        separators=(",", ":"),
    ).encode()
    assert len(input_blob) < 1024, len(input_blob)
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:git-diff-input",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "git_diff",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text
    effect = prepared.json()["data"]
    assert effect["tool_ref"] == "git_diff"
    assert effect["replay_class"] == "READ_ONLY"

    monkeypatch.setenv(
        "PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
    )
    ran = run_git_diff_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=effect,
        project_id=project_id,
        workspace_root=workspace,
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED"
    assert ran["result"].dirty is True
    assert ran["result"].base_commit == expected_head
    assert ran["result_artifact_ids"]
    assert external_diff_sentinel.exists() is False
    assert path_git_sentinel.exists() is False
    meta = json.loads(ran["result"].content.decode())
    assert meta["dirty"] is True
    assert "main.py" in meta["status_preview"]
    assert meta["diff_digest"] == (
        "sha256:" + hashlib.sha256(expected_diff).hexdigest()
    )
    assert meta["diff_bytes"] == len(expected_diff)
    assert meta["diff_truncated"] is True

    got = client.get(f"/api/v1/effects/{effect['id']}", headers=auth)
    assert got.json()["data"]["status"] == "SUCCEEDED"

    timeout_step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": step.json()["data"]["logical_step_id"],
            "purpose": "git_diff typed timeout",
            "tool_ref": "git_diff",
        },
        headers=worker_auth,
    )
    assert timeout_step.status_code == 201, timeout_step.text
    timeout_prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": timeout_step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "git_diff",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert timeout_prepared.status_code == 201, timeout_prepared.text
    timeout_effect = timeout_prepared.json()["data"]
    timed_out = run_git_diff_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=timeout_effect,
        project_id=project_id,
        workspace_root=workspace,
        input_bytes=input_blob,
        timeout_seconds=0,
    )
    assert timed_out["result"].observed_outcome == "FAILED"
    assert timed_out["result"].exit_code == 124
    assert timed_out["result"].timed_out is True
    timeout_got = client.get(
        f"/api/v1/effects/{timeout_effect['id']}", headers=auth
    )
    assert timeout_got.json()["data"]["status"] == "FAILED"

    goal_got = client.get(f"/api/v1/goals/{goal['id']}", headers=auth)
    assert goal_got.json()["data"]["status"] != "DONE"
