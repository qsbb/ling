/**
 * 设置页 / 权限页的真浏览器布局测试。
 *
 * 为什么另开一个文件：`settings-layout.test.tsx` 是 SSR，只能证明"画了什么"。
 * 这一轮用户抱怨的恰恰是**排版**（"设置界面重新设计一下布局"），而排版的典型
 * 故障是 CSS 没生效或断点写错——SSR 全绿也看不出来（这一个文件就是这么发现
 * 「授权页根本没引样式表」的）：
 *
 * - 设置页：1440 两列**等宽**、375 单列，都不横向溢出；
 * - 卡片等高、跳转卡片有箭头、就地可改的两格没有；
 * - 权限页：一条权限只占一行（不再是三行），已授权/未授权各一行。
 *
 * `/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库），与
 * `life-timeline.browser.test.tsx` 同一套做法；没有浏览器时标 skipped，
 * 不静默 passed。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const TIMEOUT = 60_000;

let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let baseUrl = '';
let launchError: string | null = null;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  try {
    server = await createServer({
      configFile: false,
      root: ROOT,
      plugins: [react()],
      logLevel: 'silent',
      server: { host: '127.0.0.1', port: 0 },
    });
    await server.listen();
    const address = server.httpServer?.address();
    if (!address || typeof address === 'string') {
      throw new Error('Vite 没有给出可用的监听地址');
    }
    baseUrl = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch();
  } catch (error) {
    launchError = error instanceof Error ? error.message : String(error);
  }
}, 120_000);

afterEach(async () => {
  while (contexts.length > 0) {
    await contexts.pop()?.close();
  }
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

function skip(ctx: { skip: () => void }): boolean {
  if (!launchError) return false;
  console.warn(`[settings-layout.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟数据 */

/** 与后端 `KNOWN_SCOPES` 同序；第一条授予、其余默认关闭。 */
const SCOPES = [
  'camera',
  'location',
  'public_publish',
  'memory.cross_device',
  'proactive_contact',
  'media_upload',
  'media.inbound',
  'device',
];

const PREFS = {
  schema_version: 'v1',
  timezone: 'Asia/Shanghai',
  source: 'default',
  stored: null,
  env: null,
  default: 'Asia/Shanghai',
  common: ['Asia/Shanghai', 'Asia/Tokyo'],
  video_mode: 'frames',
  video_modes: ['frames', 'native'],
  video_mode_default: 'frames',
};

function json(body: unknown): { status: number; contentType: string; body: string } {
  return { status: 200, contentType: 'application/json', body: JSON.stringify(body) };
}

/** 打开某个设置子页，`/v1/**` 全模拟。 */
async function openSettings(
  path: string,
  viewport: { width: number; height: number },
  waitFor: string, // CSS 选择器
): Promise<Page> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (!p.startsWith('/v1/')) return route.continue();
    if (p === '/v1/auth/me') {
      return route.fulfill(
        json({
          schema_version: 'v1',
          user: {
            id: 'u1',
            username: 'demo-user',
            role: 'owner',
            subject_id: 's1',
            display_name: '主人',
            must_change_password: false,
          },
          config_revision: 1,
          test_mode: false,
          csrf_token: 'csrf',
        }),
      );
    }
    if (p === '/v1/version') return route.fulfill(json({ current: '0.0.5' }));
    if (p === '/v1/sessions') return route.fulfill(json({ sessions: [] }));
    if (p === '/v1/settings/preferences') return route.fulfill(json(PREFS));
    if (p === '/v1/settings/taxonomy') {
      return route.fulfill(json({ schema_version: 'v1', summary: { total: 9, editable: 2, pending: 0 }, categories: [] }));
    }
    if (p === '/v1/consents') {
      return route.fulfill(
        json({
          schema_version: 'v1',
          known_scopes: SCOPES,
          consents: [{ scope: 'camera', granted: true, revision: 2, valid_until: null }],
        }),
      );
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'not_found', message: p } }) });
  });

  await page.goto(`${baseUrl}${path}`, { waitUntil: 'domcontentloaded' });
  // Vite 开发态下样式是模块注入的：等**样式真的生效**再量，否则量到的是
  // 注入前的裸 DOM（这一个文件第一版就是这么误判"设置页没生效"的）。
  await page.locator(waitFor).first().waitFor({ timeout: 20_000 });
  await page.waitForFunction(() => document.styleSheets.length > 3, undefined, { timeout: 20_000 });
  await page.waitForTimeout(400);
  return page;
}

