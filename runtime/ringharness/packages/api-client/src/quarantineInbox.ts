/**
 * Quarantine inbox 与 SystemStatus 只读客户端；列表/信号 ≠ 结算 ≠ DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type VerificationObligationResource =
  components['schemas']['VerificationObligationResource'];
export type SystemStatus = components['schemas']['SystemStatus'];

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

/** GET /api/v1/verification-obligations?status=QUARANTINED */
export async function listQuarantinedObligations(
  input: AuthInput & {projectId: string},
): Promise<VerificationObligationResource[]> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    project_id: projectId,
    status: 'QUARANTINED',
    limit: '50',
  });
  const response = await fetch(`/api/v1/verification-obligations?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeList(response, '无权读取 quarantine inbox');
}

/** GET /api/v1/system/status?project_id= */
export async function getSystemStatus(
  input: AuthInput & {projectId: string},
): Promise<SystemStatus> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/system/status?project_id=${encodeURIComponent(projectId)}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  return readEnvelopeData(response, '无权读取系统状态');
}
