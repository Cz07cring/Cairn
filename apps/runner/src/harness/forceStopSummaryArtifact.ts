/**
 * AB06：ForceStop 零工具总结 Artifact。
 * 允许模型侧一次总结证据落盘；绝不是 Audit PASS / Goal DONE。
 */
import type {ArtifactPutPorts} from './artifactPutHttpPorts.js';
import type {GuardVerdict} from './noProgressGuard.js';

export type ForceStopSummaryLease = {
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
};

export type ForceStopSummaryContext = {
  tool?: string;
  argsDigest?: string;
  callId?: string;
};

export type ForceStopSummaryDocument = {
  schema_version: 1;
  kind: 'NO_PROGRESS_FORCE_STOP_SUMMARY';
  reason: string;
  code: 'NO_PROGRESS_FORCE_STOP';
  close_tool_admission: boolean;
  allow_zero_tool_summary: true;
  marks_goal_done: false;
  tool?: string;
  args_digest?: string;
  call_id?: string;
};

export type PersistForceStopSummaryResult = {
  artifactId: string;
  digest: string;
  marksGoalDone: false;
};

export function buildForceStopSummaryDocument(
  verdict: Extract<GuardVerdict, {action: 'force_stop'}>,
  context: ForceStopSummaryContext = {},
): ForceStopSummaryDocument {
  return {
    schema_version: 1,
    kind: 'NO_PROGRESS_FORCE_STOP_SUMMARY',
    reason: verdict.reason,
    code: 'NO_PROGRESS_FORCE_STOP',
    close_tool_admission: verdict.closeToolAdmission,
    allow_zero_tool_summary: true,
    marks_goal_done: false,
    ...(context.tool ? {tool: context.tool} : {}),
    ...(context.argsDigest ? {args_digest: context.argsDigest} : {}),
    ...(context.callId ? {call_id: context.callId} : {}),
  };
}

/**
 * ForceStop 时 PUT collector JSON；失败向上抛（调用方可吞并仍返回 ToolResult ≠ DONE）。
 */
export async function persistForceStopSummary(input: {
  artifacts: Pick<ArtifactPutPorts, 'putCollectorContent'>;
  projectId: string;
  lease: ForceStopSummaryLease;
  verdict: Extract<GuardVerdict, {action: 'force_stop'}>;
  context?: ForceStopSummaryContext;
}): Promise<PersistForceStopSummaryResult> {
  const doc = buildForceStopSummaryDocument(input.verdict, input.context);
  const body = JSON.stringify(doc);
  const put = await input.artifacts.putCollectorContent({
    projectId: input.projectId,
    lease: input.lease,
    body,
    mime: 'application/json',
  });
  return {
    artifactId: put.artifactId,
    digest: put.digest,
    marksGoalDone: false,
  };
}

/** 准入门关闭原因是否来自 No-Progress 硬 ForceStop（关闸先于 PUT 的崩溃窗口）。 */
export function isNoProgressForceStopClosedReason(
  closedReason: string | null | undefined,
): boolean {
  return (closedReason ?? '').includes('NO_PROGRESS_FORCE_STOP');
}

/**
 * 闸门已关时，若关闭原因可归因于 No-Progress 硬 ForceStop，
 * 返回可用于补落总结 Artifact 的 verdict；其它关闭原因返回 null（禁止冒充）。
 * 注意：EFFECT_UNKNOWN 等也会让 Guard 记 force_stop，但 closedReason 不同——不得走本路径。
 */
export function forceStopVerdictFromClosedGate(
  closedReason: string | null | undefined,
  last?: GuardVerdict | null,
): Extract<GuardVerdict, {action: 'force_stop'}> | null {
  if (!isNoProgressForceStopClosedReason(closedReason)) {
    return null;
  }
  if (last?.action === 'force_stop') {
    return last;
  }
  const raw = closedReason ?? 'admission_closed';
  const prefix = 'NO_PROGRESS_FORCE_STOP:';
  const reason = raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
  return {
    action: 'force_stop',
    code: 'NO_PROGRESS_FORCE_STOP',
    reason: reason || 'admission_closed',
    closeToolAdmission: true,
    allowZeroToolSummary: true,
    marksGoalDone: false,
  };
}
