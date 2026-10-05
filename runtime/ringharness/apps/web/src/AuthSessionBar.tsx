import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {
  buildAuthLoginHref,
  getAuthSession,
  postAuthLogout,
} from '@ring/api-client';
import {
  authSessionCaption,
  type AuthSessionView,
} from './authSessionView.js';

function toView(
  data: Awaited<ReturnType<typeof getAuthSession>> | undefined,
  error: Error | null,
  pending: boolean,
  hasDevBearer: boolean,
): AuthSessionView {
  if (pending) {
    return {kind: 'loading'};
  }
  if (error) {
    if (hasDevBearer) {
      return {kind: 'dev_bearer'};
    }
    return {kind: 'unavailable', message: error.message};
  }
  if (data == null) {
    if (hasDevBearer) {
      return {kind: 'dev_bearer'};
    }
    return {kind: 'anonymous'};
  }
  return {kind: 'signed_in', session: data};
}

/**
 * 页头 OIDC 会话：读 /auth/session、跳转 /auth/login、CSRF 退出登录。
 * 不开启创建目标；会话存在 ≠ Goal DONE。
 */
export function AuthSessionBar() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['auth-session'],
    queryFn: ({signal}) => getAuthSession({signal}),
    retry: false,
    refetchInterval: 60_000,
  });
  const view = toView(
    query.data,
    query.error instanceof Error ? query.error : null,
    query.isPending,
    Boolean(String(import.meta.env.VITE_RING_DEV_BEARER ?? '').trim()),
  );

  const logout = useMutation({
    mutationFn: async () => {
      if (view.kind !== 'signed_in') {
        throw new Error('未登录');
      }
      return postAuthLogout({csrfToken: view.session.csrf_token});
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['auth-session']});
    },
  });

  const loginHref =
    typeof window !== 'undefined'
      ? buildAuthLoginHref(`${window.location.origin}/`)
      : '/api/v1/auth/login?return_to=';

  return (
    <div className="auth-bar" role="status">
      <p className="auth-caption">{authSessionCaption(view)}</p>
      <div className="auth-actions">
        {view.kind === 'anonymous' || view.kind === 'unavailable' ? (
          <a className="auth-login" href={loginHref}>
            登录
          </a>
        ) : null}
        {view.kind === 'signed_in' ? (
          <button
            type="button"
            className="auth-logout"
            disabled={logout.isPending}
            onClick={() => logout.mutate()}
          >
            退出登录
          </button>
        ) : null}
      </div>
      {logout.isError ? (
        <p className="observe-error">
          {logout.error instanceof Error ? logout.error.message : '退出登录失败'}
        </p>
      ) : null}
    </div>
  );
}
