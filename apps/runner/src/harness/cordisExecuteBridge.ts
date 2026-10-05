/**
 * EXECUTE：Cordis/模型 tool-call → Broker 宿主（不本地读盘）。
 * PLAN 路径禁止调用本模块；零工具 PLAN 仍走 cordisLlmBridge。
 */
import {authorizeToolProposal} from '../roles.js';
import {
  bootPinnedCordis,
  type CordisContext,
  type KernelLlmChunk,
  type KernelLlmService,
  type KernelLlmStreamOptions,
} from './cordisBootGate.js';
import type {
  KernelLlmWatchdogOpts,
  KernelModelInvocationPorts,
} from './cordisLlmBridge.js';
import {invokeKernelLlmStreamWithWatchdog} from './cordisLlmBridge.js';
import type {ActivationWatchdog} from './activationWatchdog.js';
import {
  argsDigestOfCanonicalPayload,
  type GuardVerdict,
  type NoProgressGuard,
} from './noProgressGuard.js';
import {
  forceStopVerdictFromClosedGate,
  persistForceStopSummary,
} from './forceStopSummaryArtifact.js';
import {
  bindingDigestOf,
  type ActivityLeaseView,
  type LeaseIdentity,
} from './fakePlanHost.js';
import {
  dispatchExecuteToolProposal,
  type ExecuteToolHost,
} from './executeToolHost.js';
import {applyHeartbeatStopControl} from './heartbeatStopHandler.js';
import {resolveLeaseHeartbeatIntervalMs} from './leaseTiming.js';
import {
  runToolResultSecondTurn,
  type ToolResultRoundEvidence,
  type ToolResultRoundOpts,
} from './executeToolResultRound.js';
import type {StopSeamPorts} from './stopSeam.js';
import {
  createTurnUserMessageGate,
  type TurnUserMessage,
  type TurnUserMessageGate,
} from './turnUserMessageGate.js';

export type ExecuteToolCallResolution = {
  artifactId: string;
  purpose: string;
};

const KERNEL_PROVIDER = 'ring-kernel';
/** E2E-2 受控工具集；PLAN 仍须空工具。 */
export const DEFAULT_EXECUTE_TOOLS = [
  {name: 'read_file'},
  {name: 'write_file'},
  {name: 'run_tests'},
  {name: 'git_diff'},
  {name: 'seal_candidate'},
] as const;

export const DEFAULT_EXECUTE_TOOL_NAMES: ReadonlySet<string> = new Set(
  DEFAULT_EXECUTE_TOOLS.map((t) => t.name),
);

/**
 * EXECUTE 专用 llm：允许 tools，不拒绝 tool-call chunk。
 * 禁止用于 PLAN Context（调用方须保证 activity.kind=EXECUTE）。
 */
export function registerKernelLlmOnContextForExecute(
  ctx: CordisContext,
  ports: KernelModelInvocationPorts,
  lease: LeaseIdentity,
  contextDigest: string,
  opts?: KernelLlmWatchdogOpts,
): () => void {
  const service: KernelLlmService = {
    implementation: 'ring-kernel-bridge',
    async *stream(options: KernelLlmStreamOptions): AsyncIterable<KernelLlmChunk> {
      const tools = options.tools?.map((t) => t.name) ?? [];
      for (const name of tools) {
        authorizeToolProposal('EXECUTE', name, new Set(tools));
      }
      yield* invokeKernelLlmStreamWithWatchdog(
        ports,
        {
          lease,
          contextDigest,
          providerRef: options.provider || KERNEL_PROVIDER,
          modelId: options.model,
          messages: options.messages,
          toolsExposedToModel: tools,
        },
        opts,
      );
    },
  };
  return ctx.provide('llm', service);
}

/**
 * 将模型产出的 tool-call chunk 逐条交给 Broker。
 * arguments 进入内容寻址工件；同名工具不同 arguments → 不同 artifact。
 * 无 tool-call → 空结果（诚实，不虚构 effect）。
 * PLAN activity → UNEXPECTED_ACTIVITY_KIND。
 */
