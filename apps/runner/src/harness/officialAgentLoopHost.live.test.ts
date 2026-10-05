/**
 * AB01 live：官方 AgentLoop + 真实 chat（DeepSeek/Qwen）；缺 env/checkout 则 skip。
 * ≠ RunActivation 默认；≠ Goal DONE；Broker 真实回执链仍可外接 executeTool。
 */
import {existsSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test} from 'vitest';
import {cordisEntryPath} from './cordisBootGate.js';
import {runOfficialAgentLoopLiveReadFileTurn} from './officialAgentLoopHost.js';
import {isOfficialAgentLoopBuilt} from './officialAgentLoopPaths.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

const MARKER = 'RING_AB01_OFFICIAL_LIVE_MARKER_9c1e';

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
  'AB01 live official AgentLoop + real chat',
  () => {
    test(
      '真实模型经官方 AgentLoop 发 read_file → ToolResult 回灌 → 第二轮引用标记',
      async () => {
        const evidence = await runOfficialAgentLoopLiveReadFileTurn({
          checkoutDir: checkout,
          chat: chat!,
          expectedPath: 'notes/ab01-official.txt',
          userPrompt:
            '你必须调用工具 read_file，参数 path 恰好为 notes/ab01-official.txt。不要输出其它工具。',
          idleTimeoutMs: 180_000,
          executeTool: async (_call) => ({
            content: [
              {
                type: 'text',
                text: `file notes/ab01-official.txt\n${MARKER}\nend`,
              },
            ],
            isError: false,
            meta: {
              effectId: 'live-official-injected-effect',
              status: 'SUCCEEDED',
              evidenceIds: [],
              requiresReconciliation: false,
            },
          }),
        });

        expect(evidence.driver).toBe(
          'official-dsh-agent-loop+openai-compatible',
        );
        expect(evidence.marksGoalDone).toBe(false);
        expect(evidence.toolName).toBe('read_file');
        expect(evidence.modelRounds).toBeGreaterThanOrEqual(2);
        expect(evidence.round2CitesToolResult).toBe(true);
        expect(evidence.assistantTexts.join('\n')).toContain(MARKER);
      },
      200_000,
    );
  },
);
