/**
 * Harness 运行时注册表：插拔驱动 + 旁路目录热切换。
 *
 * 环境变量：
 * - RING_HARNESS_RUNTIME：驱动 id（默认 deepseek-official-agent-loop）
 * - RING_HARNESS_CHECKOUT / RING_HARNESS_PIN：当前激活树与 SHA
 * - RING_HARNESS_PINS_ROOT：旁路 pins 根（默认 .runtime/harness-pins）
 *
 * 热更新正确姿势：pull 新 SHA 到独立目录 → hotSwapHarnessRuntime（换路径）。
 * 禁止同路径 query cache-bust（ESM 双实例会丢工具注册）。
 */
import {deepseekOfficialRuntimeFactory} from './deepseekOfficialRuntimePlugin.js';
import {
  HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
  type HarnessRuntimeCreateOptions,
  type HarnessRuntimeFactory,
  type HarnessRuntimeId,
  type HarnessRuntimePlugin,
} from './harnessRuntimePlugin.js';
import {resolveHarnessPin} from './pin.js';

export type HarnessRuntimeRegistry = {
  register(factory: HarnessRuntimeFactory): void;
  listIds(): HarnessRuntimeId[];
  resolve(options?: HarnessRuntimeCreateOptions & {runtimeId?: string}): HarnessRuntimePlugin;
  current(): HarnessRuntimePlugin | null;
  /** dispose 旧实例 → generation++ → 用新 checkout/pin 重建 */
  reload(
    options?: HarnessRuntimeCreateOptions & {runtimeId?: string},
  ): HarnessRuntimePlugin;
};

export function createHarnessRuntimeRegistry(
  seed: HarnessRuntimeFactory[] = [deepseekOfficialRuntimeFactory],
): HarnessRuntimeRegistry {
  const factories = new Map<HarnessRuntimeId, HarnessRuntimeFactory>();
  for (const f of seed) {
    factories.set(f.id, f);
  }
  let active: HarnessRuntimePlugin | null = null;
  let generation = 0;

  function resolveId(explicit?: string): HarnessRuntimeId {
    return (
      explicit ??
      process.env.RING_HARNESS_RUNTIME?.trim() ??
      HARNESS_RUNTIME_DEEPSEEK_OFFICIAL
    );
  }

  const registry: HarnessRuntimeRegistry = {
    register(factory) {
      if (!factory.id.trim()) {
        throw new Error('HARNESS_RUNTIME_ID_EMPTY');
      }
      factories.set(factory.id, factory);
    },
    listIds() {
      return [...factories.keys()].sort();
    },
    resolve(options = {}) {
      const id = resolveId(options.runtimeId);
      const factory = factories.get(id);
      if (!factory) {
        throw new Error(`HARNESS_RUNTIME_UNKNOWN: ${id}`);
      }
      if (active) {
        void active.dispose();
        active = null;
      }
      generation += 1;
      active = factory.create({
        checkoutDir: options.checkoutDir ?? process.env.RING_HARNESS_CHECKOUT,
        pin: options.pin ?? resolveHarnessPin(),
        generation,
      });
      return active;
    },
    current() {
      return active;
    },
    reload(options = {}) {
      return registry.resolve(options);
    },
  };
  return registry;
}

let defaultRegistry: HarnessRuntimeRegistry | null = null;

export function getHarnessRuntimeRegistry(): HarnessRuntimeRegistry {
  if (!defaultRegistry) {
    defaultRegistry = createHarnessRuntimeRegistry();
  }
  return defaultRegistry;
}

export function resetHarnessRuntimeRegistryForTests(): void {
  if (defaultRegistry?.current()) {
    void defaultRegistry.current()?.dispose();
  }
  defaultRegistry = null;
}
