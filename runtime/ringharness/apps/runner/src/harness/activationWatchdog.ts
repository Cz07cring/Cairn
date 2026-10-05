/**
 * 阶段化 Activation Watchdog（M3.5）。
 *
 * 超时绑定显式阶段与等待所有者；Soft Idle 仅可见性（每 phase epoch 一次），
 * Hard Idle 类型化取消并关闭工具准入。不得因超时自动产生新 Effect 或写 Goal DONE。
 */

import type {ToolAdmissionGate} from './brokerToolHttpPorts.js';

export type TurnPhase =
  | 'setup'
  | 'awaiting_llm'
  | 'streaming_llm'
  | 'executing_tool'
  | 'awaiting_effect_observation'
  | 'awaiting_approval'
  | 'injecting_stop'
  | 'finalizing';

/** 等待所有者：只有当前阶段声明的 owner 的 progress 才能重置该阶段时钟。 */
export type WatchdogOwner =
  | 'runner_setup'
  | 'llm_provider'
  | 'llm_stream'
  | 'tool_runtime'
  | 'effect_observer'
  | 'approval'
  | 'stop_seam'
  | 'finalizer';

export type PhaseIdleBudget = {
  softIdleMs: number;
  hardIdleMs: number;
};

export type WatchdogBudgets = Record<TurnPhase, PhaseIdleBudget>;

/** 开发/单测默认；生产可由配置覆盖。hard ≥ soft。 */
export const DEFAULT_WATCHDOG_BUDGETS: WatchdogBudgets = {
  setup: {softIdleMs: 5_000, hardIdleMs: 30_000},
  awaiting_llm: {softIdleMs: 15_000, hardIdleMs: 60_000},
  streaming_llm: {softIdleMs: 10_000, hardIdleMs: 45_000},
  executing_tool: {softIdleMs: 30_000, hardIdleMs: 300_000},
  awaiting_effect_observation: {softIdleMs: 20_000, hardIdleMs: 180_000},
  // 审批等待：Soft 可见，Hard 故意拉长，避免把「等人」当卡死
  awaiting_approval: {softIdleMs: 60_000, hardIdleMs: 3_600_000},
  injecting_stop: {softIdleMs: 5_000, hardIdleMs: 60_000},
  finalizing: {softIdleMs: 5_000, hardIdleMs: 30_000},
};

export type WatchdogHeartbeatDetails = {
  phase: TurnPhase;
  phase_epoch: number;
  phase_started_at_ms: number;
  last_progress_at_ms: number;
  owner: WatchdogOwner;
  tool_call_id: string | null;
  effect_id: string | null;
  soft_idle_fired: boolean;
  hard_idle_fired: boolean;
};

export type SoftIdleEvent = {
  kind: 'soft_idle';
  phase: TurnPhase;
  phaseEpoch: number;
  owner: WatchdogOwner;
  idleMs: number;
  /** Soft 不取消、不关准入 */
  closesAdmission: false;
  marksGoalDone: false;
};

export type HardIdleOutcome = {
  kind: 'hard_idle';
  code: 'WATCHDOG_HARD_IDLE';
  phase: TurnPhase;
  phaseEpoch: number;
  owner: WatchdogOwner;
  idleMs: number;
  closesAdmission: true;
  /** 红线：超时绝不是 Goal DONE */
  marksGoalDone: false;
  /** 若观察中断，须对账而非盲重试 */
  requiresReconciliation: boolean;
};

export type WatchdogTickResult = SoftIdleEvent | HardIdleOutcome | null;

export type ActivationWatchdog = {
  enterPhase: (phase: TurnPhase, owner: WatchdogOwner) => void;
  reportProgress: (
    owner: WatchdogOwner,
    detail?: {toolCallId?: string; effectId?: string},
  ) => void;
  /** 推进时钟；返回 Soft（每 epoch 至多一次）或 Hard（永久）。 */
  tick: (nowMs?: number) => WatchdogTickResult;
  heartbeatDetails: () => WatchdogHeartbeatDetails;
  hardOutcome: () => HardIdleOutcome | null;
  softEvents: () => readonly SoftIdleEvent[];
};

