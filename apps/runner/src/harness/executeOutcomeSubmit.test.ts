/**
 * EXECUTE 活动回执提交单测（不打真网）。
 *
 * 背景（2026-09-15 实测）：Kernel 的活动行只有收到
 * `POST /internal/v1/activities/{id}/outcomes` 才离开 `RUNNING`。Runner 侧此前
 * PLAN / AUDIT / FINALIZE / INTEGRATE 都有回执提交方，**唯独 EXECUTE 没有**，
 * 于是执行腿干完活后 Temporal 侧已 COMPLETED、Kernel 侧永停 RUNNING，
 * 编排工作流的观察循环永远等不到变化（控制面 15 次 outcomes 全给 PLAN，EXECUTE 0 次）。
 *
 * 本文件锁住的边界：
 *  1. 正常路径按「封印 effect → 证据工件 → candidate_manifest_id」反查并提交；
 *  2. 各失败分支**只报因、不抛、不冒充成功**（回执失败不得把已完成的执行谎报为失败）；
 *  3. `expected_state_revision` 必须**现读**，不得沿用 claim 时的旧值（Kernel 乐观并发校验）。
 */
import {describe, expect, test, vi} from 'vitest';
import {
  candidateManifestIdFromEvidence,
  createHttpExecuteOutcomeDeps,
  submitExecuteOutcome,
  type ExecuteOutcomeDeps,
} from './executeOutcomeSubmit.js';

const ACTIVITY = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
const ATTEMPT = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb';
const SEAL_EFFECT = 'cccccccc-3333-4333-8333-cccccccccccc';
const READ_EFFECT = 'dddddddd-4444-4444-8444-dddddddddddd';
const EVIDENCE = 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee';
const CANDIDATE = 'ffffffff-6666-4666-8666-ffffffffffff';

/** 组装一套默认可用的端口，按需覆盖。 */
function deps(overrides: Partial<ExecuteOutcomeDeps> = {}): ExecuteOutcomeDeps {
  return {
    readActivity: vi.fn(async () => ({stateRevision: 7, status: 'RUNNING'})),
    readEffect: vi.fn(async (effectId: string) =>
      effectId === SEAL_EFFECT
        ? {toolRef: 'seal_candidate', status: 'SUCCEEDED', evidenceIds: [EVIDENCE]}
        : {toolRef: 'read_file', status: 'SUCCEEDED', evidenceIds: []},
    ),
    readArtifactContent: vi.fn(async () =>
      JSON.stringify({file_count: 50, candidate_manifest_id: CANDIDATE}),
    ),
    postOutcome: vi.fn(async () => undefined),
    ...overrides,
  };
}

function call(d: ExecuteOutcomeDeps, effectIds: string[] = [READ_EFFECT, SEAL_EFFECT]) {
  return submitExecuteOutcome({
    deps: d,
    activityId: ACTIVITY,
    attemptId: ATTEMPT,
    fencingEpoch: '3',
    effectIds,
  });
}

