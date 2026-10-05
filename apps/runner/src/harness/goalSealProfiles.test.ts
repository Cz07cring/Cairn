/**
 * goalSealProfiles：Worker GET Goal+Task → Task acceptance seal / 验收要点文案。
 */
import {describe, expect, test, vi} from 'vitest';
import {fetchGoalSealActivation} from './goalSealProfiles.js';

const mechanical = '11111111-1111-1111-1111-111111111111';
const globalProfile = '22222222-2222-2222-2222-222222222222';

describe('fetchGoalSealActivation', () => {
  test('优先 Task acceptance，并把 description 写入提示（不含 UUID）', async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      if (String(url).includes('/api/v1/goals/')) {
        return Response.json({
          data: {
            id: 'goal-1',
            objective: '修复订单幂等',
            contract: {
              objective: '修复订单幂等',
              success_criteria: [
                {id: 'C1', verification_profile_id: globalProfile},
              ],
            },
          },
        });
      }
      if (String(url).includes('/api/v1/tasks/')) {
        return Response.json({
          data: {
            id: 'task-1',
            contract: {
              acceptance: [
                {
                  id: 'A1',
                  description: '同 key 顺序请求须同一 order_id 且库存只扣一次',
                  verification_profile_id: mechanical,
                },
                {
                  id: 'A1b',
                  description: '同 key 顺序请求须同一 order_id 且库存只扣一次',
                  verification_profile_id: mechanical,
                },
              ],
            },
          },
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    const got = await fetchGoalSealActivation({
      baseUrl: 'http://control.test',
      authorization: 'Bearer worker',
      goalId: 'goal-1',
      taskId: 'task-1',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(got.sealVerificationProfileIds).toEqual([mechanical]);
    expect(got.acceptanceDescriptions).toEqual([
      '同 key 顺序请求须同一 order_id 且库存只扣一次',
    ]);
    expect(got.userPromptFromGoal).toContain('修复订单幂等');
    expect(got.userPromptFromGoal).toContain('验收要点：');
    expect(got.userPromptFromGoal).toContain('同 key 顺序请求须同一 order_id');
    expect(got.userPromptFromGoal).toContain('同一文件只读一次');
    expect(got.userPromptFromGoal).not.toContain(mechanical);
    expect(got.userPromptFromGoal).not.toContain(globalProfile);
    expect(got.userPromptFromGoal).not.toMatch(/read_file|write_file|seal_candidate/);
  });

  test('Task 缺 acceptance profile 失败关闭（不回退 Goal GLOBAL）', async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      if (String(url).includes('/api/v1/goals/')) {
        return Response.json({
          data: {
            id: 'goal-1',
            objective: 'x',
            contract: {
              success_criteria: [{verification_profile_id: globalProfile}],
            },
          },
        });
      }
      return Response.json({
        data: {id: 'task-1', contract: {acceptance: []}},
      });
    });
    await expect(
      fetchGoalSealActivation({
        baseUrl: 'http://control.test',
        authorization: 'token',
        goalId: 'goal-1',
        taskId: 'task-1',
        fetchImpl: fetchImpl as unknown as typeof fetch,
      }),
    ).rejects.toThrow(/TASK_SEAL_PROFILES_EMPTY/);
  });

  test('缺 taskId 失败关闭', async () => {
    await expect(
      fetchGoalSealActivation({
        baseUrl: 'http://control.test',
        authorization: 'token',
        goalId: 'goal-1',
        taskId: '  ',
      }),
    ).rejects.toThrow(/TASK_ID_REQUIRED_FOR_SEAL/);
  });
});
