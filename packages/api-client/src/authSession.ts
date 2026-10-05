/**
 * 浏览器 OIDC session：cookie 凭据；无万能 Bearer 旁路。
 * 未登录返回 null；OIDC/会话未配置时由调用方展示诚实空态。
 */
import type {components} from './generated';

export type AuthSession = components['schemas']['AuthSession'];
export type LogoutResult = components['schemas']['LogoutResult'];

const CSRF_HEADER = 'X-CSRF-Token';

async function readEnvelopeData<T>(
  response: Response,
  emptyMessage: string,
): Promise<T> {
  if (!response.ok) {
    throw new Error(
      response.status === 401 || response.status === 403
        ? emptyMessage
        : `请求失败（HTTP ${response.status}）`,
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

/** 构造浏览器导航用的 login 入口（302→IdP）；return_to 须在服务端 allowlist。 */
export function buildAuthLoginHref(returnTo: string): string {
  const target = returnTo.trim();
  if (!target) {
    throw new Error('缺少 return_to');
  }
  return `/api/v1/auth/login?return_to=${encodeURIComponent(target)}`;
}

/**
 * GET /api/v1/auth/session（credentials: include）
 * 401 → null（未登录）；其它错误抛出。
 */
export async function getAuthSession(input?: {
  signal?: AbortSignal;
}): Promise<AuthSession | null> {
  const response = await fetch('/api/v1/auth/session', {
    signal: input?.signal,
    cache: 'no-store',
    credentials: 'include',
    headers: {Accept: 'application/json'},
  });
  if (response.status === 401) {
    return null;
  }
  if (response.status === 503) {
    throw new Error('会话/OIDC 依赖未配置（503）');
  }
  return readEnvelopeData<AuthSession>(response, '需要浏览器会话');
}

/** POST /api/v1/auth/logout；须 CSRF；成功后 cookie 清除。 */
export async function postAuthLogout(input: {
  csrfToken: string;
  signal?: AbortSignal;
}): Promise<LogoutResult> {
  const csrf = input.csrfToken.trim();
  if (!csrf) {
    throw new Error('缺少 CSRF 令牌');
  }
  const response = await fetch('/api/v1/auth/logout', {
    method: 'POST',
    signal: input.signal,
    cache: 'no-store',
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      [CSRF_HEADER]: csrf,
    },
    body: '{}',
  });
  if (response.status === 403) {
    throw new Error('CSRF 校验失败或 Origin 不允许');
  }
  return readEnvelopeData<LogoutResult>(response, '请先登录');
}
