"""业务指标采集器（`scripts/report_business_metrics.py`）的门禁。

**为什么需要测试**：该采集器的输出会被用来判断「10–20 个真实任务跑得好不好」，
以及 8h/24h/100h 长跑的结论（用户路线 #4/#5/#6）。一个**悄悄算错**的指标比没有指标更糟 ——
它会让人对着错误数字做决策。故钉住三件事：

1. **失败关闭**：无库地址必须拒绝运行（不得退化成「空报告 = 全 0%」）；
2. **必须带分母**：每个比率都可给出分子/分母，且 `0/0` 显示为 n/a 而非 `0%`；
3. **不做 DONE 判定**：输出里不得出现「已完成/达标」式的业务裁决措辞，
   且上下文里必须带「运营观测，非业务裁决」的口径声明。

DB 相关的用例在缺 `RING_TEST_DATABASE_URL` 时**显式 skip 并说明原因**（缺环境 ≠ 能力缺）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "report_business_metrics", _ROOT / "scripts" / "report_business_metrics.py"
)
assert _spec and _spec.loader, "采集器脚本缺失"
metrics = importlib.util.module_from_spec(_spec)
sys.modules["report_business_metrics"] = metrics
_spec.loader.exec_module(metrics)


# ---------------- 1) 失败关闭 ----------------


def test_connect_fails_closed_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """无库地址时拒绝运行 —— 否则会输出一份「空的全 0% 报告」冒充真实采集。"""
    monkeypatch.delenv("RING_DATABASE_URL", raising=False)
    monkeypatch.delenv("RING_TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="RING_DATABASE_URL"):
        metrics._connect()


def test_main_returns_nonzero_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 层同样失败关闭（退出码非 0，供长跑脚本判定）。"""
    monkeypatch.delenv("RING_DATABASE_URL", raising=False)
    monkeypatch.delenv("RING_TEST_DATABASE_URL", raising=False)
    assert metrics.main([]) == 2


# ---------------- 2) 比率必须带分母 ----------------


def test_ratio_reports_zero_denominator_as_na_not_zero_percent() -> None:
    """**关键**：0/0 必须显示 n/a —— 显示 0% 会让人误读成「成功率为零」。

    该场景真实存在：最近创建的 Goal 常常还没有任何模型调用/审计（分母为 0）。
    """
    empty = metrics.Ratio(numerator=0, denominator=0, note="无样本")
    assert empty.rate is None
    assert "n/a" in empty.render()
    assert "0.0%" not in empty.render()


def test_ratio_keeps_numerator_and_denominator_visible() -> None:
    """比率必须同时暴露分子与分母（估算数字与真实数字必须可区分）。"""
    r = metrics.Ratio(numerator=16, denominator=497)
    assert r.as_dict() == {
        "numerator": 16,
        "denominator": 497,
        "rate": pytest.approx(16 / 497),
        "note": "",
    }
    assert "16/497" in r.render()


def test_ratio_from_dict_roundtrips() -> None:
    """`as_dict()` 含派生字段 `rate`，还原时不得炸（曾经真实踩到）。"""
    original = metrics.Ratio(numerator=3, denominator=4, note="x")
    restored = metrics._ratio_from_dict(original.as_dict())
    assert (restored.numerator, restored.denominator, restored.note) == (3, 4, "x")


# ---------------- 3) 窗口作用域与污染提示 ----------------


def test_scope_sql_is_empty_for_full_database() -> None:
    scope, params = metrics._scope_sql(hours=None, goal_id=None, project_id=None)
    assert scope == "" and params == {}


def test_scope_sql_filters_by_hours_and_goal() -> None:
    scope, params = metrics._scope_sql(hours=24, goal_id="abc", project_id=None)
    assert "created_at >=" in scope
    assert "g.id = CAST(:goal_id AS uuid)" in scope
    assert params["hours"] == 24 and params["goal_id"] == "abc"


def test_pollution_warning_flags_test_heavy_database() -> None:
    """大量 Goal 未走完 ⇒ 提示整体比率不可作验收依据（共享开发库的真实状况）。"""
    warn = metrics._pollution_warning({"PLANNING": 4508, "RUNNING": 3783, "DONE": 2276})
    assert warn is not None
    assert "不可作为验收依据" in warn


def test_pollution_warning_silent_on_healthy_window() -> None:
    """健康窗口（多数已终态）不应告警 —— 否则告警会被无视。"""
    assert metrics._pollution_warning({"DONE": 90, "FAILED": 5, "RUNNING": 5}) is None


def test_pollution_warning_silent_on_small_sample() -> None:
    """样本太少不告警（避免刚起一个 Goal 就报警）。"""
    assert metrics._pollution_warning({"RUNNING": 3}) is None


# ---------------- 4) 真实库上的形状不变量 ----------------


def _engine_or_skip():
    url = (
        metrics.os.environ.get("RING_TEST_DATABASE_URL")
        or metrics.os.environ.get("RING_DATABASE_URL")
        or ""
    ).strip()
    if not url:
        pytest.skip("需要测试库 URL（RING_TEST_DATABASE_URL）")
    return metrics.create_engine(url)


def test_collect_produces_four_metrics_with_valid_ratios() -> None:
    """四项指标齐备，且每个比率的分子 ≤ 分母（比率 >100% 一定是算错了）。"""
    db = _engine_or_skip()
    report = metrics.collect(db)
    data = report.as_dict()

    for key in (
        "model_first_attempt_success",
        "retry_success",
        "auditor_blocking",
        "unknown_recovery",
    ):
        assert key in data["metrics"], f"缺少指标：{key}"

    ratios = [
        data["metrics"]["model_first_attempt_success"],
        data["metrics"]["retry_success"],
        data["metrics"]["auditor_blocking"]["overall"],
        data["metrics"]["unknown_recovery"]["recovery"],
    ]
    ratios += list(data["metrics"]["auditor_blocking"]["by_layer"].values())
    for ratio in ratios:
        assert 0 <= ratio["numerator"] <= ratio["denominator"], f"比率越界：{ratio}"


