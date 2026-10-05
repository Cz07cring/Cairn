/**
 * chat-driven 诊断 × Gateway：四次 prepare；无 Runner dispatch；≠ DONE。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialChatDrivenDiagnoseBrokerCycle} from './officialChatDrivenDiagnoseBrokerSeam.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

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

describe.skipIf(!ready)('officialChatDrivenDiagnoseBrokerSeam', () => {
  test(
    'read→红→write→绿：四 prepare、无 dispatch、scriptedOrder=false、≠DONE',
    async () => {
      const controlRequests: Array<{url: string; method: string}> = [];
      let stepN = 0;
      let prepareN = 0;
      const effectReads = new Map<string, number>();
      let runTestsPrepares = 0;

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
          let toolRef = 'unknown';
          try {
            const body = JSON.parse(String(init?.body ?? '{}')) as {
              tool_ref?: string;
            };
            toolRef = body.tool_ref ?? toolRef;
          } catch {
            /* ignore */
          }
          if (toolRef === 'run_tests') runTestsPrepares += 1;
          const id = `effect-diag-${prepareN}`;
          return Response.json(
            {
              data: {
                id,
                status: 'PREPARED',
                state_revision: 1,
                tool_ref: toolRef,
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
          const evidenceId = `result-${id}`;
          return Response.json({
            data:
              n === 1
                ? {id, status: 'DISPATCHED', evidence_ids: []}
                : {id, status: 'SUCCEEDED', evidence_ids: [evidenceId]},
          });
        }
        if (method === 'GET' && href.includes('/api/v1/artifacts/result-')) {
          const isWrite = href.includes('effect-diag-3');
          const isRead = href.includes('effect-diag-1');
          const isTests1 = href.includes('effect-diag-2');
          const body = isRead
            ? 'READ_BUG_MARKER\nbuggy'
            : isTests1
              ? 'exit_code=1\nsuite=public'
              : isWrite
                ? 'WRITE_OK_MARKER\nok'
                : 'exit_code=0\nsuite=public';
          return new Response(body, {status: 200});
        }
        throw new Error(`unexpected ${method} ${href}`);
      });

      const evidence = await runOfficialChatDrivenDiagnoseBrokerCycle({
        checkoutDir: checkout,
        readPath: 'order_service/store.py',
        writePath: 'order_service/store.py',
        writeContent: 'fixed\n',
        userPrompt: '诊断修复订单幂等',
        idleTimeoutMs: 60_000,
        chat: {
          baseUrl: 'http://injected.invalid',
          apiKey: 'test-key',
          modelId: 'injected-diagnose-gw',
        },
        broker: {
          baseUrl: 'http://control.test',
          authorization: 'Bearer worker',
          activation,
          fetchImpl: controlFetch as unknown as typeof fetch,
          poll: {maxAttempts: 3, delayMs: 0},
        },
      });

      expect(evidence.scriptedOrder).toBe(false);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.trail.map((t) => t.toolName)).toEqual([
        'read_file',
        'run_tests',
        'write_file',
        'run_tests',
      ]);
      expect(prepareN).toBe(4);
      expect(runTestsPrepares).toBe(2);
      expect(
        controlRequests.some((r) => r.url.includes('/dispatch')),
      ).toBe(false);
      expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
    },
    90_000,
  );

  test(
    'read→红→write→绿→seal：五 prepare、无 dispatch、≠DONE',
    async () => {
      const profileId = '11111111-1111-1111-1111-111111111111';
      const controlRequests: Array<{url: string; method: string}> = [];
      let stepN = 0;
      let prepareN = 0;
      const effectReads = new Map<string, number>();
      const prepareTools: string[] = [];

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
          let toolRef = 'unknown';
          try {
            const body = JSON.parse(String(init?.body ?? '{}')) as {
              tool_ref?: string;
            };
            toolRef = body.tool_ref ?? toolRef;
          } catch {
            /* ignore */
          }
          prepareTools.push(toolRef);
          const id = `effect-diag-seal-${prepareN}`;
          return Response.json(
            {
              data: {
                id,
                status: 'PREPARED',
                state_revision: 1,
                tool_ref: toolRef,
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
          return Response.json({
            data:
              n === 1
                ? {id, status: 'DISPATCHED', evidence_ids: []}
                : {id, status: 'SUCCEEDED', evidence_ids: [`result-${id}`]},
          });
        }
        if (method === 'GET' && href.includes('/api/v1/artifacts/result-')) {
          const m = href.match(/effect-diag-seal-(\d+)/);
          const idx = m ? Number(m[1]) : 0;
          const tool = prepareTools[idx - 1] ?? '';
          let body = 'ok';
          if (tool === 'read_file') body = 'READ_BUG_MARKER';
          else if (tool === 'write_file') body = 'WRITE_OK_MARKER';
          else if (tool === 'run_tests') {
            const which = prepareTools
              .slice(0, idx)
              .filter((t) => t === 'run_tests').length;
            body =
              which <= 1
                ? 'exit_code=1\nsuite=public'
                : 'exit_code=0\nsuite=public';
          } else if (tool === 'seal_candidate') {
            body = 'SEAL_OK_MARKER\ncandidate=ok';
          }
          return new Response(body, {status: 200});
        }
        throw new Error(`unexpected ${method} ${href}`);
      });

      const evidence = await runOfficialChatDrivenDiagnoseBrokerCycle({
        checkoutDir: checkout,
        readPath: 'order_service/store.py',
        writePath: 'order_service/store.py',
        writeContent: 'fixed\n',
        sealVerificationProfileIds: [profileId],
        userPrompt: '诊断修复并 seal',
        idleTimeoutMs: 60_000,
        chat: {
          baseUrl: 'http://injected.invalid',
          apiKey: 'test-key',
          modelId: 'injected-diagnose-seal-gw',
        },
        broker: {
          baseUrl: 'http://control.test',
          authorization: 'Bearer worker',
          activation,
          fetchImpl: controlFetch as unknown as typeof fetch,
          poll: {maxAttempts: 3, delayMs: 0},
        },
      });

      expect(evidence.scriptedOrder).toBe(false);
      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.trail.map((t) => t.toolName)).toEqual([
        'read_file',
        'run_tests',
        'write_file',
        'run_tests',
        'seal_candidate',
      ]);
      expect(prepareN).toBe(5);
      expect(prepareTools).toContain('seal_candidate');
      expect(
        controlRequests.some((r) => r.url.includes('/dispatch')),
      ).toBe(false);
      expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
    },
    90_000,
  );
});
