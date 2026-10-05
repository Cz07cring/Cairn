/**
 * DRAFT Goal START 门禁投影；命令受理 ≠ Goal DONE。
 */
export type StartGoalCandidate = {
  id: string;
  status: string;
  stateRevision: number;
  objective: string;
  orchestrationBackend: string;
};

export type StartGoalEligibility = {
  canStart: boolean;
  reason: string;
};

/** 仅 DRAFT 可 START；其它状态一律拒绝按钮。 */
export function startGoalEligibility(
  goal: StartGoalCandidate | null,
  operatorOk: boolean,
): StartGoalEligibility {
  if (!operatorOk) {
    return {canStart: false, reason: '须 operator（或开发 Bearer 试探）'};
  }
  if (goal == null) {
    return {canStart: false, reason: '请选择 DRAFT Goal'};
  }
  if (goal.status !== 'DRAFT') {
    return {
      canStart: false,
      reason: `状态为 ${goal.status}，无法 START（仅 DRAFT）`,
    };
  }
  if (!Number.isInteger(goal.stateRevision) || goal.stateRevision < 1) {
    return {canStart: false, reason: 'state_revision 无效，请刷新'};
  }
  return {
    canStart: true,
    reason: `可 START（backend=${goal.orchestrationBackend}）；202 ≠ DONE`,
  };
}

export function startGoalSuccessCaption(input: {
  commandStatus: string;
  finalStatus: string | null | undefined;
}): string {
  const finalPart = input.finalStatus
    ? `；Goal 进入 ${input.finalStatus}`
    : '';
  return `START 命令 ${input.commandStatus}${finalPart}。命令成功 ≠ Harness 就绪 ≠ Goal DONE。`;
}

export function goalOptionLabel(goal: StartGoalCandidate): string {
  const obj =
    goal.objective.trim().slice(0, 48) || goal.id.slice(0, 8);
  return `${goal.status} · ${obj} (${goal.id.slice(0, 8)}…)`;
}

export function toStartGoalCandidate(goal: {
  id: string;
  status: string;
  state_revision: number;
  orchestration_backend?: string;
  contract?: {objective?: string};
}): StartGoalCandidate {
  return {
    id: goal.id,
    status: goal.status,
    stateRevision: goal.state_revision,
    objective: goal.contract?.objective ?? '',
    orchestrationBackend: goal.orchestration_backend ?? 'LEGACY',
  };
}