def test_require_model_invocation_ledger_fails_closed_when_empty() -> None:
    """Codex P0-1：分母为 0 必须失败关闭，不得当验收绿。"""
    empty = {
        "metrics": {
            "model_first_attempt_success": {
                "numerator": 0,
                "denominator": 0,
                "rate": None,
                "note": "无样本",
            }
        }
    }
    with pytest.raises(RuntimeError, match="MODEL_INVOCATION_LEDGER_EMPTY"):
        metrics.require_model_invocation_ledger(empty)


def test_require_model_invocation_ledger_accepts_positive_denominator() -> None:
    ok = {
        "metrics": {
            "model_first_attempt_success": {
                "numerator": 2,
                "denominator": 3,
                "rate": 2 / 3,
                "note": "",
            }
        }
    }
    assert metrics.require_model_invocation_ledger(ok) == 3


def test_require_fault_metric_samples_fails_closed_when_empty() -> None:
    empty = {
        "metrics": {
            "unknown_recovery": {
                "recovery": {"numerator": 0, "denominator": 0, "rate": None, "note": ""}
            },
            "retry_success": {"numerator": 0, "denominator": 0, "rate": None, "note": ""},
        }
    }
    with pytest.raises(RuntimeError, match="FAULT_METRIC_UNKNOWN_EMPTY"):
        metrics.require_fault_metric_samples(empty)


def test_require_fault_metric_samples_accepts_positive() -> None:
    ok = {
        "metrics": {
            "unknown_recovery": {
                "recovery": {"numerator": 1, "denominator": 2, "rate": 0.5, "note": ""}
            },
            "retry_success": {"numerator": 1, "denominator": 1, "rate": 1.0, "note": ""},
        }
    }
    dens = metrics.require_fault_metric_samples(ok)
    assert dens == {"unknown_recovery": 2, "retry_success": 1}


def test_collect_by_goal_rows_carry_denominators() -> None:
    """按 Goal 明细同样带分母；无样本时 rate 为 None（不得伪造成 0%）。

    Codex P1-3：本测自建确定样本，不依赖共享库残留或其他用例写入顺序。
    """
    import hashlib
    import json
    from uuid import uuid4

    from sqlalchemy import text

    db = _engine_or_skip()
    project_id = uuid4()
    goal_id = uuid4()
    contract = {
        "project_id": str(project_id),
        "objective": "metrics-unit-sample",
        "acceptance_description": "自建样本",
        "success_criteria": [{"id": "C1", "description": "x"}],
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(contract, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    budget = {
        "elapsed_wall_seconds": 0,
        "model_tokens": 0,
        "model_cost_usd": "0",
        "tool_calls": 0,
    }

    with db.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO projects(id, name, repository_ref)
                   VALUES (:id, :name, :repo)"""
            ),
            {
                "id": project_id,
                "name": f"metrics-sample-{project_id.hex[:8]}",
                "repo": "local/metrics-sample",
            },
        )
        conn.execute(
            text(
                """INSERT INTO project_trust_states(project_id, status)
                   VALUES (:id, 'OPEN')
                   ON CONFLICT (project_id) DO NOTHING"""
            ),
            {"id": project_id},
        )
        conn.execute(
            text(
                """INSERT INTO goals(
                     id, project_id, status, state_revision, contract_revision,
                     contract, contract_digest, criterion_verified, criterion_total,
                     budget_usage, write_epoch
                   ) VALUES (
                     :id, :project, 'DRAFT', 1, 1,
                     CAST(:contract AS jsonb), :digest, 0, 1,
                     CAST(:budget AS jsonb), '0'
                   )"""
            ),
            {
                "id": goal_id,
                "project": project_id,
                "contract": json.dumps(contract),
                "digest": digest,
                "budget": json.dumps(budget),
            },
        )

    try:
        # collect 另开连接；须在样本已提交后查询
        report = metrics.collect(db, goal_id=str(goal_id), by_goal_limit=3)
        assert len(report.by_goal) == 1, "自建 Goal 必须出现在 by_goal 明细"
        row = report.by_goal[0]
        assert row["goal_id"] == str(goal_id)
        assert row["status"] == "DRAFT"
        for key in (
            "model_first_attempt_success",
            "retry_success",
            "auditor_blocking",
            "unknown_recovery",
        ):
            ratio = row[key]
            assert ratio["numerator"] <= ratio["denominator"]
            if ratio["denominator"] == 0:
                assert ratio["rate"] is None
    finally:
        with db.begin() as conn:
            conn.execute(text("DELETE FROM goals WHERE id=:id"), {"id": goal_id})
            conn.execute(
                text("DELETE FROM project_trust_states WHERE project_id=:id"),
                {"id": project_id},
            )
            conn.execute(text("DELETE FROM projects WHERE id=:id"), {"id": project_id})


def test_render_does_not_crash_and_shows_scope() -> None:
    """人类可读渲染不抛异常，且写明作用域（避免「不知道看的是哪个窗口」）。"""
    db = _engine_or_skip()
    text = metrics._render(metrics.collect(db, by_goal_limit=2))
    assert "真实业务运行指标" in text
    assert "作用域" in text
    assert "非业务裁决" in text
