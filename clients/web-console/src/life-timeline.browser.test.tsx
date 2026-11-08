/**
 * 生活时间轴 + 总览两块摘要的**真实浏览器**验证。
 *
 * 仓库没有 jsdom：SSR 只能断言"画出来是什么"，量不到布局。这里用 playwright
 * 起一个真的 Vite 开发服务器 + 真 Chromium，`/v1/**` 全部由 `page.route` 模拟
 * （不碰真后端、不写库），在 1440 与 375 两个视口下量横向溢出。
 *
 * 断言的口径与 `prompt.browser.test.tsx` 一致：
 * `documentElement.scrollWidth <= clientWidth`、`body` 同理，再加上时间轴
 * 根节点自己的 `scrollWidth <= clientWidth`（内部 grid 没有撑破）。
 *
 * 没有 playwright 浏览器时这一组标成 skipped（不是静默 passed），
 * `pnpm vitest run` 在没装浏览器的机器上仍然全绿，但不会假装测过。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const NOW = new Date().toISOString();
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
  console.warn(`[life-timeline.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟数据 */

const USER = {
  id: 'user-1',
  username: 'lingxi',
  role: 'owner',
  subject_id: 'subject-owner',
  display_name: '主人',
  must_change_password: false,
};

/** 长时间轴：够多段、父子、跨午夜、事实、意向、梦，用来压 375px 的宽度。 */
const TIMELINE = {
  schema_version: 'v1',
  state: 'ready',
  plan_state: 'ready',
  revision: 7,
  last_error_code: null,
  display_date: '2026-11-08',
  display_timezone: 'Asia/Shanghai',
  display_timezone_source: 'preference',
  display_window_start_at: '2026-11-07T16:00:00Z',
  display_window_end_at: '2026-11-08T16:00:00Z',
  actor_timezone: 'America/New_York',
  actor_timezone_source: 'preference',
  plan_day: '2026-11-07',
  plan_window_start_at: '2026-11-07T05:00:00Z',
  plan_window_end_at: '2026-11-08T05:00:00Z',
  outline_due_at: '2026-11-07T02:00:00Z',
  timezone_source: 'preference',
  generated_at: '2026-11-07T13:45:00Z',
  segments: [
    {
      schema_version: 'v1',
      segment_id: 'seg-night',
      plan_id: 'plan-1',
      plan_day: '2026-11-07',
      parent_id: null,
      layer: 'block',
      is_leaf: true,
      status: 'current',
      revision: 7,
      source: 'model',
      anchor_kind: 'sleep',
      commitment_level: 'tentative',
      place_id: null,
      title: '睡觉（预计休息，不是"她睡着了"）',
      start_at: '2026-11-07T13:00:00Z',
      end_at: '2026-11-07T22:00:00Z',
      display_start_at: '2026-11-07T16:00:00Z',
      display_end_at: '2026-11-07T22:00:00Z',
      continues_from_previous_day: true,
      continues_next_day: false,
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-block',
      plan_id: 'plan-1',
      plan_day: '2026-11-07',
      parent_id: null,
      layer: 'block',
      is_leaf: false,
      status: 'current',
      revision: 7,
      source: 'model',
      anchor_kind: '',
      commitment_level: 'tentative',
      place_id: null,
      title: '上午的工作块（一个很长的标题，用来验证 375px 下不会把卡片撑破）',
      start_at: '2026-11-08T00:00:00Z',
      end_at: '2026-11-08T04:00:00Z',
      display_start_at: '2026-11-08T00:00:00Z',
      display_end_at: '2026-11-08T04:00:00Z',
      continues_from_previous_day: false,
      continues_next_day: false,
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-hour',
      plan_id: 'plan-1',
      plan_day: '2026-11-07',
      parent_id: 'seg-block',
      layer: 'hour',
      is_leaf: true,
      status: 'current',
      revision: 7,
      source: 'model',
      anchor_kind: '',
      commitment_level: 'tentative',
      place_id: null,
      title: '整理邮件',
      start_at: '2026-11-08T00:00:00Z',
      end_at: '2026-11-08T01:00:00Z',
      display_start_at: '2026-11-08T00:00:00Z',
      display_end_at: '2026-11-08T01:00:00Z',
      continues_from_previous_day: false,
      continues_next_day: false,
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-minute',
      plan_id: 'plan-1',
      plan_day: '2026-11-07',
      parent_id: 'seg-hour',
      layer: 'minute',
      is_leaf: true,
      status: 'current',
      revision: 7,
      source: 'model',
      anchor_kind: '',
      commitment_level: 'tentative',
      place_id: null,
      title: '回一封急件',
      start_at: '2026-11-08T00:15:00Z',
      end_at: '2026-11-08T00:30:00Z',
      display_start_at: '2026-11-08T00:15:00Z',
      display_end_at: '2026-11-08T00:30:00Z',
      continues_from_previous_day: false,
      continues_next_day: false,
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-commit',
      plan_id: 'plan-1',
      plan_day: '2026-11-07',
      parent_id: null,
      layer: 'hour',
      is_leaf: true,
      status: 'current',
      revision: 7,
      source: 'user',
      anchor_kind: 'commitment',
      commitment_level: 'confirmed',
      place_id: null,
      title: '约定通话',
      start_at: '2026-11-08T02:00:00Z',
      end_at: '2026-11-08T03:00:00Z',
      display_start_at: '2026-11-08T02:00:00Z',
      display_end_at: '2026-11-08T03:00:00Z',
      continues_from_previous_day: false,
      continues_next_day: false,
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-midnight',
      plan_id: 'plan-1',
      plan_day: '2026-11-08',
      parent_id: null,
      layer: 'hour',
      is_leaf: true,
      status: 'current',
      revision: 7,
      source: 'model',
      anchor_kind: '',
      commitment_level: 'tentative',
      place_id: null,
      title: '夜间整理',
      start_at: '2026-11-08T15:30:00Z',
      end_at: '2026-11-08T17:00:00Z',
      display_start_at: '2026-11-08T15:30:00Z',
      display_end_at: '2026-11-08T16:00:00Z',
      continues_from_previous_day: false,
      continues_next_day: true,
    },
  ],
  facts: [
    {
      schema_version: 'v1',
      activity_id: 'fact-clock',
      state: 'completed',
      origin: 'simulated',
      evidence_kind: 'clock_only',
      scene_valid_until: null,
      started_at: '2026-11-08T00:00:00Z',
      ended_at: '2026-11-08T00:10:00Z',
      place_id: null,
    },
    {
      schema_version: 'v1',
      activity_id: 'fact-observed',
      state: 'completed',
      origin: 'simulated',
      evidence_kind: 'observed',
      scene_valid_until: null,
      started_at: '2026-11-08T00:15:00Z',
      ended_at: '2026-11-08T00:30:00Z',
      place_id: null,
    },
  ],
  intents: [
    {
      schema_version: 'v1',
      intention_id: 'intent-1',
      target_start_day: '2026-11-09',
      target_end_day: '2026-11-09',
      state: 'planned',
      source_kind: 'user',
      visibility: 'actor_private',
      priority: 50,
    },
    {
      schema_version: 'v1',
      intention_id: 'intent-2',
      target_start_day: '2026-11-10',
      target_end_day: '2026-11-13',
      state: 'confirmed',
      source_kind: 'recurrence',
      visibility: 'actor_private',
      priority: 40,
    },
  ],
  horizon_unplanned: true,
  dream: {
    schema_version: 'v1',
    status: 'ready',
    origin: 'simulated',
    visibility: 'actor_private',
    title: '一条很长的走廊',
    narrative: '她在走廊里走了很久，尽头是一扇没有把手的门。',
    restricted: false,
  },
  superseded_segment_count: 2,
  hidden_segment_count: 0,
  hidden_fact_count: 0,
  hidden_intent_count: 0,
  has_private_content: false,
  note: null,
};

