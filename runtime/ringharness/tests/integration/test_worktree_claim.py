"""claim EXECUTE 返回独占 workspace_root，Broker 只读该目录。"""

import hashlib
import shlex
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from control_kernel.storage.artifacts import Artifacts
from execution_broker import run_read_file_effect
from execution_broker.worktree import (
    WorkspaceUnavailable,
    ensure_attempt_workspace,
    seed_workspace_at_commit,
)
from test_effects import _publish_and_claim_execute


def test_execute_claim_isolates_workspace(api, objects, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RING_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    store, _, _ = objects
    client, _token, _auth, _goal, worker_auth, exec_lease = _publish_and_claim_execute(
        api, objects
    )
    # _publish_and_claim_execute 内部 claim 时已带 env
    assert exec_lease.get("workspace_root"), exec_lease
    workspace = Path(exec_lease["workspace_root"])
    assert workspace.is_dir()
    assert (workspace / ".ring-attempt").is_file()

    # 写入 attempt 工作区；不得依赖测试随意路径
    (workspace / "src").mkdir(parents=True)
    file_bytes = b"print('isolated')\n"
    (workspace / "src" / "main.py").write_bytes(file_bytes)

    client.app.state.objects = store
    activity_id = exec_lease["activity"]["id"]
    project_id = UUID(exec_lease["activity"]["project_id"])
    engine = client.app.state.engine

    step = client.post(
        f"/internal/v1/activities/{activity_id}/steps",
        json={
            "lease": exec_lease["lease"],
            "predecessor_step_id": None,
            "purpose": "读取入口",
            "tool_ref": "read_file",
        },
        headers=worker_auth,
    )
    assert step.status_code == 201, step.text
    input_blob = b'{"path":"src/main.py"}'
    input_digest = "sha256:" + hashlib.sha256(input_blob).hexdigest()
    input_art = Artifacts(engine, store).ingest_raw(
        project_id,
        input_digest,
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:input",
    )
    prepared = client.post(
        "/internal/v1/effects/prepare",
        json={
            "lease": exec_lease["lease"],
            "logical_step_id": step.json()["data"]["logical_step_id"],
            "intent_revision": 1,
            "tool_ref": "read_file",
            "input_artifact_id": str(input_art.id),
        },
        headers=worker_auth,
    )
    assert prepared.status_code == 201, prepared.text

    ran = run_read_file_effect(
        client,
        worker_auth=worker_auth,
        lease=exec_lease["lease"],
        effect=prepared.json()["data"],
        project_id=project_id,
        workspace_root=workspace,
        allowed_paths=["src/**"],
        input_bytes=input_blob,
    )
    assert ran["result"].observed_outcome == "SUCCEEDED"
    assert ran["result"].content == file_bytes

    # 其他 attempt 路径不可见
    other = tmp_path / "workspaces" / "projects" / str(project_id) / "attempts" / "00000000-0000-0000-0000-000000000099"
    other.mkdir(parents=True)
    (other / "src").mkdir()
    (other / "src" / "main.py").write_bytes(b"leak")
    assert (workspace / "src" / "main.py").read_bytes() == file_bytes

    linked_attempt = uuid4()
    linked_path = other.parent / str(linked_attempt)
    linked_path.symlink_to(other, target_is_directory=True)
    with pytest.raises(WorkspaceUnavailable, match="符号链接|安全打开"):
        ensure_attempt_workspace(project_id, linked_attempt, required=True)
    assert (other / ".ring-attempt").exists() is False

    repo = tmp_path / "origin"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "main.py").write_text("print('seeded')\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@ring.local",
            "-c",
            "user.name=ring-test",
            "commit",
            "-m",
            "seed",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    timeout_workspace = ensure_attempt_workspace(project_id, uuid4(), required=True)
    assert timeout_workspace is not None
    with pytest.raises(WorkspaceUnavailable, match="超时"):
        seed_workspace_at_commit(
            timeout_workspace,
            repo,
            sha,
            git_timeout_seconds=0,
        )

    hook = repo / ".git" / "hooks" / "post-checkout"
    hook_sentinel = tmp_path / "post-checkout-ran"
    hook.write_text(
        "#!/bin/sh\nprintf ran > "
        + shlex.quote(str(hook_sentinel))
        + "\nexit 77\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    seed_attempt = uuid4()
    seeded_workspace = ensure_attempt_workspace(project_id, seed_attempt, required=True)
    assert seeded_workspace is not None
    script = (
        "from pathlib import Path\n"
        "from execution_broker.worktree import seed_workspace_at_commit\n"
        f"print(seed_workspace_at_commit(Path({str(seeded_workspace)!r}), "
        f"Path({str(repo)!r}), {sha!r}, git_timeout_seconds=3))\n"
    )
    seeded = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert seeded.returncode == 0, seeded.stderr
    assert seeded.stdout.strip() == sha
    assert (seeded_workspace / "main.py").read_text(encoding="utf-8") == (
        "print('seeded')\n"
    )
    assert (seeded_workspace / ".ring-attempt").read_text(encoding="utf-8") == (
        f"{project_id}:{seed_attempt}\n"
    )
    assert hook_sentinel.exists() is False

    hook.unlink()
    (repo / ".gitattributes").write_text("*.py filter=ringevil\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@ring.local",
            "-c",
            "user.name=ring-test",
            "commit",
            "-m",
            "add filter attributes",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    filtered_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    smudge_sentinel = tmp_path / "smudge-ran"
    smudge = tmp_path / "smudge-filter"
    smudge.write_text(
        "#!/bin/sh\ncat\nprintf ran > "
        + shlex.quote(str(smudge_sentinel))
        + "\n",
        encoding="utf-8",
    )
    smudge.chmod(0o755)
    subprocess.run(
        ["git", "config", "filter.ringevil.smudge", str(smudge)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    filter_attempt = uuid4()
    filter_workspace = ensure_attempt_workspace(
        project_id, filter_attempt, required=True
    )
    assert filter_workspace is not None
    with pytest.raises(WorkspaceUnavailable, match="filter"):
        seed_workspace_at_commit(
            filter_workspace,
            repo,
            filtered_sha,
            git_timeout_seconds=3,
        )
    assert smudge_sentinel.exists() is False
    assert (filter_workspace / ".ring-attempt").read_text(encoding="utf-8") == (
        f"{project_id}:{filter_attempt}\n"
    )

    subprocess.run(
        ["git", "config", "--unset", "filter.ringevil.smudge"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / ".gitattributes").write_text("*.py filter=envbad\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@ring.local",
            "-c",
            "user.name=ring-test",
            "commit",
            "-m",
            "switch to env filter",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    env_filtered_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env_smudge_sentinel = tmp_path / "env-smudge-ran"
    env_smudge = tmp_path / "env-smudge-filter"
    env_smudge.write_text(
        "#!/bin/sh\ncat\nprintf ran > "
        + shlex.quote(str(env_smudge_sentinel))
        + "\n",
        encoding="utf-8",
    )
    env_smudge.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.envbad.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(env_smudge))
    env_filter_attempt = uuid4()
    env_filter_workspace = ensure_attempt_workspace(
        project_id, env_filter_attempt, required=True
    )
    assert env_filter_workspace is not None
    seeded_env_filter = seed_workspace_at_commit(
        env_filter_workspace,
        repo,
        env_filtered_sha,
        git_timeout_seconds=3,
    )
    assert seeded_env_filter == env_filtered_sha
    assert env_smudge_sentinel.exists() is False
    assert (env_filter_workspace / "main.py").read_text(encoding="utf-8") == (
        "print('seeded')\n"
    )

    marker_target = tmp_path / "outside-marker-target"
    marker_target.write_bytes(b"outside-must-not-change\n")
    (repo / ".ring-attempt").symlink_to(marker_target)
    subprocess.run(
        ["git", "add", ".ring-attempt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@ring.local",
            "-c",
            "user.name=ring-test",
            "commit",
            "-m",
            "add malicious attempt marker",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    marker_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    marker_attempt = uuid4()
    marker_workspace = ensure_attempt_workspace(
        project_id, marker_attempt, required=True
    )
    assert marker_workspace is not None
    with pytest.raises(WorkspaceUnavailable, match="marker|保留"):
        seed_workspace_at_commit(
            marker_workspace,
            repo,
            marker_sha,
            git_timeout_seconds=3,
        )
    assert marker_target.read_bytes() == b"outside-must-not-change\n"
    assert (marker_workspace / ".ring-attempt").is_symlink() is False
    assert (marker_workspace / ".ring-attempt").read_text(encoding="utf-8") == (
        "QUARANTINED\n"
    )
    with pytest.raises(WorkspaceUnavailable, match="身份不匹配"):
        ensure_attempt_workspace(project_id, marker_attempt, required=True)

    detached_code = (
        "import subprocess,sys\n"
        "sys.stderr.write('y' * 300000); sys.stderr.flush()\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=sys.stderr, "
        "start_new_session=True)\n"
    )
    leak_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from execution_broker.bounded_process import run_bounded_process\n"
        "try:\n"
        f"    run_bounded_process([sys.executable, '-c', {detached_code!r}], "
        f"cwd=Path({str(tmp_path)!r}), timeout_seconds=3, "
        "max_stdout_bytes=4096, max_stderr_bytes=8192)\n"
        "except OSError as exc:\n"
        "    print(f'rejected:{exc}')\n"
        "else:\n"
        "    raise RuntimeError('detached pipe holder was accepted')\n"
    )
    leaked = subprocess.run(
        [sys.executable, "-c", leak_script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert leaked.returncode == 0, leaked.stderr
    assert leaked.stdout.startswith("rejected:"), leaked.stdout