export type ResolveExecuteInputArtifact = (
  tool: string,
  args: string | undefined,
) => Promise<ExecuteToolCallResolution>;

/** Cordis ForceStop：尽量落零工具总结 Artifact，再抛/跳过；≠ DONE */
async function cordisForceStopPersist(
  host: ExecuteToolHost,
  claimed: ActivityLeaseView,
  verdict: Extract<GuardVerdict, {action: 'force_stop'}>,
  context: {tool: string; argsDigest?: string; callId?: string},
): Promise<string | undefined> {
  try {
    const persisted = await persistForceStopSummary({
      artifacts: host.artifacts,
      projectId: claimed.activity.project_id,
      lease: claimed.lease,
      verdict,
      context,
    });
    return persisted.artifactId;
  } catch {
    return undefined;
  }
}

function cordisForceStopError(
  verdict: Extract<GuardVerdict, {action: 'force_stop'}>,
  summaryId?: string,
): Error {
  const suffix = summaryId ? `:summary_artifact=${summaryId}` : '';
  if (verdict.closeToolAdmission) {
    return new Error(
      `TOOL_ADMISSION_CLOSED:${verdict.code}:${verdict.reason}${suffix}`,
    );
  }
  return new Error(
    `NO_PROGRESS_REPEAT_BLOCKED:${verdict.code}:${verdict.reason}${suffix}`,
  );
}

export type CordisDispatchedEffect = {
  tool: string;
  effectId: string;
  dispatchStatus: string;
  logicalStepId: string;
};

/**
 * post-dispatch 硬 ForceStop：携带已 dispatch 的 effect，供 FAILED 透出 effect_ids；≠ DONE。
 */
export class CordisHardForceStopError extends Error {
  readonly marksGoalDone = false as const;
  readonly effects: readonly CordisDispatchedEffect[];
  readonly summaryArtifactId?: string;

  constructor(
    message: string,
    effects: readonly CordisDispatchedEffect[],
    summaryArtifactId?: string,
  ) {
    super(message);
    this.name = 'CordisHardForceStopError';
    this.effects = effects;
    if (summaryArtifactId) {
      this.summaryArtifactId = summaryArtifactId;
    }
  }
}

export function isCordisHardForceStopError(
  err: unknown,
): err is CordisHardForceStopError {
  return err instanceof CordisHardForceStopError;
}

/** 闸门已关：No-Progress 硬停则补落总结再抛；其它原因原样失败关闭。 */
async function cordisThrowIfAdmissionClosed(
  host: ExecuteToolHost,
  claimed: ActivityLeaseView,
  progressGuard?: NoProgressGuard,
  context: {tool?: string; callId?: string} = {},
): Promise<void> {
  if (host.gate.allowed()) {
    return;
  }
  const closed = host.gate.closedReason();
  const verdict = forceStopVerdictFromClosedGate(
    closed,
    progressGuard?.lastVerdict(),
  );
  if (verdict) {
    const summaryId = await cordisForceStopPersist(host, claimed, verdict, {
      tool: context.tool ?? 'unknown',
      callId: context.callId,
    });
    throw cordisForceStopError(verdict, summaryId);
  }
  throw new Error(
    `TOOL_ADMISSION_CLOSED:${closed ?? 'admission_closed'}`,
  );
}

export async function forwardExecuteToolCallsFromChunks(
  host: ExecuteToolHost,
  claimed: ActivityLeaseView,
  chunks: readonly KernelLlmChunk[],
  resolveInputArtifact: ResolveExecuteInputArtifact,
  registeredTools: ReadonlySet<string> = DEFAULT_EXECUTE_TOOL_NAMES,
  progressGuard?: NoProgressGuard,
): Promise<
  Array<{tool: string; effectId: string; dispatchStatus: string; logicalStepId: string}>
