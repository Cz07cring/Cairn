/**
 * 官方 DeepSeek Harness AgentLoop 宿主入口（AB01 / M2–M3）。
 *
 * - 循环归上游 `dsh-agent-loop`；副作用经注入的 Broker-backed execute（Effect Gateway）。
 * - ≠ RunActivation 默认路径（须显式调用）；marksGoalDone 恒 false；≠ Goal DONE。
 * - 未 build:lib:host → HARNESS_AGENT_LOOP_NOT_BUILT。
 * - 热更新须换旁路 checkout 路径（见 hotSwapHarnessRuntime），禁止 query cache-bust。
 */
import type {HarnessToolCall, HarnessToolResult} from './brokerBackedHarnessTool.js';
import {bootPinnedCordis, type CordisContext} from './cordisBootGate.js';
import {requireHarnessCheckout} from './harnessPlanAdapter.js';
import {
  createOpenAiCompatibleOfficialAdapter,
  type OfficialAdapterWatchdogOpts,
} from './officialAgentLoopOpenAiAdapter.js';
import {
  isOfficialAgentLoopBuilt,
  officialAgentLoopImportUrl,
  type OfficialAgentLoopLibKey,
} from './officialAgentLoopPaths.js';
import type {OpenAiCompatibleToolChatConfig} from './openaiCompatibleToolChat.js';
import {chatCompletionMockResponse} from './openaiCompatibleToolChat.js';
import {resolveHarnessPin} from './pin.js';
import {
  successfulToolNames,
  trailEntrySucceeded,
  trailHasGreenRunTestsAfterWrite,
  trailHasSuccessfulTool,
} from './sealGreenGate.js';
import {formatAcceptanceCriteriaBlock} from './goalSealProfiles.js';
import type {ActivationWatchdog} from './activationWatchdog.js';
import {
  createTurnUserMessageGate,
  type TurnUserMessage,
  type TurnUserMessageGate,
} from './turnUserMessageGate.js';
import {
  maybeRunHardForceStopCloseout,
  type ForceStopZeroToolCloseoutResult,
} from './forceStopZeroToolCloseout.js';
import type {ModelInvocationLedgerPorts} from './modelInvocationLedger.js';
import {
  buildDiagnoseResumeCoachText,
  countSuccessfulWritesSinceLastRunTests,
  diagnosePhaseRejectReason,
  diagnoseResumeMaxRounds,
  lastFailedWriteSummary,
} from './diagnoseResumeCoach.js';

/** AB08：官方 Loop 回合结束后的用户消息门禁（≠ Cordis 专用）。 */
export type OfficialLoopAb08Opts = {
  messageGate?: TurnUserMessageGate;
  turnId?: string;
  /** 工具轮次结束后、seal 前 */
  onBetweenToolsAndSeal?: (gate: TurnUserMessageGate) => Promise<void>;
  /** seal 后（测 deferred） */
  onAfterSeal?: (gate: TurnUserMessageGate) => Promise<void>;
};

export type OfficialAgentLoopExecuteTool = (
  call: HarnessToolCall,
) => Promise<HarnessToolResult>;

export type OfficialAgentHandle = {
  followup: (message: unknown) => void;
  session: {snapshotEvents: () => ReadonlyArray<{type: string; data?: unknown}>};
};

export type OfficialAgentLoopHost = {
  ctx: CordisContext & {
    plugin: (plugin: unknown, config?: unknown) => Promise<unknown>;
    llm: {registerAdapter: (providers: string[], adapter: unknown) => void};
    tools: {register: (definition: unknown) => () => void};
    agentLoop: {
      create: (
        sessionId: unknown,
        options: {provider: string; model: string},
      ) => Promise<OfficialAgentHandle>;
    };
    on: (
      event: string,
      listener: (payload: {agent: OfficialAgentHandle; status: string}) => void,
    ) => () => void;
  };
  pin: string;
  disposeTool: () => void;
  marksGoalDone: false;
};

export type OfficialAgentLoopTurnEvidence = {
  toolCallId: string;
  toolName: string;
  toolArguments: string;
  effectId: string;
  effectStatus: string;
  toolResultText: string;
  toolIsError: boolean;
  modelRounds: number;
  round2CitesToolResult: boolean;
  assistantTexts: string[];
  pin: string;
  driver:
    | 'official-dsh-agent-loop'
    | 'official-dsh-agent-loop+openai-compatible';
  marksGoalDone: false;
};

/** 官方 Loop 多工具证据；scriptedOrder=true 表示脚本预定；chatDriven 表示经 OpenAI 兼容轮次按消息状态决策。 */
export type OfficialAgentLoopMultistepEvidence = {
  trail: Array<{
    toolName: string;
    toolCallId: string;
    toolArguments: string;
    effectId: string;
    effectStatus: string;
    toolResultText: string;
    toolIsError: boolean;
  }>;
  modelRounds: number;
  laterRoundsCitePriorToolResults: boolean;
  assistantTexts: string[];
  pin: string;
  driver:
    | 'official-dsh-agent-loop'
    | 'official-dsh-agent-loop+openai-compatible';
  marksGoalDone: false;
  /**
   * true：LlmAdapter 预推 script 块（≠模型决策）。
   * false：经 chat/completions 轮次，按已回灌消息决定下一工具。
   */
  scriptedOrder: boolean;
  /** 诚实：注入/真实 chat 驱动，仍可能非生产模型 */
  chatDrivenOrder?: boolean;
  /** AB08：本回合 Turn id（未接门禁时可为 undefined） */
  turnId?: string;
  /** AB08：seal 前挂上的用户消息（证据；不冒充回灌完成） */
  attachedInjected?: readonly TurnUserMessage[];
  /** AB08：seal 后迟到消息的新 Turn 种子 */
  deferredTurn?: {
    turnId: string;
    messages: readonly TurnUserMessage[];
  } | null;
  /**
   * AB06：硬 ForceStop 后零工具 LLM 收口证据（若发生）；≠ DONE。
   */
  forceStopCloseout?: ForceStopZeroToolCloseoutResult;
};

type LlmChunk =
  | {type: 'block-start'; index: number; blockType: string}
  | {type: 'text-delta'; index: number; text: string}
  | {type: 'block-end'; index: number; block: unknown}
  | {
      type: 'tool-call-delta';
      index: number;
      id: string;
      name?: string;
      argumentsDelta?: string;
    }
  | {type: 'usage'; usage: {inputTokens: number; outputTokens: number}}
  | {type: 'finish'; reason: {kind: string}};

function textResponse(text: string): LlmChunk[] {
  return [
    {type: 'block-start', index: 0, blockType: 'text'},
    {type: 'text-delta', index: 0, text},
    {type: 'block-end', index: 0, block: {type: 'text', text}},
    {type: 'usage', usage: {inputTokens: 1, outputTokens: text.length}},
    {type: 'finish', reason: {kind: 'stop'}},
  ];
}

function toolCallResponse(
  ToolCallId: (raw: string) => string,
  rawCallId: string,
  name: string,
  args: object,
): LlmChunk[] {
  const callId = ToolCallId(rawCallId);
  const argumentsJson = JSON.stringify(args);
  return [
    {type: 'block-start', index: 0, blockType: 'tool-call'},
    {
      type: 'tool-call-delta',
      index: 0,
      id: callId,
      name,
      argumentsDelta: argumentsJson,
    },
    {
      type: 'block-end',
      index: 0,
      block: {type: 'tool-call', id: callId, name, arguments: argumentsJson},
    },
    {type: 'usage', usage: {inputTokens: 1, outputTokens: 5}},
    {type: 'finish', reason: {kind: 'tool-calls'}},
  ];
}

function toolResultText(result: HarnessToolResult): string {
  return result.content
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('');
}

type DefineTool = (opts: Record<string, unknown>) => unknown;

function registerBrokerTool(
  ctx: OfficialAgentLoopHost['ctx'],
  defineTool: DefineTool,
  executeTool: OfficialAgentLoopExecuteTool,
  spec: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
    buildArguments: (args: Record<string, unknown>) => string;
  },
): () => void {
  return ctx.tools.register(
    defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      output: {
        schema: {type: 'string'},
        render: (_args: unknown, value: unknown) => [
          {type: 'text', text: String(value)},
        ],
      },
      async execute(args: Record<string, unknown>, exec?: {callId?: unknown}) {
        const fromExec = String(exec?.callId ?? '').trim();
        const callId = fromExec || `official-missing-callid-${Date.now()}`;
        const result = await executeTool({
          callId,
          name: spec.name,
          arguments: spec.buildArguments(args ?? {}),
        });
        const text = toolResultText(result);
        if (result.isError) {
          throw new Error(
            (result.error?.message ?? text) || 'BROKER_TOOL_ERROR',
          );
        }
        return JSON.stringify({
          text,
          effectId: result.meta.effectId,
          status: result.meta.status,
          marksGoalDone: false as const,
        });
      },
    }),
  );
}

