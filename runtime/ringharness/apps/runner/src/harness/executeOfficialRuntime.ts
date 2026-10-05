/**
 * RunActivation EXECUTE 官方 AgentLoop 可选驱动。
 *
 * 环境变量 `RING_HARNESS_EXECUTE_RUNTIME=deepseek-official-agent-loop` 时启用；
 * `RING_HARNESS_EXECUTE_OFFICIAL_MODE=read_file|diagnose`（缺省 read_file）；
 * diagnose 可用 `RING_HARNESS_EXECUTE_OFFICIAL_FSM=1` 注入状态机（测），缺省走真实 chat；
 * 测 mock Control 可另设 `RING_HARNESS_EXECUTE_OFFICIAL_FAST_POLL=1`。
 * 官方路径默认租约心跳（`RING_HARNESS_EXECUTE_HEARTBEAT=0` 可关）。
 * 默认仍走 Cordis+Broker 宿主。循环归上游；副作用经 Broker Gateway（Runner 不 dispatch）。
 * ≠ Goal DONE。
 */
import {HARNESS_RUNTIME_DEEPSEEK_OFFICIAL} from './harnessRuntimePlugin.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {runOfficialAb01BrokerReadFileTurn} from './officialAb01BrokerSeam.js';
import {runOfficialChatDrivenDiagnoseBrokerCycle} from './officialChatDrivenDiagnoseBrokerSeam.js';
import {startLeaseHeartbeat} from './leaseHeartbeat.js';
import {resolveLeaseHeartbeatIntervalMs} from './leaseTiming.js';
import {fetchGoalSealActivation} from './goalSealProfiles.js';
import {createHttpArtifactPutPorts} from './artifactPutHttpPorts.js';
import {reportActivationTermination} from './reportActivationTermination.js';
import type {ForceStopZeroToolCloseoutResult} from './forceStopZeroToolCloseout.js';
import {
  formatWatchdogHardIdleFailReason,
  isWatchdogHardIdleError,
  persistWatchdogHardIdlePartial,
} from './watchdogPartialArtifact.js';
import {
  toolChatConfigFromEnv,
  type OpenAiCompatibleToolChatConfig,
} from './openaiCompatibleToolChat.js';
import {
  bindingDigestOf,
  type ActivityLeaseView,
} from './fakePlanHost.js';
import {createControlHttpCordisPorts} from './controlHttpPorts.js';
import {resolveOfficialProviderRef} from './modelInvocationLedger.js';

export const EXECUTE_RUNTIME_OFFICIAL = HARNESS_RUNTIME_DEEPSEEK_OFFICIAL;

export type ExecuteRuntimeId = 'default' | typeof EXECUTE_RUNTIME_OFFICIAL;

export type OfficialExecuteMode = 'read_file' | 'diagnose';

/** 解析 EXECUTE 驱动；非法值失败关闭。 */
export function resolveExecuteRuntime(
  env: NodeJS.ProcessEnv = process.env,
): ExecuteRuntimeId {
  const raw = (env.RING_HARNESS_EXECUTE_RUNTIME || '').trim();
  if (!raw || raw === 'default' || raw === 'cordis') {
    return 'default';
  }
  if (raw === EXECUTE_RUNTIME_OFFICIAL) {
    return EXECUTE_RUNTIME_OFFICIAL;
  }
  throw new Error(
    `HARNESS_EXECUTE_RUNTIME_UNKNOWN: ${raw}（允许 default|cordis|${EXECUTE_RUNTIME_OFFICIAL}）`,
  );
}

/** 官方 EXECUTE 回合形态；非法失败关闭。 */
export function resolveOfficialExecuteMode(
  env: NodeJS.ProcessEnv = process.env,
): OfficialExecuteMode {
  const raw = (env.RING_HARNESS_EXECUTE_OFFICIAL_MODE || '').trim();
  if (!raw || raw === 'read_file' || raw === 'ab01') {
    return 'read_file';
  }
  if (raw === 'diagnose' || raw === 'diagnose_seal') {
    return 'diagnose';
  }
  throw new Error(
    `HARNESS_EXECUTE_OFFICIAL_MODE_UNKNOWN: ${raw}（允许 read_file|diagnose）`,
  );
}