export type ActivationWatchdogConfig = {
  budgets?: Partial<{[K in TurnPhase]: Partial<PhaseIdleBudget>}>;
  /** 可注入假时钟（单测 time-skipping） */
  now?: () => number;
  /** Hard 时关闭工具准入；不自动 dispatch / 不写 DONE */
  gate?: ToolAdmissionGate;
};

function mergeBudgets(
  partial?: ActivationWatchdogConfig['budgets'],
): WatchdogBudgets {
  const out = {...DEFAULT_WATCHDOG_BUDGETS};
  if (!partial) {
    return out;
  }
  for (const phase of Object.keys(partial) as TurnPhase[]) {
    const patch = partial[phase];
    if (!patch) {
      continue;
    }
    out[phase] = {
      softIdleMs: patch.softIdleMs ?? out[phase].softIdleMs,
      hardIdleMs: patch.hardIdleMs ?? out[phase].hardIdleMs,
    };
  }
  return out;
}

function assertBudgets(budgets: WatchdogBudgets): void {
  for (const [phase, b] of Object.entries(budgets) as Array<
    [TurnPhase, PhaseIdleBudget]
  >) {
    if (!Number.isFinite(b.softIdleMs) || b.softIdleMs < 0) {
      throw new Error(`WATCHDOG_BUDGET_INVALID:${phase}:soft`);
    }
    if (!Number.isFinite(b.hardIdleMs) || b.hardIdleMs < b.softIdleMs) {
      throw new Error(`WATCHDOG_BUDGET_INVALID:${phase}:hard`);
    }
  }
}

export function createActivationWatchdog(
  config: ActivationWatchdogConfig = {},
): ActivationWatchdog {
  const budgets = mergeBudgets(config.budgets);
  assertBudgets(budgets);
  const nowFn = config.now ?? (() => Date.now());

  let phase: TurnPhase = 'setup';
  let owner: WatchdogOwner = 'runner_setup';
  let phaseEpoch = 0;
  let phaseStartedAt = nowFn();
  let lastProgressAt = phaseStartedAt;
  let softIdleFired = false;
  let hard: HardIdleOutcome | null = null;
  const softLog: SoftIdleEvent[] = [];
  let toolCallId: string | null = null;
  let effectId: string | null = null;

  return {
    enterPhase(nextPhase, nextOwner) {
      if (hard) {
        return;
      }
      phase = nextPhase;
      owner = nextOwner;
      phaseEpoch += 1;
      const t = nowFn();
      phaseStartedAt = t;
      lastProgressAt = t;
      softIdleFired = false;
      toolCallId = null;
      effectId = null;
    },

    reportProgress(progressOwner, detail) {
      if (hard) {
        return;
      }
      // 不同所有者的活动不能互相重置时钟
      if (progressOwner !== owner) {
        return;
      }
      lastProgressAt = nowFn();
      if (detail?.toolCallId !== undefined) {
        toolCallId = detail.toolCallId;
      }
      if (detail?.effectId !== undefined) {
        effectId = detail.effectId;
      }
    },

    tick(nowMs) {
      if (hard) {
        return hard;
      }
      const t = nowMs ?? nowFn();
      const idleMs = Math.max(0, t - lastProgressAt);
      const budget = budgets[phase];

      if (idleMs >= budget.hardIdleMs) {
        hard = {
          kind: 'hard_idle',
          code: 'WATCHDOG_HARD_IDLE',
          phase,
          phaseEpoch,
          owner,
          idleMs,
          closesAdmission: true,
          marksGoalDone: false,
          requiresReconciliation:
            phase === 'awaiting_effect_observation' ||
            phase === 'executing_tool',
        };
        config.gate?.onHeartbeatFailure(
          new Error(
            `WATCHDOG_HARD_IDLE:${phase}:epoch=${phaseEpoch}:idleMs=${idleMs}`,
          ),
        );
        return hard;
      }

      if (!softIdleFired && idleMs >= budget.softIdleMs) {
        softIdleFired = true;
        const soft: SoftIdleEvent = {
          kind: 'soft_idle',
          phase,
          phaseEpoch,
          owner,
          idleMs,
          closesAdmission: false,
          marksGoalDone: false,
        };
        softLog.push(soft);
        return soft;
      }

      return null;
    },

    heartbeatDetails() {
      return {
        phase,
        phase_epoch: phaseEpoch,
        phase_started_at_ms: phaseStartedAt,
        last_progress_at_ms: lastProgressAt,
        owner,
        tool_call_id: toolCallId,
        effect_id: effectId,
        soft_idle_fired: softIdleFired,
        hard_idle_fired: hard !== null,
      };
    },

    hardOutcome: () => hard,
    softEvents: () => softLog,
  };
}

