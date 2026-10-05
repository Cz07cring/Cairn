/**
 * Goal Plan 列表；只读观察。
 * Plan PUBLISHED ≠ Goal DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type PlanResource = components['schemas']['PlanResource'];

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

/** GET /api/v1/goals/{goal_id}/plans */
export async function listGoalPlans(input: {
  goalId: string;
  cursor?: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<PlanResource[]> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    limit: String(input.limit ?? 50),
  });
  if (input.cursor?.trim()) {
    params.set('cursor', input.cursor.trim());
  }
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/plans?${params}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeList<PlanResource>(response, '无权读取计划列表');
}
