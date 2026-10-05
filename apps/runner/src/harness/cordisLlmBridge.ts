/**
 * 零工具 Cordis llm 桥：真 Context + ring 薄 llm → Kernel ModelInvocation 端口。
 * ≠ 官方 @deepseek-ai/dsh-llm LlmRuntime；≠ 完整 agent loop。
 */
import {createHash} from 'node:crypto';
import {authorizeToolProposal} from '../roles.js';
import {
  awaitWithWatchdog,
  throwIfWatchdogHard,
  WatchdogHardIdleError,
  type ActivationWatchdog,
} from './activationWatchdog.js';
import {
  bootPinnedCordis,
  type CordisContext,
  type KernelLlmChunk,
  type KernelLlmService,
  type KernelLlmStreamOptions,
} from './cordisBootGate.js';
import {applyHeartbeatStopControl} from './heartbeatStopHandler.js';
import {
  bindingDigestOf,
  rejectPlanTools,
  type ActivityLeaseView,
  type LeaseIdentity,
} from './fakePlanHost.js';
import type {StopSeamPorts} from './stopSeam.js';

/** Kernel ModelInvocation 端口（由控制面 HTTP 或测试 mock 注入）。 */
export type KernelModelInvocationPorts = {
  createInvocation: (input: {
    lease: LeaseIdentity;
    contextDigest: string;
    providerRef: string;
    modelId: string;
    inputDigest: string;
    toolsExposedToModel: readonly string[];
  }) => Promise<{invocationId: string}>;
  /** live：dispatch 打本地 Qwen；单测：直接回执文本。 */
  completeInvocation: (input: {
    invocationId: string;
    lease: LeaseIdentity;
  }) => Promise<{text: string; chunks?: KernelLlmChunk[]}>;
};

export type CordisPlanBridgePorts = {
  claimPlan: () => Promise<ActivityLeaseView | null>;
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
  /** 续约 ACTIVE attempt；长 Cordis/Qwen 回合必须周期性调用。 */
  heartbeat?: (input: {
    activityId: string;
    lease: LeaseIdentity;
    renewalSeq: number;
  }) => Promise<{
    renewal_seq: number;
    control?: string;
    pending_stop_ids?: string[];
  }>;
  /**
   * 可选：与 Broker 工具准入门联动。心跳失败时关闭，后续禁止 prepare/dispatch。
   * PLAN 零工具回合也可注入，便于同一宿主在切 EXECUTE 前保持一致。
   */
  toolAdmissionGate?: {
    onHeartbeatFailure: (err: unknown) => void;
  };
  /** 可选：heartbeat STOP 时交诚实 StopReceipt */
  stopSeam?: StopSeamPorts;
  /** 阶段化 Watchdog；缺省不启用（兼容旧测）。Hard ≠ DONE。 */
  watchdog?: ActivationWatchdog;
  /** awaitWithWatchdog 轮询间隔；单测可缩小 */
  watchdogTickEveryMs?: number;
  modelInvocation: KernelModelInvocationPorts;
  /**
   * 构造 PlanCreate。
   * live：须传入 modelNarrative，由实现方解析模型 JSON（禁静默 fixture）。
   * 非 live：可忽略 narrative，使用合同骨架。
   */
  buildPlan: (
    lease: ActivityLeaseView,
    opts?: {modelNarrative?: string},
  ) => Record<string, unknown>;
  /** live 用户提示；缺省则仅传 context_digest。 */
  livePlanPrompt?: (contextDigest: string) => string;
  submitPlanOutcome: (input: {
    activityId: string;
    lease: LeaseIdentity;
    expectedStateRevision: number;
    plan: Record<string, unknown>;
  }) => Promise<void>;
};

const KERNEL_PROVIDER = 'ring-kernel';

function resolveProviderRef(optionsProvider?: string): string {
  const fromOptions = (optionsProvider || '').trim();
  if (fromOptions) {
    return fromOptions;
  }
  const cloudRef = (process.env.RING_CHAT_CLOUD_PROVIDER_REF || '').trim();
  if (cloudRef) {
    return cloudRef;
  }
  return KERNEL_PROVIDER;
}

function inputDigestOf(messages: KernelLlmStreamOptions['messages']): string {
  const canonical = JSON.stringify(
    messages.map((m) => ({role: m.role, content: m.content})),
  );
  return 'sha256:' + createHash('sha256').update(canonical).digest('hex');
}

