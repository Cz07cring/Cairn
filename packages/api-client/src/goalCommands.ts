/**
 * 命令列表与 Goal pause/resume/cancel：202 ≠ Goal DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type CommandOperation = components['schemas']['CommandOperation'];
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

/** GET /api/v1/commands */
export async function listCommands(
  input: AuthInput & {
    projectId?: string;
    goalId?: string;
  },
): Promise<CommandOperation[]> {
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({limit: '50'});
  if (input.projectId?.trim()) {
    params.set('project_id', input.projectId.trim());
  }
  if (input.goalId?.trim()) {
    params.set('goal_id', input.goalId.trim());
  }
  const response = await fetch(`/api/v1/commands?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeList(response, '无权读取命令列表');
}

async function postGoalControl(
  pathSuffix: 'pause' | 'resume' | 'cancel',
  input: AuthInput & {
    goalId: string;
    body: ControlRequest;
    idempotencyKey: string;
  },
): Promise<CommandOperation> {
  const goalId = input.goalId.trim();
  const key = input.idempotencyKey.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  if (!key) {
    throw new Error('缺少 Idempotency-Key');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/${pathSuffix}`,
    {
      method: 'POST',
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(
        auth,
        {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': key,
        },
        {mutating: true},
      ),
      body: JSON.stringify(input.body),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error(`无权 ${pathSuffix} Goal（须 operator）`);
  }
  if (response.status === 409) {
    await readEnvelopeData(response, `${pathSuffix} 被拒绝：状态/版本冲突`);
  }
  if (response.status !== 202 && !response.ok) {
    throw new Error(`${pathSuffix} 失败（HTTP ${response.status}）`);
  }
  return readEnvelopeData(response, `${pathSuffix} 命令不可见`);
}

export async function pauseGoal(
  input: AuthInput & {
    goalId: string;
    body: ControlRequest;
    idempotencyKey: string;
  },
): Promise<CommandOperation> {
  return postGoalControl('pause', input);
}

export async function resumeGoal(
  input: AuthInput & {
    goalId: string;
    body: ControlRequest;
    idempotencyKey: string;
  },
): Promise<CommandOperation> {
  return postGoalControl('resume', input);
}

export async function cancelGoal(
  input: AuthInput & {
    goalId: string;
    body: ControlRequest;
    idempotencyKey: string;
  },
): Promise<CommandOperation> {
  return postGoalControl('cancel', input);
}
