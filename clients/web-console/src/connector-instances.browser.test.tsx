/**
 * 接入器**实例**面板的真浏览器回归：左列表右详情，到底是不是一份能用的多实例界面。
 *
 * 为什么必须真浏览器：这一块最容易"名字比实现强"——列表画出来了、选中态也有了，
 * 但保存时永远不带 `instance_id`（或永远发默认值）。在 `renderToStaticMarkup`
 * 里那一切全绿，因为它根本不执行点击与请求。
 *
 * 钉住的六条（每条都对着一个"看起来对、实际错了"的实现）：
 *
 * 1. **保存带的是当前选中的实例**：选 B → 改一个字段 → 保存，请求 URL 里
 *    `instance_id=b`，**不是** `default`、不是第一个实例。这是本文件存在的理由。
 * 2. **切实例草稿重置**：在 A 里输入、不保存 → 切到 B，B 的输入框必须是 B 自己
 *    的已保存值（不是 A 刚输入的那串）；再切回 A，A 的未保存内容还在。
 * 3. **空列表**：`items: []` → 显示"还没有任何实例"的引导，不是空白、不是错误。
 * 4. **加载失败**：列表端点 500 → 显示错误与原因，不是空白。
 * 5. **默认实例的删除按钮是禁用的**，并且旁边写明了原因（不是点了才 409）。
 * 6. **删除是软删**：确认框里说明历史配置与修订链仍在。
 *
 * `/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库）。没有浏览器时标 skipped，
 * 不静默 passed。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';
import {
  makeConnector,
  makeResponse,
  onebotFields,
} from './connectors.fixtures.test-helpers';
import type {
  ConnectorChangeResult,
  ConnectorInstance,
  ConnectorsResponse,
} from './types';

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
  console.warn(`[connector-instances.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 固件 */

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

function instance(overrides: Partial<ConnectorInstance> = {}): ConnectorInstance {
  const instanceId = overrides.instance_id ?? 'default';
  return {
    scope: overrides.scope ?? 'onebot',
    instance_id: instanceId,
    display_name: overrides.display_name ?? '默认',
    enabled: overrides.enabled ?? true,
    deletable: overrides.deletable ?? instanceId !== 'default',
    created_at: overrides.created_at ?? '2026-11-10T02:00:00+00:00',
    created_by: null,
    deleted_at: null,
  };
}

/** 两个普通实例 + 默认实例（顺序与后端一致：默认最前）。 */
const DEFAULT_ROW = instance({ instance_id: 'default', display_name: '默认' });
const ROW_A = instance({ instance_id: 'alpha', display_name: '主号' });
const ROW_B = instance({ instance_id: 'beta', display_name: '小号二号', enabled: false });

interface PutCall {
  /**
   * 请求的**路径 + 查询串**（不含 origin）。
   *
   * 从前这里存的是 `route.request().url()`——完整 URL（`http://127.0.0.1:5173/v1/...`）。
   * 而下面断言比的是 `/v1/settings/connectors/onebot?instance_id=beta` 这种路径
   * 形状，两者**永远不相等**：那条断言会一直红，看上去像"实例维度没接上"，
   * 其实请求一直是对的（`instanceId` 那几条断言一直绿）。
   *
   * 断实例维度只需要路径和 query，origin 是噪音。
   */
  url: string;
  instanceId: string | null;
  ifMatch: string | null;
  body: { set: Record<string, unknown>; clear: string[] };
}

interface Harness {
  page: Page;
  puts: PutCall[];
  /** 列表端点被调用了几次（刷新/新建后要重读）。 */
  listCalls: () => number;
}

interface OpenOptions {
  items?: ConnectorInstance[];
  /** 列表端点返回错误时的状态码。 */
  listError?: number;
  /**
   * 每个实例自己的字段视图（按 `?instance_id=` 区分）。默认实例用页面级那份。
   * 键是实例 id；没给的实例 → 返回 `{}`（模拟"服务端没有实例维度"）。
   */
  views?: Record<string, ConnectorsResponse>;
  /** 每次 PUT 的返回，按顺序排队；用完一律 500。 */
  plans?: { kind: 'ok'; result: ConnectorChangeResult }[];
}

function changeResult(instanceId: string): ConnectorChangeResult {
  return {
    schema_version: 'v2',
    operation_id: `crev_${instanceId}`,
    scope: 'onebot',
    generation: 4,
    status: 'active',
    state: 'active',
    changed: true,
    revision_id: `crev_${instanceId}`,
    active_revision_id: `crev_${instanceId}`,
    ack_count: 1,
    expected_count: 1,
    started_at: '2026-11-10T02:00:00+00:00',
    finished_at: '2026-11-10T02:00:01+00:00',
    error_code: null,
    error_safe: null,
    timings: {},
  };
}

