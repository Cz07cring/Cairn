/**
 * activation 终止只读列表（M3.5/M4 观察）；marks_goal_done 恒 false；≠ DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type ActivationTerminationResource =
  components['schemas']['ActivationTerminationResource'];

type AuthInput = {
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
};

async function readEnvelopeList<T>(
  response: Response,
  emptyMessage: string,
): Promise<T[]> {
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
    throw new Error(
      detail
        ? `读取 activation 终止失败：${detail}`
        : `读取 activation 终止失败（HTTP ${response.status}）`,
    );
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

/** POST /api/v1/goals/{goal_id}/no-progress-review-ack；须 operator；≠ DONE。 */
export async function postNoProgressReviewAck(
  input: AuthInput & {
    goalId: string;
    body: {expected_state_revision: number; detail?: string | null};
  },
): Promise<ActivationTerminationResource> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/no-progress-review-ack`,
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
    throw new Error('无权确认无进展复盘（须 operator）');
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
    throw new Error(
      detail
        ? `无进展复盘确认失败：${detail}`
        : `无进展复盘确认失败（HTTP ${response.status}）`,
    );
  }
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' ||
    body === null ||
    !('data' in body) ||
    typeof (body as {data: unknown}).data !== 'object' ||
    (body as {data: unknown}).data === null
  ) {
    throw new Error('复盘确认响应无法识别');
  }
  const data = (body as {data: ActivationTerminationResource}).data;
  if (data.marks_goal_done !== false) {
    throw new Error('服务端 marks_goal_done 非 false，拒绝采纳');
  }
  if (data.reason !== 'GOAL_REQUIRES_REVIEW') {
    throw new Error('复盘确认必须返回 GOAL_REQUIRES_REVIEW');
  }
  return data;
}

/** GET /api/v1/goals/{goal_id}/activation-terminations */
export async function listActivationTerminations(
  input: AuthInput & {goalId: string},
): Promise<ActivationTerminationResource[]> {
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/activation-terminations`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeList(response, '无权读取 activation 终止记录');
}
