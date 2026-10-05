/**
 * Harness 插拔注册表：换驱动 / 热 reload generation；≠ Goal DONE。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test} from 'vitest';
import type {HarnessToolResult} from './brokerBackedHarnessTool.js';
import {cordisEntryPath} from './cordisBootGate.js';
import {
  HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
  type HarnessRuntimeFactory,
  type HarnessRuntimePlugin,
} from './harnessRuntimePlugin.js';
import {
  createHarnessRuntimeRegistry,
  resetHarnessRuntimeRegistryForTests,
} from './harnessRuntimeRegistry.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {DEEPSEEK_HARNESS_COMMIT, resolveHarnessPin} from './pin.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const agentLoopBuilt =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

describe('harnessRuntimeRegistry', () => {
  test('默认注册 deepseek-official；未知驱动失败关闭', () => {
    resetHarnessRuntimeRegistryForTests();
    const reg = createHarnessRuntimeRegistry();
    expect(reg.listIds()).toContain(HARNESS_RUNTIME_DEEPSEEK_OFFICIAL);
    expect(() => reg.resolve({runtimeId: 'no-such-runtime'})).toThrow(
      /HARNESS_RUNTIME_UNKNOWN/,
    );
  });

  test('可注册自定义驱动并 resolve', () => {
    const stub: HarnessRuntimeFactory = {
      id: 'ring-stub-runtime',
      create(options) {
        const plugin: HarnessRuntimePlugin = {
          manifest: {
            id: 'ring-stub-runtime',
            kind: 'ring-native',
            pin: options?.pin ?? 'stub',
            generation: options?.generation ?? 0,
            marksGoalDone: false,
          },
          isReady: () => true,
          async runReadFileTurn() {
            throw new Error('STUB_NO_TURN');
          },
          dispose() {},
        };
        return plugin;
      },
    };
    const reg = createHarnessRuntimeRegistry([]);
    reg.register(stub);
    const p = reg.resolve({runtimeId: 'ring-stub-runtime', pin: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'});
    expect(p.manifest.id).toBe('ring-stub-runtime');
    expect(p.manifest.generation).toBe(1);
    expect(p.manifest.marksGoalDone).toBe(false);
    const reloaded = reg.reload({runtimeId: 'ring-stub-runtime'});
    expect(reloaded.manifest.generation).toBe(2);
    expect(reg.current()).toBe(reloaded);
  });

  test('resolveHarnessPin：默认常量；非法 RING_HARNESS_PIN 失败关闭', () => {
    expect(resolveHarnessPin({})).toBe(DEEPSEEK_HARNESS_COMMIT);
    expect(() =>
      resolveHarnessPin({RING_HARNESS_PIN: 'latest'} as NodeJS.ProcessEnv),
    ).toThrow(/HARNESS_PIN_INVALID/);
  });

  test.skipIf(!agentLoopBuilt)(
    '热 reload 后官方插件仍可跑 read_file 两轮；marksGoalDone=false',
    async () => {
      const reg = createHarnessRuntimeRegistry();
      const first = reg.resolve({
        runtimeId: HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
        checkoutDir: checkout,
        pin: DEEPSEEK_HARNESS_COMMIT,
      });
      expect(first.isReady()).toBe(true);
      // 同路径 reload 只升 generation（禁止 query cache-bust；换码用旁路 pin 目录）
      const second = reg.reload({
        runtimeId: HARNESS_RUNTIME_DEEPSEEK_OFFICIAL,
        checkoutDir: checkout,
        pin: DEEPSEEK_HARNESS_COMMIT,
      });
      expect(second.manifest.generation).toBeGreaterThan(first.manifest.generation);
      expect(first.manifest.generation).not.toBe(second.manifest.generation);

      const expectedPath = 'src/hot_reload.ts';
      const evidence = await second.runReadFileTurn({
        userPrompt: `读取 ${expectedPath}`,
        expectedPath,
        idleTimeoutMs: 25_000,
        executeTool: async (call) => {
          const result: HarnessToolResult = {
            content: [{type: 'text', text: 'HOT_RELOAD_OK'}],
            isError: false,
            meta: {
              effectId: 'effect-hot-1',
              status: 'SUCCEEDED',
              evidenceIds: [],
              requiresReconciliation: false,
            },
          };
          expect(call.name).toBe('read_file');
          return result;
        },
      });
      expect(evidence.runtimeId).toBe(HARNESS_RUNTIME_DEEPSEEK_OFFICIAL);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.toolResultText).toBe('HOT_RELOAD_OK');
      expect(evidence.modelRounds).toBeGreaterThanOrEqual(2);
      second.dispose();
    },
    40_000,
  );
});
