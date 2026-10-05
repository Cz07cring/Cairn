/**
 * DeepSeek Harness 工具 interface 的 Broker-backed adapter。
 *
 * Runner 只提交已规范化的工具意图；独立 ExecutionBroker 负责 dispatch 和真实执行。
 * 只有 Kernel 已采纳可信回执并给出终态后，才向 Harness 返回工具结果。
 */
import {
  createActivationWatchdog,
  type ActivationWatchdog,
  type ActivationWatchdogConfig,
} from './activationWatchdog.js';
import {bearerAuthHeader} from './authHeader.js';
import {canonicalizeToolArgumentsPayload} from './cordisExecuteBridge.js';
import {createExecuteToolHost} from './executeToolHost.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
  type GuardVerdict,
  type NoProgressGuard,
  type NoProgressGuardConfig,
} from './noProgressGuard.js';
import {getOrCreateBanCallKeyScope} from './banCallKeyScope.js';
import {getOrCreateNudgeBudgetScope} from './nudgeBudgetScope.js';
import {
  forceStopVerdictFromClosedGate,
  persistForceStopSummary,
} from './forceStopSummaryArtifact.js';
import {
  createTurnEnvelopeTracker,
  type ToolResultEnvelopeBudgets,
} from './toolResultEnvelope.js';
import {
  assertSerialOnly,
  classifyToolCallConcurrency,
  type ToolCallConcurrencyDecision,
} from './toolConcurrencyClass.js';
import {formatRunTestsToolResultText} from './runTestsToolResult.js';

type LeaseIdentity = {
  activity_id: string;
  attempt_id: string;
  fencing_epoch: string;
};

export type HarnessToolActivation = {
  kind: 'EXECUTE';
  projectId: string;
  activityId: string;
  lease: LeaseIdentity;
};

export type HarnessToolCall = {
  callId: string;
  name: string;
  arguments: string;
};

export type HarnessToolResult = {
  content: Array<{type: 'text'; text: string}>;
  isError: boolean;
  error?: {
    message: string;
    info: {name: 'BrokerToolError'; code: string};
  };
  meta: {
    effectId: string;
    status: string;
    evidenceIds: string[];
    requiresReconciliation: boolean;
    /** AB07：调用级并发裁决；V1 恒为 SERIAL */
    concurrency?: {
      class: 'SERIAL' | 'PARALLEL_SAFE' | 'UNKNOWN';
      resourceKeys: string[];
      reason: string;
      marksGoalDone: false;
    };
    /** 外溢信封元数据；未触达预算时 omitted */
    envelope?: {
      spilled: boolean;
      digest: string;
      runeCount: number;
      previewRunes: number;
    };
    /** No-Progress Guard 裁决；continue 时 omitted */
    progressGuard?: {
      action: 'nudge' | 'force_stop';
      code: string;
      marksGoalDone: false;
      /** force_stop 时：false=仅禁同参；true/缺省=关全闸 */
      closeToolAdmission?: boolean;
    };
    /** AB06：ForceStop 零工具总结 Artifact id；≠ DONE */
    summaryArtifactId?: string;
  };
};

type EffectResource = {
  id: string;
  status: string;
  evidence_ids?: string[];
};

type Envelope<T> = {data: T};

export type BrokerBackedHarnessTool = {
  execute(call: HarnessToolCall): Promise<HarnessToolResult>;
  /** 供 RunActivation heartbeat details / 观测；不写 Goal DONE */
  watchdog: ActivationWatchdog;
  /** 无进展滑窗；ForceStop ≠ DONE */
  progressGuard: NoProgressGuard;
};

