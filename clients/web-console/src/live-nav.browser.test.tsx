/**
 * 直播入口与直播工作台的**真浏览器**回归（真 Chromium + `page.route` 模拟 `/v1/**`）。
 *
 * 为什么必须真浏览器：这里要验的是**「点了之后去哪」**与**「没配置时能做什么」**，
 * 而 `renderToStaticMarkup` 既不执行点击，也不跑 `useEffect` 里那轮 connector 轮询——
 * 在静态渲染里，「空态引导按钮」只是一个不会动的元素，看起来对、其实不知道它指向哪。
 *
 * 钉住的四条（每条都对着一个「看起来对、实际错了」的实现）：
 *
 * 1. **未配置（新实例）**：`GET /v1/live/sessions/current` 返回
 *    `{session: null, planned_connector_id: null, navigation_visible: false}`、
 *    接入器列表为空时，左栏**仍然**有「直播」，点它进工作台——
 *    而不是先看到一个空荡荡的左栏、连入口都没有。
 * 2. **空态可操作**：工作台说清三步，并且「去添加直播间」真的把地址换成
 *    `/settings/connectors`（不是只画一个按钮）。
 * 3. **平台接入里没有重复的「管理直播」**：`自定义接入器` 展开后只有刷新，
 *    往返靠工作台里那个「接入设置」——所以这里同时验工作台 → 接入设置能走通。
 * 4. **普通用户**：`role: 'member'` 时左栏没有「直播」；直接开 `/live` 也给可见的
 *    「由所有者管理」，不是白屏。两条同时成立才算权限落在同一条线上。
 *
 * 没有浏览器时标 skipped，不静默 passed（与 `connector-instances.browser.test.tsx` 同一取舍）。
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
  console.warn(`[live-nav.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

const OWNER = {
  id: 'user_owner',
  username: 'ling',
  role: 'owner',
  subject_id: 'subj_owner',
  display_name: 'Ling',
  must_change_password: false,
} as const;

const MEMBER = {
  id: 'user_member',
  username: 'guest',
  role: 'member',
  subject_id: 'subj_member',
  display_name: '访客',
  must_change_password: false,
} as const;

/**
 * 接入器页在「所有者 + 空接入器」下的最小合法响应。
 *
 * 手写而不复用 `connectors.fixtures.test-helpers`：那是给 Node 侧单测用的夹具，
 * 在这里连它会把一整个测试夹具模块拉进浏览器 bundle，出问题时报的是
 * 「body 是空的」，很难指到真正原因。这里只需要页面判断所有权的那几个字段。
 *
 * `can_edit: true` 是关键：`ConnectorsPage` 只在它是 `true` 时渲染
 * `LiveConnectors`，也就是「管理直播」原来所在的那一栏。给 `false`，
 * 这一栏整块不出现，用例就会把「没渲染」误当成「按钮被删了」。
 */
function CONNECTORS_VIEW(): unknown {
  return {
    schema_version: 'v2',
    as_of: '2026-11-10T02:00:00+00:00',
    can_edit: true,
    connectors: [],
    access: {
      id: 'access',
      generation: 1,
      activation: { mode: 'env', active: true, source: 'env' },
      editable: true,
      fields: [],
    },
  };
}

/** 「新实例」：一个直播间都没有，什么场次都没有，`navigation_visible` 因此为 false。 */
const EMPTY_CURRENT = { session: null, planned_connector_id: null, navigation_visible: false };

interface Harness {
  page: Page;
  /** 访问过的 pathname（按顺序）。 */
  visited: string[];
}