function registerE2e2Tools(
  ctx: OfficialAgentLoopHost['ctx'],
  defineTool: DefineTool,
  executeTool: OfficialAgentLoopExecuteTool,
  sealVerificationProfileIds?: string[],
  defaults?: {readPath?: string; writePath?: string},
): () => void {
  const sealIds = (sealVerificationProfileIds ?? []).filter((id) =>
    Boolean(id.trim()),
  );
  const defaultRead = (defaults?.readPath || '').trim();
  const defaultWrite = (defaults?.writePath || '').trim() || defaultRead;
  const sealDesc =
    sealIds.length > 0
      ? `从真实 worktree 采集快照并封存候选；verification_profile_ids 须包含合同 profile：${sealIds.join(',')}`
      : '从真实 worktree 采集快照并封存候选';
  const disposers = [
    registerBrokerTool(ctx, defineTool, executeTool, {
      name: 'read_file',
      description:
        '读取工作区内一个文本文件并返回内容' +
        (defaultRead ? `；path 缺省时用 ${defaultRead}` : ''),
      parameters: {
        // required:true → 上游编入 JSON Schema required[]；缺省时用 activation 路径（同 run_tests suite）
        path: {type: 'string', description: '相对路径', required: true},
      },
      buildArguments: (args) => {
        const raw = typeof args.path === 'string' ? args.path.trim() : '';
        const path = raw || defaultRead;
        return JSON.stringify(path ? {path} : {});
      },
    }),
    registerBrokerTool(ctx, defineTool, executeTool, {
      name: 'write_file',
      description:
        '向工作区批准路径写入 UTF-8 文本' +
        (defaultWrite ? `；path 缺省时用 ${defaultWrite}` : ''),
      parameters: {
        path: {type: 'string', description: '相对路径', required: true},
        content: {type: 'string', description: '文件全文', required: true},
      },
      buildArguments: (args) => {
        const raw = typeof args.path === 'string' ? args.path.trim() : '';
        const path = raw || defaultWrite;
        const out: {path?: string; content?: string} = {};
        if (path) out.path = path;
        if (typeof args.content === 'string') out.content = args.content;
        return JSON.stringify(out);
      },
    }),
    registerBrokerTool(ctx, defineTool, executeTool, {
      name: 'run_tests',
      description: '运行批准的测试套件（仅 public）',
      parameters: {
        suite: {type: 'string', description: '套件名，仅 public', required: true},
      },
      buildArguments: (args) =>
        JSON.stringify({
          suite: typeof args.suite === 'string' ? args.suite : 'public',
        }),
    }),
    registerBrokerTool(ctx, defineTool, executeTool, {
      name: 'git_diff',
      description: '查看相对 HEAD 的工作区 status/diff（只读）',
      parameters: {},
      buildArguments: () => '{}',
    }),
    registerBrokerTool(ctx, defineTool, executeTool, {
      name: 'seal_candidate',
      description: sealDesc,
      parameters: {
        verification_profile_ids: {
          type: 'array',
          description:
            sealIds.length > 0
              ? `VerificationProfile UUID 列表；须包含：${sealIds.join(',')}`
              : 'VerificationProfile UUID 列表',
        },
      },
      buildArguments: (args) =>
        JSON.stringify({
          verification_profile_ids: Array.isArray(args.verification_profile_ids)
            ? args.verification_profile_ids
            : [],
        }),
    }),
  ];
  return () => {
    for (const dispose of disposers) dispose();
  };
}

/**
 * 挂载官方 LlmRuntime / Session / ToolRuntime / AgentLoop，并注册 Broker-backed E2E-2 工具。
 */
export async function bootOfficialAgentLoopHost(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  /** Kernel 权威 seal profile；写入工具 schema，≠ USER_PROMPT 粘贴 */
  sealVerificationProfileIds?: string[];
  /** diagnose 缺省 path（模型省略 path 时填入，≠ 静默空串） */
  defaultReadPath?: string;
  defaultWritePath?: string;
}): Promise<OfficialAgentLoopHost> {
  const checkout = input.checkoutDir ?? process.env.RING_HARNESS_CHECKOUT;
  requireHarnessCheckout(checkout);
  const root = checkout as string;
  if (!isOfficialAgentLoopBuilt(root)) {
    throw new Error(
      'HARNESS_AGENT_LOOP_NOT_BUILT: 钉扎 checkout 缺 AgentLoop peers lib；' +
        '请在钉扎 checkout 内执行 pnpm run build:lib:host',
    );
  }

  const imp = (key: OfficialAgentLoopLibKey) =>
    officialAgentLoopImportUrl(root, key);

  const cordisBoot = await bootPinnedCordis(root);
  const ctx = cordisBoot.ctx as OfficialAgentLoopHost['ctx'];
  if (typeof ctx.plugin !== 'function') {
    throw new Error('HARNESS_CORDIS_INVALID: Context 无 plugin()');
  }

  const AgentLoop = (await import(imp('agentLoop'))).default as unknown;
  const LlmRuntime = (await import(imp('llm'))).default as unknown;
  const SessionStore = (await import(imp('session'))).default as unknown;
  const SessionProjectionRegistry = (await import(imp('sessionProjection')))
    .default as unknown;
  const SystemPrompt = (await import(imp('systemPrompt'))).default as unknown;
  const toolsMod = await import(imp('tools'));
  const ToolRuntime = toolsMod.default as unknown;
  const defineTool = toolsMod.defineTool as DefineTool;
  const AgentRegistry = (await import(imp('agent'))).default as unknown;

  await ctx.plugin(LlmRuntime);
  await ctx.plugin(SessionStore);
  await ctx.plugin(SessionProjectionRegistry);
  await ctx.plugin(SystemPrompt, {});
  await ctx.plugin(ToolRuntime);
  await ctx.plugin(AgentRegistry);
  await ctx.plugin(AgentLoop, {agents: []});

  const disposeTool = registerE2e2Tools(
    ctx,
    defineTool,
    input.executeTool,
    input.sealVerificationProfileIds,
    {
      readPath: input.defaultReadPath,
      writePath: input.defaultWritePath,
    },
  );

  return {
    ctx,
    pin: resolveHarnessPin(),
    disposeTool,
    marksGoalDone: false,
  };
}

/**
 * 用脚本化官方 LlmAdapter 跑：tool_call → Broker 工具 → 第二轮。
 * 证明官方 AgentLoop 已真实驱动（非本仓自建两轮 chat）。
 */
export async function runOfficialAgentLoopReadFileTurn(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  userPrompt: string;
  expectedPath: string;
  sessionId?: string;
  idleTimeoutMs?: number;
}): Promise<OfficialAgentLoopTurnEvidence> {
  return runOfficialAgentLoopReadFileTurnWithAdapter({
    ...input,
    mode: 'scripted',
  });
}

/**
 * 官方 AgentLoop + OpenAI 兼容 chat（真实模型或注入 fetch）。
 * 循环归上游；chat 仅作 LlmAdapter 线缆；marksGoalDone 恒 false。
 */
export async function runOfficialAgentLoopLiveReadFileTurn(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  userPrompt: string;
  expectedPath: string;
  chat: OpenAiCompatibleToolChatConfig;
  sessionId?: string;
  idleTimeoutMs?: number;
  provider?: string;
  modelId?: string;
  /** Codex P0-1：官方 chat 轮次登记 ModelInvocation */
  modelInvocationLedger?: ModelInvocationLedgerPorts;
}): Promise<OfficialAgentLoopTurnEvidence> {
  return runOfficialAgentLoopReadFileTurnWithAdapter({
    checkoutDir: input.checkoutDir,
    executeTool: input.executeTool,
    userPrompt: input.userPrompt,
    expectedPath: input.expectedPath,
    sessionId: input.sessionId,
    idleTimeoutMs: input.idleTimeoutMs ?? 180_000,
    mode: 'openai-compatible',
    chat: input.chat,
    provider: input.provider ?? 'openai-compatible',
    modelId: input.modelId ?? input.chat.modelId,
    modelInvocationLedger: input.modelInvocationLedger,
  });
}