function parseSealProfileIds(env: NodeJS.ProcessEnv): string[] | undefined {
  const raw = (env.RING_HARNESS_EXECUTE_SEAL_PROFILE_IDS || '').trim();
  if (!raw) return undefined;
  if (raw.startsWith('[')) {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed) || !parsed.every((x) => typeof x === 'string')) {
      throw new Error(
        'RING_HARNESS_EXECUTE_SEAL_PROFILE_IDS 须为 JSON string 数组',
      );
    }
    return parsed.filter((id) => id.trim());
  }
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function bearerAuth(raw: string): string {
  const t = raw.trim();
  return t.startsWith('Bearer ') ? t : `Bearer ${t}`;
}

function resolveHeartbeatIntervalMs(env: NodeJS.ProcessEnv): number | null {
  return resolveLeaseHeartbeatIntervalMs(env).intervalMs;
}

/** 下一拍 renewal_seq（当前值+1）；与 E2E 预续期对齐。 */
function resolveNextRenewalSeq(env: NodeJS.ProcessEnv): number | undefined {
  const raw = (env.RING_HARNESS_EXECUTE_NEXT_RENEWAL_SEQ || '').trim();
  if (!raw) return undefined;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 1) {
    throw new Error('RING_HARNESS_EXECUTE_NEXT_RENEWAL_SEQ 须为正整数');
  }
  return Math.floor(n);
}

/**
 * 官方 Loop 入账前置：compile+bind context，再装配 ModelInvocationLedgerPorts。
 * Codex P0-1；≠ DONE。
 */
async function prepareOfficialModelInvocationLedger(input: {
  claimed: ActivityLeaseView;
  controlUrl: string;
  workerJwt: string;
  chat: OpenAiCompatibleToolChatConfig;
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
}): Promise<import('./modelInvocationLedger.js').ModelInvocationLedgerPorts> {
  const {claimed} = input;
  const ports = createControlHttpCordisPorts({
    baseUrl: input.controlUrl,
    authorization: bearerAuth(input.workerJwt),
    admittedLease: claimed.lease,
    fetchImpl: input.fetchImpl,
    liveDispatch: false,
    buildPlan: () => {
      throw new Error('EXECUTE_OFFICIAL_NO_PLAN');
    },
  });
  const compiled = await ports.compileContext({
    activityId: claimed.activity.id,
    lease: claimed.lease,
  });
  const bound = await ports.bindContext({
    activityId: claimed.activity.id,
    lease: claimed.lease,
    bindingDigest: bindingDigestOf(claimed.activity.binding),
    contextBundleId: compiled.id,
  });
  return {
    baseUrl: input.controlUrl,
    authorization: bearerAuth(input.workerJwt),
    fetchImpl: input.fetchImpl,
    lease: claimed.lease,
    contextDigest: bound.context_digest,
    providerRef: resolveOfficialProviderRef(input.env ?? process.env),
    modelId: input.chat.modelId,
    maxOutputTokens: input.chat.maxTokens ?? 512,
  };
}

async function withOfficialLeaseHeartbeat<T>(input: {
  claimed: ActivityLeaseView;
  controlUrl: string;
  workerJwt: string;
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  /** 下一心跳 renewal_seq；来自 claim/预续期后的 attempt */
  nextRenewalSeq?: number;
  run: () => Promise<T>;
}): Promise<T> {
  const intervalMs = resolveHeartbeatIntervalMs(input.env ?? process.env);
  if (intervalMs === null) {
    return input.run();
  }
  let rejectRun: ((err: unknown) => void) | undefined;
  let settled = false;
  const failed = new Promise<never>((_, reject) => {
    rejectRun = reject;
  });
  const hb = startLeaseHeartbeat({
    activityId: input.claimed.activity.id,
    lease: input.claimed.lease,
    baseUrl: input.controlUrl,
    authorization: input.workerJwt,
    fetchImpl: input.fetchImpl,
    intervalMs,
    nextRenewalSeq: input.nextRenewalSeq,
    onFailure: (err) => {
      if (settled) return;
      rejectRun?.(err instanceof Error ? err : new Error(String(err)));
    },
  });
  try {
    const result = await Promise.race([input.run(), failed]);
    settled = true;
    return result;
  } finally {
    settled = true;
    hb.stop();
  }
}