async function open(
  path: string,
  options: {
    user?: typeof OWNER | typeof MEMBER;
    /** `null` 表示「状态接口失效」（500）。 */
    current?: unknown | null;
    connectors?: unknown[];
  } = {},
): Promise<Harness> {
  if (!browser) throw new Error('浏览器未就绪');
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  contexts.push(context);
  const page = await context.newPage();
  const visited: string[] = [];

  // 每次导航都记一笔：断言「点了之后真的换了页」靠它，比读 UI 更直接。
  page.on('framenavigated', (frame) => {
    if (frame !== page.mainFrame()) return;
    try {
      const url = new URL(frame.url());
      visited.push(url.pathname + url.search);
    } catch {
      // about:blank 之类，忽略
    }
  });

  const user = options.user ?? OWNER;
  const current = options.current === undefined ? EMPTY_CURRENT : options.current;
  const connectors = options.connectors ?? [];

  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const method = request.method();

    if (pathname === '/v1/auth/me') {
      await route.fulfill(json({ user, test_mode: false }));
      return;
    }
    if (pathname === '/v1/live/sessions/current') {
      if (current === null) {
        // 「状态接口失效」：一个 500，页面不该因此丢掉入口或白屏。
        await route.fulfill(json({ detail: 'live status unavailable' }, 500));
        return;
      }
      await route.fulfill(json(current));
      return;
    }
    if (pathname === '/v1/settings/custom-connectors') {
      await route.fulfill(json({ connectors }));
      return;
    }
    // 实例列表（该页并行读）：空列表是合法状态，页面显示「还没有实例」。
    if (pathname.endsWith('/instances')) {
      await route.fulfill(json({ instances: [] }));
      return;
    }
    /*
     * 接入器页的完整视图。**必须给对形状**：`ConnectorsPage` 读
     * `body.can_edit` 决定渲不渲染 `LiveConnectors`（`can_edit === true` 才渲染），
     * 而这一栏正是「管理直播」重复入口原来所在的地方。回 `{}` 会让它判成非所有者，
     * 那一栏根本不出现——于是这条用例测的就不再是「按钮被删了」，
     * 而是「整块没渲染」，两者在结果上看起来一样。
     */
    if (pathname === '/v1/settings/connectors') {
      await route.fulfill(json(CONNECTORS_VIEW()));
      return;
    }
    /*
     * 总览：这是一个**必须给对形状**的端点。`DashboardBody` 会解构
     * `data.sections.today_outline`——回 `{}` 就等于让整棵树在渲染时崩掉
     * （`Cannot read properties of undefined (reading 'today_outline')`），
     * 于是左导航根本不存在，所有断言都变成「找不到 sidenav」，
     * 看起来像导航坏了。给一个 `sections: {}` 的空壳即可：
     * 缺的段落各自落到 `MISSING_SECTION`。
     */
    if (pathname === '/v1/dashboard') {
      await route.fulfill(json({ sections: {} }));
      return;
    }
    // 其余端点给一个空成功体：这不是本节要验的东西，别让 404 噪音盖住真正的失败。
    if (method === 'GET') {
      await route.fulfill(json({}));
      return;
    }
    await route.fulfill(json({}, 200));
  });

  // 等 `networkidle` 而不是 `domcontentloaded`：后者在 `<script type="module">`
  // 执行完之前就到了，`#root` 里还是空的——后续断言会拿到「body 是空的」，
  // 看起来像整棵树没起来。Vite dev 下模块加载是若干次请求，等网络静下来才对。
  await page.goto(`${baseUrl}${path}`, { waitUntil: 'networkidle' });
  return { page, visited };
}


type Locator = ReturnType<Page['locator']>;

/**
 * 等一个 locator 出现并可见。
 *
 * 不用 Playwright 的 web-first 匹配器（`expect(locator).toBeVisible()`）：
 * 这个仓库的 `expect` 来自 vitest，没有装上 Playwright 那套断言扩展，
 * 直接写会得到 `Invalid Chai property: toBeVisible`——一个看起来像"功能坏了"、
 * 实际是断言库没接上的失败。`expect.poll` 是两边都有的。
 */
async function visible(target: Locator, timeout = 15_000): Promise<void> {
  await expect.poll(() => target.count(), { timeout }).toBeGreaterThan(0);
  await expect.poll(() => target.first().isVisible(), { timeout }).toBe(true);
}

async function gone(target: Locator, timeout = 15_000): Promise<void> {
  await expect.poll(() => target.count(), { timeout }).toBe(0);
}

async function url(page: Page, pattern: RegExp, timeout = 15_000): Promise<void> {
  await expect.poll(() => new URL(page.url()).pathname, { timeout }).toMatch(pattern);
}

/** 左栏（桌面）里「直播」那一项。窄屏在抽屉里，另有断言。 */
const sidenavLive = (page: Page) => page.locator('[data-testid="sidenav"] [data-testid="nav-live"]');

