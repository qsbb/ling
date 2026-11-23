import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext, type Page, type Route } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 「允许晚点回」的浏览器验收。
 *
 * 只验界面这一侧真正能骗人的地方：
 * - 群聊 / 网页 / 官方 QQ 机器人会话**不该出现**这个开关（服务端会 422，界面
 *   也就不摆一个注定失败的控件）；
 * - 非所有者没有会话设置入口；
 * - 窗口来回切换时，**上一会话的慢响应**不能改写当前开关；
 * - 保存失败（含 409 未确认实例）后，界面必须回到服务端的状态，而不是停在
 *   "看起来成功了"的那一格。
 *
 * 服务端行为由 `tests/integration/test_onebot_deferred_reply.py` 与真实 API 验收覆盖；
 * 这里刻意不复制服务端判定，只复述它对界面可见的部分。
 */

const root = fileURLToPath(new URL('..', import.meta.url));
let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let address = '';
let startupError: string | null = null;
const contexts: BrowserContext[] = [];

const json = (body: unknown, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
});

const onebotPrivate = {
  schema_version: 'v1', session_id: 'p1', window_id: 'p1', actor_id: 'a', subject_id: 's',
  title: 'onebot-default:private:100200301', channel: 'onebot-default', peer_id: '100200301',
  created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z', message_count: 2, archived: false,
};
const onebotPrivate2 = {
  ...onebotPrivate, session_id: 'p2', window_id: 'p2', title: 'onebot-default:private:100200302', peer_id: '100200302',
};
const onebotGroup = {
  ...onebotPrivate, session_id: 'g1', window_id: 'g1', title: 'onebot-default:group:100200400', peer_id: '100200400',
};
const qqbotPrivate = {
  ...onebotPrivate, session_id: 'q1', window_id: 'q1', title: 'qqbot-main:private:100200303',
  channel: 'qqbot-main', peer_id: '100200303',
};
const webSession = {
  schema_version: 'v1', session_id: 'w1', window_id: 'w1', actor_id: 'a', subject_id: 's',
  title: '自己的会话', channel: 'web', peer_id: null,
  created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z', message_count: 2, archived: false,
};

const FIELDS = {
  persona_id: {
    global_value: 'p1', instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: 'p1', source: 'global',
  },
  private_may_stay_silent: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: true, source: 'global',
  },
  group_mention_requires_reply: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: true, source: 'global',
  },
  group_attention_deferred: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: true, source: 'global',
  },
  group_turn_budget: {
    global_value: 4, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: 4, source: 'global',
  },
  rest_may_skip: {
    global_value: true, instance_override: null, has_instance_override: false,
    session_override: null, has_session_override: false, effective: true, source: 'global',
  },
};

const policyView = (originStatus: 'known' | 'unknown' | 'not_applicable') => ({
  field: 'private_may_stay_silent',
  global_value: true,
  override: null,
  effective: true,
  source: 'global',
  fields: FIELDS,
  origin_status: originStatus,
  instance_scope: originStatus === 'known' ? 'onebot' : null,
  instance_id: originStatus === 'known' ? 'default' : null,
  global_revision: 0,
  instance_revision: 0,
  session_revision: 0,
});

beforeAll(async () => {
  try {
    server = await createServer({
      configFile: false, root, plugins: [react()], logLevel: 'silent',
      server: { host: '127.0.0.1', port: 0 },
    });
    await server.listen();
    const socket = server.httpServer?.address();
    if (!socket || typeof socket === 'string') throw new Error('Vite 没有可用端口');
    address = `http://127.0.0.1:${socket.port}`;
    browser = await chromium.launch();
  } catch (cause) {
    startupError = cause instanceof Error ? cause.message : String(cause);
  }
}, 90000);

afterEach(async () => {
  while (contexts.length) await contexts.pop()?.close();
});
afterAll(async () => { await browser?.close(); await server?.close(); });