const TODAY_OUTLINE = {
  schema_version: 'v1',
  state: 'ready',
  display_date: '2026-11-08',
  display_timezone: 'Asia/Shanghai',
  plan_day: '2026-11-07',
  actor_timezone: 'America/New_York',
  items: [
    {
      schema_version: 'v1',
      segment_id: 'seg-hour',
      relation: 'now',
      title: '整理邮件（一条很长的摘要，用来验证 375px 下不横向溢出）',
      start_at: '2026-11-08T00:00:00Z',
      end_at: '2026-11-08T01:00:00Z',
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-commit',
      relation: 'anchor',
      title: '约定通话',
      start_at: '2026-11-08T02:00:00Z',
      end_at: '2026-11-08T03:00:00Z',
    },
  ],
  note: null,
};

const INTENT_TEASER = {
  schema_version: 'v1',
  horizon_unplanned: true,
  items: [
    {
      schema_version: 'v1',
      intention_id: 'intent-1',
      title: '想去看海（候选，不是已经约好）',
      target_start_day: '2026-11-09',
      target_end_day: '2026-11-09',
      state: 'planned',
      source_kind: 'user',
    },
    {
      schema_version: 'v1',
      intention_id: 'intent-2',
      title: '整理旧照片',
      target_start_day: '2026-11-10',
      target_end_day: '2026-11-12',
      state: 'confirmed',
      source_kind: 'recurrence',
    },
  ],
  note: '未来日尚未生成大纲；这里是意向，不是那一天的安排',
};

