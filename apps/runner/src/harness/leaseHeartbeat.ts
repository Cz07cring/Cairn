/**
 * EXECUTE 租约心跳：官方 AgentLoop 长轮次期间续期 attempt，避免 ARTIFACT_PUT 409。
 * 不写 Goal DONE；STOP 信号由调用方可选处理。
 */
import type {LeaseIdentity} from './fakePlanHost.js';

export type LeaseHeartbeatPorts = {
  activityId: string;
  lease: LeaseIdentity;
  baseUrl: string;
  authorization: string;
  fetchImpl?: typeof fetch;
  /** 默认 10s；须明显短于 Kernel claim TTL（约 90s） */
  intervalMs?: number;
  /**
   * 下一拍应提交的 renewal_seq（= 当前库内值 + 1）。
   * 缺省从 1 起；若上游已预续期须传入，否则首拍可能只 no-op 而不延 TTL。
   */
  nextRenewalSeq?: number;
  onStop?: (input: {
    control?: string;
    pendingStopIds?: string[];
  }) => void | Promise<void>;
  onFailure?: (err: unknown) => void;
};

export type LeaseHeartbeatHandle = {
  stop: () => void;
  /** 已成功续期次数（含启动时立即一拍） */
  successCount: () => number;
};

function bearer(raw: string): string {
  const t = raw.trim();
  return t.startsWith('Bearer ') ? t : `Bearer ${t}`;
}

function trimBase(url: string): string {
  return url.replace(/\/+$/, '');
}

/**
 * 启动租约心跳：立即一拍，再按 interval 续期。
 * 调用方须在 finally 中 stop。
 */
export function startLeaseHeartbeat(
  ports: LeaseHeartbeatPorts,
): LeaseHeartbeatHandle {
  const baseUrl = trimBase(ports.baseUrl);
  const auth = bearer(ports.authorization);
  const fetchFn = ports.fetchImpl ?? fetch;
  const intervalMs = ports.intervalMs ?? 10_000;
  let renewalSeq =
    typeof ports.nextRenewalSeq === 'number' &&
    Number.isFinite(ports.nextRenewalSeq) &&
    ports.nextRenewalSeq >= 1
      ? Math.floor(ports.nextRenewalSeq)
      : 1;
  let successes = 0;
  let stopped = false;
  let timer: ReturnType<typeof setInterval> | undefined;

  const beat = async (): Promise<void> => {
    if (stopped) return;
    const res = await fetchFn(
      `${baseUrl}/internal/v1/activities/${ports.activityId}/heartbeat`,
      {
        method: 'POST',
        headers: {
          Authorization: auth,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          lease: ports.lease,
          renewal_seq: renewalSeq,
        }),
      },
    );
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`LEASE_HEARTBEAT_FAILED: HTTP ${res.status} ${text}`);
    }
    const body = (await res.json()) as {
      data?: {
        renewal_seq?: number;
        control?: string;
        pending_stop_ids?: string[];
      };
    };
    const data = body.data ?? {};
    if (typeof data.renewal_seq === 'number') {
      renewalSeq = data.renewal_seq + 1;
    } else {
      renewalSeq += 1;
    }
    successes += 1;
    if (data.control || (data.pending_stop_ids?.length ?? 0) > 0) {
      await ports.onStop?.({
        control: data.control,
        pendingStopIds: data.pending_stop_ids,
      });
    }
  };

  const tick = (): void => {
    void beat().catch((err: unknown) => {
      ports.onFailure?.(err);
    });
  };

  tick();
  timer = setInterval(tick, intervalMs);
  timer.unref?.();

  return {
    stop: () => {
      stopped = true;
      if (timer !== undefined) {
        clearInterval(timer);
        timer = undefined;
      }
    },
    successCount: () => successes,
  };
}
