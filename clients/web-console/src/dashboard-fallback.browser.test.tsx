/**
 * 总览「默认值提示」的**真实浏览器**验证。
 *
 * 为什么另开一个文件：`dashboard-fallback.test.tsx` 是 SSR，只能证明"画了什么"。
 * 这条提示的典型故障恰恰是排版——375px 下那句中文原因很长，如果它撑破卡片，
 * 用户得横向拖动才能读完"这是默认值"，提示就等于没起作用。SSR 全绿看不出来。
 *
 * 做法与 `settings-layout.browser.test.tsx` / `life-timeline.browser.test.tsx`
 * 一致：真的 Vite 开发服务器 + 真 Chromium，`/v1/**` 全部由 `page.route` 模拟
 * （不碰真后端、不写库）；没有浏览器时标 skipped，不静默 passed。
 *
 * 断言的是三件事，两种视口各来一遍：
 * 1. 回退文案**真的出现在页面上**（不是只在 SSR 字符串里）；
 * 2. 文档、卡片、提示条都没有横向溢出；
 * 3. 正常（`used=false`）时页面上**没有**这条提示（不制造噪音）。
 *
 * mock 的字段名取自后端真实响应：`sections.phone.data.fallback`，
 * 形状是 `contracts/v1.py::FallbackNotice`。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const NOW = '2026-11-08T02:30:00Z';
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
  console.warn(`[dashboard-fallback.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟数据 */

const USER = {
  id: 'user-1',
  username: 'demo-user',
  role: 'owner',
  subject_id: 'subject-owner',
  display_name: '主人',
  must_change_password: false,
};

/** 后端 `FallbackNotice` 两态：这次用没用默认参数。 */
const FALLBACK_USED = {
  upstream: 'schedule',
  used: true,
  state: 'empty',
  source: 'domain_default',
  reason: '今天还没有日程，先按默认每 20 分钟看一次手机',
};

const FALLBACK_NOT_USED = {
  upstream: 'schedule',
  used: false,
  state: 'ready',
  source: 'upstream',
  reason: null,
};

const EMPTY_SECTION = { status: 'ok', as_of: NOW, data: {} };

function dashboard(
  phone: Record<string, unknown>,
  presence: Record<string, unknown> = {},
  media: Record<string, unknown> | null = null,
): unknown {
  return {
    schema_version: 'v1',
    generated_at: NOW,
    timezone: 'Asia/Shanghai',
    timezone_source: 'default',
    sections: {
      activity: { status: 'ok', as_of: NOW, data: { state: 'running', kind: 'reading', origin: 'simulated' } },
      affect: { status: 'ok', as_of: NOW, data: { emotion: 'calm', intensity: 0.4 } },
      presence: {
        status: 'ok',
        as_of: NOW,
        data: {
          availability: 'low',
          energy: 41,
          social_load: 78,
          describe: '你精神一般，回复可以短一点',
          fallback: { ...FALLBACK_NOT_USED, upstream: 'scene' },
          ...presence,
        },
      },
      proactive: EMPTY_SECTION,
      agenda: { status: 'ok', as_of: NOW, data: { plans: [], commitments: [], goals: [] } },
      memories: EMPTY_SECTION,
      timeline: EMPTY_SECTION,
      relationship: EMPTY_SECTION,
      connectivity: { status: 'ok', as_of: NOW, data: { channels: [], bound_model_roles: [] } },
      phone: { status: 'ok', as_of: NOW, data: phone },
      ...(media ? { media: { status: 'ok', as_of: NOW, data: media } } : {}),
    },
  };
}

function json(body: unknown): { status: number; contentType: string; body: string } {
  return { status: 200, contentType: 'application/json', body: JSON.stringify(body) };
}

/** 打开总览，`/v1/**` 全模拟。 */
async function openDashboard(
  viewport: { width: number; height: number },
  payload: unknown,
): Promise<Page> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/v1/')) return route.continue();
    if (path === '/v1/auth/me') {
      return route.fulfill(
        json({ schema_version: 'v1', user: USER, config_revision: 1, test_mode: false, csrf_token: 'csrf' }),
      );
    }
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.5' }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [] }));
    if (path === '/v1/dashboard') return route.fulfill(json(payload));
    return route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'not_found', message: path } }),
    });
  });

  await page.goto(`${baseUrl}/dashboard`, { waitUntil: 'domcontentloaded' });
  // Vite 开发态下样式是模块注入的：等**样式真的生效**再量，否则量到的是注入前
  // 的裸 DOM（会得出"提示撑破卡片"这种假结论）。
  await page.locator('[data-testid="dash-手机"]').first().waitFor({ timeout: 20_000 });
  await page.waitForFunction(() => document.styleSheets.length > 3, undefined, { timeout: 20_000 });
  await page.waitForTimeout(400);
  return page;
}

