/**
 * EXECUTE 官方 runtime 解析与门禁（无 checkout 亦可测）。
 */
import {describe, expect, test} from 'vitest';
import {
  EXECUTE_RUNTIME_OFFICIAL,
  resolveExecuteRuntime,
  resolveOfficialExecuteMode,
} from './executeOfficialRuntime.js';

describe('executeOfficialRuntime', () => {
  test('默认 / cordis → default；官方 id 直通；非法失败关闭', () => {
    expect(resolveExecuteRuntime({})).toBe('default');
    expect(resolveExecuteRuntime({RING_HARNESS_EXECUTE_RUNTIME: 'cordis'})).toBe(
      'default',
    );
    expect(
      resolveExecuteRuntime({
        RING_HARNESS_EXECUTE_RUNTIME: EXECUTE_RUNTIME_OFFICIAL,
      }),
    ).toBe(EXECUTE_RUNTIME_OFFICIAL);
    expect(() =>
      resolveExecuteRuntime({RING_HARNESS_EXECUTE_RUNTIME: 'latest'}),
    ).toThrow(/HARNESS_EXECUTE_RUNTIME_UNKNOWN/);
  });

  test('OFFICIAL_MODE：缺省/ab01→read_file；diagnose；非法失败关闭', () => {
    expect(resolveOfficialExecuteMode({})).toBe('read_file');
    expect(
      resolveOfficialExecuteMode({RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'ab01'}),
    ).toBe('read_file');
    expect(
      resolveOfficialExecuteMode({
        RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'diagnose',
      }),
    ).toBe('diagnose');
    expect(
      resolveOfficialExecuteMode({
        RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'diagnose_seal',
      }),
    ).toBe('diagnose');
    expect(() =>
      resolveOfficialExecuteMode({
        RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'agent_loop',
      }),
    ).toThrow(/HARNESS_EXECUTE_OFFICIAL_MODE_UNKNOWN/);
  });
});