> {
  if (claimed.activity.kind !== 'EXECUTE') {
    throw new Error('UNEXPECTED_ACTIVITY_KIND');
  }
  await cordisThrowIfAdmissionClosed(host, claimed, progressGuard);

  const calls = chunks.filter(
    (c): c is Extract<KernelLlmChunk, {type: 'tool-call'}> => c.type === 'tool-call',
  );
  const out: Array<{
    tool: string;
    effectId: string;
    dispatchStatus: string;
    logicalStepId: string;
  }> = [];

  for (const call of calls) {
    await cordisThrowIfAdmissionClosed(host, claimed, progressGuard, {
      tool: call.name,
      callId: call.name,
    });
    authorizeToolProposal('EXECUTE', call.name, registeredTools);
    const args = call.arguments;
    if (args === undefined || !String(args).trim()) {
      throw new Error('EXECUTE_TOOL_ARGUMENTS_REQUIRED');
    }

    let argsDigest: string;
    try {
      const canonical = canonicalizeToolArgumentsPayload(call.name, String(args));
      argsDigest = argsDigestOfCanonicalPayload(canonical);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'validation_failed';
      const verdict = progressGuard?.observe({
        callId: call.name,
        tool: call.name,
        argsDigest: argsDigestOfCanonicalPayload(String(args)),
        outcome: 'VALIDATION_REJECTED',
        signals: [{kind: 'validation_rejected', value: message}],
      });
      if (verdict?.action === 'force_stop') {
        const summaryId = await cordisForceStopPersist(host, claimed, verdict, {
          tool: call.name,
          argsDigest: argsDigestOfCanonicalPayload(String(args)),
          callId: call.name,
        });
        throw cordisForceStopError(verdict, summaryId);
      }
      throw err;
    }

    const admit = progressGuard?.beforeAdmit({
      tool: call.name,
      argsDigest,
    });
    if (admit?.action === 'force_stop') {
      const summaryId = await cordisForceStopPersist(host, claimed, admit, {
        tool: call.name,
        argsDigest,
        callId: call.name,
      });
      if (admit.closeToolAdmission) {
        throw cordisForceStopError(admit, summaryId);
      }
      // 软停：已落总结 Artifact；跳过本条同参，继续后续 tool-call
      continue;
    }

    const resolved = await resolveInputArtifact(call.name, String(args));
    if (!resolved.artifactId?.trim()) {
      throw new Error('EXECUTE_INPUT_ARTIFACT_REQUIRED');
    }
    const forwarded = await dispatchExecuteToolProposal(
      host,
      {
        kind: 'EXECUTE',
        tool: call.name,
        purpose: resolved.purpose || `tool:${call.name}`,
        inputArtifactId: resolved.artifactId,
        activityId: claimed.activity.id,
        lease: claimed.lease,
      },
      registeredTools,
    );
    out.push({
      tool: call.name,
      effectId: forwarded.effectId,
      dispatchStatus: forwarded.dispatchStatus,
      logicalStepId: forwarded.logicalStepId,
    });

    // dispatch 缝尚无 evidence；同参重复 DISPATCHED 仍可被 Guard 判无进展
    const verdict = progressGuard?.observe({
      callId: call.name,
      tool: call.name,
      argsDigest,
      outcome: 'DISPATCHED',
      signals: [],
    });
    if (verdict?.action === 'force_stop') {
      const summaryId = await cordisForceStopPersist(host, claimed, verdict, {
        tool: call.name,
        argsDigest,
        callId: call.name,
      });
      if (verdict.closeToolAdmission) {
        // 硬停：不得再 ACTIVATION_SUBMITTED 冒充推进；携带已 dispatch effects
        throw new CordisHardForceStopError(
          cordisForceStopError(verdict, summaryId).message,
          out,
          summaryId,
        );
      }
      // 软停：已禁同参并落总结；继续后续不同参 tool-call
      continue;
    }
  }
  return out;
}

