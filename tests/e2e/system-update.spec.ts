import fs from 'node:fs';
import path from 'node:path';
import { expect, Page, test } from '@playwright/test';


type Stage = 'checking' | 'pulling' | 'succeeded' | 'failed' | 'manual_intervention';
type StatusResult = 'network' | 'unavailable' | Stage | 'failed_pending' | 'failed_ready' | 'succeeded_pending' | null;
const DEFAULT_TASK_ID = '00000000-0000-0000-0000-000000000001';
const PENDING_START_KEY = 'shunda.system-update.pending-start';
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;


function status(result: Exclude<StatusResult, 'network' | 'unavailable'> = null, taskId = DEFAULT_TASK_ID) {
  const stage = result === 'succeeded_pending'
    ? 'succeeded'
    : result === 'failed_pending' || result === 'failed_ready'
      ? 'failed'
      : result;
  return {
    current_version: stage === 'succeeded' ? 'v0.2.1' : 'v0.2.0',
    current_published_at: '2026-08-06T23:30:00+00:00',
    latest_version: 'v0.2.1',
    latest_published_at: '2026-08-07T12:00:00+00:00',
    update_available: stage !== 'succeeded',
    checked_at: '2026-08-07T12:00:00+00:00',
    task: stage ? {
      id: taskId,
      from_version: 'v0.2.0',
      to_version: 'v0.2.1',
      stage,
      created_at: '2026-08-07T12:00:00+00:00',
      started_at: '2026-08-07T12:00:00+00:00',
      finished_at: ['succeeded', 'failed', 'manual_intervention'].includes(stage)
        ? '2026-08-07T12:01:00+00:00' : null,
      backup_complete: true,
      rolled_back: false,
      cleanup: result === 'succeeded_pending' || result === 'failed_pending'
        ? 'pending'
        : stage === 'succeeded'
          ? 'complete'
          : 'not_run',
      error_code: stage === 'failed' ? 'pull_failed' : stage === 'manual_intervention' ? 'rollback_failed' : '',
      error_message: stage === 'failed' ? '下载升级版本失败，请联系管理员。' : stage === 'manual_intervention' ? '升级失败，需要人工处理。' : '',
    } : null,
  };
}


function monitorPage(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !/(503|net::ERR_FAILED)/.test(message.text())) {
      errors.push(`console: ${message.text()}`);
    }
  });
  return errors;
}


async function loginOwner(page: Page) {
  await page.goto('/accounts/login/');
  await page.getByLabel('用户名').fill('owner-e2e');
  await page.getByLabel('密码').fill('owner-e2e');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page).toHaveURL(/\/ledger\/invoices\/$/);
}


async function installSystemUpdateRoutes(
  page: Page,
  sequence: StatusResult[],
  { failFirstStart = false } = {},
) {
  let statusRequests = 0;
  let startRequests = 0;
  let checked = false;
  let acceptedTaskId: string | null = null;
  const submittedTaskIds: string[] = [];
  await page.route('**/system/update/status/', async (route) => {
    const result = sequence[Math.min(statusRequests, sequence.length - 1)];
    statusRequests += 1;
    if (result === 'network') return route.abort('failed');
    if (result === 'unavailable') {
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"updater_unavailable"}' });
    }
    return route.fulfill({
      contentType: 'application/json',
      json: status(result, acceptedTaskId || DEFAULT_TASK_ID),
    });
  });
  await page.route('**/system/update/check/', async (route) => {
    checked = true;
    expect(route.request().postDataJSON()).toEqual({});
    return route.fulfill({ contentType: 'application/json', json: status(null) });
  });
  await page.route('**/system/update/start/', async (route) => {
    startRequests += 1;
    expect(checked).toBe(true);
    const payload = route.request().postDataJSON();
    expect(Object.keys(payload).sort()).toEqual(['target_version', 'task_id']);
    expect(payload.target_version).toBe('v0.2.1');
    expect(payload.task_id).toMatch(UUID_V4_PATTERN);
    submittedTaskIds.push(payload.task_id);
    if (acceptedTaskId === null) acceptedTaskId = payload.task_id;
    expect(payload.task_id).toBe(acceptedTaskId);
    if (failFirstStart && startRequests === 1) return route.abort('failed');
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      json: status('pulling', acceptedTaskId).task,
    });
  });
  return {
    get startRequests() { return startRequests; },
    get statusRequests() { return statusRequests; },
    get submittedTaskIds() { return [...submittedTaskIds]; },
  };
}


