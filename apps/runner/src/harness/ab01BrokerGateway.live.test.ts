/**
 * AB01 live：真实 chat + 协议形 Broker Gateway（mock Control，无 Runner dispatch）。
 * 缺 chat env 则 skip。完整独立 Broker 进程 live 仍属后续。
 */
import {readFileSync, existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test, vi} from 'vitest';
import {createAb01BrokerExecuteTool} from './ab01BrokerGateway.js';
import {runAb01ReadFileTwoTurn} from './ab01TwoTurnLoop.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

const MARKER = 'RING_AB01_BROKER_LIVE_8d2f';
const FILE_BODY = `live broker evidence\n${MARKER}\n`;

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

describe.skipIf(!chat)('AB01 live × Broker Gateway protocol', () => {
  test(
    '真实模型 read_file → Gateway PREPARED→SUCCEEDED+evidence → 第二轮引用',
    async () => {
      let effectReads = 0;
      const controlFetch = vi.fn(async (url: string | URL, init?: RequestInit) => {
        const href = String(url);
        const method = init?.method ?? 'GET';
        if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
          return Response.json({data: {id: 'input-artifact'}});
        }
        if (method === 'POST' && href.endsWith('/steps')) {
          return Response.json(
            {data: {id: 'step-1', logical_step_id: 'step-1', intent_revision: 1}},
            {status: 201},
          );
        }
        if (method === 'POST' && href.endsWith('/effects/prepare')) {
          return Response.json(
            {data: {id: 'effect-live', status: 'PREPARED', state_revision: 1}},
            {status: 201},
          );
        }
        if (method === 'GET' && href.endsWith('/api/v1/effects/effect-live')) {
          effectReads += 1;
          return Response.json({
            data:
              effectReads === 1
                ? {id: 'effect-live', status: 'DISPATCHED', evidence_ids: []}
                : {
                    id: 'effect-live',
                    status: 'SUCCEEDED',
                    evidence_ids: ['result-live'],
                  },
          });
        }
        if (method === 'GET' && href.endsWith('/api/v1/artifacts/result-live/content')) {
          return new Response(FILE_BODY, {status: 200});
        }
        if (method === 'POST' && href.endsWith('/dispatch')) {
          throw new Error('RUNNER_MUST_NOT_DISPATCH');
        }
        throw new Error(`unexpected ${method} ${href}`);
      });

      const {executeTool} = createAb01BrokerExecuteTool({
        baseUrl: 'http://control.test',
        authorization: 'Bearer worker',
        activation,
        fetchImpl: controlFetch as unknown as typeof fetch,
        poll: {maxAttempts: 5, delayMs: 0},
      });

      const evidence = await runAb01ReadFileTwoTurn({
        chat: chat!,
        expectedPath: 'notes/ab01.txt',
        userPrompt:
          '你必须调用工具 read_file，参数 path 恰好为 notes/ab01.txt。不要输出其它工具。',
        executeTool,
      });

      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.effectStatus).toBe('SUCCEEDED');
      expect(evidence.effectId).toBe('effect-live');
      expect(evidence.round2CitesToolResult).toBe(true);
      expect(evidence.round2.assistantText).toContain(MARKER);
    },
    180_000,
  );
});
