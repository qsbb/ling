/**
 * 「她想撤回这条」的真实浏览器验证（真 Vite + 真 Chromium，`/v1/**` 全模拟）。
 *
 * SSR 测试能证明"画出来是什么"，但证明不了这些：
 * - 点「撤回」真的发出 `POST .../confirm`，且**只发一次**（双击不重复）；
 * - 成功 → 卡片变「已撤回」、按钮消失、目标消息出现「她撤回了这条」；
 * - 失败 → 显示后端 `note` 原文，且页面上**找不到 `reason_code`**；
 * - 「算了」→ `POST .../decline`，卡片变「好，不撤了」；
 * - 列表接口 500 → **只有卡片区**降级，消息照常渲染、输入框还能用；
 * - 375 / 1440 无横向溢出（数字由断言给出，不手写）。
 *
 * 没有 playwright 浏览器时整组标 skipped（不是静默 passed）。
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const CREATED = '2026-11-09T02:00:00Z';

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
}, 90_000);

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
  console.warn(`[recall.browser] 跳过：${launchError}`);
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
};

const SESSION = {
  schema_version: 'v1',
  session_id: 's1',
  actor_id: 'actor_ling',
  subject_id: 'subject-owner',
  window_id: 's1',
  title: '新的对话',
  created_at: CREATED,
  updated_at: CREATED,
  message_count: 3,
  archived: false,
};

function chatMessage(
  messageId: string,
  role: 'user' | 'actor',
  text: string,
  sequence: number,
  extra: Record<string, unknown> = {},
) {
  return {
    schema_version: 'v1',
    message_id: messageId,
    turn_id: `turn-${sequence}`,
    role,
    text,
    state: 'completed',
    created_at: CREATED,
    sequence,
    origin: 'web',
    error_code: null,
    model_label: null,
    ...extra,
  };
}

const MESSAGES = [
  chatMessage('msg-user', 'user', '在吗', 1),
  chatMessage('msg-actor', 'actor', '在。刚才那句是我说错了，我把它收回。', 2),
  // 被撤的这条：正文故意长一点（窄屏下最容易撑破的就是这类）
  chatMessage(
    'msg-target',
    'actor',
    '这是一句很长的话，长到在 375 像素宽的手机上必须换行才放得下，用来验证撤回卡片不会把整页撑出横向滚动条。',
    3,
  ),
];

/**
 * 提议的 `created_at` 必须是**刚刚**。
 *
 * 撤回窗口只有 120 秒：写死一个历史时刻的话，卡片一渲染就是 `expired`，
 * 按钮根本不存在——那种测试只能证明"过期态对"，永远测不到点击链路。
 * 这里每个用例现取当前时刻，与真实运行时一致（提议刚被写下）。
 */
function proposal(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 'v1',
    proposal_id: 'recall_1',
    message_id: 'msg-target',
    window_id: 's1',
    note: '她要撤回这条，确认吗？',
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

interface Harness {
  page: Page;
  calls: string[];
}

/**
 * 打开对话页。
 *
 * `confirmResult` 决定 `POST .../confirm` 的响应；`listStatus` 决定列表接口
 * 的 HTTP 状态（用来验降级）。所有 `/v1/**` 都从这里回，不碰真后端。
 */
async function openChat(
  viewport: { width: number; height: number },
  options: {
    proposals?: unknown[];
    listStatus?: number;
    confirmResult?: { status: number; body: unknown };
    messages?: unknown[];
  } = {},
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const calls: string[] = [];

  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (!path.startsWith('/v1/')) return route.continue();
    calls.push(`${method} ${path}`);

    if (path === '/v1/auth/me') {
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
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.3' }));
    if (path === '/v1/persona') {
      return route.fulfill(json({ name: '灵', display_name: '灵' }));
    }
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [SESSION] }));
    if (path === '/v1/sessions/s1/messages') {
      return route.fulfill(
        json({ messages: options.messages ?? MESSAGES, active_turn: null }),
      );
    }
    if (path === '/v1/sessions/s1/recall-proposals' && method === 'GET') {
      if (options.listStatus && options.listStatus !== 200) {
        return route.fulfill(
          json({ error: { code: 'internal', message: '读了但失败了', retriable: true } }, options.listStatus),
        );
      }
      const body = (options.proposals ?? []).map((item) => item);
      return route.fulfill({
        ...json(body),
        headers: { date: new Date().toUTCString() },
      });
    }
    if (path.endsWith('/confirm') && method === 'POST') {
      const result = options.confirmResult ?? {
        status: 200,
        body: {
          schema_version: 'v1',
          message_id: 'msg-target',
          state: 'recalled',
          note: '撤回了',
          recalled_at: CREATED,
          receipt_id: 'rcpt_1',
          attempts: 1,
          reason_code: null,
        },
      };
      return route.fulfill(json(result.body, result.status));
    }
    if (path.endsWith('/decline') && method === 'POST') {
      return route.fulfill({ status: 204, contentType: 'text/plain', body: '' });
    }
    return route.fulfill(
      json({ error: { code: 'not_found', message: path, retriable: false } }, 404),
    );
  });

  await page.goto(`${baseUrl}/s/s1`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  return { page, calls };
}

