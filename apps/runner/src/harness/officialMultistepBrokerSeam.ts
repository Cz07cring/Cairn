/**
 * 官方 AgentLoop scripted 多工具 × Broker Gateway 缝。
 * write_file → run_tests；Runner 不 dispatch；scriptedOrder；≠ Goal DONE。
 */
import {
  createAb01BrokerExecuteTool,
  type Ab01BrokerGateway,
} from './ab01BrokerGateway.js';
import type {BrokerBackedHarnessToolConfig} from './brokerBackedHarnessTool.js';
import {
  runOfficialAgentLoopWriteThenRunTestsTurn,
  type OfficialAgentLoopMultistepEvidence,
} from './officialAgentLoopHost.js';

export type OfficialMultistepBrokerSeamInput = {
  checkoutDir?: string;
  broker: BrokerBackedHarnessToolConfig;
  userPrompt: string;
  writePath: string;
  writeContent: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  /** 若提供，scripted 末工具 seal_candidate */
  sealVerificationProfileIds?: string[];
  /** 若提供：read→红测→write→绿测[→seal] */
  diagnoseReadPath?: string;
};

export type OfficialMultistepBrokerSeamResult = OfficialAgentLoopMultistepEvidence & {
  gateway: Ab01BrokerGateway;
  marksGoalDone: false;
};

/**
 * Gateway executeTool + 官方 Loop scripted write→run_tests[→seal]
 * 或诊断全周期 read→run→write→run[→seal]。
 */
export async function runOfficialMultistepBrokerWriteRunTests(
  input: OfficialMultistepBrokerSeamInput,
): Promise<OfficialMultistepBrokerSeamResult> {
  const gateway = createAb01BrokerExecuteTool(input.broker);
  const evidence = await runOfficialAgentLoopWriteThenRunTestsTurn({
    checkoutDir: input.checkoutDir,
    executeTool: gateway.executeTool,
    userPrompt: input.userPrompt,
    writePath: input.writePath,
    writeContent: input.writeContent,
    sessionId: input.sessionId,
    idleTimeoutMs: input.idleTimeoutMs,
    sealVerificationProfileIds: input.sealVerificationProfileIds,
    diagnoseReadPath: input.diagnoseReadPath,
  });
  return {
    ...evidence,
    gateway,
    marksGoalDone: false,
  };
}
