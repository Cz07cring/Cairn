/**
 * No-Progress Guard（M3.5-C）：确定性进展滑窗 + 一次 Nudge + ForceStop。
 *
 * 进展只认 Artifact digest / 可信终态 / Step 推进等信号；模型自述不算。
 * ForceStop 关闭工具准入，可允许一次零工具总结，但 ≠ Audit PASS / Goal DONE。
 */

import {contentDigestSha256} from './artifactPutHttpPorts.js';
import type {ToolAdmissionGate} from './brokerToolHttpPorts.js';
import type {BanCallKeyScope} from './banCallKeyScope.js';
import type {NudgeBudgetScope} from './nudgeBudgetScope.js';

export type ProgressSignalKind =
  | 'artifact_digest'
  | 'effect_terminal'
  | 'step_advanced'
  | 'validation_rejected';

export type ProgressSignal = {
  kind: ProgressSignalKind;
  /** artifact / evidence 内容 digest；effect 终态可用 status 串 */
  value: string;
};

export type ToolObservation = {
  callId: string;
  tool: string;
  /** 规范化参数字节的 content digest */
  argsDigest: string;
  outcome: 'SUCCEEDED' | 'FAILED' | 'UNKNOWN' | 'VALIDATION_REJECTED' | string;
  signals: ProgressSignal[];
};

export type GuardVerdict =
  | {action: 'continue'; marksGoalDone: false}
  | {
      action: 'nudge';
      code: 'NO_PROGRESS_NUDGE';
      message: string;
      nudgeCount: number;
      marksGoalDone: false;
    }
  | {
      action: 'force_stop';
      code: 'NO_PROGRESS_FORCE_STOP';
      reason: string;
      /**
       * true：关全闸（UNKNOWN / Nudge 预算耗尽）。
       * false：仅禁止被钉扎的同参调用，其它工具仍可准入（避免 diagnose 读循环后无法 write）。
       */
      closeToolAdmission: boolean;
      allowZeroToolSummary: true;
      marksGoalDone: false;
    };

export type NoProgressGuardConfig = {
  /** 滑窗长度（观察条数） */
  windowSize?: number;
  /** 相同成功签名连续次数触发 Nudge（含当前） */
  repeatSuccessBeforeNudge?: number;
  /** Nudge 后再遇相同无进展签名 → ForceStop */
  forceStopAfterNudge?: boolean;
  /** 校验失败更宽容：达此次数才 Nudge */
  repeatValidationBeforeNudge?: number;
  /**
   * Nudge 预算上限（单位=次数）。每次 Nudge 消耗 1；耗尽后再触发 Nudge 条件则 ForceStop。
   * 防止「无限提醒」；默认 1（与一次 Nudge 后停一致）。
   * 若同时传入 nudgeBudgetScope，则以 scope.max 为准（本字段忽略）。
   */
  maxNudgeBudget?: number;
  /**
   * 跨 activation 共享账本（AB06）。同 goal 重领时须注入同一 scope，
   * 否则每次新建 guard 会重置 consumed，可无限提醒。
   */
  nudgeBudgetScope?: NudgeBudgetScope;
  /**
   * 跨 activation 共享软禁同参键（AB06）。
   * 未注入时仅本 guard 进程局部 Set（失租重领会丢）。
   */
  banCallKeyScope?: BanCallKeyScope;
  gate?: ToolAdmissionGate;
};

export type NoProgressGuard = {
  beforeAdmit: (proposed: {tool: string; argsDigest: string}) => GuardVerdict;
  observe: (obs: ToolObservation) => GuardVerdict;
  metrics: () => {
    nudgeCount: number;
    forceStopped: boolean;
    windowSize: number;
    observations: number;
    /** 已消耗的 Nudge 预算单位 */
    nudgeBudgetConsumed: number;
    /** Nudge 预算上限 */
    nudgeBudgetMax: number;
    /** 软禁同参键数量（含跨 activation scope） */
    bannedCallKeyCount: number;
  };
  lastVerdict: () => GuardVerdict;
};

const DEFAULTS = {
  windowSize: 8,
  repeatSuccessBeforeNudge: 2,
  forceStopAfterNudge: true,
  repeatValidationBeforeNudge: 3,
  maxNudgeBudget: 1,
} as const;

function signatureOf(tool: string, argsDigest: string, outcome: string): string {
  return `${tool}|${argsDigest}|${outcome}`;
}

function hasFreshProgress(
  signals: ProgressSignal[],
  seenDigests: Set<string>,
): boolean {
  // Step 每调必增，不能单独解除「同参重复」；须有新 Artifact digest 等可信字节变化
  for (const s of signals) {
    if (s.kind === 'artifact_digest') {
      const key = `${s.kind}:${s.value}`;
      if (!seenDigests.has(key)) {
        return true;
      }
    }
  }
  return false;
}