/** 整页是否出现横向滚动（1px 容差应付亚像素取整）。 */
async function overflowX(page: Page): Promise<number> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    return doc.scrollWidth - doc.clientWidth;
  });
}

/* ------------------------------------------------------------ 测试 */

describe('真实浏览器：撤回提议卡片', () => {
  const TIMEOUT = 20_000;

  it(
    '列表里有提议 → 卡片出现，挂在被撤那条下面，并带正文摘要',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page } = await openChat(
        { width: 1440, height: 900 },
        { proposals: [proposal()] },
      );
      const card = page.getByTestId('recall-card');
      await card.waitFor();
      expect(await card.count()).toBe(1);
      expect(await card.getAttribute('role')).toBe('group');
      expect(await card.getAttribute('aria-label')).toContain('她想撤回这条');
      // 摘要来自那条消息的正文
      expect(await page.getByTestId('recall-excerpt').innerText()).toContain('这是一句很长的话');
      // 位置：卡片在被撤那条之后
      const order = await page.evaluate(() => {
        const nodes = Array.from(document.querySelectorAll('[data-testid="message-scroll"] *'));
        const target = nodes.findIndex((node) => node.textContent?.includes('这是一句很长的话'));
        const cardAt = nodes.findIndex((node) =>
          node.matches?.('[data-testid="recall-card"]'),
        );
        return { target, cardAt };
      });
      expect(order.cardAt).toBeGreaterThan(order.target);
      // aria-live：状态变化能被读屏播报
      expect(await page.getByTestId('recall-status').getAttribute('aria-live')).toBe('polite');
      expect(await overflowX(page)).toBe(0);
    },
    TIMEOUT,
  );

  it(
    '点「撤回」→ 只发一次 confirm → 卡片变「已撤回」、按钮消失、目标消息标已撤回',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page, calls } = await openChat(
        { width: 1440, height: 900 },
        { proposals: [proposal()] },
      );
      await page.getByTestId('recall-card').waitFor();
      await page.getByTestId('recall-confirm').click();
      await page.getByTestId('message-recalled').waitFor();

      const confirmCalls = calls.filter((call) => call.endsWith('/confirm'));
      expect(confirmCalls).toEqual(['POST /v1/sessions/s1/recall-proposals/recall_1/confirm']);

      await expect.poll(async () => page.getByTestId('recall-status').innerText()).toContain('已撤回');
      expect(await page.getByTestId('recall-confirm').count()).toBe(0);
      expect(await page.getByTestId('recall-decline').count()).toBe(0);
      expect(await page.getByTestId('message-recalled').innerText()).toBe('她撤回了这条');
    },
    TIMEOUT,
  );

  it(
    'confirm 失败 → 显示后端 note 原文，且页面上没有 reason_code',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page } = await openChat(
        { width: 1440, height: 900 },
        {
          proposals: [proposal()],
          confirmResult: {
            status: 200,
            body: {
              schema_version: 'v1',
              message_id: 'msg-target',
              state: 'expired',
              note: '超过平台允许的撤回时间了',
              recalled_at: null,
              receipt_id: null,
              attempts: 0,
              reason_code: 'expired',
            },
          },
        },
      );
      await page.getByTestId('recall-card').waitFor();
      await page.getByTestId('recall-confirm').click();
      await expect
        .poll(async () => page.getByTestId('recall-status').innerText())
        .toContain('已经来不及撤了');
      const body = await page.locator('body').innerText();
      expect(body).not.toContain('reason_code');
      expect(body).not.toContain('"expired"');
    },
    TIMEOUT,
  );

  it(
    '点「算了」→ 发 decline → 卡片变「好，不撤了」',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page, calls } = await openChat(
        { width: 1440, height: 900 },
        { proposals: [proposal()] },
      );
      await page.getByTestId('recall-card').waitFor();
      await page.getByTestId('recall-decline').click();
      await expect
        .poll(async () => page.getByTestId('recall-status').innerText())
        .toBe('好，不撤了');
      expect(calls.filter((call) => call.endsWith('/decline'))).toEqual([
        'POST /v1/sessions/s1/recall-proposals/recall_1/decline',
      ]);
      expect(await page.getByTestId('recall-confirm').count()).toBe(0);
    },
    TIMEOUT,
  );

  it(
    '列表接口 500 → 只有卡片区降级，聊天照常（消息在、输入框能用）',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page } = await openChat(
        { width: 1440, height: 900 },
        { listStatus: 500 },
      );
      await page.getByTestId('recall-unavailable').waitFor();
      expect(await page.getByTestId('recall-unavailable').innerText()).toContain('暂时读不到撤回提议');
      // 消息与输入框都不受影响
      expect(await page.getByText('这是一句很长的话').count()).toBeGreaterThan(0);
      expect(await page.locator('textarea').count()).toBe(1);
      // 没有变成整页错误
      expect(await page.locator('.notice--error').count()).toBe(0);
    },
    TIMEOUT,
  );

  it(
    '超过 120 秒的提议：按钮不渲染，文案「已经来不及撤了」',
    async (ctx) => {
      if (skip(ctx)) return;
      const stale = new Date(Date.now() - 121_000).toISOString();
      const { page, calls } = await openChat(
        { width: 1440, height: 900 },
        { proposals: [proposal({ created_at: stale })] },
      );
      await page.getByTestId('recall-card').waitFor();
      await expect
        .poll(async () => page.getByTestId('recall-status').innerText())
        .toContain('已经来不及撤了');
      // **不许**给出一个点了必失败的按钮
      expect(await page.getByTestId('recall-confirm').count()).toBe(0);
      expect(await page.getByTestId('recall-decline').count()).toBe(0);
      expect(calls.filter((call) => call.endsWith('/confirm'))).toEqual([]);
    },
    TIMEOUT,
  );

  for (const viewport of [
    { width: 375, height: 720 },
    { width: 1440, height: 900 },
  ]) {
    it(
      `${viewport.width}px：卡片与已撤回气泡都不横向溢出`,
      async (ctx) => {
        if (skip(ctx)) return;
        const { page } = await openChat(viewport, {
          proposals: [
            proposal(),
            proposal({ proposal_id: 'recall_2', message_id: 'msg-missing' }),
          ],
          messages: [
            ...MESSAGES,
            chatMessage('msg-recalled', 'actor', '这条已经被撤回了，正文仍然保留。', 4, {
              recalled_at: CREATED,
            }),
          ],
        });
        await page.getByTestId('recall-card').first().waitFor();
        expect(await page.getByTestId('recall-card').count()).toBe(2);
        expect(await page.getByTestId('message-recalled').count()).toBe(1);
        expect(await overflowX(page)).toBe(0);

        // 1440 下顺手留一张截图（给 lead 看形状），只截对话区
        if (viewport.width === 1440) {
          await page.screenshot({ path: '/tmp/recall-proposal-1440.png', fullPage: false });
        }
      },
      TIMEOUT,
    );
  }
});
