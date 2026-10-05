"""迁移版本漂移护栏：`EXPECTED_DB_HEAD` 必须等于 alembic 的实际 head。

**为什么需要本测试**（真实事故）：`/health/ready` 原先把期望版本硬编码为字面量
`"0033_trust_invalidation"`，而新增迁移后 head 已推进到 `0037_model_exposed_tools`：
就绪端点在**任何已升级到 head 的数据库**上恒返回 503，破坏面却只在无关测试
（`/health/ready` 取 `["scope"]`）上以 `KeyError` 暴露 —— 定位成本高，且 main
因此确定性地红过一次。

本测试把「忘记同步版本常量」从**运行时静默降级**变成**提交前明确失败**：
信息直指要改哪个常量、改成什么值。
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from control_api.app import EXPECTED_DB_HEAD


def _repo_root() -> Path:
    """定位仓库根（含 alembic.ini）。"""
    for candidate in [Path.cwd(), *Path(__file__).resolve().parents]:
        if (candidate / "alembic.ini").is_file():
            return candidate
    raise AssertionError("未找到 alembic.ini；本测试需在仓库内运行")


def _alembic_heads() -> list[str]:
    script = ScriptDirectory.from_config(Config(str(_repo_root() / "alembic.ini")))
    return sorted(script.get_heads())


def test_expected_db_head_matches_alembic_head() -> None:
    """新增迁移后必须同步 `EXPECTED_DB_HEAD`，否则就绪端点会拒绝一切已升级的库。"""
    heads = _alembic_heads()
    assert heads, "alembic 未发现任何 head —— 迁移目录可能被破坏"
    assert EXPECTED_DB_HEAD in heads, (
        f"EXPECTED_DB_HEAD={EXPECTED_DB_HEAD!r} 已不是 alembic head {heads!r}；"
        "新增迁移后请同步 apps/control/src/control_api/app.py 的 EXPECTED_DB_HEAD，"
        "否则 /health/ready 会对所有已升级到 head 的数据库返回 503"
    )


def test_exactly_one_head() -> None:
    """多 head 说明存在未合并的迁移分支，`alembic upgrade head` 语义将不明确。"""
    heads = _alembic_heads()
    assert len(heads) == 1, f"存在多个迁移 head：{heads!r}；请先合并迁移分支"
