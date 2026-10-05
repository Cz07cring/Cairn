/**
 * live：真实模型诊断全周期 read→run_tests→write→run_tests。
 * useInjectedDecisionFsm=false；tool_choice 仅 auto（DeepSeek thinking 拒收 required）。
 * ≠ Goal DONE。缺 env 则 skip。
 */
import {existsSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAgentLoopChatDrivenDiagnoseCycleTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

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
  'official AgentLoop chat-driven diagnose cycle live',
  () => {
    test(
      '真实模型 read→红测→write→绿测；scriptedOrder=false；≠DONE',
      async () => {
        let runN = 0;
        const evidence =
          await runOfficialAgentLoopChatDrivenDiagnoseCycleTurn({
            checkoutDir: checkout,
            chat: {
              ...chat!,
              // DeepSeek thinking：禁止 required；显式 auto
              toolChoice: 'auto',
              maxTokens: 512,
            },
            useInjectedDecisionFsm: false,
            readPath: 'order_service/store.py',
            writePath: 'order_service/store.py',
            writeContent: 'fixed_idempotent\n',
            userPrompt:
              'You are a coding agent. You MUST use tools; never answer with only text until all four tools are done.\n' +
              'Call tools ONE AT A TIME in this exact order:\n' +
              '1) read_file with path exactly "order_service/store.py"\n' +
              '2) after tool result: run_tests with suite exactly "public"\n' +
              '3) after tool result: write_file path "order_service/store.py" content exactly "fixed_idempotent\\n"\n' +
              '4) after tool result: run_tests suite "public" again\n' +
              'Do not call other tools. Do not skip steps.',
            idleTimeoutMs: 240_000,
            executeTool: async (call) => {
              if (call.name === 'read_file') {
                return {
                  content: [
                    {
                      type: 'text',
                      text: 'READ_BUG_MARKER\ndouble_charge on Idempotency-Key',
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'live-diag-read',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              if (call.name === 'run_tests') {
                runN += 1;
                return {
                  content: [
                    {
                      type: 'text',
                      text:
                        runN === 1
                          ? 'exit_code=1\nsuite=public\nFAIL idempotency'
                          : 'exit_code=0\nsuite=public\nPASS',
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: `live-diag-tests-${runN}`,
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              if (call.name === 'write_file') {
                return {
                  content: [
                    {
                      type: 'text',
                      text: 'WRITE_OK_MARKER\nafter_digest=sha256:live',
                    },
                  ],
                  isError: false,
                  meta: {
                    effectId: 'live-diag-write',
                    status: 'SUCCEEDED',
                    evidenceIds: [],
                    requiresReconciliation: false,
                  },
                };
              }
              throw new Error(`unexpected live diag tool ${call.name}`);
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
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(5);
        expect(evidence.laterRoundsCitePriorToolResults).toBe(true);
      },
      260_000,
    );
  },
);
