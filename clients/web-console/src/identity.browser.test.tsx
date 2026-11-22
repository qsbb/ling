/**
 * 关系页（`/relationships`）的**真实浏览器**回归：卡死、合并、分页、窄屏。
 *
 * 背景（`shots/diag-relations-freeze-2026-11-09.md`）：用户反馈「点开关系页就卡死，
 * 刷新也刷不动」。根因不是接口慢（2000 条 16.8 ms），而是页面给**每张卡片**都渲染
 * 一个含全部候选的 `<select>` —— n 条关系 = n² 个 `<option>`，2000 条 = **4,023,210
 * 个 DOM 元素 / 289 MB**，SSR 就要 10.5 s，浏览器再建 400 万节点必卡死。
 *
 * 这个文件钉住两类性质，**必须在真浏览器里**量（SSR 量不到布局，也量不到
 * "点一下之后还剩多少个节点"）：
 *
 * A. 不卡死（原报告 `shots/diag-relations-freeze-2026-11-09.md` 的回归）：
 *    首屏只渲染一页；整页**没有** `<option>` / `<select>`；元素数有预算；
 *    翻页按 `offset` 续取、到底说「没有更多了」；375 / 1440 都不横向溢出。
 *
 * B. 图六的关系页（本文件后半部分）：
 *    1. **姓名是列表首要信息**，平台与账号数只做扫读；完整外部 ID 只在详情出现；
 *    2. **同名的人能用平台 + 账号区分**，不需要名字不同；
 *    3. 合并走**确认面板**，面板里同时看得到源、目标与双方账号；候选搜索
 *       **跨分页**（人不在首屏也搜得到）；
 *    4. 合并后**目标自然人能直接改名**；拆开还原；刷新后状态都还在。
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
  const page = rows.slice(offset, offset + limit).map((row) => ({
    ...row,
    can_rename: !row.merged_into,
    // 服务端字段：有多少身份并入这一行。成员可能不在本页，所以必须由服务端给。
    merged_member_count: store.filter((other) => other.merged_into === row.subject_id).length,
  }));
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
  allowDetails: () => void;
}

/**
 * 打开关系页并等到"首屏定下来"。
 *
 * 等的是**三种终态之一**（有卡片 / 空态提示），而不是固定等卡片：空库时
 * 一张卡片都不会有，`waitFor` 卡片会直接超时——那正是空态用例要覆盖的场景。
 */
