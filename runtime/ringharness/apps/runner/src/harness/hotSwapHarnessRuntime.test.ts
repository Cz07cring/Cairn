/**
 * 旁路目录热切换：更新 env + generation；≠ Goal DONE。
 */
import {mkdirSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {afterEach, describe, expect, test} from 'vitest';
import {writeCheckoutPinFile} from './harnessCheckoutPull.js';
import {
  createHarnessRuntimeRegistry,
} from './harnessRuntimeRegistry.js';
import type {
  HarnessRuntimeFactory,
  HarnessRuntimePlugin,
} from './harnessRuntimePlugin.js';
import {hotSwapHarnessRuntime} from './hotSwapHarnessRuntime.js';

const PIN_A = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const PIN_B = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

describe('hotSwapHarnessRuntime', () => {
  const dirs: string[] = [];

  afterEach(() => {
    for (const d of dirs.splice(0)) {
      rmSync(d, {recursive: true, force: true});
    }
  });

  test('换 checkout 路径热切换并升 generation；marksGoalDone=false', () => {
    const a = mkdtempSync(join(tmpdir(), 'ring-hs-a-'));
    const b = mkdtempSync(join(tmpdir(), 'ring-hs-b-'));
    dirs.push(a, b);
    mkdirSync(a, {recursive: true});
    mkdirSync(b, {recursive: true});
    writeCheckoutPinFile(a, PIN_A);
    writeCheckoutPinFile(b, PIN_B);

    const stub: HarnessRuntimeFactory = {
      id: 'ring-hot-stub',
      create(options) {
        const plugin: HarnessRuntimePlugin = {
          manifest: {
            id: 'ring-hot-stub',
            kind: 'ring-native',
            pin: options?.pin ?? 'x',
            generation: options?.generation ?? 0,
            marksGoalDone: false,
          },
          isReady: () => true,
          async runReadFileTurn() {
            throw new Error('STUB');
          },
          dispose() {},
        };
        return plugin;
      },
    };

    const reg = createHarnessRuntimeRegistry([]);
    reg.register(stub);
    const env: NodeJS.ProcessEnv = {};
    reg.resolve({
      runtimeId: 'ring-hot-stub',
      checkoutDir: a,
      pin: PIN_A,
    });

    const swapped = hotSwapHarnessRuntime({
      registry: reg,
      checkoutDir: b,
      pin: PIN_B,
      runtimeId: 'ring-hot-stub',
      environ: env,
    });

    expect(swapped.generation).toBe(2);
    expect(swapped.previousGeneration).toBe(1);
    expect(swapped.pin).toBe(PIN_B);
    expect(swapped.marksGoalDone).toBe(false);
    expect(env.RING_HARNESS_CHECKOUT).toBe(b);
    expect(env.RING_HARNESS_PIN).toBe(PIN_B);
    expect(reg.current()?.manifest.pin).toBe(PIN_B);
  });

  test('pin 与 checkout 不符时零副作用（不改 env）', () => {
    const a = mkdtempSync(join(tmpdir(), 'ring-hs-bad-'));
    dirs.push(a);
    writeCheckoutPinFile(a, PIN_A);
    const stub: HarnessRuntimeFactory = {
      id: 'ring-hot-stub',
      create() {
        throw new Error('SHOULD_NOT_CREATE');
      },
    };
    const reg = createHarnessRuntimeRegistry([]);
    reg.register(stub);
    const env: NodeJS.ProcessEnv = {RING_HARNESS_PIN: PIN_A};
    expect(() =>
      hotSwapHarnessRuntime({
        registry: reg,
        checkoutDir: a,
        pin: PIN_B,
        runtimeId: 'ring-hot-stub',
        environ: env,
      }),
    ).toThrow(/HARNESS_PIN_MISMATCH|HARNESS_CHECKOUT/);
    expect(env.RING_HARNESS_PIN).toBe(PIN_A);
    expect(env.RING_HARNESS_CHECKOUT).toBeUndefined();
  });
});
