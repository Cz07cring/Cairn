/**
 * Effect 列表与 UNKNOWN 对账；对账 202 ≠ Effect 终态自动改写 ≠ Goal DONE。
 * UNKNOWN 禁止「再执行一次」捷径。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type EffectResource = components['schemas']['EffectResource'];
export type ReconciliationRequest = components['schemas']['ReconciliationRequest'];
export type CommandOperation = components['schemas']['CommandOperation'];

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
  if (response.status === 409) {
    throw new Error('状态冲突或幂等冲突，请刷新后重试');
  }
  if (response.status === 422) {
    throw new Error('对账请求无效（校验失败）');
  }
  if (!response.ok && response.status !== 202) {
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

/** GET /api/v1/effects */
export async function listEffects(input: {
  projectId: string;
  goalId?: string;
  status?: EffectResource['status'];
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<EffectResource[]> {
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
  if (input.status) {
    params.set('status', input.status);
  }
  const response = await fetch(`/api/v1/effects?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeList<EffectResource>(response, '无权读取 effect 列表');
}

/**
 * POST /api/v1/effects/{id}/reconcile
 * 须 approver/admin；202 创建 RECONCILE Activity，不直接改 Effect；≠ DONE。
 */
export async function postEffectReconcile(input: {
  effectId: string;
  idempotencyKey: string;
  body: ReconciliationRequest;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<CommandOperation> {
  const effectId = input.effectId.trim();
  const key = input.idempotencyKey.trim();
  if (!effectId) {
    throw new Error('缺少 effect_id');
  }
  if (!key) {
    throw new Error('缺少 Idempotency-Key');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/effects/${encodeURIComponent(effectId)}/reconcile`,
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
    throw new Error('无权提交对账（须 approver/admin）');
  }
  return readEnvelopeData<CommandOperation>(response, '对账命令不可见');
}