async function openInstances(options: OpenOptions = {}): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: 'reduce',
  });
  contexts.push(context);
  const page = await context.newPage();

  const puts: PutCall[] = [];
  const plans = [...(options.plans ?? [])];
  const items = options.items ?? [DEFAULT_ROW, ROW_A, ROW_B];
  let listCalls = 0;

  // 页面级 `GET /v1/settings/connectors`（不带实例）→ 默认实例的诊断与字段。
  const pageBody = makeResponse({
    connectors: [makeConnector({ fields: onebotFields() })],
  });

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (!path.startsWith('/v1/')) return route.continue();

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
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.6' }));
    if (path === '/v1/persona') return route.fulfill(json({ name: '灵', display_name: '灵' }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [] }));

    // 实例列表
    if (path === '/v1/settings/connectors/onebot/instances' && method === 'GET') {
      listCalls += 1;
      if (options.listError) {
        return route.fulfill(
          json(
            { error: { code: 'internal', message: '实例目录读取失败（测试）' } },
            options.listError,
          ),
        );
      }
      return route.fulfill(
        json({ schema_version: 'v1', scope: 'onebot', items, include_deleted: false }),
      );
    }

    // 页面级读（不带实例）
    if (path === '/v1/settings/connectors' && method === 'GET') {
      const instanceId = url.searchParams.get('instance_id');
      if (instanceId === null) return route.fulfill(json(pageBody));
      // 按实例读：**回显** instance_id，让面板能自证这份视图属于这个实例。
      const body = options.views?.[instanceId];
      if (!body) {
        return route.fulfill(
          json({ error: { code: 'internal', message: `测试没为实例 ${instanceId} 准备视图` } }, 500),
        );
      }
      return route.fulfill(
        json({
          ...body,
          // 回显字段：后端补上之后前端才敢用这份视图。
          instance_id: instanceId,
          connectors: body.connectors.map((connector) => ({
            ...connector,
            id: 'onebot',
          })),
        }),
      );
    }

    // 保存：`PUT /v1/settings/connectors/{scope}?instance_id=`
    if (path === '/v1/settings/connectors/onebot' && method === 'PUT') {
      const body = JSON.parse(route.request().postData() ?? '{}') as PutCall['body'];
      puts.push({
        url: `${url.pathname}${url.search}`,
        instanceId: url.searchParams.get('instance_id'),
        ifMatch: route.request().headers()['if-match'] ?? null,
        body,
      });
      const plan = plans.shift();
      if (!plan) {
        return route.fulfill(
          json({ error: { code: 'internal', message: '测试没有为这次 PUT 准备响应' } }, 500),
        );
      }
      return route.fulfill(json(plan.result, 202));
    }

    return route.fulfill(json({ error: { code: 'not_found', message: path } }, 404));
  });

  await page.goto(`${baseUrl}/settings/connectors`, { waitUntil: 'domcontentloaded' });
  // 实例面板挂进页面需要页面级 GET 先回来，所以等它的 testid。
  await page.getByTestId('connector-instance-panel-onebot').waitFor({ timeout: 20_000 });
  await page.waitForFunction(() => document.styleSheets.length > 3, undefined, {
    timeout: 20_000,
  });
  return { page, puts, listCalls: () => listCalls };
}

/**
 * 卡片字段表单里的某个输入框。
 *
 * **限定在 `.connector-form` 里**：实例面板自己也有一个 `<input>`（新建实例的
 * 显示名），不加作用域会让"按 testid 找输入框"变成"找到页面上任意一个"。这里
 * 只关心字段表单，所以显式钉住它。
 */
function inputOf(page: Page, key: string) {
  // 用属性选择器而不是 `getByTestId`：字段 testid 里带**大写**（`LING_ONEBOT_*`），
  // 而 `getByTestId` 会把值当成 CSS 属性选择器处理（大小写敏感），匹配不到元素。
  return page.locator(`.connector-fields [data-testid="connector-input-${key}"]`);
}

async function selectInstance(page: Page, instanceId: string) {
  await page.getByTestId(`connector-instance-${instanceId}`).click();
  // 选中态落到 DOM 上：等它真的变成 current，而不是靠"点了就算"。
  await expect
    .poll(() => page.getByTestId(`connector-instance-${instanceId}`).getAttribute('aria-current'))
    .toBe('true');
}

