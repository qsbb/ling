/**
 * 外部插件页（`/settings/plugins`）的真浏览器测试。
 *
 * 为什么必须开真浏览器：这一页的三件事都只有浏览器里才看得见——
 *
 * 1. **布局**：一行里塞了名字、状态、四个能力格和四个按钮，窄屏必须折成竖排
 *    且不横向溢出。SSR 全绿也看不出 CSS 没生效或断点写错（`settings-layout.browser`
 *    当年就是这么发现「授权页根本没引样式表」的）。
 * 2. **失败后能不能看见**：失败原因块跟着那一行走，紧贴操作按钮。
 * 3. **浏览器存储**：这一页不能把令牌写进 `localStorage`/`sessionStorage`。
 *    SSR 测不到，只有真浏览器能查。
 *
 * 断言一律用**仓库现有的 `expect.poll` + `textContent()`**（`connectors.browser`
 * 的做法）：项目没装 `@playwright/test`，`toHaveText`/`toBeVisible` 这些断言
 * 在这里不存在。轮询式断言还顺带解决了"点了之后等 React 重渲染"的时序问题——
 * 它等的是**结果**，不是固定的 sleep。
 *
 * `/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库）。真实后端联调在
 * `scripts/probe_external_plugins_api.py`（已跑过，报告在验收文档里）。
 * 没有浏览器时标 skipped，不静默 passed。
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
  console.warn(`[external-plugins.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/** 等某个 testid 的文字满足条件；超时给一句能定位的报错。 */
async function pollText(
  page: Page,
  testId: string,
  predicate: (text: string) => boolean,
  label: string,
): Promise<void> {
  await expect
    .poll(
      async () => (await page.getByTestId(testId).textContent()) ?? '',
      { timeout: 15_000, message: `等待 ${label}（testid=${testId}）` },
    )
    .toSatisfy(predicate);
}

const has = (needle: string) => (text: string) => text.includes(needle);

/* ------------------------------------------------------------ 模拟后端 */

type Row = {
  id: string;
  name: string;
  version: string;
  api_version: string;
  min_host_version: string;
  max_host_version: string;
  capabilities: string[];
  events: string[];
  tools: string[];
  commands: string[];
  granted_capabilities: string[];
  enabled: boolean;
  status: string;
  last_error: string | null;
  last_checked_at: string | null;
};

function row(overrides: Partial<Row> = {}): Row {
  return {
    id: 'example.notes',
    name: '隔离测试插件',
    version: '1.2.0',
    api_version: 'ling-plugin/1',
    min_host_version: '0.0.15',
    max_host_version: '0.0.15',
    capabilities: ['tools', 'commands', 'events', 'host.state'],
    events: ['turn.completed'],
    tools: ['lookup'],
    commands: ['lookup'],
    granted_capabilities: ['tools', 'commands'],
    enabled: false,
    status: 'ready',
    last_error: null,
    last_checked_at: '2026-11-23T15:40:05.766147+00:00',
    ...overrides,
  };
}

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/**
 * 一个可变的模拟后端。
 *
 * 刻意**在服务端记请求体**：断言要证明"页面发的是契约里的键"，
 * 只看界面文字证明不了——服务端 `extra=forbid`，多一个键就是 422。
 */
interface FakeBackend {
  rows: Row[];
  requests: Array<{ method: string; path: string; body: unknown }>;
  /** 登记时返回这个错误（模拟 422）。`null` = 走成功路径。 */
  registerError: { code: string; message: string; trace: string } | null;
  /** 同步时模拟"标识变了"：先把记录改成 identity_mismatch，再回 422。 */
  syncMismatch: boolean;
  /** 403 模拟（成员账号）。 */
  forbidden: boolean;
}

function fakeBackend(): FakeBackend {
  return { rows: [], requests: [], registerError: null, syncMismatch: false, forbidden: false };
}

