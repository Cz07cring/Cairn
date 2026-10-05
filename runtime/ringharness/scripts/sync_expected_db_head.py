#!/usr/bin/env python
"""同步 `EXPECTED_DB_HEAD` 到 alembic 实际 head，并重算契约 manifest。

**为什么需要本脚本**（真实事故，本会话内同族出现 **9 次**）：
新增迁移后若忘记同步该常量，`/health/ready` 会**在所有已升级到 head 的库**上恒返回 503，
而破窗只在无关测试上以 `KeyError` 暴露 —— 定位成本高、main 因此红过。
现有护栏（`tests/unit/test_expected_db_head.py`）已能**明确报错**并指出改法，
但每次都要**由人手照做**；落在集成方身上，属「靠记得」的正确性。

本脚本把「记得做」变成一条命令：
    uv run python scripts/sync_expected_db_head.py            # 报告差异
    uv run python scripts/sync_expected_db_head.py --apply      # 改常量 + 重算契约

设计口径：
- **只改一个常量 + 跑官方生成脚本**，不手改任何生成产物（契约单向，见 AGENTS §4.9）；
- 幂等：已同步时 `--apply` 也安全（无改动则明说）；
- 失败关闭：找不到 `alembic.ini`、常量行或 alembic head 时报错退出，不猜。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CONST_NAME = "EXPECTED_DB_HEAD"
APP_REL = Path("apps/control/src/control_api/app.py")
LINE_RE = re.compile(rf'^({CONST_NAME}\s*=\s*)"([^"]+)"\s*$', re.MULTILINE)


def _repo_root() -> Path:
    for cand in [Path.cwd(), *Path(__file__).resolve().parents]:
        if (cand / "alembic.ini").is_file():
            return cand
    raise SystemExit("失败关闭：未找到 alembic.ini（请在仓库内运行）")


def _alembic_head(root: Path) -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = sorted(ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads())
    if not heads:
        raise SystemExit("失败关闭：alembic 未发现任何 head（迁移目录可能损坏）")
    if len(heads) > 1:
        raise SystemExit(f"失败关闭：alembic 有多个 head {heads!r} —— 需先合并分支")
    return heads[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步 EXPECTED_DB_HEAD 到 alembic head")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只报告）")
    args = ap.parse_args(argv)

    root = _repo_root()
    app = root / APP_REL
    if not app.is_file():
        raise SystemExit(f"失败关闭：找不到 {APP_REL}")

    text = app.read_text(encoding="utf-8")
    m = LINE_RE.search(text)
    if not m:
        raise SystemExit(f"失败关闭：{APP_REL} 中未找到形如 {CONST_NAME} = \"...\" 的行")

    current, head = m.group(2), _alembic_head(root)
    print(f"{CONST_NAME}: {current}")
    print(f"alembic head : {head}")

    if current == head:
        print("已同步，无需改动。")
        return 0

    print("\n**不一致** ⇒ /health/ready 会对所有已升级到 head 的库返回 503。")
    print(f"建议改为： {CONST_NAME} = \"{head}\"")
    if not args.apply:
        print("\n（未写入；加 --apply 执行）")
        return 1

    app.write_text(LINE_RE.sub(rf'\g<1>"{head}"', text, count=1), encoding="utf-8")
    print(f"\n已写入 {APP_REL}")

    gen = root / "scripts/generate_contracts.py"
    if gen.is_file():
        print("重算契约（官方脚本，不手改生成产物）…")
        rc = subprocess.run([sys.executable, str(gen)], cwd=root, check=False).returncode
        if rc != 0:
            raise SystemExit(f"失败关闭：generate_contracts.py 退出码 {rc}")
        print("契约已重算。")
    else:
        print("提示：未找到 scripts/generate_contracts.py，请手工确认契约无漂移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