async function openRelations(
  viewport: { width: number; height: number },
  options: { expectEmpty?: boolean; failDetail?: boolean; emptyInteractions?: boolean } = {},
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const calls: ApiCall[] = [];
  let failDetail = options.failDetail ?? false;

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
    if (/^\/v1\/accounts\/subjects\/[^/]+\/details$/.test(pathname) && method === 'GET') {
      calls.push({ path: pathname, query: Object.fromEntries(url.searchParams) });
      const subjectId = decodeURIComponent(pathname.split('/')[4] ?? '');
      const section = url.searchParams.get('section');
      if (failDetail && section === 'memories') {
        return route.fulfill(json({ error: { code: 'internal', message: '暂时无法读取记忆' } }, 503));
      }
      const isSelf = subjectId === 'subj_0000';
      const subject = store.find((row) => row.subject_id === subjectId);
      const cursor = url.searchParams.get('cursor');
      if (section === 'accounts') {
        const items = (subject?.accounts ?? []).map((account) => ({ ...account, subject_id: subjectId }));
        return route.fulfill(json({ section, access: 'full', items, next_cursor: null }));
      }
      if (section === 'interactions') {
        const items = isSelf && !options.emptyInteractions
          ? [{ event_id: 'event-a', kind: 'warmth', origin: 'user_statement', occurred_at: '2026-01-01T00:00:00Z' }]
          : [];
        return route.fulfill(json({ section, access: isSelf ? 'full' : 'restricted', items, next_cursor: null }));
      }
      const items = isSelf
        ? [{ memory_id: cursor ? 'memory-b' : 'memory-a', text: cursor ? '第二条记忆' : '第一条记忆', kind: 'assertion', visibility: 'subject_private', created_at: null, valid_until: null }]
        : [{ memory_id: 'memory-public', text: '第二个人的公开记忆', kind: 'assertion', visibility: 'public', created_at: null, valid_until: null }];
      return route.fulfill(json({ section, access: isSelf ? 'full' : 'public', items, next_cursor: isSelf && !cursor ? 'next' : null }));
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
    if (/^\/v1\/accounts\/subjects\/[^/]+$/.test(pathname) && method === 'PATCH') {
      const subjectId = decodeURIComponent(pathname.split('/')[4] ?? '');
      const subject = store.find((row) => row.subject_id === subjectId);
      if (!subject || subject.merged_into) {
        return route.fulfill(json({ error: { code: 'conflict', message: '请选择合并后的自然人' } }, 409));
      }
      const body = route.request().postDataJSON() as { display_name: string };
      subject.display_name = body.display_name;
      return route.fulfill(json({ subject_id: subjectId, display_name: subject.display_name }));
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
  return { page, calls, allowDetails: () => { failDetail = false; } };
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
  it('按需展开详情、独立翻页并切换不同人的内容', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page, calls } = await openRelations({ width: 1440, height: 900 });
    expect(calls.filter((call) => call.path.endsWith('/details'))).toHaveLength(0);

    await page.getByTestId('detail-open').first().click();
    const detail = page.getByTestId('subject-detail');
    await expect.poll(async () => detail.innerText()).toContain('第一条记忆');
    expect(await detail.innerText()).toContain('温暖的互动');
    expect(new Set(calls.filter((call) => call.path.endsWith('/details')).map((call) => call.query.section)))
      .toEqual(new Set(['accounts', 'interactions', 'memories']));
    await detail.getByRole('button', { name: '加载更多记忆' }).click();
    await expect.poll(async () => detail.innerText()).toContain('第二条记忆');
    expect(calls.at(-1)?.query.cursor).toBe('next');

    await page.getByTestId('detail-open').nth(1).click();
    await expect.poll(async () => detail.innerText()).toContain('第二个人的公开记忆');
    expect(await detail.innerText()).toContain('仅本人可查看关系事件');
    expect(await detail.innerText()).toContain('仅显示公开记忆');
    expect(await detail.innerText()).not.toContain('第一条记忆');
    expect(await page.getByTestId('subject-detail').count()).toBe(1);
  }, TEST_TIMEOUT);

  it('列表用「有未验证」扫读，详情按证明状态区分已证明 / 待验证 / 状态未知', async (ctx) => {
    if (skip(ctx)) return;
    const subject = SUBJECTS[0];
    const template = subject?.accounts[0];
    if (!subject || !template) throw new Error('缺少测试账号');
    const proofs = ['admin', 'proven', 'pending', 'unknown'];
    resetStore([{
      ...subject,
      accounts: proofs.map((proof) => ({
        ...template,
        account_id: `account-${proof}`,
        external_account_id: `account-${proof}`,
        proof,
      })),
    }]);
    const { page } = await openRelations({ width: 1440, height: 900 });
    const card = page.getByTestId('subject-card').first();

    // 列表层：只给一个"有未验证"的扫读徽标，不逐条铺 ID 与证明
    expect(await card.getByTestId('unverified-badge').count()).toBe(1);
    expect(await card.innerText()).not.toContain('account-pending');

    await card.getByTestId('detail-open').click();
    const detail = card.getByTestId('subject-detail');
    await expect.poll(async () => detail.getByLabel('关联账号').locator('.identity-detail__row').count()).toBe(4);

    const expected: Array<[string, string, string]> = [
      ['admin', '已证明', 'badge badge--ok'],
      ['proven', '已证明', 'badge badge--ok'],
      ['pending', '待验证', 'badge badge--warn'],
      ['unknown', '状态未知', 'badge badge--muted'],
    ];
    for (const [proof, label, cls] of expected) {
      const row = detail.getByLabel('关联账号').locator('.identity-detail__row').filter({ hasText: `account-${proof}` });
      expect(await row.innerText()).toContain(label);
      // 三档必须**在样式上**可分：同一个灰徽章等于把"未知"说成"还在等"。
      expect(await row.locator(`.${cls.replaceAll(' ', '.')}`).count()).toBe(1);
    }
    // 详情里给出"未验证意味着什么"，而不是只贴标签
    expect(await detail.innerText()).toContain('个账号还没有验证');
  }, TEST_TIMEOUT);

  it('没有关系事件时说明统计范围和空态', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page } = await openRelations({ width: 1440, height: 900 }, { emptyInteractions: true });
    await page.getByTestId('detail-open').first().click();
    const section = page.getByTestId('subject-detail').getByLabel('近期关系事件');
    await expect.poll(async () => section.innerText()).toContain('暂无可显示的关系事件');
    expect(await section.innerText()).toContain('可追溯到本人未撤回私聊的关系事件');
  }, TEST_TIMEOUT);

  it('详情某一部分失败时保留其他部分并可重试', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page, allowDetails } = await openRelations({ width: 1440, height: 900 }, { failDetail: true });
    await page.getByTestId('detail-open').first().click();
    const detail = page.getByTestId('subject-detail');
    await expect.poll(async () => detail.innerText()).toContain('服务端内部错误');
    expect(await detail.innerText()).toContain('温暖的互动');
    allowDetails();
    await detail.getByRole('button', { name: '重试' }).click();
    await expect.poll(async () => detail.innerText()).toContain('第一条记忆');
  }, TEST_TIMEOUT);

  for (const width of [375, 1440]) {
    it(`${width} 宽度下展开详情不会横向溢出`, async (ctx) => {
      if (skip(ctx)) return;
      resetStore();
      const { page } = await openRelations({ width, height: 900 });
      await page.getByTestId('detail-open').first().click();
      await expect.poll(async () => page.getByTestId('subject-detail').innerText()).toContain('第一条记忆');
      await assertNoHorizontalOverflow(page, `${width} 详情`);
    }, TEST_TIMEOUT);
  }

  it('首屏只渲染一页，整页没有 <option> / <select>，元素数在预算内', async (ctx) => {
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
    /*
     * 预算按**这一版**的行结构重算，不是从旧数字抄来的。
     *
     * 每行新增的平台徽标（平台名 + 计数各一个元素）与三个按钮，
     * 都是图六要求的信息（平台扫读、操作目标可见）——预算要跟着需求走，
     * 但不能取消：它防的是"每条候选都渲染进首屏"这一类回归。
     */
    expect(stats.elements).toBeLessThan(1400);

    // 3. **核心断言**：一个 <option> / <select> 都没有
    expect(stats.options).toBe(0);
    expect(stats.selects).toBe(0);

    // 4. 这一页的人都有「合并…」入口（是否可点由"有没有别人"决定，不再是预禁用）
    expect(await page.getByTestId('merge-open').count()).toBe(PAGE_SIZE);
  }, TEST_TIMEOUT);

  it('合并面板一次只开一个；取消后回到零个弹层，并回到原记录', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page } = await openRelations({ width: 1440, height: 900 });

    const opens = page.getByTestId('merge-open');
    await opens.nth(0).click();
    await page.getByTestId('merge-confirm').waitFor();
    expect(await page.getByTestId('merge-confirm').count()).toBe(1);

    // 关掉第一张，换第二个人：仍然只有一个面板，而且标题里是第二个人
    await page.getByTestId('merge-confirm').getByRole('button', { name: '取消' }).click();
    await expect.poll(async () => page.getByTestId('merge-confirm').count()).toBe(0);
    await opens.nth(1).click();
    await page.getByTestId('merge-confirm').waitFor();
    expect(await page.getByTestId('merge-confirm').count()).toBe(1);

    await page.getByTestId('merge-confirm').getByRole('button', { name: '取消' }).click();
    await expect.poll(async () => page.getByTestId('merge-confirm').count()).toBe(0);

    // 取消不改数据：没有任何卡片被并进去
    expect(await page.getByTestId('merged-badge').count()).toBe(0);
    // 取消后仍能定位到原记录（它在页面上且可见）
    const second = page.getByTestId('subject-card').nth(1);
    expect(await second.isVisible()).toBe(true);
  }, TEST_TIMEOUT);

  it('合并确认同时展示源、目标与双方账号，合并后目标可直接改名，拆开还原', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page } = await openRelations({ width: 1440, height: 900 });

    const sourceCard = page.getByTestId('subject-card').first();
    const sourceName = (await sourceCard.getByTestId('person-name').innerText()).trim();
    await sourceCard.getByTestId('merge-open').click();

    const dialog = page.getByTestId('merge-confirm');
    await dialog.waitFor();

    // 源在面板里就是刚才点的那个人
    expect(await dialog.getByTestId('merge-confirm-source').innerText()).toContain(sourceName);

    // 选目标：面板里的候选是按钮，不是 <select>
    const targetOption = dialog.getByTestId('merge-candidate').nth(1);
    const targetName = (await targetOption.locator('.merge-candidates__name').innerText()).trim();
    await targetOption.click();

    // 目标一侧接着就是它的账号：确认依据是账号，不是名字
    const targetParty = dialog.getByTestId('merge-confirm-target');
    await expect.poll(async () => targetParty.innerText()).toContain(targetName);
    const targetIds = await targetParty.locator('.merge-dialog__id').allInnerTexts();
    expect(targetIds.length).toBeGreaterThan(0);
    // 确认面板里是**完整**外部 ID（列表上的缩写在这里不够核对）
    for (const id of targetIds) expect(id).toMatch(/^\d+$/);

    await dialog.getByTestId('merge-confirm-ok').click();
    await page.getByTestId('identity-message').waitFor();
    expect(await page.getByTestId('identity-message').innerText()).toContain('已把');

    // 刷新（= 用户按 F5）：指针在库里，页面重新读出来仍是同一个人
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="subject-card"]').first().waitFor();
    const mergedTarget = page
      .getByTestId('subject-card')
      .filter({ hasText: targetName })
      .first();
    await mergedTarget.waitFor();
    /*
     * 合并后：目标那一行显示「已并入 N 个号」（数量来自服务端），
     * 被并入的记录在「已并入」那一段里自己占一行、写着并入了谁。
     *
     * 不把成员折进目标行是有意的：成员可能在别的页，折进去会让"整页浏览"
     * 与"搜索只命中目标"两种情形显示成两样。
     */
    expect(await mergedTarget.getByTestId('merged-members-badge').count()).toBe(1);
    const mergedSection = page.getByLabel('已并入的身份');
    await mergedSection.waitFor();
    const mergedRow = mergedSection.getByTestId('subject-card').first();
    expect(await mergedRow.getByTestId('merged-badge').innerText()).toContain(targetName);
    expect(await mergedRow.innerText()).toContain(sourceName);
    expect(await mergedRow.getByTestId('unmerge-button').count()).toBe(1);

    // 合并后的目标自然人可以直接改名
    await mergedTarget.getByTestId('rename-open').click();
    const renameDialog = page.getByTestId('rename-dialog');
    await renameDialog.waitFor();
    await renameDialog.getByTestId('rename-input').fill('两个账号的共同姓名');
    await renameDialog.getByTestId('rename-save').click();
    // 改名后：目标行（独立的人）出现新姓名。用 person-list 限定，避免命中
    // 「已并入」那一段里写着同一目标姓名的成员行。
    const renamedTarget = page.getByTestId('person-list')
      .getByTestId('subject-card')
      .filter({ hasText: '两个账号的共同姓名' })
      .first();
    await renamedTarget.waitFor();
    expect(await renamedTarget.getByTestId('person-name').innerText()).toBe('两个账号的共同姓名');

    // 拆开还原：源从「已并入」那一段消失，重新成为独立的人
    await mergedRow.getByTestId('unmerge-button').click();
    await page.getByTestId('identity-message').waitFor();
    expect(await page.getByTestId('identity-message').innerText()).toContain('已拆开');
    await expect.poll(async () => page.getByTestId('merged-badge').count()).toBe(0);
    await page.getByTestId('person-list').getByTestId('subject-card')
      .filter({ hasText: sourceName }).first().waitFor();
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
    expect(await page.getByTestId('subjects-count').innerText()).toContain('120 人');

    const stats = await domStats(page);
    expect(stats.options).toBe(0);
    console.info(`[identity.browser] 120 人翻到底：${stats.elements} 元素`);
  }, TEST_TIMEOUT);

  it('搜索走服务端 q，能搜到首屏没加载的人', async (ctx) => {
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
    expect(await page.getByTestId('subjects-count').innerText()).toContain('1 人');
    // 搜到的是那个人本身，而不是"当前页里碰巧含这几个字的"
    expect(await page.locator('[data-testid="subject-card"]').first().getAttribute('data-subject-id'))
      .toBe('subj_0199');
  }, TEST_TIMEOUT);

  it('合并候选跨分页：目标不在首屏，也能搜到并选上', async (ctx) => {
    if (skip(ctx)) return;
    resetStore();
    const { page, calls } = await openRelations({ width: 1440, height: 900 });

    await page.getByTestId('merge-open').first().click();
    const dialog = page.getByTestId('merge-confirm');
    await dialog.waitFor();

    // 首屏候选里不该有第 4 页的人
    const beforeIds = await dialog.getByTestId('merge-candidate').count();
    expect(beforeIds).toBeLessThanOrEqual(PAGE_SIZE);

    await dialog.getByTestId('merge-candidate-search').fill('subj_0199');
    await expect.poll(async () => dialog.getByTestId('merge-candidate').count()).toBe(1);

    // 这一次搜索是**跨全部人**的服务端查询，不是本地过滤
    const searchCalls = calls.filter((call) => call.path === '/v1/accounts/subjects' && call.query.q === 'subj_0199');
    expect(searchCalls.length).toBeGreaterThan(0);
    expect(await dialog.getByTestId('merge-candidates-count').innerText()).toContain('共 1 人');

    await dialog.getByTestId('merge-candidate').first().click();
    // 目标一侧显示**姓名 + 账号**（subj_0199 是第 200 个人：30000000 + 199*10 = 30001990）
    const targetPartyText = async () => dialog.getByTestId('merge-confirm-target').innerText();
    await expect.poll(targetPartyText).toContain('诊断用户0199');
    expect(await targetPartyText()).toContain('30001990');
    expect(await dialog.getByTestId('merge-confirm-ok').isEnabled()).toBe(true);
  }, TEST_TIMEOUT);

  it('同名的人用平台与账号区分，不靠名字', async (ctx) => {
    if (skip(ctx)) return;
    const template = SUBJECTS[0];
    if (!template) throw new Error('缺少测试数据');
    // 两个人同名「小明」，挂在不同平台的不同账号上
    const twins = [
      {
        ...template,
        subject_id: 'twin_a',
        display_name: '小明',
        accounts: [{
          account_id: 'twin_a_1',
          connector_instance: 'napcat-2',
          external_account_id: '11000001',
          proof: 'proven',
          revoked: false,
        }],
      },
      {
        ...template,
        subject_id: 'twin_b',
        display_name: '小明',
        accounts: [{
          account_id: 'twin_b_1',
          connector_instance: 'onebot-default',
          external_account_id: '22000002',
          proof: 'pending',
          revoked: false,
        }],
      },
    ];
    resetStore(twins);
    const { page } = await openRelations({ width: 1440, height: 900 });

    const cards = page.getByTestId('subject-card');
    expect(await cards.count()).toBe(2);
    // 两个名字一样
    expect(await cards.nth(0).getByTestId('person-name').innerText()).toBe('小明');
    expect(await cards.nth(1).getByTestId('person-name').innerText()).toBe('小明');

    // 但扫读信息不同：平台徽标 + 同名区分提示
    const firstText = await cards.nth(0).getByTestId('person-platforms').innerText();
    const secondText = await cards.nth(1).getByTestId('person-platforms').innerText();
    expect(firstText).toContain('napcat');
    expect(secondText).toContain('onebot');
    expect(await cards.nth(0).getByTestId('same-name-hint').innerText()).toContain('…0001');
    expect(await cards.nth(1).getByTestId('same-name-hint').innerText()).toContain('…0002');

    // 合并确认里也一样能分开
    await cards.nth(0).getByTestId('merge-open').click();
    const dialog = page.getByTestId('merge-confirm');
    await dialog.waitFor();
    const option = dialog.getByTestId('merge-candidate').first();
    expect(await option.innerText()).toContain('onebot');
    await option.click();
    expect(await dialog.getByTestId('merge-confirm-target').innerText()).toContain('22000002');
  }, TEST_TIMEOUT);

  it('搜索只命中被并入的记录时，说"目标不在结果里"而不是"找不到目标"', async (ctx) => {
    if (skip(ctx)) return;
    /*
     * 真实里会遇到：合并之后按源的账号搜索，结果里只有源自己——目标在不同
     * 页、或没被这次搜索命中。此时页面**不能**说"找不到目标"（目标存在），
     * 也不能把它悄悄折进某个看着像的行。它要如实说明并保留拆开入口。
     */
    const template = SUBJECTS[0];
    if (!template) throw new Error('缺少测试数据');
    resetStore([
      {
        ...template,
        subject_id: 'target_x',
        display_name: '目标本人',
        merged_into: null,
        accounts: [{
          account_id: 'target_x_1',
          connector_instance: 'napcat-2',
          external_account_id: '77000001',
          proof: 'proven',
          revoked: false,
        }],
      },
      {
        ...template,
        subject_id: 'source_x',
        display_name: '被并入的人',
        // 目标在一次搜索里不一定被返回；这里模拟"服务端只给了源"
        merged_into: 'target_x',
        accounts: [{
          account_id: 'source_x_1',
          connector_instance: 'onebot-default',
          external_account_id: '88000002',
          proof: 'pending',
          revoked: false,
        }],
      },
    ]);
    const { page } = await openRelations({ width: 1440, height: 900 });

    // 搜索只命中源（昵称只在源上）
    await page.getByTestId('subjects-search').fill('被并入的人');
    await page.getByTestId('subjects-search').press('Enter');
    await expect.poll(async () => page.getByTestId('subject-card').count()).toBe(1);

    const orphanSection = page.getByLabel('目标不在本次结果中的并入记录');
    await orphanSection.waitFor();
    const text = await orphanSection.innerText();
    expect(text).toContain('目标这次没有出现在结果里');
    // 不能出现"找不到目标"这种把事情说死的判断
    expect(text).not.toContain('找不到目标');
    // 并入对象仍要照实写出来（id 是核对依据）
    expect(text).toContain('target_x');
    // 拆开与详情都可用：不能因为"没有目标行"就失去操作
    const row = orphanSection.getByTestId('subject-card').first();
    expect(await row.getByTestId('unmerge-button').count()).toBe(1);
    expect(await row.getByTestId('detail-open').count()).toBe(1);
    expect(await row.getAttribute('data-subject-id')).toBe('source_x');
  }, TEST_TIMEOUT);

  it('空态与「只有 1 条」都不渲染下拉，且只有 1 人时合并面板仍可搜索', async (ctx) => {
    if (skip(ctx)) return;
    resetStore([]);
    const { page } = await openRelations({ width: 1440, height: 900 }, { expectEmpty: true });
    expect(await page.getByTestId('subjects-empty').innerText()).toContain('还没有认识任何人');
    expect(await page.locator('[data-testid="subject-card"]').count()).toBe(0);
    expect(await page.locator('option').count()).toBe(0);

    // 只有一个人：能打开合并面板，里面明确说已加载的人里没有目标
    resetStore(SUBJECTS.slice(0, 1));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="subject-card"]').first().waitFor();
    expect(await page.getByTestId('subjects-empty').count()).toBe(0);
    expect(await page.locator('option').count()).toBe(0);
    await page.getByTestId('merge-open').click();
    const dialog = page.getByTestId('merge-confirm');
    await dialog.waitFor();
    expect(await dialog.getByTestId('merge-candidates-empty').count()).toBe(1);
    // 没有目标就不能提交
    expect(await dialog.getByTestId('merge-confirm-ok').isDisabled()).toBe(true);
  }, TEST_TIMEOUT);
});

