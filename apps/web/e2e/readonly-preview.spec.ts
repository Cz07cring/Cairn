import {expect, test} from '@playwright/test';

test('只读驾驶舱自动打开真实活动 Goal 和 Task', async ({page}) => {
  const expectedGoal = process.env.RING_WEB_PREVIEW_GOAL_ID;
  const apiErrors: string[] = [];
  page.on('response', (response) => {
    if (
      response.url().includes('/api/') &&
      !response.url().endsWith('/api/v1/auth/session') &&
      !response.ok()
    ) {
      apiErrors.push(`${response.status()} ${response.url()}`);
    }
  });
  expect(expectedGoal, '须提供预览脚本选中的 Goal id').toBeTruthy();
  await page.goto('/#overview');
  await expect(page.getByText(/开发调试身份已启用/)).toBeVisible();
  await page.waitForTimeout(1_000);
  expect(apiErrors).toEqual([]);
  await expect(page.getByText(/当前工作 ·/)).toContainText(expectedGoal!.slice(0, 8));
  await expect(page.getByText(/真实任务：共/)).toBeVisible();
  await page.getByRole('link', {name: '查看任务流程与实时进度'}).click();
  await expect(page.locator('#observe-goal-picker')).toContainText(expectedGoal!);
  await expect(page.locator('.wb-task-node').first()).toBeVisible();
  await expect(page.locator('.wb-task-node')).not.toHaveCount(0);
  await page.getByRole('link', {name: '执行交流', exact: true}).click();
  await expect(page).toHaveURL(/#run-live$/);
  await expect(page.locator('.wb-agent-chat')).toContainText('与执行腿跟流对话');
  await expect(page.locator('.wb-agent-chat')).toContainText('≠ Goal DONE');
  await expect(page.locator('#run-live')).toContainText('数据连接正常');
  await expect(page.locator('#run-live .wb-run-progress')).toContainText('任务没有继续推进');
  await expect(page.locator('#run-live .wb-run-progress')).toContainText(
    '目标仍显示运行中，但现在没有执行中的步骤；最近一步已经停止。',
  );
  await expect(page.locator('#run-live .wb-live-card').first()).toBeVisible();
  await expect(page.locator('#run-live')).toContainText(/工作步骤|模型工作|工具与命令|运行控制/);
  await expect(page.locator('#run-live')).toContainText('不展示或编造私有推理');
  expect(apiErrors).toEqual([]);
});
