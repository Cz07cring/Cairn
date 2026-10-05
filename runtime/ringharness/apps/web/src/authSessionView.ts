/**
 * OIDC 会话栏投影：登录后 operator 可创建 DRAFT；≠ Goal DONE。
 */
import type {AuthSession} from '@ring/api-client';

export type AuthSessionView =
  | {kind: 'loading'}
  | {kind: 'anonymous'}
  | {kind: 'dev_bearer'}
  | {kind: 'signed_in'; session: AuthSession}
  | {kind: 'unavailable'; message: string};

export function authSessionCaption(view: AuthSessionView): string {
  switch (view.kind) {
    case 'loading':
      return '正在读取浏览器会话…';
    case 'anonymous':
      return '尚未登录。登录后可以创建目标、查看执行进度并处理待办事项。';
    case 'dev_bearer':
      return '开发调试身份已启用；可用操作以服务端授权为准，生产环境请使用正式登录。';
    case 'signed_in': {
      const canCreate = view.session.roles.includes('operator');
      const createHint = canCreate
        ? '可创建 Goal DRAFT 并 START（命令受理 ≠ DONE）'
        : '当前角色不能创建 Goal（需要 operator）';
      return `已登录 ${view.session.user_id}；当前权限：${view.session.roles.join(', ')}。${createHint}；登录成功不代表目标已经完成。`;
    }
    case 'unavailable':
      return `登录服务暂时不可用：${view.message}。系统不会绕过身份检查。`;
    default: {
      const _exhaustive: never = view;
      return String(_exhaustive);
    }
  }
}

/** 创建目标入口：operator 可开 DRAFT；仍不宣称 DONE。 */
export function createGoalButtonLabel(view: AuthSessionView): string {
  if (view.kind === 'signed_in') {
    if (view.session.roles.includes('operator')) {
      return '创建目标（DRAFT）';
    }
    return '创建目标 · 需要 operator 角色';
  }
  if (view.kind === 'dev_bearer') {
    return '创建目标（DRAFT）· 开发身份';
  }
  if (view.kind === 'unavailable') {
    return '创建目标 · 身份依赖未就绪';
  }
  return '创建目标 · 请先登录';
}
