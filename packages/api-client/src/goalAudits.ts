import type {components, paths} from './generated';
import {
  browserAuthRequestInit,
  resolveBrowserClientAuth,
  type BrowserClientAuth,
} from './clientAuth.js';

export type GoalAuditItem = NonNullable<
  paths['/api/v1/goals/{goal_id}/audits']['get']['responses'][200]['content']['application/json']['data']
>[number];

export type GoalReviewResource = components['schemas']['GoalReviewResource'];

/**
 * 只读拉取 Goal 联合审计列表（候选验收 ∪ GOAL_REVIEW）。
 * 支持 Bearer 或 session cookie；不把空列表伪装成「无诊断」。
 */
export async function listGoalAuditItems(input: {
  goalId: string;
  /** @deprecated 优先传 auth */
  authorization?: string;
  auth?: BrowserClientAuth;
  signal?: AbortSignal;
  limit?: number;
}): Promise<GoalAuditItem[]> {
  const auth = resolveBrowserClientAuth(input);
  const goalId = input.goalId.trim();
  if (!goalId) {
    throw new Error('缺少 goal_id');
  }
  const limit = input.limit ?? 50;
  const response = await fetch(
    `/api/v1/goals/${encodeURIComponent(goalId)}/audits?limit=${limit}`,
    {
      signal: input.signal,
      cache: 'no-store',
      ...browserAuthRequestInit(auth, {Accept: 'application/json'}),
    },
  );
  if (response.status === 401 || response.status === 403) {
    throw new Error('无权读取该 Goal 的审计列表');
  }
  if (response.status === 404) {
    throw new Error('Goal 不存在或不可见');
  }
  if (!response.ok) {
    throw new Error(`读取审计失败（HTTP ${response.status}）`);
  }
  const body: unknown = await response.json();
  if (
    typeof body !== 'object' ||
    body === null ||
    !('data' in body) ||
    !Array.isArray((body as {data: unknown}).data)
  ) {
    throw new Error('审计响应无法识别');
  }
  return (body as {data: GoalAuditItem[]}).data;
}
