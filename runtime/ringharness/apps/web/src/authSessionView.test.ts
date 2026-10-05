import {describe, expect, it} from 'vitest';
import {buildAuthLoginHref, type AuthSession} from '@ring/api-client';
import {
  authSessionCaption,
  createGoalButtonLabel,
  type AuthSessionView,
} from './authSessionView.js';

const session: AuthSession = {
  user_id: 'user-1',
  roles: ['operator', 'viewer'],
  project_ids: ['p1'],
  csrf_token: 'csrf-token-at-least-16',
  expires_at: '2026-09-13T12:00:00Z',
};

describe('authSessionView', () => {
  it('匿名文案使用普通用户语言', () => {
    const view: AuthSessionView = {kind: 'anonymous'};
    expect(authSessionCaption(view)).toContain('尚未登录');
    expect(createGoalButtonLabel(view)).toContain('请先登录');
  });

  it('已登录 operator 可创建 DRAFT，且不宣称 DONE', () => {
    const view: AuthSessionView = {kind: 'signed_in', session};
    const caption = authSessionCaption(view);
    expect(caption).toContain('user-1');
    expect(caption).toContain('不代表目标已经完成');
    expect(caption).toContain('DRAFT');
    expect(createGoalButtonLabel(view)).toContain('DRAFT');
  });

  it('503/依赖不可用失败关闭文案', () => {
    const view: AuthSessionView = {
      kind: 'unavailable',
      message: '会话/OIDC 依赖未配置（503）',
    };
    expect(authSessionCaption(view)).toContain('不会绕过身份检查');
    expect(createGoalButtonLabel(view)).toContain('身份依赖未就绪');
  });
});

describe('buildAuthLoginHref', () => {
  it('编码 return_to', () => {
    expect(buildAuthLoginHref('http://127.0.0.1:58102/')).toBe(
      '/api/v1/auth/login?return_to=http%3A%2F%2F127.0.0.1%3A58102%2F',
    );
  });

  it('空 return_to 失败关闭', () => {
    expect(() => buildAuthLoginHref('')).toThrow(/return_to/);
  });
});
