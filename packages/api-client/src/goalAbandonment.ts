/**
 * 编排放弃只读列表与人工解除（Issue #24）；解除 ≠ DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type OrchestrationAbandonmentResource =
  components['schemas']['OrchestrationAbandonmentResource'];
export type GoalResource = components['schemas']['GoalResource'];
export type ControlRequest = components['schemas']['ControlRequest'];

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
    let detail = '';
    try {
      const errBody: unknown = await response.json();
      if (
        typeof errBody === 'object' &&
        errBody !== null &&
        'error' in errBody &&
        typeof (errBody as {error?: {message?: unknown}}).error?.message ===
          'string'
      ) {
        detail = (errBody as {error: {message: string}}).error.message;
      }
    } catch {
      detail = '';
    }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`);
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

/** GET /api/v1/goals/{goal_id}/orchestration-abandonments */
export async function listOrchestrationAbandonments(
  input: AuthInput & {goalId: string},
): Promise<OrchestrationAbandonmentResource[]> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/orchestration-abandonments`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeList(response, '无权读取编排放弃记录');
}

/**
 * POST /api/v1/goals/{goal_id}/orchestration-abandonment-release
 * 须 operator；session 须 CSRF；恢复 previous_status ≠ DONE。
 */
export async function releaseOrchestrationAbandonment(input: AuthInput & {
  goalId: string;
  body: ControlRequest;
}): Promise<GoalResource> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/orchestration-abandonment-release`,
    {
      method: 'POST',
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(
        auth,
        {
          Accept: 'application/json',
          'Content-Type': 'application/json',
        },
        {mutating: true},
      ),
      body: JSON.stringify(input.body),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权解除编排放弃封锁（须 operator）');
  }
  if (response.status === 409) {
    // 读 envelope 错误信息后抛出
    await readEnvelopeData(response, '解除被拒绝：状态冲突或门禁未满足');
  }
  return readEnvelopeData(response, '解除结果不可见');
}