/** 给某个实例造一份"它自己"的字段视图（API_BASE 值不同，方便分辨）。 */
function viewFor(apiBase: string): ConnectorsResponse {
  return makeResponse({
    connectors: [
      makeConnector({
        fields: onebotFields().map((field) =>
          field.key === 'LING_ONEBOT_API_BASE'
            ? { ...field, value: apiBase, source: 'db' as const, configured: true }
            : field,
        ),
      }),
    ],
  });
}

/* ------------------------------------------------------------ 用例 */

describe('接入器实例：左列表右详情', () => {
  it('列表渲染每一项（显示名 + 身份 + 启停 + 默认），点选切换右侧', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openInstances({
      views: { alpha: viewFor('http://192.0.2.21:5700'), beta: viewFor('http://192.0.2.22:5700') },
    });

    for (const row of [DEFAULT_ROW, ROW_A, ROW_B]) {
      const item = page.getByTestId(`connector-instance-${row.instance_id}`);
      await expect.poll(() => item.isVisible()).toBe(true);
      expect(await item.textContent()).toContain(row.display_name);
      expect(await item.textContent()).toContain(row.instance_id);
    }
    // 默认实例有「默认」标签，并且处在启用态；beta 是停用态。
    expect(await page.getByTestId('connector-instance-default-default').isVisible()).toBe(true);
    expect(await page.getByTestId('connector-instance-default-enabled').textContent()).toBe('已启用');
    expect(await page.getByTestId('connector-instance-beta-enabled').textContent()).toBe('已停用');

    // 首帧选中后端排序第一个（默认实例）。
    expect(await page.getByTestId('connector-instance-default').getAttribute('aria-current')).toBe(
      'true',
    );

    await selectInstance(page, 'alpha');
    // 右侧显示的是 alpha 自己的已保存值。
    await expect
      .poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').inputValue())
      .toBe('http://192.0.2.21:5700');
    // 只允许一个选中。
    const currents = await page.locator('[data-testid^="connector-instance-"][aria-current="true"]').count();
    expect(currents).toBe(1);
  }, TIMEOUT);

  it('保存时 instance_id 是**当前选中的那个**，不是默认值', async (ctx) => {
    if (skip(ctx)) return;
    const { page, puts } = await openInstances({
      views: {
        alpha: viewFor('http://192.0.2.21:5700'),
        beta: viewFor('http://192.0.2.22:5700'),
      },
      plans: [{ kind: 'ok', result: changeResult('beta') }],
    });

    // 选 beta（**不是**第一个、不是默认实例）。
    await selectInstance(page, 'beta');
    await expect
      .poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').inputValue())
      .toBe('http://192.0.2.22:5700');

    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.30:5700');
    await page.getByTestId('connector-onebot-save').click();
    await page.getByTestId('connector-onebot-notice').waitFor({ timeout: 15_000 });

    expect(puts).toHaveLength(1);
    const put = puts[0]!;
    // ← 这一条就是本文件存在的理由。
    expect(put.instanceId).toBe('beta');
    expect(put.instanceId).not.toBe('default');
    expect(put.instanceId).not.toBe('alpha');
    expect(put.url).toBe('/v1/settings/connectors/onebot?instance_id=beta');
    expect(put.body.set).toEqual({ LING_ONEBOT_API_BASE: 'http://192.0.2.30:5700' });
    expect(put.body).not.toHaveProperty('instance_id');
  }, TIMEOUT);

  it('切实例时草稿被重置：A 的未保存输入不会出现在 B 上', async (ctx) => {
    if (skip(ctx)) return;
    const { page, puts } = await openInstances({
      views: {
        alpha: viewFor('http://192.0.2.21:5700'),
        beta: viewFor('http://192.0.2.22:5700'),
      },
      plans: [{ kind: 'ok', result: changeResult('beta') }],
    });

    // 1) 在 alpha 里输入但**不保存**。
    await selectInstance(page, 'alpha');
    await expect
      .poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').inputValue())
      .toBe('http://192.0.2.21:5700');
    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.99:5700');
    expect(await page.getByTestId('connector-onebot-note').textContent()).toContain('有未保存的改动');
    // 列表上标出"未保存"。
    expect(await page.getByTestId('connector-instance-alpha-dirty').isVisible()).toBe(true);

    // 2) 切到 beta：输入框必须是 **beta 自己的值**，不是刚输入的 99。
    await selectInstance(page, 'beta');
    await expect
      .poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').inputValue())
      .toBe('http://192.0.2.22:5700');
    expect(await inputOf(page, 'LING_ONEBOT_API_BASE').inputValue()).not.toContain('192.0.2.99');
    // beta 没有未保存改动。
    expect(await page.getByTestId('connector-instance-beta-dirty').count()).toBe(0);

    // 3) 在 beta 里什么都不改就点保存 → 不该发出任何请求（beta 是干净的）。
    await page.getByTestId('connector-onebot-save').click();
    await expect
      .poll(() => page.getByTestId('connector-onebot-note').textContent())
      .toContain('没有要保存的改动');
    expect(puts).toHaveLength(0);

    // 4) 切回 alpha：**它自己的**未保存内容还在（切实例不是"丢草稿"）。
    await selectInstance(page, 'alpha');
    await expect
      .poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').inputValue())
      .toBe('http://192.0.2.99:5700');

    // 5) 此时保存：必须发到 alpha（带着它的草稿），不是 beta。
    await page.getByTestId('connector-onebot-save').click();
    await page.getByTestId('connector-onebot-notice').waitFor({ timeout: 15_000 });
    expect(puts).toHaveLength(1);
    expect(puts[0]?.instanceId).toBe('alpha');
    expect(puts[0]?.body.set).toEqual({ LING_ONEBOT_API_BASE: 'http://192.0.2.99:5700' });
  }, TIMEOUT);

  it('空列表：显示"新建第一个实例"的引导，不是空白也不是错误', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openInstances({ items: [] });

    const empty = page.getByTestId('connector-instance-panel-onebot-empty');
    await expect.poll(() => empty.isVisible()).toBe(true);
    expect(await empty.textContent()).toContain('还没有任何实例');
    // 引导里要有一个真的能用的入口。
    await expect.poll(() => page.getByTestId('connector-instance-name').isVisible()).toBe(true);
    await expect.poll(() => page.getByTestId('connector-instance-create-submit').isVisible()).toBe(
      true,
    );
    // 不是错误态，也不是"正在读取"。
    expect(await page.getByTestId('connector-instance-panel-onebot-error').count()).toBe(0);
    expect(await page.getByTestId('connector-instance-panel-onebot-loading').count()).toBe(0);

    // 新建按钮在没有名字时禁用（不能建一个空名字的实例）。
    expect(await page.getByTestId('connector-instance-create-submit').isDisabled()).toBe(true);
  }, TIMEOUT);

  it('列表加载失败：显示错误与原因，不是空白', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openInstances({ listError: 500 });

    const error = page.getByTestId('connector-instance-panel-onebot-error');
    await expect.poll(() => error.isVisible()).toBe(true);
    const text = (await error.textContent()) ?? '';
    expect(text.length).toBeGreaterThan(0);
    // 不能把失败画成空列表。
    expect(await page.getByTestId('connector-instance-panel-onebot-empty').count()).toBe(0);
    // 要有一个重试动作（刷新后能恢复，而不是"只能重开页面"）。
    expect(await error.locator('button').count()).toBeGreaterThan(0);
  }, TIMEOUT);

  it('默认实例的删除按钮是禁用的，并写明原因', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openInstances({
      views: { alpha: viewFor('http://192.0.2.21:5700'), beta: viewFor('http://192.0.2.22:5700') },
    });

    await selectInstance(page, 'default');
    const del = page.getByTestId('connector-instance-delete');
    await expect.poll(() => del.isVisible()).toBe(true);
    expect(await del.isDisabled()).toBe(true);
    const reason = (await page.getByTestId('connector-instance-delete-reason').textContent()) ?? '';
    expect(reason).toContain('默认实例');
    expect(reason).toContain('不能删除');

    // 换成普通实例：按钮可用，原因清空。
    await selectInstance(page, 'alpha');
    await expect.poll(() => del.isDisabled()).toBe(false);
    expect(((await page.getByTestId('connector-instance-delete-reason').textContent()) ?? '').trim()).toBe(
      '',
    );
  }, TIMEOUT);

  it('删除前有确认，并说明是软删（历史配置还在）', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openInstances({
      views: { alpha: viewFor('http://192.0.2.21:5700'), beta: viewFor('http://192.0.2.22:5700') },
    });

    await selectInstance(page, 'alpha');
    await page.getByTestId('connector-instance-delete').click();
    const dialog = page.getByRole('alertdialog');
    await expect.poll(() => dialog.isVisible()).toBe(true);
    const text = (await dialog.textContent()) ?? '';
    expect(text).toContain('软删除');
    expect(text).toContain('修订链');

    // 「再想想」不删。
    await page.getByTestId('connector-instance-delete-cancel').click();
    await expect.poll(() => dialog.count()).toBe(0);
  }, TIMEOUT);
});
