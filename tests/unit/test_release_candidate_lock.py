"""Codex P0-3：发布候选须锁同一 git_sha。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "assert_release_candidate_lock",
    _ROOT / "scripts" / "assert_release_candidate_lock.py",
)
assert _spec and _spec.loader
lock = importlib.util.module_from_spec(_spec)
sys.modules["assert_release_candidate_lock"] = lock
_spec.loader.exec_module(lock)


def test_assert_same_candidate_accepts_identical_sha() -> None:
    sha = "a" * 40
    got = lock.assert_same_candidate(
        [{"git_sha": sha}, {"git_sha": sha}],
        paths=[Path("m1"), Path("m2")],
    )
    assert got == sha


def test_assert_same_candidate_rejects_missing_sha() -> None:
    with pytest.raises(RuntimeError, match="MISSING_SHA"):
        lock.assert_same_candidate([{"pass": "1"}], paths=[Path("m")])


def test_assert_same_candidate_rejects_mismatch() -> None:
    with pytest.raises(RuntimeError, match="MISMATCH"):
        lock.assert_same_candidate(
            [{"git_sha": "a" * 40}, {"git_sha": "b" * 40}],
            paths=[Path("m1"), Path("m2")],
        )


def test_assert_same_candidate_rejects_expect_mismatch() -> None:
    with pytest.raises(RuntimeError, match="EXPECT_MISMATCH"):
        lock.assert_same_candidate(
            [{"git_sha": "c" * 40}],
            paths=[Path("m")],
            expect_sha="d" * 40,
        )


def test_assert_same_candidate_require_clean_rejects_dirty() -> None:
    with pytest.raises(RuntimeError, match="DIRTY"):
        lock.assert_same_candidate(
            [{"git_sha": "e" * 40, "git_dirty": "1"}],
            paths=[Path("m")],
            require_clean=True,
        )


def test_assert_same_candidate_require_clean_accepts_zero() -> None:
    sha = "f" * 40
    got = lock.assert_same_candidate(
        [{"git_sha": sha, "git_dirty": "0"}],
        paths=[Path("m")],
        require_clean=True,
        expect_sha=sha,
    )
    assert got == sha


def test_parse_manifest_reads_git_sha(tmp_path: Path) -> None:
    p = tmp_path / "MANIFEST.txt"
    p.write_text("label=x\ngit_sha=deadbeef\npass=1\n", encoding="utf-8")
    assert lock.parse_manifest(p)["git_sha"] == "deadbeef"