export type OfficialExecuteReadiness =
  | {
      ok: true;
      checkout: string;
      chat: OpenAiCompatibleToolChatConfig;
    }
  | {ok: false; reason: string};

export function assessOfficialExecuteRuntime(
  checkout: string,
  env: NodeJS.ProcessEnv = process.env,
): OfficialExecuteReadiness {
  if (!isOfficialAgentLoopBuilt(checkout)) {
    return {
      ok: false,
      reason:
        'RING_HARNESS_EXECUTE_RUNTIME=deepseek-official-agent-loop 但 AgentLoop peers 未 build:lib:host',
    };
  }
  const chat = toolChatConfigFromEnv(env);
  if (!chat) {
    return {
      ok: false,
      reason:
        '官方 EXECUTE 需要 RING_LOCAL_QWEN_BASE/API_KEY/MODEL（或注入 chat）；保持 pending_harness',
    };
  }
  return {ok: true, checkout, chat};
}

export type OfficialExecuteTurnOutcome = {
  status: 'ACTIVATION_SUBMITTED' | 'FAILED';
  pending_harness: false;
  kind: 'EXECUTE';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
  effect_ids: string[];
  tool_names?: string[];
  tool_result_round?: {
    round2_cites_tool_result: boolean;
    requires_reconciliation: boolean;
    marks_goal_done: false;
  };
  driver: typeof EXECUTE_RUNTIME_OFFICIAL;
  marks_goal_done: false;
  /** AB05：Hard Idle 部分助手文本已落 collector 时的工件 id */
  partial_artifact_id?: string;
  /** AB06：硬 ForceStop 零工具收口（FAILED 时）；≠ DONE */
  force_stop_closeout?: {
    assistant_text: string;
    summary_artifact_id: string | null;
    closeout_artifact_id?: string;
    used_fallback: boolean;
    marks_goal_done: false;
  };
  /** M3.5：Kernel activation_terminations 行 id */
  activation_termination_id?: string;
  /** 诚实：diagnose 可为 chat-driven / FSM */
  scripted_order?: boolean;
  chat_driven_order?: boolean;
};

function failedOfficial(
  claimed: ActivityLeaseView,
  reason: string,
  extra?: {
    partial_artifact_id?: string;
    effect_ids?: string[];
    force_stop_closeout?: OfficialExecuteTurnOutcome['force_stop_closeout'];
    activation_termination_id?: string;
  },
): OfficialExecuteTurnOutcome {
  return {
    status: 'FAILED',
    pending_harness: false,
    kind: 'EXECUTE',
    reason,
    activity_id: claimed.activity.id,
    attempt_id: claimed.lease.attempt_id,
    fencing_epoch: claimed.lease.fencing_epoch,
    effect_ids: extra?.effect_ids ?? [],
    driver: EXECUTE_RUNTIME_OFFICIAL,
    marks_goal_done: false,
    ...(extra?.partial_artifact_id
      ? {partial_artifact_id: extra.partial_artifact_id}
      : {}),
    ...(extra?.force_stop_closeout
      ? {force_stop_closeout: extra.force_stop_closeout}
      : {}),
    ...(extra?.activation_termination_id
      ? {activation_termination_id: extra.activation_termination_id}
      : {}),
  };
}