async function runOfficialAgentLoopReadFileTurnWithAdapter(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  userPrompt: string;
  expectedPath: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  mode: 'scripted' | 'openai-compatible';
  chat?: OpenAiCompatibleToolChatConfig;
  provider?: string;
  modelId?: string;
  modelInvocationLedger?: ModelInvocationLedgerPorts;
}): Promise<OfficialAgentLoopTurnEvidence> {
  const captured: {
    call?: HarnessToolCall;
    result?: HarnessToolResult;
  } = {};
  const wrapped: OfficialAgentLoopExecuteTool = async (call) => {
    captured.call = call;
    const result = await input.executeTool(call);
    captured.result = result;
    return result;
  };

  const host = await bootOfficialAgentLoopHost({
    checkoutDir: input.checkoutDir,
    executeTool: wrapped,
  });
  const root =
    input.checkoutDir ?? (process.env.RING_HARNESS_CHECKOUT as string);
  const llmMod = await import(officialAgentLoopImportUrl(root, 'llm'));
  // 上游抽象类；运行时动态 import 后子类化
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const LlmAdapterBase = llmMod.LlmAdapter as any;
  const createUserMessage = llmMod.createUserMessage as (body: unknown) => unknown;
  const ToolCallId = llmMod.ToolCallId as (raw: string) => string;
  const sessionMod = await import(
    officialAgentLoopImportUrl(root, 'session')
  );
  const SessionId = sessionMod.SessionId as (raw: string) => unknown;

  const provider = input.provider ?? 'mock';
  const modelId = input.modelId ?? 'mock';

  if (input.mode === 'scripted') {
    class ScriptedAdapter extends LlmAdapterBase {
      requests: unknown[] = [];
      constructor(private readonly script: LlmChunk[][]) {
        super();
      }
      async *stream(options: unknown) {
        this.requests.push(options);
        const next = this.script.shift();
        if (!next) throw new Error('OFFICIAL_LOOP_SCRIPT_EXHAUSTED');
        for (const chunk of next) yield chunk;
      }
    }
    const adapter = new ScriptedAdapter([
      toolCallResponse(ToolCallId, 'call-1', 'read_file', {
        path: input.expectedPath,
      }),
      textResponse(`cited:${input.expectedPath}`),
    ]);
    host.ctx.llm.registerAdapter([provider], adapter);
    const evidence = await finishOfficialTurn({
      host,
      SessionId,
      createUserMessage,
      userPrompt: input.userPrompt,
      sessionId: input.sessionId,
      idleTimeoutMs: input.idleTimeoutMs ?? 20_000,
      provider,
      modelId,
      captured,
      expectedPath: input.expectedPath,
      getModelRounds: () => adapter.requests.length,
      getRound2Probe: () =>
        adapter.requests.length >= 2
          ? JSON.stringify(adapter.requests[1])
          : '',
      driver: 'official-dsh-agent-loop',
    });
    return evidence;
  }

  if (!input.chat) {
    throw new Error('OFFICIAL_LOOP_CHAT_REQUIRED');
  }
  const {adapter, rounds} = createOpenAiCompatibleOfficialAdapter(
    LlmAdapterBase,
    ToolCallId,
    input.chat,
    undefined,
    input.modelInvocationLedger,
  );
  host.ctx.llm.registerAdapter([provider], adapter);
  return finishOfficialTurn({
    host,
    SessionId,
    createUserMessage,
    userPrompt: input.userPrompt,
    sessionId: input.sessionId,
    idleTimeoutMs: input.idleTimeoutMs ?? 180_000,
    provider,
    modelId,
    captured,
    expectedPath: input.expectedPath,
    getModelRounds: () => rounds.length,
    getRound2Probe: () =>
      rounds.length >= 2 ? JSON.stringify(rounds[1]) : '',
    driver: 'official-dsh-agent-loop+openai-compatible',
  });
}

async function finishOfficialTurn(input: {
  host: OfficialAgentLoopHost;
  SessionId: (raw: string) => unknown;
  createUserMessage: (body: unknown) => unknown;
  userPrompt: string;
  sessionId?: string;
  idleTimeoutMs: number;
  provider: string;
  modelId: string;
  captured: {
    call?: HarnessToolCall;
    result?: HarnessToolResult;
  };
  expectedPath: string;
  getModelRounds: () => number;
  getRound2Probe: () => string;
  driver: OfficialAgentLoopTurnEvidence['driver'];
}): Promise<OfficialAgentLoopTurnEvidence> {
  const agent = await input.host.ctx.agentLoop.create(
    input.SessionId(input.sessionId ?? `ring-official-${Date.now()}`),
    {provider: input.provider, model: input.modelId},
  );

  const idle = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error('OFFICIAL_LOOP_IDLE_TIMEOUT')),
      input.idleTimeoutMs,
    );
    const dispose = input.host.ctx.on(
      'agent/status',
      ({agent: subject, status}) => {
        if (subject === agent && status === 'idle') {
          clearTimeout(timer);
          dispose();
          resolve();
        }
      },
    );
  });

  agent.followup(
    input.createUserMessage({
      content: [{type: 'text', text: input.userPrompt}],
      source: {kind: 'user'},
    }),
  );
  await idle;

  const toolText = input.captured.result
    ? toolResultText(input.captured.result)
    : '';
  const assistantTexts: string[] = [];
  for (const ev of agent.session.snapshotEvents()) {
    if (ev.type !== 'assistant/message') continue;
    const data = ev.data as
      | {
          content?: Array<{type: string; text?: string}>;
          message?: {content?: Array<{type: string; text?: string}>};
        }
      | undefined;
    const blocks = data?.message?.content ?? data?.content ?? [];
    for (const block of blocks) {
      if (block.type === 'text' && block.text) assistantTexts.push(block.text);
    }
  }

  if (!input.captured.result || !input.captured.call) {
    throw new Error('OFFICIAL_LOOP_NO_TOOL_EXECUTION: AgentLoop 未调用 read_file');
  }

  const modelRounds = input.getModelRounds();
  const probe = `${input.getRound2Probe()}\n${assistantTexts.join('\n')}`;
  const toolLines = toolText
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length >= 8);
  return {
    toolCallId: input.captured.call.callId,
    toolName: input.captured.call.name,
    toolArguments: input.captured.call.arguments,
    effectId: input.captured.result.meta.effectId,
    effectStatus: input.captured.result.meta.status,
    toolResultText: toolText,
    toolIsError: input.captured.result.isError,
    modelRounds,
    round2CitesToolResult:
      modelRounds >= 2 &&
      (probe.includes(input.expectedPath) ||
        (toolText.length > 8 && probe.includes(toolText.slice(0, 16))) ||
        toolLines.some((line) => probe.includes(line)) ||
        assistantTexts.some((t) => t.includes(input.expectedPath))),
    assistantTexts,
    pin: resolveHarnessPin(),
    driver: input.driver,
    marksGoalDone: false,
  };
}

export {isOfficialAgentLoopBuilt};

/**
 * 脚本化官方 Loop：write→run_tests[→seal]，或诊断全周期
 * read→run_tests→write→run_tests[→seal]。
 * script 顺序 ≠ 模型自主决策；marksGoalDone 恒 false。
 */
