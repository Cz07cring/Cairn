/**
 * 官方 AgentLoop chat-driven 多工具 × Broker Gateway 缝。
 * write_file → run_tests；无预推 LlmChunk 脚本；scriptedOrder=false；≠ Goal DONE。
 */
import {
  createAb01BrokerExecuteTool,
  type Ab01BrokerGateway,
} from './ab01BrokerGateway.js';
import type {BrokerBackedHarnessToolConfig} from './brokerBackedHarnessTool.js';
import {
  runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn,
  type OfficialAgentLoopMultistepEvidence,
} from './officialAgentLoopHost.js';
import type {OpenAiCompatibleToolChatConfig} from './openaiCompatibleToolChat.js';

export type OfficialChatDrivenMultistepBrokerSeamInput = {
  checkoutDir?: string;
  broker: BrokerBackedHarnessToolConfig;
  chat: OpenAiCompatibleToolChatConfig;
  userPrompt: string;
  writePath: string;
  writeContent: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  /** live 测须 false，禁止默认装注入 FSM */
  useInjectedDecisionFsm?: boolean;
};

export type OfficialChatDrivenMultistepBrokerSeamResult =
  OfficialAgentLoopMultistepEvidence & {
    gateway: Ab01BrokerGateway;
    marksGoalDone: false;
  };

/**
 * Gateway executeTool + 官方 Loop chat-driven write→run_tests。
 */
export async function runOfficialChatDrivenMultistepBrokerWriteRunTests(
  input: OfficialChatDrivenMultistepBrokerSeamInput,
): Promise<OfficialChatDrivenMultistepBrokerSeamResult> {
  const gateway = createAb01BrokerExecuteTool(input.broker);
  const evidence = await runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn({
    checkoutDir: input.checkoutDir,
    executeTool: gateway.executeTool,
    userPrompt: input.userPrompt,
    writePath: input.writePath,
    writeContent: input.writeContent,
    sessionId: input.sessionId,
    idleTimeoutMs: input.idleTimeoutMs,
    chat: input.chat,
    useInjectedDecisionFsm: input.useInjectedDecisionFsm,
  });
  return {
    ...evidence,
    gateway,
    marksGoalDone: false,
  };
}
