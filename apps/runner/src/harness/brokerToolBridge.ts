/**
 * 工具提案 → ExecutionBroker：禁止在 Runner 内直接读盘/起进程冒充成功。
 */
import {authorizeToolProposal} from '../roles.js';

export type BrokerToolPorts = {
  prepareEffect: (input: {
    lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
    logicalStepId: string;
    toolRef: string;
    inputArtifactId: string;
    /** 缺省 1；须与已登记 Step.intent_revision 对齐。 */
    intentRevision?: number;
  }) => Promise<{effectId: string; status: string; stateRevision: number}>;
  dispatchEffect: (input: {
    lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
    effectId: string;
    effectStateRevision: number;
  }) => Promise<{status: string}>;
};

/** Step 登记 + Effect 端口（EXECUTE 宿主完整链路）。 */
export type ExecuteToolPorts = BrokerToolPorts & {
  createStep: (input: {
    activityId: string;
    lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
    purpose: string;
    toolRef: string;
    predecessorStepId?: string | null;
  }) => Promise<{
    stepId: string;
    logicalStepId: string;
    intentRevision: number;
  }>;
};

export type ToolProposal = {
  kind: 'EXECUTE' | 'AUDIT' | 'INTEGRATE' | 'FINALIZE';
  tool: string;
  logicalStepId: string;
  inputArtifactId: string;
  lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
  intentRevision?: number;
};

export type ToolProposalNeedingStep = {
  kind: 'EXECUTE' | 'AUDIT' | 'INTEGRATE' | 'FINALIZE';
  tool: string;
  purpose: string;
  inputArtifactId: string;
  lease: {activity_id: string; attempt_id: string; fencing_epoch: string};
  activityId: string;
  predecessorStepId?: string | null;
};

/**
 * 将模型侧工具提案交给 Broker；本地永不执行 tool 副作用。
 *
 * **只 prepare，不 dispatch**（2026-09-17 修）。派发唯一归 Broker：
 * Broker 自己的执行函数以「对已 PREPARED 的 effect：dispatch → 执行 → 证据 → 回执」
 * 为契约，且它的取件队列只认 `PREPARED` / `AUTHORIZED`。
 * 此前本函数在 prepare 之后**又调了一次 dispatch**，把 effect 推到 `DISPATCHED` ——
 * 这一步会把 effect 从 Broker 的取件队列里**踢出去**，于是没有任何东西会执行它。
 *
 * 代价是竞态而非确定性失败：谁先谁后决定成败。实测同一函数在两条路径上分别呈现
 * 「成功」（Broker 先取到 PREPARED）与「永卡」（本函数先 dispatch）——
 * 执行的六个工具全 SUCCEEDED，而验收的 run_tests 永停 `DISPATCHED`、零回执，
 * 审计只能读到 `EFFECT_UNSETTLED`，Goal 因此到不了 DONE。
 *
 * 副作用咽喉路径只能有**一个**派发者。返回 `dispatchStatus: 'PREPARED'` 是诚实的：
 * 意图已提交并已准备，等待 Broker 派发与执行；本函数不声称已派发。
 */
export async function forwardToolProposalToBroker(
  ports: BrokerToolPorts,
  proposal: ToolProposal,
  registeredTools: ReadonlySet<string>,
): Promise<{effectId: string; dispatchStatus: string}> {
  authorizeToolProposal(proposal.kind, proposal.tool, registeredTools);
  const prepared = await ports.prepareEffect({
    lease: proposal.lease,
    logicalStepId: proposal.logicalStepId,
    toolRef: proposal.tool,
    inputArtifactId: proposal.inputArtifactId,
    intentRevision: proposal.intentRevision ?? 1,
  });
  // 已 DISPATCHED 说明 Broker 抢先派发过（幂等重放/并发窗口）：同样是可接受态，
  // 不再二次派发。其余状态一律拒绝，不冒充成功。
  if (prepared.status !== 'PREPARED' && prepared.status !== 'DISPATCHED') {
    throw new Error(`BROKER_PREPARE_REJECTED:${prepared.status}`);
  }
  return {effectId: prepared.effectId, dispatchStatus: prepared.status};
}

/** 先登记 Step，再 prepare/dispatch；仍不在本地执行工具。 */
export async function registerStepAndForwardToBroker(
  ports: ExecuteToolPorts,
  proposal: ToolProposalNeedingStep,
  registeredTools: ReadonlySet<string>,
): Promise<{effectId: string; dispatchStatus: string; logicalStepId: string}> {
  authorizeToolProposal(proposal.kind, proposal.tool, registeredTools);
  const step = await ports.createStep({
    activityId: proposal.activityId,
    lease: proposal.lease,
    purpose: proposal.purpose,
    toolRef: proposal.tool,
    predecessorStepId: proposal.predecessorStepId ?? null,
  });
  const forwarded = await forwardToolProposalToBroker(
    ports,
    {
      kind: proposal.kind,
      tool: proposal.tool,
      logicalStepId: step.logicalStepId,
      inputArtifactId: proposal.inputArtifactId,
      lease: proposal.lease,
      intentRevision: step.intentRevision,
    },
    registeredTools,
  );
  return {...forwarded, logicalStepId: step.logicalStepId};
}