export type BrokerBackedHarnessToolConfig = {
  baseUrl: string;
  authorization: string;
  activation: HarnessToolActivation;
  fetchImpl?: typeof fetch;
  poll?: {
    maxAttempts?: number;
    delayMs?: number;
    sleep?: (ms: number) => Promise<void>;
  };
  /** Prompt 双层预算；缺省用 DEFAULT_TOOL_RESULT_ENVELOPE_BUDGETS */
  envelopeBudgets?: Partial<ToolResultEnvelopeBudgets>;
  /** 注入已有 watchdog；否则按 watchdogConfig + 宿主 gate 创建 */
  watchdog?: ActivationWatchdog;
  watchdogConfig?: Omit<ActivationWatchdogConfig, 'gate'>;
  progressGuard?: NoProgressGuard;
  progressGuardConfig?: Omit<NoProgressGuardConfig, 'gate'>;
  /**
   * AB06：同 Goal 跨 activation 共享 Nudge 预算。
   * 仅在未注入 progressGuard 时生效；与 progressGuardConfig.nudgeBudgetScope 并存时以后者为准。
   */
  goalId?: string;
};

const TERMINAL_EFFECTS = new Set(['SUCCEEDED', 'FAILED', 'UNKNOWN', 'CANCELLED']);

/** 参数/步骤/绿测闸冲突：返回 ToolResult 供模型续跑，不关准入。 */
function isRetryableGatewayClientError(msg: string): boolean {
  return (
    /EFFECT_PREPARE_FAILED: HTTP (400|409|422)\b/.test(msg) ||
    /STEP_CREATE_FAILED: HTTP (400|409|422)\b/.test(msg) ||
    /ARTIFACT_PUT_FAILED: HTTP (400|422)\b/.test(msg)
  );
}

function retryableGatewayErrorResult(
  msg: string,
  concurrency?: ToolCallConcurrencyDecision,
): HarnessToolResult {
  let code = 'GATEWAY_CLIENT_REJECTED';
  if (msg.includes('SEAL_REQUIRES_GREEN_TESTS')) {
    code = 'SEAL_REQUIRES_GREEN_TESTS';
  } else if (msg.includes('STEP_CONFLICT')) {
    code = 'STEP_CONFLICT';
  } else if (/EFFECT_PREPARE_FAILED/.test(msg)) {
    code = 'EFFECT_PREPARE_REJECTED';
  } else if (/STEP_CREATE_FAILED/.test(msg)) {
    code = 'STEP_CREATE_REJECTED';
  } else if (/ARTIFACT_PUT_FAILED/.test(msg)) {
    code = 'ARTIFACT_PUT_REJECTED';
  }
  const hint =
    code === 'SEAL_REQUIRES_GREEN_TESTS'
      ? ' 请先 write_file 后跑 public run_tests 至 exit_code=0，再 seal_candidate。'
      : ' 可换参或换工具后重试。';
  return {
    content: [
      {
        type: 'text',
        text: `Error: ${msg}${hint}（≠DONE）`,
      },
    ],
    isError: true,
    error: {
      message: msg,
      info: {name: 'BrokerToolError', code},
    },
    meta: {
      effectId: '',
      status: 'VALIDATION_REJECTED',
      evidenceIds: [],
      requiresReconciliation: false,
      ...(concurrency
        ? {
            concurrency: {
              class: concurrency.class,
              resourceKeys: concurrency.resourceKeys,
              reason: concurrency.reason,
              marksGoalDone: false as const,
            },
          }
        : {}),
    },
  };
}

function errorResult(
  effect: EffectResource,
  code: string,
  message: string,
  requiresReconciliation: boolean,
  concurrency?: ToolCallConcurrencyDecision,
): HarnessToolResult {
  return {
    content: [{type: 'text', text: `Error: ${message}`}],
    isError: true,
    error: {message, info: {name: 'BrokerToolError', code}},
    meta: {
      effectId: effect.id,
      status: effect.status,
      evidenceIds: effect.evidence_ids ?? [],
      requiresReconciliation,
      ...(concurrency
        ? {
            concurrency: {
              class: concurrency.class,
              resourceKeys: concurrency.resourceKeys,
              reason: concurrency.reason,
              marksGoalDone: false as const,
            },
          }
        : {}),
    },
  };
}

/**
 * 将远端 Control/Broker 集群隐藏在一个 Harness 工具 interface 后。
 *
 * 注意：此处只 prepare，不调用 dispatch。Broker 是唯一真实 dispatch 方；否则 Runner
 * 先把状态置为 DISPATCHED 后，轮询 PREPARED/AUTHORIZED 的 Broker 将永远领不到该 effect。
 */