interface DeferredServer {
  /** 每个会话自己的开关状态，按 session_id 存。 */
  state: Map<string, boolean>;
  /** PUT 的调用记录。 */
  writes: Array<{ sessionId: string; enabled: boolean }>;
  /** 让下一次 GET 故意晚回，用来制造会话切换竞态。 */
  delayNextGet: Map<string, Promise<void>>;
  /** 让下一次 PUT 返回指定状态码。 */
  failNextPut: number | null;
}

function deferredServer(): DeferredServer {
  return {
    state: new Map(),
    writes: [],
    delayNextGet: new Map(),
    failNextPut: null,
  };
}

async function openChat(
  role: 'owner' | 'member',
  sessions: unknown[],
  originStatus: 'known' | 'unknown' | 'not_applicable',
  options: { server?: DeferredServer; startAt?: string; putError?: () => { status: number; body: unknown } | null } = {},
): Promise<{ page: Page; server: DeferredServer }> {
  if (!browser) throw new Error(startupError ?? '浏览器未启动');
  const state = options.server ?? deferredServer();
  // 宽于 900px：侧栏在桌面布局里常驻，切换会话不必先开抽屉。
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  contexts.push(context);
  const page = await context.newPage();
  let failNextPut = options.putError ?? null;

  const handle = async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();

    const deferredMatch = /^\/v1\/sessions\/([^/]+)\/onebot-deferred$/.exec(path);
    if (deferredMatch) {
      const sessionId = decodeURIComponent(deferredMatch[1] ?? '');
      if (method === 'GET') {
        const wait = state.delayNextGet.get(sessionId);
        if (wait) {
          state.delayNextGet.delete(sessionId);
          await wait;
        }
        return route.fulfill(json({ enabled: state.state.get(sessionId) ?? false }));
      }
      if (method === 'PUT') {
        const body = req.postDataJSON() as { enabled: boolean };
        state.writes.push({ sessionId, enabled: body.enabled });
        const failure = failNextPut?.();
        if (failure) {
          failNextPut = null;
          return route.fulfill(json(failure.body, failure.status));
        }
        state.state.set(sessionId, body.enabled);
        return route.fulfill(json({ enabled: body.enabled }));
      }
    }

    if (path === '/v1/auth/me') {
      return route.fulfill(json({
        user: { id: 'u', username: 'test', role, subject_id: 's', display_name: '用户' },
        test_mode: false,
      }));
    }
    if (path === '/v1/sessions') return route.fulfill(json({ sessions }));
    if (/^\/v1\/sessions\/[^/]+\/messages$/.test(path)) {
      return route.fulfill(json({ messages: [], active_turn: null }));
    }
    if (path === '/v1/personas') {
      return route.fulfill(json({
        items: [{ id: 'p1', persona_id: 'p1', name: 'default', display_name: '全局人格' }],
        active_persona_id: 'p1', total: 1,
      }));
    }
    if (/^\/v1\/sessions\/[^/]+\/dialogue-policy$/.test(path)) {
      return route.fulfill(json(policyView(originStatus)));
    }
    if (/^\/v1\/sessions\/[^/]+\/origin-binding$/.test(path)) {
      return route.fulfill(json({
        scope: 'onebot',
        candidates: [{ scope: 'onebot', instance_id: 'default', display_name: '本机 OneBot', enabled: true }],
        manually_bound: originStatus === 'known',
      }));
    }
    if (path.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: '目标不存在', retriable: false } }, 404));
    }
    return route.continue();
  };

  await page.route('**/*', handle);
  await page.goto(`${address}/s/${(sessions[0] as { session_id: string }).session_id}`, {
    waitUntil: 'domcontentloaded',
  });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  return { page, server: state };
}

async function openSettings(page: Page): Promise<void> {
  const opener = page.getByRole('button', { name: '会话设置' });
  await opener.waitFor();
  // 切会话会把面板自动收起；但重复点开/反复点开时它可能已经是展开的。
  // 无脑 click 会把它关掉，所以按 `aria-expanded` 决定要不要点。
  if (await opener.getAttribute('aria-expanded') !== 'true') await opener.click();
  await settingsRegion(page).waitFor();
}

