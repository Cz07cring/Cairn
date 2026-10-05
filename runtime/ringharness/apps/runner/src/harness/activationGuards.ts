/**
 * RunActivation 默认装配的阶段钟 + 无进展守卫。
 * Soft/Hard 与 ForceStop 均 ≠ Goal DONE；不替代 Kernel heartbeat 合同字段。
 */
import {
  createActivationWatchdog,
  type ActivationWatchdog,
} from './activationWatchdog.js';
import type {ToolAdmissionGate} from './brokerToolHttpPorts.js';
import {
  createNoProgressGuard,
  type NoProgressGuard,
} from './noProgressGuard.js';
import {
  getOrCreateBanCallKeyScope,
  hydrateBanCallKeyScopeFromKernel,
  type BanCallKeyScope,
} from './banCallKeyScope.js';
import {
  addGoalBanCallKey,
  fetchGoalBanCallKeys,
} from './banCallKeyKernel.js';
import {
  getOrCreateNudgeBudgetScope,
  hydrateNudgeBudgetScopeFromKernel,
  type NudgeBudgetScope,
} from './nudgeBudgetScope.js';
import {
  consumeGoalNudgeBudget,
  fetchGoalNudgeBudget,
  type NudgeBudgetKernelPorts,
} from './nudgeBudgetKernel.js';

export type ActivationGuards = {
  watchdog: ActivationWatchdog;
  progressGuard: NoProgressGuard;
};

export type ActivationGuardsOptions = {
  /**
   * 同 Goal 跨 activation 共享 Nudge 预算与软禁同参键（AB06）。
   * 失租重领后新建 guard 仍扣同一账 / 同禁令，防止无限提醒与同参风暴。
   */
  goalId?: string;
  maxNudgeBudget?: number;
  /** 可选：预 hydrate 的 scope（通常来自 Kernel） */
  nudgeBudgetScope?: NudgeBudgetScope;
  banCallKeyScope?: BanCallKeyScope;
};

export function createDefaultActivationGuards(
  gate: ToolAdmissionGate,
  options: ActivationGuardsOptions = {},
): ActivationGuards {
  const maxNudgeBudget = options.maxNudgeBudget ?? 1;
  const goalKey = (options.goalId || '').trim();
  const nudgeBudgetScope =
    options.nudgeBudgetScope ??
    (goalKey
      ? getOrCreateNudgeBudgetScope(goalKey, maxNudgeBudget)
      : undefined);
  const banCallKeyScope =
    options.banCallKeyScope ??
    (goalKey ? getOrCreateBanCallKeyScope(goalKey) : undefined);
  return {
    watchdog: createActivationWatchdog({gate}),
    progressGuard: createNoProgressGuard({
      gate,
      maxNudgeBudget,
      nudgeBudgetScope,
      banCallKeyScope,
    }),
  };
}

/**
 * 先从 Kernel hydrate Nudge 预算与软禁键，再装配 guards（进程重启可恢复）；≠ DONE。
 */
export async function createDefaultActivationGuardsHydrated(
  gate: ToolAdmissionGate,
  options: ActivationGuardsOptions & {
    kernel?: NudgeBudgetKernelPorts;
  },
): Promise<ActivationGuards> {
  const maxNudgeBudget = options.maxNudgeBudget ?? 1;
  const goalKey = (options.goalId || '').trim();
  let nudgeBudgetScope = options.nudgeBudgetScope;
  let banCallKeyScope = options.banCallKeyScope;
  if (goalKey && options.kernel) {
    const ports = options.kernel;
    if (!nudgeBudgetScope) {
      nudgeBudgetScope = await hydrateNudgeBudgetScopeFromKernel({
        goalId: goalKey,
        max: maxNudgeBudget,
        fetchConsumed: async () => {
          const snap = await fetchGoalNudgeBudget(
            ports,
            goalKey,
            maxNudgeBudget,
          );
          return snap.consumed;
        },
        persistConsume: async () => {
          await consumeGoalNudgeBudget(ports, goalKey, maxNudgeBudget);
        },
      });
    }
    if (!banCallKeyScope) {
      banCallKeyScope = await hydrateBanCallKeyScopeFromKernel({
        goalId: goalKey,
        fetchKeys: async () => {
          const snap = await fetchGoalBanCallKeys(ports, goalKey);
          return snap.callKeys;
        },
        persistAdd: async (key) => {
          await addGoalBanCallKey(ports, goalKey, key);
        },
      });
    }
  }
  return createDefaultActivationGuards(gate, {
    ...options,
    ...(nudgeBudgetScope ? {nudgeBudgetScope} : {}),
    ...(banCallKeyScope ? {banCallKeyScope} : {}),
  });
}
