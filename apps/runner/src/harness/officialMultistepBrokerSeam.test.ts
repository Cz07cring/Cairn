/**
 * 官方 Loop scripted 多工具 × Gateway：prepare→SUCCEEDED；无 Runner dispatch；≠ DONE。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialMultistepBrokerWriteRunTests} from './officialMultistepBrokerSeam.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

const WRITE_MARKER = 'RING_MS_WRITE_MARKER_c4e2';
const TESTS_MARKER = 'RING_MS_TESTS_MARKER_exit0';

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

describe.skipIf(!ready)('officialMultistepBrokerSeam', () => {
  test(
    'write→run_tests：双 prepare、无 dispatch、scriptedOrder、≠DONE',
    async () => {
      const controlRequests: Array<{url: string; method: string}> = [];
      let stepN = 0;
      let prepareN = 0;
      const effectReads = new Map<string, number>();

      const controlFetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
        const href = String(url);
        const method = init?.method ?? 'GET';
        controlRequests.push({url: href, method});

        if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
          return Response.json({
            data: {id: `input-art-${controlRequests.length}`},
          });
        }
        if (method === 'POST' && href.endsWith('/steps')) {
          stepN += 1;
          return Response.json(
            {
              data: {
                id: `step-${stepN}`,
                logical_step_id: `step-${stepN}`,
                intent_revision: 1,
              },
            },
            {status: 201},
          );
        }
        if (method === 'POST' && href.endsWith('/effects/prepare')) {
          prepareN += 1;
          const id = prepareN === 1 ? 'effect-write-ms' : 'effect-tests-ms';
          return Response.json(
            {
              data: {
                id,
                status: 'PREPARED',
                state_revision: 1,
                tool_ref: prepareN === 1 ? 'write_file' : 'run_tests',
              },
            },
            {status: 201},
          );
        }
        const effectMatch = href.match(/\/api\/v1\/effects\/(effect-[^/?]+)$/);
        if (method === 'GET' && effectMatch) {
          const id = effectMatch[1]!;
          const n = (effectReads.get(id) ?? 0) + 1;
          effectReads.set(id, n);
          const evidenceId =
            id === 'effect-write-ms' ? 'result-write' : 'result-tests';
          return Response.json({
            data:
              n === 1
                ? {id, status: 'DISPATCHED', evidence_ids: []}
                : {id, status: 'SUCCEEDED', evidence_ids: [evidenceId]},
          });
        }
        if (method === 'GET' && href.endsWith('/api/v1/artifacts/result-write/content')) {
          return new Response(`${WRITE_MARKER}\nafter_digest=sha256:ab`, {
            status: 200,
          });
        }
        if (method === 'GET' && href.endsWith('/api/v1/artifacts/result-tests/content')) {
          return new Response(`${TESTS_MARKER}\nexit_code=0`, {status: 200});
        }
        throw new Error(`unexpected ${method} ${href}`);
      });

      const evidence = await runOfficialMultistepBrokerWriteRunTests({
        checkoutDir: checkout,
        writePath: 'order_service/store.py',
        writeContent: 'fixed\n',
        userPrompt: '先 write_file 再 run_tests public',
        idleTimeoutMs: 40_000,
        broker: {
          baseUrl: 'http://control.test',
          authorization: 'Bearer worker',
          activation,
          fetchImpl: controlFetch as unknown as typeof fetch,
          poll: {maxAttempts: 3, delayMs: 0},
        },
      });

      expect(evidence.scriptedOrder).toBe(true);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.trail.map((t) => t.toolName)).toEqual([
        'write_file',
        'run_tests',
      ]);
      expect(evidence.trail[0]?.effectId).toBe('effect-write-ms');
      expect(evidence.trail[1]?.effectId).toBe('effect-tests-ms');
      expect(evidence.trail[0]?.toolResultText).toContain(WRITE_MARKER);
      expect(evidence.trail[1]?.toolResultText).toContain(TESTS_MARKER);
      expect(evidence.modelRounds).toBeGreaterThanOrEqual(3);
      expect(prepareN).toBe(2);
      expect(stepN).toBe(2);
      expect(
        controlRequests.some(
          (r) => r.method === 'POST' && r.url.endsWith('/effects/prepare'),
        ),
      ).toBe(true);
      expect(
        controlRequests.some(
          (r) => r.method === 'POST' && r.url.includes('/dispatch'),
        ),
      ).toBe(false);
    },
    60_000,
  );
});
