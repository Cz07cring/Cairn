import {expect, test} from 'vitest';
import {runWorkerLoop} from './worker.js';

test('worker idle without RING_TEMPORAL_TARGET exits 0 on once', async () => {
  const code = await runWorkerLoop({
    once: true,
    env: {},
  });
  expect(code).toBe(0);
});