export async function runOfficialAgentLoopWriteThenRunTestsTurn(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  userPrompt: string;
  writePath: string;
  writeContent: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  /** 若提供，scripted 末工具为 seal_candidate（仍 ≠ Goal DONE） */
  sealVerificationProfileIds?: string[];
  /**
   * 若提供：scripted 为 read→红测→write→绿测[→seal]
   *（研究案例最低多步序列；仍 scriptedOrder）
   */
  diagnoseReadPath?: string;
}): Promise<OfficialAgentLoopMultistepEvidence> {
  const trail: OfficialAgentLoopMultistepEvidence['trail'] = [];
  const executeErrors: string[] = [];
  const sealIds = (input.sealVerificationProfileIds ?? []).filter((id) =>
    Boolean(id.trim()),
  );
  const expectSeal = sealIds.length > 0;
  const diagnose = Boolean((input.diagnoseReadPath || '').trim());
  const readPath = (input.diagnoseReadPath || '').trim();
  const expectedNames = diagnose
    ? expectSeal
      ? (['read_file', 'run_tests', 'write_file', 'run_tests', 'seal_candidate'] as const)
      : (['read_file', 'run_tests', 'write_file', 'run_tests'] as const)
    : expectSeal
      ? (['write_file', 'run_tests', 'seal_candidate'] as const)
      : (['write_file', 'run_tests'] as const);
  const minTools = expectedNames.length;
  const wrapped: OfficialAgentLoopExecuteTool = async (call) => {
    try {
      const result = await input.executeTool(call);
      trail.push({
        toolName: call.name,
        toolCallId: call.callId,
        toolArguments: call.arguments,
        effectId: result.meta.effectId,
        effectStatus: result.meta.status,
        toolResultText: toolResultText(result),
        toolIsError: result.isError,
      });
      return result;
    } catch (err) {
      executeErrors.push(
        `${call.name}:${err instanceof Error ? err.message : String(err)}`,
      );
      throw err;
    }
  };

  const host = await bootOfficialAgentLoopHost({
    checkoutDir: input.checkoutDir,
    executeTool: wrapped,
    sealVerificationProfileIds: sealIds,
    defaultReadPath: readPath || input.writePath,
    defaultWritePath: input.writePath,
  });
  const root =
    input.checkoutDir ?? (process.env.RING_HARNESS_CHECKOUT as string);
  const llmMod = await import(officialAgentLoopImportUrl(root, 'llm'));
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const LlmAdapterBase = llmMod.LlmAdapter as any;
  const createUserMessage = llmMod.createUserMessage as (body: unknown) => unknown;
  const ToolCallId = llmMod.ToolCallId as (raw: string) => string;
  const sessionMod = await import(
    officialAgentLoopImportUrl(root, 'session')
  );
  const SessionId = sessionMod.SessionId as (raw: string) => unknown;

  class ScriptedAdapter extends LlmAdapterBase {
    requests: unknown[] = [];
    constructor(private readonly script: LlmChunk[][]) {
      super();
    }
    async *stream(options: unknown) {
      this.requests.push(options);
      const next = this.script.shift();
      if (!next) throw new Error('OFFICIAL_LOOP_SCRIPT_EXHAUSTED');
      for (const chunk of next) yield chunk;
    }
  }

  const writeMarker = 'WRITE_OK_MARKER';
  const sealMarker = 'SEAL_OK_MARKER';
  const readMarker = 'READ_OK_MARKER';
  const script: LlmChunk[][] = [];
  if (diagnose) {
    script.push(
      toolCallResponse(ToolCallId, 'call-read', 'read_file', {path: readPath}),
    );
    script.push(
      toolCallResponse(ToolCallId, 'call-tests-red', 'run_tests', {
        suite: 'public',
      }),
    );
  }
  script.push(
    toolCallResponse(ToolCallId, 'call-write', 'write_file', {
      path: input.writePath,
      content: input.writeContent,
    }),
  );
  script.push(
    toolCallResponse(ToolCallId, 'call-tests-green', 'run_tests', {
      suite: 'public',
    }),
  );
  if (expectSeal) {
    script.push(
      toolCallResponse(ToolCallId, 'call-seal', 'seal_candidate', {
        verification_profile_ids: sealIds,
      }),
    );
    script.push(
      textResponse(
        diagnose
          ? `done:${readMarker}:${writeMarker}:suite=public:${sealMarker}`
          : `done:${writeMarker}:suite=public:${sealMarker}`,
      ),
    );
  } else {
    script.push(
      textResponse(
        diagnose
          ? `done:${readMarker}:${writeMarker}:suite=public`
          : `done:${writeMarker}:suite=public`,
      ),
    );
  }
  const adapter = new ScriptedAdapter(script);
  const provider = 'mock';
  const modelId = diagnose
    ? 'mock-diagnose-cycle'
    : expectSeal
      ? 'mock-multistep-seal'
      : 'mock-multistep';
  host.ctx.llm.registerAdapter([provider], adapter);

  const agent = await host.ctx.agentLoop.create(
    SessionId(input.sessionId ?? `ring-official-ms-${Date.now()}`),
    {provider, model: modelId},
  );
  const idleTimeoutMs = input.idleTimeoutMs ?? 25_000;
  const idle = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error('OFFICIAL_LOOP_IDLE_TIMEOUT')),
      idleTimeoutMs,
    );
    const dispose = host.ctx.on(
      'agent/status',
      ({agent: subject, status}) => {
        if (subject === agent && status === 'idle') {
          clearTimeout(timer);
          dispose();
          resolve();
        }
      },
    );
  });
  agent.followup(
    createUserMessage({
      content: [{type: 'text', text: input.userPrompt}],
      source: {kind: 'user'},
    }),
  );
  await idle;

  const assistantTexts: string[] = [];
  for (const ev of agent.session.snapshotEvents()) {
    if (ev.type !== 'assistant/message') continue;
    const data = ev.data as
      | {
          content?: Array<{type: string; text?: string}>;
          message?: {content?: Array<{type: string; text?: string}>};
        }
      | undefined;
    const blocks = data?.message?.content ?? data?.content ?? [];
    for (const block of blocks) {
      if (block.type === 'text' && block.text) assistantTexts.push(block.text);
    }
  }

  if (trail.length < minTools) {
    const eventTypes = agent.session
      .snapshotEvents()
      .map((ev) => ev.type)
      .slice(0, 40);
    throw new Error(
      `OFFICIAL_LOOP_MULTISTEP_SHORT: 期望 ≥${minTools} 工具调用，实际 ${trail.length};` +
        ` modelRounds=${adapter.requests.length};` +
        ` executeErrors=${executeErrors.join('|') || '(none)'};` +
        ` events=${eventTypes.join(',') || '(none)'};` +
        ` writeBytes=${input.writeContent.length}`,
    );
  }
  const gotNames = trail.slice(0, minTools).map((t) => t.toolName);
  if (gotNames.join(',') !== expectedNames.join(',')) {
    throw new Error(
      `OFFICIAL_LOOP_MULTISTEP_ORDER: want=${expectedNames.join(',')} got=${trail
        .map((t) => t.toolName)
        .join(',')}`,
    );
  }

  const modelRounds = adapter.requests.length;
  const minRounds = minTools + 1;
  const laterProbe = [
    ...adapter.requests.slice(1).map((r) => JSON.stringify(r)),
    ...assistantTexts,
  ].join('\n');
  const citeSources = trail.map((t) => t.toolResultText ?? '');
  const laterRoundsCitePriorToolResults =
    modelRounds >= minRounds &&
    (citeSources.some(
      (text) => text.length > 4 && laterProbe.includes(text.slice(0, 8)),
    ) ||
      laterProbe.includes(input.writePath) ||
      laterProbe.includes(writeMarker) ||
      laterProbe.includes('suite=public') ||
      (diagnose && laterProbe.includes(readPath)) ||
      (diagnose && laterProbe.includes(readMarker)) ||
      (expectSeal && laterProbe.includes(sealMarker)) ||
      assistantTexts.some(
        (t) =>
          t.includes(writeMarker) ||
          (diagnose && t.includes(readMarker)) ||
          (expectSeal && t.includes(sealMarker)),
      ));

  host.disposeTool();
  return {
    trail,
    modelRounds,
    laterRoundsCitePriorToolResults,
    assistantTexts,
    pin: resolveHarnessPin(),
    driver: 'official-dsh-agent-loop',
    marksGoalDone: false,
    scriptedOrder: true,
  };
}

/**
 * 注入 chat：按已回灌 messages 决定下一工具（非预推 LlmChunk 脚本）。
 * 仍 ≠ live 模型自主；仅证 AgentLoop ToolResult→下一轮 chat→下一工具。
 */
