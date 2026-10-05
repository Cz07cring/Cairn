/**
 * Goal 工作台只读列表、创建 DRAFT 与 START：Bearer 或 OIDC session。
 * 创建/START 成功 ≠ Goal DONE。
 */
import type {components} from './generated';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type ProjectResource = components['schemas']['ProjectResource'];
export type GoalResource = components['schemas']['GoalResource'];
export type GoalCreate = components['schemas']['GoalCreate'];
export type ControlRequest = components['schemas']['ControlRequest'];
export type CommandOperation = components['schemas']['CommandOperation'];
export type PolicyResource = components['schemas']['PolicyResource'];
export type ModelProfileResource = components['schemas']['ModelProfileResource'];
export type SkillSetResource = components['schemas']['SkillSetResource'];
export type VerificationProfileResource =
  components['schemas']['VerificationProfileResource'];

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
    throw new Error(
      detail || `请求失败（HTTP ${response.status}）`,
    );
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

type AuthInput = {
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
};

function authInit(input: AuthInput, mutating = false): RequestInit {
  const auth = resolveBrowserClientAuth(input);
  return browserAuthRequestInit(
    auth,
    {Accept: 'application/json', ...(mutating ? {'Content-Type': 'application/json'} : {})},
    {mutating},
  );
}

/** GET /api/v1/projects */
export async function listProjects(input: AuthInput): Promise<ProjectResource[]> {
  const response = await fetch('/api/v1/projects?limit=50', {
    signal: input.signal,
    cache: 'no-store',
    ...authInit(input),
  });
  return readEnvelopeList(response, '无权读取项目列表');
}

/** GET /api/v1/goals?project_id= */
export async function listGoals(input: AuthInput & {
  projectId: string;
  status?: string;
}): Promise<GoalResource[]> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const params = new URLSearchParams({
    project_id: projectId,
    limit: '50',
  });
  if (input.status) {
    params.set('status', input.status);
  }
  const response = await fetch(`/api/v1/goals?${params}`, {
    signal: input.signal,
    cache: 'no-store',
    ...authInit(input),
  });
  return readEnvelopeList(response, '无权读取 Goal 列表');
}

async function listProjectScoped<T>(
  path: string,
  input: AuthInput & {projectId: string},
): Promise<T[]> {
  const projectId = input.projectId.trim();
  if (!projectId) {
    throw new Error('缺少 project_id');
  }
  const response = await fetch(
    `${path}?project_id=${encodeURIComponent(projectId)}&limit=50`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...authInit(input),
    },
  );
  return readEnvelopeList(response, `无权读取 ${path}`);
}

export async function listPolicies(
  input: AuthInput & {projectId: string},
): Promise<PolicyResource[]> {
  return listProjectScoped('/api/v1/policies', input);
}

export async function listModelProfiles(
  input: AuthInput & {projectId: string},
): Promise<ModelProfileResource[]> {
  return listProjectScoped('/api/v1/model-profiles', input);
}

export async function listSkillSets(
  input: AuthInput & {projectId: string},
): Promise<SkillSetResource[]> {
  return listProjectScoped('/api/v1/skill-sets', input);
}

export async function listVerificationProfiles(
  input: AuthInput & {projectId: string},
): Promise<VerificationProfileResource[]> {
  return listProjectScoped('/api/v1/verification-profiles', input);
}

/**
 * POST /api/v1/goals → DRAFT。
 * 须 operator；session 须 CSRF；201 ≠ START ≠ DONE。
 */
export async function createGoal(input: AuthInput & {
  body: GoalCreate;
  idempotencyKey: string;
}): Promise<GoalResource> {
  const key = input.idempotencyKey.trim();
  if (!key) {
    throw new Error('缺少 Idempotency-Key');
  }
  const auth = resolveBrowserClientAuth(input);
  const response = await fetch('/api/v1/goals', {
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
  });
  if (response.status === 403) {
    throw new Error('无权创建 Goal（须 operator）');
  }
  return readEnvelopeData(response, '请先登录');
}

/**
 * POST /api/v1/goals/{goal_id}/start → 202 CommandOperation。
 * 须 operator；session 须 CSRF；命令 SUCCEEDED / final_status=PLANNING ≠ DONE。
 */
export async function startGoal(input: AuthInput & {
  goalId: string;
  body: ControlRequest;
  idempotencyKey: string;
}): Promise<CommandOperation> {
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
    `/api/v1/goals/${encodeURIComponent(goalId)}/start`,
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
    throw new Error('无权 START Goal（须 operator）');
  }
  if (response.status === 409) {
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
    throw new Error(detail || 'START 被拒绝：状态/版本冲突或编排门禁');
  }
  if (response.status !== 202 && !response.ok) {
    throw new Error(`START 失败（HTTP ${response.status}）`);
  }
  return readEnvelopeData(response, 'START 命令不可见');
}
