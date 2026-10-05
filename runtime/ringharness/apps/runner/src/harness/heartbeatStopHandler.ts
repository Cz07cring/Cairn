/**
 * heartbeat.control=STOP 时：关闭工具准入，并对 pending_stop_ids 交诚实观察。
 * Harness 未接进程杀伤 → observeStop(adapter=harness) 报 RUNNING，不得假 EXITED。
 * ≠ Goal DONE。
 */
import {randomUUID} from 'node:crypto';
import {
  observeStop,
  type StopAdapterKind,
  type StopSeamPorts,
} from './stopSeam.js';

export type HeartbeatStopGate = {
  onHeartbeatFailure: (err: unknown) => void;
};

export type HeartbeatStopApplyResult = {
  closedAdmission: boolean;
  observations: Array<{
    stopId: string;
    observation: string;
    disposition: string;
  }>;
  marksGoalDone: false;
};

/**
 * 处理 Kernel heartbeat 的 STOP 信号。
 * control 非 STOP 时零副作用。
 */
export async function applyHeartbeatStopControl(input: {
  control?: string;
  pendingStopIds?: readonly string[];
  gate?: HeartbeatStopGate;
  stopSeam?: StopSeamPorts;
  activationId: string;
  attemptId: string;
  /** 默认 harness：诚实 RUNNING */
  adapter?: StopAdapterKind;
  newReceiptId?: () => string;
}): Promise<HeartbeatStopApplyResult> {
  if (input.control !== 'STOP') {
    return {closedAdmission: false, observations: [], marksGoalDone: false};
  }

  const ids = [...(input.pendingStopIds ?? [])];
  input.gate?.onHeartbeatFailure(
    new Error(`HEARTBEAT_CONTROL_STOP:${ids.join(',') || 'pending'}`),
  );

  const observations: HeartbeatStopApplyResult['observations'] = [];
  if (!input.stopSeam || ids.length === 0) {
    return {closedAdmission: true, observations, marksGoalDone: false};
  }

  const adapter = input.adapter ?? 'harness';
  const newId = input.newReceiptId ?? (() => randomUUID());
  for (const stopId of ids) {
    try {
      const result = await observeStop(input.stopSeam, {
        stopId,
        adapter,
        activationId: input.activationId,
        attemptId: input.attemptId,
        receiptId: newId(),
      });
      if (adapter === 'harness' && result.observation === 'EXITED') {
        throw new Error('STOP_SEAM_FORGED_EXITED');
      }
      observations.push({
        stopId,
        observation: result.observation,
        disposition: result.disposition,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      input.gate?.onHeartbeatFailure(
        new Error(`HEARTBEAT_STOP_RECEIPT_FAILED:${stopId}:${msg}`),
      );
    }
  }

  return {closedAdmission: true, observations, marksGoalDone: false};
}
