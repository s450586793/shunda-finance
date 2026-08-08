import fs from 'node:fs';
import path from 'node:path';
import { expect, Page, test } from '@playwright/test';


const fixtures = path.resolve('tests/fixtures/synthetic_railway');
const screenshotRoot = path.resolve('test-results/screenshots');
const viewports = [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 390, height: 844 },
  { width: 360, height: 800 },
];


function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}


function monitorPage(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  return errors;
}


async function login(page: Page, username: string, password: string) {
  await page.goto('/accounts/login/');
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page).toHaveURL(/\/ledger\/invoices\/$/);
}


async function importFixture(page: Page, filename: string, expectedSourceKind: string) {
  await page.goto('/imports/');
  await page.getByLabel('原始文件').setInputFiles(path.join(fixtures, filename));
  await page.getByRole('button', { name: '预检文件' }).click();
  const previewHeader = page.locator('header').filter({
    has: page.getByRole('heading', { name: '导入预检' }),
  });
  const sourceMetaPattern = new RegExp(
    `^${escapeRegExp(expectedSourceKind)}\\s+·\\s+\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}$`,
  );
  await expect(previewHeader.getByText(sourceMetaPattern)).toBeVisible();
  await expect(page.getByText('有效数据').locator('..').getByRole('strong')).not.toHaveText('0');
  await page.getByRole('button', { name: '确认导入' }).click();
  await expect(page.getByText('导入已确认。')).toBeVisible();
}


async function filterWorkbenchForJune(page: Page) {
  await page.getByLabel('开始日期').fill('2026-06-01');
  await page.getByLabel('结束日期').fill('2026-06-30');
  await page.getByRole('button', { name: '查询' }).click();
}


async function assertLayout(page: Page) {
  const rootLayout = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    offenders: [...document.querySelectorAll('body *')].filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.right > document.documentElement.clientWidth + 1;
    }).slice(0, 12).map((element) => ({
      element: element.tagName.toLowerCase(),
      classes: element.className,
      right: Math.round(element.getBoundingClientRect().right),
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      overflowX: getComputedStyle(element).overflowX,
    })),
  }));
  expect(rootLayout, JSON.stringify(rootLayout)).toMatchObject({ overflow: 0 });
  const clippedButtons = await page.locator('button:visible, a.button:visible').evaluateAll(
    (elements) => elements.filter((element) => (
      element.scrollWidth > element.clientWidth + 1
      || element.scrollHeight > element.clientHeight + 1
    )).map((element) => element.textContent?.trim() || element.getAttribute('aria-label')),
  );
  expect(clippedButtons).toEqual([]);
  const overlaps = await page.locator('.toolbar').evaluateAll((toolbars) => {
    const collisions: string[] = [];
    for (const toolbar of toolbars) {
      const children = [...toolbar.children].filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      });
      for (let left = 0; left < children.length; left += 1) {
        for (let right = left + 1; right < children.length; right += 1) {
          const a = children[left].getBoundingClientRect();
          const b = children[right].getBoundingClientRect();
          if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
            && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
            collisions.push(`${left}:${right}`);
          }
        }
      }
    }
    return collisions;
  });
  expect(overlaps).toEqual([]);
}


async function assertCharts(page: Page) {
  const canvases = page.locator('.chart canvas');
  await expect(canvases).toHaveCount(3);
  const changedPixels = await canvases.evaluateAll((elements) => elements.map((element) => {
    const canvas = element as HTMLCanvasElement;
    const context = canvas.getContext('2d');
    if (!context) return 0;
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let changed = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      const nearWhite = pixels[index] > 248 && pixels[index + 1] > 248 && pixels[index + 2] > 248;
      if (pixels[index + 3] > 0 && !nearWhite) changed += 1;
    }
    return changed;
  }));
  for (const count of changedPixels) expect(count).toBeGreaterThan(100);
  const chartImage = await page.locator('#cashflow-chart').screenshot();
  expect(chartImage.byteLength).toBeGreaterThan(1000);
}


test.describe.configure({ mode: 'serial' });


test('finance imports and partially reconciles while owner stays read-only', async ({ page }) => {
  const errors = monitorPage(page);
  await login(page, 'finance-e2e', 'finance-e2e');
  await importFixture(page, 'input_invoices.xlsx', '进项发票');
  await importFixture(page, 'bank_june.xls', '银行');

  await page.goto('/reconciliation/workbench/');
  await filterWorkbenchForJune(page);
  await page.locator('tr').filter({ hasText: '2,000.00' }).getByRole('link').click();
  await page.getByLabel('选择 2026-06-16 资金').check();
  await expect(page.getByRole('button', { name: '确认核销' })).toBeEnabled();
  await page.getByRole('button', { name: '确认核销' }).click();

  await page.goto('/reconciliation/workbench/');
  await filterWorkbenchForJune(page);
  await page.locator('tr').filter({ hasText: '46,050.00' }).getByRole('link').click();
  const payments = page.locator('[data-transaction-select]');
  await expect(payments).toHaveCount(12);
  for (let index = 0; index < 12; index += 1) await payments.nth(index).check();
  await expect(page.locator('[data-difference-total]').first()).toHaveText('1,000.00');
  await page.getByRole('checkbox', { name: /本次为部分核销/ }).check();
  await page.getByLabel('备注').fill('测试铁路物流 2026 年 6 月结算，尚差 1,000.00 元');
  await page.getByRole('button', { name: '确认核销' }).click();

  await page.goto('/reconciliation/workbench/');
  await expect(page.locator('tr').filter({ hasText: '1,000.00' })).toHaveCount(1);
  await page.getByRole('button', { name: '退出登录' }).click();
  await login(page, 'owner-e2e', 'owner-e2e');
  await page.goto('/reporting/?month=2026-06');
  await expect(page.getByRole('heading', { name: '经营总览' })).toBeVisible();
  await expect(page.getByRole('link', { name: '导入中心' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: '人工核销' })).toHaveCount(0);
  expect(errors).toEqual([]);
});


test('dashboard import center and workbench fit all required viewports', async ({ page }) => {
  const errors = monitorPage(page);
  fs.mkdirSync(screenshotRoot, { recursive: true });
  await login(page, 'finance-e2e', 'finance-e2e');
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    const viewportName = `${viewport.width}x${viewport.height}`;
    const viewportRoot = path.join(screenshotRoot, viewportName);
    fs.mkdirSync(viewportRoot, { recursive: true });
    for (const pageDefinition of [
      { name: 'dashboard', url: '/reporting/?month=2026-06', charts: true },
      { name: 'imports', url: '/imports/', charts: false },
      { name: 'workbench', url: '/reconciliation/workbench/', charts: false },
    ]) {
      await page.goto(pageDefinition.url);
      await assertLayout(page);
      if (pageDefinition.charts) await assertCharts(page);
      await page.screenshot({
        path: path.join(viewportRoot, `${pageDefinition.name}.png`),
        fullPage: true,
      });
    }
  }
  expect(errors).toEqual([]);
});
