/**
 * live：官方 AgentLoop + 真实 chat 多工具（write→run_tests）。
 * useInjectedDecisionFsm=false；scriptedOrder=false；≠ Goal DONE。
 * 缺 env/checkout 则 skip。
 */
import {existsSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

const WRITE_MARKER = 'WRITE_OK_MARKER';
const TESTS_MARKER = 'TESTS_OK_LIVE_exit0';

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
  'official AgentLoop chat-driven multistep live (real model)',
  () => {
    test(
      '真实模型 write_file→run_tests；scriptedOrder=false；无注入 FSM；≠DONE',
      async () => {
        const evidence =
          await runOfficialAgentLoopChatDrivenWriteThenRunTestsTurn({
            checkoutDir: checkout,
            chat: chat!,
            useInjectedDecisionFsm: false,
            writePath: 'order_service/store.py',
            writeContent: 'fixed_idempotent\n',
            userPrompt:
              '你必须严格按顺序调用工具，且只调用这两个：' +
              '（1）write_file，path 恰好为 order_service/store.py，content 恰好为 fixed_idempotent\\n；' +
              '（2）在看到 write_file 的工具结果后，再调用 run_tests，suite 恰好为 public。' +
              '不要调用其它工具。完成后简短引用工具结果中的标记。',
            idleTimeoutMs: 180_000,
            executeTool: async (call) => {
              if (call.name === 'write_file') {
                const args = JSON.parse(call.arguments) as {
                  path?: string;
                  content?: string;
                };
                expect(args.path).toBe('order_service/store.py');
                return {
                  content: [
                    {
                      type: 'text',
                      text: `${WRITE_MARKER}\nafter_digest=sha256:live`,
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'live-cd-write',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              if (call.name === 'run_tests') {
                return {
                  content: [
                    {
                      type: 'text',
                      text: `${TESTS_MARKER}\nexit_code=0\nsuite=public`,
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'live-cd-tests',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              throw new Error(`unexpected live tool ${call.name}`);
            },
          });

        expect(evidence.scriptedOrder).toBe(false);
        expect(evidence.chatDrivenOrder).toBe(true);
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.driver).toBe(
          'official-dsh-agent-loop+openai-compatible',
        );
        const names = evidence.trail.map((t) => t.toolName);
        expect(names).toContain('write_file');
        expect(names).toContain('run_tests');
        expect(names.indexOf('write_file')).toBeLessThan(
          names.indexOf('run_tests'),
        );
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(3);
        expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
      },
      200_000,
    );
  },
);