async function openPage(
  state: FakeBackend,
  viewport: { width: number; height: number },
): Promise<Page> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();

  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (!path.startsWith('/v1/')) return route.continue();

    const body = request.postData() ? JSON.parse(request.postData() ?? '{}') : null;
    if (method !== 'GET') state.requests.push({ method, path, body });

    if (path === '/v1/auth/me') {
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
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.15' }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [] }));

    if (path === '/v1/plugins' && method === 'GET') {
      if (state.forbidden) {
        return route.fulfill(
          json({ error: { code: 'forbidden', message: '外部插件设置仅管理员可访问' } }, 403),
        );
      }
      return route.fulfill(json({ plugins: state.rows, legacy: [] }));
    }

    if (path === '/v1/plugins/register' && method === 'POST') {
      if (state.registerError) {
        return route.fulfill(
          json(
            {
              schema_version: 'v1',
              error: {
                code: state.registerError.code,
                message: state.registerError.message,
                retriable: false,
                detail: null,
                trace_id: state.registerError.trace,
                field: null,
              },
            },
            422,
          ),
        );
      }
      const created = row({
        id: (body as { expected_id?: string } | null)?.expected_id ?? 'example.notes',
        enabled: Boolean((body as { enabled?: boolean } | null)?.enabled),
        granted_capabilities:
          (body as { granted_capabilities?: string[] } | null)?.granted_capabilities ?? [],
      });
      state.rows = [...state.rows.filter((r) => r.id !== created.id), created];
      return route.fulfill(json(created));
    }

    const actionMatch = /^\/v1\/plugins\/([^/]+)\/(enable|disable|sync|status)$/.exec(path);
    if (actionMatch) {
      const id = decodeURIComponent(actionMatch[1] ?? '');
      const action = actionMatch[2];
      const target = state.rows.find((r) => r.id === id);
      if (!target) {
        return route.fulfill(json({ error: { code: 'not_found', message: '插件不存在' } }, 404));
      }
      if (action === 'status') {
        return route.fulfill(
          json({ ...target, reachable: !state.syncMismatch, health_checked_at: new Date().toISOString() }),
        );
      }
      if (action === 'sync' && state.syncMismatch) {
        target.enabled = false;
        target.status = 'identity_mismatch';
        target.last_error = '插件 ID 与端点声明不一致，已停用';
        return route.fulfill(
          json(
            {
              schema_version: 'v1',
              error: {
                code: 'invalid_request',
                message: '插件 ID 与端点声明不一致，已停用',
                retriable: false,
                detail: null,
                trace_id: 'trace-sync',
                field: null,
              },
            },
            422,
          ),
        );
      }
      if (action === 'enable') target.enabled = true;
      if (action === 'disable') target.enabled = false;
      if (action === 'sync') target.status = 'ready';
      return route.fulfill(json(target));
    }

    const deleteMatch = /^\/v1\/plugins\/([^/]+)$/.exec(path);
    if (deleteMatch && method === 'DELETE') {
      const id = decodeURIComponent(deleteMatch[1] ?? '');
      state.rows = state.rows.filter((r) => r.id !== id);
      return route.fulfill(json({ id, deleted: true }));
    }

    return route.fulfill(json({ error: { code: 'not_found', message: path } }, 404));
  });

  await page.goto(`${baseUrl}/settings/plugins`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="external-plugins-page"]').first().waitFor({ timeout: 20_000 });
  // Vite 开发态样式是模块注入的：等样式真的生效再量。
  await page.waitForFunction(() => document.styleSheets.length > 3, undefined, { timeout: 20_000 });
  await page.waitForTimeout(300);
  return page;
}

/* ------------------------------------------------------------------ 演练 */