/** 官方 Loop 硬 ForceStop：FAILED + Kernel NO_PROGRESS_STOP；≠ DONE。 */
export async function failedOfficialHardForceStop(input: {
  claimed: ActivityLeaseView;
  closeout: ForceStopZeroToolCloseoutResult;
  reason?: string;
  effectIds?: string[];
  controlUrl: string;
  workerJwt: string;
  fetchImpl?: typeof fetch;
}): Promise<OfficialExecuteTurnOutcome> {
  const reason =
    (input.reason ?? '').trim() ||
    'NO_PROGRESS_FORCE_STOP:official_zero_tool_closeout';
  let terminationId: string | undefined;
  try {
    const reported = await reportActivationTermination({
      baseUrl: input.controlUrl,
      authorization: input.workerJwt,
      activityId: input.claimed.activity.id,
      lease: {
        activity_id: input.claimed.lease.activity_id,
        attempt_id: input.claimed.lease.attempt_id,
        fencing_epoch: input.claimed.lease.fencing_epoch,
      },
      reason: 'NO_PROGRESS_STOP',
      detail: reason.slice(0, 2000),
      summaryArtifactId: input.closeout.summaryArtifactId,
      closeoutArtifactId: input.closeout.closeoutArtifactId,
      fetchImpl: input.fetchImpl,
    });
    terminationId = reported.id;
  } catch {
    /* 登记失败仍 FAILED ≠ DONE */
  }
  return failedOfficial(input.claimed, reason, {
    effect_ids: input.effectIds ?? [],
    force_stop_closeout: {
      assistant_text: input.closeout.assistantText,
      summary_artifact_id: input.closeout.summaryArtifactId,
      ...(input.closeout.closeoutArtifactId
        ? {closeout_artifact_id: input.closeout.closeoutArtifactId}
        : {}),
      used_fallback: input.closeout.usedFallback,
      marks_goal_done: false,
    },
    ...(terminationId ? {activation_termination_id: terminationId} : {}),
  });
}

/** Hard Idle：尽量落 partial Artifact，再 FAILED（≠ DONE）。 */
async function failedOfficialHardIdle(
  claimed: ActivityLeaseView,
  err: unknown,
  ports: {
    controlUrl: string;
    workerJwt: string;
    fetchImpl?: typeof fetch;
  },
): Promise<OfficialExecuteTurnOutcome> {
  if (!isWatchdogHardIdleError(err)) {
    return failedOfficial(
      claimed,
      err instanceof Error ? err.message : String(err),
    );
  }
  let partialId: string | undefined;
  try {
    const artifacts = createHttpArtifactPutPorts({
      baseUrl: ports.controlUrl,
      authorization: bearerAuth(ports.workerJwt),
      fetchImpl: ports.fetchImpl,
    });
    const persisted = await persistWatchdogHardIdlePartial({
      artifacts,
      projectId: claimed.activity.project_id,
      lease: claimed.lease,
      error: err,
    });
    partialId = persisted?.artifactId;
  } catch {
    // 落盘失败不吞 Hard；仍 FAILED 且无 partial id
  }
  return failedOfficial(
    claimed,
    formatWatchdogHardIdleFailReason(err, partialId),
    partialId ? {partial_artifact_id: partialId} : undefined,
  );
}

/**
 * 已 claim 的 EXECUTE 租约上跑官方 AgentLoop × Broker Gateway（read_file）。
 */