function rejectToolChunks(chunks: readonly KernelLlmChunk[]): void {
  for (const chunk of chunks) {
    if (chunk.type === 'tool-call') {
      authorizeToolProposal('PLAN', chunk.name, new Set([chunk.name]));
    }
  }
}

export type KernelLlmWatchdogOpts = {
  watchdog?: ActivationWatchdog;
  tickEveryMs?: number;
  setIntervalFn?: typeof setInterval;
  clearIntervalFn?: typeof clearInterval;
};

/**
 * create → complete → yield chunks，全程挂 awaiting_llm / streaming_llm 钟。
 * Hard Idle 抛 WatchdogHardIdleError；不写 Plan/Goal DONE。
 */
export async function* invokeKernelLlmStreamWithWatchdog(
  ports: KernelModelInvocationPorts,
  input: {
    lease: LeaseIdentity;
    contextDigest: string;
    providerRef: string;
    modelId: string;
    messages: KernelLlmStreamOptions['messages'];
    toolsExposedToModel: readonly string[];
  },
  opts?: KernelLlmWatchdogOpts,
): AsyncGenerator<KernelLlmChunk> {
  const watchdog = opts?.watchdog;
  const tickOpts = {
    tickEveryMs: opts?.tickEveryMs,
    setIntervalFn: opts?.setIntervalFn,
    clearIntervalFn: opts?.clearIntervalFn,
  };

  if (watchdog) {
    watchdog.enterPhase('awaiting_llm', 'llm_provider');
  }

  const createWork = ports.createInvocation({
    lease: input.lease,
    contextDigest: input.contextDigest,
    providerRef: input.providerRef,
    modelId: input.modelId,
    inputDigest: inputDigestOf(input.messages),
    toolsExposedToModel: input.toolsExposedToModel,
  });
  const created = watchdog
    ? await awaitWithWatchdog(watchdog, createWork, tickOpts)
    : await createWork;
  watchdog?.reportProgress('llm_provider');

  const completeWork = ports.completeInvocation({
    invocationId: created.invocationId,
    lease: input.lease,
  });
  const done = watchdog
    ? await awaitWithWatchdog(watchdog, completeWork, tickOpts)
    : await completeWork;

  // 首包到达：进入流间隙钟；异主（tool）不能重置
  if (watchdog) {
    watchdog.enterPhase('streaming_llm', 'llm_stream');
    watchdog.reportProgress('llm_stream');
  }

  const chunks: KernelLlmChunk[] =
    done.chunks ??
    (done.text
      ? [
          {type: 'text-delta', text: done.text},
          {type: 'finish', reason: 'stop'},
        ]
      : [{type: 'finish', reason: 'stop'}]);

  let partialAssistantText = '';
  for (const chunk of chunks) {
    if (watchdog) {
      // 先检查流间隙，再记进度；避免本 chunk 的 progress 救活已超时钟
      try {
        throwIfWatchdogHard(watchdog);
      } catch (err) {
        if (
          err instanceof WatchdogHardIdleError &&
          partialAssistantText.trim() &&
          !err.partialAssistantText
        ) {
          throw new WatchdogHardIdleError(
            err.outcome,
            partialAssistantText,
          );
        }
        throw err;
      }
      watchdog.reportProgress('llm_stream');
    }
    if (chunk.type === 'text-delta' && chunk.text) {
      partialAssistantText += chunk.text;
    }
    yield chunk;
  }
}

/**
 * 在 Cordis Context 上 provide('llm')：stream 只走 Kernel ModelInvocation。
 * PLAN 场景 tools 非空立即 ROLE_TOOL_FORBIDDEN。
 */
export function registerKernelLlmOnContext(
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
      rejectPlanTools(tools);
      const stream = invokeKernelLlmStreamWithWatchdog(
        ports,
        {
          lease,
          contextDigest,
          providerRef: resolveProviderRef(options.provider),
          modelId: options.model,
          messages: options.messages,
          toolsExposedToModel: tools,
        },
        opts,
      );
      const collected: KernelLlmChunk[] = [];
      for await (const chunk of stream) {
        collected.push(chunk);
      }
      rejectToolChunks(collected);
      for (const chunk of collected) {
        yield chunk;
      }
    },
  };
  return ctx.provide('llm', service);
}

/**
 * 已持有 ActivityLeaseView 时跑零工具回合（跳过 claimPlan）。
 * TEMPORAL RunActivation：lease 已 admit，勿再扫 claim。
 */
