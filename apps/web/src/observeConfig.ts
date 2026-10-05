/**
 * 观察面板身份：优先 OIDC session，其次 DEV_BEARER；须显式 goal_id。
 * 会话存在 ≠ Goal DONE；不自动开启创建目标。
 */
import type {AuthSession, BrowserClientAuth} from '@ring/api-client';

export type ObserveConfig = {
  goalId: string;
  auth: BrowserClientAuth;
  source: 'session' | 'dev_bearer';
};

export function resolveObserveConfig(input: {
  goalIdEnv: string;
  bearerEnv: string;
  session: AuthSession | null | undefined;
  /** localStorage / 选择器覆盖；优先于 env */
  storedGoalId?: string;
}): ObserveConfig | null {
  const goalId = (input.storedGoalId ?? '').trim() || input.goalIdEnv.trim();
  if (!goalId) {
    return null;
  }
  if (input.session) {
    return {
      goalId,
      auth: {kind: 'session', csrfToken: input.session.csrf_token},
      source: 'session',
    };
  }
  const bearer = input.bearerEnv.trim();
  if (bearer) {
    return {
      goalId,
      auth: {kind: 'bearer', authorization: bearer},
      source: 'dev_bearer',
    };
  }
  return null;
}

export function observeConfigEmptyHint(cfg: ObserveConfig | null): string {
  if (cfg != null) {
    return cfg.source === 'session'
      ? '使用浏览器 OIDC 会话读取（须 operator 才能提交 recovery）。'
      : '使用 VITE_RING_DEV_BEARER 开发令牌读取；生产应走 OIDC session。';
  }
  return '需选择/创建 Goal，或设置 VITE_RING_GOAL_ID，并完成 OIDC 登录或配置 VITE_RING_DEV_BEARER；未配置时不用示例数据冒充。';
}
