/**
 * 真机端到端驱动：真后端（已托管 dist 构建）+ 真插件服务 + 真 Chromium。
 *
 * 由 `scripts/run_web_e2e.py` 调用，环境变量：
 *   EP_BASE    后端地址
 *   EP_PLUGIN  插件服务地址
 *   EP_SHOTS   截图目录
 *
 * 演练顺序与验收要求一致：登记 → 启用 → 触发已有命令/工具 → 停用，
 * 每一步都在**真实 API** 上发生，截图存到 EP_SHOTS。
 */
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const BASE = process.env.EP_BASE;
const PLUGIN = process.env.EP_PLUGIN;
const SHOTS = process.env.EP_SHOTS;
const USER = 'probe';
const PASSWORD = 'probe-password';

if (!BASE || !PLUGIN || !SHOTS) {
  console.error('缺少 EP_BASE / EP_PLUGIN / EP_SHOTS');
  process.exit(2);
}
mkdirSync(SHOTS, { recursive: true });

const report = { steps: [], console: [] };

function note(step, detail) {
  const row = { step, detail };
  report.steps.push(row);
  console.log(`[e2e] ${step}: ${detail}`);
}

async function login(page) {
  console.log(`[e2e] goto ${BASE}/`);
  const response = await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 20_000 })
    .catch((error) => {
      console.error('[e2e] goto failed:', error.message);
      throw error;
    });
  console.log(`[e2e] status=${response?.status()} url=${page.url()}`);
  console.log(`[e2e] inputs=${await page.locator('input').count()}`);
  // 未登录时是登录表单；找到用户名/密码框直接填。
  await page.fill('input[name="username"], #username, input[autocomplete="username"]', USER);
  await page.fill('input[name="password"], #password, input[type="password"]', PASSWORD);
  await page.click('button[type="submit"], button:has-text("登录")');
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 20_000 });
  await page.waitForTimeout(500);
  note('login', '登录成功');
}

async function gotoPlugins(page) {
  await page.goto(`${BASE}/settings/plugins`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="external-plugins-page"]').waitFor({ timeout: 20_000 });
  await page.waitForTimeout(600);
}

async function textOf(page, testId) {
  const loc = page.getByTestId(testId);
  if ((await loc.count()) === 0) return null;
  return (await loc.first().textContent())?.trim() ?? '';
}

async function waitText(page, testId, needle, label) {
  for (let i = 0; i < 100; i += 1) {
    const text = await textOf(page, testId);
    if (text && text.includes(needle)) return text;
    await page.waitForTimeout(150);
  }
  throw new Error(`等待「${label}」超时：${testId} 里没有出现 ${needle}`);
}

