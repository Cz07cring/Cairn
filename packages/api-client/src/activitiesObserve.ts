/**
 * Goal 活动列表（含无 Task 的 PLAN）；只读观察。
 * Activity SUCCEEDED ≠ Goal/Task DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type ActivityResource = components['schemas']['ActivityResource'];
export type ActivityKind = ActivityResource['kind'];
export type ActivityStatus = ActivityResource['status'];

async function readEnvelopeList<T>(
  response: Response,
  emptyMessage: string,
): Promise<T[]> {
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
    !Array.isArray((body as {data: unknown}).data)
  ) {
    throw new Error('列表响应无法识别');
  }
  return (body as {data: T[]}).data;
}

/** GET /api/v1/goals/{goal_id}/activities */
export async function listGoalActivities(input: {
  goalId: string;
  kind?: ActivityKind;
  status?: ActivityStatus;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<ActivityResource[]> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    limit: String(input.limit ?? 50),
  });
  if (input.kind) {
    params.set('kind', input.kind);
  }
  if (input.status) {
    params.set('status', input.status);
  }
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/activities?${params}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeList<ActivityResource>(response, '无权读取活动列表');
}