const EMPTY_SECTION = { status: 'ok', as_of: NOW, data: {} };

const DASHBOARD = {
  schema_version: 'v1',
  generated_at: NOW,
  timezone: 'Asia/Shanghai',
  sections: {
    activity: EMPTY_SECTION,
    affect: EMPTY_SECTION,
    presence: EMPTY_SECTION,
    proactive: EMPTY_SECTION,
    agenda: EMPTY_SECTION,
    memories: EMPTY_SECTION,
    timeline: EMPTY_SECTION,
    relationship: EMPTY_SECTION,
    connectivity: EMPTY_SECTION,
    phone: EMPTY_SECTION,
    today_outline: { status: 'ok', as_of: NOW, data: TODAY_OUTLINE },
    intent_teaser: { status: 'ok', as_of: NOW, data: INTENT_TEASER },
  },
};

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/* ------------------------------------------------------------ 打开页面 */

async function openRoute(
  path: string,
  viewport: { width: number; height: number },
  waitTestId: string,
): Promise<Page> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === '/v1/auth/me') {
      return route.fulfill(
        json({
          schema_version: 'v1',
          user: USER,
          config_revision: 1,
          test_mode: false,
          csrf_token: 'csrf',
        }),
      );
    }
    if (pathname === '/v1/version') return route.fulfill(json({ current: '0.0.3' }));
    if (pathname === '/v1/persona') return route.fulfill(json({ name: '灵', display_name: '灵' }));
    if (pathname === '/v1/sessions') return route.fulfill(json({ sessions: [] }));
    if (pathname === '/v1/dashboard') return route.fulfill(json(DASHBOARD));
    if (pathname === '/v1/life/timeline') return route.fulfill(json(TIMELINE));
    if (pathname === '/v1/activity') {
      return route.fulfill(json({ current: null, history: [], note: '' }));
    }
    if (pathname === '/v1/goals') return route.fulfill(json({ goals: [] }));
    if (pathname === '/v1/proactive') {
      return route.fulfill(json({ pending: [], gate: false, gate_reasons: [] }));
    }
    if (pathname.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: pathname } }, 404));
    }
    return route.continue();
  });

  await page.goto(`${baseUrl}${path}`, { waitUntil: 'domcontentloaded' });
  await page.locator(`[data-testid="${waitTestId}"]`).waitFor();
  return page;
}

interface Widths {
  client: number;
  scroll: number;
}

