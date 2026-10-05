/**
 * 第三百四十七批：官方 ForceStop → Kernel NO_PROGRESS_STOP；≠ DONE。
 */
import {describe, expect, test} from 'vitest';
import {failedOfficialHardForceStop} from './executeOfficialRuntime.js';
import type {ActivityLeaseView} from './fakePlanHost.js';

function claimedFixture(): ActivityLeaseView {
  return {
    activity: {
      id: 'act-official-fs',
      kind: 'EXECUTE',
      project_id: 'proj-1',
      goal_id: 'goal-1',
      task_id: 'task-1',
      state_revision: 1,
      binding: {},
    },
    lease: {
      activity_id: 'act-official-fs',
      attempt_id: 'att-official-fs',
      fencing_epoch: '3',
    },
  };
}

describe('failedOfficialHardForceStop', () => {
  test('登记 NO_PROGRESS_STOP 后 FAILED；marks_goal_done=false', async () => {
    const posts: Array<{url: string; body: unknown}> = [];
    const fetchImpl = (async (url: RequestInfo | URL, init?: RequestInit) => {
      const href = String(url);
      posts.push({
        url: href,
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      return new Response(
        JSON.stringify({
          data: {
            id: 'term-official-1',
            reason: 'NO_PROGRESS_STOP',
            marks_goal_done: false,
            summary_artifact_id: 'sum-1',
            closeout_artifact_id: 'close-1',
          },
        }),
        {status: 201},
      );
    }) as typeof fetch;

    const out = await failedOfficialHardForceStop({
      claimed: claimedFixture(),
      closeout: {
        assistantText: '已无进展，收口。',
        summaryArtifactId: 'sum-1',
        closeoutArtifactId: 'close-1',
        toolCalls: [],
        marksGoalDone: false,
        usedFallback: false,
      },
      reason:
        'TOOL_ADMISSION_CLOSED:NO_PROGRESS_FORCE_STOP:nudge:summary_artifact=sum-1',
      effectIds: ['eff-1'],
      controlUrl: 'http://control.test',
      workerJwt: 'tok',
      fetchImpl,
    });

    expect(out.status).toBe('FAILED');
    expect(out.marks_goal_done).toBe(false);
    expect(out.activation_termination_id).toBe('term-official-1');
    expect(out.effect_ids).toEqual(['eff-1']);
    expect(out.force_stop_closeout?.closeout_artifact_id).toBe('close-1');
    expect(out.force_stop_closeout?.marks_goal_done).toBe(false);
    expect(posts).toHaveLength(1);
    expect(posts[0]!.url).toContain(
      '/internal/v1/activities/act-official-fs/activation-terminations',
    );
    const body = posts[0]!.body as {
      reason: string;
      lease: {attempt_id: string};
    };
    expect(body.reason).toBe('NO_PROGRESS_STOP');
    expect(body.lease.attempt_id).toBe('att-official-fs');
  });

  test('登记 HTTP 失败仍 FAILED ≠ DONE（不吞 ForceStop）', async () => {
    const fetchImpl = (async () =>
      new Response('nope', {status: 503})) as typeof fetch;
    const out = await failedOfficialHardForceStop({
      claimed: claimedFixture(),
      closeout: {
        assistantText: 'fallback',
        summaryArtifactId: null,
        toolCalls: [],
        marksGoalDone: false,
        usedFallback: true,
      },
      controlUrl: 'http://control.test',
      workerJwt: 'tok',
      fetchImpl,
    });
    expect(out.status).toBe('FAILED');
    expect(out.marks_goal_done).toBe(false);
    expect(out.activation_termination_id).toBeUndefined();
    expect(out.reason).toContain('NO_PROGRESS_FORCE_STOP');
  });
});
