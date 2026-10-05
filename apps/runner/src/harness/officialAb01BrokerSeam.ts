/**
 * 官方 AgentLoop × AB01 Broker Gateway 缝。
 * 循环归上游 AgentLoop；副作用经 createAb01BrokerExecuteTool（Runner 不 dispatch）。
 * ≠ Goal DONE；≠ RunActivation 默认路径。
 */
import {
  createAb01BrokerExecuteTool,
  type Ab01BrokerGateway,
} from './ab01BrokerGateway.js';
import type {BrokerBackedHarnessToolConfig} from './brokerBackedHarnessTool.js';
import {
  runOfficialAgentLoopLiveReadFileTurn,
  type OfficialAgentLoopTurnEvidence,
} from './officialAgentLoopHost.js';
import type {OpenAiCompatibleToolChatConfig} from './openaiCompatibleToolChat.js';
import type {ModelInvocationLedgerPorts} from './modelInvocationLedger.js';

export type OfficialAb01BrokerSeamInput = {
  checkoutDir?: string;
  chat: OpenAiCompatibleToolChatConfig;
  broker: BrokerBackedHarnessToolConfig;
  userPrompt: string;
  expectedPath: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  provider?: string;
  modelId?: string;
  /** Codex P0-1：官方 chat → ModelInvocation 账本 */
  modelInvocationLedger?: ModelInvocationLedgerPorts;
};

export type OfficialAb01BrokerSeamResult = OfficialAgentLoopTurnEvidence & {
  gateway: Ab01BrokerGateway;
  marksGoalDone: false;
};

/**
 * 装配 Gateway executeTool 后跑官方 AgentLoop 两轮 read_file。
 */
export async function runOfficialAb01BrokerReadFileTurn(
  input: OfficialAb01BrokerSeamInput,
): Promise<OfficialAb01BrokerSeamResult> {
  const gateway = createAb01BrokerExecuteTool(input.broker);
  const evidence = await runOfficialAgentLoopLiveReadFileTurn({
    checkoutDir: input.checkoutDir,
    chat: input.chat,
    executeTool: gateway.executeTool,
    userPrompt: input.userPrompt,
    expectedPath: input.expectedPath,
    sessionId: input.sessionId,
    idleTimeoutMs: input.idleTimeoutMs,
    provider: input.provider,
    modelId: input.modelId,
    modelInvocationLedger: input.modelInvocationLedger,
  });
  return {
    ...evidence,
    gateway,
    marksGoalDone: false,
  };
}
