"""周期 GoalReview：计时归工作流、去重与快照归 Kernel（doc/v0.6/01 §7）。

**为什么需要**：运行中的 Goal 若长期无人复盘，停滞只会在最终屏障才暴露 —— 那时
代价已经付出（预算耗尽、资源被隔离）。设计（`doc/v0.6/01-开发文档.md:59`）要求
「运行中 GoalReview 由 Workflow 的**持久计时器/进度事件**触发，Kernel 用**触发键去重**
创建 `AUDIT(target=GOAL_REVIEW)`」，且「复盘频率、停滞阈值、最大重规划次数必须配置
并纳入预算，**不能无限复盘**」。

本文件钉住七件事，全部是**失败关闭**语义：

1. 缺省（未配间隔/上限）⇒ **零触发**（旧历史与既有部署行为逐字不变）；
2. 参数非法（间隔低于下限）⇒ **零触发**（不因误配而高频复盘）；
3. 到点触发且**恰好**受 `max_goal_reviews` 约束（不无限复盘）；
4. 活动失败**不推进计数**（不把「失败」当成「复盘过」），并留下可见原因；
5. **外发 `review_seq` 必须 ≥1**（Kernel 对 0 抛 `GOAL_REVIEW_TRIGGER_INVALID`）；
6. **设计内拒绝**（未停滞 / 间隔过短）记 `rejected:<code>`，与接线故障分开；
7. **预算耗尽 / 额度用尽** ⇒ 停止再请求（不每 tick 撞同一面墙）。

**刻意不覆盖**：Kernel 侧的去重与快照钉扎（属 `control_kernel` 的验收面，由
`tests/integration/test_goal_review*.py` 覆盖）；本文件只钉编排侧的计时与上限行为。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from orchestration.temporal_workflows import (
    _DEFAULT_MAX_GOAL_REVIEWS,
    _GOAL_REVIEW_MIN_INTERVAL_SECONDS,
    GoalWorkflow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_CONTROL_QUEUE = "ring-control"
_RUNNER_QUEUE = "ring-runner"
_FIXED_ACTIVITY_ID = "act-review-1"

# 桩调用记录（模块级，用例间显式清空，避免跨用例污染）
_review_calls: list[dict] = []


@activity.defn(name="ensure_goal_delivery")
async def _stub_ensure(command_id: str, goal_id: str) -> dict[str, str | None]:
    return {
        "command_id": command_id,
        "workflow_id": f"goal-{goal_id}",
        "run_id": "run-review",
        "delivery_status": "ACKNOWLEDGED",
    }


@activity.defn(name="record_recovery_abandonment")
async def _stub_abandon(
    goal_id: str, reason: str, generation: int, prior_run_id: str | None
) -> dict:
    return {
        "abandonment_id": "a1",
        "goal_id": goal_id,
        "reason": reason,
        "generation": generation,
        "prior_run_id": prior_run_id,
        "marks_goal_done": False,
    }


@activity.defn(name="list_runtime_actions")
async def _stub_list(goal_id: str, owner_epoch: str) -> dict:
    return {
        "actions": [
            {
                "activity_id": _FIXED_ACTIVITY_ID,
                "action_id": _FIXED_ACTIVITY_ID,
                "goal_id": goal_id,
                "project_id": str(uuid4()),
                "owner_epoch": owner_epoch,
                "kind": "PLAN",
            }
        ],
        "wait_hint": None,
    }


@activity.defn(name="admit_plan_action")
async def _stub_admit(activity_id: str, idempotency_key: str) -> dict[str, str | None]:
    return {"activity_id": activity_id, "attempt_id": "att-1", "fencing_epoch": "1"}


@activity.defn(name="RunActivation")
async def _stub_run(payload: dict) -> dict:
    return {
        "status": "PENDING_ENV",
        "reason": "stub",
        "kind": "PLAN",
        "pending_harness": True,
    }


@activity.defn(name="observe_activity_status")
async def _stub_observe_running(activity_id: str) -> dict[str, str]:
    """活动一直「在跑」⇒ 观察循环跑满所有 tick（正好用来观察周期触发）。"""
    return {"activity_id": activity_id, "status": "RUNNING", "kind": "PLAN"}


@activity.defn(name="request_goal_review")
async def _stub_review(goal_id: str, trigger_key: str, review_seq: int) -> dict:
    _review_calls.append(
        {"goal_id": goal_id, "trigger_key": trigger_key, "review_seq": review_seq}
    )
    return {
        "activity_id": f"audit-{review_seq}",
        "review_snapshot_digest": f"digest-{review_seq}",
        "created": True,
        "deduped": False,
        "rejected": False,
        "reviews_remaining": 99,
        "max_reviews": 99,
        "min_interval_seconds": 30,
        "stagnation_seconds": 360,
        "trigger_key": trigger_key,
        "review_seq": review_seq,
        "marks_goal_done": False,
    }


@activity.defn(name="request_goal_review")
async def _stub_review_raises(goal_id: str, trigger_key: str, review_seq: int) -> dict:
    """模拟 Kernel 拒绝（如 Goal 已终态）—— 用于钉「失败不推进计数」。"""
    _review_calls.append(
        {"goal_id": goal_id, "trigger_key": trigger_key, "review_seq": review_seq}
    )
    raise RuntimeError("KERNEL_REJECTED")


@activity.defn(name="request_goal_review")
async def _stub_review_rejected(goal_id: str, trigger_key: str, review_seq: int) -> dict:
    """模拟 Kernel **设计内拒绝**（未停滞 / 间隔过短 / 预算耗尽）—— 非故障。"""
    _review_calls.append(
        {"goal_id": goal_id, "trigger_key": trigger_key, "review_seq": review_seq}
    )
    return {
        "activity_id": "",
        "review_snapshot_digest": "",
        "created": False,
        "deduped": False,
        "rejected": True,
        "reason_code": "GOAL_REVIEW_NOT_STAGNANT",
        "reason": "工程尚未停滞（需 ≥360s 无进展，已空闲 0s）",
        "trigger_key": trigger_key,
        "review_seq": review_seq,
        "marks_goal_done": False,
    }


@activity.defn(name="request_goal_review")
async def _stub_review_budget_exhausted(
    goal_id: str, trigger_key: str, review_seq: int
) -> dict:
    """模拟复盘预算耗尽：Kernel 拒绝，工作流应**停止再请求**。"""
    _review_calls.append(
        {"goal_id": goal_id, "trigger_key": trigger_key, "review_seq": review_seq}
    )
    return {
        "activity_id": "",
        "review_snapshot_digest": "",
        "created": False,
        "deduped": False,
        "rejected": True,
        "reason_code": "GOAL_REVIEW_BUDGET_EXHAUSTED",
        "reason": "GOAL_REVIEW 已达上限",
        "trigger_key": trigger_key,
        "review_seq": review_seq,
        "marks_goal_done": False,
    }


@activity.defn(name="request_goal_review")
async def _stub_review_last_slot(goal_id: str, trigger_key: str, review_seq: int) -> dict:
    """成功但额度用尽（reviews_remaining=0）⇒ 工作流应停止再请求。"""
    _review_calls.append(
        {"goal_id": goal_id, "trigger_key": trigger_key, "review_seq": review_seq}
    )
    return {
        "activity_id": f"audit-{review_seq}",
        "review_snapshot_digest": f"digest-{review_seq}",
        "created": True,
        "deduped": False,
        "rejected": False,
        "reviews_remaining": 0,
        "max_reviews": 1,
        "min_interval_seconds": 30,
        "stagnation_seconds": 360,
        "trigger_key": trigger_key,
        "review_seq": review_seq,
        "marks_goal_done": False,
    }


async def _run_workflow(
    payload: dict,
    *,
    failing: bool = False,
    stub: object | None = None,
) -> dict:
    if stub is not None:
        review = stub
    else:
        review = _stub_review_raises if failing else _stub_review
    async with await WorkflowEnvironment.start_time_skipping() as env:
        assert env.client is not None
        async with (
            Worker(
                env.client,
                task_queue=_CONTROL_QUEUE,
                workflows=[GoalWorkflow],
                activities=[
                    _stub_ensure,
                    _stub_abandon,
                    _stub_list,
                    _stub_admit,
                    _stub_observe_running,
                    review,
                ],
            ),
            Worker(env.client, task_queue=_RUNNER_QUEUE, activities=[_stub_run]),
        ):
            return await asyncio.wait_for(
                env.client.execute_workflow(
                    GoalWorkflow.run,
                    payload,
                    id=f"goal-review-{uuid4()}",
                    task_queue=_CONTROL_QUEUE,
                ),
                timeout=60,
            )


def _payload(**extra: object) -> dict:
    """观察 12 轮：退避 1+2+4+8+16+32+60×5 ≈ 363s，足够越过多个 30s 间隔。"""
    base = {
        "command_id": f"cmd-review-{uuid4()}",
        "goal_id": str(uuid4()),
        "owner_epoch": "1",
        "generation": 0,
        "observe_max_ticks": 12,
    }
    base.update(extra)
    return base


def test_default_off_triggers_no_review() -> None:
    """**失败关闭**：未配间隔与上限 ⇒ 一次都不触发（既有行为逐字不变）。"""
    _review_calls.clear()
    result = asyncio.run(_run_workflow(_payload()))
    assert _review_calls == []
    assert result["goal_reviews_requested"] == 0
    assert result["goal_reviews_created"] == []
    assert result.get("marks_goal_done") is False


def test_interval_below_floor_triggers_no_review() -> None:
    """参数非法（间隔低于下限）不得高频复盘 —— 误配即静默关闭，而非退化成忙轮询。"""
    _review_calls.clear()
    result = asyncio.run(
        _run_workflow(
            _payload(
                goal_review_interval_seconds=_GOAL_REVIEW_MIN_INTERVAL_SECONDS - 1,
                max_goal_reviews=5,
            )
        )
    )
    assert _review_calls == []
    assert result["goal_reviews_requested"] == 0


def test_periodic_review_fires_and_respects_max() -> None:
    """**核心**：到点触发，且**恰好**受上限约束（不无限复盘）。"""
    _review_calls.clear()
    max_reviews = 2
    payload = _payload(
        goal_review_interval_seconds=_GOAL_REVIEW_MIN_INTERVAL_SECONDS,
        max_goal_reviews=max_reviews,
    )
    result = asyncio.run(_run_workflow(payload))

    assert len(_review_calls) == max_reviews
    # **契约**：Kernel 要求 review_seq ≥ 1（0 判 GOAL_REVIEW_TRIGGER_INVALID），
    # 故外发必须是 1,2,…（内部 0 基计数 +1）。曾经发 0 让整条通路不可达。
    assert [c["review_seq"] for c in _review_calls] == [1, 2]
    # 幂等键与所发 seq 同源（Kernel 依 key 去重）
    assert [c["trigger_key"] for c in _review_calls] == [
        f"{payload['goal_id']}:1",
        f"{payload['goal_id']}:2",
    ]
    assert result["goal_reviews_requested"] == max_reviews
    assert [r["seq"] for r in result["goal_reviews_created"]] == [1, 2]
    # 复盘绝不等于完成
    assert result.get("marks_goal_done") is False


def test_review_activity_failure_does_not_advance_counter() -> None:
    """活动失败**不算**「复盘过」：计数不推进，原因可见（失败关闭）。"""
    _review_calls.clear()
    result = asyncio.run(
        _run_workflow(
            _payload(
                goal_review_interval_seconds=_GOAL_REVIEW_MIN_INTERVAL_SECONDS,
                max_goal_reviews=1,
            ),
            failing=True,
        )
    )
    # 尝试过（可能多次重试到各 tick），但**一次都没被记为成功**
    assert _review_calls, "至少应尝试一次请求"
    assert result["goal_reviews_requested"] == 0
    assert result["goal_reviews_created"] == []
    assert result["goal_review_notes"], "失败原因必须可见，不得静默"
    assert all(
        note.startswith("request_failed:") for note in result["goal_review_notes"]
    )


def test_defaults_are_documented_constants() -> None:
    """常量语义固定：默认上限为正、间隔下限为正（改动须有意为之）。"""
    assert _DEFAULT_MAX_GOAL_REVIEWS >= 1
    assert _GOAL_REVIEW_MIN_INTERVAL_SECONDS >= 1

def test_kernel_rejection_is_reported_not_retried_as_failure() -> None:
    """设计内拒绝（未停滞）应记为 `rejected:<code>`，**不**混进 request_failed。

    为什么重要：Kernel 只在 Goal **真正停滞**时才允许复盘（Critic 只看卡住的）。
    工程正常推进时被拒属预期，若记成「活动失败」会与接线故障混淆，
    排查时把正常运转看成故障。
    """
    _review_calls.clear()
    result = asyncio.run(
        _run_workflow(
            _payload(
                goal_review_interval_seconds=_GOAL_REVIEW_MIN_INTERVAL_SECONDS,
                max_goal_reviews=1,
            ),
            stub=_stub_review_rejected,
        )
    )
    assert result["goal_reviews_requested"] == 0, "被拒不等于复盘过"
    assert result["goal_reviews_created"] == []
    notes = result["goal_review_notes"]
    assert notes, "拒绝原因必须可见"
    assert all(n.startswith("rejected:") for n in notes), notes
    assert any("GOAL_REVIEW_NOT_STAGNANT" in n for n in notes), notes
    # 绝不因复盘（无论成功或拒绝）写 DONE
    assert result.get("marks_goal_done") is False


def test_budget_exhausted_stops_requesting() -> None:
    """预算耗尽后工作流**停止**再请求 —— 否则每 tick 撞同一面墙刷满日志。"""
    _review_calls.clear()
    result = asyncio.run(
        _run_workflow(
            _payload(
                goal_review_interval_seconds=_GOAL_REVIEW_MIN_INTERVAL_SECONDS,
                max_goal_reviews=3,
            ),
            stub=_stub_review_budget_exhausted,
        )
    )
    assert len(_review_calls) == 1, f"应只请求一次即停，实为 {len(_review_calls)}"
    assert any("GOAL_REVIEW_BUDGET_EXHAUSTED" in n for n in result["goal_review_notes"])


def test_last_slot_success_stops_requesting() -> None:
    """成功但 reviews_remaining=0 ⇒ 同样停止（避免无谓的拒绝往返）。"""
    _review_calls.clear()
    result = asyncio.run(
        _run_workflow(
            _payload(
                goal_review_interval_seconds=_GOAL_REVIEW_MIN_INTERVAL_SECONDS,
                max_goal_reviews=3,
            ),
            stub=_stub_review_last_slot,
        )
    )
    assert len(_review_calls) == 1, f"应只请求一次即停，实为 {len(_review_calls)}"
    assert result["goal_reviews_requested"] == 1


def test_activity_unpack_matches_kernel_return_arity() -> None:
    """**回归门**：活动解包的元素数必须等于 Kernel 声明的返回元组长度。

    历史事故：Kernel 返回 7 元组而活动只解包 3 个 ⇒ 活动**必然**抛
    ValueError ⇒ 周期复盘整条通路不可达（而单测全绿，因为桩替代了活动）。
    这条门用**静态对照**两侧契约，不依赖 DB 或 Temporal。
    """
    import ast
    import typing

    root = Path(__file__).resolve().parents[2]

    # Kernel 侧：解析返回注解 tuple[...] 的元素个数（注解在 PEP 563 下是字符串，
    # 故先尝试运行期解析，失败则退回 AST 文本解析）
    audits_src = (
        root / "packages/control_kernel/src/control_kernel/storage/audits.py"
    ).read_text(encoding="utf-8")
    kernel_arity = None
    try:
        sys.path.insert(0, str(root / "packages/control_kernel/src"))
        from control_kernel.storage.audits import (  # type: ignore[import-not-found]
            ensure_goal_review_activity,
        )

        hints = typing.get_type_hints(ensure_goal_review_activity)
        args = typing.get_args(hints.get("return", object()))
        if args:
            kernel_arity = len(args)
    except Exception:  # noqa: BLE001 - 环境缺依赖时退回静态解析
        kernel_arity = None
    if kernel_arity is None:
        kernel_arity = _tuple_len_from_source(audits_src, "def ensure_goal_review_activity")
    assert kernel_arity == 7, f"Kernel 返回元组长度变化：{kernel_arity}"

    # 编排侧：活动里对该函数的解包目标个数
    ka_src = (
        root / "packages/orchestration/src/orchestration/kernel_activities.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(ka_src)
    mine = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "ensure_goal_review_activity":
                target = node.targets[0]
                mine = len(target.elts) if isinstance(target, ast.Tuple) else 1
    assert mine == kernel_arity, (
        f"解包数量 {mine} ≠ Kernel 返回 {kernel_arity} —— "
        "活动会抛 ValueError 且整条通路不可达"
    )


def test_workflow_passes_one_based_review_seq() -> None:
    """**回归门**：外发 review_seq 必须 ≥1（Kernel 对 0 抛 GOAL_REVIEW_TRIGGER_INVALID）。"""
    src = (
        Path(__file__).resolve().parents[2]
        / "packages/orchestration/src/orchestration/temporal_workflows.py"
    ).read_text(encoding="utf-8")
    # 断言「有 +1」而非字面整行（代码可能被格式化折行）
    assert "goal_reviews_requested + 1" in src, (
        "周期复盘必须外发一基 seq；改回 0 基会让 Kernel 拒绝每一次请求"
    )
    # 该修复变更命令参数 ⇒ 必须走 patch gating，旧历史逐字重放
    assert "_GOAL_REVIEW_SEQ_BASE_PATCH" in src, "seq 基数修复必须登记 patch 标记"
    assert "goal_review_seq_base_patched" in src, "patch 必须真正参与分支选择"


def _tuple_len_from_source(src: str, needle: str) -> int:
    """从源码里取 `def X(...) -> tuple[A, B, ...]:` 的元素个数（静态兜底）。"""
    import re

    idx = src.index(needle)
    tail = src[idx : idx + 2000]
    m = re.search(r"->\s*tuple\[([^\]]*)\]", tail)
    assert m, "未找到返回注解 tuple[...]"
    inner = m.group(1)
    depth = 0
    count = 1
    for ch in inner:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count