function rememberSignals(
  signals: ProgressSignal[],
  seenDigests: Set<string>,
): void {
  for (const s of signals) {
    if (s.kind === 'artifact_digest') {
      seenDigests.add(`${s.kind}:${s.value}`);
    }
  }
}

export function argsDigestOfCanonicalPayload(canonical: string): string {
  return contentDigestSha256(canonical);
}

export function createNoProgressGuard(
  config: NoProgressGuardConfig = {},
): NoProgressGuard {
  const windowSize = config.windowSize ?? DEFAULTS.windowSize;
  const repeatSuccessBeforeNudge =
    config.repeatSuccessBeforeNudge ?? DEFAULTS.repeatSuccessBeforeNudge;
  const forceStopAfterNudge =
    config.forceStopAfterNudge ?? DEFAULTS.forceStopAfterNudge;
  const repeatValidationBeforeNudge =
    config.repeatValidationBeforeNudge ?? DEFAULTS.repeatValidationBeforeNudge;
  const budgetScope = config.nudgeBudgetScope;
  const maxNudgeBudget =
    budgetScope?.max ?? config.maxNudgeBudget ?? DEFAULTS.maxNudgeBudget;

  if (!Number.isInteger(windowSize) || windowSize < 2) {
    throw new Error('NO_PROGRESS_WINDOW_INVALID');
  }
  if (!Number.isInteger(maxNudgeBudget) || maxNudgeBudget < 1) {
    throw new Error('NO_PROGRESS_NUDGE_BUDGET_INVALID');
  }

  const window: ToolObservation[] = [];
  const seenProgressKeys = new Set<string>();
  /**
   * 同签名累计（非仅连续尾部）。Issue #68：交替 A/B 读或中间插入 run_tests
   * 时 countTrailingSame 恒为 1，守卫盲区导致数十轮空转。
   * 键 = tool|argsDigest|outcome。
   */
  const signatureCounts = new Map<string, number>();
  let nudgeCount = 0;
  /** 无共享账本时本地累计；有 scope 时以 scope.consumed 为准 */
  let localNudgeBudgetConsumed = 0;
  /** 硬停：全闸关闭（UNKNOWN / Nudge 预算耗尽） */
  let forceStopped = false;
  let last: GuardVerdict = {action: 'continue', marksGoalDone: false};
  /** Nudge 时钉住的无进展签名；再次命中则软 ForceStop（禁同参，不关全闸） */
  let nudgedSignature: string | null = null;
  /** 无共享 scope 时的局部禁令；有 banCallKeyScope 时以 scope 为准 */
  const localBannedCallKeys = new Set<string>();
  const banScope = config.banCallKeyScope;

  function nudgeBudgetConsumed(): number {
    return budgetScope ? budgetScope.consumed : localNudgeBudgetConsumed;
  }

  function callKeyOf(tool: string, argsDigest: string): string {
    return `${tool}|${argsDigest}`;
  }

  function isBanned(key: string): boolean {
    return banScope ? banScope.has(key) : localBannedCallKeys.has(key);
  }

  function banKey(key: string): void {
    if (banScope) {
      banScope.add(key);
    } else {
      localBannedCallKeys.add(key);
    }
  }

  function bannedCount(): number {
    return banScope ? banScope.size() : localBannedCallKeys.size;
  }

  function forceStop(
    reason: string,
    opts: {closeAdmission?: boolean; banCallKey?: string} = {},
  ): GuardVerdict {
    const closeAdmission = opts.closeAdmission !== false;
    if (opts.banCallKey) {
      banKey(opts.banCallKey);
    }
    if (closeAdmission) {
      forceStopped = true;
      config.gate?.onHeartbeatFailure(new Error(`NO_PROGRESS_FORCE_STOP:${reason}`));
    }
    last = {
      action: 'force_stop',
      code: 'NO_PROGRESS_FORCE_STOP',
      reason,
      closeToolAdmission: closeAdmission,
      allowZeroToolSummary: true,
      marksGoalDone: false,
    };
    return last;
  }

  function nudge(message: string, signature: string): GuardVerdict {
    // 预算耗尽：不得再发 Nudge，直接 ForceStop（AB06）
    if (budgetScope) {
      if (!budgetScope.tryConsume()) {
        return forceStop(`nudge_budget_exhausted:${signature}`);
      }
    } else {
      if (localNudgeBudgetConsumed >= maxNudgeBudget) {
        return forceStop(`nudge_budget_exhausted:${signature}`);
      }
      localNudgeBudgetConsumed += 1;
    }
    nudgeCount += 1;
    nudgedSignature = signature;
    last = {
      action: 'nudge',
      code: 'NO_PROGRESS_NUDGE',
      message,
      nudgeCount,
      marksGoalDone: false,
    };
    return last;
  }

  function bumpSignatureCount(sig: string, resetToOne: boolean): number {
    if (resetToOne) {
      signatureCounts.set(sig, 1);
      return 1;
    }
    const next = (signatureCounts.get(sig) ?? 0) + 1;
    signatureCounts.set(sig, next);
    return next;
  }

  function evaluateAfterObserve(obs: ToolObservation): GuardVerdict {
    if (forceStopped) {
      return last;
    }
    if (obs.outcome === 'UNKNOWN') {
      return forceStop(`effect_unknown:${obs.callId}`);
    }

    const sig = signatureOf(obs.tool, obs.argsDigest, obs.outcome);
    const fresh = hasFreshProgress(obs.signals, seenProgressKeys);
    rememberSignals(obs.signals, seenProgressKeys);

    if (fresh) {
      // 真实进展：本签名计数重置为 1（允许再读「新内容」一次）；清 Nudge 钉扎
      bumpSignatureCount(sig, true);
      nudgedSignature = null;
      last = {action: 'continue', marksGoalDone: false};
      return last;
    }

    const repeats = bumpSignatureCount(sig, false);

    // VALIDATION_REJECTED 必须先于通用「Nudge 后再同签」分支：
    // 否则空参循环会落成 repeat_after_nudge，掩盖 validation_loop 运营签名。
    if (obs.outcome === 'VALIDATION_REJECTED') {
      if (repeats >= repeatValidationBeforeNudge && nudgeCount === 0) {
        return nudge(
          '连续参数校验失败且无新进展；请改参数或换工具，勿重复同一非法调用。',
          sig,
        );
      }
      if (
        repeats >= repeatValidationBeforeNudge &&
        nudgeCount >= 1 &&
        forceStopAfterNudge
      ) {
        // 与成功路径软停一致：禁同参，不关全闸 —— 空参 write 循环不得堵死 seal_candidate
        return forceStop(`validation_loop:${sig}`, {
          closeAdmission: false,
          banCallKey: callKeyOf(obs.tool, obs.argsDigest),
        });
      }
      last = {action: 'continue', marksGoalDone: false};
      return last;
    }

    // 已 Nudge 过的同一无进展签名 → 软 ForceStop：禁同参，不关全闸（可换工具写修复）
    if (
      forceStopAfterNudge &&
      nudgedSignature !== null &&
      sig === nudgedSignature &&
      nudgeCount >= 1
    ) {
      return forceStop(`repeat_after_nudge:${sig}`, {
        closeAdmission: false,
        banCallKey: callKeyOf(obs.tool, obs.argsDigest),
      });
    }

    // 成功或失败但无新 digest：同签名累计达阈值（含非连续，Issue #68）
    if (repeats >= repeatSuccessBeforeNudge && nudgeCount === 0) {
      return nudge(
        '检测到重复工具调用且无新 Artifact/Step 进展；禁止再次调用相同工具与相同参数，请换路径或改写后继续（勿重复打开同一文件）。',
        sig,
      );
    }
    // 显式关闭「Nudge 后即停」时：继续消耗 Nudge 预算，耗尽则 ForceStop（AB06）
    if (
      repeats >= repeatSuccessBeforeNudge &&
      nudgeCount >= 1 &&
      !forceStopAfterNudge
    ) {
      return nudge(
        '仍无进展；Nudge 预算继续扣减，耗尽将强制停止工具准入（≠DONE）。',
        sig,
      );
    }

    last = {action: 'continue', marksGoalDone: false};
    return last;
  }

  return {
    beforeAdmit(proposed) {
      if (forceStopped) {
        return last.action === 'force_stop'
          ? last
          : forceStop('already_force_stopped');
      }
      const key = callKeyOf(proposed.tool, proposed.argsDigest);
      if (isBanned(key)) {
        // 同参已软拒绝：不再准入该调用；全闸仍开，换工具可写
        last = {
          action: 'force_stop',
          code: 'NO_PROGRESS_FORCE_STOP',
          reason: `repeat_banned:${key}`,
          closeToolAdmission: false,
          allowZeroToolSummary: true,
          marksGoalDone: false,
        };
        return last;
      }
      // Nudge / 软 ForceStop 不阻断其它工具；硬停才关闸
      last = {action: 'continue', marksGoalDone: false};
      return last;
    },

    observe(obs) {
      window.push(obs);
      while (window.length > windowSize) {
        window.shift();
      }
      return evaluateAfterObserve(obs);
    },

    metrics: () => ({
      nudgeCount,
      forceStopped,
      windowSize,
      observations: window.length,
      nudgeBudgetConsumed: nudgeBudgetConsumed(),
      nudgeBudgetMax: maxNudgeBudget,
      bannedCallKeyCount: bannedCount(),
    }),

    lastVerdict: () => last,
  };
}