describe('演练：登记 → 启用 → 停用 → 取消登记', () => {
  it('空列表自动展开登记表；登记后出现一行，请求体只有契约的四个键', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    const page = await openPage(state, { width: 1440, height: 900 });

    await page.getByTestId('ep-empty').waitFor({ timeout: 10_000 });

    await page.fill('#ep-endpoint', 'http://127.0.0.1:8197');
    await page.fill('#ep-expected', 'example.notes');
    await page.getByTestId('ep-grant-tools').check();
    await page.getByTestId('ep-grant-commands').check();
    await page.getByTestId('ep-submit').click();

    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 15_000 });

    const register = state.requests.find((r) => r.path === '/v1/plugins/register');
    expect(register, '页面没有发出登记请求').toBeTruthy();
    // `extra=forbid`：多一个键服务端就 422。这条是唯一能证明"发对了"的断言。
    expect(Object.keys(register?.body as object).sort()).toEqual([
      'enabled',
      'endpoint',
      'expected_id',
      'granted_capabilities',
    ]);
    expect((register?.body as { granted_capabilities: string[] }).granted_capabilities).toEqual([
      'tools',
      'commands',
    ]);

    await pollText(page, 'ep-notice', has('尚未启用'), '登记成功的横幅');
    await pollText(page, 'ep-state-example.notes', has('已停用'), '新登记行的状态');
    await page.close();
  }, TIMEOUT);

  it('启用 → 停用：按钮与实际 API 一致，徽标与行状态同步变化', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row({ enabled: false })];

    const page = await openPage(state, { width: 1440, height: 900 });
    await pollText(page, 'ep-state-example.notes', has('已停用'), '初始状态');

    await page.getByTestId('ep-toggle-example.notes').click();
    await pollText(page, 'ep-state-example.notes', has('已启用'), '启用后');
    expect(state.requests.some((r) => r.path.endsWith('/enable') && r.method === 'POST')).toBe(true);

    await page.getByTestId('ep-toggle-example.notes').click();
    await pollText(page, 'ep-state-example.notes', has('已停用'), '停用后');
    expect(state.requests.some((r) => r.path.endsWith('/disable') && r.method === 'POST')).toBe(true);

    // 行状态数据属性也跟着变（不只是文字）
    expect(await page.getByTestId('ep-row-example.notes').getAttribute('data-state')).toBe('off');
    await page.close();
  }, TIMEOUT);

  it('取消登记要二次确认；点「取消」不发请求，确认后才 DELETE', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row()];
    const page = await openPage(state, { width: 1440, height: 900 });

    await page.getByTestId('ep-delete-example.notes').click();
    await page.getByTestId('ep-confirm-delete-example.notes').waitFor({ timeout: 10_000 });
    expect(state.requests.some((r) => r.method === 'DELETE')).toBe(false);

    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByTestId('ep-delete-example.notes').waitFor({ timeout: 10_000 });
    expect(state.requests.some((r) => r.method === 'DELETE')).toBe(false);

    await page.getByTestId('ep-delete-example.notes').click();
    await page.getByTestId('ep-confirm-delete-example.notes').click();
    await page.getByTestId('ep-empty').waitFor({ timeout: 15_000 });
    expect(state.requests.some((r) => r.method === 'DELETE')).toBe(true);
    await page.close();
  }, TIMEOUT);
});