async function measure(page: Page): Promise<Record<string, Widths>> {
  return page.evaluate(() => {
    const widths = (element: Element | null): { client: number; scroll: number } => {
      if (!element) return { client: 0, scroll: 0 };
      const html = element as HTMLElement;
      return { client: html.clientWidth, scroll: html.scrollWidth };
    };
    return {
      documentElement: widths(document.documentElement),
      body: widths(document.body),
      page: widths(document.querySelector('.app-main .page')),
      timeline: widths(document.querySelector('[data-testid="life-timeline"]')),
      overview: widths(document.querySelector('.dash__grid')),
    };
  });
}

function report(label: string, widths: Record<string, Widths>): void {
  const parts = Object.entries(widths)
    .filter(([, value]) => value.client > 0 || value.scroll > 0)
    .map(([key, value]) => `${key} ${value.scroll}/${value.client}`);
  console.info(`[life-timeline.browser] ${label}: ${parts.join(', ')}`);
}

async function assertNoHorizontalOverflow(
  page: Page,
  label: string,
  selectors: string[],
): Promise<Record<string, Widths>> {
  const widths = await measure(page);
  report(label, widths);
  expect(widths.documentElement?.scroll).toBeLessThanOrEqual(widths.documentElement?.client ?? 0);
  expect(widths.body?.scroll).toBeLessThanOrEqual(widths.body?.client ?? 0);
  for (const selector of selectors) {
    const value = widths[selector];
    expect(value, `量不到 ${selector}`).toBeDefined();
    expect(value?.scroll, `${label} ${selector} 横向溢出`).toBeLessThanOrEqual(value?.client ?? 0);
  }
  return widths;
}

/* ------------------------------------------------------------ 测试 */

describe('真实浏览器：生活时间轴与总览两块', () => {
  it(
    '1440：时间轴 / 总览都没有横向溢出',
    async (ctx) => {
      if (skip(ctx)) return;
      const life = await openRoute('/life', { width: 1440, height: 900 }, 'life-timeline');
      await life.locator('[data-testid="tl-seg-seg-hour"]').waitFor();
      await assertNoHorizontalOverflow(life, 'life 1440', ['page', 'timeline']);
      // 折叠按钮真的在（不是只有文字）且默认展开
      const toggle = life.locator('[data-testid="tl-toggle-seg-block"]');
      expect(await toggle.getAttribute('aria-expanded')).toBe('true');
      await toggle.click();
      expect(await life.locator('[data-testid="tl-seg-seg-minute"]').count()).toBe(0);
      await toggle.click();
      expect(await life.locator('[data-testid="tl-seg-seg-minute"]').count()).toBe(1);

      const dash = await openRoute('/dashboard', { width: 1440, height: 900 }, 'dash-今天的节奏');
      await dash.locator('[data-testid="dash-outline-list"]').waitFor();
      await assertNoHorizontalOverflow(dash, 'dashboard 1440', ['page', 'overview']);
      expect(await dash.getByText('查看生活').count()).toBeGreaterThan(0);
    },
    TIMEOUT,
  );

  it(
    '375：时间轴 / 总览都没有横向溢出（数字见 stdout）',
    async (ctx) => {
      if (skip(ctx)) return;
      const life = await openRoute('/life', { width: 375, height: 667 }, 'life-timeline');
      await life.locator('[data-testid="tl-seg-seg-hour"]').waitFor();
      const widths = await assertNoHorizontalOverflow(life, 'life 375', ['page', 'timeline']);
      // 375 下时间列折进段内，标题允许换行：段本身没有被裁掉。
      expect(await life.locator('[data-testid="tl-seg-seg-block"]').count()).toBe(1);
      expect((widths.timeline?.client ?? 0)).toBeLessThanOrEqual(375);

      const dash = await openRoute('/dashboard', { width: 375, height: 667 }, 'dash-今天的节奏');
      await dash.locator('[data-testid="dash-outline-list"]').waitFor();
      const dashWidths = await assertNoHorizontalOverflow(dash, 'dashboard 375', ['page', 'overview']);
      expect(dashWidths.overview?.client ?? 0).toBeLessThanOrEqual(375);
      // relation 标签没有被省略成"此…"：innerText 完整可读。
      expect(await dash.getByText('此刻可能在做').count()).toBeGreaterThan(0);
    },
    TIMEOUT,
  );
});