export async function runZeroToolPlanTurnViaCordisWithLease(
  ports: Omit<CordisPlanBridgePorts, 'claimPlan'>,
  claimed: ActivityLeaseView,
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
  modelId: string = 'plan-fixture',
): Promise<'succeeded'> {
  if (claimed.activity.kind !== 'PLAN') {
    throw new Error('UNEXPECTED_ACTIVITY_KIND');
  }

  // 长回合续约：默认 claim TTL 90s，Cordis/Qwen 常更久
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
    const gate = ports.toolAdmissionGate;
    const wd = ports.watchdog;
    heartbeatTimer = setInterval(() => {
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
          // v0.6 §6：Kernel 续期失败立即撤销后续工具准入；不在本地重试放大权限
          gate?.onHeartbeatFailure(err);
        });
    }, 30_000);
    // 不阻止进程退出（测试子进程）
    heartbeatTimer.unref?.();
  }

  ports.watchdog?.enterPhase('setup', 'runner_setup');
  ports.watchdog?.reportProgress('runner_setup');

  let narrative = '';
  try {
    const boot = await bootPinnedCordis(checkoutDir);
    const compiled = await ports.compileContext({
      activityId: claimed.activity.id,
      lease: claimed.lease,
    });
    if (compiled.content.role !== 'PLANNER') {
      throw new Error('CONTEXT_ROLE_MISMATCH');
    }
    const bound = await ports.bindContext({
      activityId: claimed.activity.id,
      lease: claimed.lease,
      bindingDigest: bindingDigestOf(claimed.activity.binding),
      contextBundleId: compiled.id,
    });

    const dispose = registerKernelLlmOnContext(
      boot.ctx,
      ports.modelInvocation,
      claimed.lease,
      bound.context_digest,
      {
        watchdog: ports.watchdog,
        tickEveryMs: ports.watchdogTickEveryMs,
      },
    );
    try {
      const llm = boot.ctx.get('llm') as KernelLlmService | undefined;
      if (!llm || llm.implementation !== 'ring-kernel-bridge') {
        throw new Error('HARNESS_LLM_MISSING: Context 上未挂 ring-kernel-bridge');
      }
      // 零工具回合：provider 走 Kernel 权威；live 时 modelId 须为真实 Qwen id
      const collected: KernelLlmChunk[] = [];
      const userContent =
        modelId !== 'plan-fixture' && ports.livePlanPrompt
          ? ports.livePlanPrompt(bound.context_digest)
          : `PLAN context_digest=${bound.context_digest}`;
      for await (const chunk of llm.stream({
        provider: KERNEL_PROVIDER,
        model: modelId,
        messages: [
          {
            role: 'user',
            content: userContent,
          },
        ],
        tools: [],
      })) {
        collected.push(chunk);
        if (chunk.type === 'text-delta' && chunk.text) {
          narrative += chunk.text;
        }
      }
      if (!collected.some((c) => c.type === 'finish')) {
        throw new Error('HARNESS_LLM_INCOMPLETE: stream 未结束');
      }
      // live 模型路径：禁止在无正文时继续用合同 fixture 冒充模型产出
      if (modelId !== 'plan-fixture' && !narrative.trim()) {
        throw new Error('HARNESS_LIVE_EMPTY: live 模型未返回正文');
      }
    } finally {
      dispose();
    }

    const plan =
      modelId !== 'plan-fixture'
        ? ports.buildPlan(claimed, {modelNarrative: narrative.trim()})
        : ports.buildPlan(claimed);
    await ports.submitPlanOutcome({
      activityId: claimed.activity.id,
      lease: claimed.lease,
      expectedStateRevision: claimed.activity.state_revision,
      plan,
    });
    return 'succeeded';
  } finally {
    stopHeartbeat();
  }
}

/**
 * boot Cordis → 挂薄 llm → 零工具 stream → PlanCreate outcome。
 * 成功只证明通路；Goal 是否 RUNNING 由 Kernel 决定；≠ 官方 LlmRuntime。
 */
export async function runZeroToolPlanTurnViaCordis(
  ports: CordisPlanBridgePorts,
  checkoutDir: string | undefined = process.env.RING_HARNESS_CHECKOUT,
  modelId: string = 'plan-fixture',
): Promise<'idle' | 'succeeded'> {
  const claimed = await ports.claimPlan();
  if (!claimed) {
    return 'idle';
  }
  return runZeroToolPlanTurnViaCordisWithLease(
    ports,
    claimed,
    checkoutDir,
    modelId,
  );
}