export async function runOfficialExecuteReadFileTurn(input: {
  claimed: ActivityLeaseView;
  checkout: string;
  chat: OpenAiCompatibleToolChatConfig;
  controlUrl: string;
  workerJwt: string;
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  userPrompt?: string;
  expectedPath?: string;
  idleTimeoutMs?: number;
}): Promise<OfficialExecuteTurnOutcome> {
  const {claimed} = input;
  if (claimed.activity.kind !== 'EXECUTE') {
    return failedOfficial(
      claimed,
      `UNEXPECTED_ACTIVITY_KIND:${claimed.activity.kind}`,
    );
  }

  const expectedPath = input.expectedPath ?? 'notes/execute.txt';
  let nextRenewalSeq: number | undefined;
  try {
    nextRenewalSeq = resolveNextRenewalSeq(input.env ?? process.env);
  } catch (err) {
    return failedOfficial(
      claimed,
      err instanceof Error ? err.message : String(err),
    );
  }
  return withOfficialLeaseHeartbeat({
    claimed,
    controlUrl: input.controlUrl,
    workerJwt: input.workerJwt,
    fetchImpl: input.fetchImpl,
    env: input.env ?? process.env,
    nextRenewalSeq,
    run: async () => {
      try {
        const modelInvocationLedger = await prepareOfficialModelInvocationLedger({
          claimed,
          controlUrl: input.controlUrl,
          workerJwt: input.workerJwt,
          chat: input.chat,
          fetchImpl: input.fetchImpl,
          env: input.env ?? process.env,
        });
        const evidence = await runOfficialAb01BrokerReadFileTurn({
          checkoutDir: input.checkout,
          chat: input.chat,
          userPrompt:
            input.userPrompt ??
            `你必须调用工具 read_file，参数 path 恰好为 ${expectedPath}。`,
          expectedPath,
          idleTimeoutMs: input.idleTimeoutMs ?? 180_000,
          modelId: input.chat.modelId,
          modelInvocationLedger,
          broker: {
            baseUrl: input.controlUrl,
            authorization: bearerAuth(input.workerJwt),
            activation: {
              kind: 'EXECUTE',
              projectId: claimed.activity.project_id,
              activityId: claimed.activity.id,
              lease: claimed.lease,
            },
            fetchImpl: input.fetchImpl,
            poll: {maxAttempts: 120, delayMs: 1_000},
          },
        });

        if (!evidence.effectId) {
          return failedOfficial(claimed, 'NO_TOOL_PROPOSAL');
        }

        return {
          status: 'ACTIVATION_SUBMITTED' as const,
          pending_harness: false as const,
          kind: 'EXECUTE' as const,
          activity_id: claimed.activity.id,
          attempt_id: claimed.lease.attempt_id,
          fencing_epoch: claimed.lease.fencing_epoch,
          effect_ids: [evidence.effectId],
          tool_names: ['read_file'],
          tool_result_round: {
            round2_cites_tool_result: evidence.round2CitesToolResult,
            requires_reconciliation: evidence.effectStatus === 'UNKNOWN',
            marks_goal_done: false as const,
          },
          driver: EXECUTE_RUNTIME_OFFICIAL,
          marks_goal_done: false as const,
        };
      } catch (err) {
        return failedOfficialHardIdle(claimed, err, {
          controlUrl: input.controlUrl,
          workerJwt: input.workerJwt,
          fetchImpl: input.fetchImpl,
        });
      }
    },
  });
}

/**
 * EXECUTE 官方 diagnose：read→红→write→绿[→seal] × Gateway。
 * FSM=1 时用注入状态机（测）；缺省真实 chat（可 followup 续跑）。≠ Goal DONE。
 */
