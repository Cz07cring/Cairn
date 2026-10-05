/**
 * live：真实 chat × 官方 Loop chat-driven × Gateway 协议（mock Control）。
 * useInjectedDecisionFsm=false；无 Runner dispatch；≠ Goal DONE。
 */
import {existsSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialChatDrivenMultistepBrokerWriteRunTests} from './officialChatDrivenMultistepBrokerSeam.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

const WRITE_MARKER = 'WRITE_OK_MARKER';
const TESTS_MARKER = 'RING_CD_LIVE_GW_TESTS_exit0';

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

function loadRuntimeEnv(): void {
  const candidates = [
    resolve(process.cwd(), '.runtime'),
    resolve(process.cwd(), '../../.runtime'),
    resolve(process.cwd(), '../../../.runtime'),
  ];
  for (const dir of candidates) {
    for (const name of ['chat.env', 'qwen.env']) {
      const file = resolve(dir, name);
      if (!existsSync(file)) continue;
      for (const line of readFileSync(file, 'utf8').split('\n')) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) continue;
        const i = trimmed.indexOf('=');
        if (i <= 0) continue;
        const key = trimmed.slice(0, i).trim();
        let val = trimmed.slice(i + 1).trim();
        if (
          (val.startsWith('"') && val.endsWith('"')) ||
          (val.startsWith("'") && val.endsWith("'"))
        ) {
          val = val.slice(1, -1);
        }
        if (!(key in process.env) || !String(process.env[key] || '').trim()) {
          process.env[key] = val;
        }
      }
    }
  }
}

loadRuntimeEnv();
const chat = toolChatConfigFromEnv();
const checkout = process.env.RING_HARNESS_CHECKOUT;
const ready =
  Boolean(chat) &&
  Boolean(checkout) &&
  existsSync(cordisEntryPath(checkout as string)) &&
  isOfficialAgentLoopBuilt(checkout);

describe.skipIf(!ready)(
  'official chat-driven multistep × Gateway live',
  () => {
    test(
      '真实模型 write→run_tests × Gateway prepare；无 dispatch；≠DONE',
      async () => {
        const controlRequests: Array<{url: string; method: string}> = [];
        let stepN = 0;
        let prepareN = 0;
        const effectReads = new Map<string, number>();
        const prepareTools: string[] = [];

        const controlFetch = vi.fn(
          async (url: string | URL, init?: RequestInit) => {
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
              const id =
                toolRef === 'run_tests'
                  ? 'effect-tests-live-cd'
                  : 'effect-write-live-cd';
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
            const effectMatch = href.match(
              /\/api\/v1\/effects\/(effect-[^/?]+)$/,
            );
            if (method === 'GET' && effectMatch) {
              const id = effectMatch[1]!;
              const n = (effectReads.get(id) ?? 0) + 1;
              effectReads.set(id, n);
              const evidenceId =
                id === 'effect-write-live-cd'
                  ? 'result-write-live-cd'
                  : 'result-tests-live-cd';
              return Response.json({
                data:
                  n === 1
                    ? {id, status: 'DISPATCHED', evidence_ids: []}
                    : {id, status: 'SUCCEEDED', evidence_ids: [evidenceId]},
              });
            }
            if (
              method === 'GET' &&
              href.endsWith('/api/v1/artifacts/result-write-live-cd/content')
            ) {
              return new Response(`${WRITE_MARKER}\nafter_digest=sha256:live`, {
                status: 200,
              });
            }
            if (
              method === 'GET' &&
              href.endsWith('/api/v1/artifacts/result-tests-live-cd/content')
            ) {
              return new Response(
                `${TESTS_MARKER}\nexit_code=0\nsuite=public`,
                {status: 200},
              );
            }
            throw new Error(`unexpected ${method} ${href}`);
          },
        );

        const evidence = await runOfficialChatDrivenMultistepBrokerWriteRunTests(
          {
            checkoutDir: checkout,
            chat: chat!,
            useInjectedDecisionFsm: false,
            writePath: 'order_service/store.py',
            writeContent: 'fixed_idempotent\n',
            userPrompt:
              '你必须严格按顺序只调用两个工具：' +
              '（1）write_file，path=order_service/store.py，content=fixed_idempotent\\n；' +
              '（2）看到 write 结果后再 run_tests，suite=public。不要其它工具。',
            idleTimeoutMs: 180_000,
            broker: {
              baseUrl: 'http://control.test',
              authorization: 'Bearer worker',
              activation,
              fetchImpl: controlFetch as unknown as typeof fetch,
              poll: {maxAttempts: 3, delayMs: 0},
            },
          },
        );

        expect(evidence.scriptedOrder).toBe(false);
        expect(evidence.chatDrivenOrder).toBe(true);
        expect(evidence.marksGoalDone).toBe(false);
        const names = evidence.trail.map((t) => t.toolName);
        expect(names).toContain('write_file');
        expect(names).toContain('run_tests');
        expect(names.indexOf('write_file')).toBeLessThan(
          names.indexOf('run_tests'),
        );
        expect(prepareN).toBeGreaterThanOrEqual(2);
        expect(prepareTools).toContain('write_file');
        expect(prepareTools).toContain('run_tests');
        expect(
          controlRequests.some((r) => r.url.includes('/dispatch')),
        ).toBe(false);
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(3);
        expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
      },
      200_000,
    );
  },
);