describe('关系页：375 / 1440 不横向溢出', () => {
  for (const width of [375, 1440]) {
    it(`${width}px：只读状态与展开状态都不溢出`, async (ctx) => {
      if (skip(ctx)) return;
      resetStore();
      const { page } = await openRelations({ width, height: 900 });

      await assertNoHorizontalOverflow(page, `${width} 只读`);
      // 列表不能被父级 flex 压缩后用 overflow:hidden 裁掉后续行。
      // scrollIntoView 会滚动 hidden 容器，单独做命中测试检测不到这种裁切。
      const listSizes = await page.locator('.identity-list').evaluateAll((lists) =>
        lists.map((list) => ({ visible: list.clientHeight, content: list.scrollHeight })),
      );
      expect(listSizes.length).toBeGreaterThan(0);
      for (const size of listSizes) {
        expect(size.content, `${width}px 人员列表不能被纵向裁切`).toBeLessThanOrEqual(size.visible + 1);
      }
      const lastCard = page.getByTestId('subject-card').last();
      await lastCard.scrollIntoViewIfNeeded();
      const reachable = await lastCard.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        const hit = document.elementFromPoint(centerX, centerY);
        return hit !== null && element.contains(hit);
      });
      expect(reachable).toBe(true);


      // 打开第一张的合并面板：候选是 49 个按钮（含长昵称）+
      // 两块"源/目标 + 完整账号 ID"——最窄视口下最容易撑破的组合。
      await page.getByTestId('merge-open').first().click();
      const dialog = page.getByTestId('merge-confirm');
      await dialog.waitFor();
      await dialog.getByTestId('merge-candidate').first().click();
      await expect.poll(async () => dialog.getByTestId('merge-confirm-target').innerText()).toContain('保留的一方');
      await assertNoHorizontalOverflow(page, `${width} 展开合并`);

      // 面板本身也不能横向溢出（它是独立于 .page 的顶层元素）
      const dialogWidths = await dialog.evaluate((element) => ({
        client: element.clientWidth,
        scroll: element.scrollWidth,
      }));
      console.info(`[identity.browser] ${width} 合并面板: ${dialogWidths.scroll}/${dialogWidths.client}`);
      expect(dialogWidths.scroll, `${width}px 合并面板横向溢出`).toBeLessThanOrEqual(dialogWidths.client + 1);
    }, TEST_TIMEOUT);
  }
});