export function createBrokerBackedHarnessTool(
  config: BrokerBackedHarnessToolConfig,
): BrokerBackedHarnessTool {
  const fetchFn = config.fetchImpl ?? fetch;
  const base = config.baseUrl.replace(/\/$/, '');
  const host = createExecuteToolHost({
    baseUrl: base,
    authorization: config.authorization,
    fetchImpl: fetchFn,
  });
  const maxAttempts = config.poll?.maxAttempts ?? 120;
  const delayMs = config.poll?.delayMs ?? 1_000;
  const sleep =
    config.poll?.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));

  if (!Number.isInteger(maxAttempts) || maxAttempts < 1) {
    throw new Error('BROKER_TOOL_MAX_ATTEMPTS_INVALID');
  }

  // 当前 Kernel Step 合同是单链；隐藏 Harness 并行调度与该合同的形状差异。
  // 前一调用若抛异常则队列保持失败关闭，避免在未知 effect 后继续创建后继。
  let predecessorStepId: string | null = null;
  let executionTail: Promise<void> = Promise.resolve();
  const envelopeTracker = createTurnEnvelopeTracker(config.envelopeBudgets);
  // Soft/Hard 与 ForceStop 必须关同一 host.gate；禁止用孤儿 gate 装 watchdog。
  const goalKey = (config.goalId || '').trim();
  const maxNudgeBudget = config.progressGuardConfig?.maxNudgeBudget ?? 1;
  const nudgeBudgetScope =
    config.progressGuardConfig?.nudgeBudgetScope ??
    (goalKey
      ? getOrCreateNudgeBudgetScope(goalKey, maxNudgeBudget)
      : undefined);
  const banCallKeyScope =
    config.progressGuardConfig?.banCallKeyScope ??
    (goalKey ? getOrCreateBanCallKeyScope(goalKey) : undefined);
  const watchdog =
    config.watchdog ??
    createActivationWatchdog({
      ...config.watchdogConfig,
      gate: host.gate,
    });
  const progressGuard =
    config.progressGuard ??
    createNoProgressGuard({
      ...config.progressGuardConfig,
      gate: host.gate,
      maxNudgeBudget,
      ...(nudgeBudgetScope ? {nudgeBudgetScope} : {}),
      ...(banCallKeyScope ? {banCallKeyScope} : {}),
    });

  /** ForceStop：尽量落零工具总结 Artifact；≠ DONE */
  async function forceStopToolResult(
    verdict: Extract<GuardVerdict, {action: 'force_stop'}>,
    opts: {
      status: string;
      tool: string;
      argsDigest?: string;
      callId: string;
      effectId?: string;
      evidenceIds?: string[];
      /** admit 硬关闸：落盘后仍抛 TOOL_ADMISSION_CLOSED */
      throwIfHardClose?: boolean;
    },
  ): Promise<HarnessToolResult> {
    let summaryId: string | undefined;
    try {
      const persisted = await persistForceStopSummary({
        artifacts: host.artifacts,
        projectId: config.activation.projectId,
        lease: config.activation.lease,
        verdict,
        context: {
          tool: opts.tool,
          argsDigest: opts.argsDigest,
          callId: opts.callId,
        },
      });
      summaryId = persisted.artifactId;
    } catch {
      /* 落盘失败不吞 ForceStop */
    }
    if (opts.throwIfHardClose && verdict.closeToolAdmission) {
      throw new Error(
        `TOOL_ADMISSION_CLOSED:${verdict.code}:${verdict.reason}${
          summaryId ? `:summary_artifact=${summaryId}` : ''
        }`,
      );
    }
    const soft = verdict.closeToolAdmission === false;
    const softHint = soft ? '；禁止重复本调用，可换工具继续' : '';
    const hardHint =
      !soft && verdict.allowZeroToolSummary
        ? '；允许零工具总结，禁止再调工具'
        : '';
    const summaryHint = summaryId ? ` summary_artifact=${summaryId}` : '';
    const evidenceIds = [
      ...(opts.evidenceIds ?? []),
      ...(summaryId ? [summaryId] : []),
    ];
    return {
      content: [
        {
          type: 'text',
          text: `Error: ${verdict.reason}${softHint}${hardHint}${summaryHint}（≠DONE）`,
        },
      ],
      isError: true,
      error: {
        message: verdict.reason,
        info: {name: 'BrokerToolError', code: verdict.code},
      },
      meta: {
        effectId: opts.effectId ?? '',
        status: opts.status,
        evidenceIds,
        requiresReconciliation: false,
        progressGuard: {
          action: 'force_stop',
          code: verdict.code,
          marksGoalDone: false,
          closeToolAdmission: verdict.closeToolAdmission,
        },
        ...(summaryId ? {summaryArtifactId: summaryId} : {}),
      },
    };
  }

  async function readEffect(effectId: string): Promise<EffectResource> {
    const res = await fetchFn(`${base}/api/v1/effects/${effectId}`, {
      method: 'GET',
      headers: {Authorization: bearerAuthHeader(config.authorization)},
    });
    if (!res.ok) {
      throw new Error(`EFFECT_GET_FAILED: HTTP ${res.status}`);
    }
    const body = (await res.json()) as Envelope<EffectResource>;
    return body.data;
  }

  async function waitForTerminal(
    effectId: string,
    callId: string,
  ): Promise<EffectResource> {
    watchdog.enterPhase('awaiting_effect_observation', 'effect_observer');
    let last: EffectResource | undefined;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      last = await readEffect(effectId);
      // 合法长工具：每次观察到状态即算 effect_observer 进展
      watchdog.reportProgress('effect_observer', {
        toolCallId: callId,
        effectId,
      });
      const tick = watchdog.tick();
      if (tick?.kind === 'hard_idle') {
        // Hard：关准入、对账，禁止盲重试 / 假 SUCCEEDED / DONE
        return last;
      }
      if (TERMINAL_EFFECTS.has(last.status)) {
        return last;
      }
      if (attempt + 1 < maxAttempts && delayMs > 0) {
        await sleep(delayMs);
        const afterSleep = watchdog.tick();
        if (afterSleep?.kind === 'hard_idle') {
          return last;
        }
      }
    }
    if (!last) {
      throw new Error('EFFECT_POLL_EMPTY');
    }
    return last;
  }

  async function readEvidence(evidenceIds: string[]): Promise<string> {
    const parts: string[] = [];
    for (const artifactId of evidenceIds) {
      const res = await fetchFn(`${base}/api/v1/artifacts/${artifactId}/content`, {
        method: 'GET',
        headers: {Authorization: bearerAuthHeader(config.authorization)},
      });
      if (!res.ok) {
        throw new Error(`EFFECT_RESULT_ARTIFACT_GET_FAILED: HTTP ${res.status}`);
      }
      parts.push(await res.text());
    }
    return parts.join('\n');
  }

  return {
    watchdog,
    progressGuard,
    execute(call) {
      const current = executionTail.then(async (): Promise<HarnessToolResult> => {
        if (!host.gate.allowed()) {
          const closed = host.gate.closedReason();
          const verdict = forceStopVerdictFromClosedGate(
            closed,
            progressGuard.lastVerdict(),
          );
          if (verdict) {
            // 关闸先于 PUT 的崩溃窗口：补落总结 ToolResult；≠ DONE
            return forceStopToolResult(verdict, {
              status: 'ADMISSION_CLOSED',
              tool: call.name,
              callId: call.callId,
            });
          }
          throw new Error(
            `TOOL_ADMISSION_CLOSED:${closed ?? 'admission_closed'}`,
          );
        }

        watchdog.enterPhase('executing_tool', 'tool_runtime');
        watchdog.reportProgress('tool_runtime', {toolCallId: call.callId});

        let canonicalInput: string;
        let argsDigest: string;
        let concurrency: ToolCallConcurrencyDecision;
        try {
          canonicalInput = canonicalizeToolArgumentsPayload(
            call.name,
            call.arguments,
          );
          argsDigest = argsDigestOfCanonicalPayload(canonicalInput);
          // AB07：READ_ONLY 标签不授并行；分类未就绪前一律 SERIAL
          concurrency = classifyToolCallConcurrency({
            toolRef: call.name,
            argumentsJson: canonicalInput,
            replayClass: call.name === 'read_file' ? 'READ_ONLY' : null,
          });
          assertSerialOnly(concurrency);
        } catch (err) {
          const message = err instanceof Error ? err.message : 'validation_failed';
          const verdict = progressGuard.observe({
            callId: call.callId,
            tool: call.name,
            argsDigest: argsDigestOfCanonicalPayload(call.arguments || ''),
            outcome: 'VALIDATION_REJECTED',
            signals: [{kind: 'validation_rejected', value: message}],
          });
          if (verdict.action === 'force_stop') {
            return forceStopToolResult(verdict, {
              status: 'VALIDATION_REJECTED',
              tool: call.name,
              argsDigest: argsDigestOfCanonicalPayload(call.arguments || ''),
              callId: call.callId,
            });
          }
          const baseMsg = `参数校验失败: ${message}`;
          const text =
            verdict.action === 'nudge'
              ? `${baseMsg}\n[no_progress_nudge] ${verdict.message}`
              : baseMsg;
          return {
            content: [{type: 'text', text}],
            isError: true,
            error: {
              message: baseMsg,
              info: {name: 'BrokerToolError', code: 'VALIDATION_REJECTED'},
            },
            meta: {
              effectId: '',
              status: 'VALIDATION_REJECTED',
              evidenceIds: [],
              requiresReconciliation: false,
              ...(verdict.action === 'nudge'
                ? {
                    progressGuard: {
                      action: 'nudge' as const,
                      code: verdict.code,
                      marksGoalDone: false as const,
                    },
                  }
                : {}),
            },
          };
        }

        // 须与 observe 使用同一 canonicalize 后的 argsDigest，否则软禁同参对不上
        const admit = progressGuard.beforeAdmit({
          tool: call.name,
          argsDigest,
        });
        if (admit.action === 'force_stop') {
          return forceStopToolResult(admit, {
            status: 'FAILED',
            tool: call.name,
            argsDigest,
            callId: call.callId,
            throwIfHardClose: true,
          });
        }

        const concurrencyMeta = {
          class: concurrency.class,
          resourceKeys: concurrency.resourceKeys,
          reason: concurrency.reason,
          marksGoalDone: false as const,
        };

        let inputArtifact: {artifactId: string};
        let step: {stepId: string; logicalStepId: string; intentRevision: number};
        let prepared: {effectId: string; status: string};
        try {
          inputArtifact = await host.artifacts.putCollectorContent({
            projectId: config.activation.projectId,
            lease: config.activation.lease,
            body: canonicalInput,
            mime: 'application/json',
          });
          watchdog.reportProgress('tool_runtime', {toolCallId: call.callId});
          step = await host.ports.createStep({
            activityId: config.activation.activityId,
            lease: config.activation.lease,
            purpose: `tool:${call.name}:${call.callId}`,
            toolRef: call.name,
            predecessorStepId,
          });
          watchdog.reportProgress('tool_runtime', {toolCallId: call.callId});
          prepared = await host.ports.prepareEffect({
            lease: config.activation.lease,
            logicalStepId: step.logicalStepId,
            toolRef: call.name,
            inputArtifactId: inputArtifact.artifactId,
            intentRevision: step.intentRevision,
          });
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          if (isRetryableGatewayClientError(msg)) {
            return retryableGatewayErrorResult(msg, concurrency);
          }
          throw err;
        }
        watchdog.reportProgress('tool_runtime', {
          toolCallId: call.callId,
          effectId: prepared.effectId,
        });

        const effect = TERMINAL_EFFECTS.has(prepared.status)
          ? await readEffect(prepared.effectId)
          : await waitForTerminal(prepared.effectId, call.callId);
        const evidenceIds = effect.evidence_ids ?? [];

        const hard = watchdog.hardOutcome();
        if (hard) {
          return errorResult(
            effect,
            hard.code,
            `watchdog hard idle：phase=${hard.phase} owner=${hard.owner} idleMs=${hard.idleMs}（≠DONE）`,
            hard.requiresReconciliation,
            concurrency,
          );
        }

        if (!TERMINAL_EFFECTS.has(effect.status)) {
          host.gate.onHeartbeatFailure(new Error('EFFECT_UNSETTLED'));
          return errorResult(
            effect,
            'EFFECT_UNSETTLED',
            `effect ${effect.id} 仍为 ${effect.status}，必须先对账`,
            true,
            concurrency,
          );
        }
        if (effect.status === 'UNKNOWN') {
          progressGuard.observe({
            callId: call.callId,
            tool: call.name,
            argsDigest,
            outcome: 'UNKNOWN',
            signals: [{kind: 'effect_terminal', value: 'UNKNOWN'}],
          });
          host.gate.onHeartbeatFailure(new Error('EFFECT_UNKNOWN'));
          return errorResult(
            effect,
            'EFFECT_UNKNOWN',
            `effect ${effect.id} 结果未知，禁止重试`,
            true,
            concurrency,
          );
        }

        predecessorStepId = step.stepId;
        if (effect.status !== 'SUCCEEDED') {
          const verdict = progressGuard.observe({
            callId: call.callId,
            tool: call.name,
            argsDigest,
            outcome: effect.status,
            signals: [
              {kind: 'effect_terminal', value: effect.status},
              {kind: 'step_advanced', value: step.stepId},
            ],
          });
          const base = errorResult(
            effect,
            `EFFECT_${effect.status}`,
            `effect ${effect.id} 终态为 ${effect.status}`,
            false,
            concurrency,
          );
          if (verdict.action === 'force_stop') {
            return forceStopToolResult(verdict, {
              status: effect.status,
              tool: call.name,
              argsDigest,
              callId: call.callId,
              effectId: effect.id,
              evidenceIds,
            });
          }
          return base;
        }
        if (evidenceIds.length === 0) {
          return errorResult(
            effect,
            'EFFECT_RESULT_MISSING',
            `effect ${effect.id} 成功但缺少可信结果工件`,
            true,
            concurrency,
          );
        }

        const rawEvidence = await readEvidence(evidenceIds);
        const content =
          call.name === 'run_tests'
            ? formatRunTestsToolResultText(rawEvidence)
            : rawEvidence;
        const enveloped = envelopeTracker.envelopeNext({
          callId: call.callId,
          effectId: effect.id,
          evidenceIds,
          fullText: content,
        });
        const verdict = progressGuard.observe({
          callId: call.callId,
          tool: call.name,
          argsDigest,
          outcome: 'SUCCEEDED',
          signals: [
            {kind: 'artifact_digest', value: enveloped.digest},
            {kind: 'step_advanced', value: step.stepId},
            {kind: 'effect_terminal', value: 'SUCCEEDED'},
          ],
        });

        if (verdict.action === 'force_stop') {
          return forceStopToolResult(verdict, {
            status: effect.status,
            tool: call.name,
            argsDigest,
            callId: call.callId,
            effectId: effect.id,
            evidenceIds,
          });
        }

        const promptText =
          verdict.action === 'nudge'
            ? `${enveloped.promptText}\n[no_progress_nudge] ${verdict.message}`
            : enveloped.promptText;

        return {
          content: [{type: 'text', text: promptText}],
          isError: false,
          meta: {
            effectId: effect.id,
            status: effect.status,
            evidenceIds,
            requiresReconciliation: false,
            concurrency: concurrencyMeta,
            envelope: {
              spilled: enveloped.spilled,
              digest: enveloped.digest,
              runeCount: enveloped.runeCount,
              previewRunes: enveloped.previewRunes,
            },
            ...(verdict.action === 'nudge'
              ? {
                  progressGuard: {
                    action: 'nudge' as const,
                    code: verdict.code,
                    marksGoalDone: false as const,
                  },
                }
              : {}),
          },
        };
      });
      executionTail = current.then(
        () => undefined,
        (err) => {
          if (!host.gate.allowed()) return;
          const msg = err instanceof Error ? err.message : String(err);
          // 已转 ToolResult 的 400/409/422 不会进这里；其余仍失败关闭
          if (isRetryableGatewayClientError(msg)) {
            return;
          }
          host.gate.onHeartbeatFailure(err);
        },
      );
      return current;
    },
  };
}