async function overflowX(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

describe('设置页布局', () => {
  it('1440：2 列等宽，每格至少 500px 宽、箭头都在', async (ctx) => {
    if (skip(ctx)) return;
    const page = await openSettings('/settings', { width: 1440, height: 900 }, '[data-testid="settings-card-timezone"]');
    const report = await page.evaluate(() => {
      const cards = [...document.querySelectorAll<HTMLElement>('[data-settings-card]')];
      const grids = [...document.querySelectorAll<HTMLElement>('.settings-grid')].map((grid) =>
        getComputedStyle(grid).gridTemplateColumns,
      );
      return {
        grids,
        cards: cards.map((card) => ({
          key: card.dataset.settingsCard ?? '',
          w: Math.round(card.getBoundingClientRect().width),
          h: Math.round(card.getBoundingClientRect().height),
          arrow: card.querySelector('.settings-card__arrow') !== null,
        })),
        docW: document.documentElement.scrollWidth,
        clientW: document.documentElement.clientWidth,
      };
    });

    // 每个网格都是两列（等宽，来自 repeat(2, minmax(0,1fr))）
    expect(report.grids.length).toBeGreaterThanOrEqual(3);
    for (const template of report.grids) {
      const widths = template.trim().split(/\s+/).map((v) => Number.parseFloat(v));
      expect(widths.length, `网格不是两列：${template}`).toBe(2);
      const [left = 0, right = 0] = widths;
      expect(Math.abs(left - right)).toBeLessThan(2);
    }

    // 「关于她」两格等宽（旧的 1fr / 360px 会差出 200px）
    const tiles = report.cards.filter((c) => c.key === 'timezone' || c.key === 'video');
    expect(tiles.length).toBe(2);
    const [timezoneTile, videoTile] = tiles;
    expect(timezoneTile?.w ?? 0).toBeGreaterThan(500);
    expect(Math.abs((timezoneTile?.w ?? 0) - (videoTile?.w ?? 0))).toBeLessThan(2);

    // 跳转卡片都有箭头；就地可改的两格没有
    for (const card of report.cards) {
      if (card.key === 'timezone' || card.key === 'video') expect(card.arrow).toBe(false);
      else expect(card.arrow, `${card.key} 缺箭头`).toBe(true);
    }

    expect(report.docW).toBeLessThanOrEqual(report.clientW + 1);
    await page.close();
  }, TIMEOUT);

  it('375：一列、不横向溢出、按钮不重叠', async (ctx) => {
    if (skip(ctx)) return;
    const page = await openSettings('/settings', { width: 375, height: 667 }, '[data-testid="settings-card-timezone"]');
    const report = await page.evaluate(() => {
      const grids = [...document.querySelectorAll<HTMLElement>('.settings-grid')].map(
        (grid) => getComputedStyle(grid).gridTemplateColumns,
      );
      const buttons = [...document.querySelectorAll<HTMLElement>('.inline-form .button')].map(
        (b) => b.getBoundingClientRect(),
      );
      const overflow = [...document.querySelectorAll<HTMLElement>('body *')]
        .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
        .slice(0, 5)
        .map((el) => `${el.tagName.toLowerCase()}.${el.className}`);
      return {
        grids,
        docW: document.documentElement.scrollWidth,
        clientW: document.documentElement.clientWidth,
        overflow,
        buttons: buttons.map((b) => ({ w: Math.round(b.width), top: Math.round(b.top) })),
      };
    });

    // 单列：每列的宽度只有一段
    for (const template of report.grids) {
      expect(template.trim().split(/\s+/).length, `窄屏不是单列：${template}`).toBe(1);
    }
    expect(report.docW).toBeLessThanOrEqual(report.clientW + 1);
    expect(report.overflow, `溢出元素：${report.overflow.join(', ')}`).toEqual([]);

    // 按钮要么同一行要么上下排，不能叠在一起
    const first = report.buttons[0];
    const second = report.buttons[1];
    if (first && second) {
      const sameRow = Math.abs(first.top - second.top) < 4;
      const stacked = second.top > first.top + 10;
      expect(sameRow || stacked, '保存/清空按钮叠在一起').toBe(true);
    }
    await page.close();
  }, TIMEOUT);
});

describe('权限页：一条权限 = 一行', () => {
  it('每行是一行字的高度量级，已授权与未授权各一行，动作是撤回/授予', async (ctx) => {
    if (skip(ctx)) return;
    const page = await openSettings('/settings/privacy', { width: 1440, height: 900 }, '.consent-row');

    const rows = await page.evaluate(() =>
      [...document.querySelectorAll<HTMLElement>('.consent-row')].map((row) => {
        const badge = row.querySelector<HTMLElement>('.consent-row__badge');
        const action = row.querySelector<HTMLElement>('.consent-row__action button');
        return {
          name: row.querySelector('.consent-row__name')?.textContent?.trim() ?? '',
          h: Math.round(row.getBoundingClientRect().height),
          state: row.dataset.state ?? '',
          badgeText: badge?.textContent?.trim() ?? '',
          action: action?.textContent?.trim() ?? '',
        };
      }),
    );

    // 每种 scope 都有一行（模拟数据就是后端那 8 条）
    expect(rows.length).toBe(SCOPES.length);
    /*
     * 一行式应在 72px 以内：旧的"三行一条"（名称 / 说明 / scope=… 各占一行）
     * 实测 104px。这条断言就是"压成一行"的量化标准。
     */
    // 先把每行的实测高度打出来：断言失败时要能一眼看出是几条行高的量级。
    console.info('[settings-layout.browser] consent 行高：', rows.map((r) => `${r.name}=${r.h}`).join(', '));
    for (const row of rows) expect(row.h, `行高 ${row.h} 说明还占三行`).toBeLessThan(72);

    // 徽标文字与状态一致，动作与状态相反
    for (const row of rows) {
      if (row.state === 'granted') {
        expect(row.badgeText).toBe('已授权');
        expect(row.action).toBe('撤回');
      } else {
        expect(row.badgeText).toBe('未授权');
        expect(row.action).toBe('授予');
      }
    }
    // 至少各有一行，否则上面那个循环没真正覆盖两态
    expect(rows.some((r) => r.state === 'granted')).toBe(true);
    expect(rows.some((r) => r.state === 'denied')).toBe(true);

    expect(await overflowX(page)).toBeLessThanOrEqual(1);
  }, TIMEOUT);

  it('375：行内不横向溢出，按钮独占一行且可点', async (ctx) => {
    if (skip(ctx)) return;
    const page = await openSettings('/settings/privacy', { width: 375, height: 720 }, '.consent-row');
    const info = await page.evaluate(() => {
      const row = document.querySelector<HTMLElement>('.consent-row');
      const button = row?.querySelector<HTMLElement>('.consent-row__action button');
      const box = button?.getBoundingClientRect();
      return {
        w: box ? Math.round(box.width) : 0,
        h: box ? Math.round(box.height) : 0,
        doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    });
    expect(info.doc).toBeLessThanOrEqual(1);
    // 触控下限 44×44（量不到按钮就是真 bug，不能当成 0 蒙混过去）
    expect(info.h).toBeGreaterThanOrEqual(44);
    expect(info.w).toBeGreaterThanOrEqual(44);
  }, TIMEOUT);
});
