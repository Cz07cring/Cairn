import {describe, expect, test} from 'vitest';
import {resolveLeaseHeartbeatIntervalMs} from './leaseTiming.js';

describe('resolveLeaseHeartbeatIntervalMs', () => {
  test('显式关闭', () => {
    expect(
      resolveLeaseHeartbeatIntervalMs({RING_HARNESS_EXECUTE_HEARTBEAT: '0'}),
    ).toEqual({intervalMs: null, clampedToTtlThird: false});
  });

  test('优先 HEARTBEAT_MS', () => {
    expect(
      resolveLeaseHeartbeatIntervalMs({
        RING_HARNESS_EXECUTE_HEARTBEAT_MS: '7000',
        RING_HEARTBEAT_SECONDS: '15',
      }),
    ).toEqual({intervalMs: 7000, clampedToTtlThird: false});
  });

  test('接通 RING_HEARTBEAT_SECONDS（无 MS 时）', () => {
    expect(
      resolveLeaseHeartbeatIntervalMs({RING_HEARTBEAT_SECONDS: '15'}),
    ).toEqual({intervalMs: 15_000, clampedToTtlThird: false});
  });

  test('默认 10s', () => {
    expect(resolveLeaseHeartbeatIntervalMs({})).toEqual({
      intervalMs: 10_000,
      clampedToTtlThird: false,
    });
  });

  test('超过 TTL/3 时压到上界', () => {
    expect(
      resolveLeaseHeartbeatIntervalMs({
        RING_HARNESS_EXECUTE_HEARTBEAT_MS: '40000',
        RING_LEASE_TTL_SECONDS: '90',
      }),
    ).toEqual({intervalMs: 30_000, clampedToTtlThird: true});
  });

  test('cordis 默认 30s 在 TTL=90 时恰为上界、不压', () => {
    expect(
      resolveLeaseHeartbeatIntervalMs(
        {RING_LEASE_TTL_SECONDS: '90'},
        {defaultIntervalMs: 30_000},
      ),
    ).toEqual({intervalMs: 30_000, clampedToTtlThird: false});
  });
});