export function createChatDrivenWriteThenRunTestsFetch(input: {
  writePath: string;
  writeContent: string;
  writeMarker?: string;
}): typeof fetch {
  const writeMarker = input.writeMarker ?? 'WRITE_OK_MARKER';
  return async (_url, init) => {
    const body = JSON.parse(String(init?.body ?? '{}')) as {
      messages?: Array<Record<string, unknown>>;
    };
    const messages = body.messages ?? [];
    const called = new Set<string>();
    for (const msg of messages) {
      if (msg.role !== 'assistant') continue;
      const toolCalls = msg.tool_calls as
        | Array<{function?: {name?: string}}>
        | undefined;
      for (const tc of toolCalls ?? []) {
        const name = tc.function?.name;
        if (name) called.add(name);
      }
    }
    const toolMsgs = messages.filter((m) => m.role === 'tool');

    if (!called.has('write_file')) {
      return chatCompletionMockResponse(init, {
        choices: [
          {
            finish_reason: 'tool_calls',
            message: {
              content: null,
              tool_calls: [
                {
                  id: 'call_cd_write',
                  type: 'function',
                  function: {
                    name: 'write_file',
                    arguments: JSON.stringify({
                      path: input.writePath,
                      content: input.writeContent,
                    }),
                  },
                },
              ],
            },
          },
        ],
      });
    }

    // 须见 write 的 ToolResult 回灌后再发 run_tests（禁止无结果抢跑）
    if (!called.has('run_tests')) {
      if (toolMsgs.length < 1) {
        throw new Error(
          'CHAT_DRIVEN_PENDING_TOOL_RESULT: write_file 已记录但尚无 role=tool 回灌',
        );
      }
      return chatCompletionMockResponse(init, {
        choices: [
          {
            finish_reason: 'tool_calls',
            message: {
              content: null,
              tool_calls: [
                {
                  id: 'call_cd_tests',
                  type: 'function',
                  function: {
                    name: 'run_tests',
                    arguments: JSON.stringify({suite: 'public'}),
                  },
                },
              ],
            },
          },
        ],
      });
    }

    // 终轮只回写 tool 消息里真实出现的片段，禁止自种 marker 冒充引用
    const toolBlob = toolMsgs
      .map((m) => String(m.content ?? ''))
      .join('\n');
    const snippets: string[] = [];
    if (toolBlob.includes(writeMarker)) snippets.push(writeMarker);
    if (toolBlob.includes(input.writePath)) snippets.push(input.writePath);
    if (toolBlob.includes('suite=public')) snippets.push('suite=public');
    if (toolBlob.includes('exit_code=0')) snippets.push('exit_code=0');
    for (const m of toolMsgs) {
      const c = String(m.content ?? '');
      const testsHit = c.match(/TESTS_[A-Za-z0-9_]+/);
      if (testsHit) snippets.push(testsHit[0]!);
    }
    if (snippets.length === 0) {
      throw new Error(
        'CHAT_DRIVEN_NO_TOOL_CITE: 终轮 messages 的 tool 内容无可引用片段',
      );
    }
    return chatCompletionMockResponse(init, {
      choices: [
        {
          finish_reason: 'stop',
          message: {
            content: `cited:${snippets.join('|')}`,
          },
        },
      ],
    });
  };
}

/**
 * 经 OpenAI 兼容 chat 驱动的官方 Loop 多工具：write→run_tests。
 * 与 scripted 区别：无预推 LlmChunk 脚本；下一工具经 chat 轮次产生。
 * scriptedOrder=false；marksGoalDone 恒 false。
 *
 * useInjectedDecisionFsm（默认 true）：无 fetchImpl 时装宿主 messages 状态机（≠ 模型自主）。
 * live 测须 `useInjectedDecisionFsm: false` 且 chat 指向真实后端。
 */
export async function runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  userPrompt: string;
  writePath: string;
  writeContent: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  chat: OpenAiCompatibleToolChatConfig;
  provider?: string;
  modelId?: string;
  /** 缺省 true。live 模型测必须 false，否则会装注入 FSM。 */
  useInjectedDecisionFsm?: boolean;
}): Promise<OfficialAgentLoopMultistepEvidence> {
  const trail: OfficialAgentLoopMultistepEvidence['trail'] = [];
  const executeErrors: string[] = [];
  const writeMarker = 'WRITE_OK_MARKER';
  const wrapped: OfficialAgentLoopExecuteTool = async (call) => {
    try {
      const result = await input.executeTool(call);
      trail.push({
        toolName: call.name,
        toolCallId: call.callId,
        toolArguments: call.arguments,
        effectId: result.meta.effectId,
        effectStatus: result.meta.status,
        toolResultText: toolResultText(result),
        toolIsError: result.isError,
      });
      return result;
    } catch (err) {
      executeErrors.push(
        `${call.name}:${err instanceof Error ? err.message : String(err)}`,
      );
      throw err;
    }
  };

  const host = await bootOfficialAgentLoopHost({
    checkoutDir: input.checkoutDir,
    executeTool: wrapped,
  });
  const root =
    input.checkoutDir ?? (process.env.RING_HARNESS_CHECKOUT as string);
  const llmMod = await import(officialAgentLoopImportUrl(root, 'llm'));
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const LlmAdapterBase = llmMod.LlmAdapter as any;
  const createUserMessage = llmMod.createUserMessage as (body: unknown) => unknown;
  const ToolCallId = llmMod.ToolCallId as (raw: string) => string;
  const sessionMod = await import(
    officialAgentLoopImportUrl(root, 'session')
  );
  const SessionId = sessionMod.SessionId as (raw: string) => unknown;

  const provider = input.provider ?? 'openai-compatible';
  const modelId = input.modelId ?? input.chat.modelId ?? 'chat-driven-multistep';
  const useFsm = input.useInjectedDecisionFsm ?? true;
  const chat: OpenAiCompatibleToolChatConfig = {
    ...input.chat,
    fetchImpl:
      input.chat.fetchImpl ??
      (useFsm
        ? createChatDrivenWriteThenRunTestsFetch({
            writePath: input.writePath,
            writeContent: input.writeContent,
            writeMarker,
          })
        : undefined),
  };
  const {adapter, rounds, requestMessages} = createOpenAiCompatibleOfficialAdapter(
    LlmAdapterBase,
    ToolCallId,
    chat,
  );
  host.ctx.llm.registerAdapter([provider], adapter);

  const agent = await host.ctx.agentLoop.create(
    SessionId(input.sessionId ?? `ring-official-cd-${Date.now()}`),
    {provider, model: modelId},
  );
  const idleTimeoutMs = input.idleTimeoutMs ?? 40_000;
  const idle = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error('OFFICIAL_LOOP_IDLE_TIMEOUT')),
      idleTimeoutMs,
    );
    const dispose = host.ctx.on(
      'agent/status',
      ({agent: subject, status}) => {
        if (subject === agent && status === 'idle') {
          clearTimeout(timer);
          dispose();
          resolve();
        }
      },
    );
  });
  agent.followup(
    createUserMessage({
      content: [{type: 'text', text: input.userPrompt}],
      source: {kind: 'user'},
    }),
  );
  await idle;

  const assistantTexts: string[] = [];
  for (const ev of agent.session.snapshotEvents()) {
    if (ev.type !== 'assistant/message') continue;
    const data = ev.data as
      | {
          content?: Array<{type: string; text?: string}>;
          message?: {content?: Array<{type: string; text?: string}>};
        }
      | undefined;
    const blocks = data?.message?.content ?? data?.content ?? [];
    for (const block of blocks) {
      if (block.type === 'text' && block.text) assistantTexts.push(block.text);
    }
  }

  if (trail.length < 2) {
    throw new Error(
      `OFFICIAL_LOOP_CHAT_DRIVEN_SHORT: 期望 ≥2 工具，实际 ${trail.length};` +
        ` modelRounds=${rounds.length};` +
        ` executeErrors=${executeErrors.join('|') || '(none)'}`,
    );
  }
  const names = trail.map((t) => t.toolName);
  if (!names.includes('write_file') || !names.includes('run_tests')) {
    throw new Error(
      `OFFICIAL_LOOP_CHAT_DRIVEN_TOOLS: want write_file+run_tests got=${names.join(',')}`,
    );
  }
  // 决策须基于 ToolResult 回灌：run_tests 不得先于 write_file
  if (names.indexOf('write_file') > names.indexOf('run_tests')) {
    throw new Error(
      `OFFICIAL_LOOP_CHAT_DRIVEN_ORDER: write_file must precede run_tests; got=${names.join(',')}`,
    );
  }

  const modelRounds = rounds.length;
  // 证明力：后轮「请求」messages 含先轮 ToolResult 文本（非终轮自种 marker）
  const laterRequestProbe = requestMessages
    .slice(1)
    .map((msgs) => JSON.stringify(msgs))
    .join('\n');
  const citeSources = trail
    .map((t) => t.toolResultText ?? '')
    .filter((t) => t.length > 4);
  const laterRoundsCitePriorToolResults =
    modelRounds >= 3 &&
    citeSources.some(
      (text) =>
        laterRequestProbe.includes(text.slice(0, Math.min(24, text.length))) ||
        laterRequestProbe.includes(text.slice(0, 8)),
    );

  host.disposeTool();
  return {
    trail,
    modelRounds,
    laterRoundsCitePriorToolResults,
    assistantTexts,
    pin: resolveHarnessPin(),
    driver: 'official-dsh-agent-loop+openai-compatible',
    marksGoalDone: false,
    scriptedOrder: false,
    chatDrivenOrder: true,
  };
}