/**
 * 切到另一条会话，并等到**新会话的设置面板**能打开。
 *
 * 不能只等 URL：`ChatPage` 在会话切换期间的 `session` 仍是上一条，
 * 设置按钮会短暂消失/指向旧会话。等 URL 再点，点到的是正在换的那一帧。
 */
function settingsRegion(page: Page) {
  return page.getByRole('region', { name: '会话设置' });
}

async function switchTo(page: Page, sessionId: string, title: string): Promise<void> {
  await page.waitForURL(new RegExp(`/s/${sessionId}$`));
  // URL 先变、`currentId` 后变：设置面板按会话挂载，只等 URL 会读到上一条
  // 会话的开关。等到标题栏真的换成目标会话，才算切换完成。
  await expect.poll(() => page.getByTestId('session-title').textContent()).toBe(title);
  await page.getByRole('button', { name: '会话设置' }).waitFor();
}

describe('OneBot 私聊的「允许晚点回」', () => {
  it('默认关闭，勾选后开启，刷新后仍是开启，取消后关闭', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page, server } = await openChat('owner', [onebotPrivate], 'known');
    await openSettings(page);

    const toggle = page.getByRole('checkbox', { name: '允许晚点回' });
    await toggle.waitFor();
    expect(await toggle.isChecked()).toBe(false);
    await toggle.click();
    await expect.poll(() => server.state.get('p1')).toBe(true);
    await expect.poll(() => toggle.isEnabled()).toBe(true);
    expect(await toggle.isChecked()).toBe(true);

    // 刷新 = 重新读服务端，而不是复用本地乐观值。
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="message-scroll"]').waitFor();
    await openSettings(page);
    const reopened = page.getByRole('checkbox', { name: '允许晚点回' });
    await reopened.waitFor();
    expect(await reopened.isChecked()).toBe(true);

    await reopened.click();
    await expect.poll(() => server.state.get('p1')).toBe(false);
    await expect.poll(() => reopened.isChecked()).toBe(false);
    expect(server.writes).toEqual([
      { sessionId: 'p1', enabled: true },
      { sessionId: 'p1', enabled: false },
    ]);
  }, 30000);

  it('两个私聊各自保存，切回去读到的是各自的值', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page, server } = await openChat('owner', [onebotPrivate, onebotPrivate2], 'known');
    await openSettings(page);
    await page.getByRole('checkbox', { name: '允许晚点回' }).click();
    await expect.poll(() => server.state.get('p1')).toBe(true);

    // 切到第二个私聊：它仍是关闭的，不能被第一个会话的值带过去。
    await page.getByRole('button', { name: /私聊 100200302/ }).first().click();
    await switchTo(page, 'p2', '私聊 100200302');
    await openSettings(page);
    const second = page.getByRole('checkbox', { name: '允许晚点回' });
    await second.waitFor();
    expect(await second.isChecked()).toBe(false);

    await second.click();
    await expect.poll(() => server.state.get('p2')).toBe(true);
    await expect.poll(() => server.state.get('p1')).toBe(true);

    await page.getByRole('button', { name: /私聊 100200301/ }).first().click();
    await switchTo(page, 'p1', '私聊 100200301');
    await openSettings(page);
    const first = page.getByRole('checkbox', { name: '允许晚点回' });
    await first.waitFor();
    expect(await first.isChecked()).toBe(true);
  }, 30000);

  it('上一会话的慢响应在卸载后被丢弃，不改写当前开关', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page, server } = await openChat('owner', [onebotPrivate, onebotPrivate2], 'known');
    server.state.set('p1', true);
    server.state.set('p2', false);

    // 让 p1 的开关请求在**切换之后**才返回。它读到的值是「开启」，
    // 而那时站着的已经是 p2。
    let releaseP1: () => void = () => {};
    server.delayNextGet.set('p1', new Promise<void>((resolve) => { releaseP1 = resolve; }));

    await openSettings(page);
    await page.getByRole('checkbox', { name: '允许晚点回' }).waitFor();

    await page.getByRole('button', { name: /私聊 100200302/ }).first().click();
    await switchTo(page, 'p2', '私聊 100200302');
    // 切会话时面板收起——这正是丢弃上一会话响应的机制，值得单独钉住。
    expect(await settingsRegion(page).count()).toBe(0);

    await openSettings(page);
    const second = page.getByRole('checkbox', { name: '允许晚点回' });
    await second.waitFor();
    expect(await second.isChecked()).toBe(false);

    releaseP1();
    await page.waitForTimeout(400);
    expect(await second.isChecked()).toBe(false);
    expect(server.writes).toEqual([]);
  }, 30000);

  it('群聊、网页与官方 QQ 机器人会话不出现这个开关', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    for (const session of [onebotGroup, webSession, qqbotPrivate]) {
      const { page } = await openChat('owner', [session], 'known');
      await openSettings(page);
      const region = settingsRegion(page);
      await region.getByLabel('人格').waitFor();
      expect(await region.getByRole('checkbox', { name: '允许晚点回' }).count()).toBe(0);
    }
  }, 40000);

  it('非所有者没有会话设置入口', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page } = await openChat('member', [onebotPrivate], 'known');
    expect(await page.getByRole('button', { name: '会话设置' }).count()).toBe(0);
  }, 20000);

  it('实例未确认时开启得到 409，开关回到关闭并说明下一步', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page, server } = await openChat('owner', [onebotPrivate], 'unknown', {
      putError: () => ({
        status: 409,
        body: { error: { code: 'conflict', message: '请先确认会话的 OneBot 接入实例', retriable: false } },
      }),
    });
    await openSettings(page);
    const toggle = page.getByRole('checkbox', { name: '允许晚点回' });
    await toggle.waitFor();
    await toggle.click();
    // 409 的文案要说清「下一步去哪」——面板里就有实例确认入口。
    await expect.poll(() =>
      settingsRegion(page).getByText(/请先确认会话的 OneBot 接入实例。可在下方「平台来源」里确认后再开启。/).count(),
    ).toBe(1);
    expect(await toggle.isChecked()).toBe(false);
    expect(server.state.get('p1')).toBeUndefined();
    // 保留关闭操作：请求确实发出去了，只是被服务端拒绝。
    expect(server.writes).toEqual([{ sessionId: 'p1', enabled: true }]);
  }, 20000);

  it('普通保存失败不留下"看起来已开启"的界面', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page, server } = await openChat('owner', [onebotPrivate], 'known', {
      putError: () => ({
        status: 500,
        body: { error: { code: 'internal', message: '保存失败，请稍后重试', retriable: true } },
      }),
    });
    await openSettings(page);
    const toggle = page.getByRole('checkbox', { name: '允许晚点回' });
    await toggle.waitFor();
    await toggle.click();
    await expect.poll(() => settingsRegion(page).getByText('保存失败，请稍后重试').count()).toBe(1);
    expect(await toggle.isChecked()).toBe(false);
    expect(server.state.get('p1')).toBeUndefined();
  }, 20000);

  it('反复点开、关闭设置，状态始终来自服务端', async (ctx) => {
    if (startupError) { console.warn(startupError); ctx.skip(); return; }
    const { page } = await openChat('owner', [onebotPrivate], 'known');
    for (let round = 0; round < 3; round += 1) {
      await openSettings(page);
      const toggle = page.getByRole('checkbox', { name: '允许晚点回' });
      await toggle.waitFor();
      expect(await toggle.isChecked()).toBe(false);
      await page.getByRole('button', { name: '关闭' }).click();
      await expect.poll(() => page.getByRole('region', { name: '会话设置' }).count()).toBe(0);
    }
  }, 30000);
});
