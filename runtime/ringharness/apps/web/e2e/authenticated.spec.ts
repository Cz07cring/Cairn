import {expect, test} from '@playwright/test';

const bearer = process.env.RING_WEB_E2E_BEARER;

test.beforeAll(() => {
  expect(bearer, '认证 E2E 必须由配置生成短期 Bearer').toBeTruthy();
});

test('真实 operator 可以创建项目，并在目标工作台读回', async ({page, request}) => {
  const projectName = `浏览器联调项目-${Date.now()}`;
  const response = await request.post('/api/v1/projects', {
    headers: {
      Authorization: bearer!,
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
    },
    data: {name: projectName, repository_ref: 'fixture'},
  });
  const body = await response.json();
  expect(response.status(), JSON.stringify(body)).toBe(201);
  expect(body.error).toBeNull();
  expect(body.data.name).toBe(projectName);

  await page.goto('/#goals');
  await expect(page.getByText(/开发调试身份已启用/)).toBeVisible();
  const projectOptions = page.locator('select').locator('option');
  await expect(projectOptions.filter({hasText: projectName}).first()).toBeAttached();
  await expect(page.getByRole('heading', {name: '新建一个目标'})).toBeVisible();
  await expect(page.getByRole('heading', {name: '登录后查看目标'})).toHaveCount(0);
});

test('真实项目列表进入执行记录页，不生成演示执行行', async ({page}) => {
  await page.goto('/#runs');
  await expect(page.getByRole('heading', {name: '登录后查看执行记录'})).toHaveCount(0);
  await expect(page.locator('.wb-record-row')).toHaveCount(0);
  await expect(page.getByText('还没有执行记录')).toBeVisible();
});
