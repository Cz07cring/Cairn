#!/usr/bin/env python3
"""Codex P0-3：发布候选证据必须锁在同一 git SHA。

用法：
  uv run python scripts/assert_release_candidate_lock.py OUT_DIR [OUT_DIR ...]
  uv run python scripts/assert_release_candidate_lock.py --expect-sha HEAD MANIFEST.txt
  uv run python scripts/assert_release_candidate_lock.py --self-test

失败关闭（退出码 3）：缺 git_sha、多份清单 SHA 不一致、与 --expect-sha 不符、
或 --require-clean 时 git_dirty≠0。
本脚本不做业务裁决；≠ Goal DONE。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_manifest(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def resolve_manifest_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_dir():
        cand = p / "MANIFEST.txt"
        if not cand.is_file():
            raise FileNotFoundError(f"目录内无 MANIFEST.txt: {p}")
        return cand
    if not p.is_file():
        raise FileNotFoundError(f"清单不存在: {p}")
    return p


def resolve_expect_sha(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.upper() == "HEAD":
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        return out
    return value


def assert_same_candidate(
    manifests: list[dict[str, str]],
    *,
    paths: list[Path],
    expect_sha: str | None = None,
    require_clean: bool = False,
) -> str:
    """返回锁定的完整 git_sha；失败抛 RuntimeError。"""
    if not manifests:
        raise RuntimeError("RELEASE_CANDIDATE_LOCK_EMPTY: 未提供任何 MANIFEST")

    shas: list[str] = []
    for path, data in zip(paths, manifests, strict=True):
        sha = (data.get("git_sha") or "").strip()
        if not sha or sha in {"unset", "unknown"}:
            raise RuntimeError(
                f"RELEASE_CANDIDATE_LOCK_MISSING_SHA: {path} 缺少 git_sha"
                "（Codex P0-3：禁止拼无锁版证据）"
            )
        shas.append(sha)
        if require_clean:
            dirty = (data.get("git_dirty") or "").strip()
            if dirty not in {"0", "false", "False"}:
                raise RuntimeError(
                    f"RELEASE_CANDIDATE_LOCK_DIRTY: {path} git_dirty={dirty or '<缺>'}"
                    "（发布候选须干净树；运行期间提交/脏树禁止拼包）"
                )

    unique = sorted(set(shas))
    if len(unique) != 1:
        detail = "; ".join(f"{p.name}={s[:12]}" for p, s in zip(paths, shas, strict=True))
        raise RuntimeError(
            "RELEASE_CANDIDATE_LOCK_MISMATCH: 多份证据 git_sha 不一致"
            f"（{detail}；Codex P0-3：禁止跨提交拼发布候选）"
        )

    locked = unique[0]
    if expect_sha is not None and locked != expect_sha:
        raise RuntimeError(
            "RELEASE_CANDIDATE_LOCK_EXPECT_MISMATCH: "
            f"清单 git_sha={locked[:12]}… ≠ expect={expect_sha[:12]}…"
            "（Codex P0-3：须在锁版 SHA 上重跑；禁止用结束时 HEAD 冒充启动锁）"
        )
    return locked


def _self_test() -> int:
    ok = 0
    # 同 SHA
    try:
        assert_same_candidate(
            [{"git_sha": "abc" * 10 + "abcd"}, {"git_sha": "abc" * 10 + "abcd"}],
            paths=[Path("a"), Path("b")],
        )
        ok += 1
    except RuntimeError as exc:
        print(f"SELF_TEST_FAIL same-sha: {exc}", file=sys.stderr)
        return 1
    # 缺字段
    try:
        assert_same_candidate([{"pass": "1"}], paths=[Path("x")])
        print("SELF_TEST_FAIL missing-sha 应抛错", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        if "MISSING_SHA" not in str(exc):
            print(f"SELF_TEST_FAIL missing 文案: {exc}", file=sys.stderr)
            return 1
        ok += 1
    # 不一致
    try:
        assert_same_candidate(
            [{"git_sha": "a" * 40}, {"git_sha": "b" * 40}],
            paths=[Path("a"), Path("b")],
        )
        print("SELF_TEST_FAIL mismatch 应抛错", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        if "MISMATCH" not in str(exc):
            print(f"SELF_TEST_FAIL mismatch 文案: {exc}", file=sys.stderr)
            return 1
        ok += 1
    # expect
    try:
        assert_same_candidate(
            [{"git_sha": "c" * 40}],
            paths=[Path("c")],
            expect_sha="d" * 40,
        )
        print("SELF_TEST_FAIL expect 应抛错", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        if "EXPECT_MISMATCH" not in str(exc):
            print(f"SELF_TEST_FAIL expect 文案: {exc}", file=sys.stderr)
            return 1
        ok += 1
    # require-clean：脏树拒绝
    try:
        assert_same_candidate(
            [{"git_sha": "e" * 40, "git_dirty": "1"}],
            paths=[Path("e")],
            require_clean=True,
        )
        print("SELF_TEST_FAIL dirty 应抛错", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        if "DIRTY" not in str(exc):
            print(f"SELF_TEST_FAIL dirty 文案: {exc}", file=sys.stderr)
            return 1
        ok += 1
    # require-clean：干净通过
    try:
        assert_same_candidate(
            [{"git_sha": "f" * 40, "git_dirty": "0"}],
            paths=[Path("f")],
            require_clean=True,
        )
        ok += 1
    except RuntimeError as exc:
        print(f"SELF_TEST_FAIL clean: {exc}", file=sys.stderr)
        return 1
    print(f"OK ({ok}/6)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Codex P0-3：校验多份 soak/MANIFEST 锁在同一 git_sha"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="MANIFEST.txt 或含 MANIFEST.txt 的 soak 输出目录",
    )
    parser.add_argument(
        "--expect-sha",
        default=None,
        help="期望 SHA（可用 HEAD，但发布候选包应传启动时锁定的完整 SHA）",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="要求 MANIFEST.git_dirty=0（发布候选证据包）",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="不连仓：跑内建闸门用例",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.paths:
        print("失败关闭：须提供至少一份 MANIFEST / OUT 目录", file=sys.stderr)
        return 2

    try:
        paths = [resolve_manifest_path(p) for p in args.paths]
        manifests = [parse_manifest(p) for p in paths]
        expect = resolve_expect_sha(args.expect_sha)
        locked = assert_same_candidate(
            manifests,
            paths=paths,
            expect_sha=expect,
            require_clean=bool(args.require_clean),
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"失败关闭：{exc}", file=sys.stderr)
        return 3 if isinstance(exc, RuntimeError) else 2

    print(f"发布候选锁版 OK：git_sha={locked}（Codex P0-3；≠ Goal DONE）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