describe('演练：失败时看得懂下一步', () => {
  it('manifest 有问题：横幅列出逐项排查顺序与 trace，不是一句「失败」', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.registerError = {
      code: 'invalid_request',
      message: '插件登记失败，请检查端点是否可达且符合 v1 契约',
      trace: 'trace-abc123',
    };
    const page = await openPage(state, { width: 1440, height: 900 });

    await page.fill('#ep-endpoint', 'http://127.0.0.1:1/dead');
    await page.getByTestId('ep-grant-tools').check();
    await page.getByTestId('ep-submit').click();

    await pollText(page, 'ep-notice', has('manifest'), '登记失败说明');
    const text = (await page.getByTestId('ep-notice').textContent()) ?? '';
    // 四种可能的原因都要在（服务端只有一句通用兜底，页面不能假装知道是哪一个）
    expect(text).toContain('/v1/manifest');
    expect(text).toContain('min/max_host_version');
    expect(text).toContain('trace-abc123');
    await page.close();
  }, TIMEOUT);

  it('同步时标识变化：422 之后列表必须重读，行变成「标识已变，已停用」', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row({ enabled: true })];
    state.syncMismatch = true;
    const page = await openPage(state, { width: 1440, height: 900 });

    await pollText(page, 'ep-state-example.notes', has('已启用'), '同步前');
    await page.getByTestId('ep-sync-example.notes').click();

    // 失败不等于没生效：服务端**已经提交**了停用（routes 里显式 commit）。
    // 「只在成功时刷新」的实现会在这里红：徽标仍是"已启用"。
    await pollText(page, 'ep-state-example.notes', has('标识已变'), '同步失败后的状态');
    expect(await page.getByTestId('ep-row-example.notes').getAttribute('data-state')).toBe('blocked');

    const fix = (await page.getByTestId('ep-fix-example.notes').textContent()) ?? '';
    expect(fix).toContain('插件 ID 与端点声明不一致，已停用');
    // 下一步是取消登记，不是再无脑同步
    expect(fix).toContain('取消登记');
    await page.close();
  }, TIMEOUT);

  it('点「检查」后探测结果要留在屏幕上（不能刚拿到就被重读擦掉）', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row({ enabled: true })];
    const page = await openPage(state, { width: 1440, height: 900 });

    await page.getByTestId('ep-check-example.notes').click();
    // 这条是实机联调抓到的：检查完会重读列表，而重读默认清空探测结果，
    // 于是「检查」在界面上什么也不留下（只有一条一闪而过的横幅）。
    await pollText(page, 'ep-health-example.notes', has('可达'), '检查后的探测结果');
    // 而且必须仍然成立：记录可用时探测结果该在
    expect(await page.getByTestId('ep-state-example.notes').textContent()).toContain('已启用');
    await page.close();
  }, TIMEOUT);

  it('权限不足（403）：说明去要权限，而不是显示成「没有插件」', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.forbidden = true;
    const page = await openPage(state, { width: 1440, height: 900 });

    await page.getByTestId('ep-load-error').waitFor({ timeout: 15_000 });
    // 403 不是"没有插件"：空列表会让人以为登记丢了。
    expect(await page.getByTestId('ep-empty').count()).toBe(0);
    await page.close();
  }, TIMEOUT);
});

describe('权限与存储：不把令牌写进浏览器', () => {
  it('整轮操作后 localStorage / sessionStorage 都是空的，cookie 里没有令牌', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row({ enabled: false })];
    const page = await openPage(state, { width: 1440, height: 900 });

    await page.getByTestId('ep-toggle-example.notes').click();
    await pollText(page, 'ep-state-example.notes', has('已启用'), '启用后');
    await page.getByTestId('ep-sync-example.notes').click();

    const storage = await page.evaluate(() => ({
      local: { ...localStorage },
      session: { ...sessionStorage },
      cookieHasToken: /token|bearer|authorization/i.test(document.cookie),
    }));
    expect(storage.local).toEqual({});
    expect(storage.session).toEqual({});
    expect(storage.cookieHasToken).toBe(false);
    await page.close();
  }, TIMEOUT);

  it('刷新后重新向服务端要状态，不沿用本地缓存', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row({ enabled: true, status: 'ready' })];
    const page = await openPage(state, { width: 1440, height: 900 });
    await pollText(page, 'ep-state-example.notes', has('已启用'), '刷新前');

    // 服务端在别处被改了（别处启用失败）；刷新后必须看到新状态。
    state.rows = [
      row({ enabled: false, status: 'unavailable', last_error: 'PluginUnavailable: 连接被拒' }),
    ];
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 20_000 });

    await pollText(page, 'ep-state-example.notes', has('端点不可达'), '刷新后的状态');
    await pollText(page, 'ep-fix-example.notes', has('连接被拒'), '刷新后的失败原因');
    await page.close();
  }, TIMEOUT);
});

