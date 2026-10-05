/**
 * AB07 命名缝：同 path 两读即便 READ_ONLY 仍 SERIAL，执行窗口不重叠。
 * 不宣称 PARALLEL；≠ Goal DONE。
 */
import {expect, test, vi} from 'vitest';
import {createBrokerBackedHarnessTool} from './brokerBackedHarnessTool.js';

const activation = {
  kind: 'EXECUTE' as const,
  projectId: '11111111-1111-1111-1111-111111111111',
  activityId: '22222222-2222-2222-2222-222222222222',
  lease: {
    activity_id: '22222222-2222-2222-2222-222222222222',
    attempt_id: '33333333-3333-3333-3333-333333333333',
    fencing_epoch: '7',
  },
};

test('AB07：同 path 两读 READ_ONLY 仍串行且 concurrency 元数据诚实', async () => {
  const stepBodies: Array<Record<string, unknown>> = [];
  let stepIndex = 0;
  let effectIndex = 0;
  let active = 0;
  let maxActive = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';

    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise<void>((r) => setTimeout(r, 15));
      active -= 1;
      return Response.json({data: {id: `input-${stepBodies.length + 1}`}});
    }
    if (method === 'POST' && href.endsWith('/steps')) {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      stepBodies.push(body);
      stepIndex += 1;
      return Response.json(
        {
          data: {
            id: `step-${stepIndex}`,
            logical_step_id: `step-${stepIndex}`,
            intent_revision: 1,
          },
        },
        {status: 201},
      );
    }
    if (method === 'POST' && href.endsWith('/effects/prepare')) {
      effectIndex += 1;
      return Response.json(
        {
          data: {
            id: `effect-${effectIndex}`,
            status: 'PREPARED',
            state_revision: 1,
          },
        },
        {status: 201},
      );
    }
    const effectMatch = href.match(/\/api\/v1\/effects\/(effect-\d+)$/);
    if (method === 'GET' && effectMatch) {
      const id = effectMatch[1] as string;
      return Response.json({
        data: {id, status: 'SUCCEEDED', evidence_ids: [`result-${id}`]},
      });
    }
    const artifactMatch = href.match(
      /\/api\/v1\/artifacts\/(result-effect-\d+)\/content$/,
    );
    if (method === 'GET' && artifactMatch) {
      return new Response('ok', {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });
  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  const samePath = JSON.stringify({path: 'src/mutable.ts'});
  const [first, second] = await Promise.all([
    tool.execute({callId: 'ab07-a', name: 'read_file', arguments: samePath}),
    tool.execute({callId: 'ab07-b', name: 'read_file', arguments: samePath}),
  ]);

  expect(maxActive).toBe(1);
  expect(first.isError).toBe(false);
  expect(second.isError).toBe(false);
  expect(first.meta.concurrency).toEqual({
    class: 'SERIAL',
    resourceKeys: ['path:src/mutable.ts'],
    reason: 'READ_ONLY_LABEL_DOES_NOT_GRANT_PARALLEL',
    marksGoalDone: false,
  });
  expect(second.meta.concurrency?.class).toBe('SERIAL');
  expect(second.meta.concurrency?.resourceKeys).toEqual([
    'path:src/mutable.ts',
  ]);
  expect(stepBodies).toHaveLength(2);
  expect(stepBodies[0]?.predecessor_step_id).toBeNull();
  expect(stepBodies[1]?.predecessor_step_id).toBe('step-1');
});
