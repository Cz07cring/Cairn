/**
 * 本仓 Agent 聊天投影：把 Kernel 权威事实排成 Codex/Harness 风格对话流。
 * - 不编造私有推理 / chain-of-thought
 * - 对话气泡 ≠ Goal DONE
 * - 用户可发送消息须另接受控入口（本投影层不假装已接通）
 */

export type AgentChatRole = 'system' | 'assistant' | 'tool' | 'user';

export type AgentChatTone = 'neutral' | 'active' | 'success' | 'warning' | 'danger';

export type AgentChatMessage = {
  id: string;
  role: AgentChatRole;
  /** 气泡主文案（已脱敏事实或模型主动写出的摘要） */
  body: string;
  /** 次行说明：工具名、状态等 */
  meta: string;
  occurredAt: string | null;
  tone: AgentChatTone;
  /** 技术详情（可折叠） */
  details: string;
};

type Timed = {created_at?: string | null; updated_at?: string | null};

type Invocation = Timed & {
  id: string;
  status: string;
  invocation_seq: number;
  model_id: string;
  provider_ref: string;
  assistant_text?: string | null;
  tool_calls?: Array<{name: string; arguments?: unknown}> | null;
};

type Effect = Timed & {
  id: string;
  status: string;
  tool_ref: string;
  evidence_ids?: string[];
};

type Activity = Timed & {
  id: string;
  kind: string;
  status: string;
  wait_reason?: string | null;
};

const toolNames: Record<string, string> = {
  read_file: '读取文件',
  write_file: '写入文件',
  run_tests: '运行测试',
  shell: '运行命令',
  exec_command: '运行命令',
  git_diff: '查看代码改动',
  browser: '浏览器操作',
  search: '资料搜索',
};

function timeOf(item: Timed): string | null {
  return item.updated_at ?? item.created_at ?? null;
}

function clip(value: string, max = 800): string {
  const clean = value.replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

function toneFor(status: string): AgentChatTone {
  if (status === 'RUNNING' || status === 'DISPATCHED' || status === 'AUTHORIZED') {
    return 'active';
  }
  if (status === 'SUCCEEDED') return 'success';
  if (status === 'UNKNOWN' || status === 'WAITING' || status === 'RECOVERING') {
    return 'warning';
  }
  if (status === 'FAILED' || status === 'CANCELLED') return 'danger';
  return 'neutral';
}

function toolLabel(name: string): string {
  return toolNames[name] ?? name.replaceAll('_', ' ');
}

function sortKey(occurredAt: string | null, id: string): string {
  return `${occurredAt ?? ''}\0${id}`;
}

/**
 * 将模型轮次与工具 Effect 投影为聊天消息（时间升序，便于跟流）。
 * Activity 治理态压成一条 system 提示，避免冲淡对话感。
 */
export function buildAgentChatMessages(input: {
  activities?: Activity[];
  invocations: Invocation[];
  effects: Effect[];
}): AgentChatMessage[] {
  const messages: AgentChatMessage[] = [];

  const running = (input.activities ?? []).filter((a) => a.status === 'RUNNING');
  if (running.length > 0) {
    const kinds = [...new Set(running.map((a) => a.kind))].join('、');
    messages.push({
      id: 'system:running',
      role: 'system',
      body: `执行腿有 ${running.length} 个步骤正在进行（${kinds}）。下方是模型摘要与工具回执；不是私有推理链。`,
      meta: '治理提示 · 步骤成功 ≠ Goal DONE',
      occurredAt: timeOf(running[0]!),
      tone: 'active',
      details: running.map((a) => `${a.kind} ${a.id} ${a.status}`).join('\n'),
    });
  }

  for (const item of input.invocations) {
    const calls = item.tool_calls ?? [];
    const callLine =
      calls.length === 0
        ? '本轮未请求工具'
        : `请求工具：${calls.map((c) => toolLabel(c.name)).join('、')}`;
    const body = clip(
      item.assistant_text?.trim() ||
        (item.status === 'SUCCEEDED'
          ? '模型本轮已完成；暂无工作摘要文本。'
          : `模型第 ${item.invocation_seq} 轮：${item.status}`),
    );
    messages.push({
      id: `assistant:${item.id}`,
      role: 'assistant',
      body,
      meta: `助手 · 第 ${item.invocation_seq} 轮 · ${item.model_id} · ${item.status}`,
      occurredAt: timeOf(item),
      tone: toneFor(item.status),
      details: `${callLine}\nprovider ${item.provider_ref}\ninvocation ${item.id}`,
    });
  }

  for (const item of input.effects) {
    const tool = toolLabel(item.tool_ref);
    let body: string;
    if (item.status === 'UNKNOWN') {
      body = `${tool}：结果 UNKNOWN。系统会先核对已发生事实，不会为「看起来卡住」而重复执行。`;
    } else if (item.status === 'SUCCEEDED') {
      const n = item.evidence_ids?.length ?? 0;
      body =
        n > 0
          ? `${tool} 已成功；已封存 ${n} 份证据（可在验收页核对）。`
          : `${tool} 已成功；仍须后续验收，≠ Goal DONE。`;
    } else if (item.status === 'DISPATCHED') {
      body = `${tool} 已发出，等待可信回执…`;
    } else if (item.status === 'FAILED') {
      body = `${tool} 执行失败（状态 FAILED）。`;
    } else {
      body = `${tool}：${item.status}`;
    }
    messages.push({
      id: `tool:${item.id}`,
      role: 'tool',
      body,
      meta: `工具 · ${item.tool_ref} · ${item.status}`,
      occurredAt: timeOf(item),
      tone: toneFor(item.status),
      details: `effect ${item.id}${
        item.evidence_ids?.length ? `\nevidence ${item.evidence_ids.join(', ')}` : ''
      }`,
    });
  }

  return messages.sort((a, b) =>
    sortKey(a.occurredAt, a.id).localeCompare(sortKey(b.occurredAt, b.id)),
  );
}

/** Composer：公开用户消息入 activation 的 API 尚未落地时失败关闭。 */
export function agentChatComposeGate(input: {
  hasObserveGoal: boolean;
  userMessageApiReady: boolean;
}): {ok: true} | {ok: false; reason: string} {
  if (!input.hasObserveGoal) {
    return {ok: false, reason: '请先选择要观察的目标。'};
  }
  if (!input.userMessageApiReady) {
    return {
      ok: false,
      reason:
        '用户消息入执行腿的受控入口尚未接通；当前可跟流阅读，不能从聊天框直达模型。',
    };
  }
  return {ok: true};
}
