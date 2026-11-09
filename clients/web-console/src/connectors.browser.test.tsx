/**
 * 接入器页（`/settings/connectors`）的**真实浏览器**回归：能不能真的改、改完说了什么。
 *
 * 为什么必须真浏览器：这一轮用户的原话是「点进去居然没有配置界面」。SSR 测试
 * 能证明"画出了 `<input>`"，证明不了**真的能输入、点击真的会发请求**——
 * 受控输入框没接 `onChange`、表单 `onSubmit` 没 `preventDefault` 导致整页刷新、
 * 按钮被 CSS 盖住点不到，这些在 `renderToStaticMarkup` 里全绿。
 *
 * 钉住的六条（都是"只看 HTTP 状态码就会骗人"的地方）：
 *
 * 1. 改一个字段 → 点保存 → **请求体只含那一个键**、`If-Match` 是当前 generation，
 *    界面说的是"已生效"并刷新了卡片；
 * 2. **密钥留空不发**（`set` 里没有它），填了新值才发；
 * 3. `rolled_back`（后端此时仍返回 **202**）→ 界面必须说"没起来 / 已恢复旧配置 /
 *    没有生效"，**不能出现"已生效"**；
 * 4. `409` → 说"别处改过了"并给「刷新后重试」；
 * 5. `422` → 点名哪个键不合法，并说"一个连接都没停过"，只有那一行标红；
 * 6. `503` → 说是进程切换不了，不是你填错了。
 *
 * 另外量布局：375px 下不横向溢出、输入框与按钮 ≥44px。
 *
 * `/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库），固件取自
 * `connectors.fixtures.test-helpers.tsx`（字段名来自后端 REGISTRY）。没有浏览器时
 * 标 skipped，不静默 passed。
 */
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';
import { makeActivation, makeConnector, makeResponse, onebotFields } from './connectors.fixtures.test-helpers';
import type { ConnectorChangeResult, ConnectorsResponse } from './types';

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
  console.warn(`[connectors.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟后端 */

interface PutCall {
  scope: string;
  ifMatch: string | null;
  body: { set: Record<string, unknown>; clear: string[] };
}

interface Harness {
  page: Page;
  puts: PutCall[];
  posts: string[];
  /** 每次 PUT 的响应（按调用顺序排队；用完之后一律 500，避免"没配就当成成功"）。 */
}

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/** 一份 `PUT` 的成功返回：`state` 决定界面说什么。 */
function changeResult(
  scope: string,
  state: string,
  overrides: Partial<ConnectorChangeResult> = {},
): ConnectorChangeResult {
  return {
    schema_version: 'v2',
    operation_id: `crev_${state}`,
    scope,
    generation: state === 'active' ? 4 : 3,
    status: state,
    state,
    changed: state === 'active',
    revision_id: `crev_${state}`,
    active_revision_id: state === 'active' ? `crev_${state}` : 'crev_0',
    ack_count: 1,
    expected_count: 1,
    started_at: '2026-11-10T02:00:00+00:00',
    finished_at: '2026-11-10T02:00:01+00:00',
    error_code: null,
    error_safe: null,
    timings: {},
    ...overrides,
  };
}

/** 保存成功之后 GET 到的"新状态"：generation +1，值变成刚才保存的那个。 */
function afterSave(apiBase: string): ConnectorsResponse {
  const body = makeResponse();
  return {
    ...body,
    connectors: body.connectors.map((connector) =>
      connector.id === 'onebot'
        ? {
            ...connector,
            fields: onebotFields().map((field) =>
              field.key === 'LING_ONEBOT_API_BASE'
                ? { ...field, value: apiBase, source: 'db' as const, configured: true }
                : field,
            ),
            generation: 4,
            activation: makeActivation({ scope: 'onebot', generation: 4, state: 'active' }),
          }
        : connector,
    ),
  };
}

type PutPlan =
  | { kind: 'ok'; result: ConnectorChangeResult }
  | { kind: 'error'; status: number; error: Record<string, unknown> };

async function openConnectors(
  viewport: { width: number; height: number },
  plans: PutPlan[] = [],
  options: { body?: ConnectorsResponse } = {},
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const puts: PutCall[] = [];
  const posts: string[] = [];
  let getBody = options.body ?? makeResponse();

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

    if (path === '/v1/settings/connectors' && method === 'GET') {
      return route.fulfill(json(getBody));
    }
    if (path.startsWith('/v1/settings/connectors/') && method === 'PUT') {
      const scope = decodeURIComponent(path.split('/').pop() ?? '');
      const body = JSON.parse(route.request().postData() ?? '{}') as PutCall['body'];
      puts.push({ scope, ifMatch: route.request().headers()['if-match'] ?? null, body });
      const plan = plans.shift();
      if (!plan) {
        return route.fulfill(
          json({ error: { code: 'internal', message: '测试没有为这次 PUT 准备响应' } }, 500),
        );
      }
      if (plan.kind === 'error') return route.fulfill(json({ error: plan.error }, plan.status));
      // 生效后 GET 到的是新状态（卡片要跟着变）。
      if (plan.result.state === 'active') {
        const next = afterSave(String(body.set.LING_ONEBOT_API_BASE ?? ''));
        getBody = next;
      }
      return route.fulfill(json(plan.result, 202));
    }
    if (path === '/v1/settings/connectors/reset' && method === 'POST') {
      posts.push(path);
      return route.fulfill(
        json({
          schema_version: 'v2',
          ok: true,
          results: [{ scope: 'onebot', state: 'active', cleared: ['LING_ONEBOT_API_BASE'] }],
        }),
      );
    }
    return route.fulfill(json({ error: { code: 'not_found', message: path } }, 404));
  });

  await page.goto(`${baseUrl}/settings/connectors`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('connector-onebot').waitFor({ timeout: 20_000 });
  // Vite 开发态下样式是模块注入的：等样式真的生效再量布局。
  await page.waitForFunction(() => document.styleSheets.length > 3, undefined, { timeout: 20_000 });
  return { page, puts, posts };
}

/** 打开 OneBot 卡里的一个字段输入框。 */
function inputOf(page: Page, key: string) {
  return page.getByTestId(`connector-input-${key}`);
}

/* ------------------------------------------------------------ 用例 */

describe('接入器：真的能改并保存', () => {
  it('改一个字段 → 保存：请求体只含那一个键，If-Match 是当前 generation，界面说已生效', async (ctx) => {
    if (skip(ctx)) return;
    const { page, puts } = await openConnectors({ width: 1440, height: 1000 }, [
      { kind: 'ok', result: changeResult('onebot', 'active') },
    ]);

    // 打开时表单就已展开（用户抱怨的正是"点进去没有配置界面"）。
    await expect.poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').isVisible()).toBe(true);
    await expect.poll(() => page.getByTestId('connector-onebot-save').isVisible()).toBe(true);

    const input = inputOf(page, 'LING_ONEBOT_API_BASE');
    await expect.poll(() => input.inputValue()).toBe('http://127.0.0.1:5700');
    // 改之前：备注是"当前版本 + 终态"，不是"有未保存的改动"。
    expect(await page.getByTestId('connector-onebot-note').textContent()).toContain('当前版本 3');

    await input.fill('http://192.0.2.10:5700');
    expect(await page.getByTestId('connector-onebot-note').textContent()).toContain('有未保存的改动');

    await page.getByTestId('connector-onebot-save').click();
    await page.getByTestId('connector-onebot-notice').waitFor({ timeout: 15_000 });

    // 请求体：只有改过的那一个键；密钥一个都没发。
    expect(puts).toHaveLength(1);
    const put = puts[0]!;
    expect(put.scope).toBe('onebot');
    expect(put.ifMatch).toBe('3');
    expect(put.body.set).toEqual({ LING_ONEBOT_API_BASE: 'http://192.0.2.10:5700' });
    expect(put.body.clear).toEqual([]);

    const notice = page.getByTestId('connector-onebot-notice');
    expect(await notice.getAttribute('data-tone')).toBe('ok');
    expect(await notice.textContent()).toContain('已生效');
    // 保存成功后重新读了 GET：卡片的值与新 generation 都来自服务端。
    await expect.poll(() => inputOf(page, 'LING_ONEBOT_API_BASE').inputValue()).toBe(
      'http://192.0.2.10:5700',
    );
    expect(await page.getByTestId('connector-onebot-note').textContent()).toContain('当前版本 4');
  }, TIMEOUT);

  it('密钥留空不发；填了新值才发，且输入框始终不回显已有值', async (ctx) => {
    if (skip(ctx)) return;
    const { page, puts } = await openConnectors({ width: 1440, height: 1000 }, [
      { kind: 'ok', result: changeResult('onebot', 'active') },
    ]);

    const token = inputOf(page, 'LING_ONEBOT_WS_TOKEN');
    // 输入框是密码框且值为空：已有密钥不回显。
    expect(await token.getAttribute('type')).toBe('password');
    expect(await token.inputValue()).toBe('');
    expect(await token.getAttribute('placeholder')).toContain('已设置（留空则不修改）');

    // 只改一个普通字段就保存：密钥不出现在请求体里。
    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.10:5700');
    await page.getByTestId('connector-onebot-save').click();
    await page.getByTestId('connector-onebot-notice').waitFor();
    expect(puts[0]?.body.set).toEqual({ LING_ONEBOT_API_BASE: 'http://192.0.2.10:5700' });
    expect('LING_ONEBOT_WS_TOKEN' in (puts[0]?.body.set ?? {})).toBe(false);
    expect(puts[0]?.body.clear).not.toContain('LING_ONEBOT_WS_TOKEN');
  }, TIMEOUT);

  it('「清除」密钥 → 走 clear（不是 set:""），输入框换成一段说明', async (ctx) => {
    if (skip(ctx)) return;
    const { page, puts } = await openConnectors({ width: 1440, height: 1000 }, [
      { kind: 'ok', result: changeResult('onebot', 'active') },
    ]);

    await page.getByTestId('connector-clear-LING_ONEBOT_WS_TOKEN').click();
    await page.getByTestId('connector-cleared-LING_ONEBOT_WS_TOKEN').waitFor();
    await page.getByTestId('connector-onebot-save').click();
    await page.getByTestId('connector-onebot-notice').waitFor();

    expect(puts[0]?.body.clear).toEqual(['LING_ONEBOT_WS_TOKEN']);
    expect('LING_ONEBOT_WS_TOKEN' in (puts[0]?.body.set ?? {})).toBe(false);
  }, TIMEOUT);

  it('白名单组（access）也能保存，If-Match 用它自己的 generation', async (ctx) => {
    if (skip(ctx)) return;
    const { page, puts } = await openConnectors({ width: 1440, height: 1000 }, [
      { kind: 'ok', result: changeResult('access', 'active') },
    ]);

    await page.getByTestId('connector-input-LING_ACCESS_LIST').fill('100200300\n0456');
    await page.getByTestId('connector-access-save').click();
    await page.getByTestId('connector-access-notice').waitFor();

    expect(puts[0]?.scope).toBe('access');
    expect(puts[0]?.ifMatch).toBe('1');
    // 名单保持字符串（前导零 0456 不能变成 456）。
    expect(puts[0]?.body.set).toEqual({ LING_ACCESS_LIST: ['100200300', '0456'] });
  }, TIMEOUT);
});

describe('接入器：最难的分支 —— rolled_back（HTTP 202 但没生效）', () => {
  it('说"新配置没起来、已恢复旧配置、没有生效"，绝不出现"已生效"', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openConnectors({ width: 1440, height: 1000 }, [
      {
        kind: 'ok',
        result: changeResult('onebot', 'rolled_back', {
          error_code: 'connector_start_failed',
          error_safe: '切换时发生异常：RuntimeError',
        }),
      },
    ]);

    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.99:1');
    await page.getByTestId('connector-onebot-save').click();
    const notice = page.getByTestId('connector-onebot-notice');
    await notice.waitFor({ timeout: 15_000 });

    const tone = await notice.getAttribute('data-tone');
    const text = (await notice.textContent()) ?? '';
    // eslint-disable-next-line no-console
    console.info('[connectors.browser] rolled_back 提示：', text);

    expect(tone).toBe('error');
    expect(text).toContain('新配置没起来');
    expect(text).toContain('已恢复旧配置');
    expect(text).toContain('没有生效');
    // **关键**：不能出现"已生效"这句（"没有生效"是允许的否定句）。
    expect(text.replace(/没有生效/g, '')).not.toContain('已生效');
    // 也不能出现"保存成功"这类说法。
    expect(text).not.toContain('保存成功');
    // 后端给的安全原因要转达出来（用户要知道为什么没起来）。
    expect(text).toContain('切换时发生异常');

    // 回滚后卡片状态仍是原来那个（generation 没涨）：不能说成"已连接"。
    expect(await page.getByTestId('connector-onebot-note').textContent()).not.toContain(
      '当前版本 4',
    );
    // 用户填的内容要留着（他还要接着改），不能被清空。
    expect(await inputOf(page, 'LING_ONEBOT_API_BASE').inputValue()).toBe('http://192.0.2.99:1');
  }, TIMEOUT);
});

describe('接入器：失败分支各说各的话', () => {
  it('409 → 说"别处改过了"，给「刷新后重试」，不假装保存成功', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openConnectors({ width: 1440, height: 1000 }, [
      {
        kind: 'error',
        status: 409,
        error: {
          code: 'conflict',
          message: '配置刚被别处改过，请刷新后再保存',
          detail: 'generation_conflict',
        },
      },
    ]);

    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.10:5700');
    await page.getByTestId('connector-onebot-save').click();
    const notice = page.getByTestId('connector-onebot-notice');
    await notice.waitFor();

    expect(await notice.getAttribute('data-tone')).toBe('error');
    const text = (await notice.textContent()) ?? '';
    expect(text).toContain('别处已经改过了');
    expect(text).toContain('刷新状态');
    expect(text).not.toContain('已生效');
    expect(await page.getByTestId('connector-onebot-notice-reload').isVisible()).toBe(true);

    // 点「刷新后重试」= 重新读 GET（拿到新 generation 才能重试）。
    await page.getByTestId('connector-onebot-notice-reload').click();
    await expect.poll(() => page.getByTestId('connector-onebot').isVisible()).toBe(true);
  }, TIMEOUT);

  it('422 → 点名那个键，说"一个连接都没停过"，只有那一行标红', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openConnectors({ width: 1440, height: 1000 }, [
      {
        kind: 'error',
        status: 422,
        error: {
          code: 'invalid_request',
          message: '端口必须是数字',
          detail: 'invalid_field',
          field: 'LING_ONEBOT_WS_PORT',
        },
      },
    ]);

    await inputOf(page, 'LING_ONEBOT_WS_PORT').fill('abc');
    await page.getByTestId('connector-onebot-save').click();
    const notice = page.getByTestId('connector-onebot-notice');
    await notice.waitFor();

    expect(await notice.getAttribute('data-tone')).toBe('error');
    const text = (await notice.textContent()) ?? '';
    expect(text).toContain('LING_ONEBOT_WS_PORT');
    expect(text).toContain('一个连接都没停过');
    expect(text).toContain('端口必须是数字');

    // 逐字段：只有出错那一行有 data-invalid。
    expect(
      await page.locator('[data-invalid="true"]').count(),
    ).toBe(1);
    const row = page.getByTestId('connector-field-LING_ONEBOT_WS_PORT');
    expect(await row.getAttribute('data-invalid')).toBe('true');
    expect(await inputOf(page, 'LING_ONEBOT_WS_PORT').getAttribute('aria-invalid')).toBe('true');
    expect(await inputOf(page, 'LING_ONEBOT_API_BASE').getAttribute('aria-invalid')).toBeNull();
  }, TIMEOUT);

  it('503 → 说是这个进程切换不了，不是你填错了', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openConnectors({ width: 1440, height: 1000 }, [
      {
        kind: 'error',
        status: 503,
        error: {
          code: 'invalid_request',
          message: '这个进程不负责切换连接器，改配置请到 API 进程',
          detail: 'activation_unavailable',
        },
      },
    ]);

    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.10:5700');
    await page.getByTestId('connector-onebot-save').click();
    const notice = page.getByTestId('connector-onebot-notice');
    await notice.waitFor();

    const text = (await notice.textContent()) ?? '';
    expect(text).toContain('切换不了接入器');
    expect(text).toContain('不是你填错了');
    expect(text).not.toContain('已生效');
  }, TIMEOUT);
});

describe('接入器：恢复默认要二次确认', () => {
  it('点「恢复默认」先弹确认；确认才 POST reset，并说清结果', async (ctx) => {
    if (skip(ctx)) return;
    const { page, posts } = await openConnectors({ width: 1440, height: 1000 });

    await page.getByTestId('connectors-reset').click();
    await page.getByTestId('connectors-reset-confirm').waitFor();
    expect(posts).toHaveLength(0);

    // 先「再想想」：不发任何请求。
    await page.getByTestId('connectors-reset-cancel').click();
    await expect.poll(() => page.getByTestId('connectors-reset-confirm').count()).toBe(0);
    expect(posts).toHaveLength(0);

    await page.getByTestId('connectors-reset').click();
    await page.getByTestId('connectors-reset-confirm').click();
    await page.getByTestId('connectors-reset-notice').waitFor();
    expect(posts).toEqual(['/v1/settings/connectors/reset']);
    const text = (await page.getByTestId('connectors-reset-notice').textContent()) ?? '';
    expect(text).toContain('已恢复默认');
    expect(text).toContain('LING_ONEBOT_API_BASE');
  }, TIMEOUT);
});

describe('接入器：窄屏表单可用', () => {
  it('375：不横向溢出，输入框与保存按钮都 ≥44px', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openConnectors({ width: 375, height: 720 });

    const info = await page.evaluate(() => {
      const input = document.querySelector<HTMLElement>(
        '[data-testid="connector-input-LING_ONEBOT_API_BASE"]',
      );
      const save = document.querySelector<HTMLElement>('[data-testid="connector-onebot-save"]');
      const clear = document.querySelector<HTMLElement>(
        '[data-testid="connector-clear-LING_ONEBOT_WS_TOKEN"]',
      );
      const box = (el: HTMLElement | null) => {
        const rect = el?.getBoundingClientRect();
        return rect ? { w: Math.round(rect.width), h: Math.round(rect.height) } : null;
      };
      return {
        doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        input: box(input),
        save: box(save),
        clear: box(clear),
        overflow: [...document.querySelectorAll<HTMLElement>('body *')]
          .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
          .slice(0, 5)
          .map((el) => `${el.tagName.toLowerCase()}.${el.className}`),
      };
    });

    expect(info.doc).toBeLessThanOrEqual(1);
    expect(info.overflow, `溢出元素：${info.overflow.join(', ')}`).toEqual([]);
    // 量不到就是真 bug，不能当成 0 蒙混过去。
    expect(info.input).not.toBeNull();
    expect(info.input?.h ?? 0).toBeGreaterThanOrEqual(44);
    expect(info.save?.h ?? 0).toBeGreaterThanOrEqual(44);
    expect(info.save?.w ?? 0).toBeGreaterThanOrEqual(44);
    expect(info.clear?.h ?? 0).toBeGreaterThanOrEqual(44);

    // 窄屏也能真的输入（不是被 CSS 盖住 / 不可聚焦）。
    await inputOf(page, 'LING_ONEBOT_API_BASE').fill('http://192.0.2.10:5700');
    expect(await inputOf(page, 'LING_ONEBOT_API_BASE').inputValue()).toBe(
      'http://192.0.2.10:5700',
    );
  }, TIMEOUT);
});

describe('接入器：非 owner 只读', () => {
  it('can_edit=false 时没有保存按钮与「恢复默认」', async (ctx) => {
    if (skip(ctx)) return;
    const body: ConnectorsResponse = {
      ...makeResponse(),
      can_edit: false,
      connectors: [
        makeConnector({
          editable: false,
          status: 'connected',
          configured: true,
          enabled: true,
          fields: onebotFields(),
        }),
      ],
    };
    const { page } = await openConnectors({ width: 1440, height: 1000 }, [], { body });

    await expect.poll(() => page.getByTestId('connectors-readonly').isVisible()).toBe(true);
    const save = page.getByTestId('connector-onebot-save');
    expect(await save.isDisabled()).toBe(true);
    expect(await inputOf(page, 'LING_ONEBOT_API_BASE').isDisabled()).toBe(true);
    expect(await page.getByTestId('connectors-reset').count()).toBe(0);
  }, TIMEOUT);
});
