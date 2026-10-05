/**
 * Runner stop seam（doc/05）：接收 StopRequest 意图并提交可信 StopReceipt。
 * Fake adapter 无真实进程，可诚实报告 EXITED+compute_released。
 * Harness 未接进程杀伤前不得伪造 EXITED（应报 RUNNING/UNKNOWN 或 ISOLATED）。
 */
import type {RuntimeEvent} from './activation.js';

export type StopRequestView = {
  request_id: string;
  activation_id: string;
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
  reason: 'PAUSE' | 'CANCEL' | 'LEASE_EXPIRED' | 'FINALIZATION_RECOVERY' | 'SHUTDOWN' | 'TRUST_INVALIDATION';
  deadline_at: string;
};

export type StopResourceView = {
  id: string;
  status: 'REQUESTED' | 'CONFIRMED' | 'UNCONFIRMED';
  state_revision: number;
  receipt_ids: string[];
};

export type StopReceiptView = {
  receipt_id: string;
  stop_id: string;
  activation_id: string;
  attempt_id: string;
  resource_instance_id: string;
  observed_at: string;
  observation: 'EXITED' | 'ISOLATED' | 'RUNNING' | 'UNKNOWN';
  compute_released: boolean;
  write_capability_revoked: boolean;
  proof_artifact_ids: string[];
};

export type StopSeamPorts = {
  /** Kernel 已登记的 StopResource（REQUESTED）。 */
  loadStop: (stopId: string) => Promise<StopResourceView>;
  postReceipt: (body: StopReceiptView) => Promise<{disposition: string}>;
  /** 可选：记录 STOP_OBSERVED（仅运行输入，≠ 业务成功）。 */
  emitRuntimeEvent?: (event: RuntimeEvent) => Promise<void>;
};

export type StopAdapterKind = 'fake' | 'harness';

/**
 * 对已 REQUESTED 的 stop 提交观察。
 * fake：无进程可杀 → EXITED∧compute_released（诚实）。
 * harness：本切片未接杀进程 → RUNNING∧未释放（失败关闭，不假 CONFIRMED）。
 */
export async function observeStop(
  ports: StopSeamPorts,
  input: {
    stopId: string;
    adapter: StopAdapterKind;
    activationId: string;
    attemptId: string;
    resourceInstanceId?: string;
    receiptId: string;
    nowIso?: string;
  },
): Promise<{disposition: string; observation: StopReceiptView['observation']}> {
  const stop = await ports.loadStop(input.stopId);
  if (stop.status === 'CONFIRMED') {
    return {disposition: 'DUPLICATE', observation: 'EXITED'};
  }

  const observedAt = input.nowIso ?? new Date().toISOString();
  let observation: StopReceiptView['observation'];
  let computeReleased: boolean;
  let writeRevoked: boolean;

  if (input.adapter === 'fake') {
    observation = 'EXITED';
    computeReleased = true;
    writeRevoked = true;
  } else {
    // Harness 未实现进程杀伤：不得报告 EXITED
    observation = 'RUNNING';
    computeReleased = false;
    writeRevoked = false;
  }

  const receipt: StopReceiptView = {
    receipt_id: input.receiptId,
    stop_id: input.stopId,
    activation_id: input.activationId,
    attempt_id: input.attemptId,
    resource_instance_id: input.resourceInstanceId ?? input.attemptId,
    observed_at: observedAt,
    observation,
    compute_released: computeReleased,
    write_capability_revoked: writeRevoked,
    proof_artifact_ids: [],
  };

  if (ports.emitRuntimeEvent) {
    await ports.emitRuntimeEvent({
      activation_id: input.activationId,
      seq: 1,
      type: 'STOP_OBSERVED',
      payload_ref: null,
    });
  }

  const accepted = await ports.postReceipt(receipt);
  return {disposition: accepted.disposition, observation};
}