function assistantToolCallNames(
  messages: Array<Record<string, unknown>>,
): string[] {
  const names: string[] = [];
  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;
    const toolCalls = msg.tool_calls as
      | Array<{function?: {name?: string}}>
      | undefined;
    for (const tc of toolCalls ?? []) {
      const name = tc.function?.name;
      if (name) names.push(name);
    }
  }
  return names;
}

/**
 * 注入 chat：诊断全周期 read→run_tests→write→run_tests[→seal_candidate]。
 * ≠ live 模型自主；≠ Goal DONE。
 */
export function createChatDrivenDiagnoseCycleFetch(input: {
  readPath: string;
  writePath: string;
  writeContent: string;
  readMarker?: string;
  writeMarker?: string;
  sealMarker?: string;
  /** 非空则在绿测后追加 seal_candidate */
  sealVerificationProfileIds?: string[];
}): typeof fetch {
  const readMarker = input.readMarker ?? 'READ_BUG_MARKER';
  const writeMarker = input.writeMarker ?? 'WRITE_OK_MARKER';
  const sealMarker = input.sealMarker ?? 'SEAL_OK_MARKER';
  const sealIds = (input.sealVerificationProfileIds ?? []).filter((id) =>
    Boolean(id.trim()),
  );
  const expectSeal = sealIds.length > 0;
  return async (_url, init) => {
    const body = JSON.parse(String(init?.body ?? '{}')) as {
      messages?: Array<Record<string, unknown>>;
    };
    const messages = body.messages ?? [];
    const called = assistantToolCallNames(messages);
    const toolMsgs = messages.filter((m) => m.role === 'tool');
    const runTestsCount = called.filter((n) => n === 'run_tests').length;
    const hasRead = called.includes('read_file');
    const hasWrite = called.includes('write_file');
    const hasSeal = called.includes('seal_candidate');

    const toolCall = (id: string, name: string, args: Record<string, unknown>) =>
      chatCompletionMockResponse(init, {
        choices: [
          {
            finish_reason: 'tool_calls',
            message: {
              content: null,
              tool_calls: [
                {
                  id,
                  type: 'function',
                  function: {
                    name,
                    arguments: JSON.stringify(args),
                  },
                },
              ],
            },
          },
        ],
      });

    if (!hasRead) {
      return toolCall('call_cd_diag_read', 'read_file', {path: input.readPath});
    }
    if (runTestsCount === 0) {
      if (toolMsgs.length < 1) {
        throw new Error(
          'CHAT_DRIVEN_DIAG_PENDING: read_file 后缺 role=tool 回灌',
        );
      }
      return toolCall('call_cd_diag_tests1', 'run_tests', {suite: 'public'});
    }
    if (!hasWrite) {
      if (toolMsgs.length < 2) {
        throw new Error(
          'CHAT_DRIVEN_DIAG_PENDING: 首轮 run_tests 后缺 role=tool 回灌',
        );
      }
      return toolCall('call_cd_diag_write', 'write_file', {
        path: input.writePath,
        content: input.writeContent,
      });
    }
    if (runTestsCount === 1) {
      if (toolMsgs.length < 3) {
        throw new Error(
          'CHAT_DRIVEN_DIAG_PENDING: write_file 后缺 role=tool 回灌',
        );
      }
      return toolCall('call_cd_diag_tests2', 'run_tests', {suite: 'public'});
    }
    if (expectSeal && !hasSeal) {
      if (toolMsgs.length < 4) {
        throw new Error(
          'CHAT_DRIVEN_DIAG_PENDING: 绿测 run_tests 后缺 role=tool 回灌',
        );
      }
      return toolCall('call_cd_diag_seal', 'seal_candidate', {
        verification_profile_ids: sealIds,
      });
    }

    const toolBlob = toolMsgs.map((m) => String(m.content ?? '')).join('\n');
    const snippets: string[] = [];
    if (toolBlob.includes(readMarker)) snippets.push(readMarker);
    if (toolBlob.includes(writeMarker)) snippets.push(writeMarker);
    if (toolBlob.includes('exit_code=0')) snippets.push('exit_code=0');
    if (toolBlob.includes('suite=public')) snippets.push('suite=public');
    if (expectSeal && toolBlob.includes(sealMarker)) snippets.push(sealMarker);
    if (snippets.length === 0) {
      throw new Error('CHAT_DRIVEN_DIAG_NO_CITE: tool 内容无可引用片段');
    }
    return chatCompletionMockResponse(init, {
      choices: [
        {
          finish_reason: 'stop',
          message: {content: `cited:${snippets.join('|')}`},
        },
      ],
    });
  };
}

/**
 * chat-driven 诊断全周期：read→run_tests→write→run_tests[→seal]。
 * scriptedOrder=false；默认注入 FSM；live 须 useInjectedDecisionFsm=false。
 */
