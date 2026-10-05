/**
 * 租约心跳：mock Control 续期计数。
 */
import {describe, expect, test, vi} from 'vitest';
import {startLeaseHeartbeat} from './leaseHeartbeat.js';

describe('leaseHeartbeat', () => {
  test('立即一拍并按 interval 续期；stop 后不再打', async () => {
    let renewalSeen = 0;
    const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
      expect(String(url)).toContain('/internal/v1/activities/act-1/heartbeat');
      expect(init?.method).toBe('POST');
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        renewal_seq?: number;
      };
      renewalSeen = body.renewal_seq ?? 0;
      return Response.json({
        data: {renewal_seq: renewalSeen, control: undefined},
      });
    }) as unknown as typeof fetch;

    const hb = startLeaseHeartbeat({
      activityId: 'act-1',
      lease: {
        activity_id: 'act-1',
        attempt_id: 'att-1',
        fencing_epoch: '1',
      },
      baseUrl: 'http://control.test',
      authorization: 'tok',
      fetchImpl,
      intervalMs: 40,
    });

    await vi.waitFor(() => expect(hb.successCount()).toBeGreaterThanOrEqual(1));
    await vi.waitFor(() => expect(hb.successCount()).toBeGreaterThanOrEqual(2));
    const mid = hb.successCount();
    hb.stop();
    await new Promise((r) => setTimeout(r, 80));
    expect(hb.successCount()).toBe(mid);
    expect(fetchImpl).toHaveBeenCalled();
  });

  test('nextRenewalSeq 对齐预续期，首拍提交该序号', async () => {
    const seen: number[] = [];
    const fetchImpl = vi.fn(async (_url: string | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        renewal_seq?: number;
      };
      seen.push(body.renewal_seq ?? 0);
      return Response.json({
        data: {renewal_seq: body.renewal_seq ?? 0},
      });
    }) as unknown as typeof fetch;

    const hb = startLeaseHeartbeat({
      activityId: 'act-1',
      lease: {
        activity_id: 'act-1',
        attempt_id: 'att-1',
        fencing_epoch: '1',
      },
      baseUrl: 'http://control.test',
      authorization: 'tok',
      fetchImpl,
      intervalMs: 10_000,
      nextRenewalSeq: 3,
    });
    await vi.waitFor(() => expect(hb.successCount()).toBeGreaterThanOrEqual(1));
    hb.stop();
    expect(seen[0]).toBe(3);
  });
});
