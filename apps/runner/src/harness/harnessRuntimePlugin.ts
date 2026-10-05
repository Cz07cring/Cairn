/**
 * Harness 运行时插拔合同：Gateway/Runner 只依赖本面，不依赖 DeepSeek 包路径。
 *
 * - 换上游版本：新 checkout + RING_HARNESS_PIN + registry.reload()
 * - 副作用仍经注入的 executeTool（Effect Gateway）；marksGoalDone 恒 false
 */
import type {HarnessToolCall, HarnessToolResult} from './brokerBackedHarnessTool.js';

/** 内建驱动 id；自定义驱动可注册任意非空字符串。 */
export const HARNESS_RUNTIME_DEEPSEEK_OFFICIAL =
  'deepseek-official-agent-loop' as const;

export type HarnessRuntimeId = string;

export type HarnessRuntimeKind = 'deepseek-harness' | 'ring-native';

export type HarnessRuntimeManifest = {
  id: HarnessRuntimeId;
  kind: HarnessRuntimeKind;
  /** 当前实例钉扎的上游 SHA（或 ring-native 哨兵） */
  pin: string;
  checkoutDir?: string;
  /** 热重载代数；每次 reload +1 */
  generation: number;
  marksGoalDone: false;
};

export type HarnessRuntimeExecuteTool = (
  call: HarnessToolCall,
) => Promise<HarnessToolResult>;

export type HarnessRuntimeTurnInput = {
  executeTool: HarnessRuntimeExecuteTool;
  userPrompt: string;
  expectedPath: string;
  sessionId?: string;
  idleTimeoutMs?: number;
};

export type HarnessRuntimeTurnEvidence = {
  runtimeId: HarnessRuntimeId;
  driver: string;
  pin: string;
  generation: number;
  toolCallId: string;
  toolName: string;
  toolArguments: string;
  effectId: string;
  effectStatus: string;
  toolResultText: string;
  toolIsError: boolean;
  modelRounds: number;
  round2CitesToolResult: boolean;
  marksGoalDone: false;
};

export type HarnessRuntimePlugin = {
  manifest: HarnessRuntimeManifest;
  /** 无副作用就绪探测 */
  isReady(): boolean;
  /**
   * 官方/脚本化两轮：read_file → ToolResult → 第二轮。
   * 具体循环实现由插件持有（官方 AgentLoop 或其他）。
   */
  runReadFileTurn(
    input: HarnessRuntimeTurnInput,
  ): Promise<HarnessRuntimeTurnEvidence>;
  /** 释放 Context / 工具注册；热更新前调用 */
  dispose(): void | Promise<void>;
};

export type HarnessRuntimeCreateOptions = {
  checkoutDir?: string;
  pin?: string;
  generation?: number;
};

export type HarnessRuntimeFactory = {
  id: HarnessRuntimeId;
  create(options?: HarnessRuntimeCreateOptions): HarnessRuntimePlugin;
};
