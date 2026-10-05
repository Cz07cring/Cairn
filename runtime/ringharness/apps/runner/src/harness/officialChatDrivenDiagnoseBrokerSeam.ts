/**
 * chat-driven 诊断全周期 × Broker Gateway。
 * read→run_tests→write→run_tests；scriptedOrder=false；≠ Goal DONE。
 *
 * AB05：Broker 工具与官方 LLM adapter 共享同一 ActivationWatchdog（绑 host.gate）。
 * AB06：可选 goalId → 跨 activation 持久 Nudge 预算。
 */
import type {NoProgressGuard} from './noProgressGuard.js';
import {
  createAb01BrokerExecuteTool,
  type Ab01BrokerGateway,
} from './ab01BrokerGateway.js';
import type {BrokerBackedHarnessToolConfig} from './brokerBackedHarnessTool.js';
import type {ActivationWatchdog} from './activationWatchdog.js';
import {
  runOfficialAgentLoopChatDrivenDiagnoseCycleTurn,
  type OfficialAgentLoopMultistepEvidence,
  type OfficialLoopAb08Opts,
} from './officialAgentLoopHost.js';
import type {OfficialAdapterWatchdogOpts} from './officialAgentLoopOpenAiAdapter.js';
import type {OpenAiCompatibleToolChatConfig} from './openaiCompatibleToolChat.js';
import type {ModelInvocationLedgerPorts} from './modelInvocationLedger.js';

export type OfficialChatDrivenDiagnoseBrokerSeamInput = {
  checkoutDir?: string;
  broker: BrokerBackedHarnessToolConfig;
  chat: OpenAiCompatibleToolChatConfig;
  userPrompt: string;
  readPath: string;
  writePath: string;
  writeContent: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  useInjectedDecisionFsm?: boolean;
  sealVerificationProfileIds?: string[];
  acceptanceDescriptions?: string[];
  /** AB08：官方 Loop idle 后 Turn 门禁 */
  ab08?: OfficialLoopAb08Opts;
  /** AB06：拉取 ForceStop 总结 Artifact 正文（可选） */
  getForceStopSummaryContent?: (artifactId: string) => Promise<string>;
  /** AB06：落盘零工具收口 Artifact（可选） */
  putForceStopCloseout?: (body: string) => Promise<{artifactId: string}>;
  /** AB05：注入共享 watchdog（必须已绑 broker host.gate，或与 progressGuard 同 gate）。
   * 缺省：先建 Broker 工具，再用 tool.watchdog（与 progressGuard / 准入共 gate）。
   */
  watchdog?: ActivationWatchdog;
  /** 显式注入无进展守卫；缺省用 Broker 默认（可经 goalId 共享 Nudge 账） */
  progressGuard?: NoProgressGuard;
  /** AB06：同 Goal 跨 activation Nudge 预算键 */
  goalId?: string;
  watchdogTick?: Omit<OfficialAdapterWatchdogOpts, 'watchdog'>;
  /** Codex P0-1：官方 chat → ModelInvocation 账本 */
  modelInvocationLedger?: ModelInvocationLedgerPorts;
};

export type OfficialChatDrivenDiagnoseBrokerSeamResult =
  OfficialAgentLoopMultistepEvidence & {
    gateway: Ab01BrokerGateway;
    /** AB05：与 LLM/工具共享的钟（观测用） */
    watchdog: ActivationWatchdog;
    /** AB06：与工具共用的无进展守卫（观测用） */
    progressGuard: NoProgressGuard;
    marksGoalDone: false;
  };

export async function runOfficialChatDrivenDiagnoseBrokerCycle(
  input: OfficialChatDrivenDiagnoseBrokerSeamInput,
): Promise<OfficialChatDrivenDiagnoseBrokerSeamResult> {
  // 先建 Broker：默认 watchdog+progressGuard 均绑 host.gate。
  // 禁止在此用 createHeartbeatLinkedAdmissionGate() 另造孤儿钟再注入——
  // Hard 会关错 gate，工具准入仍开（AB05 假共享）。
  const broker: BrokerBackedHarnessToolConfig = {
    ...input.broker,
    goalId: input.goalId ?? input.broker.goalId,
    ...(input.watchdog ? {watchdog: input.watchdog} : {}),
    ...(input.progressGuard ? {progressGuard: input.progressGuard} : {}),
  };
  const gateway = createAb01BrokerExecuteTool(broker);
  const sharedWatchdog = gateway.tool.watchdog;
  const sharedProgress = gateway.tool.progressGuard;
  const evidence = await runOfficialAgentLoopChatDrivenDiagnoseCycleTurn({
    checkoutDir: input.checkoutDir,
    executeTool: gateway.executeTool,
    userPrompt: input.userPrompt,
    readPath: input.readPath,
    writePath: input.writePath,
    writeContent: input.writeContent,
    sessionId: input.sessionId,
    idleTimeoutMs: input.idleTimeoutMs,
    chat: input.chat,
    useInjectedDecisionFsm: input.useInjectedDecisionFsm,
    sealVerificationProfileIds: input.sealVerificationProfileIds,
    acceptanceDescriptions: input.acceptanceDescriptions,
    ab08: input.ab08,
    watchdog: sharedWatchdog,
    watchdogTick: input.watchdogTick,
    getForceStopSummaryContent: input.getForceStopSummaryContent,
    putForceStopCloseout: input.putForceStopCloseout,
    modelInvocationLedger: input.modelInvocationLedger,
  });
  return {
    ...evidence,
    gateway,
    watchdog: sharedWatchdog,
    progressGuard: sharedProgress,
    marksGoalDone: false,
  };
}