describe('直播入口与工作台（真浏览器）', () => {
  it('新实例未配置时左栏仍有直播，进入后能看清第一次配置的三步', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await open('/dashboard');

    // 1. 未配置也看得见入口——这是本次改动的核心，旧实现下这里会超时。
    await visible(sidenavLive(page));
    expect(await page.locator('[data-testid="nav-live"]').count()).toBe(1);

    // 2. 点它真的到 /live，并且工作台是可用的（不是一句「由所有者管理」）。
    await sidenavLive(page).click();
    await visible(page.locator('[data-testid="live-page"]'));
    await url(page, /^\/live$/);
    expect(await page.locator('[data-testid="nav-live"]').getAttribute('aria-current')).toBe('page');

    // 3. 空态：三步 + 一个真的能走的入口。
    const empty = page.locator('[data-testid="live-empty"]');
    await visible(empty);
    expect(await empty.locator('li').count()).toBe(3);

    // 4. 「去添加直播间」把地址换到接入设置——不是只画个按钮。
    await page.locator('[data-testid="live-empty-setup"]').click();
    await url(page, /^\/settings\/connectors$/);
  }, TIMEOUT);

  it('平台接入里不再有重复的「管理直播」，且工作台的「接入设置」能回去', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await open('/settings/connectors');

    // 展开「自定义接入器」折叠区（原先「管理直播」就在这一栏里）。
    const details = page.locator('details.live-connector');
    // 先等这一页真的画出内容：`ConnectorsPage` 首帧是 loading，
    // 直接找 `details` 会把「还没渲染」误读成「这一栏没了」。
    {
      const { writeFileSync } = await import('node:fs');
      await page.waitForTimeout(2000);
      writeFileSync('/tmp/diag3.json', JSON.stringify({
        url: page.url(),
        main: await page.locator('main').count(),
        body: (await page.locator('body').innerText()).slice(0, 400),
        html: (await page.content()).slice(0, 200),
      }, null, 2));
    }
    await visible(page.locator('main'));
    await visible(details);
    expect(await details.count()).toBe(1);
    await details.locator('summary').click();
    // 展开后**要等接入选择框真的出现**：折叠区打开时才去拉实例列表，
    // 立刻断言会拿到展开前的那一帧（`select` 数 0），失败信息看起来像
    // 「这一栏没了」，其实是还没渲染。
    await expect
      .poll(() => details.locator('select').count(), { timeout: 15_000 })
      .toBeGreaterThan(0);

    // 重复入口已移除；正向配对：这一栏本身、接入选择框与刷新仍然在。
    await gone(details.getByRole('button', { name: '管理直播' }));
    expect(await details.getByRole('button', { name: /刷新/ }).count()).toBe(1);
    expect(await details.getByRole('button', { name: '保存接入器' }).count()).toBe(1);

    // 从工作台往回走的那条路仍然通（往返的另一半）。
    await page.goto(`${baseUrl}/live`, { waitUntil: 'networkidle' });
    await visible(page.locator('[data-testid="live-page"]'));
    await page.getByRole('button', { name: '接入设置' }).click();
    await url(page, /^\/settings\/connectors$/);
  }, TIMEOUT);

  it('状态接口失效时入口不消失，也不白屏', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await open('/dashboard', { current: null });

    // 入口的可见性不依赖状态接口：它挂了，导航照样在。
    await visible(sidenavLive(page));
    await sidenavLive(page).click();
    await visible(page.locator('[data-testid="live-page"]'));
    // 工作台给出可见的失败原因，而不是一片空白。
    await visible(page.locator('[role="alert"]'));
  }, TIMEOUT);

  it('普通用户：左栏没有直播，直接开 /live 得到可见的权限说明', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await open('/dashboard', { user: MEMBER });

    // 负向断言必须配正向断言：主组照旧渲染，否则「没有 nav-live」可能只是整页没起来。
    await visible(page.locator('[data-testid="sidenav"] [data-testid="nav-dashboard"]'));
    await gone(page.locator('[data-testid="nav-live"]'));

    // 直接开地址也给可见说明，不是白屏、不是 404。
    await page.goto(`${baseUrl}/live`, { waitUntil: 'networkidle' });
    await visible(page.locator('.page__hint'));
    expect(await page.locator('.page__hint').textContent()).toContain('所有者');
  }, TIMEOUT);
});

describe('窄屏抽屉（375px）', () => {
  it('抽屉里与桌面是同一套导航：有直播、有分组、点完关抽屉', async (ctx) => {
    if (skip(ctx)) return;
    if (!browser) throw new Error('浏览器未就绪');
    const context = await browser.newContext({
      viewport: { width: 375, height: 667 },
      isMobile: true,
      hasTouch: true,
    });
    contexts.push(context);
    const page = await context.newPage();
    await page.route('**/v1/**', async (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === '/v1/auth/me') return route.fulfill(json({ user: OWNER, test_mode: false }));
      if (pathname === '/v1/live/sessions/current') return route.fulfill(json(EMPTY_CURRENT));
      if (pathname === '/v1/settings/custom-connectors') return route.fulfill(json({ connectors: [] }));
      if (pathname === '/v1/dashboard') return route.fulfill(json({ sections: {} }));
      return route.fulfill(json({}));
    });
    await page.goto(`${baseUrl}/dashboard`, { waitUntil: 'networkidle' });

    await page.locator('[data-testid="nav-open"]').click();
    const drawer = page.locator('[data-testid="nav-drawer"]');
    await visible(drawer);

    // 桌面 rail 在窄屏是 display:none；这里取到的是抽屉里那一份。
    const drawerLive = drawer.locator('[data-testid="nav-live"]');
    await visible(drawerLive);
    expect(await drawer.locator('nav[aria-label="主导航"] [data-nav-group="live"]').count()).toBe(1);
    // 图标可访问名称：按钮有可读文字（rail 态是视觉隐藏，这里抽屉是展开态）。
    expect((await drawerLive.textContent()) ?? '').toContain('直播');

    // 点「直播」→ 换页 + 抽屉自己关掉（不是留着挡住工作台）。
    await drawerLive.click();
    await url(page, /^\/live$/);
    await visible(page.locator('[data-testid="live-page"]'));
    await gone(page.locator('[data-testid="nav-drawer"]'));
  }, TIMEOUT);
});
