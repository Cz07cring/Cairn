/**
 * 第三百四十五批：reportActivationTermination HTTP 客户端；≠ DONE。
 */
import {describe, expect, test, vi} from 'vitest';
import {reportActivationTermination} from './reportActivationTermination.js';

describe('reportActivationTermination', () => {
  test('POST 成功：marksGoalDone=false', async () => {
    const fetchImpl = vi.fn(async (_url: string | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        reason: string;
        lease: {attempt_id: string};
      };
      expect(body.reason).toBe('NO_PROGRESS_STOP');
      expect(body.lease.attempt_id).toBe('att-1');
      return new Response(
        JSON.stringify({
          data: {
            id: 'term-1',
            reason: 'NO_PROGRESS_STOP',
            marks_goal_done: false,
            summary_artifact_id: 'sum-1',
          },
          error: null,
        }),
        {status: 201, headers: {'Content-Type': 'application/json'}},
      );
    });

    const out = await reportActivationTermination({
      baseUrl: 'http://control.test',
      authorization: 'Bearer w',
      activityId: 'act-1',
      lease: {
        activity_id: 'act-1',
        attempt_id: 'att-1',
        fencing_epoch: '7',
      },
      reason: 'NO_PROGRESS_STOP',
      summaryArtifactId: 'sum-1',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(out.marksGoalDone).toBe(false);
    expect(out.id).toBe('term-1');
    expect(out.summaryArtifactId).toBe('sum-1');
  });

  test('服务端 marks_goal_done=true → 失败关闭', async () => {
    await expect(
      reportActivationTermination({
        baseUrl: 'http://control.test',
        authorization: 'Bearer w',
        activityId: 'act-1',
        lease: {
          activity_id: 'act-1',
          attempt_id: 'att-1',
          fencing_epoch: '7',
        },
        reason: 'NO_PROGRESS_STOP',
        fetchImpl: (async () =>
          new Response(
            JSON.stringify({
              data: {id: 'x', reason: 'NO_PROGRESS_STOP', marks_goal_done: true},
            }),
            {status: 201},
          )) as unknown as typeof fetch,
      }),
    ).rejects.toThrow(/MARKED_GOAL_DONE/);
  });
});
