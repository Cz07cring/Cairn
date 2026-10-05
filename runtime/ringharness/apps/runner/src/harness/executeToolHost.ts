/**
 * EXECUTE 工具宿主薄封装：Step 登记 → Broker prepare/dispatch。
 * 本地永不读盘；须配合 toolAdmissionGate（心跳失败则关闭）。
 * dispatch 后默认只返回 DISPATCHED；终态由 Broker 回执或显式 poll，禁止宿主假 SUCCEEDED。
 * ≠ Cordis EXECUTE 全回合入口（见 cordisExecuteBridge.runExecuteToolTurnViaCordisWithLease）。
 * RunActivation 默认路径会装配本宿主。
 */
import {
  createHttpArtifactPutPorts,
  type ArtifactPutPorts,
} from './artifactPutHttpPorts.js';
import {
  createGatedHttpExecuteToolHost,
  type ControlHttpBrokerConfig,
  type ToolAdmissionGate,
} from './brokerToolHttpPorts.js';
import {
  createHttpEffectObservePorts,
  pollEffectStatus,
  type EffectObservePorts,
  type EffectStatusView,
} from './effectObserveHttpPorts.js';
import {
  registerStepAndForwardToBroker,
  type ExecuteToolPorts,
  type ToolProposalNeedingStep,
} from './brokerToolBridge.js';

export type ExecuteToolHost = {
  ports: ExecuteToolPorts;
  gate: ToolAdmissionGate;
  observe: EffectObservePorts;
  artifacts: ArtifactPutPorts;
};

export function createExecuteToolHost(
  config: ControlHttpBrokerConfig,
): ExecuteToolHost {
  const {ports, gate} = createGatedHttpExecuteToolHost(config);
  return {
    ports,
    gate,
    observe: createHttpEffectObservePorts(config),
    artifacts: createHttpArtifactPutPorts(config),
  };
}

/**
 * 将单次工具提案交给 Kernel/Broker；准入门关闭时失败关闭。
 * 返回 DISPATCHED 意图，不把 effect 标成业务成功。
 */
export async function dispatchExecuteToolProposal(
  host: ExecuteToolHost,
  proposal: ToolProposalNeedingStep,
  registeredTools: ReadonlySet<string> = new Set(['read_file']),
): Promise<{effectId: string; dispatchStatus: string; logicalStepId: string}> {
  if (!host.gate.allowed()) {
    throw new Error(
      `TOOL_ADMISSION_CLOSED:${host.gate.closedReason() ?? 'admission_closed'}`,
    );
  }
  return registerStepAndForwardToBroker(host.ports, proposal, registeredTools);
}

/**
 * 观察已 dispatch 的 effect；超时仍可能为 DISPATCHED（诚实），绝不自动写 SUCCEEDED 回执。
 */
export async function observeDispatchedEffect(
  host: ExecuteToolHost,
  effectId: string,
  opts?: {maxAttempts?: number; delayMs?: number; sleep?: (ms: number) => Promise<void>},
): Promise<EffectStatusView> {
  return pollEffectStatus(host.observe, effectId, opts);
}