export async function runOfficialExecuteDiagnoseCycleTurn(input: {
  claimed: ActivityLeaseView;
  checkout: string;
  chat: OpenAiCompatibleToolChatConfig;
  controlUrl: string;
  workerJwt: string;
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  userPrompt?: string;
  idleTimeoutMs?: number;
}): Promise<OfficialExecuteTurnOutcome> {
  const {claimed} = input;
  const env = input.env ?? process.env;
  if (claimed.activity.kind !== 'EXECUTE') {
    return failedOfficial(
      claimed,
      `UNEXPECTED_ACTIVITY_KIND:${claimed.activity.kind}`,
    );
  }

  const useFsm = (env.RING_HARNESS_EXECUTE_OFFICIAL_FSM || '').trim() === '1';
  const fastPoll = (env.RING_HARNESS_EXECUTE_OFFICIAL_FAST_POLL || '').trim() === '1';
  const readPath =
    (env.RING_HARNESS_EXECUTE_READ_PATH || '').trim() ||
    'order_service/store.py';
  const writePath =
    (env.RING_HARNESS_EXECUTE_WRITE_PATH || '').trim() || readPath;
  const writeContent =
    env.RING_HARNESS_EXECUTE_WRITE_CONTENT ?? 'fixed_idempotent\n';
  let sealIds: string[] | undefined;
  let nextRenewalSeq: number | undefined;
  try {
    sealIds = parseSealProfileIds(env);
    nextRenewalSeq = resolveNextRenewalSeq(env);
  } catch (err) {
    return failedOfficial(
      claimed,
      err instanceof Error ? err.message : String(err),
    );
  }

  return withOfficialLeaseHeartbeat({
    claimed,
    controlUrl: input.controlUrl,
    workerJwt: input.workerJwt,
    fetchImpl: input.fetchImpl,
    env,
    nextRenewalSeq,
    run: async () => {
      let userPrompt = (env.RING_HARNESS_EXECUTE_USER_PROMPT || '').trim();
      const goalId = (claimed.activity.goal_id || '').trim();
      const taskId = (claimed.activity.task_id || '').trim();
      let acceptanceDescriptions: string[] | undefined;
      // 无 env seal / 无 USER_PROMPT：Task acceptance → seal schema；Goal/Task 文案
      if ((!sealIds?.length || !userPrompt) && goalId && taskId) {
        try {
          const activation = await fetchGoalSealActivation({
            baseUrl: input.controlUrl,
            authorization: bearerAuth(input.workerJwt),
            goalId,
            taskId,
            fetchImpl: input.fetchImpl,
          });
          if (!sealIds?.length) {
            sealIds = activation.sealVerificationProfileIds;
          }
          if (!userPrompt) {
            userPrompt = activation.userPromptFromGoal;
          }
          acceptanceDescriptions = activation.acceptanceDescriptions;
        } catch (err) {
          // live（非 FSM）必须能解析 Task seal；FSM 单测可无合同 mock、无 seal 工具
          if (!sealIds?.length && !useFsm) {
            return failedOfficial(
              claimed,
              err instanceof Error ? err.message : String(err),
            );
          }
        }
      } else if (!sealIds?.length && !useFsm && goalId && !taskId) {
        return failedOfficial(claimed, 'TASK_ID_REQUIRED_FOR_SEAL');
      }
      // 无 sealIds：官方 loop 走四步（无 seal_candidate）；live 有 Task 合同时应已填上
      if (!userPrompt) {
        userPrompt =
          `诊断并修复 ${readPath}：read_file → run_tests → write_file → run_tests` +
          (sealIds?.length ? ` → seal_candidate` : '');
      }

      try {
        const modelInvocationLedger = await prepareOfficialModelInvocationLedger({
          claimed,
          controlUrl: input.controlUrl,
          workerJwt: input.workerJwt,
          chat: input.chat,
          fetchImpl: input.fetchImpl,
          env,
        });

        const artifacts = createHttpArtifactPutPorts({
          baseUrl: input.controlUrl,
          authorization: bearerAuth(input.workerJwt),
          fetchImpl: input.fetchImpl,
        });
        const evidence = await runOfficialChatDrivenDiagnoseBrokerCycle({
          checkoutDir: input.checkout,
          chat: {
            ...input.chat,
            // FSM 时禁止 Control fetch 压过 chat 状态机
            fetchImpl: useFsm ? undefined : input.chat.fetchImpl,
            toolChoice: 'auto',
          },
          useInjectedDecisionFsm: useFsm,
          readPath,
          writePath,
          writeContent,
          sealVerificationProfileIds: sealIds,
          acceptanceDescriptions,
          userPrompt,
          // AB06：diagnose 通路与 RunActivation Cordis 同源 goal 键
          goalId: goalId || undefined,
          idleTimeoutMs: input.idleTimeoutMs ?? (useFsm ? 120_000 : 300_000),
          getForceStopSummaryContent: (id) => artifacts.getArtifactContent(id),
          putForceStopCloseout: async (body) => {
            const put = await artifacts.putCollectorContent({
              projectId: claimed.activity.project_id,
              lease: claimed.lease,
              body,
              mime: 'application/json',
            });
            return {artifactId: put.artifactId};
          },
          modelInvocationLedger,
          broker: {
            baseUrl: input.controlUrl,
            authorization: bearerAuth(input.workerJwt),
            activation: {
              kind: 'EXECUTE',
              projectId: claimed.activity.project_id,
              activityId: claimed.activity.id,
              lease: claimed.lease,
            },
            fetchImpl: input.fetchImpl,
            goalId: goalId || undefined,
            // FSM 仅决定工具顺序；真实 Broker 仍须正常 poll（测可 FAST_POLL=1）
            poll: fastPoll
              ? {maxAttempts: 3, delayMs: 0}
              : {maxAttempts: 300, delayMs: 150},
          },
        });

        const succeeded = evidence.trail.filter((t) => Boolean(t.effectId));
        const effectIds = succeeded.map((t) => t.effectId);

        // AB06/M3.5：硬 ForceStop 收口 → FAILED + Kernel 终止；禁止冒充 SUBMITTED
        if (evidence.forceStopCloseout) {
          const signal =
            evidence.trail
              .map((t) => t.toolResultText)
              .find(
                (t) =>
                  t.includes('TOOL_ADMISSION_CLOSED') ||
                  t.includes('NO_PROGRESS_FORCE_STOP') ||
                  t.includes('禁止再调工具'),
              ) ??
            evidence.assistantTexts.find(
              (t) =>
                t.includes('TOOL_ADMISSION_CLOSED') ||
                t.includes('NO_PROGRESS_FORCE_STOP'),
            );
          return failedOfficialHardForceStop({
            claimed,
            closeout: evidence.forceStopCloseout,
            reason: signal || undefined,
            effectIds,
            controlUrl: input.controlUrl,
            workerJwt: input.workerJwt,
            fetchImpl: input.fetchImpl,
          });
        }

        if (effectIds.length === 0) {
          return failedOfficial(claimed, 'NO_TOOL_PROPOSAL');
        }

        return {
          status: 'ACTIVATION_SUBMITTED' as const,
          pending_harness: false as const,
          kind: 'EXECUTE' as const,
          activity_id: claimed.activity.id,
          attempt_id: claimed.lease.attempt_id,
          fencing_epoch: claimed.lease.fencing_epoch,
          effect_ids: effectIds,
          tool_names: succeeded.map((t) => t.toolName),
          tool_result_round: {
            round2_cites_tool_result: evidence.laterRoundsCitePriorToolResults,
            requires_reconciliation: evidence.trail.some(
              (t) => t.effectStatus === 'UNKNOWN',
            ),
            marks_goal_done: false as const,
          },
          driver: EXECUTE_RUNTIME_OFFICIAL,
          marks_goal_done: false as const,
          scripted_order: evidence.scriptedOrder,
          chat_driven_order: evidence.chatDrivenOrder,
        };
      } catch (err) {
        return failedOfficialHardIdle(claimed, err, {
          controlUrl: input.controlUrl,
          workerJwt: input.workerJwt,
          fetchImpl: input.fetchImpl,
        });
      }
    },
  });
}

/** 按 RING_HARNESS_EXECUTE_OFFICIAL_MODE 分发官方 EXECUTE 回合。 */
export async function runOfficialExecuteTurn(input: {
  claimed: ActivityLeaseView;
  checkout: string;
  chat: OpenAiCompatibleToolChatConfig;
  controlUrl: string;
  workerJwt: string;
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  userPrompt?: string;
  idleTimeoutMs?: number;
}): Promise<OfficialExecuteTurnOutcome> {
  const mode = resolveOfficialExecuteMode(input.env ?? process.env);
  if (mode === 'diagnose') {
    return runOfficialExecuteDiagnoseCycleTurn(input);
  }
  return runOfficialExecuteReadFileTurn(input);
}
