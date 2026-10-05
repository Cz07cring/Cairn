/**
 * DeepSeek 官方 AgentLoop 的 HarnessRuntime 插件实现。
 * 上游细节关在本文件；换 pin/checkout 不改 Gateway。
 */
import {
  isOfficialAgentLoopBuilt,
  runOfficialAgentLoopReadFileTurn,
} from './officialAgentLoopHost.js';
import {resolveHarnessPin} from './pin.js';
import {
  HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
  type HarnessRuntimeCreateOptions,
  type HarnessRuntimeFactory,
  type HarnessRuntimePlugin,
  type HarnessRuntimeTurnEvidence,
  type HarnessRuntimeTurnInput,
} from './harnessRuntimePlugin.js';

export function createDeepseekOfficialRuntimePlugin(
  options: HarnessRuntimeCreateOptions = {},
): HarnessRuntimePlugin {
  const checkoutDir =
    options.checkoutDir ?? process.env.RING_HARNESS_CHECKOUT;
  const pin = options.pin ?? resolveHarnessPin();
  const generation = options.generation ?? 0;
  let disposed = false;

  const plugin: HarnessRuntimePlugin = {
    manifest: {
      id: HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
      kind: 'deepseek-harness',
      pin,
      checkoutDir,
      generation,
      marksGoalDone: false,
    },
    isReady(): boolean {
      if (disposed) return false;
      if (!checkoutDir) return false;
      return isOfficialAgentLoopBuilt(checkoutDir);
    },
    async runReadFileTurn(
      input: HarnessRuntimeTurnInput,
    ): Promise<HarnessRuntimeTurnEvidence> {
      if (disposed) {
        throw new Error('HARNESS_RUNTIME_DISPOSED');
      }
      if (!checkoutDir) {
        throw new Error('HARNESS_CHECKOUT_REQUIRED');
      }
      const raw = await runOfficialAgentLoopReadFileTurn({
        checkoutDir,
        executeTool: input.executeTool,
        userPrompt: input.userPrompt,
        expectedPath: input.expectedPath,
        sessionId: input.sessionId,
        idleTimeoutMs: input.idleTimeoutMs,
      });
      return {
        runtimeId: HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
        driver: raw.driver,
        pin: raw.pin,
        generation,
        toolCallId: raw.toolCallId,
        toolName: raw.toolName,
        toolArguments: raw.toolArguments,
        effectId: raw.effectId,
        effectStatus: raw.effectStatus,
        toolResultText: raw.toolResultText,
        toolIsError: raw.toolIsError,
        modelRounds: raw.modelRounds,
        round2CitesToolResult: raw.round2CitesToolResult,
        marksGoalDone: false,
      };
    },
    dispose(): void {
      disposed = true;
    },
  };
  return plugin;
}

export const deepseekOfficialRuntimeFactory: HarnessRuntimeFactory = {
  id: HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
  create: createDeepseekOfficialRuntimePlugin,
};