export type ExecuteToolTurnResult = {
  status: 'succeeded';
  effects: Array<{
    tool: string;
    effectId: string;
    dispatchStatus: string;
    logicalStepId: string;
  }>;
  /** AB08：本回合 Turn id */
  turnId: string;
  /** seal 前挂上的迟到用户消息（本回合不自动回灌模型；证据用） */
  attachedInjected: readonly TurnUserMessage[];
  /** seal 后迟到消息的新 Turn 种子（本回合不跑） */
  deferredTurn: {
    turnId: string;
    messages: readonly TurnUserMessage[];
  } | null;
  /** 可选：观察终态 + Harness chat 第二轮（≠ 官方 AgentLoop） */
  toolResultRound: ToolResultRoundEvidence | null;
  marksGoalDone: false;
};

export type ExecuteTurnAb08Opts = {
  messageGate?: TurnUserMessageGate;
  turnId?: string;
  /** 工具转发完成后、seal 前 */
  onBetweenToolsAndSeal?: (gate: TurnUserMessageGate) => Promise<void>;
  /** seal 后（测 deferred） */
  onAfterSeal?: (gate: TurnUserMessageGate) => Promise<void>;
};

export type ExecuteTurnToolResultRoundOpts = ToolResultRoundOpts & {
  /** 首轮用户提示（写入第二轮 prior） */
  userPrompt: string;
};

/**
 * 已收集的模型 chunk → Broker；成功只表示已 dispatch，≠ effect SUCCEEDED ≠ Goal DONE。
 * AB08：工具转发后 seal Turn；attached/deferred 只作证据，不冒充 Goal DONE。
 * 可选 toolResultRound：观察终态 + Harness chat 第二轮（≠ 官方 AgentLoop）。
 */
