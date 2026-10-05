export type RunLiveTone = 'neutral' | 'active' | 'success' | 'warning' | 'danger';

export type RunLiveRow = {
  id: string;
  source: 'activity' | 'model' | 'effect' | 'command';
  title: string;
  summary: string;
  details: string;
  occurredAt: string | null;
  tone: RunLiveTone;
};

type Timed = {created_at?: string | null; updated_at?: string | null};
type Activity = Timed & {id: string; kind: string; status: string; wait_reason?: string | null};
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
type Command = Timed & {id: string; kind: string; status: string};

export type RunProgressAssessment = {
  state: 'active' | 'queued' | 'waiting' | 'recovering' | 'stalled' | 'terminal';
  label: string;
  message: string;
};

const activityNames: Record<string, string> = {
  PLAN: '规划任务', EXECUTE: '执行任务', AUDIT: '独立验收',
  INTEGRATE: '合并工作成果', RECONCILE: '核对不确定结果',
  FINALIZE: '检查是否可以交付', PROBE_MODEL: '检查模型连接',
  VALIDATE_SKILL: '检查技能', INDEX_MEMORY: '整理记忆',
  EXPORT_EVIDENCE: '整理交付证据',
};
const toolNames: Record<string, string> = {
  read_file: '文件读取', write_file: '文件写入', run_tests: '测试命令',
  shell: '命令行', exec_command: '命令行', git_diff: '代码改动检查',
  browser: '浏览器操作', search: '资料搜索',
};
const toolActions: Record<string, string> = {
  read_file: '读取文件', write_file: '写入文件', run_tests: '运行测试',
  shell: '运行命令', exec_command: '运行命令', git_diff: '查看代码改动',
  browser: '操作浏览器', search: '搜索资料',
};
const commandNames: Record<string, string> = {
  START: '开始运行目标', PAUSE: '请求安全暂停', RESUME: '请求继续运行',
  CANCEL_GOAL: '请求停止目标', CANCEL_TASK: '请求停止任务',
  RETRY_TASK: '请求重新处理任务', REPLAN: '请求重新规划',
  RECOVER_FINALIZATION: '请求恢复交付检查', PROBE_MODEL: '请求检查模型连接',
  VALIDATE_SKILL: '请求检查技能', INDEX_MEMORY: '请求整理记忆',
  RECONCILE_EFFECT: '请求核对不确定结果', EXPORT_EVIDENCE: '请求整理交付证据',
};

function timeOf(item: Timed): string | null {
  return item.updated_at ?? item.created_at ?? null;
}

/** 业务推进与浏览器连接分开判断；只根据权威 Goal/Activity 状态陈述事实。 */
export function assessRunProgress(
  goalStatus: string | null | undefined,
  activities: Activity[],
): RunProgressAssessment {
  if (['DONE', 'FAILED', 'CANCELLED'].includes(goalStatus ?? '')) {
    return {
      state: 'terminal',
      label: goalStatus === 'DONE' ? '目标已经通过最终验收' : '目标已经结束',
      message: `目标当前状态为 ${goalStatus}。`,
    };
  }
  if (activities.some((item) => item.status === 'RUNNING')) {
    return {
      state: 'active', label: '系统记录有步骤正在执行',
      message: '存在 RUNNING 步骤；是否仍有心跳需要结合执行器状态继续核对。',
    };
  }
  if (activities.some((item) => ['PENDING', 'READY'].includes(item.status))) {
    return {
      state: 'queued', label: '任务正在等待执行器',
      message: '已有待处理步骤，但目前没有步骤处于执行中。',
    };
  }
  if (activities.some((item) => item.status === 'WAITING')) {
    return {state: 'waiting', label: '任务正在等待条件', message: '系统记录了等待中的步骤。'};
  }
  if (activities.some((item) => item.status === 'RECOVERING')) {
    return {state: 'recovering', label: '任务正在安全恢复', message: '系统记录了恢复中的步骤。'};
  }
  if (activities.length > 0 && ['PLANNING', 'RUNNING', 'VERIFYING'].includes(goalStatus ?? '')) {
    const latest = [...activities].sort((a, b) =>
      Date.parse(timeOf(b) ?? '') - Date.parse(timeOf(a) ?? ''))[0];
    const ending: Record<string, string> = {
      CANCELLED: '最近一步已经停止', FAILED: '最近一步执行失败', SUCCEEDED: '最近一步已经完成',
    };
    return {
      state: 'stalled', label: '任务没有继续推进',
      message: `目标仍显示运行中，但现在没有执行中的步骤；${ending[latest?.status ?? ''] ?? '当前没有可继续推进的步骤'}。`,
    };
  }
  return {
    state: 'queued', label: '正在等待生成执行步骤',
    message: '目标已经受理，目前还没有可以展示的执行步骤。',
  };
}