describe('布局：桌面一行，窄屏折成竖排且不横向溢出', () => {
  it('1440：一条登记是一行，能力格与操作并排、操作在最右', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [row()];
    const page = await openPage(state, { width: 1440, height: 900 });
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 15_000 });

    const layout = await page.evaluate(() => {
      const main = document.querySelector<HTMLElement>('.ep-row__main');
      const actions = document.querySelector<HTMLElement>('.ep-row__actions');
      const caps = document.querySelector<HTMLElement>('.ep-caps');
      const rect = (el: HTMLElement | null) => el?.getBoundingClientRect() ?? null;
      return {
        direction: main ? getComputedStyle(main).flexDirection : '',
        capsTop: rect(caps)?.top ?? 0,
        actionsTop: rect(actions)?.top ?? 0,
        capsRight: rect(caps)?.right ?? 0,
        actionsRight: rect(actions)?.right ?? 0,
        docW: document.documentElement.scrollWidth,
        clientW: document.documentElement.clientWidth,
      };
    });

    expect(layout.direction).toBe('row');
    // 同一行：能力格与操作区的纵向位置接近（允许基线微差）
    expect(Math.abs(layout.capsTop - layout.actionsTop)).toBeLessThan(24);
    // 操作区在最右侧
    expect(layout.actionsRight).toBeGreaterThan(layout.capsRight);
    expect(layout.docW).toBeLessThanOrEqual(layout.clientW + 1);
    await page.close();
  }, TIMEOUT);

  it('375：折成竖排、操作区占满宽度、不横向溢出、触控目标 ≥40px', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    state.rows = [
      row({
        enabled: false,
        status: 'identity_mismatch',
        last_error: '插件 ID 与端点声明不一致，已停用',
        name: '名字很长很长很长的隔离测试插件用于验证窄屏折行不会把操作按钮挤出去',
      }),
    ];
    const page = await openPage(state, { width: 375, height: 667 });
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 15_000 });

    const layout = await page.evaluate(() => {
      const main = document.querySelector<HTMLElement>('.ep-row__main');
      const actions = document.querySelector<HTMLElement>('.ep-row__actions');
      const rowEl = document.querySelector<HTMLElement>('.ep-row');
      const buttons = [...document.querySelectorAll<HTMLElement>('.ep-row__actions .button')].map(
        (b) => b.getBoundingClientRect().height,
      );
      const overflowing = [...document.querySelectorAll<HTMLElement>('body *')]
        .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
        .slice(0, 5)
        .map((el) => `${el.tagName.toLowerCase()}.${el.className}`);
      return {
        direction: main ? getComputedStyle(main).flexDirection : '',
        actionsWidth: actions?.getBoundingClientRect().width ?? 0,
        rowWidth: rowEl?.getBoundingClientRect().width ?? 0,
        buttonHeights: buttons,
        overflowing,
        docW: document.documentElement.scrollWidth,
        clientW: document.documentElement.clientWidth,
      };
    });

    expect(layout.direction).toBe('column');
    // 操作区占满整行（不是挂在右边的小按钮堆）
    expect(layout.actionsWidth).toBeGreaterThan(layout.rowWidth * 0.85);
    // 触控目标 ≥ 40px（窄屏 CSS 里写的）
    expect(layout.buttonHeights.length).toBeGreaterThan(0);
    for (const height of layout.buttonHeights) expect(height).toBeGreaterThanOrEqual(39);
    expect(layout.overflowing).toEqual([]);
    expect(layout.docW).toBeLessThanOrEqual(layout.clientW + 1);
    await page.close();
  }, TIMEOUT);

  it('窄屏也能完整登记（表单不溢出、按钮点得动）', async (ctx) => {
    if (skip(ctx)) return;
    const state = fakeBackend();
    const page = await openPage(state, { width: 375, height: 667 });

    await page.fill('#ep-endpoint', 'http://127.0.0.1:8197');
    await page.getByTestId('ep-grant-tools').check();
    await page.getByTestId('ep-submit').click();
    await page.getByTestId('ep-row-example.notes').waitFor({ timeout: 15_000 });

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
    await page.close();
  }, TIMEOUT);
});
