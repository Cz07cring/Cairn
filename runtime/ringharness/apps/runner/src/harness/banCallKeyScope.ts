/**
 * AB06：跨 activation 共享的软禁同参键集合。
 *
 * 软 ForceStop 写入的 bannedCallKeys 若仅存于单次 guard，失租重领会清零，
 * 同参风暴可再开。同 goal 须复用本 scope；可选 hydrate/persist 对接 Kernel。
 * ≠ Goal DONE。
 */

export type BanCallKeyScope = {
  has: (key: string) => boolean;
  add: (key: string) => void;
  /** 已禁键数量（观测用） */
  size: () => number;
};

export type BanCallKeyPersistHooks = {
  /** 本地 add 成功后异步落 Kernel；失败不回滚本地 */
  persistAdd?: (key: string) => Promise<void>;
};

export function createBanCallKeyScope(
  initialKeys: Iterable<string> = [],
  hooks: BanCallKeyPersistHooks = {},
): BanCallKeyScope {
  const keys = new Set<string>();
  for (const k of initialKeys) {
    const t = k.trim();
    if (t) {
      keys.add(t);
    }
  }
  return {
    has(key) {
      return keys.has(key);
    },
    add(key) {
      const t = key.trim();
      if (!t) {
        return;
      }
      if (keys.has(t)) {
        return;
      }
      keys.add(t);
      if (hooks.persistAdd) {
        void hooks.persistAdd(t).catch(() => {
          /* 持久失败保留本地；重启可能少一条禁令 */
        });
      }
    },
    size() {
      return keys.size;
    },
  };
}

const registry = new Map<string, BanCallKeyScope>();

export function getOrCreateBanCallKeyScope(
  key: string,
  hooks?: BanCallKeyPersistHooks,
): BanCallKeyScope {
  const k = key.trim();
  if (!k) {
    throw new Error('BAN_CALL_KEY_SCOPE_KEY_EMPTY');
  }
  const existing = registry.get(k);
  if (existing !== undefined) {
    return existing;
  }
  const created = createBanCallKeyScope([], hooks);
  registry.set(k, created);
  return created;
}

/** 用 Kernel 快照替换进程内禁令账本（进程重启恢复）。 */
export function replaceBanCallKeyScope(
  key: string,
  initialKeys: Iterable<string>,
  hooks?: BanCallKeyPersistHooks,
): BanCallKeyScope {
  const k = key.trim();
  if (!k) {
    throw new Error('BAN_CALL_KEY_SCOPE_KEY_EMPTY');
  }
  const next = createBanCallKeyScope(initialKeys, hooks);
  registry.set(k, next);
  return next;
}

/** 从 Kernel hydrate 后写入 registry。 */
export async function hydrateBanCallKeyScopeFromKernel(input: {
  goalId: string;
  fetchKeys: () => Promise<string[]>;
  persistAdd?: (key: string) => Promise<void>;
}): Promise<BanCallKeyScope> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('BAN_CALL_KEY_SCOPE_KEY_EMPTY');
  }
  let keys: string[] = [];
  try {
    keys = await input.fetchKeys();
  } catch {
    keys = [];
  }
  if (!Array.isArray(keys)) {
    keys = [];
  }
  return replaceBanCallKeyScope(goalId, keys, {
    persistAdd: input.persistAdd,
  });
}

/** 测试专用：清空进程内禁令账本 */
export function resetBanCallKeyScopeRegistryForTests(): void {
  registry.clear();
}
