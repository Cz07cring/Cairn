"""git worktree 按 base_commit 检出。"""

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from execution_broker.worktree import (
    WorkspaceUnavailable,
    ensure_attempt_workspace,
    seed_workspace_at_commit,
)


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.check_call(["git", "init"], cwd=path, stdout=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "base"],
        cwd=path,
        stdout=subprocess.DEVNULL,
    )
    (path / "src").mkdir()
    (path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "src/main.py"], cwd=path, stdout=subprocess.DEVNULL)
    subprocess.check_call(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "file"],
        cwd=path,
        stdout=subprocess.DEVNULL,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_seed_rejects_bad_commit(tmp_path: Path, monkeypatch):
    repo = tmp_path / "origin"
    _init_repo(repo)
    monkeypatch.setenv("RING_WORKSPACE_ROOT", str(tmp_path / "ws"))
    workspace = ensure_attempt_workspace(uuid4(), uuid4(), required=True)
    with pytest.raises(WorkspaceUnavailable):
        seed_workspace_at_commit(workspace, repo, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