interface HintReport {
  /** 页面上所有回退提示的文字（正常时应当是空数组）。 */
  texts: string[];
  /** 提示所在的卡片标题（「手机」/「可用性」）。 */
  cards: string[];
  documentOverflow: number;
  bodyOverflow: number;
  /** 溢出右边界的前几个元素（失败时要能一眼看出是谁撑破的）。 */
  overflowing: string[];
  /** 提示条自己的宽度与被裁切情况。 */
  hintBoxes: { w: number; scrollW: number; clientW: number }[];
  /** 提示所在的卡片有没有被裁。 */
  cardBoxes: { scrollW: number; clientW: number }[];
}

async function inspect(page: Page): Promise<HintReport> {
  return page.evaluate(() => {
    const hints = [...document.querySelectorAll<HTMLElement>('[data-testid="dash-fallback"]')];
    const overflowing = [...document.querySelectorAll<HTMLElement>('.dash *')]
      .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${el.className}`);
    return {
      texts: hints.map((el) => el.textContent ?? ''),
      cards: hints.map((el) => el.closest<HTMLElement>('.dash-card')?.dataset.testid ?? ''),
      documentOverflow:
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
      bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
      overflowing,
      hintBoxes: hints.map((el) => ({
        w: Math.round(el.getBoundingClientRect().width),
        scrollW: el.scrollWidth,
        clientW: el.clientWidth,
      })),
      cardBoxes: [...document.querySelectorAll<HTMLElement>('[data-testid="dash-手机"]')].map((el) => ({
        scrollW: el.scrollWidth,
        clientW: el.clientWidth,
      })),
    };
  });
}

/* ------------------------------------------------------------ 测试 */

const VIEWPORTS = [
  { label: '1440', width: 1440, height: 900 },
  { label: '375', width: 375, height: 667 },
] as const;

for (const viewport of VIEWPORTS) {
  describe(`总览回退提示 @ ${viewport.label}`, () => {
    it('日程不可用时页面上真的出现"默认值"说明，且不横向溢出', async (ctx) => {
      if (skip(ctx)) return;
      const page = await openDashboard(
        { width: viewport.width, height: viewport.height },
        dashboard({
          state: 'idle',
          last_looked_at: '2026-11-08T02:10:00Z',
          next_look_at: '2026-11-08T02:50:00Z',
          next_look_interval_minutes: 20,
          fallback: FALLBACK_USED,
        }),
      );

      // 1) 文案真的出现在页面上（不只是 SSR 字符串里有）
      const hint = page.locator('[data-testid="dash-手机"] [data-testid="dash-fallback"]');
      expect(await hint.count()).toBe(1);
      const text = (await hint.textContent()) ?? '';
      expect(text).toContain('默认值');
      expect(text).toContain('今天还没有日程');
      expect(text).toContain('不是她此刻的真实状态');
      // 真的可见（不是 0 宽 / display:none 那种"在 DOM 里但看不见"）
      expect(await hint.isVisible()).toBe(true);

      // 2) 不横向溢出
      const report = await inspect(page);
      console.info(
        `[dashboard-fallback.browser] ${viewport.label}: doc+${report.documentOverflow}, body+${report.bodyOverflow}, ` +
          `hints=${JSON.stringify(report.hintBoxes)}, cards=${JSON.stringify(report.cardBoxes)}`,
      );
      expect(report.documentOverflow).toBeLessThanOrEqual(1);
      expect(report.bodyOverflow).toBeLessThanOrEqual(1);
      expect(report.overflowing, `溢出元素：${report.overflowing.join(', ')}`).toEqual([]);
      for (const box of report.hintBoxes) {
        // 提示条自己有宽度、没被裁（换行生效），否则"读不完"等于没提示
        expect(box.w).toBeGreaterThan(0);
        expect(box.scrollW).toBeLessThanOrEqual(box.clientW + 1);
      }
      for (const box of report.cardBoxes) {
        expect(box.scrollW).toBeLessThanOrEqual(box.clientW + 1);
      }
      await page.close();
    }, TIMEOUT);

    it('正常（日程算出来的）时不出现任何提示，口径与上面一致', async (ctx) => {
      if (skip(ctx)) return;
      const page = await openDashboard(
        { width: viewport.width, height: viewport.height },
        dashboard({
          state: 'idle',
          last_looked_at: '2026-11-08T02:10:00Z',
          next_look_at: '2026-11-08T02:50:00Z',
          next_look_interval_minutes: 15,
          fallback: FALLBACK_NOT_USED,
        }),
      );
      expect(await page.locator('[data-testid="dash-fallback"]').count()).toBe(0);
      expect(await page.getByText('不是她此刻的真实状态').count()).toBe(0);
      // 内容照常
      expect(await page.locator('[data-testid="dash-手机"]').innerText()).toContain('下次大约');
      const report = await inspect(page);
      expect(report.documentOverflow).toBeLessThanOrEqual(1);
      expect(report.bodyOverflow).toBeLessThanOrEqual(1);
      await page.close();
    }, TIMEOUT);
  });
}

describe('总览回退提示：场景回退（可用性卡片）@ 375', () => {
  it('场景不明时把默认值说出来，可用性那句话不被误标，且不横向溢出', async (ctx) => {
    if (skip(ctx)) return;
    const page = await openDashboard(
      { width: 375, height: 667 },
      dashboard(
        {
          state: 'idle',
          last_looked_at: '2026-11-08T02:10:00Z',
          next_look_at: '2026-11-08T02:50:00Z',
          next_look_interval_minutes: 20,
          fallback: FALLBACK_NOT_USED,
        },
        {
          describe: '状态正常，可以随时说话',
          fallback: {
            upstream: 'scene',
            used: true,
            state: 'empty',
            source: 'domain_default',
            reason: '场景不明（没有有效场景 / 已过期），按未知处理，不冒充她在做什么',
          },
        },
      ),
    );

    const presence = page.locator('[data-testid="dash-可用性"]');
    expect(await presence.locator('[data-testid="dash-fallback"]').count()).toBe(1);
    const text = await presence.innerText();
    expect(text).toContain('场景不明');
    expect(text).toContain('不是她此刻的真实状态');
    // 可用性自己的那句话来自精力/社交负担，不该被标成默认值
    expect(text).toContain('状态正常，可以随时说话');
    expect(text).not.toContain('是按默认情况说的');

    const report = await inspect(page);
    console.info(
      `[dashboard-fallback.browser] 375 场景回退: doc+${report.documentOverflow}, hints=${JSON.stringify(report.hintBoxes)}`,
    );
    expect(report.documentOverflow).toBeLessThanOrEqual(1);
    expect(report.bodyOverflow).toBeLessThanOrEqual(1);
    expect(report.overflowing, `溢出元素：${report.overflowing.join(', ')}`).toEqual([]);
    // 手机那块正常，所以只该有一条提示（不是每块都挂标签）
    expect(report.texts.length).toBe(1);
    expect(report.cards).toEqual(['dash-可用性']);
    await page.close();
  }, TIMEOUT);
});

/**
 * 「正在忙什么」在真浏览器里：1440 与 375 都要真的出现在页面上，
 * 且不把页面撑出横向滚动。
 *
 * 为什么不只做 SSR 断言：这块是新增卡片，宽度与换行只有真排版才知道。
 * 「她正在看视频」这类短句在 375 下换行后如果卡片没有弹性约束，最容易撑破。
 */
describe('正在忙什么：真浏览器渲染', () => {
  it('1440：自己的会话 → 说出在看什么，且无横向溢出', async () => {
    if (!browser || launchError) {
      console.warn(`[dashboard-media.browser] 跳过：${launchError}`);
      return;
    }
    const page = await openDashboard(
      { width: 1440, height: 900 },
      dashboard({ state: 'idle' }, {}, {
        kind: 'video_watch',
        started_at: '2026-11-08T02:12:00Z',
        in_my_session: true,
      }),
    );
    const title = page.getByText('正在忙什么').first();
    await title.waitFor({ timeout: 10_000 });
    expect(await title.isVisible()).toBe(true);
    const body = page.getByText('正在看视频').first();
    expect(await body.isVisible()).toBe(true);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    await page.close();
  }, 60_000);

  it('375：别人的会话只说"正忙着"，且无横向溢出', async () => {
    if (!browser || launchError) {
      console.warn(`[dashboard-media.browser] 跳过：${launchError}`);
      return;
    }
    const page = await openDashboard(
      { width: 375, height: 812 },
      dashboard({ state: 'idle' }, {}, {
        kind: 'bilibili_feed',
        started_at: '2026-11-08T02:12:00Z',
        in_my_session: false,
      }),
    );
    const text = page.getByText('正忙着看东西').first();
    await text.waitFor({ timeout: 10_000 });
    expect(await text.isVisible()).toBe(true);
    // 「刷视频」是别人会话里的细节，这一侧不该出现
    expect(await page.getByText('正在刷视频').count()).toBe(0);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    await page.close();
  }, 60_000);
});
