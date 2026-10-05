/**
 * 命令进度与 pause/resume/cancel 门禁投影；命令 SUCCEEDED ≠ Goal DONE。
 */
export type GoalControlAction = 'pause' | 'resume' | 'cancel';

export type GoalControlEligibility = {
  canPause: boolean;
  canResume: boolean;
  canCancel: boolean;
  reason: string;
};

/** 对齐 Kernel control_commands 状态机；UI 不乐观改终态。 */
export function goalControlEligibility(
  status: string | null | undefined,
  operatorOk: boolean,
): GoalControlEligibility {
  if (!operatorOk) {
    return {
      canPause: false,
      canResume: false,
      canCancel: false,
      reason: '须 operator 才能提交 pause/resume/cancel',
    };
  }
  if (!status) {
    return {
      canPause: false,
      canResume: false,
      canCancel: false,
      reason: '请先选择观察 Goal',
    };
  }
  const canPause = ['PLANNING', 'RUNNING', 'VERIFYING'].includes(status);
  const canResume = status === 'PAUSED';
  const canCancel = ![
    'DRAFT',
    'CANCELLED',
    'CANCELLING',
    'DONE',
    'FAILED',
  ].includes(status);
  if (!canPause && !canResume && !canCancel) {
    return {
      canPause: false,
      canResume: false,
      canCancel: false,
      reason: `状态 ${status} 不可 pause/resume/cancel`,
    };
  }
  return {
    canPause,
    canResume,
    canCancel,
    reason:
      '可提交控制命令；202/PAUSING/CANCELLING ≠ PAUSED/CANCELLED ≠ DONE（须 Stop 确认）',
  };
}

export function commandRowLabel(cmd: {
  id: string;
  kind: string;
  status: string;
  result?: {final_status?: string | null} | null;
}): string {
  const final = cmd.result?.final_status
    ? ` → ${cmd.result.final_status}`
    : '';
  return `${cmd.kind} · ${cmd.status}${final} (${cmd.id.slice(0, 8)}…)`;
}

export function commandsCaption(input: {
  count: number;
  anySucceeded: boolean;
}): string {
  if (input.count === 0) {
    return '暂无命令；创建/START/控制后会出现在此。列表 ≠ DONE。';
  }
  const tip = input.anySucceeded
    ? '含 SUCCEEDED 命令；命令成功 ≠ Goal DONE。'
    : '命令受理/处理中；202 ≠ 业务终态。';
  return `共 ${input.count} 条。${tip}`;
}

export function controlSuccessCaption(input: {
  action: GoalControlAction;
  commandStatus: string;
  finalStatus: string | null | undefined;
}): string {
  const finalPart = input.finalStatus ? `；Goal→${input.finalStatus}` : '';
  return `${input.action} 命令 ${input.commandStatus}${finalPart}。排空/Stop 确认前勿当作终态；≠ DONE。`;
}