/** Hard Idle 抛出；调用方须对账/关闸，禁止当作 Goal DONE。 */
export class WatchdogHardIdleError extends Error {
  readonly outcome: HardIdleOutcome;
  /** AB05：SSE 流间隙 Hard 时已收到的部分助手文本（可落 Artifact；≠ DONE） */
  readonly partialAssistantText?: string;

  constructor(outcome: HardIdleOutcome, partialAssistantText?: string) {
    super(
      `WATCHDOG_HARD_IDLE:${outcome.phase}:epoch=${outcome.phaseEpoch}:idleMs=${outcome.idleMs}`,
    );
    this.name = 'WatchdogHardIdleError';
    this.outcome = outcome;
    if (partialAssistantText !== undefined) {
      this.partialAssistantText = partialAssistantText;
    }
  }
}

/**
 * 在等待 Promise 期间周期性 tick；Hard 则拒绝（不取消底层 Promise，由调用方停后续副作用）。
 * 可注入 setInterval 以便假时钟单测。
 */
export async function awaitWithWatchdog<T>(
  watchdog: ActivationWatchdog,
  work: Promise<T>,
  opts?: {
    tickEveryMs?: number;
    setIntervalFn?: typeof setInterval;
    clearIntervalFn?: typeof clearInterval;
    /** Hard 时附带部分文本（SSE 流间隙） */
    partialText?: () => string | undefined;
  },
): Promise<T> {
  const tickEveryMs = opts?.tickEveryMs ?? 250;
  if (!Number.isFinite(tickEveryMs) || tickEveryMs < 1) {
    throw new Error('WATCHDOG_TICK_EVERY_MS_INVALID');
  }
  const setI = opts?.setIntervalFn ?? setInterval;
  const clearI = opts?.clearIntervalFn ?? clearInterval;

  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = setI(() => {
      if (settled) {
        return;
      }
      const result = watchdog.tick();
      if (result?.kind === 'hard_idle') {
        settled = true;
        clearI(timer);
        reject(new WatchdogHardIdleError(result, opts?.partialText?.()));
      }
    }, tickEveryMs);
    // Node Timeout
    (timer as {unref?: () => void}).unref?.();

    work.then(
      (value) => {
        if (settled) {
          return;
        }
        settled = true;
        clearI(timer);
        resolve(value);
      },
      (err: unknown) => {
        if (settled) {
          return;
        }
        settled = true;
        clearI(timer);
        reject(err);
      },
    );
  });
}

/** 流式产出间隙：reportProgress 后若已 Hard 则抛。 */
export function throwIfWatchdogHard(watchdog: ActivationWatchdog): void {
  const existing = watchdog.hardOutcome();
  if (existing) {
    throw new WatchdogHardIdleError(existing);
  }
  const tick = watchdog.tick();
  if (tick?.kind === 'hard_idle') {
    throw new WatchdogHardIdleError(tick);
  }
}
