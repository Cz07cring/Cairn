/**
 * 模型调用列表只读观察。
 * SUCCEEDED / usage CONFIRMED ≠ Goal DONE；列表不展示模型原始输入。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type ModelInvocationResource =
  components['schemas']['ModelInvocationResource'];
export type ModelInvocationStatus = ModelInvocationResource['status'];

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

/** GET /api/v1/model-invocations */
export async function listModelInvocations(input: {
  projectId: string;
  goalId?: string;
  activityId?: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<ModelInvocationResource[]> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    project_id: projectId,
    limit: String(input.limit ?? 50),
  });
  if (input.goalId?.trim()) {
    params.set('goal_id', input.goalId.trim());
  }
  if (input.activityId?.trim()) {
    params.set('activity_id', input.activityId.trim());
  }
  const response = await fetch(`/api/v1/model-invocations?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeList<ModelInvocationResource>(
    response,
    '无权读取模型调用列表',
  );
}
