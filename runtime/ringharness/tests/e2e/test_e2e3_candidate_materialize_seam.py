"""E2E-3 物化缝：封存后从对象仓重建 Auditor 树；holdout 非候选；≠ Goal DONE。"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import shutil
import stat
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from control_kernel.domain.tool_capability_manifest import SEAL_CANDIDATE_SCHEMA_DIGEST
from control_kernel.storage.artifacts import Artifacts
from e2e3_order_helpers import ALLOW, boot_execute_for_seal, run_pytest
from execution_broker import (
    MaterializeRejected,
    attach_holdout_tests,
    materialize_candidate_worktree,
    run_seal_candidate_effect,
)

_ROOT = Path(__file__).resolve().parents[2]
_HOLDOUT = _ROOT / "tests" / "fixtures" / "business_e2e" / "order_service" / "tests_hidden"


def test_e2e3_materialize_sealed_candidate_for_auditor(
    api, objects, tmp_path: Path, monkeypatch
):
    if not os.environ.get("RING_TEST_DATABASE_URL"):
        pytest.skip("RING_TEST_DATABASE_URL required")

    ctx = boot_execute_for_seal(api, objects, tmp_path, run_id="e2e3-mat")
    run = ctx["run"]
    seed = ctx["seed"]
    client, auth = ctx["client"], ctx["auth"]

    # 修复仅在 Executor；不碰 Auditor 树（本缝用物化取代 apply_reference_fix）
    seed.apply_reference_fix(run.executor_worktree)
    assert (run.executor_worktree / "tests_hidden").exists() is False

    step = client.post(
        f"/internal/v1/activities/{ctx['exec_lease']['activity']['id']}/steps",
        json={
            "lease": ctx["exec_lease"]["lease"],
            "predecessor_step_id": None,
            "purpose": "e2e3 seal for materialize",
            "tool_ref": "seal_candidate",
        },
        headers=ctx["exec_auth"],
    )
    assert step.status_code == 201, step.text
    input_blob = json.dumps(
        {
            "tool_ref": "seal_candidate",
            "tool_schema_digest": SEAL_CANDIDATE_SCHEMA_DIGEST,
            "parameters": {"verification_profile_ids": [ctx["profile_id"]]},
        },
        separators=(",", ":"),
    ).encode()
    input_art = Artifacts(ctx["engine"], ctx["store"]).ingest_raw(
        ctx["project_id"],
        "sha256:" + hashlib.sha256(input_blob).hexdigest(),
        BytesIO(input_blob),
        mime="application/json",
        producer_identity="test:e2e3-mat-seal",
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
    assert sealed["candidate"] is not None, sealed.get("seal_error")
    candidate = sealed["candidate"]
    assert all(not f["path"].startswith("tests_hidden/") for f in candidate["files"])

    # 从对象仓物化到全新目录（不复用 seed auditor_worktree）
    mat_root = run.artifacts / "auditor_materialized"
    if mat_root.exists():
        shutil.rmtree(mat_root)
    store = ctx["store"]

    def read_bytes(digest: str) -> bytes:
        return store.read(UUID(str(ctx["project_id"])), digest)

    limit_bodies = {"src/a.py": b"aaa", "src/b.py": b"bbb"}
    limit_files = [
        {
            "path": path,
            "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            "mode": "100644",
        }
        for path, body in limit_bodies.items()
    ]
    limit_blobs = {
        entry["digest"]: limit_bodies[entry["path"]] for entry in limit_files
    }
    count_root = run.artifacts / "auditor_materialize_too_many"
    with pytest.raises(MaterializeRejected, match="文件数"):
        materialize_candidate_worktree(
            count_root,
            limit_files,
            read_bytes=lambda digest: limit_blobs[digest],
            max_files=1,
        )
    assert count_root.exists() is False

    file_size_root = run.artifacts / "auditor_materialize_file_too_large"
    with pytest.raises(MaterializeRejected, match="单文件"):
        materialize_candidate_worktree(
            file_size_root,
            [limit_files[0]],
            read_bytes=lambda digest: limit_blobs[digest],
            max_file_bytes=2,
        )
    assert file_size_root.exists() is False

    total_size_root = run.artifacts / "auditor_materialize_total_too_large"
    with pytest.raises(MaterializeRejected, match="总字节"):
        materialize_candidate_worktree(
            total_size_root,
            limit_files,
            read_bytes=lambda digest: limit_blobs[digest],
            max_file_bytes=3,
            max_total_bytes=5,
        )
    assert total_size_root.exists() is False

    attack_root = run.artifacts / "auditor_materialize_attack"
    outside_root = tmp_path / "outside-materialize"
    outside_root.mkdir()
    attack_body = b"must-stay-inside-auditor-root\n"
    attack_digest = "sha256:" + hashlib.sha256(attack_body).hexdigest()

    def inject_parent_symlink(_digest: str) -> bytes:
        (attack_root / "order_service").symlink_to(
            outside_root, target_is_directory=True
        )
        return attack_body

    with pytest.raises(MaterializeRejected, match="符号链接|安全创建"):
        materialize_candidate_worktree(
            attack_root,
            [
                {
                    "path": "order_service/escaped.py",
                    "digest": attack_digest,
                    "mode": "100644",
                }
            ],
            read_bytes=inject_parent_symlink,
            read_only=True,
        )
    assert (outside_root / "escaped.py").exists() is False
    assert attack_root.exists() is False

    n = materialize_candidate_worktree(
        mat_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    assert n == len(candidate["files"])
    assert (mat_root / "tests_hidden").exists() is False
    assert (mat_root / "order_service" / "store.py").is_file()

    outside_holdout = tmp_path / "outside-hidden.py"
    outside_holdout.write_text("def test_outside():\n    assert False\n", encoding="utf-8")
    malicious_holdout = tmp_path / "malicious-holdout"
    malicious_holdout.mkdir()
    (malicious_holdout / "test_linked.py").symlink_to(outside_holdout)
    with pytest.raises(MaterializeRejected, match="符号链接"):
        attach_holdout_tests(mat_root, malicious_holdout)
    assert (mat_root / "tests_hidden").exists() is False
    assert mat_root.stat().st_mode & stat.S_IWUSR == 0

    # 源目录预检后替换普通文件：路径式复制会跟随新链接，
    # 并把保护域外字节伪装成 Auditor 树内的普通文件。
    race_root = run.artifacts / "auditor_materialize_holdout_race"
    materialize_candidate_worktree(
        race_root, candidate["files"], read_bytes=read_bytes, read_only=True
    )
    outside_race = tmp_path / "outside-race.py"
    outside_race.write_text("OUTSIDE_HOLDOUT_BYTES = True\n", encoding="utf-8")
    racing_holdout = tmp_path / "racing-holdout"
    racing_holdout.mkdir()
    racing_victim = racing_holdout / "test_victim.py"
    racing_victim.write_text("def test_safe():\n    assert True\n", encoding="utf-8")
    source_identity = racing_holdout.stat()
    original_builtin_open = builtins.open
    original_os_open = os.open
    race_injected = False

    def _swap_victim() -> None:
        nonlocal race_injected
        if race_injected:
            return
        racing_victim.unlink()
        racing_victim.symlink_to(outside_race)
        race_injected = True

    def _racing_builtin_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and Path(file) == racing_victim:
            _swap_victim()
        return original_builtin_open(file, *args, **kwargs)

    def _racing_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == racing_victim.name and dir_fd is not None:
            metadata = os.fstat(dir_fd)
            if (
                metadata.st_dev == source_identity.st_dev
                and metadata.st_ino == source_identity.st_ino
            ):
                _swap_victim()
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as race_patch:
        race_patch.setattr(builtins, "open", _racing_builtin_open)
        race_patch.setattr(os, "open", _racing_os_open)
        with pytest.raises(MaterializeRejected, match="符号链接|源.*变化|无法安全"):
            attach_holdout_tests(race_root, racing_holdout)
    assert race_injected is True
    assert (race_root / "tests_hidden").exists() is False
    assert race_root.stat().st_mode & stat.S_IWUSR == 0

    bounded_holdout = tmp_path / "bounded-holdout"
    bounded_holdout.mkdir()
    (bounded_holdout / "test_a.py").write_bytes(b"aaa")
    (bounded_holdout / "test_b.py").write_bytes(b"bbb")
    limit_cases = (
        ("files", {"max_files": 1}, "文件数"),
        ("single", {"max_file_bytes": 2}, "单文件"),
        ("total", {"max_total_bytes": 5}, "总字节"),
    )
    for suffix, limits, error_pattern in limit_cases:
        bounded_root = run.artifacts / f"auditor_holdout_limit_{suffix}"
        materialize_candidate_worktree(
            bounded_root,
            candidate["files"],
            read_bytes=read_bytes,
            read_only=True,
        )
        with pytest.raises(MaterializeRejected, match=error_pattern):
            attach_holdout_tests(bounded_root, bounded_holdout, **limits)
        assert (bounded_root / "tests_hidden").exists() is False
        assert bounded_root.stat().st_mode & stat.S_IWUSR == 0

    entries_holdout = tmp_path / "entries-holdout"
    (entries_holdout / "a").mkdir(parents=True)
    (entries_holdout / "b").mkdir()
    entries_root = run.artifacts / "auditor_holdout_limit_entries"
    materialize_candidate_worktree(
        entries_root,
        candidate["files"],
        read_bytes=read_bytes,
        read_only=True,
    )
    with pytest.raises(MaterializeRejected, match="目录项"):
        attach_holdout_tests(entries_root, entries_holdout, max_entries=1)
    assert (entries_root / "tests_hidden").exists() is False
    assert entries_root.stat().st_mode & stat.S_IWUSR == 0

    depth_holdout = tmp_path / "depth-holdout"
    (depth_holdout / "a" / "b").mkdir(parents=True)
    depth_root = run.artifacts / "auditor_holdout_limit_depth"
    materialize_candidate_worktree(
        depth_root,
        candidate["files"],
        read_bytes=read_bytes,
        read_only=True,
    )
    with pytest.raises(MaterializeRejected, match="深度"):
        attach_holdout_tests(depth_root, depth_holdout, max_depth=1)
    assert (depth_root / "tests_hidden").exists() is False
    assert depth_root.stat().st_mode & stat.S_IWUSR == 0

    attach_holdout_tests(mat_root, _HOLDOUT)
    assert (mat_root / "tests_hidden").is_dir()

    # 物化树 + holdout 应绿；候选自身不含 holdout
    green = run_pytest(mat_root, "tests/", "tests_hidden/")
    assert green.returncode == 0, green.stdout + green.stderr

    summary = {
        "phase": "E2E-3-materialize",
        "candidate_manifest_id": candidate["id"],
        "materialized_files": n,
        "holdout_attached": True,
        "marks_goal_done": False,
        "non_goals": ["full_profile_layers", "integrate_finalize_release", "goal_done"],
    }
    (run.artifacts / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 本缝不写 Goal DONE；仅证明物化可跑通 Auditor 输入
    goal_after = client.get(f"/api/v1/goals/{ctx['goal']['id']}", headers=auth).json()["data"]
    assert goal_after["status"] != "DONE"
