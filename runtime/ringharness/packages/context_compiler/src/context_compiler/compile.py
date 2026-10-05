"""compile(activity_snapshot, token_budget) → ContextBundle content。"""

from __future__ import annotations

from dataclasses import dataclass

from .snapshot import ActivitySnapshot


class CompileRejected(Exception):
    """预算不足或快照不完整，禁止调用模型。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class TokenBudget:
    """粗粒度输入预算；强制绑定优先，超限则拒绝而非静默裁剪合同。"""

    max_input_tokens: int = 8192


def _estimate_tokens(snapshot: ActivitySnapshot) -> int:
    # 清单级粗估：每个 binding ~256 token，反馈与排除各计少量开销。
    base = 128
    bindings = len(snapshot.input_bindings) * 256
    extras = len(snapshot.excluded_refs) * 16 + len(snapshot.planning_feedback_ids) * 32
    return base + bindings + extras


def compile_context_bundle(snapshot: ActivitySnapshot, budget: TokenBudget | None = None) -> dict:
    budget = budget or TokenBudget()
    if budget.max_input_tokens < 1:
        raise CompileRejected("token_budget.max_input_tokens 必须 ≥ 1")
    if snapshot.role == "PLANNER" and snapshot.goal_id is None:
        raise CompileRejected("PLANNER 上下文必须绑定 Goal")
    if snapshot.role != "PLANNER" and snapshot.planning_feedback_ids:
        raise CompileRejected("仅 PLANNER 可接收 planning_feedback_ids")

    estimated = _estimate_tokens(snapshot)
    if estimated > budget.max_input_tokens:
        raise CompileRejected(
            f"上下文超出 token 预算（估算 {estimated} > {budget.max_input_tokens}），"
            "不得静默裁剪合同或 Skill"
        )

    # Content v3 set 语义：按 artifact_id+classification 排序去重由 encode 再保证。
    bindings = sorted(
        snapshot.input_bindings,
        key=lambda b: (str(b.artifact_id), b.classification),
    )
    seen: set[tuple[str, str]] = set()
    input_bindings = []
    for item in bindings:
        key = (str(item.artifact_id), item.classification)
        if key in seen:
            raise CompileRejected("input_bindings 存在重复键")
        seen.add(key)
        input_bindings.append(
            {
                "artifact_id": str(item.artifact_id),
                "digest": item.digest,
                "classification": item.classification,
            }
        )

    excluded = sorted(snapshot.excluded_refs, key=lambda pair: pair[0])
    excluded_refs = [{"ref": ref, "reason_code": reason} for ref, reason in excluded]
    feedback = sorted(str(i) for i in snapshot.planning_feedback_ids)

    return {
        "project_id": str(snapshot.project_id),
        "goal_id": str(snapshot.goal_id) if snapshot.goal_id is not None else None,
        "activity_id": str(snapshot.activity_id),
        "role": snapshot.role,
        "goal_contract_revision": snapshot.goal_contract_revision,
        "task_contract_revision": snapshot.task_contract_revision,
        "plan_revision": snapshot.plan_revision,
        "input_bindings": input_bindings,
        "excluded_refs": excluded_refs,
        "session_generation": snapshot.session_generation,
        "compaction_source_digest": snapshot.compaction_source_digest,
        "planning_feedback_ids": feedback,
    }