describe('EXECUTE 活动回执提交', () => {
  test('正常路径：反查候选并提交，带现读的状态版本与租约身份', async () => {
    const d = deps();
    const result = await call(d);

    expect(result).toEqual({
      submitted: true,
      candidateManifestId: CANDIDATE,
      evidenceIds: [EVIDENCE],
    });
    expect(d.postOutcome).toHaveBeenCalledTimes(1);
    expect(d.postOutcome).toHaveBeenCalledWith({
      activityId: ACTIVITY,
      lease: {activity_id: ACTIVITY, attempt_id: ATTEMPT, fencing_epoch: '3'},
      // 必须是现读值（7），不是 claim 时的旧值
      expectedStateRevision: 7,
      candidateManifestId: CANDIDATE,
      evidenceIds: [EVIDENCE],
    });
  });

  test('无 effect：不提交（NO_EFFECTS）', async () => {
    const d = deps();
    const result = await call(d, []);
    expect(result).toEqual({submitted: false, reason: 'NO_EFFECTS'});
    expect(d.postOutcome).not.toHaveBeenCalled();
  });

  test('没有成功的封印 effect：不提交（NO_SUCCESSFUL_SEAL_EFFECT）', async () => {
    const d = deps({
      readEffect: vi.fn(async () => ({
        toolRef: 'read_file',
        status: 'SUCCEEDED',
        evidenceIds: [],
      })),
    });
    const result = await call(d);
    expect(result).toEqual({submitted: false, reason: 'NO_SUCCESSFUL_SEAL_EFFECT'});
    expect(d.postOutcome).not.toHaveBeenCalled();
  });

  test('封印未 SUCCEEDED（如 FAILED）不算数', async () => {
    const d = deps({
      readEffect: vi.fn(async () => ({
        toolRef: 'seal_candidate',
        status: 'FAILED',
        evidenceIds: [EVIDENCE],
      })),
    });
    const result = await call(d);
    expect(result).toEqual({submitted: false, reason: 'NO_SUCCESSFUL_SEAL_EFFECT'});
  });

  test('证据里没有候选 id：不提交（CANDIDATE_NOT_FOUND_IN_SEAL_EVIDENCE）', async () => {
    const d = deps({
      readArtifactContent: vi.fn(async () => JSON.stringify({file_count: 3})),
    });
    const result = await call(d);
    expect(result).toEqual({
      submitted: false,
      reason: 'CANDIDATE_NOT_FOUND_IN_SEAL_EVIDENCE',
    });
    expect(d.postOutcome).not.toHaveBeenCalled();
  });

  test('活动已非 RUNNING：不重复提交（ACTIVITY_NOT_RUNNING）', async () => {
    const d = deps({
      readActivity: vi.fn(async () => ({stateRevision: 9, status: 'SUCCEEDED'})),
    });
    const result = await call(d);
    expect(result).toEqual({submitted: false, reason: 'ACTIVITY_NOT_RUNNING:SUCCEEDED'});
    expect(d.postOutcome).not.toHaveBeenCalled();
  });

  test('回执 POST 失败：只报因，不抛（不得把已完成的执行谎报为失败）', async () => {
    const d = deps({
      postOutcome: vi.fn(async () => {
        throw new Error('EXECUTE_OUTCOME_POST_FAILED: HTTP 409');
      }),
    });
    const result = await call(d);
    expect(result.submitted).toBe(false);
    if (!result.submitted) {
      expect(result.reason).toContain('HTTP 409');
    }
  });

  test('读活动失败：只报因，不抛', async () => {
    const d = deps({
      readActivity: vi.fn(async () => {
        throw new Error('HTTP 404');
      }),
    });
    const result = await call(d);
    expect(result.submitted).toBe(false);
    if (!result.submitted) {
      expect(result.reason).toContain('ACTIVITY_READ_FAILED');
    }
  });

  test('单个 effect 读失败不影响其它 effect 的判定', async () => {
    const d = deps({
      readEffect: vi.fn(async (effectId: string) => {
        if (effectId === READ_EFFECT) throw new Error('HTTP 500');
        return {toolRef: 'seal_candidate', status: 'SUCCEEDED', evidenceIds: [EVIDENCE]};
      }),
    });
    const result = await call(d);
    expect(result.submitted).toBe(true);
  });

  test('候选 id 只在第二个证据工件里：仍能反查到', async () => {
    const second = 'eeeeeeee-7777-4777-8777-eeeeeeeeeeee';
    const d = deps({
      readEffect: vi.fn(async () => ({
        toolRef: 'seal_candidate',
        status: 'SUCCEEDED',
        evidenceIds: [EVIDENCE, second],
      })),
      readArtifactContent: vi.fn(async (artifactId: string) =>
        artifactId === second
          ? JSON.stringify({candidate_manifest_id: CANDIDATE})
          : JSON.stringify({file_count: 1}),
      ),
    });
    const result = await call(d);
    expect(result.submitted).toBe(true);
  });

  test('恒不写 Goal/Task DONE（返回值里没有任何 done 语义）', async () => {
    const result = await call(deps());
    expect(JSON.stringify(result)).not.toMatch(/done|DONE/);
  });
});

describe('证据工件解析', () => {
  test('取出 candidate_manifest_id', () => {
    expect(
      candidateManifestIdFromEvidence(`{"candidate_manifest_id":"${CANDIDATE}"}`),
    ).toBe(CANDIDATE);
  });

  test('非 JSON（如 run_tests stdout）返回 null，不抛', () => {
    expect(candidateManifestIdFromEvidence('2 passed in 0.31s')).toBeNull();
  });

  test('JSON 但缺字段返回 null', () => {
    expect(candidateManifestIdFromEvidence('{"file_count":2}')).toBeNull();
  });

  test('字段为空串返回 null（不提交空 id）', () => {
    expect(candidateManifestIdFromEvidence('{"candidate_manifest_id":"  "}')).toBeNull();
  });
});

describe('控制面 HTTP 端口鉴权头', () => {
  test('裸 token 自动补 Bearer 前缀（漏补即 401，实测踩过）', async () => {
    const seen: Record<string, string> = {};
    const fakeFetch = (async (_url: string, init: {headers: Record<string, string>}) => {
      Object.assign(seen, init.headers);
      return {
        ok: true,
        status: 200,
        json: async () => ({data: {state_revision: 1, status: 'RUNNING'}}),
      };
    }) as unknown as typeof fetch;

    const deps = createHttpExecuteOutcomeDeps({
      baseUrl: 'http://127.0.0.1:58101',
      authorization: 'raw-token-abc',
      fetchImpl: fakeFetch,
    });
    await deps.readActivity(ACTIVITY);
    expect(seen.Authorization).toBe('Bearer raw-token-abc');
  });

  test('已带 Bearer 前缀时不重复叠加', async () => {
    const seen: Record<string, string> = {};
    const fakeFetch = (async (_url: string, init: {headers: Record<string, string>}) => {
      Object.assign(seen, init.headers);
      return {
        ok: true,
        status: 200,
        json: async () => ({data: {state_revision: 1, status: 'RUNNING'}}),
      };
    }) as unknown as typeof fetch;

    const deps = createHttpExecuteOutcomeDeps({
      baseUrl: 'http://127.0.0.1:58101',
      authorization: 'Bearer already-there',
      fetchImpl: fakeFetch,
    });
    await deps.readActivity(ACTIVITY);
    expect(seen.Authorization).toBe('Bearer already-there');
  });
});

