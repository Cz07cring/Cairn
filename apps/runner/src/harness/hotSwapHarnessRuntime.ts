/**
 * 热切换：旁路 checkout（新 SHA）→ 更新 env → registry.reload。
 * 禁止用 query cache-bust 同路径热更（会 ESM 双实例导致工具未注册）。
 */
import type {HarnessRuntimeRegistry} from './harnessRuntimeRegistry.js';
import {
  HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
  type HarnessRuntimePlugin,
} from './harnessRuntimePlugin.js';
import {assertCheckoutMatchesPin} from './pinGate.js';
import {assertHarnessSha} from './harnessCheckoutPull.js';

export type HotSwapHarnessResult = {
  plugin: HarnessRuntimePlugin;
  checkoutDir: string;
  pin: string;
  generation: number;
  previousGeneration: number | null;
  marksGoalDone: false;
};

/**
 * 将进程指向新 pin checkout 并热切换插件（不拉仓库；拉用 pullHarnessPinCheckout）。
 */
export function hotSwapHarnessRuntime(input: {
  registry: HarnessRuntimeRegistry;
  checkoutDir: string;
  pin: string;
  runtimeId?: string;
  environ?: NodeJS.ProcessEnv;
}): HotSwapHarnessResult {
  const pin = assertHarnessSha(input.pin);
  assertCheckoutMatchesPin(input.checkoutDir, pin);
  const env = input.environ ?? process.env;
  const prev = input.registry.current()?.manifest.generation ?? null;

  env.RING_HARNESS_CHECKOUT = input.checkoutDir;
  env.RING_HARNESS_PIN = pin;

  const plugin = input.registry.reload({
    runtimeId: input.runtimeId ?? HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
    checkoutDir: input.checkoutDir,
    pin,
  });

  return {
    plugin,
    checkoutDir: input.checkoutDir,
    pin,
    generation: plugin.manifest.generation,
    previousGeneration: prev,
    marksGoalDone: false,
  };
}
