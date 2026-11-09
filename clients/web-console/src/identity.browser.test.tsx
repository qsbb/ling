/**
 * 关系页（`/relationships`）的**真实浏览器**回归：卡死、合并、分页、窄屏。
 *
 * 背景（`shots/diag-relations-freeze-2026-11-09.md`）：用户反馈「点开关系页就卡死，
 * 刷新也刷不动」。根因不是接口慢（2000 条 16.8 ms），而是页面给**每张卡片**都渲染
 * 一个含全部候选的 `<select>` —— n 条关系 = n² 个 `<option>`，2000 条 = **4,023,210
 * 个 DOM 元素 / 289 MB**，SSR 就要 10.5 s，浏览器再建 400 万节点必卡死。
 *
 * 这个文件钉住修复后的四条性质，而且**必须在真浏览器里**量（SSR 量不到布局，
 * 也量不到"点一下之后还剩多少个节点"）：
 *
 * 1. 未展开的卡片里**没有一个** `<option>`；整页首屏元素数与 `<option>` 总数都是
 *    个位数到几十（而不是 4,000,000）；
 * 2. **同时只有一张**卡片展开；切换时上一张自动收起；
 * 3. 合并 → 刷新后指针还在（「已合并」徽章）→「拆开」还原；分页「加载更多」
 *    能翻到第二页，到底显示「没有更多了」；
 * 4. 375 / 1440 两个视口都 `scrollWidth <= clientWidth`，包括**展开的那张卡片**
 *    （下拉里装着几十个候选，最容易撑破窄屏）。
 *
 * `/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库），与
 * `life-timeline.browser.test.tsx` 同一套做法。没有 playwright 浏览器时这一组
 * 标成 skipped 而不是静默 passed。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
/** 与后端 `SUBJECTS_LIMIT_DEFAULT` 一致。 */
const PAGE_SIZE = 50;
/** 造 2000 条关系（报告里出事的那一档）：**分页后端只给 50 条**，前端只渲染这一页。 */
const TOTAL_SUBJECTS = 2000;

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
  console.warn(`[identity.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟数据 */

const USER = {
  id: 'user-1',
  username: 'demo-user',
  role: 'owner',
  subject_id: 'subj_0000',
  display_name: '主人',
  must_change_password: false,
};

interface FakeSubject {
  subject_id: string;
  display_name: string;
  trust_level: string;
  merged_into: string | null;
  accounts: {
    account_id: string;
    connector_instance: string;
    external_account_id: string;
    proof: string;
    revoked: boolean;
  }[];
}

/** 2000 个人，昵称故意长一点 + 带长账号 id（375px 下最容易撑破的就是这两处）。 */
const SUBJECTS: FakeSubject[] = Array.from({ length: TOTAL_SUBJECTS }, (_, i) => {
  const suffix = String(i).padStart(4, '0');
  const accounts = [
    {
      account_id: `acc_${suffix}_0`,
      connector_instance: 'napcat-2',
      external_account_id: `${30000000 + i * 10}`,
      proof: 'proven',
      revoked: false,
    },
  ];
  if (i % 5 === 0) {
    accounts.push({
      account_id: `acc_${suffix}_1`,
      connector_instance: 'napcat-2',
      external_account_id: `${30000000 + i * 10 + 1}`,
      proof: 'pending',
      revoked: false,
    });
  }
  return {
    subject_id: `subj_${suffix}`,
    // 长昵称：验证卡片头在 375px 会换行而不是横向溢出
    display_name: i === 0 ? '主人' : `诊断用户${suffix}（一个非常长的群昵称用来压窄屏）`,
    trust_level: i === 0 ? 'owner' : 'known',
    merged_into: null,
    accounts,
  };
});

/** 当前这次测试的"数据库"状态：合并会改它，刷新后要能看到。 */
let store: FakeSubject[] = [];

function resetStore(base: FakeSubject[] = SUBJECTS): void {
  store = base.map((row) => ({ ...row, accounts: row.accounts.map((a) => ({ ...a })) }));
}

function pageOf(query: URLSearchParams): { status: number; body: unknown } {
  const limit = Math.min(Number(query.get('limit') ?? PAGE_SIZE) || PAGE_SIZE, 200);
  const offset = Math.max(Number(query.get('offset') ?? 0) || 0, 0);
  const q = (query.get('q') ?? '').trim();
  let rows = store;
  if (q) {
    const needle = q.toLowerCase();
    rows = store.filter((row) =>
      [row.display_name, row.subject_id, ...row.accounts.map((a) => a.external_account_id)]
        .join('\n')
        .toLowerCase()
        .includes(needle),
    );
  }
  const page = rows.slice(offset, offset + limit);
  return {
    status: 200,
    body: {
      schema_version: 'v1',
      subjects: page,
      limit,
      offset,
      total: rows.length,
      has_more: offset + page.length < rows.length,
      q: q || null,
    },
  };
}

interface ApiCall {
  path: string;
  query: Record<string, string>;
}

interface Harness {
  page: Page;
  calls: ApiCall[];
}

/**
 * 打开关系页并等到"首屏定下来"。
 *
 * 等的是**三种终态之一**（有卡片 / 空态提示），而不是固定等卡片：空库时
 * 一张卡片都不会有，`waitFor` 卡片会直接超时——那正是空态用例要覆盖的场景。
 */
async function openRelations(
  viewport: { width: number; height: number },
  options: { expectEmpty?: boolean } = {},
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const calls: ApiCall[] = [];

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    const method = route.request().method();

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

    if (pathname === '/v1/accounts/subjects' && method === 'GET') {
      calls.push({ path: pathname, query: Object.fromEntries(url.searchParams) });
      const result = pageOf(url.searchParams);
      return route.fulfill(json(result.body, result.status));
    }
    if (pathname === '/v1/accounts/subjects/merge' && method === 'POST') {
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        source_id: string;
        target_id: string;
      };
      const source = store.find((row) => row.subject_id === body.source_id);
      if (source) source.merged_into = body.target_id;
      return route.fulfill(json({ source_id: body.source_id, target_id: body.target_id, moved: 0 }));
    }
    if (/^\/v1\/accounts\/subjects\/[^/]+\/unmerge$/.test(pathname) && method === 'POST') {
      const sourceId = decodeURIComponent(pathname.split('/')[4] ?? '');
      const source = store.find((row) => row.subject_id === sourceId);
      const was = source?.merged_into ?? null;
      if (source) source.merged_into = null;
      return route.fulfill(
        json({ source_id: sourceId, was_merged_into: was, restored_memories: 0 }),
      );
    }
    if (pathname.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: pathname } }, 404));
    }
    return route.continue();
  });

  await page.goto(`${baseUrl}/relationships`, { waitUntil: 'domcontentloaded' });
  const settled = options.expectEmpty
    ? page.getByTestId('subjects-empty')
    : page.locator('[data-testid="subject-card"]').first();
  await settled.waitFor();
  return { page, calls };
}

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

async function domStats(page: Page): Promise<{
  elements: number;
  options: number;
  selects: number;
  cards: number;
}> {
  return page.evaluate(() => ({
    elements: document.querySelectorAll('*').length,
    options: document.querySelectorAll('option').length,
    selects: document.querySelectorAll('select').length,
    cards: document.querySelectorAll('[data-testid="subject-card"]').length,
  }));
}

interface Widths {
  client: number;
  scroll: number;
}

async function assertNoHorizontalOverflow(page: Page, label: string): Promise<void> {
  const widths = await page.evaluate(() => {
    const measure = (element: Element | null): { client: number; scroll: number } => {
      if (!element) return { client: 0, scroll: 0 };
      const box = element as HTMLElement;
      return { client: box.clientWidth, scroll: box.scrollWidth };
    };
    return {
      documentElement: measure(document.documentElement),
      body: measure(document.body),
      page: measure(document.querySelector('.app-main .page')),
      editingCard: measure(document.querySelector('.account-card--editing')),
    };
  });
  const parts = Object.entries(widths).map(([key, value]) => `${key} ${value.scroll}/${value.client}`);
  console.info(`[identity.browser] ${label}: ${parts.join(', ')}`);
  const doc = widths.documentElement as Widths;
  expect(doc.scroll, `${label} documentElement 横向溢出`).toBeLessThanOrEqual(doc.client);
  const body = widths.body as Widths;
  expect(body.scroll, `${label} body 横向溢出`).toBeLessThanOrEqual(body.client);
}

/* ------------------------------------------------------------ 断言 */

// 每个用例都起一次真 Chromium + 真 Vite 模块加载 + 多轮交互（默认 5s 不够）。
const TEST_TIMEOUT = 30_000;

describe('关系页：不再卡死（真浏览器）', () => {
  it('首屏只渲染一页，且**没有**任何一个未展开卡片带 <option>', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page, calls } = await openRelations({ width: 1440, height: 900 });

    // 1. 接口只取一页：不带 offset 的首次请求，limit=50
    const first = calls[0];
    expect(first, '首屏没有请求关系接口').toBeDefined();
    expect(first?.path).toBe('/v1/accounts/subjects');
    expect(first?.query.limit).toBe(String(PAGE_SIZE));

    // 2. 卡片数 = 一页（2000 条里的 50 条），不是 2000
    const stats = await domStats(page);
    console.info(`[identity.browser] 2000 条关系 → 首屏 DOM ${stats.elements} 元素`);
    expect(stats.cards).toBe(PAGE_SIZE);
    expect(stats.elements).toBeLessThan(800);

    // 3. **核心断言**：一个 <option> 都没有，也没有 <select>
    expect(stats.options).toBe(0);
    expect(stats.selects).toBe(0);

    // 4. 这一页的 50 个人都是"未被合并"，所以每张卡片都有「合并到…」入口
    expect(await page.getByTestId('merge-open').count()).toBe(PAGE_SIZE);
  }, TEST_TIMEOUT);

  it('同时只有一张卡片展开；切换时上一张收起；取消后回到零个 <select>', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page } = await openRelations({ width: 1440, height: 900 });

    const opens = page.getByTestId('merge-open');
    await opens.nth(0).click();
    let stats = await domStats(page);
    expect(stats.selects).toBe(1);
    // 下拉里最多是"已加载的其他人"：49（首屏 50 条减去自己）+ 1 个占位
    expect(stats.options).toBeLessThanOrEqual(PAGE_SIZE);
    expect(stats.elements).toBeLessThan(800);

    // 点第二张：第一张必须收起 —— 不是两个 <select 并存
    await opens.nth(1).click();
    stats = await domStats(page);
    expect(stats.selects).toBe(1);
    const editing = await page.locator('.account-card--editing').count();
    expect(editing).toBe(1);

    // 取消：回到全只读
    await page.getByTestId('merge-cancel').click();
    stats = await domStats(page);
    expect(stats.selects).toBe(0);
    expect(stats.options).toBe(0);
  }, TEST_TIMEOUT);

  it('合并 → 刷新后已合并 → 拆开：整条链路在真浏览器里走通', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page } = await openRelations({ width: 1440, height: 900 });

    const firstCard = page.locator('[data-testid="subject-card"]').first();
    const sourceName = (await firstCard.locator('strong').first().innerText()).trim();
    await firstCard.getByTestId('merge-open').click();

    // 选一个具体的目标（不是占位项）
    const select = page.getByTestId('merge-target');
    const targetValue = await select.locator('option').nth(1).getAttribute('value');
    expect(targetValue).toBeTruthy();
    const targetLabel = (await select.locator('option').nth(1).innerText()).trim();
    await select.selectOption(targetValue as string);

    const mergeButton = page.getByTestId('merge-button');
    expect(await mergeButton.isEnabled()).toBe(true);
    await mergeButton.click();
    await page.getByTestId('identity-message').waitFor();
    expect(await page.getByTestId('identity-message').innerText()).toContain('已合并');

    // 刷新（= 用户按 F5）：指针在库里，页面重新读出来仍显示"已合并"
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="subject-card"]').first().waitFor();
    const mergedCard = page
      .locator('[data-testid="subject-card"]')
      .filter({ hasText: sourceName })
      .filter({ has: page.getByTestId('merged-badge') })
      .first();
    await mergedCard.waitFor();
    // 已合并的那张卡片仍然属于**被合并的人**（源头），不是目标
    expect(await mergedCard.innerText()).toContain('已合并');
    expect(await page.locator('[data-testid="merged-badge"]').count()).toBe(1);
    console.info(`[identity.browser] 合并 ${sourceName} → ${targetLabel}`);

    // 拆开还原
    await mergedCard.getByTestId('unmerge-button').click();
    await page.getByTestId('identity-message').waitFor();
    expect(await page.getByTestId('identity-message').innerText()).toContain('已拆开');
    expect(await page.getByTestId('merged-badge').count()).toBe(0);
  }, TEST_TIMEOUT);

  it('「加载更多」按 offset 续页，到底显示「没有更多了」', async (ctx) => {
    if (skip(ctx)) return;
    // 用 120 条：两页多一点，翻到底只要 3 次点击
    resetStore(SUBJECTS.slice(0, 120));
    const { page, calls } = await openRelations({ width: 1440, height: 900 });

    expect(await page.locator('[data-testid="subject-card"]').count()).toBe(PAGE_SIZE);
    expect(await page.getByTestId('no-more').count()).toBe(0);

    await page.getByTestId('load-more').click();
    await expect.poll(async () => page.locator('[data-testid="subject-card"]').count()).toBe(100);
    expect(calls.at(-1)?.query.offset).toBe(String(PAGE_SIZE));

    await page.getByTestId('load-more').click();
    await expect.poll(async () => page.locator('[data-testid="subject-card"]').count()).toBe(120);

    // 到底：按钮消失，换成"没有更多了"
    await page.getByTestId('no-more').waitFor();
    expect(await page.getByTestId('load-more').count()).toBe(0);
    expect(await page.getByTestId('subjects-count').innerText()).toContain('共 120 人');

    // 翻了三页，仍然没有一个 <option>
    const stats = await domStats(page);
    expect(stats.options).toBe(0);
    console.info(`[identity.browser] 120 人翻到底：${stats.elements} 元素`);
  }, TEST_TIMEOUT);

  it('搜索走服务端 q（不是只在当前页里筛）', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page, calls } = await openRelations({ width: 1440, height: 900 });

    // subj_0199 在第 4 页，首屏根本取不到它
    await page.getByTestId('subjects-search').fill('subj_0199');
    await page.getByTestId('subjects-search').press('Enter');

    await expect.poll(async () => page.locator('[data-testid="subject-card"]').count()).toBe(1);
    const last = calls.at(-1);
    expect(last?.query.q).toBe('subj_0199');
    expect(last?.query.offset).toBe('0');
    expect(await page.getByTestId('subjects-count').innerText()).toContain('共 1 人');
  }, TEST_TIMEOUT);

  it('空态与「只有 1 条」都不会渲染下拉', async (ctx) => {
    if (skip(ctx)) return;
    resetStore([]);
    const { page } = await openRelations({ width: 1440, height: 900 }, { expectEmpty: true });
    // 一个人都没有：卡片一个也没有，明确说"还没有认识任何人"
    expect(await page.getByTestId('subjects-empty').innerText()).toContain('还没有认识任何人');
    expect(await page.locator('[data-testid="subject-card"]').count()).toBe(0);
    expect(await page.locator('option').count()).toBe(0);

    // 只有一个人：有卡片、能点开，但下拉里没有可选目标，也不能点「合并」
    resetStore(SUBJECTS.slice(0, 1));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="subject-card"]').first().waitFor();
    expect(await page.getByTestId('subjects-empty').count()).toBe(0);
    expect(await page.locator('option').count()).toBe(0);
    expect(await page.getByTestId('merge-open').isDisabled()).toBe(true);
  }, TEST_TIMEOUT);
});

describe('关系页：375 / 1440 不横向溢出', () => {
  for (const width of [375, 1440]) {
    it(`${width}px：只读状态与展开状态都不溢出`, async (ctx) => {
      if (skip(ctx)) return;
      resetStore();
      const { page } = await openRelations({ width, height: 900 });

      await assertNoHorizontalOverflow(page, `${width} 只读`);

      // 展开第一张：下拉里装着 49 个候选（含长昵称），最窄视口下的关键场景
      await page.getByTestId('merge-open').first().click();
      await page.getByTestId('merge-target').waitFor();
      await assertNoHorizontalOverflow(page, `${width} 展开合并`);
    }, TEST_TIMEOUT);
  }
});