test('Owner confirms one exact version and observes completion', async ({ page }) => {
  const errors = monitorPage(page);
  const routes = await installSystemUpdateRoutes(page, [null, 'succeeded']);
  await loginOwner(page);
  await page.goto('/system/update/');
  await expect(page.getByRole('link', { name: '系统设置' })).toBeVisible();
  await expect.poll(() => routes.statusRequests).toBeGreaterThan(0);
  await expect(page.locator('[data-current-version]')).toHaveText('v0.2.0');
  expect(errors).toEqual([]);
  await page.getByRole('button', { name: '检查更新' }).click();
  await page.getByRole('button', { name: '升级到 v0.2.1' }).click();
  const dialog = page.locator('[data-confirm-dialog]');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认升级到 v0.2.1' })).toBeVisible();
  await dialog.getByRole('button', { name: '确认升级到 v0.2.1' }).click();
  await expect(page.getByText('升级成功')).toBeVisible({ timeout: 5_000 });
  expect(routes.startRequests).toBe(1);
  expect(errors).toEqual([]);
});


test('status recovery retries after 503 and network failure', async ({ page }) => {
  const errors = monitorPage(page);
  await installSystemUpdateRoutes(page, ['unavailable', 'network', 'pulling']);
  await loginOwner(page);
  await page.goto('/system/update/');
  await expect(page.getByText('无法连接升级服务，将自动重试。')).toBeVisible();
  await expect(page.getByText('正在下载升级版本')).toBeVisible({ timeout: 8_000 });
  expect(errors).toEqual([]);
});


test('ambiguous start survives reload, retries the same task id, and clears it at terminal status', async ({ page }) => {
  const errors = monitorPage(page);
  const routes = await installSystemUpdateRoutes(page, [null, null, 'succeeded'], {
    failFirstStart: true,
  });
  await loginOwner(page);
  await page.goto('/system/update/');
  await page.getByRole('button', { name: '检查更新' }).click();
  await page.getByRole('button', { name: '升级到 v0.2.1' }).click();
  await page.locator('[data-confirm-dialog]').getByRole('button', { name: '确认升级到 v0.2.1' }).click();
  await expect(page.getByText('无法提交升级任务，请稍后重试。')).toBeVisible();

  const firstPending = await page.evaluate((key) => sessionStorage.getItem(key), PENDING_START_KEY);
  expect(firstPending).not.toBeNull();
  expect(JSON.parse(firstPending || '{}')).toEqual({
    target_version: 'v0.2.1',
    task_id: routes.submittedTaskIds[0],
  });

  await page.reload();
  await expect(page.getByRole('button', { name: '升级到 v0.2.1' })).toBeVisible();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), PENDING_START_KEY)).toBe(firstPending);
  await page.getByRole('button', { name: '升级到 v0.2.1' }).click();
  await page.locator('[data-confirm-dialog]').getByRole('button', { name: '确认升级到 v0.2.1' }).click();

  await expect(page.getByText('升级成功')).toBeVisible({ timeout: 5_000 });
  expect(routes.startRequests).toBe(2);
  expect(routes.submittedTaskIds).toEqual([
    routes.submittedTaskIds[0],
    routes.submittedTaskIds[0],
  ]);
  await expect.poll(
    () => page.evaluate((key) => sessionStorage.getItem(key), PENDING_START_KEY),
  ).toBeNull();
  expect(errors).toEqual([]);
});


