/**
 * AB01 live：真实 chat（DeepSeek/Qwen）+ 注入工具结果；缺 env 则 skip。
 * 不经官方 AgentLoop；不写 Goal DONE；Broker 真实通路仍属后续（本文件用确定性 executeTool）。
 */
import {readFileSync, existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe, expect, test} from 'vitest';
import {runAb01ReadFileTwoTurn} from './ab01TwoTurnLoop.js';
import {toolChatConfigFromEnv} from './openaiCompatibleToolChat.js';

const MARKER = 'RING_AB01_LIVE_MARKER_7e2a';

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

describe.skipIf(!chat)('AB01 live two-turn (real chat, injected tool)', () => {
  test(
    '模型发 read_file → ToolResult 按 tool_call_id 回灌 → 第二轮引用标记',
    async () => {
      const evidence = await runAb01ReadFileTwoTurn({
        chat: chat!,
        expectedPath: 'notes/ab01.txt',
        userPrompt: `你必须调用工具 read_file，参数 path 恰好为 notes/ab01.txt。不要输出其它工具。`,
        executeTool: async (_call) => ({
          content: [
            {
              type: 'text',
              text: `file notes/ab01.txt\n${MARKER}\nend`,
            },
          ],
          isError: false,
          meta: {
            effectId: 'live-injected-effect',
            status: 'SUCCEEDED',
            evidenceIds: [],
            requiresReconciliation: false,
          },
        }),
      });

      expect(evidence.marksGoalDone).toBe(false);
      expect(evidence.toolName).toBe('read_file');
      expect(evidence.toolCallId.length).toBeGreaterThan(4);
      expect(evidence.round2CitesToolResult).toBe(true);
      expect(evidence.round2.assistantText).toContain(MARKER);
    },
    180_000,
  );
});
