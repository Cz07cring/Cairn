"""Plan 发布前静态校验（M4 PlanStaticValidator）。

纯领域：无 DB/网络。环、悬空依赖、无 producer、路径边界冲突、
criterion 无验证路径、单任务预算超过 Goal。拒绝不得产生 effect / EXECUTE 准入
（由调用方在 submit_plan_outcome 前调用）。
"""

from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal
from uuid import UUID

from ..protocols.goals import Budget
from ..protocols.plans import PlanCreate


class PlanStaticViolation(Exception):
    """静态计划不合法；code 供日志/测试，message 为中文说明。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _acyclic(tasks: list) -> bool:
    deps = {t.id: set(t.contract.depends_on) for t in tasks}
    indegree = {tid: len(deps[tid]) for tid in deps}
    reverse: dict[UUID, list[UUID]] = defaultdict(list)
    for tid, parents in deps.items():
        for parent in parents:
            reverse[parent].append(tid)
    queue = deque([tid for tid, n in indegree.items() if n == 0])
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        for child in reverse[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return seen == len(deps)


def _ancestors(task_id: UUID, deps: dict[UUID, set[UUID]]) -> set[UUID]:
    out: set[UUID] = set()
    stack = list(deps.get(task_id, ()))
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(deps.get(cur, ()))
    return out


def _budget_exceeds(task: Budget, goal: Budget) -> str | None:
    """单任务任一维度超过 Goal 即不可能。"""
    checks: list[tuple[str, int | Decimal, int | Decimal]] = [
        ("wall_clock_seconds", task.wall_clock_seconds, goal.wall_clock_seconds),
        ("max_tokens", task.max_tokens, goal.max_tokens),
        ("max_tool_calls", task.max_tool_calls, goal.max_tool_calls),
        ("max_network_calls", task.max_network_calls, goal.max_network_calls),
        ("max_disk_bytes", task.max_disk_bytes, goal.max_disk_bytes),
        ("max_cost_usd", Decimal(task.max_cost_usd), Decimal(goal.max_cost_usd)),
    ]
    for name, tv, gv in checks:
        if tv > gv:
            return name
    if task.max_gpu_seconds is not None and goal.max_gpu_seconds is not None and (
        Decimal(task.max_gpu_seconds) > Decimal(goal.max_gpu_seconds)
    ):
        return "max_gpu_seconds"
    return None


def validate_plan_static(
    body: PlanCreate,
    *,
    goal_budget: Budget,
    goal_criterion_ids: set[str],
    required_goal_criterion_ids: set[str],
) -> None:
    """发布前结构闸门。通过则静默；失败抛 PlanStaticViolation。"""
    known = {t.id for t in body.tasks}
    if len(known) != len(body.tasks):
        raise PlanStaticViolation("PLAN_DUPLICATE_TASK", "任务 id 重复")

    for task in body.tasks:
        for dep in task.contract.depends_on:
            if dep not in known:
                raise PlanStaticViolation(
                    "PLAN_DANGLING_DEPENDENCY",
                    f"任务 {task.id} 依赖悬空：{dep}",
                )
            if dep == task.id:
                raise PlanStaticViolation(
                    "PLAN_DANGLING_DEPENDENCY",
                    f"任务 {task.id} 不能依赖自身",
                )

    if not _acyclic(body.tasks):
        raise PlanStaticViolation("PLAN_CYCLE", "任务依赖存在环")

    deps_map = {t.id: set(t.contract.depends_on) for t in body.tasks}
    by_id = {t.id: t for t in body.tasks}

    # 无 producer：被依赖任务须至少有一条 deliverable
    for task in body.tasks:
        for dep in task.contract.depends_on:
            parent = by_id[dep]
            if not parent.contract.deliverables:
                raise PlanStaticViolation(
                    "PLAN_MISSING_PRODUCER",
                    f"任务 {task.id} 依赖的 {dep} 无 deliverable（无 producer）",
                )

    # 路径边界：任务内 allowed ∩ protected；无依赖关系的任务不得声明同一 allowed 路径
    for task in body.tasks:
        allowed = set(task.contract.allowed_paths)
        protected = set(task.contract.protected_paths)
        clash = allowed & protected
        if clash:
            raise PlanStaticViolation(
                "PLAN_PATH_BOUNDARY_CONFLICT",
                f"任务 {task.id} 的 allowed/protected 冲突：{min(clash)}",
            )

    task_ids = list(by_id.keys())
    for i, a_id in enumerate(task_ids):
        for b_id in task_ids[i + 1 :]:
            a = by_id[a_id]
            b = by_id[b_id]
            related = (
                a_id in _ancestors(b_id, deps_map)
                or b_id in _ancestors(a_id, deps_map)
            )
            if related:
                continue
            overlap = set(a.contract.allowed_paths) & set(b.contract.allowed_paths)
            # 忽略空与通配过大的误报：仅精确相同非空路径
            overlap = {p for p in overlap if p and p != "**"}
            if overlap:
                raise PlanStaticViolation(
                    "PLAN_PATH_BOUNDARY_CONFLICT",
                    f"无依赖任务 {a_id} 与 {b_id} 共享 allowed_paths：{min(overlap)}",
                )

    # criterion 验证路径：必要目标标准须有 coverage；coverage 指向可达 required 验收
    if not required_goal_criterion_ids.issubset(
        {e.goal_criterion_id for e in body.coverage}
    ):
        raise PlanStaticViolation(
            "PLAN_CRITERION_NO_VERIFICATION_PATH",
            "必要目标标准缺少 coverage 验证路径",
        )

    for entry in body.coverage:
        if entry.goal_criterion_id not in goal_criterion_ids:
            raise PlanStaticViolation(
                "PLAN_CRITERION_NO_VERIFICATION_PATH",
                f"覆盖引用未知目标标准：{entry.goal_criterion_id}",
            )
        task = by_id.get(entry.task_id)
        if task is None:
            raise PlanStaticViolation(
                "PLAN_CRITERION_NO_VERIFICATION_PATH",
                f"coverage 引用未知任务：{entry.task_id}",
            )
        acceptance = {c.id: c for c in task.contract.acceptance}
        acc = acceptance.get(entry.task_acceptance_id)
        if acc is None:
            raise PlanStaticViolation(
                "PLAN_CRITERION_NO_VERIFICATION_PATH",
                f"任务 {entry.task_id} 无验收项 {entry.task_acceptance_id}",
            )
        if not acc.required:
            raise PlanStaticViolation(
                "PLAN_CRITERION_NO_VERIFICATION_PATH",
                f"coverage 指向非 required 验收 {entry.task_acceptance_id}",
            )

    # 预算不可能：单任务任一维度超过 Goal
    for task in body.tasks:
        field = _budget_exceeds(task.contract.budget, goal_budget)
        if field is not None:
            raise PlanStaticViolation(
                "PLAN_BUDGET_IMPOSSIBLE",
                f"任务 {task.id} 预算维度 {field} 超过 Goal 上限",
            )
