import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdirSync } from 'node:fs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '../../..');
const SHOTS = join(ROOT, 'docs/evidence/shots');
mkdirSync(SHOTS, { recursive: true });
const BASE = process.env.BASE ?? 'http://127.0.0.1:5199';
const FIX = join(ROOT, 'tests/fixtures/astrbot-memory');
const USER = process.env.LING_USER ?? 'ling';
const PASS = process.env.LING_PASS ?? 'Accept-Only-2026';

const browser = await chromium.launch();
const log = (...a) => console.log(...a);

async function session(viewport, mobile) {
  const ctx = await browser.newContext({ viewport, hasTouch: mobile, isMobile: mobile, reducedMotion: 'reduce' });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  const calls = [];
  page.on('request', (r) => { if (r.url().includes('/v1/memory')) calls.push(`${r.method()} ${r.url().replace(BASE, '')}`); });
  await page.goto(`${BASE}/memory`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  // 登录（已经登录时没有该输入框，直接跳过）
  const user = page.locator('input[name="username"]');
  if (await user.count()) {
    await user.fill(USER);
    await page.locator('input[name="password"]').fill(PASS);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.waitForTimeout(2500);
  }
  // 登录后前端回到默认页；统一再进一次记忆库。
  await page.goto(`${BASE}/memory`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('mem-import-entry').waitFor({ state: 'visible' });
  return { ctx, page, calls };
}

const result = {};
{
  const { ctx, page, calls } = await session({ width: 1366, height: 900 }, false);
  await page.getByTestId('mem-import-entry').click();
  await page.getByTestId('import-panel').waitFor();
  await page.getByTestId('import-file-input').setInputFiles(join(FIX, 'old-memories.jsonl'));
  await page.getByTestId('import-accepted-value').waitFor();
  result.accepted = (await page.getByTestId('import-accepted-value').innerText()).trim();
  result.destination = (await page.getByTestId('import-destination-name').innerText()).trim();
  result.pair = (await page.getByTestId('import-source-pair').innerText()).replace(/\s+/g, ' ').trim();
  result.samples = await page.getByTestId('import-sample').count();
  await page.waitForTimeout(300);
  await page.screenshot({ path: join(SHOTS, 'astrbot-import-preview-desktop-1366x900.png'), fullPage: false });

  const confirm = page.getByTestId('import-confirm');
  result.confirmLabel = (await confirm.innerText()).trim();
  await confirm.click();
  await page.getByTestId('import-result-imported').waitFor();
  result.imported = (await page.getByTestId('import-result-imported').innerText()).replace(/\s+/g, ' ').trim();
  result.duplicates = (await page.getByTestId('import-result-duplicates').innerText()).trim();
  result.skipped = (await page.getByTestId('import-result-skipped').innerText()).trim();
  await page.waitForTimeout(800);
  result.listCountAfterCommit = (await page.getByTestId('mem-list-count').innerText()).replace(/\s+/g, ' ').trim();
  await page.screenshot({ path: join(SHOTS, 'astrbot-import-result-desktop-1366x900.png'), fullPage: false });

  await page.getByTestId('import-view-imported').click();
  await page.waitForTimeout(1500);
  result.sourceSelect = await page.getByTestId('mem-source').inputValue();
  result.rows = await page.getByTestId('mem-results').locator('li').count();
  result.listCalls = calls.filter((c) => c.includes('view=library')).slice(-3);
  const first = page.getByTestId('mem-results').locator('button.mem-record').first();
  await first.click();
  await page.waitForTimeout(600);
  result.detailSource = (await page.getByTestId('mem-import-source').innerText()).replace(/\s+/g, ' ').trim();
  result.detailTimes = (await page.getByTestId('mem-import-times').innerText()).replace(/\s+/g, ' ').trim().slice(0, 160);
  await page.screenshot({ path: join(SHOTS, 'astrbot-import-detail-desktop-1366x900.png'), fullPage: false });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  result.desktopOverflow = overflow;

  // 预览失效路径（真实 409）
  await page.getByTestId('mem-import-entry').click();
  await page.getByTestId('import-file-input').setInputFiles(join(FIX, 'mixed.jsonl'));
  await page.getByTestId('import-accepted-value').waitFor();
  result.mixedAccepted = (await page.getByTestId('import-accepted-value').innerText()).trim();
  await ctx.close();
}
// 手机环节要拍"成功路径"，先把桌面那轮导入的记录清掉（同一份文件否则全是重复）。
if (process.env.RESET_CMD) (await import('node:child_process')).execSync(process.env.RESET_CMD, { stdio: 'ignore' });
{
  const { ctx, page } = await session({ width: 375, height: 760 }, true);
  result.mobileEntryVisible = await page.getByTestId('mem-import-entry').isVisible();
  await page.getByTestId('mem-import-entry').click();
  await page.getByTestId('import-panel').waitFor({ state: 'visible' });
  await page.getByTestId('import-file-input').setInputFiles(join(FIX, 'old-memories.jsonl'));
  await page.getByTestId('import-accepted-value').waitFor();
  result.mobileAccepted = (await page.getByTestId('import-accepted-value').innerText()).trim();
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(SHOTS, 'astrbot-import-preview-mobile-375x760.png'), fullPage: false });
  await page.getByTestId('import-confirm').click();
  await page.getByTestId('import-result-imported').waitFor();
  result.mobileImported = (await page.getByTestId('import-result-imported').innerText()).replace(/\s+/g, ' ').trim();
  await page.waitForTimeout(300);
  await page.screenshot({ path: join(SHOTS, 'astrbot-import-result-mobile-375x760.png'), fullPage: false });
  result.mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

  // 零条：换文件后看原因与下一步，并确认没有确认按钮
  await page.getByRole('button', { name: '关闭' }).first().click();
  await page.waitForTimeout(500);
  await page.getByTestId('mem-import-entry').click();
  await page.getByTestId('import-file-input').setInputFiles(join(FIX, 'zero-accepted.jsonl'));
  await page.getByTestId('import-zero').waitFor();
  result.mobileZero = (await page.getByTestId('import-zero').innerText()).replace(/\s+/g, ' ').trim();
  result.mobileConfirmCount = await page.getByTestId('import-confirm').count();
  await page.waitForTimeout(300);
  await page.screenshot({ path: join(SHOTS, 'astrbot-import-zero-mobile-375x760.png'), fullPage: false });
  await ctx.close();
}
await browser.close();
log(JSON.stringify(result, null, 2));