function toneFor(status: string): RunLiveTone {
  if (status === 'RUNNING' || status === 'DISPATCHED' || status === 'AUTHORIZED') return 'active';
  if (status === 'SUCCEEDED') return 'success';
  if (status === 'UNKNOWN' || status === 'WAITING' || status === 'RECOVERING') return 'warning';
  if (status === 'FAILED' || status === 'CANCELLED') return 'danger';
  return 'neutral';
}

function progressWord(status: string): string {
  const words: Record<string, string> = {
    PENDING: '等待开始', READY: '已经准备好', RUNNING: '正在进行',
    WAITING: '正在等待条件满足', RECOVERING: '正在安全恢复',
    SUCCEEDED: '这一步已经完成', FAILED: '这一步失败了', CANCELLED: '这一步已停止',
    PREPARED: '已经准备好', AUTHORIZED: '已经获准执行',
    DISPATCHED: '已经发出，正在等待结果', UNKNOWN: '结果还不能确认',
    ACCEPTED: '系统已经收到',
  };
  return words[status] ?? `当前状态：${status}`;
}

function clip(value: string, max = 300): string {
  const clean = value.replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

function toolLabel(name: string): string {
  return toolNames[name] ?? name.replaceAll('_', ' ');
}

function invocationDetails(item: Invocation): string {
  const calls = item.tool_calls ?? [];
  if (!calls.length) return `使用模型 ${item.model_id}；本轮没有请求工具。`;
  return `本轮请求：${calls.map((call) => toolActions[call.name] ?? toolLabel(call.name)).join('、')}。`;
}

function effectTitle(item: Effect): string {
  const tool = toolLabel(item.tool_ref);
  if (item.status === 'SUCCEEDED') return `${tool}执行成功`;
  if (item.status === 'UNKNOWN') return `${tool}结果还不能确认`;
  if (item.status === 'FAILED') return `${tool}执行失败`;
  if (item.status === 'DISPATCHED') return `${tool}已经发出`;
  return `${tool}：${progressWord(item.status)}`;
}

function effectSummary(item: Effect): string {
  if (item.status === 'UNKNOWN') return '系统会先核对已经发生的结果，不会直接再执行一次。';
  const count = item.evidence_ids?.length ?? 0;
  if (item.status === 'SUCCEEDED' && count > 0) {
    return `命令结果已保存为 ${count} 份证据，可在验收页核对原始输出。`;
  }
  if (item.status === 'SUCCEEDED') return '工具已返回成功，但还要经过后续验收。';
  return progressWord(item.status);
}

export function buildRunLiveTimeline(input: {
  activities: Activity[];
  invocations: Invocation[];
  effects: Effect[];
  commands: Command[];
}): RunLiveRow[] {
  const rows: RunLiveRow[] = [];
  for (const item of input.activities) {
    const action = activityNames[item.kind] ?? item.kind;
    rows.push({
      id: `activity:${item.id}`, source: 'activity',
      title: item.status === 'RUNNING' ? `正在${action}` : `${action}：${progressWord(item.status)}`,
      summary: item.wait_reason ? clip(item.wait_reason) : `${progressWord(item.status)}；完成这一步不等于整个目标已经完成。`,
      details: `步骤记录 ${item.id} · 类型 ${item.kind} · 状态 ${item.status}`,
      occurredAt: timeOf(item), tone: toneFor(item.status),
    });
  }
  for (const item of input.invocations) {
    const summary = clip(item.assistant_text ?? '模型已返回结果，暂无可展示的工作摘要。');
    rows.push({
      id: `model:${item.id}`, source: 'model',
      title: item.status === 'SUCCEEDED'
        ? `模型已完成第 ${item.invocation_seq} 次工作`
        : `模型第 ${item.invocation_seq} 次工作：${progressWord(item.status)}`,
      summary,
      details: `${invocationDetails(item)} 技术状态 ${item.status} · 来源 ${item.provider_ref} · 调用 ${item.id}`,
      occurredAt: timeOf(item), tone: toneFor(item.status),
    });
  }
  for (const item of input.effects) {
    rows.push({
      id: `effect:${item.id}`, source: 'effect', title: effectTitle(item),
      summary: effectSummary(item),
      details: `工具 ${item.tool_ref} · 技术状态 ${item.status} · 记录 ${item.id}${item.evidence_ids?.length ? ` · 证据 ${item.evidence_ids.join('、')}` : ''}`,
      occurredAt: timeOf(item), tone: toneFor(item.status),
    });
  }
  for (const item of input.commands) {
    rows.push({
      id: `command:${item.id}`, source: 'command',
      title: commandNames[item.kind] ?? item.kind,
      summary: `${progressWord(item.status)}；控制命令处理成功也不代表目标已经完成。`,
      details: `控制命令 ${item.kind} · 技术状态 ${item.status} · 记录 ${item.id}`,
      occurredAt: timeOf(item), tone: toneFor(item.status),
    });
  }
  return rows.sort((a, b) => Date.parse(b.occurredAt ?? '') - Date.parse(a.occurredAt ?? ''));
}