test('reload restores an active task, terminal state stops polling, and the mobile layout fits', async ({ page }) => {
  const errors = monitorPage(page);
  const routes = await installSystemUpdateRoutes(page, ['pulling', 'pulling', 'succeeded']);
  await loginOwner(page);
  await page.goto('/system/update/');
  await expect(page.getByText('正在下载升级版本')).toBeVisible();
  await page.reload();
  await expect(page.getByText('正在下载升级版本')).toBeVisible();
  await expect(page.getByText('升级成功')).toBeVisible({ timeout: 5_000 });
  await expect(page.getByRole('button', { name: '升级到 v0.2.1' })).toBeHidden();
  const terminalRequests = routes.statusRequests;
  await page.waitForTimeout(2_500);
  expect(routes.statusRequests).toBe(terminalRequests);

  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/system/update/');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBe(0);
  fs.mkdirSync(path.resolve('test-results/screenshots'), { recursive: true });
  await page.screenshot({ path: path.resolve('test-results/screenshots/system-update-375.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({ path: path.resolve('test-results/screenshots/system-update-1440.png'), fullPage: true });
  expect(errors).toEqual([]);
});


test('pending cleanup metadata and guidance fit the desktop workbench', async ({ page }) => {
  const errors = monitorPage(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await installSystemUpdateRoutes(page, ['succeeded_pending']);
  await loginOwner(page);
  await page.goto('/system/update/');

  await expect(page.locator('[data-current-published-at]')).toHaveText('2026-08-07 07:30:00');
  await expect(page.locator('[data-latest-published-at]')).toHaveText('2026-08-07 20:00:00');
  await expect(page.locator('[data-task-id]')).toHaveText(DEFAULT_TASK_ID);
  await expect(page.locator('[data-task-created-at]')).toHaveText('2026-08-07 20:00:00');
  await expect(page.locator('[data-task-finished-at]')).toHaveText('2026-08-07 20:01:00');
  await expect(page.locator('[data-operation-guidance]')).toHaveText(
    '升级已完成，但旧版本清理尚未完成。请联系系统管理员按运维流程处理，完成前请勿再次升级。',
  );
  await expect(page.locator('[data-update-availability]')).toHaveText('旧版本清理待完成，暂不可升级');
  await expect(page.getByRole('button', { name: '升级到 v0.2.1' })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
  await page.screenshot({ path: path.resolve('test-results/screenshots/system-update-pending-1440.png'), fullPage: true });
  expect(errors).toEqual([]);
});


test('manual intervention metadata and guidance fit the mobile workbench', async ({ page }) => {
  const errors = monitorPage(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await installSystemUpdateRoutes(page, ['manual_intervention']);
  await loginOwner(page);
  await page.goto('/system/update/');

  await expect(page.locator('[data-task-started-at]')).toHaveText('2026-08-07 20:00:00');
  await expect(page.locator('[data-operation-guidance]')).toHaveText(
    '自动恢复未完成。请停止继续升级并联系系统管理员处理；备份和相关版本已保留。',
  );
  await expect(page.locator('[data-update-availability]')).toHaveText('需要管理员处理，暂不可升级');
  await expect(page.getByRole('button', { name: '升级到 v0.2.1' })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
  await page.screenshot({ path: path.resolve('test-results/screenshots/system-update-manual-375.png'), fullPage: true });
  expect(errors).toEqual([]);
});


test('failed pending cleanup blocks the real start control and shows one decision', async ({ page }) => {
  const errors = monitorPage(page);
  await installSystemUpdateRoutes(page, ['failed_pending']);
  await loginOwner(page);
  await page.goto('/system/update/');

  await expect(page.locator('[data-operation-guidance]')).toHaveText(
    '升级已完成，但旧版本清理尚未完成。请联系系统管理员按运维流程处理，完成前请勿再次升级。',
  );
  await expect(page.locator('[data-update-availability]')).toHaveText('旧版本清理待完成，暂不可升级');
  await expect(page.getByRole('button', { name: '升级到 v0.2.1' })).toBeHidden();
  expect(errors).toEqual([]);
});


test('failed nonpending task retains the existing retry control', async ({ page }) => {
  const errors = monitorPage(page);
  await installSystemUpdateRoutes(page, ['failed_ready']);
  await loginOwner(page);
  await page.goto('/system/update/');

  await expect(page.locator('[data-operation-guidance]')).toBeHidden();
  await expect(page.locator('[data-update-availability]')).toHaveText('可升级');
  await expect(page.getByRole('button', { name: '升级到 v0.2.1' })).toBeVisible();
  expect(errors).toEqual([]);
});
