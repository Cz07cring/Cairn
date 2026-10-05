"""真实业务运行的运营指标采集（只读）—— 收敛路线的「仪表」而非「裁决」。

## 为什么需要

无人值守系统要跑 10–20 个真实任务、再跑 8h/24h/100h（用户路线 #4/#6），
**必须先能回答四个问题**，否则「跑完了」与「跑好了」无从区分：

1. **模型一次成功率** —— 一次调用就产出可用结果的占比（越低越依赖重试）；
2. **重试成功率** —— 需要重试的任务里最终成功的占比（越低越说明重试无用）；
3. **Auditor 拦截率** —— 审计判 FAIL/INSUFFICIENT 的占比（按 layer 分，语义不同）；
4. **UNKNOWN 恢复率** —— 出现 UNKNOWN 副作用后最终落到终态的占比。

## 三条硬约束

- **只读**：本脚本只跑 `SELECT`。任何写操作都不属于「采集」。
- **必须给分母**：所有比率都同时打印分子/分母，**不打印孤零零的百分比** ——
  估算数字与真实到账数字必须能区分（用户明确要求）。
- **不做 DONE 判定**：本输出是**运营观测**，不是 Kernel 的业务裁决；
  `status='DONE'` 只由 Kernel 在固定 VerificationProfile 与最终屏障上判定。

## 用法

    uv run python scripts/report_business_metrics.py                    # 全库
    uv run python scripts/report_business_metrics.py --hours 24         # 近 24h 创建的 Goal
    uv run python scripts/report_business_metrics.py --goal-id <uuid>   # 单个 Goal
    uv run python scripts/report_business_metrics.py --json             # 供长跑脚本消费

数据库取自 `RING_DATABASE_URL`（测试用 `RING_TEST_DATABASE_URL`）；缺失则**失败关闭**。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, create_engine, text

# 终态口径与 Kernel 保持一致（勿在此另立一套）
_ACTIVITY_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
_INTENT_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
_AUDIT_BLOCKING = ("FAIL", "INSUFFICIENT")


@dataclass
class Ratio:
    """带分母的比率 —— 永不只给百分比。"""

    numerator: int
    denominator: int
    note: str = ""

    @property
    def rate(self) -> float | None:
        if self.denominator <= 0:
            return None
        return self.numerator / self.denominator

    def as_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "note": self.note,
        }

    def render(self) -> str:
        if self.rate is None:
            return f"n/a（分母为 0：{self.note or '无样本'}）"
        return f"{self.numerator}/{self.denominator} = {self.rate * 100:.1f}%"


@dataclass
class Report:
    window: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    by_goal: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "metrics": self.metrics,
            "extras": self.extras,
            "by_goal": self.by_goal,
        }


def _connect() -> Engine:
    url = (
        os.environ.get("RING_DATABASE_URL")
        or os.environ.get("RING_TEST_DATABASE_URL")
        or ""
    ).strip()
    if not url:
        raise RuntimeError(
            "缺少 RING_DATABASE_URL（或测试用 RING_TEST_DATABASE_URL）："
            "采集器拒绝在未知库上运行"
        )
    return create_engine(url)


def _scope_sql(
    *, hours: int | None, goal_id: str | None, project_id: str | None
) -> tuple[str, dict[str, Any]]:
    """构造 goals 的时间/归属过滤片段（各指标共用，保证同一窗口口径）。"""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if hours is not None:
        clauses.append("g.created_at >= now() - make_interval(hours => :hours)")
        params["hours"] = int(hours)
    if goal_id:
        clauses.append("g.id = CAST(:goal_id AS uuid)")
        params["goal_id"] = goal_id
    if project_id:
        clauses.append("g.project_id = CAST(:project_id AS uuid)")
        params["project_id"] = project_id
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _model_first_attempt_success(db: Engine, scope: str, params: dict) -> Ratio:
    """模型一次成功率：每个 (活动, 尝试) 的**首次**模型调用即 SUCCEEDED 的占比。

    口径说明：按 `invocation_seq` 取最小者作为「首次」；同一次尝试内的重试会递增 seq。
    """
    sql = f"""
        WITH scoped AS (
            SELECT g.id AS goal_id FROM goals g{scope}
        ), inv AS (
            SELECT i.activity_id, i.producer_attempt_id, i.invocation_seq, i.status,
                   row_number() OVER (
                       PARTITION BY i.activity_id, i.producer_attempt_id
                       ORDER BY i.invocation_seq, i.created_at
                   ) AS rn
            FROM model_invocations i
            JOIN scoped s ON s.goal_id = i.goal_id
        )
        SELECT
            count(*) FILTER (WHERE rn = 1) AS attempts,
            count(*) FILTER (WHERE rn = 1 AND status = 'SUCCEEDED') AS first_ok,
            count(*) AS calls,
            count(*) FILTER (WHERE status = 'SUCCEEDED') AS calls_ok
        FROM inv
    """
    row = db.connect().execute(text(sql), params).mappings().one()
    calls = int(row["calls"] or 0)
    calls_ok = int(row["calls_ok"] or 0)
    return Ratio(
        numerator=int(row["first_ok"] or 0),
        denominator=int(row["attempts"] or 0),
        note=(
            f"按 (activity,attempt) 计，首次调用成功即算；"
            f"同期模型调用总数 {calls}（成功 {calls_ok}）"
        ),
    )


def _retry_success(db: Engine, scope: str, params: dict) -> Ratio:
    """重试成功率：需要 ≥2 次尝试的活动里，最终 SUCCEEDED 的占比。

    「需要重试」以**实际尝试条数**为准（而非 `retry_count` 字段）——
    避免字段未被写入时把「没重试」误读成「重试成功」。
    """
    sql = f"""
        WITH scoped AS (SELECT g.id AS goal_id FROM goals g{scope}),
        per_act AS (
            SELECT a.id, a.status, count(t.id) AS attempts
            FROM activities a
            JOIN scoped s ON s.goal_id = a.goal_id
            LEFT JOIN activity_attempts t ON t.activity_id = a.id
            WHERE a.kind IN ('PLAN','EXECUTE','INTEGRATE','AUDIT','FINALIZE')
            GROUP BY a.id, a.status
        )
        SELECT
            count(*) FILTER (WHERE attempts >= 2) AS retried,
            count(*) FILTER (WHERE attempts >= 2 AND status = 'SUCCEEDED') AS retried_ok,
            count(*) AS activities,
            count(*) FILTER (WHERE attempts <= 1 AND status = 'SUCCEEDED') AS first_try_ok
        FROM per_act
    """
    row = db.connect().execute(text(sql), params).mappings().one()
    return Ratio(
        numerator=int(row["retried_ok"] or 0),
        denominator=int(row["retried"] or 0),
        note=(
            f"分母=尝试数≥2 的活动；"
            f"同期活动总数 {int(row['activities'] or 0)}"
            f"（首次即成功 {int(row['first_try_ok'] or 0)}）"
        ),
    )


def _auditor_blocking(db: Engine, scope: str, params: dict) -> dict[str, Any]:
    """Auditor 拦截率：verdict 为 FAIL/INSUFFICIENT 的占比（总率 + 按 layer 分层）。"""
    sql = f"""
        WITH scoped AS (SELECT g.id AS goal_id FROM goals g{scope})
        SELECT au.layer, au.verdict, count(*) AS n
        FROM audits au
        JOIN scoped s ON s.goal_id = au.goal_id
        GROUP BY au.layer, au.verdict
    """
    rows = db.connect().execute(text(sql), params).mappings().all()
    by_layer: dict[str, dict[str, int]] = {}
    total = 0
    blocking = 0
    for row in rows:
        layer = str(row["layer"])
        verdict = str(row["verdict"])
        n = int(row["n"])
        bucket = by_layer.setdefault(layer, {"total": 0, "blocking": 0, "PASS": 0})
        bucket["total"] += n
        if verdict in _AUDIT_BLOCKING:
            bucket["blocking"] += n
        if verdict == "PASS":
            bucket["PASS"] += n
        total += n
        if verdict in _AUDIT_BLOCKING:
            blocking += n
    layered = {
        layer: Ratio(
            numerator=v["blocking"], denominator=v["total"], note=f"PASS {v['PASS']}"
        ).as_dict()
        for layer, v in sorted(by_layer.items())
    }
    return {
        "overall": Ratio(
            numerator=blocking, denominator=total, note="FAIL + INSUFFICIENT"
        ).as_dict(),
        "by_layer": layered,
    }


def _unknown_recovery(db: Engine, scope: str, params: dict) -> dict[str, Any]:
    """UNKNOWN 恢复率：出现 UNKNOWN 副作用后，最终落到终态的占比。

    判定「曾出现 UNKNOWN」的三种证据（任一成立即计入分母）：
      · effect_intents 当前 status = 'UNKNOWN'
      · 回执 observed_outcome = 'UNKNOWN'
      · 回执 disposition = 'PENDING_RECONCILIATION'（UNKNOWN 的对账态）
    分子 = 该 effect 的**当前** status ∈ 终态（SUCCEEDED/FAILED/CANCELLED）。
    """
    sql = f"""
        WITH scoped AS (SELECT g.id AS goal_id FROM goals g{scope}),
        unknown_effects AS (
            SELECT e.id, e.status
            FROM effect_intents e
            JOIN scoped s ON s.goal_id = e.goal_id
            WHERE e.status = 'UNKNOWN'
               OR EXISTS (
                   SELECT 1 FROM effect_receipts r
                   WHERE r.effect_id = e.id
                     AND (r.observed_outcome = 'UNKNOWN'
                          OR r.disposition = 'PENDING_RECONCILIATION')
               )
        )
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE status IN ('SUCCEEDED','FAILED','CANCELLED')) AS resolved,
            count(*) FILTER (WHERE status = 'UNKNOWN') AS still_unknown,
            count(*) FILTER (WHERE status = 'DISPATCHED') AS dispatched,
            count(*) FILTER (WHERE status = 'PREPARED') AS prepared
        FROM unknown_effects
    """
    row = db.connect().execute(text(sql), params).mappings().one()
    total = int(row["total"] or 0)
    resolved = int(row["resolved"] or 0)
    outstanding = total - resolved
    return {
        "recovery": Ratio(
            numerator=resolved, denominator=total, note="已落到终态 / 曾出现 UNKNOWN"
        ).as_dict(),
        "outstanding": {
            "total": outstanding,
            "still_unknown": int(row["still_unknown"] or 0),
            "dispatched": int(row["dispatched"] or 0),
            "prepared": int(row["prepared"] or 0),
            "note": "未落终态者；UNKNOWN 未对账**不得**开启后续工具准入或最终屏障",
        },
    }


def _extras(db: Engine, scope: str, params: dict) -> dict[str, Any]:
    """上下文：Goal 终态分布、屏障状态、租约过期次数（解释上面四个比率的成因）。"""
    goals = db.connect().execute(
        text(
            f"SELECT g.status, count(*) AS n FROM goals g{scope} GROUP BY g.status"
        ),
        params,
    ).mappings().all()
    barrier_params = dict(params)
    barriers = db.connect().execute(
        text(
            f"""
            WITH scoped AS (SELECT g.id AS goal_id FROM goals g{scope})
            SELECT b.status, count(*) AS n
            FROM finalization_barriers b
            JOIN scoped s ON s.goal_id = b.goal_id
            GROUP BY b.status
            """
        ),
        barrier_params,
    ).mappings().all()
    lease = db.connect().execute(
        text(
            f"""
            WITH scoped AS (SELECT g.id AS goal_id FROM goals g{scope})
            SELECT count(*) AS n
            FROM activity_attempts t
            JOIN activities a ON a.id = t.activity_id
            JOIN scoped s ON s.goal_id = a.goal_id
            WHERE t.status = 'EXPIRED'
            """
        ),
        params,
    ).scalar_one()
    return {
        "goals_by_status": {str(r["status"]): int(r["n"]) for r in goals},
        "finalization_barriers_by_status": {
            str(r["status"]): int(r["n"]) for r in barriers
        },
        "expired_attempts": int(lease or 0),
        "pollution_warning": _pollution_warning(
            {str(r["status"]): int(r["n"]) for r in goals}
        ),
        "caveat": (
            "运营观测，非业务裁决；DONE 只由 Kernel 在固定 VerificationProfile "
            "与最终屏障上判定"
        ),
    }


def _pollution_warning(by_status: dict[str, int]) -> str | None:
    """诚实标注：共享开发库混有大量测试运行，全库比率**不可直接当验收数字**。

    判据：窗口内终态（DONE/CANCELLED/FAILED）占比过低 ⇒ 大量运行没走完，
    多半是测试/中断留下的。此时应改用 `--goal-id` 或更小 `--hours` 窗口。
    """
    total = sum(by_status.values())
    if total < 50:
        return None
    terminal = sum(by_status.get(s, 0) for s in ("DONE", "CANCELLED", "FAILED"))
    if terminal * 2 >= total:
        return None
    return (
        f"窗口内 {total} 个 Goal 中仅 {terminal} 个处于终态"
        f"（DONE/CANCELLED/FAILED）—— 本库很可能混有大量测试或中断运行，"
        f"**整体比率不可作为验收依据**；请用 --goal-id 或更小 --hours 窗口取样本"
    )


def _by_goal(db: Engine, scope: str, params: dict, limit: int) -> list[dict[str, Any]]:
    """按 Goal 逐条列出四项指标的关键量 —— 长跑（10–20 任务）评估用。"""
    sql = f"""
        WITH scoped AS (
            SELECT g.id, g.status, g.criterion_verified, g.criterion_total, g.created_at
            FROM goals g{scope}
            ORDER BY g.created_at DESC
            LIMIT :limit
        ),
        inv AS (
            SELECT i.goal_id, i.activity_id, i.producer_attempt_id, i.invocation_seq, i.status,
                   row_number() OVER (
                       PARTITION BY i.goal_id, i.activity_id, i.producer_attempt_id
                       ORDER BY i.invocation_seq, i.created_at
                   ) AS rn
            FROM model_invocations i
            JOIN scoped s ON s.id = i.goal_id
        ),
        inv_agg AS (
            SELECT goal_id,
                   count(*) FILTER (WHERE rn = 1) AS attempts,
                   count(*) FILTER (WHERE rn = 1 AND status = 'SUCCEEDED') AS first_ok
            FROM inv GROUP BY goal_id
        ),
        act AS (
            SELECT a.goal_id, a.id, a.status, count(t.id) AS attempts
            FROM activities a
            JOIN scoped s ON s.id = a.goal_id
            LEFT JOIN activity_attempts t ON t.activity_id = a.id
            GROUP BY a.goal_id, a.id, a.status
        ),
        act_agg AS (
            SELECT goal_id,
                   count(*) FILTER (WHERE attempts >= 2) AS retried,
                   count(*) FILTER (WHERE attempts >= 2 AND status = 'SUCCEEDED') AS retried_ok
            FROM act GROUP BY goal_id
        ),
        au AS (
            SELECT au.goal_id,
                   count(*) AS total,
                   count(*) FILTER (WHERE au.verdict IN ('FAIL','INSUFFICIENT')) AS blocking
            FROM audits au JOIN scoped s ON s.id = au.goal_id
            GROUP BY au.goal_id
        ),
        unk AS (
            SELECT e.goal_id, e.id, e.status
            FROM effect_intents e JOIN scoped s ON s.id = e.goal_id
            WHERE e.status = 'UNKNOWN'
               OR EXISTS (
                   SELECT 1 FROM effect_receipts r
                   WHERE r.effect_id = e.id
                     AND (r.observed_outcome = 'UNKNOWN'
                          OR r.disposition = 'PENDING_RECONCILIATION')
               )
        ),
        unk_agg AS (
            SELECT goal_id, count(*) AS total,
                   count(*) FILTER (WHERE status IN ('SUCCEEDED','FAILED','CANCELLED')) AS resolved
            FROM unk GROUP BY goal_id
        )
        SELECT s.id, s.status, s.criterion_verified, s.criterion_total, s.created_at,
               coalesce(i.attempts,0) AS model_attempts, coalesce(i.first_ok,0) AS model_first_ok,
               coalesce(a.retried,0) AS retried, coalesce(a.retried_ok,0) AS retried_ok,
               coalesce(au.total,0) AS audit_total, coalesce(au.blocking,0) AS audit_blocking,
               coalesce(u.total,0) AS unknown_total, coalesce(u.resolved,0) AS unknown_resolved
        FROM scoped s
        LEFT JOIN inv_agg i ON i.goal_id = s.id
        LEFT JOIN act_agg a ON a.goal_id = s.id
        LEFT JOIN au ON au.goal_id = s.id
        LEFT JOIN unk_agg u ON u.goal_id = s.id
        ORDER BY s.created_at DESC
    """
    rows = db.connect().execute(text(sql), {**params, "limit": int(limit)}).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "goal_id": str(r["id"]),
                "status": str(r["status"]),
                "criteria": (
                    f"{r['criterion_verified']}/{r['criterion_total']}"
                    if r["criterion_total"] is not None
                    else "n/a"
                ),
                "created_at": str(r["created_at"]),
                "model_first_attempt_success": Ratio(
                    int(r["model_first_ok"]), int(r["model_attempts"])
                ).as_dict(),
                "retry_success": Ratio(
                    int(r["retried_ok"]), int(r["retried"])
                ).as_dict(),
                "auditor_blocking": Ratio(
                    int(r["audit_blocking"]), int(r["audit_total"])
                ).as_dict(),
                "unknown_recovery": Ratio(
                    int(r["unknown_resolved"]), int(r["unknown_total"])
                ).as_dict(),
            }
        )
    return out


def collect(
    db: Engine,
    *,
    hours: int | None = None,
    goal_id: str | None = None,
    project_id: str | None = None,
    by_goal_limit: int | None = None,
) -> Report:
    scope, params = _scope_sql(hours=hours, goal_id=goal_id, project_id=project_id)
    report = Report(
        window={
            "scope_sql": scope.strip() or "（全库）",
            "hours": hours,
            "goal_id": goal_id,
            "project_id": project_id,
            "definition": "窗口按 goals.created_at 计；四项指标的样本均取该窗口内的 Goal",
        }
    )
    report.metrics = {
        "model_first_attempt_success": _model_first_attempt_success(db, scope, params).as_dict(),
        "retry_success": _retry_success(db, scope, params).as_dict(),
        "auditor_blocking": _auditor_blocking(db, scope, params),
        "unknown_recovery": _unknown_recovery(db, scope, params),
    }
    report.extras = _extras(db, scope, params)
    if by_goal_limit:
        report.by_goal = _by_goal(db, scope, params, by_goal_limit)
    return report


def _ratio_from_dict(data: dict[str, Any]) -> Ratio:
    """从 `Ratio.as_dict()` 还原（`as_dict` 含派生字段 `rate`，不能直接 `Ratio(**d)`）。"""
    return Ratio(
        numerator=int(data["numerator"]),
        denominator=int(data["denominator"]),
        note=str(data.get("note") or ""),
    )


def require_model_invocation_ledger(
    report_dict: dict[str, Any],
    *,
    min_denominator: int = 1,
) -> int:
    """Codex P0-1 闸门：返回 model_first_attempt_success.denominator。

    live 成功后分母须 ≥ min_denominator；否则抛 RuntimeError（失败关闭）。
    不宣称 Goal DONE。
    """
    metrics = report_dict.get("metrics") or {}
    ratio = metrics.get("model_first_attempt_success") or {}
    den = int(ratio.get("denominator") or 0)
    if den < min_denominator:
        raise RuntimeError(
            "MODEL_INVOCATION_LEDGER_EMPTY: "
            f"model_first_attempt_success.denominator={den} "
            f"（需要 ≥{min_denominator}；Codex P0-1：官方 Loop 须登记 ModelInvocation）"
        )
    return den


def require_fault_metric_samples(
    report_dict: dict[str, Any],
    *,
    min_unknown_denominator: int = 1,
    min_retry_denominator: int = 1,
) -> dict[str, int]:
    """Codex P1-4 闸门：UNKNOWN 恢复 / 重试样本分母须 ≥ 阈值。

    顺利路径不能证明采集器看见故障注入；长跑可设 RING_SOAK_REQUIRE_FAULT_SAMPLES=1。
    不宣称 Goal DONE。
    """
    metrics = report_dict.get("metrics") or {}
    unknown = (metrics.get("unknown_recovery") or {}).get("recovery") or {}
    retry = metrics.get("retry_success") or {}
    unknown_den = int(unknown.get("denominator") or 0)
    retry_den = int(retry.get("denominator") or 0)
    if unknown_den < min_unknown_denominator:
        raise RuntimeError(
            "FAULT_METRIC_UNKNOWN_EMPTY: "
            f"unknown_recovery.denominator={unknown_den} "
            f"（需要 ≥{min_unknown_denominator}；Codex P1-4）"
        )
    if retry_den < min_retry_denominator:
        raise RuntimeError(
            "FAULT_METRIC_RETRY_EMPTY: "
            f"retry_success.denominator={retry_den} "
            f"（需要 ≥{min_retry_denominator}；Codex P1-4）"
        )
    return {"unknown_recovery": unknown_den, "retry_success": retry_den}


def _render(report: Report) -> str:
    data = report.as_dict()
    m = data["metrics"]
    lines: list[str] = []
    lines.append("== 真实业务运行指标（只读采集）==")
    lines.append(f"窗口：{data['window']['definition']}")
    lines.append(f"      作用域：{data['window']['scope_sql']}")

    def show(title: str, ratio: dict[str, Any], extra: str = "") -> None:
        r = Ratio(ratio["numerator"], ratio["denominator"], ratio.get("note", ""))
        lines.append(f"  {title}: {r.render()}")
        if extra:
            lines.append(f"      {extra}")

    lines.append("")
    lines.append("【1】模型一次成功率（一次调用即成功 / 有过模型调用的尝试）")
    show("比率", m["model_first_attempt_success"], m["model_first_attempt_success"]["note"])

    lines.append("")
    lines.append("【2】重试成功率（尝试≥2 且最终成功 / 尝试≥2 的活动）")
    show("比率", m["retry_success"], m["retry_success"]["note"])

    lines.append("")
    lines.append("【3】Auditor 拦截率（FAIL+INSUFFICIENT / 全部审计）")
    show("总体", m["auditor_blocking"]["overall"], m["auditor_blocking"]["overall"]["note"])
    for layer, ratio in m["auditor_blocking"]["by_layer"].items():
        r = Ratio(ratio["numerator"], ratio["denominator"], ratio.get("note", ""))
        lines.append(f"    · {layer}: {r.render()}（{r.note}）")

    lines.append("")
    lines.append("【4】UNKNOWN 恢复率（已落终态 / 曾出现 UNKNOWN 的 effect）")
    show("比率", m["unknown_recovery"]["recovery"], m["unknown_recovery"]["recovery"]["note"])
    out = m["unknown_recovery"]["outstanding"]
    lines.append(
        f"      未落终态 {out['total']}（UNKNOWN {out['still_unknown']} / "
        f"DISPATCHED {out['dispatched']} / PREPARED {out['prepared']}）"
    )

    ex = data["extras"]
    lines.append("")
    lines.append("== 上下文 ==")
    lines.append(f"  Goal 状态分布: {ex['goals_by_status']}")
    lines.append(f"  最终屏障状态: {ex['finalization_barriers_by_status']}")
    lines.append(f"  租约过期尝试数: {ex['expired_attempts']}")
    warn = ex.get("pollution_warning")
    if warn:
        lines.append("")
        lines.append(f"  ⚠ 数据可信度：{warn}")

    if data.get("by_goal"):
        lines.append("")
        lines.append(f"== 按 Goal 明细（最近 {len(data['by_goal'])} 条）==")
        for row in data["by_goal"]:
            m1 = _ratio_from_dict(row["model_first_attempt_success"])
            m2 = _ratio_from_dict(row["retry_success"])
            m3 = _ratio_from_dict(row["auditor_blocking"])
            m4 = _ratio_from_dict(row["unknown_recovery"])
            lines.append(
                f"  {row['goal_id'][:8]} {row['status']:<10} 验收 {row['criteria']:<10} "
                f"模型一次 {m1.render()} | 重试 {m2.render()} | "
                f"审计拦截 {m3.render()} | UNKNOWN 恢复 {m4.render()}"
            )

    lines.append("")
    lines.append(f"  ⚠ {ex['caveat']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实业务运行指标采集（只读）")
    parser.add_argument("--hours", type=int, default=None, help="只看近 N 小时创建的 Goal")
    parser.add_argument("--goal-id", default=None, help="只看单个 Goal")
    parser.add_argument("--project-id", default=None, help="只看单个项目")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供长跑脚本消费）")
    parser.add_argument(
        "--by-goal",
        type=int,
        default=None,
        metavar="N",
        help="额外输出最近 N 个 Goal 的逐条明细（长跑 10–20 任务评估用）",
    )
    parser.add_argument(
        "--require-model-invocations",
        action="store_true",
        help="Codex P0-1：model_first_attempt_success.denominator 须 ≥1，否则退出码 3",
    )
    parser.add_argument(
        "--require-fault-samples",
        action="store_true",
        help="Codex P1-4：unknown_recovery 与 retry_success 分母须 ≥1，否则退出码 3",
    )
    args = parser.parse_args(argv)

    try:
        db = _connect()
    except RuntimeError as exc:
        print(f"失败关闭：{exc}", file=sys.stderr)
        return 2

    report = collect(
        db,
        hours=args.hours,
        goal_id=args.goal_id,
        project_id=args.project_id,
        by_goal_limit=args.by_goal,
    )
    data = report.as_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_render(report))
    if args.require_model_invocations:
        try:
            den = require_model_invocation_ledger(data)
            print(
                f"ModelInvocation 账本闸门：denominator={den}（Codex P0-1 OK）",
                file=sys.stderr,
            )
        except RuntimeError as exc:
            print(f"失败关闭：{exc}", file=sys.stderr)
            return 3
    if args.require_fault_samples:
        try:
            dens = require_fault_metric_samples(data)
            print(
                "故障注入样本闸门："
                f"unknown={dens['unknown_recovery']} retry={dens['retry_success']}"
                "（Codex P1-4 OK）",
                file=sys.stderr,
            )
        except RuntimeError as exc:
            print(f"失败关闭：{exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
