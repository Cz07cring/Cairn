/**
 * live：真实模型诊断全周期 × Gateway 协议（mock Control）。
 * useInjectedDecisionFsm=false；tool_choice=auto；无 Runner dispatch；≠ Goal DONE。
 */
import {existsSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test, vi} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialChatDrivenDiagnoseBrokerCycle} from './officialChatDrivenDiagnoseBrokerSeam.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

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
  'official chat-driven diagnose × Gateway live',
  () => {
    test(
      '真实模型 read→红→write→绿 × Gateway prepare×4；无 dispatch；≠DONE',
      async () => {
        const controlRequests: Array<{url: string; method: string}> = [];
        let stepN = 0;
        let prepareN = 0;
        const effectReads = new Map<string, number>();
        const prepareTools: string[] = [];
        let runTestsPrepares = 0;

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
              if (toolRef === 'run_tests') runTestsPrepares += 1;
              const id = `effect-live-diag-${prepareN}`;
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
              return Response.json({
                data:
                  n === 1
                    ? {id, status: 'DISPATCHED', evidence_ids: []}
                    : {
                        id,
                        status: 'SUCCEEDED',
                        evidence_ids: [`result-${id}`],
                      },
              });
            }
            if (method === 'GET' && href.includes('/api/v1/artifacts/result-')) {
              // 按 prepare 序号映射证据内容
              const m = href.match(/effect-live-diag-(\d+)/);
              const idx = m ? Number(m[1]) : 0;
              const tool = prepareTools[idx - 1] ?? '';
              let body = 'ok';
              if (tool === 'read_file') {
                body = 'READ_BUG_MARKER\ndouble_charge';
              } else if (tool === 'write_file') {
                body = 'WRITE_OK_MARKER\nafter=sha256:live';
              } else if (tool === 'run_tests') {
                const which = prepareTools
                  .slice(0, idx)
                  .filter((t) => t === 'run_tests').length;
                body =
                  which <= 1
                    ? 'exit_code=1\nsuite=public\nFAIL'
                    : 'exit_code=0\nsuite=public\nPASS';
              }
              return new Response(body, {status: 200});
            }
            throw new Error(`unexpected ${method} ${href}`);
          },
        );

        const evidence = await runOfficialChatDrivenDiagnoseBrokerCycle({
          checkoutDir: checkout,
          chat: {
            ...chat!,
            toolChoice: 'auto',
            maxTokens: 512,
          },
          useInjectedDecisionFsm: false,
          readPath: 'order_service/store.py',
          writePath: 'order_service/store.py',
          writeContent: 'fixed_idempotent\n',
          userPrompt:
            'You MUST use tools; never answer with only text until all four tools are done.\n' +
            'Call ONE AT A TIME in order:\n' +
            '1) read_file path=order_service/store.py\n' +
            '2) run_tests suite=public\n' +
            '3) write_file path=order_service/store.py content=fixed_idempotent\\n\n' +
            '4) run_tests suite=public again\n' +
            'Do not call other tools.',
          idleTimeoutMs: 240_000,
          broker: {
            baseUrl: 'http://control.test',
            authorization: 'Bearer worker',
            activation,
            fetchImpl: controlFetch as unknown as typeof fetch,
            poll: {maxAttempts: 3, delayMs: 0},
          },
        });

        expect(evidence.scriptedOrder).toBe(false);
        expect(evidence.chatDrivenOrder).toBe(true);
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.trail.map((t) => t.toolName)).toEqual([
          'read_file',
          'run_tests',
          'write_file',
          'run_tests',
        ]);
        expect(prepareN).toBe(4);
        expect(runTestsPrepares).toBe(2);
        expect(prepareTools).toContain('read_file');
        expect(prepareTools).toContain('write_file');
        expect(
          controlRequests.some((r) => r.url.includes('/dispatch')),
        ).toBe(false);
        expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
      },
      260_000,
    );

    test(
      '真实模型 read→红→write→绿→seal × Gateway prepare×5；无 dispatch；≠DONE',
      async () => {
        const profileId = '11111111-1111-1111-1111-111111111111';
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
              const id = `effect-live-diag-seal-${prepareN}`;
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
              return Response.json({
                data:
                  n === 1
                    ? {id, status: 'DISPATCHED', evidence_ids: []}
                    : {
                        id,
                        status: 'SUCCEEDED',
                        evidence_ids: [`result-${id}`],
                      },
              });
            }
            if (method === 'GET' && href.includes('/api/v1/artifacts/result-')) {
              const m = href.match(/effect-live-diag-seal-(\d+)/);
              const idx = m ? Number(m[1]) : 0;
              const tool = prepareTools[idx - 1] ?? '';
              let body = 'ok';
              if (tool === 'read_file') {
                body = 'READ_BUG_MARKER\ndouble_charge';
              } else if (tool === 'write_file') {
                body = 'WRITE_OK_MARKER\nafter=sha256:live';
              } else if (tool === 'run_tests') {
                const which = prepareTools
                  .slice(0, idx)
                  .filter((t) => t === 'run_tests').length;
                body =
                  which <= 1
                    ? 'exit_code=1\nsuite=public\nFAIL'
                    : 'exit_code=0\nsuite=public\nPASS';
              } else if (tool === 'seal_candidate') {
                body = 'SEAL_OK_MARKER\ncandidate=ok';
              }
              return new Response(body, {status: 200});
            }
            throw new Error(`unexpected ${method} ${href}`);
          },
        );

        const evidence = await runOfficialChatDrivenDiagnoseBrokerCycle({
          checkoutDir: checkout,
          chat: {
            ...chat!,
            toolChoice: 'auto',
            maxTokens: 512,
          },
          useInjectedDecisionFsm: false,
          sealVerificationProfileIds: [profileId],
          readPath: 'order_service/store.py',
          writePath: 'order_service/store.py',
          writeContent: 'fixed_idempotent\n',
          userPrompt:
            'You MUST use tools; never answer with only text until all five tools are done.\n' +
            'Call ONE AT A TIME in order:\n' +
            '1) read_file path=order_service/store.py\n' +
            '2) run_tests suite=public\n' +
            '3) write_file path=order_service/store.py content=fixed_idempotent\\n\n' +
            '4) run_tests suite=public again\n' +
            `5) seal_candidate with verification_profile_ids=["${profileId}"]\n` +
            'Do not call other tools. marksGoalDone=false does NOT mean stop.',
          idleTimeoutMs: 300_000,
          broker: {
            baseUrl: 'http://control.test',
            authorization: 'Bearer worker',
            activation,
            fetchImpl: controlFetch as unknown as typeof fetch,
            poll: {maxAttempts: 3, delayMs: 0},
          },
        });

        expect(evidence.scriptedOrder).toBe(false);
        expect(evidence.chatDrivenOrder).toBe(true);
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
      320_000,
    );
  },
);
