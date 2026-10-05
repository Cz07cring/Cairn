/**
 * 浏览器调用 Control 的身份：Bearer 或 OIDC session cookie。
 * 写请求用 session 时必须带 CSRF；无万能旁路。
 */
export type BrowserClientAuth =
  | {kind: 'bearer'; authorization: string}
  | {kind: 'session'; csrfToken?: string};

export function resolveBrowserClientAuth(input: {
  authorization?: string;
  auth?: BrowserClientAuth;
}): BrowserClientAuth {
  if (input.auth) {
    return input.auth;
  }
  const token = input.authorization?.trim() ?? '';
  if (!token) {
    throw new Error('缺少身份令牌或浏览器会话');
  }
  return {kind: 'bearer', authorization: token};
}

/** 构造 fetch 的 credentials / Authorization / CSRF。 */
export function browserAuthRequestInit(
  auth: BrowserClientAuth,
  headers: Record<string, string>,
  options?: {mutating?: boolean},
): RequestInit {
  const mutating = options?.mutating === true;
  if (auth.kind === 'bearer') {
    const token = auth.authorization.trim();
    if (!token) {
      throw new Error('缺少身份令牌');
    }
    return {
      credentials: 'same-origin',
      headers: {
        ...headers,
        Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
      },
    };
  }
  const next: Record<string, string> = {...headers};
  if (mutating) {
    const csrf = auth.csrfToken?.trim() ?? '';
    if (!csrf) {
      throw new Error('缺少 CSRF 令牌，无法用会话提交写请求');
    }
    next['X-CSRF-Token'] = csrf;
  }
  return {credentials: 'include', headers: next};
}
