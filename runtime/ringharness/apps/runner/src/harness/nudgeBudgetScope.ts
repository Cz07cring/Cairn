/**
 * AB06：跨 activation 共享的 Nudge 预算账本。
 *
 * 单次 createNoProgressGuard 内的 consumed 会随 activation 重建清零；
 * 同 goal 下重复失租/重领时，须把预算接到同一 scope，否则可无限提醒。
 *
 * 进程内 registry 仍是热路径权威；可选 hydrate/persist 对接 Kernel 表，
 * 使进程重启后仍保留已消耗次数（≠ Goal DONE）。
 */

export type NudgeBudgetScope = {
  /** 已消耗 Nudge 次数（单调不减） */
  readonly consumed: number;
  readonly max: number;
  /** 尝试消耗 1；已耗尽返回 false（调用方应 ForceStop） */
  tryConsume: () => boolean;
  isExhausted: () => boolean;
};

export type NudgeBudgetPersistHooks = {
  /** 本地扣减成功后异步落 Kernel；失败不回滚本地（残差：至多多一次 Nudge） */
  persistConsume?: (consumed: number) => Promise<void>;
};

export function createNudgeBudgetScope(
  max: number,
  initialConsumed = 0,
  hooks: NudgeBudgetPersistHooks = {},
): NudgeBudgetScope {
  if (!Number.isInteger(max) || max < 1) {
    throw new Error('NUDGE_BUDGET_SCOPE_MAX_INVALID');
  }
  if (!Number.isInteger(initialConsumed) || initialConsumed < 0) {
    throw new Error('NUDGE_BUDGET_SCOPE_INITIAL_INVALID');
  }
  let consumed = Math.min(initialConsumed, max);
  return {
    get consumed() {
      return consumed;
    },
    max,
    tryConsume() {
      if (consumed >= max) {
        return false;
      }
      consumed += 1;
      if (hooks.persistConsume) {
        const snapshot = consumed;
        void hooks.persistConsume(snapshot).catch(() => {
          /* 持久失败保留本地；重启可能少计 1 */
        });
      }
      return true;
    },
    isExhausted() {
      return consumed >= max;
    },
  };
}

/** Runner 进程内按 key（通常 goal_id）复用 scope */
const registry = new Map<string, NudgeBudgetScope>();

export function getOrCreateNudgeBudgetScope(
  key: string,
  max: number,
  hooks?: NudgeBudgetPersistHooks,
): NudgeBudgetScope {
  const k = key.trim();
  if (!k) {
    throw new Error('NUDGE_BUDGET_SCOPE_KEY_EMPTY');
  }
  const existing = registry.get(k);
  if (existing !== undefined) {
    if (existing.max !== max) {
      // 合同变更：保留已消耗，改用新上界（钳制 consumed）
      const next = createNudgeBudgetScope(max, existing.consumed, hooks);
      registry.set(k, next);
      return next;
    }
    return existing;
  }
  const created = createNudgeBudgetScope(max, 0, hooks);
  registry.set(k, created);
  return created;
}

/**
 * 用 Kernel 快照替换进程内账本（进程重启恢复）；随后 tryConsume 可继续 persist。
 */
export function replaceNudgeBudgetScope(
  key: string,
  max: number,
  initialConsumed: number,
  hooks?: NudgeBudgetPersistHooks,
): NudgeBudgetScope {
  const k = key.trim();
  if (!k) {
    throw new Error('NUDGE_BUDGET_SCOPE_KEY_EMPTY');
  }
  const next = createNudgeBudgetScope(max, initialConsumed, hooks);
  registry.set(k, next);
  return next;
}

/**
 * 从 Kernel hydrate 后写入 registry；返回可与 createDefaultActivationGuards 共用的 scope。
 */
export async function hydrateNudgeBudgetScopeFromKernel(input: {
  goalId: string;
  max: number;
  fetchConsumed: () => Promise<number>;
  persistConsume?: (consumed: number) => Promise<void>;
}): Promise<NudgeBudgetScope> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('NUDGE_BUDGET_SCOPE_KEY_EMPTY');
  }
  let consumed = 0;
  try {
    consumed = await input.fetchConsumed();
  } catch {
    // hydrate 失败：保守从 0 开始（残差：可能多一次 Nudge）；不假装已耗尽
    consumed = 0;
  }
  if (!Number.isInteger(consumed) || consumed < 0) {
    consumed = 0;
  }
  return replaceNudgeBudgetScope(goalId, input.max, consumed, {
    persistConsume: input.persistConsume,
  });
}

/** 测试专用：清空进程内账本 */
export function resetNudgeBudgetScopeRegistryForTests(): void {
  registry.clear();
}
