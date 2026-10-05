/**
 * E2E-1：官方 scripted AgentLoop × Gateway 装配（mock Control）。
 * 全栈独立 Broker 进程由 tests/e2e/test_order_idempotency_goal.py 覆盖。
 */
import {existsSync} from 'node:fs';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {
  parseE2e1Env,
  runE2e1OfficialBrokerProcessTurn,
} from './e2e1OfficialBrokerProcessTurn.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';

const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

describe('e2e1OfficialBrokerProcessTurn parseE2e1Env', () => {
  test('缺 env 失败关闭', () => {
    expect(() => parseE2e1Env({})).toThrow(/E2E1_ENV_MISSING/);
  });

  test('解析 lease 与路径', () => {
    const env = parseE2e1Env({
      RING_HARNESS_CHECKOUT: '/tmp/h',
      RING_CONTROL_URL: 'http://127.0.0.1:9',
      RING_WORKER_JWT: 'tok',
      RING_E2E1_PROJECT_ID: '11111111-1111-1111-1111-111111111111',
      RING_E2E1_LEASE_JSON: JSON.stringify({
        activity_id: '22222222-2222-2222-2222-222222222222',
        attempt_id: '33333333-3333-3333-3333-333333333333',
        fencing_epoch: '1',
      }),
      RING_E2E1_READ_PATH: 'FIXED_INPUT.json',
    });
    expect(env.expectedPath).toBe('FIXED_INPUT.json');
    expect(env.activation.lease.fencing_epoch).toBe('1');
  });
});

describe.skipIf(!ready)('e2e1OfficialBrokerProcessTurn official loop', () => {
  test('scripted Loop + Gateway：SUCCEEDED、≥2 轮、≠DONE、无 Runner dispatch', async () => {
    const MARKER = 'checkout-20260912-001';
    const controlRequests: Array<{url: string; method: string}> = [];
    let effectReads = 0;

    const controlFetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = init?.method ?? 'GET';
      controlRequests.push({url: href, method});
      if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
        return Response.json({data: {id: 'input-art'}}, {status: 201});
      }
      if (method === 'POST' && href.endsWith('/steps')) {
        return Response.json(
          {data: {id: 's1', logical_step_id: 's1', intent_revision: 1}},
          {status: 201},
        );
      }
      if (method === 'POST' && href.endsWith('/effects/prepare')) {
        return Response.json(
          {
            data: {
              id: 'effect-e2e1',
              status: 'PREPARED',
              state_revision: 1,
              evidence_ids: [],
            },
          },
          {status: 201},
        );
      }
      if (method === 'GET' && href.endsWith('/api/v1/effects/effect-e2e1')) {
        effectReads += 1;
        if (effectReads < 2) {
          return Response.json({
            data: {id: 'effect-e2e1', status: 'PREPARED', evidence_ids: []},
          });
        }
        return Response.json({
          data: {
            id: 'effect-e2e1',
            status: 'SUCCEEDED',
            evidence_ids: ['ev-1'],
          },
        });
      }
      if (method === 'GET' && href.includes('/artifacts/ev-1/content')) {
        return new Response(`{"sku":"SKU-001","idempotency_key":"${MARKER}"}\n`, {
          status: 200,
        });
      }
      return new Response(`unexpected ${method} ${href}`, {status: 500});
    });

    const result = await runE2e1OfficialBrokerProcessTurn({
      checkout: checkout as string,
      controlUrl: 'http://control.test',
      authorization: 'Bearer t',
      activation: {
        kind: 'EXECUTE',
        projectId: '11111111-1111-1111-1111-111111111111',
        activityId: '22222222-2222-2222-2222-222222222222',
        lease: {
          activity_id: '22222222-2222-2222-2222-222222222222',
          attempt_id: '33333333-3333-3333-3333-333333333333',
          fencing_epoch: '1',
        },
      },
      expectedPath: 'FIXED_INPUT.json',
      userPrompt: '请 read_file FIXED_INPUT.json',
      pollDelayMs: 1,
      pollMaxAttempts: 20,
      idleTimeoutMs: 40_000,
      fetchImpl: controlFetch as typeof fetch,
    });

    expect(result.driver).toBe('official-dsh-agent-loop');
    expect(result.modelRounds).toBeGreaterThanOrEqual(2);
    expect(result.round2CitesToolResult).toBe(true);
    expect(result.effectStatus).toBe('SUCCEEDED');
    expect(result.marksGoalDone).toBe(false);
    expect(result.runnerCalledDispatch).toBe(false);
    expect(
      controlRequests.some(
        (r) => r.method === 'POST' && r.url.endsWith('/dispatch'),
      ),
    ).toBe(false);
    expect(
      controlRequests.some(
        (r) => r.method === 'POST' && r.url.endsWith('/effects/prepare'),
      ),
    ).toBe(true);
  });
});
