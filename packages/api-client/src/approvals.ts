/**
 * 审批列表与裁决/撤销；决策成功 ≠ Effect SUCCEEDED ≠ Goal DONE。
 */
import type {components} from './generated.js';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type ApprovalResource = components['schemas']['ApprovalResource'];
export type ApprovalDecision = components['schemas']['ApprovalDecision'];
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
  if (response.status === 409) {
    throw new Error('审批状态冲突，请刷新后重试');
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

/** GET /api/v1/approvals */
export async function listApprovals(input: {
  projectId: string;
  status?: ApprovalResource['status'];
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<ApprovalResource[]> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const params = new URLSearchParams({
    project_id: projectId,
    limit: String(input.limit ?? 50),
  });
  if (input.status) {
    params.set('status', input.status);
  }
  const response = await fetch(`/api/v1/approvals?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
  });
  return readEnvelopeList<ApprovalResource>(response, '无权读取审批列表');
}

/** POST /api/v1/approvals/{id}/decision；APPROVE/DENY ≠ Effect 成功 ≠ DONE。 */
export async function postApprovalDecision(input: {
  approvalId: string;
  body: ApprovalDecision;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<ApprovalResource> {
  const approvalId = input.approvalId.trim();
  if (!approvalId) {
    throw new Error('缺少 approval_id');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}/decision`,
    {
      method: 'POST',
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      }),
      body: JSON.stringify(input.body),
    },
  );
  return readEnvelopeData<ApprovalResource>(response, '无权裁决该审批');
}

/** POST /api/v1/approvals/{id}/revoke；撤销 ≠ DONE。 */
export async function postApprovalRevoke(input: {
  approvalId: string;
  expectedStateRevision: number;
  reason: string;
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
}): Promise<ApprovalResource> {
  const approvalId = input.approvalId.trim();
  if (!approvalId) {
    throw new Error('缺少 approval_id');
  }
  const reason = input.reason.trim();
  if (!reason) {
    throw new Error('缺少撤销原因');
  }
  const auth = resolveBrowserClientAuth(input);
  const body: ControlRequest = {
    expected_state_revision: input.expectedStateRevision,
    reason,
  };
  const response = await fetch(
    `/api/v1/approvals/${encodeURIComponent(approvalId)}/revoke`,
    {
      method: 'POST',
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      }),
      body: JSON.stringify(body),
    },
  );
  return readEnvelopeData<ApprovalResource>(response, '无权撤销该审批');
}