export async function runExecuteToolTurnFromChunks(
  host: ExecuteToolHost,
  claimed: ActivityLeaseView,
  chunks: readonly KernelLlmChunk[],
  resolveInputArtifact: ResolveExecuteInputArtifact,
  registeredTools?: ReadonlySet<string>,
  progressGuard?: NoProgressGuard,
  ab08?: ExecuteTurnAb08Opts,
  toolResultRound?: ExecuteTurnToolResultRoundOpts,
): Promise<ExecuteToolTurnResult> {
  const effects = await forwardExecuteToolCallsFromChunks(
    host,
    claimed,
    chunks,
    resolveInputArtifact,
    registeredTools,
    progressGuard,
  );

  const turnId =
    ab08?.turnId?.trim() || ab08?.messageGate?.turnId || 'execute-turn';
  const gate =
    ab08?.messageGate ?? createTurnUserMessageGate({turnId});
  if (ab08?.onBetweenToolsAndSeal) {
    await ab08.onBetweenToolsAndSeal(gate);
  }
  const sealed = await gate.seal();
  if (sealed.marksGoalDone !== false) {
    throw new Error('AB08_SEAL_MARKED_GOAL_DONE');
  }
  if (ab08?.onAfterSeal) {
    await ab08.onAfterSeal(gate);
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

  let round: ToolResultRoundEvidence | null = null;
  if (toolResultRound && effects.length > 0) {
    round = await runToolResultSecondTurn({
      host,
      chunks,
      effects,
      userPrompt: toolResultRound.userPrompt,
      opts: toolResultRound,
    });
    if (round.marksGoalDone !== false) {
      throw new Error('TOOL_RESULT_ROUND_MARKED_GOAL_DONE');
    }
  }

  return {
    status: 'succeeded',
    effects,
    turnId: gate.turnId,
    attachedInjected: sealed.attached,
    deferredTurn: deferred,
    toolResultRound: round,
    marksGoalDone: false,
  };
}

/** Cordis EXECUTE 全回合所需 Kernel 端口（不含 PlanCreate）。 */
export type CordisExecuteTurnPorts = {
  compileContext: (input: {
    activityId: string;
    lease: LeaseIdentity;
    maxInputTokens?: number;
  }) => Promise<{id: string; content_digest: string; content: Record<string, unknown>}>;
  bindContext: (input: {
    activityId: string;
    lease: LeaseIdentity;
    bindingDigest: string;
    contextBundleId: string;
  }) => Promise<{context_digest: string}>;
  heartbeat?: (input: {
    activityId: string;
    lease: LeaseIdentity;
    renewalSeq: number;
  }) => Promise<{
    renewal_seq: number;
    control?: string;
    pending_stop_ids?: string[];
  }>;
  modelInvocation: KernelModelInvocationPorts;
  toolAdmissionGate?: {
    onHeartbeatFailure: (err: unknown) => void;
  };
  /** 可选：heartbeat STOP 时交诚实 StopReceipt */
  stopSeam?: StopSeamPorts;
  /** 阶段化 Watchdog；Hard 关准入，≠ Goal DONE */
  watchdog?: ActivationWatchdog;
  watchdogTickEveryMs?: number;
  /** 无进展守卫；ForceStop ≠ DONE */
  progressGuard?: NoProgressGuard;
  /** live 用户提示；缺省仅传 context_digest。 */
  liveExecutePrompt?: (contextDigest: string) => string;
};

/**
 * 将模型 tool-call.arguments 规范为 Broker/Kernel 可消费的输入工件字节。
 * 非法 JSON / 非对象 / 缺必填 / 未知字段均失败关闭。
 */
export const MAX_TOOL_ARGUMENTS_CHARS = 10_000;
/** write_file 内容可能更大；仍远低于 Kernel max_argument_bytes。 */
export const MAX_WRITE_FILE_ARGUMENTS_CHARS = 100_000;

/**
 * 与 Kernel `*_SCHEMA_DIGEST` 逐字节对齐（tool_capability_manifest）。
 * 漂移时 prepare 会 TOOL_SCHEMA_DRIFT；改 schema 须双端同批更新。
 */
export const READ_FILE_SCHEMA_DIGEST =
  'sha256:fe84c056d57a34dde9e7f1e1aacf5ee9cf724dc231ec24f8acfc29b8cabce3dc' as const;
export const WRITE_FILE_SCHEMA_DIGEST =
  'sha256:272f84ac85d08ad4042963a4e3230020739783d91995055d6177cdd369b9d79c' as const;
export const RUN_TESTS_SCHEMA_DIGEST =
  'sha256:251b221b0d7d047526e742aa1a9309332aad392ca165cf5f247d4d175c49ac2f' as const;
export const GIT_DIFF_SCHEMA_DIGEST =
  'sha256:99334726611ccf58a148b0814696bfa6fe08c1b2d027e946beccf5a74331c9aa' as const;
export const SEAL_CANDIDATE_SCHEMA_DIGEST =
  'sha256:bafe187b904280f2d2231bac9641732af54f6dc8e902991ee8d0156a23efe2e7' as const;

function parseToolArgsObject(
  args: string,
  opts?: {maxChars?: number},
): Record<string, unknown> {
  const maxChars = opts?.maxChars ?? MAX_TOOL_ARGUMENTS_CHARS;
  if (args.length > maxChars) {
    throw new Error('EXECUTE_TOOL_ARGUMENTS_TOO_LONG');
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(args);
  } catch {
    throw new Error('EXECUTE_TOOL_ARGUMENTS_INVALID_JSON');
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('EXECUTE_TOOL_ARGUMENTS_NOT_OBJECT');
  }
  return parsed as Record<string, unknown>;
}

function unwrapToolParameters(
  obj: Record<string, unknown>,
  toolRef: string,
  schemaDigest: string,
): Record<string, unknown> {
  if (
    Object.hasOwn(obj, 'tool_ref') ||
    Object.hasOwn(obj, 'tool_schema_digest') ||
    Object.hasOwn(obj, 'parameters')
  ) {
    if (obj.tool_ref !== toolRef) {
      throw new Error('EXECUTE_TOOL_ARGUMENTS_TOOL_REF_MISMATCH');
    }
    if (obj.tool_schema_digest !== schemaDigest) {
      throw new Error('EXECUTE_TOOL_ARGUMENTS_SCHEMA_DRIFT');
    }
    const params = obj.parameters;
    if (params === null || typeof params !== 'object' || Array.isArray(params)) {
      throw new Error('EXECUTE_TOOL_ARGUMENTS_NOT_OBJECT');
    }
    return params as Record<string, unknown>;
  }
  return obj;
}

function assertExactKeys(
  obj: Record<string, unknown>,
  required: readonly string[],
): void {
  const keys = Object.keys(obj);
  const requiredSet = new Set(required);
  for (const key of required) {
    if (!Object.hasOwn(obj, key)) {
      throw new Error(
        `EXECUTE_TOOL_ARGUMENTS_MISSING_${key.toUpperCase()}: 需要字段 ${required.join(', ')}；实得 keys=[${keys.join(',')}]`,
      );
    }
  }
  const unknown = keys.filter((key) => !requiredSet.has(key));
  if (unknown.length > 0) {
    throw new Error(
      `EXECUTE_TOOL_ARGUMENTS_UNKNOWN_FIELDS: 多余字段 [${unknown.join(',')}]; 仅允许 ${required.join(', ')}`,
    );
  }
}

export function canonicalizeReadFileToolPayload(args: string): string {
  const obj = unwrapToolParameters(
    parseToolArgsObject(args),
    'read_file',
    READ_FILE_SCHEMA_DIGEST,
  );
  assertExactKeys(obj, ['path']);
  const path = obj.path;
  if (typeof path !== 'string' || !path.trim()) {
    throw new Error(
      'EXECUTE_TOOL_ARGUMENTS_EMPTY_PATH: path 须为非空相对路径；期望 {"path":"<相对路径>"}，禁止 path=""',
    );
  }
  return JSON.stringify({
    tool_ref: 'read_file',
    tool_schema_digest: READ_FILE_SCHEMA_DIGEST,
    parameters: {path},
  });
}

export function canonicalizeWriteFileToolPayload(args: string): string {
  const obj = unwrapToolParameters(
    parseToolArgsObject(args, {maxChars: MAX_WRITE_FILE_ARGUMENTS_CHARS}),
    'write_file',
    WRITE_FILE_SCHEMA_DIGEST,
  );
  assertExactKeys(obj, ['path', 'content']);
  const path = obj.path;
  const content = obj.content;
  if (typeof path !== 'string' || !path.trim()) {
    throw new Error(
      'EXECUTE_TOOL_ARGUMENTS_EMPTY_PATH: path 须为非空相对路径；期望 {"path":"<相对路径>","content":"<全文>"}，禁止 path="" 或省略 path',
    );
  }
  if (typeof content !== 'string') {
    throw new Error('EXECUTE_TOOL_ARGUMENTS_INVALID_CONTENT');
  }
  return JSON.stringify({
    tool_ref: 'write_file',
    tool_schema_digest: WRITE_FILE_SCHEMA_DIGEST,
    parameters: {path, content},
  });
}

export function canonicalizeRunTestsToolPayload(args: string): string {
  const obj = unwrapToolParameters(
    parseToolArgsObject(args),
    'run_tests',
    RUN_TESTS_SCHEMA_DIGEST,
  );
  assertExactKeys(obj, ['suite']);
  const suite = obj.suite;
  if (suite !== 'public') {
    throw new Error('EXECUTE_TOOL_ARGUMENTS_INVALID_SUITE');
  }
  return JSON.stringify({
    tool_ref: 'run_tests',
    tool_schema_digest: RUN_TESTS_SCHEMA_DIGEST,
    parameters: {suite: 'public'},
  });
}

export function canonicalizeGitDiffToolPayload(args: string): string {
  const obj = unwrapToolParameters(
    parseToolArgsObject(args),
    'git_diff',
    GIT_DIFF_SCHEMA_DIGEST,
  );
  assertExactKeys(obj, []);
  return JSON.stringify({
    tool_ref: 'git_diff',
    tool_schema_digest: GIT_DIFF_SCHEMA_DIGEST,
    parameters: {},
  });
}

export function canonicalizeSealCandidateToolPayload(args: string): string {
  const obj = unwrapToolParameters(
    parseToolArgsObject(args),
    'seal_candidate',
    SEAL_CANDIDATE_SCHEMA_DIGEST,
  );
  assertExactKeys(obj, ['verification_profile_ids']);
  const ids = obj.verification_profile_ids;
  if (!Array.isArray(ids) || ids.length < 1) {
    throw new Error('EXECUTE_TOOL_ARGUMENTS_INVALID_PROFILE_IDS');
  }
  for (const id of ids) {
    if (typeof id !== 'string' || !id.trim()) {
      throw new Error('EXECUTE_TOOL_ARGUMENTS_INVALID_PROFILE_IDS');
    }
  }
  return JSON.stringify({
    tool_ref: 'seal_candidate',
    tool_schema_digest: SEAL_CANDIDATE_SCHEMA_DIGEST,
    parameters: {verification_profile_ids: ids},
  });
}

export function canonicalizeToolArgumentsPayload(tool: string, args: string): string {
  switch (tool) {
    case 'read_file':
      return canonicalizeReadFileToolPayload(args);
    case 'write_file':
      return canonicalizeWriteFileToolPayload(args);
    case 'run_tests':
      return canonicalizeRunTestsToolPayload(args);
    case 'git_diff':
      return canonicalizeGitDiffToolPayload(args);
    case 'seal_candidate':
      return canonicalizeSealCandidateToolPayload(args);
    default:
      throw new Error(`EXECUTE_TOOL_SCHEMA_UNKNOWN:${tool}`);
  }
}

/**
 * 默认物化工具输入工件：字节 = Broker 可消费的参数对象（如 {"path":"a.ts"}）。
 * 不同 arguments → 不同 digest；校验失败不 PUT、不 createStep。
 */
export function defaultResolveExecuteInputArtifact(
  host: ExecuteToolHost,
  claimed: ActivityLeaseView,
): ResolveExecuteInputArtifact {
  return async (tool, args) => {
    // 无 arguments 时无法产出 Broker 可消费的参数对象：失败关闭，不 PUT、不 createStep
    if (args === undefined) {
      throw new Error(`EXECUTE_TOOL_ARGUMENTS_MISSING:${tool}`);
    }
    const body = canonicalizeToolArgumentsPayload(tool, args);
    const put = await host.artifacts.putCollectorContent({
      projectId: claimed.activity.project_id,
      lease: claimed.lease,
      body,
      mime: 'application/json',
    });
    return {artifactId: put.artifactId, purpose: `tool:${tool}`};
  };
}

/**
 * 已持有 EXECUTE lease：boot Cordis → 挂 EXECUTE llm → 暴露工具 stream → Broker。
 * 成功 = 回合完成且 tool-call 已 dispatch（可为空）；≠ effect 终态 ≠ Goal DONE。
 * 不提交 ExecuteSuccessOutcome（需 candidate seal + 无未决 effect）。
 */
export async function runExecuteToolTurnViaCordisWithLease(
  ports: CordisExecuteTurnPorts,
  host: ExecuteToolHost,
  claimed: ActivityLeaseView,
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
  modelId: string = 'plan-fixture',
  opts?: {
    registeredTools?: ReadonlySet<string>;
    resolveInputArtifact?: ResolveExecuteInputArtifact;
    ab08?: ExecuteTurnAb08Opts;
    /** 观察终态 + Harness chat 第二轮；缺省不做（保持单轮 DISPATCHED） */
    toolResultRound?: ExecuteTurnToolResultRoundOpts;
  },
): Promise<ExecuteToolTurnResult> {
  if (claimed.activity.kind !== 'EXECUTE') {
    throw new Error('UNEXPECTED_ACTIVITY_KIND');
  }

  const registeredTools =
    opts?.registeredTools ?? new Set(DEFAULT_EXECUTE_TOOLS.map((t) => t.name));
  const resolveInputArtifact =
    opts?.resolveInputArtifact ?? defaultResolveExecuteInputArtifact(host, claimed);

  let renewalSeq = 1;
  let heartbeatTimer: ReturnType<typeof setInterval> | undefined;
  const stopHeartbeat = (): void => {
    if (heartbeatTimer !== undefined) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = undefined;
    }
  };
  if (ports.heartbeat) {
    const beat = ports.heartbeat;
    const gate = ports.toolAdmissionGate ?? host.gate;
    const wd = ports.watchdog;
    // 与 Control RING_HEARTBEAT_SECONDS / LEASE_TTL 对齐（doc/08）；默认 30s=TTL/3
    const hbInterval =
      resolveLeaseHeartbeatIntervalMs(process.env, {
        defaultIntervalMs: 30_000,
      }).intervalMs ?? 30_000;
    heartbeatTimer = setInterval(() => {
      // Soft/Hard 本地 tick；Hard 关闸。details 尚未入 HeartbeatRequest 合同。
      const tick = wd?.tick();
      if (tick?.kind === 'hard_idle') {
        gate?.onHeartbeatFailure(
          new Error(
            `WATCHDOG_HARD_IDLE:${tick.phase}:epoch=${tick.phaseEpoch}`,
          ),
        );
        return;
      }
      void beat({
        activityId: claimed.activity.id,
        lease: claimed.lease,
        renewalSeq,
      })
        .then(async (r) => {
          renewalSeq = r.renewal_seq + 1;
          await applyHeartbeatStopControl({
            control: r.control,
            pendingStopIds: r.pending_stop_ids,
            gate: gate ?? undefined,
            stopSeam: ports.stopSeam,
            activationId: claimed.activity.id,
            attemptId: claimed.lease.attempt_id,
          });
        })
        .catch((err: unknown) => {
          gate?.onHeartbeatFailure(err);
        });
    }, hbInterval);
    heartbeatTimer.unref?.();
  }

  ports.watchdog?.enterPhase('setup', 'runner_setup');
  ports.watchdog?.reportProgress('runner_setup');

  try {
    const boot = await bootPinnedCordis(checkoutDir);
    const compiled = await ports.compileContext({
      activityId: claimed.activity.id,
      lease: claimed.lease,
    });
    if (compiled.content.role !== 'EXECUTOR') {
      throw new Error('CONTEXT_ROLE_MISMATCH');
    }
    const bound = await ports.bindContext({
      activityId: claimed.activity.id,
      lease: claimed.lease,
      bindingDigest: bindingDigestOf(claimed.activity.binding),
      contextBundleId: compiled.id,
    });

    const dispose = registerKernelLlmOnContextForExecute(
      boot.ctx,
      ports.modelInvocation,
      claimed.lease,
      bound.context_digest,
      {
        watchdog: ports.watchdog,
        tickEveryMs: ports.watchdogTickEveryMs,
      },
    );
    const collected: KernelLlmChunk[] = [];
    const userContent =
      modelId !== 'plan-fixture' && ports.liveExecutePrompt
        ? ports.liveExecutePrompt(bound.context_digest)
        : `EXECUTE context_digest=${bound.context_digest}`;
    try {
      const llm = boot.ctx.get('llm') as KernelLlmService | undefined;
      if (!llm || llm.implementation !== 'ring-kernel-bridge') {
        throw new Error('HARNESS_LLM_MISSING: Context 上未挂 ring-kernel-bridge');
      }
      for await (const chunk of llm.stream({
        provider: KERNEL_PROVIDER,
        model: modelId,
        messages: [{role: 'user', content: userContent}],
        tools: [...DEFAULT_EXECUTE_TOOLS],
      })) {
        collected.push(chunk);
      }
      if (!collected.some((c) => c.type === 'finish')) {
        throw new Error('HARNESS_LLM_INCOMPLETE: stream 未结束');
      }
    } finally {
      dispose();
    }

    const roundOpts = opts?.toolResultRound
      ? {...opts.toolResultRound, userPrompt: opts.toolResultRound.userPrompt || userContent}
      : undefined;

    return runExecuteToolTurnFromChunks(
      host,
      claimed,
      collected,
      resolveInputArtifact,
      registeredTools,
      ports.progressGuard,
      opts?.ab08,
      roundOpts,
    );
  } finally {
    stopHeartbeat();
  }
}
