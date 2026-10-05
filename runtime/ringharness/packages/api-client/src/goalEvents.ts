/**
 * Goal SSE 事件：fetch 流解析；INVALIDATE 只触发重读，不合并 payload 冒充权威。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type GoalSnapshot = components['schemas']['GoalSnapshot'];

export type GoalSseEvent = {
  seq: string;
  type: string;
  schema_version?: number;
  payload?: {
    resource_type?: string;
    change?: string;
    [key: string]: unknown;
  };
  entity_state_revision?: number | null;
};

type AuthInput = {
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
};

async function readEnvelopeData<T>(
  response: Response,
  emptyMessage: string,
): Promise<T> {
  if (response.status === 401 || response.status === 403) {
    throw new Error(emptyMessage);
  }
  if (!response.ok) {
    throw new Error(`请求失败（HTTP ${response.status}）`);
  }
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' ||
    body === null ||
    !('data' in body) ||
    (body as {data: unknown}).data == null
  ) {
    throw new Error('响应无法识别');
  }
  return (body as {data: T}).data;
}

/** GET /api/v1/goals/{goal_id}/snapshot */
export async function getGoalSnapshot(
  input: AuthInput & {goalId: string},
): Promise<GoalSnapshot> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/snapshot`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeData(response, '无权读取 Goal snapshot');
}

/**
 * 解析 SSE 文本块（一个 event 以空行结束）。
 * 注释行（heartbeat）忽略；无 data 则返回 null。
 */
export function parseSseBlock(block: string): GoalSseEvent | null {
  const fields: Record<string, string> = {};
  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) {
      continue;
    }
    const idx = line.indexOf(':');
    if (idx < 0) {
      continue;
    }
    const key = line.slice(0, idx);
    const value = line.slice(idx + 1).replace(/^\s/, '');
    fields[key] = value;
  }
  if (!fields.data) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(fields.data);
  } catch {
    throw new Error('SSE data 非 JSON');
  }
  if (typeof parsed !== 'object' || parsed === null || !('seq' in parsed)) {
    throw new Error('SSE Event 缺 seq');
  }
  const event = parsed as GoalSseEvent;
  if (fields.id && fields.id !== event.seq) {
    throw new Error('SSE id 与 Event.seq 不一致');
  }
  if (fields.event && fields.event !== event.type) {
    throw new Error('SSE event 与 Event.type 不一致');
  }
  return event;
}

export type ConsumeGoalEventsResult = {
  events: GoalSseEvent[];
  lastSeq: string;
  cursorExpired: boolean;
  streamEnded: boolean;
};

/**
 * 拉一轮 events 流：追赶积压后服务端结束；客户端按 lastSeq 续订。
 * 410 → cursorExpired（须重取 snapshot）。
 */
export async function consumeGoalEventsOnce(
  input: AuthInput & {
    goalId: string;
    afterSeq: string;
    maxEvents?: number;
  },
): Promise<ConsumeGoalEventsResult> {
  const goalId = input.goalId.trim();
  const afterSeq = input.afterSeq.trim() || '0';
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  if (!/^(0|[1-9][0-9]*)$/.test(afterSeq)) {
    throw new Error('after_seq 无效');
  }
  const auth = resolveBrowserClientAuth(input);
  const maxEvents = input.maxEvents ?? 50;
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/events?after_seq=${encodeURIComponent(afterSeq)}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'text/event-stream'}),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权订阅 Goal 事件');
  }
  if (response.status === 410) {
    return {
      events: [],
      lastSeq: afterSeq,
      cursorExpired: true,
      streamEnded: true,
    };
  }
  if (!response.ok) {
    throw new Error(`事件流失败（HTTP ${response.status}）`);
  }
  if (!response.body) {
    throw new Error('事件流无 body');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  const events: GoalSseEvent[] = [];
  let lastSeq = afterSeq;
  let cursorExpired = false;

  try {
    while (events.length < maxEvents) {
      const {done, value} = await reader.read();
      if (done) {
        break;
      }
      buf += decoder.decode(value, {stream: true});
      while (buf.includes('\n\n')) {
        const idx = buf.indexOf('\n\n');
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (block.includes('cursor-expired')) {
          cursorExpired = true;
          await reader.cancel();
          return {events, lastSeq, cursorExpired, streamEnded: true};
        }
        const event = parseSseBlock(block);
        if (event == null) {
          continue;
        }
        events.push(event);
        lastSeq = event.seq;
        if (events.length >= maxEvents) {
          await reader.cancel();
          return {events, lastSeq, cursorExpired, streamEnded: false};
        }
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // ignore
    }
  }
  return {events, lastSeq, cursorExpired, streamEnded: true};
}
