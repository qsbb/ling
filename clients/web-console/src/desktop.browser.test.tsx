/**
 * Windows 桌面任务页的**真实浏览器**验证（真 Vite 开发服务器 + 真 Chromium）。
 *
 * 为什么需要这一层：SSR 能证明"画了什么"，但证明不了这一页真正的三个失败面——
 *
 * 1. **排版**：403 字符数最多的那一段提示在 390 宽下会不会把卡片撑破；
 *    令牌是 43 个不换行的字符，最容易横向溢出。
 * 2. **交互真的接上了**：点「提交」发的 `request_id` 是不是 UUID v4、
 *    第二次失败重试时**还是同一个**编号（这条只能在真实浏览器里看请求）。
 * 3. **离开页面**：轮询必须停，且不再有迟到请求把状态写回来。
 *
 * `/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库）；形状取自
 * `contracts/desktop.py`。没有浏览器时标 skipped，不静默 passed。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page, type Request } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const NOW = '2026-11-24T01:00:00Z';
const TIMEOUT = 60_000;
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

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
    if (!address || typeof address === 'string') throw new Error('Vite 没有给出可用的监听地址');
    baseUrl = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch();
  } catch (error) {
    launchError = error instanceof Error ? error.message : String(error);
  }
}, 120_000);

afterEach(async () => {
  while (contexts.length > 0) await contexts.pop()?.close();
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

function skip(ctx: { skip: () => void }): boolean {
  if (!launchError) return false;
  console.warn(`[desktop.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* -------------------------------------------------------------- 载荷 */

const USER = {
  id: 'user-owner',
  username: 'ling',
  role: 'owner',
  subject_id: 'subject-owner',
  display_name: 'Ling',
  must_change_password: false,
};

function task(overrides: Record<string, unknown> = {}) {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    kind: 'notepad_write',
    text: '第一行\n  两个空格  😀',
    state: 'queued',
    step: null,
    reason: null,
    created_at: NOW,
    started_at: null,
    finished_at: null,
    source: 'manual',
    source_segment_id: null,
    ...overrides,
  };
}

interface Scenario {
  status?: Record<string, unknown>;
  grant?: Record<string, unknown> | null;
  /** 第一次 POST /tasks 返回什么（用来造"送到一半断了"）。 */
  submitResponses?: Array<{ status: number; body: unknown }>;
}

function statusBody(overrides: Record<string, unknown> = {}) {
  return {
    device: { name: '客厅 Windows', state: 'ready', last_seen_at: NOW },
    paused: false,
    tasks: [],
    ...overrides,
  };
}

function json(body: unknown, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

interface Harness {
  page: Page;
  requests: Request[];
  pollCount: () => number;
}

async function openDesktop(
  viewport: { width: number; height: number },
  scenario: Scenario = {},
  users: typeof USER = USER,
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const requests: Request[] = [];
  let polls = 0;
  let submitIndex = 0;
  let tasks = (scenario.status?.tasks as unknown[] | undefined) ?? [];

  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/v1/desktop')) requests.push(request);
  });

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/v1/')) return route.continue();
    const method = route.request().method();

    if (path === '/v1/auth/me') return route.fulfill(json({ schema_version: 'v1', user: users, config_revision: 1, test_mode: false, csrf_token: 'csrf' }));
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.15' }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [] }));

    if (path === '/v1/desktop' && method === 'GET') {
      polls += 1;
      return route.fulfill(json(statusBody({ ...scenario.status, tasks })));
    }
    if (path === '/v1/desktop/autonomy' && method === 'GET') {
      return route.fulfill(json({ grant: scenario.grant ?? null }));
    }
    if (path === '/v1/desktop/tasks' && method === 'POST') {
      const planned = scenario.submitResponses?.[submitIndex];
      submitIndex += 1;
      if (planned && planned.status >= 400) {
        return route.fulfill(json(planned.body, planned.status));
      }
      const body = route.request().postDataJSON() as { request_id: string; text: string };
      const created = task({ id: body.request_id, text: body.text });
      tasks = [created, ...tasks];
      return route.fulfill(json(created, 201));
    }
    if (path === '/v1/desktop/pair' && method === 'PUT') {
      // 43 字符的 base64url，与后端 `new_token()` 同形：这是页面上最长的不可断行内容。
      return route.fulfill(
        json({ token: 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEFG' }),
      );
    }
    const cancelMatch = /^\/v1\/desktop\/tasks\/([^/]+)\/cancel$/.exec(path);
    if (cancelMatch && method === 'POST') {
      const id = decodeURIComponent(cancelMatch[1]!);
      tasks = tasks.map((item) => {
        const row = item as Record<string, unknown>;
        // 只有还在途的才变成 cancelled；已结束的原样返回（与后端一致）。
        if (row.id === id && (row.state === 'queued' || row.state === 'running')) {
          return { ...row, state: 'cancelled', reason: 'owner_cancelled', finished_at: NOW };
        }
        return row;
      });
      return route.fulfill(json(tasks.find((item) => (item as Record<string, unknown>).id === id)));
    }
    if (path === '/v1/desktop/control' && method === 'PUT') {
      const body = route.request().postDataJSON() as { paused: boolean };
      return route.fulfill(json(statusBody({ ...scenario.status, paused: body.paused, tasks })));
    }
    return route.fulfill(json({ error: { code: 'not_found', message: path, retriable: false, detail: null } }, 404));
  });

  await page.goto(`${baseUrl}/desktop`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="desktop-page"], [data-testid="desktop-forbidden"]').first().waitFor({ timeout: 20_000 });
  // 等样式真的注入：否则量到的是裸 DOM 的宽度，"没溢出"是假结论。
  await page.waitForFunction(() => document.styleSheets.length > 3, undefined, { timeout: 20_000 });
  await page.waitForTimeout(250);
  return { page, requests, pollCount: () => polls };
}