export async function runOfficialAgentLoopChatDrivenDiagnoseCycleTurn(input: {
  checkoutDir?: string;
  executeTool: OfficialAgentLoopExecuteTool;
  userPrompt: string;
  readPath: string;
  writePath: string;
  writeContent: string;
  sessionId?: string;
  idleTimeoutMs?: number;
  chat: OpenAiCompatibleToolChatConfig;
  provider?: string;
  modelId?: string;
  useInjectedDecisionFsm?: boolean;
  /** 若提供，末工具 seal_candidate（仍 ≠ Goal DONE） */
  sealVerificationProfileIds?: string[];
  /** Task acceptance 描述；续跑红测/催绿时注入，≠ 修复正文 */
  acceptanceDescriptions?: string[];
  /** AB08：官方 Loop idle 后 seal Turn；attached/deferred 只作证据 */
  ab08?: OfficialLoopAb08Opts;
  /**
   * AB05：与 Broker 工具共享的 ActivationWatchdog。
   * 注入后官方 chat await 挂 awaiting_llm；缺省不启用（兼容旧测）。
   */
  watchdog?: ActivationWatchdog;
  watchdogTick?: Omit<OfficialAdapterWatchdogOpts, 'watchdog'>;
  /** AB06：拉取 ForceStop 总结 Artifact 正文（可选） */
  getForceStopSummaryContent?: (artifactId: string) => Promise<string>;
  /** AB06：落盘零工具收口 Artifact（可选） */
  putForceStopCloseout?: (body: string) => Promise<{artifactId: string}>;
  /** Codex P0-1：官方 chat 轮次登记 ModelInvocation；缺省不登记（单测/FSM） */
  modelInvocationLedger?: ModelInvocationLedgerPorts;
}): Promise<OfficialAgentLoopMultistepEvidence> {
  const trail: OfficialAgentLoopMultistepEvidence['trail'] = [];
  const executeErrors: string[] = [];
  const sealIds = (input.sealVerificationProfileIds ?? []).filter((id) =>
    Boolean(id.trim()),
  );
  const acceptanceBlock = formatAcceptanceCriteriaBlock(
    input.acceptanceDescriptions ?? [],
  );
  const expected = (
    sealIds.length > 0
      ? (['read_file', 'run_tests', 'write_file', 'run_tests', 'seal_candidate'] as const)
      : (['read_file', 'run_tests', 'write_file', 'run_tests'] as const)
  );
  const liveChat = !(input.useInjectedDecisionFsm ?? true);
  const wrapped: OfficialAgentLoopExecuteTool = async (call) => {
    // live 诊断：写后阶段硬拒绝读循环（coach 不够时仍可自愈）；FSM scripted 不启用
    if (liveChat && sealIds.length > 0) {
      const hasSuccessfulWrite = trailHasSuccessfulTool(trail, 'write_file');
      const preWriteExploreCount = hasSuccessfulWrite
        ? 0
        : trail.filter(
            (t) =>
              (t.toolName === 'read_file' || t.toolName === 'git_diff') &&
              trailEntrySucceeded(t),
          ).length;
      const reject = diagnosePhaseRejectReason({
        toolName: call.name,
        hasWrite: hasSuccessfulWrite,
        greenAfterWrite: trailHasGreenRunTestsAfterWrite(trail),
        hasSeal: trailHasSuccessfulTool(trail, 'seal_candidate'),
        hasSealStep: true,
        preWriteExploreCount,
        sawSuccessfulRunTests: trailHasSuccessfulTool(trail, 'run_tests'),
        writesSinceLastRunTests:
          countSuccessfulWritesSinceLastRunTests(trail),
      });
      if (reject) {
        const blocked: HarnessToolResult = {
          content: [{type: 'text', text: reject}],
          isError: true,
          error: {
            message: reject,
            info: {name: 'BrokerToolError', code: 'DIAGNOSE_PHASE_FORBIDDEN'},
          },
          meta: {
            effectId: '',
            status: 'VALIDATION_REJECTED',
            evidenceIds: [],
            requiresReconciliation: false,
          },
        };
        trail.push({
          toolName: call.name,
          toolCallId: call.callId,
          toolArguments: call.arguments,
          effectId: '',
          effectStatus: 'VALIDATION_REJECTED',
          toolResultText: reject,
          toolIsError: true,
        });
        return blocked;
      }
    }
    try {
      const result = await input.executeTool(call);
      trail.push({
        toolName: call.name,
        toolCallId: call.callId,
        toolArguments: call.arguments,
        effectId: result.meta.effectId,
        effectStatus: result.meta.status,
        toolResultText: toolResultText(result),
        toolIsError: result.isError,
      });
      return result;
    } catch (err) {
      executeErrors.push(
        `${call.name}:${err instanceof Error ? err.message : String(err)}`,
      );
      throw err;
    }
  };

  const host = await bootOfficialAgentLoopHost({
    checkoutDir: input.checkoutDir,
    executeTool: wrapped,
    sealVerificationProfileIds: sealIds,
    defaultReadPath: input.readPath,
    defaultWritePath: input.writePath,
  });
  const root =
    input.checkoutDir ?? (process.env.RING_HARNESS_CHECKOUT as string);
  const llmMod = await import(officialAgentLoopImportUrl(root, 'llm'));
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const LlmAdapterBase = llmMod.LlmAdapter as any;
  const createUserMessage = llmMod.createUserMessage as (body: unknown) => unknown;
  const ToolCallId = llmMod.ToolCallId as (raw: string) => string;
  const sessionMod = await import(
    officialAgentLoopImportUrl(root, 'session')
  );
  const SessionId = sessionMod.SessionId as (raw: string) => unknown;

  const provider = input.provider ?? 'openai-compatible';
  const modelId = input.modelId ?? input.chat.modelId ?? 'chat-driven-diagnose';
  const useFsm = input.useInjectedDecisionFsm ?? true;
  // live 诊断：DeepSeek thinking 拒收 tool_choice=required / 指定函数，只能 auto。
  const chat: OpenAiCompatibleToolChatConfig = {
    ...input.chat,
    fetchImpl:
      input.chat.fetchImpl ??
      (useFsm
        ? createChatDrivenDiagnoseCycleFetch({
            readPath: input.readPath,
            writePath: input.writePath,
            writeContent: input.writeContent,
            sealVerificationProfileIds: sealIds,
          })
        : undefined),
  };
  const {adapter, rounds, requestMessages, lastStreamError} =
    createOpenAiCompatibleOfficialAdapter(
      LlmAdapterBase,
      ToolCallId,
      chat,
      input.watchdog
        ? {watchdog: input.watchdog, ...input.watchdogTick}
        : undefined,
      input.modelInvocationLedger,
    );
  host.ctx.llm.registerAdapter([provider], adapter);

  const agent = await host.ctx.agentLoop.create(
    SessionId(input.sessionId ?? `ring-official-diag-${Date.now()}`),
    {provider, model: modelId},
  );
  const idleTimeoutMs = input.idleTimeoutMs ?? 60_000;
  let loopError: unknown;
  const idle = new Promise<void>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error('OFFICIAL_LOOP_IDLE_TIMEOUT')),
      idleTimeoutMs,
    );
    const dispose = host.ctx.on(
      'agent/status',
      ({agent: subject, status}) => {
        if (subject === agent && status === 'idle') {
          clearTimeout(timer);
          dispose();
          resolve();
        }
      },
    );
  });
  try {
    agent.followup(
      createUserMessage({
        content: [{type: 'text', text: input.userPrompt}],
        source: {kind: 'user'},
      }),
    );
    await idle;
  } catch (err) {
    loopError = err;
  }
  // Issue #70：账本 create 失败被 AgentLoop 吞掉时，从 adapter 捞回一等错误
  if (!loopError && lastStreamError()) {
    loopError = lastStreamError();
  }

  // live：模型常中途改口述停住；按剩余工具数续跑（仍由模型选工具）
  const waitNextIdleAfterBusy = () =>
    new Promise<void>((resolve, reject) => {
      let sawBusy = false;
      const timer = setTimeout(
        () => reject(new Error('OFFICIAL_LOOP_IDLE_TIMEOUT_RESUME')),
        idleTimeoutMs,
      );
      const dispose = host.ctx.on(
        'agent/status',
        ({agent: subject, status}) => {
          if (subject !== agent) return;
          if (status !== 'idle') {
            sawBusy = true;
            return;
          }
          if (!sawBusy) return;
          clearTimeout(timer);
          dispose();
          resolve();
        },
      );
    });

  if (!useFsm) {
    const covered = (got: string[]) => {
      const need = [...new Set(expected)];
      if (need.some((n) => !got.includes(n))) return false;
      const writeAt = got.indexOf('write_file');
      const sealAt =
        sealIds.length > 0 ? got.indexOf('seal_candidate') : got.length;
      if (writeAt < 0) return false;
      if (sealIds.length > 0 && (sealAt < 0 || writeAt > sealAt)) return false;
      if (!got.slice(writeAt + 1).includes('run_tests')) return false;
      // write 后须有绿测才可视为 cover（含 seal）完成
      if (sealIds.length > 0 && !trailHasGreenRunTestsAfterWrite(trail)) {
        return false;
      }
      // 成功 write 是硬条件（拒答 write 不得冒充覆盖）
      if (!trailHasSuccessfulTool(trail, 'write_file')) return false;
      if (
        sealIds.length > 0 &&
        !trailHasSuccessfulTool(trail, 'seal_candidate')
      ) {
        return false;
      }
      return true;
    };
    const nextMissing = (got: string[]) => {
      for (const want of [...new Set(expected)]) {
        if (!got.includes(want)) {
          // seal 前若已有 write 但无绿测：先追 run_tests，不催 seal
          if (
            want === 'seal_candidate' &&
            trailHasSuccessfulTool(trail, 'write_file') &&
            !trailHasGreenRunTestsAfterWrite(trail)
          ) {
            return 'run_tests';
          }
          return want;
        }
      }
      const writeAt = got.indexOf('write_file');
      if (writeAt >= 0 && !got.slice(writeAt + 1).includes('run_tests')) {
        return 'run_tests';
      }
      if (
        sealIds.length > 0 &&
        trailHasSuccessfulTool(trail, 'write_file') &&
        !trailHasGreenRunTestsAfterWrite(trail)
      ) {
        return 'run_tests';
      }
      if (
        sealIds.length > 0 &&
        !trailHasSuccessfulTool(trail, 'seal_candidate')
      ) {
        return 'seal_candidate';
      }
      return undefined;
    };
    // 真模型可夹杂工具；按覆盖缺口续跑（上限须盖住读循环，见 diagnoseResumeCoach）
    const maxResumes = diagnoseResumeMaxRounds(expected.length);
    for (let resume = 0; resume < maxResumes; resume += 1) {
      // 硬关闸后继续催工具只会产生 TOOL_ADMISSION_CLOSED；立即停
      if (
        executeErrors.some((e) => e.includes('TOOL_ADMISSION_CLOSED')) ||
        trail.some(
          (t) =>
            t.toolIsError &&
            (t.toolResultText || '').includes('禁止再调工具'),
        )
      ) {
        break;
      }
      const got = successfulToolNames(trail);
      if (covered(got)) break;
      const next = nextMissing(got) ?? expected[expected.length - 1]!;
      try {
        const resumeIdle = waitNextIdleAfterBusy();
        agent.followup(
          createUserMessage({
            content: [
              {
                type: 'text',
                text: buildDiagnoseResumeCoachText({
                  next,
                  hasSealStep: sealIds.length > 0,
                  acceptanceBlock: acceptanceBlock || undefined,
                  writePath: input.writePath,
                  writeContentPreview: input.writeContent,
                  lastWriteFailure: lastFailedWriteSummary(trail),
                }),
              },
            ],
            source: {kind: 'user'},
          }),
        );
        await resumeIdle;
        loopError = undefined;
      } catch (err) {
        loopError = err;
        break;
      }
    }
  }

  const assistantTexts: string[] = [];
  for (const ev of agent.session.snapshotEvents()) {
    if (ev.type !== 'assistant/message') continue;
    const data = ev.data as
      | {
          content?: Array<{type: string; text?: string}>;
          message?: {content?: Array<{type: string; text?: string}>};
        }
      | undefined;
    const blocks = data?.message?.content ?? data?.content ?? [];
    for (const block of blocks) {
      if (block.type === 'text' && block.text) assistantTexts.push(block.text);
    }
  }

  const hardForceStopSignals = [
    ...executeErrors,
    ...trail.map((t) => t.toolResultText || ''),
  ];
  const forceStopCloseout = await maybeRunHardForceStopCloseout({
    signalTexts: hardForceStopSignals,
    chat: input.chat,
    getSummaryContent: input.getForceStopSummaryContent,
    putCloseout: input.putForceStopCloseout,
  });

  if (forceStopCloseout) {
    assistantTexts.push(forceStopCloseout.assistantText);

    host.disposeTool();

    const turnIdEarly =
      input.ab08?.turnId?.trim() ||
      input.ab08?.messageGate?.turnId ||
      `official-diag-${Date.now()}`;
    const gateEarly =
      input.ab08?.messageGate ?? createTurnUserMessageGate({turnId: turnIdEarly});
    if (input.ab08?.onBetweenToolsAndSeal) {
      await input.ab08.onBetweenToolsAndSeal(gateEarly);
    }
    const sealedEarly = await gateEarly.seal();
    if (sealedEarly.marksGoalDone !== false) {
      throw new Error('AB08_SEAL_MARKED_GOAL_DONE');
    }
    if (input.ab08?.onAfterSeal) {
      await input.ab08.onAfterSeal(gateEarly);
    }
    const deferredEarly = gateEarly.takeDeferredTurn();
    if (deferredEarly) {
      const deferredIds = new Set(deferredEarly.messages.map((m) => m.messageId));
      for (const m of sealedEarly.attached) {
        if (deferredIds.has(m.messageId)) {
          throw new Error(`AB08_DOUBLE_RUN: ${m.messageId}`);
        }
      }
    }

    return {
      trail,
      modelRounds: rounds.length,
      laterRoundsCitePriorToolResults: false,
      assistantTexts,
      pin: resolveHarnessPin(),
      driver: 'official-dsh-agent-loop+openai-compatible',
      marksGoalDone: false,
      scriptedOrder: false,
      chatDrivenOrder: true,
      turnId: gateEarly.turnId,
      attachedInjected: sealedEarly.attached,
      deferredTurn: deferredEarly,
      forceStopCloseout,
    };
  }

  const names = trail.map((t) => t.toolName);
  // live：0 轮模型调用多半是账本/Profile 拒登或 chat 未接通，禁止伪装成「工具覆盖不足」
  if (!useFsm && rounds.length === 0) {
    const streamDetail =
      loopError instanceof Error
        ? loopError.message
        : loopError != null
          ? String(loopError)
          : '(none)';
    const ledgerHint = streamDetail.includes('MODEL_INVOCATION_LEDGER_')
      ? '（账本登记失败：见 MODEL_INVOCATION_LEDGER_*；常见 Profile/provider 未对齐）'
      : '（常见：ModelProfile 与 RING_LOCAL_QWEN_* 不一致；live 须 RING_TEST_ALLOW_CLOUD=1）';
    throw new Error(
      `OFFICIAL_LOOP_NO_MODEL_ROUNDS:` +
        ` loopError=${streamDetail};` +
        ` executeErrors=${executeErrors.join('|') || '(none)'};` +
        ` assistant=${assistantTexts.join(' | ').slice(0, 200) || '(none)'}` +
        ledgerHint,
    );
  }
  if (useFsm) {
    if (names.join(',') !== expected.join(',')) {
      throw new Error(
        `OFFICIAL_LOOP_CHAT_DIAG_ORDER: want=${expected.join(',')} got=${names.join(',')};` +
          ` modelRounds=${rounds.length};` +
          ` executeErrors=${executeErrors.join('|') || '(none)'};` +
          ` loopError=${loopError instanceof Error ? loopError.message : loopError ?? '(none)'};` +
          ` assistant=${assistantTexts.join(' | ').slice(0, 200) || '(none)'}`,
      );
    }
  } else {
    const namesOk = successfulToolNames(trail);
    const uniqNeed = [...new Set(expected)];
    const miss = uniqNeed.filter((n) => !namesOk.includes(n));
    const writeAt = namesOk.indexOf('write_file');
    const sealAt = sealIds.length > 0 ? namesOk.indexOf('seal_candidate') : namesOk.length;
    const runAfterWrite =
      writeAt >= 0 && namesOk.slice(writeAt + 1).includes('run_tests');
    const hasSuccessfulWrite = writeAt >= 0;
    const greenAfterWrite = hasSuccessfulWrite
      ? trailHasGreenRunTestsAfterWrite(trail)
      : false;
    const writeBeforeSeal =
      sealIds.length === 0 || (writeAt >= 0 && sealAt >= 0 && writeAt < sealAt);
    const lastWriteFail = lastFailedWriteSummary(trail);
    if (
      miss.length > 0 ||
      !runAfterWrite ||
      !writeBeforeSeal ||
      (sealIds.length > 0 && hasSuccessfulWrite && !greenAfterWrite)
    ) {
      throw new Error(
        `OFFICIAL_LOOP_CHAT_DIAG_COVER: missing=${miss.join(',') || '(none)'}` +
          ` runAfterWrite=${runAfterWrite} greenAfterWrite=${greenAfterWrite}` +
          ` writeBeforeSeal=${writeBeforeSeal}` +
          ` wantCover=${uniqNeed.join(',')} got=${namesOk.join(',')}` +
          ` rawGot=${names.join(',')};` +
          (lastWriteFail ? ` lastWriteFail=${lastWriteFail};` : '') +
          ` modelRounds=${rounds.length};` +
          ` executeErrors=${executeErrors.join('|') || '(none)'};` +
          ` loopError=${loopError instanceof Error ? loopError.message : loopError ?? '(none)'};` +
          ` assistant=${assistantTexts.join(' | ').slice(0, 200) || '(none)'}`,
      );
    }
  }

  const modelRounds = rounds.length;
  const laterRequestProbe = requestMessages
    .slice(1)
    .map((msgs) => JSON.stringify(msgs))
    .join('\n');
  const citeSources = trail
    .map((t) => t.toolResultText ?? '')
    .filter((t) => t.length > 4);
  const laterRoundsCitePriorToolResults =
    modelRounds >= (sealIds.length > 0 ? 6 : 5) &&
    citeSources.some(
      (text) =>
        laterRequestProbe.includes(text.slice(0, Math.min(24, text.length))) ||
        laterRequestProbe.includes(text.slice(0, 8)),
    );

  host.disposeTool();

  // AB08：官方 Loop 工具轮次结束后封 Turn（与 Cordis EXECUTE 同语义）
  const turnId =
    input.ab08?.turnId?.trim() ||
    input.ab08?.messageGate?.turnId ||
    `official-diag-${Date.now()}`;
  const gate =
    input.ab08?.messageGate ?? createTurnUserMessageGate({turnId});
  if (input.ab08?.onBetweenToolsAndSeal) {
    await input.ab08.onBetweenToolsAndSeal(gate);
  }
  const sealed = await gate.seal();
  if (sealed.marksGoalDone !== false) {
    throw new Error('AB08_SEAL_MARKED_GOAL_DONE');
  }
  if (input.ab08?.onAfterSeal) {
    await input.ab08.onAfterSeal(gate);
  }
  const deferred = gate.takeDeferredTurn();
  if (deferred) {
    const deferredIds = new Set(deferred.messages.map((m) => m.messageId));
    for (const m of sealed.attached) {
      if (deferredIds.has(m.messageId)) {
        throw new Error(`AB08_DOUBLE_RUN: ${m.messageId}`);
      }
    }
  }

  return {
    trail,
    modelRounds,
    laterRoundsCitePriorToolResults,
    assistantTexts,
    pin: resolveHarnessPin(),
    driver: 'official-dsh-agent-loop+openai-compatible',
    marksGoalDone: false,
    scriptedOrder: false,
    chatDrivenOrder: true,
    turnId: gate.turnId,
    attachedInjected: sealed.attached,
    deferredTurn: deferred,
  };
}
