import {describe, expect, it} from 'vitest';
import {agentChatComposeGate, buildAgentChatMessages} from './agentChatView.js';

describe('buildAgentChatMessages', () => {
  it('按时间排成助手与工具气泡，不出现思维链字样', () => {
    const messages = buildAgentChatMessages({
      activities: [
        {
          id: 'a1',
          kind: 'EXECUTE',
          status: 'RUNNING',
          updated_at: '2026-09-14T01:00:00Z',
        },
      ],
      invocations: [
        {
          id: 'inv-1',
          status: 'SUCCEEDED',
          invocation_seq: 1,
          model_id: 'deepseek-flash',
          provider_ref: 'deepseek:api',
          assistant_text: '先读订单服务再改幂等键。',
          tool_calls: [{name: 'read_file', arguments: {path: 'orders.py'}}],
          updated_at: '2026-09-14T01:01:00Z',
        },
      ],
      effects: [
        {
          id: 'eff-1',
          status: 'SUCCEEDED',
          tool_ref: 'read_file',
          evidence_ids: ['art-1'],
          updated_at: '2026-09-14T01:02:00Z',
        },
      ],
    });

    expect(messages.map((m) => m.role)).toEqual(['system', 'assistant', 'tool']);
    expect(messages[1]?.body).toContain('先读订单服务');
    expect(messages[1]?.meta).toContain('deepseek-flash');
    expect(messages[2]?.body).toContain('读取文件');
    expect(messages[2]?.body).toContain('1 份证据');
    expect(JSON.stringify(messages)).not.toContain('chain-of-thought');
    expect(JSON.stringify(messages)).not.toContain('私有推理链内容');
  });

  it('UNKNOWN 工具气泡引导核对而非重试', () => {
    const messages = buildAgentChatMessages({
      invocations: [],
      effects: [
        {
          id: 'u1',
          status: 'UNKNOWN',
          tool_ref: 'write_file',
          updated_at: '2026-09-14T02:00:00Z',
        },
      ],
    });
    expect(messages[0]?.role).toBe('tool');
    expect(messages[0]?.tone).toBe('warning');
    expect(messages[0]?.body).toContain('不会');
    expect(messages[0]?.body).toContain('重复执行');
  });
});

describe('agentChatComposeGate', () => {
  it('缺 Goal 或未接通用户消息 API 时失败关闭', () => {
    expect(agentChatComposeGate({hasObserveGoal: false, userMessageApiReady: false}).ok).toBe(
      false,
    );
    expect(agentChatComposeGate({hasObserveGoal: true, userMessageApiReady: false})).toMatchObject({
      ok: false,
    });
    expect(agentChatComposeGate({hasObserveGoal: true, userMessageApiReady: true})).toEqual({
      ok: true,
    });
  });
});
