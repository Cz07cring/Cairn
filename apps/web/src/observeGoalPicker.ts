/**
 * 观察 Goal 选择器与编排放弃解除门禁投影（Issue #24）；≠ DONE。
 */
export type ObserveGoalOption = {
  id: string;
  status: string;
  stateRevision: number;
  objective: string;
  blockReason: string | null;
};

export type AbandonmentReleaseEligibility = {
  canRelease: boolean;
  reason: string;
};

export function toObserveGoalOption(goal: {
  id: string;
  status: string;
  state_revision: number;
  block_reason?: string | null;
  contract?: {objective?: string};
}): ObserveGoalOption {
  return {
    id: goal.id,
    status: goal.status,
    stateRevision: goal.state_revision,
    objective: goal.contract?.objective ?? '',
    blockReason: goal.block_reason ?? null,
  };
}

export function observeGoalOptionLabel(goal: ObserveGoalOption): string {
  const obj = goal.objective.trim().slice(0, 40) || goal.id.slice(0, 8);
  return `${goal.status} · ${obj} (${goal.id.slice(0, 8)}…)`;
}

const ACTIVE_STATUS_ORDER = ['RUNNING', 'VERIFYING', 'PLANNING', 'BLOCKED', 'PAUSED'];

/** 没有有效的历史选择时，优先打开正在推进的 Goal，避免驾驶舱只显示空壳。 */
export function preferredObserveGoalId(
  goals: ObserveGoalOption[],
  storedId: string,
): string {
  if (goals.some((goal) => goal.id === storedId)) {
    return storedId;
  }
  for (const status of ACTIVE_STATUS_ORDER) {
    const active = goals.find((goal) => goal.status === status);
    if (active) {
      return active.id;
    }
  }
  return goals[0]?.id ?? '';
}

/** BLOCKED + ORCHESTRATION_ABANDONED:* + operator → 可尝试解除。 */
export function abandonmentReleaseEligibility(
  goal: ObserveGoalOption | null,
  operatorOk: boolean,
  abandonmentCount: number,
): AbandonmentReleaseEligibility {
  if (!operatorOk) {
    return {canRelease: false, reason: '须 operator 才能解除编排放弃封锁'};
  }
  if (goal == null) {
    return {canRelease: false, reason: '请先选择观察 Goal'};
  }
  if (abandonmentCount < 1) {
    return {canRelease: false, reason: '无编排放弃记录（健康或未登记放弃）'};
  }
  if (goal.status !== 'BLOCKED') {
    return {
      canRelease: false,
      reason: `状态为 ${goal.status}；仅 BLOCKED 可解除放弃封锁`,
    };
  }
  const reason = goal.blockReason ?? '';
  if (!reason.startsWith('ORCHESTRATION_ABANDONED:')) {
    return {
      canRelease: false,
      reason: 'block_reason 非编排放弃，拒绝本入口',
    };
  }
  return {
    canRelease: true,
    reason:
      '可尝试解除 ORCHESTRATION_ABANDONED（须无未确认 SHUTDOWN；解除 ≠ DONE）',
  };
}

export function abandonmentCaption(input: {
  goalStatus: string;
  count: number;
  marksGoalDoneAny: boolean;
}): string {
  if (input.marksGoalDoneAny) {
    return '异常：放弃记录声称 marks_goal_done；放弃绝不等于 DONE。';
  }
  if (input.count === 0) {
    return `当前 Goal 状态 ${input.goalStatus}；无编排放弃事实。`;
  }
  return `已登记 ${input.count} 条编排放弃；Goal=${input.goalStatus}；放弃 ≠ DONE，需人工介入。`;
}
