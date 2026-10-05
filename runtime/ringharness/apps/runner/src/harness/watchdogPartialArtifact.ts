/**
 * AB05：Watchdog Hard Idle 时将已收到的部分助手文本落 collector Artifact。
 * 证据可对账；绝不是 Goal DONE；无正文则跳过（返回 null）。
 */
import {WatchdogHardIdleError} from './activationWatchdog.js';
import type {ArtifactPutPorts} from './artifactPutHttpPorts.js';

export type WatchdogPartialLease = {
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
};

export type PersistWatchdogHardIdlePartialInput = {
  artifacts: Pick<ArtifactPutPorts, 'putCollectorContent'>;
  projectId: string;
  lease: WatchdogPartialLease;
  error: WatchdogHardIdleError;
};

export type PersistWatchdogHardIdlePartialResult = {
  artifactId: string;
  digest: string;
  /** 红线：部分输出 Artifact ≠ Goal DONE */
  marksGoalDone: false;
};

export type WatchdogHardIdlePartialDocument = {
  schema_version: 1;
  kind: 'WATCHDOG_HARD_IDLE_PARTIAL';
  phase: string;
  idle_ms: number;
  closes_admission: true;
  marks_goal_done: false;
  requires_reconciliation: boolean;
  partial_assistant_text: string;
};

export function buildWatchdogHardIdlePartialDocument(
  error: WatchdogHardIdleError,
): WatchdogHardIdlePartialDocument | null {
  const text = (error.partialAssistantText || '').trim();
  if (!text) {
    return null;
  }
  return {
    schema_version: 1,
    kind: 'WATCHDOG_HARD_IDLE_PARTIAL',
    phase: error.outcome.phase,
    idle_ms: error.outcome.idleMs,
    closes_admission: true,
    marks_goal_done: false,
    requires_reconciliation: error.outcome.requiresReconciliation,
    partial_assistant_text: text,
  };
}

/**
 * 有 partial 正文则 PUT collector；失败向上抛（调用方仍须 FAILED ≠ DONE）。
 * 无正文 → null（Hard 仍成立，只是无部分输出可存）。
 */
export async function persistWatchdogHardIdlePartial(
  input: PersistWatchdogHardIdlePartialInput,
): Promise<PersistWatchdogHardIdlePartialResult | null> {
  const doc = buildWatchdogHardIdlePartialDocument(input.error);
  if (!doc) {
    return null;
  }
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

/** 组装 FAILED reason；可附 partial_artifact=；恒声明 ≠DONE。 */
export function formatWatchdogHardIdleFailReason(
  error: WatchdogHardIdleError,
  partialArtifactId?: string,
): string {
  const base = `WATCHDOG_HARD_IDLE:${error.outcome.phase}:epoch=${error.outcome.phaseEpoch}:idleMs=${error.outcome.idleMs}`;
  if (partialArtifactId) {
    return `${base}:partial_artifact=${partialArtifactId}`;
  }
  return base;
}

export function isWatchdogHardIdleError(err: unknown): err is WatchdogHardIdleError {
  return err instanceof WatchdogHardIdleError;
}
