import {expect, test} from '@playwright/test';

const pages = [
  ['今日总览', '#overview'], ['目标', '#goals'], ['需要我处理', '#attention'],
  ['执行记录', '#runs'], ['验收与交付', '#run-evidence'], ['系统健康', '#health'],
] as const;

test('同源代理连接真实 Control API，并正确限制健康结论', async ({page, request}) => {
  const response = await request.get('/health/live');
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toEqual({alive: true});
  await page.goto('/#health');
  await expect(page.getByRole('heading', {name: '系统健康'})).toBeVisible();
  await expect(page.getByText('当前只确认服务可以响应，还不代表所有功能都可用。')).toBeVisible();
  await expect(page.getByText('100 小时验收').first()).toBeVisible();
  await expect(page.getByText('尚未完成').first()).toBeVisible();
});

test('普通用户可以从侧栏进入全部一级功能板块', async ({page}) => {
  await page.goto('/#overview');
  for (const [label, hash] of pages) {
    await page.getByRole('link', {name: new RegExp(label)}).first().click();
    await expect(page).toHaveURL(new RegExp(`${hash.replace('#', '#')}$`));
    await expect(page.getByRole('heading', {name: label, exact: true}).first()).toBeVisible();
  }
});

test('快捷前往支持键盘搜索、选择和回车打开', async ({page}) => {
  await page.goto('/#overview');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+K' : 'Control+K');
  const input = page.getByRole('textbox', {name: '搜索页面或操作'});
  await expect(input).toBeFocused();
  await input.fill('日志');
  await expect(page.getByRole('button', {name: /打开日志/})).toBeVisible();
  await input.press('Enter');
  await expect(page).toHaveURL(/#run-logs$/);
  await expect(page.getByRole('link', {name: '日志', exact: true})).toHaveAttribute('aria-current', 'page');
});

test('主题和侧栏偏好在刷新后保持', async ({page}, testInfo) => {
  test.skip(testInfo.project.name === 'mobile', '移动端使用横向主导航，不显示桌面侧栏收起按钮');
  await page.goto('/#overview');
  await page.getByRole('button', {name: /切换浅色主题|切换深色主题/}).click();
  const theme = await page.locator('.wb-shell').getAttribute('data-theme');
  await page.getByRole('button', {name: '收起侧栏'}).click();
  await page.reload();
  await expect(page.locator('.wb-shell')).toHaveAttribute('data-theme', theme!);
  await expect(page.getByRole('button', {name: '展开侧栏'})).toBeVisible();
});

test('未登录的数据板块显示真实限制，不生成演示记录', async ({page}) => {
  await page.goto('/#runs');
  await expect(page.getByRole('heading', {name: '登录后查看执行记录'})).toBeVisible();
  await expect(page.locator('.wb-record-row')).toHaveCount(0);
  await page.goto('/#run-overview');
  await expect(page.getByRole('heading', {name: '登录后查看任务流程'})).toBeVisible();
});

test('移动端保留主要入口和交付语义', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile');
  await page.goto('/#overview');
  await expect(page.getByRole('link', {name: /今日总览/})).toBeVisible();
  await expect(page.getByText(/都不代表目标通过最终验收/)).toBeVisible();
  await expect(page.locator('.wb-shell')).toHaveCSS('display', 'block');
});
