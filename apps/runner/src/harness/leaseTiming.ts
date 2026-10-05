/**
 * 租约心跳间隔解析：对齐 Control `RING_HEARTBEAT_SECONDS` / `RING_LEASE_TTL_SECONDS`
 *（doc/08：TTL≥3×心跳）。≠ Goal DONE。
 */

export type LeaseHeartbeatResolveResult = {
  /** null = 显式关闭心跳 */
  intervalMs: number | null;
  /** 是否因 TTL/3 上界被压低 */
  clampedToTtlThird: boolean;
};

export type ResolveLeaseHeartbeatOptions = {
  defaultIntervalMs?: number;
};

const DEFAULT_INTERVAL_MS = 10_000;
const MIN_INTERVAL_MS = 1_000;

/**
 * 解析 Runner 租约心跳间隔。
 *
 * 优先级：
 * 1. `RING_HARNESS_EXECUTE_HEARTBEAT=0|off|false` → 关闭
 * 2. `RING_HARNESS_EXECUTE_HEARTBEAT_MS`（显式毫秒）
 * 3. `RING_HEARTBEAT_SECONDS`（与 Control Settings 同名旋钮）
 * 4. defaultIntervalMs（官方默认 10s；Cordis 可传 30s）
 *
 * 若同时给出 `RING_LEASE_TTL_SECONDS`，间隔不得超过 floor(TTL/3) 秒，
 * 否则压到该上界（避免「可配但必失租」）。
 */
export function resolveLeaseHeartbeatIntervalMs(
  env: NodeJS.ProcessEnv = process.env,
  opts?: ResolveLeaseHeartbeatOptions,
): LeaseHeartbeatResolveResult {
  const defaultIntervalMs = opts?.defaultIntervalMs ?? DEFAULT_INTERVAL_MS;
  const rawOff = (env.RING_HARNESS_EXECUTE_HEARTBEAT || '').trim();
  if (
    rawOff === '0' ||
    rawOff.toLowerCase() === 'off' ||
    rawOff.toLowerCase() === 'false'
  ) {
    return {intervalMs: null, clampedToTtlThird: false};
  }

  let intervalMs = defaultIntervalMs;
  const msRaw = (env.RING_HARNESS_EXECUTE_HEARTBEAT_MS || '').trim();
  if (msRaw) {
    const ms = Number(msRaw);
    intervalMs =
      Number.isFinite(ms) && ms >= MIN_INTERVAL_MS ? ms : defaultIntervalMs;
  } else {
    const secRaw = (env.RING_HEARTBEAT_SECONDS || '').trim();
    if (secRaw) {
      const sec = Number(secRaw);
      if (Number.isFinite(sec) && sec >= 1) {
        intervalMs = Math.max(MIN_INTERVAL_MS, Math.floor(sec * 1000));
      }
    }
  }

  const ttlRaw = (env.RING_LEASE_TTL_SECONDS || '').trim();
  if (!ttlRaw) {
    return {intervalMs, clampedToTtlThird: false};
  }
  const ttlSec = Number(ttlRaw);
  if (!Number.isFinite(ttlSec) || ttlSec < 3) {
    return {intervalMs, clampedToTtlThird: false};
  }
  const maxMs = Math.floor(ttlSec / 3) * 1000;
  if (intervalMs > maxMs) {
    return {
      intervalMs: Math.max(MIN_INTERVAL_MS, maxMs),
      clampedToTtlThird: true,
    };
  }
  return {intervalMs, clampedToTtlThird: false};
}
