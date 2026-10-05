/**
 * Goal Task 列表；只读观察。
 * Task.status=DONE ≠ Goal DONE（Goal 终局只经 Kernel+VerificationProfile+屏障）。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type TaskResource = components['schemas']['TaskResource'];
export type TaskStatus = TaskResource['status'];

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

/** GET /api/v1/goals/{goal_id}/tasks */
export async function listGoalTasks(input: {
  goalId: string;
  status?: TaskStatus;
  cursor?: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<TaskResource[]> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    limit: String(input.limit ?? 50),
  });
  if (input.status) {
    params.set('status', input.status);
  }
  if (input.cursor?.trim()) {
    params.set('cursor', input.cursor.trim());
  }
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/tasks?${params}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeList<TaskResource>(response, '无权读取任务列表');
}
