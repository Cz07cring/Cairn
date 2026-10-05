import {describe, expect, it} from 'vitest';
import type {AuthSession} from '@ring/api-client';
import {
  observeConfigEmptyHint,
  resolveObserveConfig,
} from './observeConfig.js';

const session: AuthSession = {
  user_id: 'u1',
  roles: ['operator'],
  project_ids: ['p1'],
  csrf_token: 'csrf-token-16chars',
  expires_at: '2026-09-13T12:00:00Z',
};

describe('resolveObserveConfig', () => {
  it('无 goal_id → null', () => {
    expect(
      resolveObserveConfig({
        goalIdEnv: '',
        bearerEnv: 'tok',
        session,
      }),
    ).toBeNull();
  });

  it('优先 session 而非 DEV_BEARER', () => {
    const cfg = resolveObserveConfig({
      goalIdEnv: 'g1',
      bearerEnv: 'dev-token',
      session,
    });
    expect(cfg?.source).toBe('session');
    expect(cfg?.auth).toEqual({
      kind: 'session',
      csrfToken: 'csrf-token-16chars',
    });
  });

  it('无 session 时回退 DEV_BEARER', () => {
    const cfg = resolveObserveConfig({
      goalIdEnv: 'g1',
      bearerEnv: 'dev-token',
      session: null,
    });
    expect(cfg?.source).toBe('dev_bearer');
    expect(cfg?.auth.kind).toBe('bearer');
  });

  it('storedGoalId 优先于 env', () => {
    const cfg = resolveObserveConfig({
      goalIdEnv: 'env-goal',
      bearerEnv: 'tok',
      session: null,
      storedGoalId: 'stored-goal',
    });
    expect(cfg?.goalId).toBe('stored-goal');
  });

  it('空态提示诚实', () => {
    expect(observeConfigEmptyHint(null)).toMatch(/Goal|VITE_RING/);
    expect(
      observeConfigEmptyHint({
        goalId: 'g1',
        auth: {kind: 'session', csrfToken: 'x'},
        source: 'session',
      }),
    ).toContain('OIDC');
  });
});