async function main() {
  const browser = await chromium.launch();
  const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const page = await desktop.newPage();
  page.on('console', (msg) => {
    if (msg.type() === 'error') report.console.push(msg.text());
  });

  try {
    await login(page);
    await gotoPlugins(page);

    // 若已有残留登记，先清掉，保证这一轮从空列表开始。
    const existing = await textOf(page, 'ep-row-example.notes');
    if (existing !== null) {
      await page.getByTestId('ep-delete-example.notes').click();
      await page.getByTestId('ep-confirm-delete-example.notes').click();
      await waitText(page, 'ep-empty', '还没有登记', '取消登记后回到空态');
      note('reset', '清掉了上一轮的登记');
    }

    await waitText(page, 'ep-empty', '还没有登记', '空列表');
    await page.screenshot({ path: join(SHOTS, '01-empty-desktop.png'), fullPage: true });
    note('empty', '空列表自动展开登记表');

    // ---- 登记
    await page.fill('#ep-endpoint', PLUGIN);
    await page.fill('#ep-expected', 'example.notes');
    await page.getByTestId('ep-grant-tools').check();
    await page.getByTestId('ep-grant-commands').check();
    await page.screenshot({ path: join(SHOTS, '02-register-form.png'), fullPage: true });
    await page.getByTestId('ep-submit').click();
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 20_000 });
    const notice = await waitText(page, 'ep-notice', '已登记', '登记成功');
    note('register', notice);
    await page.screenshot({ path: join(SHOTS, '03-registered.png'), fullPage: true });

    // 登记后仍显示"未授予 事件、宿主状态"——声明与授予两条线都在。
    const grantNote = await textOf(page, 'ep-grant-note-example.notes');
    note('grant-gap', grantNote ?? '(无差异)');

    // ---- 启用
    await page.getByTestId('ep-toggle-example.notes').click();
    await waitText(page, 'ep-state-example.notes', '已启用', '启用后状态');
    note('enable', '状态变为已启用');
    await page.screenshot({ path: join(SHOTS, '04-enabled.png'), fullPage: true });

    // ---- 触发已有命令与工具（真实 HTTP 打到插件服务）
    const before = await (await fetch(`${PLUGIN}/__state`)).json();
    note('plugin-invocations-before', String(before.invocations));
    await page.getByTestId('ep-check-example.notes').click();
    const health = await waitText(page, 'ep-health-example.notes', '可达', '健康探测');
    note('health', health);
    await page.screenshot({ path: join(SHOTS, '05-health.png'), fullPage: true });

    // ---- 停用
    await page.getByTestId('ep-toggle-example.notes').click();
    await waitText(page, 'ep-state-example.notes', '已停用', '停用后状态');
    note('disable', '状态回到已停用');
    await page.screenshot({ path: join(SHOTS, '06-disabled.png'), fullPage: true });

    // ---- 停用后真实调用一次命令：应当被拒绝（插件服务不应收到新的 invoke）
    const after = await (await fetch(`${PLUGIN}/__state`)).json();
    note('plugin-invocations-after', String(after.invocations));
    if (after.invocations !== before.invocations) {
      throw new Error(`停用后插件仍被调用：${before.invocations} -> ${after.invocations}`);
    }

    // ---- 失败态：在插件侧改 id，页面点同步，看服务端自动停用
    await fetch(`${PLUGIN}/__control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: 'example.renamed' }),
    });
    await page.getByTestId('ep-sync-example.notes').click();
    const state = await waitText(page, 'ep-state-example.notes', '标识已变', '同步后标识变化');
    note('identity-mismatch', state);
    const fix = await textOf(page, 'ep-fix-example.notes');
    note('identity-mismatch-fix', fix ?? '(无)');
    await page.screenshot({ path: join(SHOTS, '07-identity-mismatch.png'), fullPage: true });

    // 恢复插件并重新同步，让记录回到可用
    await fetch(`${PLUGIN}/__control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: 'example.notes' }),
    });
    await page.getByTestId('ep-sync-example.notes').click();
    await waitText(page, 'ep-state-example.notes', '已停用', '恢复后');

    // ---- 取消登记
    await page.getByTestId('ep-delete-example.notes').click();
    await page.getByTestId('ep-confirm-delete-example.notes').click();
    await waitText(page, 'ep-empty', '还没有登记', '取消登记后');
    note('unregister', '记录已删除，列表回到空态');
    await page.screenshot({ path: join(SHOTS, '08-unregistered.png'), fullPage: true });

    // ---- 刷新持久性：再登记一条，刷新页面，状态还在
    await page.fill('#ep-endpoint', PLUGIN);
    await page.getByTestId('ep-grant-tools').check();
    await page.getByTestId('ep-submit').click();
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 20_000 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 20_000 });
    note('refresh', '刷新后登记仍在（状态来自服务端）');

    // ---- 窄屏
    const narrow = await browser.newContext({ viewport: { width: 375, height: 667 }, reducedMotion: 'reduce' });
    const phone = await narrow.newPage();
    await login(phone);
    await gotoPlugins(phone);
    await phone.getByTestId('ep-row-example.notes').waitFor({ timeout: 20_000 });
    const layout = await phone.evaluate(() => {
      const main = document.querySelector('.ep-row__main');
      return {
        direction: main ? getComputedStyle(main).flexDirection : '',
        overflow:
          document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    });
    note('narrow-layout', JSON.stringify(layout));
    await phone.screenshot({ path: join(SHOTS, '09-narrow.png'), fullPage: true });
    await narrow.close();

    // ---- 存储检查
    const storage = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
    }));
    note('storage', JSON.stringify(storage));

    // 收尾：把这一轮留下的登记清掉，避免污染下一个用例
    await page.getByTestId('ep-delete-example.notes').click();
    await page.getByTestId('ep-confirm-delete-example.notes').click();
    await waitText(page, 'ep-empty', '还没有登记', '收尾清理');
  } finally {
    await desktop.close();
    await browser.close();
  }

  writeFileSync(join(SHOTS, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
  console.log('[e2e] done');
}

main().catch((error) => {
  console.error('[e2e] failed:', error);
  writeFileSync(join(SHOTS, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
  process.exit(1);
});