async function overflowOf(page: Page): Promise<{ doc: number; offenders: string[] }> {
  return page.evaluate(() => {
    const doc = document.documentElement.scrollWidth - window.innerWidth;
    const offenders: string[] = [];
    for (const element of Array.from(document.querySelectorAll('body *'))) {
      const rect = element.getBoundingClientRect();
      if (rect.width === 0) continue;
      if (rect.right > window.innerWidth + 1) {
        offenders.push(`${element.tagName.toLowerCase()}.${element.className}`.slice(0, 80));
      }
    }
    return { doc, offenders: offenders.slice(0, 5) };
  });
}

/* -------------------------------------------------------------- 用例 */

describe('Windows 桌面任务（真实浏览器）', () => {
  it('1440 与 390 都不横向溢出；令牌那串 43 个不换行字符不会撑破卡片', async (ctx) => {
    if (skip(ctx)) return;
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const { page } = await openDesktop(viewport, {
        // 从未配对开始：这时页面才有名字输入框（已配对时它藏在「重新配对」后面）。
        status: { device: null, paused: true, tasks: [] },
      });
      // 配对，拿到令牌面板（最长的不可断行内容就在这里）
      await page.fill('[data-testid="desktop-name"]', '客厅 Windows');
      await page.click('[data-testid="desktop-pair-submit"]');
      await page.locator('[data-testid="desktop-token-value"]').waitFor({ timeout: TIMEOUT });
      const overflow = await overflowOf(page);
      expect(overflow.doc, `${viewport.width}px 横向溢出：${overflow.offenders.join(', ')}`).toBeLessThanOrEqual(1);
    }
  }, TIMEOUT);

  it('在途时提交被禁用并给出明确反馈；成功后输入框清空', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDesktop({ width: 1440, height: 1000 }, {
      status: { tasks: [task({ state: 'running', step: 'opened' })] },
    });
    expect(await page.locator('[data-testid="desktop-submit-button"]').isDisabled()).toBe(true);
    expect(await page.locator('[data-testid="desktop-submit-blocked"]').innerText()).toContain('上一条还在等待或执行');
    // 在途任务有"取消这条任务"
    expect(await page.locator('[data-testid="desktop-cancel-active"]').count()).toBe(1);
  }, TIMEOUT);

  it('提交用 UUID v4；失败后用**同一个**编号重试（新提交才换号）', async (ctx) => {
    if (skip(ctx)) return;
    const { page, requests } = await openDesktop({ width: 1440, height: 1000 }, {
      // 第一次 500（比如网关断了），第二次成功
      submitResponses: [
        { status: 500, body: { error: { code: 'internal', message: '网关错误', retriable: true, detail: null } } },
      ],
    });
    const posts = () => requests.filter((r) => r.method() === 'POST' && new URL(r.url()).pathname === '/v1/desktop/tasks');
    await page.fill('[data-testid="desktop-note"]', '第一次会失败的一段 😀');
    await page.click('[data-testid="desktop-submit-button"]');
    await page.locator('[data-testid="desktop-submit-retry"]').waitFor({ timeout: 15000 });
    const first = posts()[0]!.postDataJSON() as { request_id: string; text: string };
    expect(first.request_id).toMatch(UUID4);
    expect(first.text, '前端不能 trim / 折叠空格').toBe('第一次会失败的一段 😀');

    // 重试：同一个编号、同一段文字
    await page.click('[data-testid="desktop-submit-retry"] button');
    await page.waitForFunction(() => document.querySelectorAll('[data-testid="desktop-task"]').length > 0, undefined, { timeout: 15000 });
    const all = posts().map((r) => r.postDataJSON() as { request_id: string; text: string });
    expect(all).toHaveLength(2);
    expect(all[1]!.request_id, '重试沿用同一个编号（后端靠它幂等）').toBe(all[0]!.request_id);
    expect(all[1]!.text).toBe(all[0]!.text);

    // 成功之后输入框清空，任务出现在列表里
    await expect(page.locator('[data-testid="desktop-note"]').inputValue()).resolves.toBe('');

    // 新提交 = 新编号。先取消上一条：这个模拟里任务一直是 queued，
    // 而"一个任务在途时其它提交被挡住"是产品的正确行为（另一条用例在验它）。
    await page.click('[data-testid="desktop-cancel-active"]');
    await page.waitForFunction(
      () => !(document.querySelector('[data-testid="desktop-submit-button"]') as HTMLButtonElement | null)?.disabled,
      undefined,
      { timeout: TIMEOUT },
    );
    await page.fill('[data-testid="desktop-note"]', '第二段');
    await page.click('[data-testid="desktop-submit-button"]');
    await page.waitForFunction(() => true);
    await page.waitForTimeout(300);
    const third = posts()[2]?.postDataJSON() as { request_id: string } | undefined;
    expect(third?.request_id).toMatch(UUID4);
    expect(third?.request_id).not.toBe(all[0]!.request_id);
  }, TIMEOUT);

  it('离开页面后轮询停止：不再有 /v1/desktop 请求', async (ctx) => {
    if (skip(ctx)) return;
    const { page, pollCount } = await openDesktop({ width: 1440, height: 1000 });
    // 至少拍了两拍（进页面一拍 + 2 秒后一拍）
    await page.waitForTimeout(2500);
    expect(pollCount()).toBeGreaterThanOrEqual(2);

    await page.goto(`${baseUrl}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);
    const afterLeaving = pollCount();
    await page.waitForTimeout(3000);
    expect(pollCount(), '离开页面后还有轮询在跑').toBe(afterLeaving);
  }, TIMEOUT);

  it('成员看到的是明确的"只对所有者开放"，不是 404', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDesktop({ width: 1440, height: 900 }, {}, { ...USER, role: 'member' });
    const forbidden = page.locator('[data-testid="desktop-forbidden"]');
    await forbidden.waitFor({ timeout: TIMEOUT });
    expect(await forbidden.innerText()).toContain('只对所有者开放');
    expect(await page.locator('[data-testid="unknown-route"]').count(), '权限不足不该显示成"地址不存在"').toBe(0);
    // 成员导航里没有「桌面」入口
    expect(await page.locator('[data-testid="nav-desktop"]').count()).toBe(0);
  }, TIMEOUT);

  it('授权 pending 时，任务列表与授权结果分得开（queued ≠ 已写入）', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDesktop({ width: 1440, height: 1200 }, {
      status: {
        tasks: [task({ id: '22222222-2222-4222-8222-222222222222', state: 'queued', source: 'life' })],
      },
      grant: {
        request_id: '33333333-3333-4333-8333-333333333333',
        text: '今天想写三行',
        start_at: '2026-11-24T01:00:00Z',
        end_at: '2026-11-24T02:00:00Z',
        decision: 'queued',
        task_id: '22222222-2222-4222-8222-222222222222',
      },
    });
    expect(await page.locator('[data-testid="desktop-grant-decision"]').innerText()).toBe('已入队，等伴生端领取');
    expect(await page.locator('[data-testid="desktop-grant-current"]').innerText()).toContain('不等于');
    // 同一张屏幕里，任务那一条说的是"等待伴生端领取"，不是"已写入"
    const taskText = await page.locator('[data-testid="desktop-task-outcome"]').first().innerText();
    expect(taskText).toContain('等待伴生端领取');
    expect(taskText).not.toContain('已写入');
    // 它标着「来自授权」，能对上号。在途任务会同时出现在「这一步」与「最近任务」：
    // 后者是紧凑复现（只留状态与去向），所以这里断"至少一处且每处都带标记"。
    const tags = await page.locator('[data-testid="desktop-task-source-life"]').count();
    const rows = await page.locator('[data-testid="desktop-task"]').count();
    expect(tags).toBeGreaterThanOrEqual(1);
    expect(tags).toBe(rows);
  }, TIMEOUT);

  it('窄屏 390：主要操作可达，且按钮不小于 40px', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDesktop({ width: 390, height: 844 });
    const box = await page.locator('[data-testid="desktop-submit-button"]').boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(40);
    // 暂停开关也在首屏可达
    expect(await page.locator('[data-testid="desktop-pause"]').isVisible()).toBe(true);
    const overflow = await overflowOf(page);
    expect(overflow.doc).toBeLessThanOrEqual(1);
  }, TIMEOUT);
});
