import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const root = fileURLToPath(new URL('..', import.meta.url));
let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let address = '';
let error: string | null = null;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  try {
    server = await createServer({ configFile: false, root, plugins: [react()], logLevel: 'silent', server: { host: '127.0.0.1', port: 0 } });
    await server.listen();
    const socket = server.httpServer?.address();
    if (!socket || typeof socket === 'string') throw new Error('Vite 没有可用端口');
    address = `http://127.0.0.1:${socket.port}`;
    browser = await chromium.launch();
  } catch (cause) {
    error = cause instanceof Error ? cause.message : String(cause);
  }
}, 90000);

afterEach(async () => {
  while (contexts.length) await contexts.pop()?.close();
});
afterAll(async () => { await browser?.close(); await server?.close(); });

const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });

async function openChat(role: 'owner' | 'member', width: number) {
  if (!browser) throw new Error(error ?? '浏览器未启动');
  const context = await browser.newContext({ viewport: { width, height: 850 } });
  contexts.push(context);
  const page = await context.newPage();
  const requests: Array<{ method: string; body: unknown }> = [];
  let saved = false;
  await page.route('**/*', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (path === '/v1/auth/me') return route.fulfill(json({ user: { id: 'u', username: 'test', role, subject_id: 's', display_name: '用户' }, test_mode: false }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [{ schema_version: 'v1', session_id: 'w', window_id: 'w', actor_id: 'a', subject_id: 's', title: '测试', channel: 'web', peer_id: null, created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z', message_count: 0, archived: false }] }));
    if (path === '/v1/sessions/w/messages') return route.fulfill(json({ messages: [], active_turn: null }));
    if (path === '/v1/sessions/w/dialogue-policy' && req.method() === 'GET') return route.fulfill(json({ field: 'private_may_stay_silent', global_value: true, override: saved ? false : null, effective: !saved, source: saved ? 'session' : 'global', global_revision: 0, session_revision: saved ? 1 : 0 }));
    if (path === '/v1/sessions/w/dialogue-policy/preview' && req.method() === 'POST') {
      requests.push({ method: 'preview', body: req.postDataJSON() });
      return route.fulfill(json({ current: {}, proposed: { override: false, effective: false, source: 'session' }, preview_token: 'test-token', expires_at: 9999999999 }));
    }
    if (path === '/v1/sessions/w/dialogue-policy' && req.method() === 'PUT') {
      requests.push({ method: 'save', body: req.postDataJSON() });
      saved = true;
      return route.fulfill(json({ field: 'private_may_stay_silent', global_value: true, override: false, effective: false, source: 'session', global_revision: 0, session_revision: 1 }));
    }
    if (path.startsWith('/v1/')) return route.fulfill(json({ error: { code: 'not_found', message: '目标不存在', retriable: false } }, 404));
    return route.continue();
  });
  await page.goto(`${address}/s/w`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  return { page, requests };
}

describe('Web 会话对话设置', () => {
  for (const width of [375, 1440]) {
    it(`预览、保存并展示生效来源，宽度 ${width}`, async (ctx) => {
      if (error) { console.warn(error); ctx.skip(); return; }
      const { page, requests } = await openChat('owner', width);
      await page.getByRole('button', { name: '会话设置' }).click();
      const panel = page.getByRole('region', { name: '会话设置' });
      await panel.getByText('当前允许选择沉默 · 来自全局设置').waitFor();
      await panel.getByLabel('私聊回应').selectOption('require');
      await panel.getByRole('button', { name: '预览影响' }).click();
      await panel.getByText('保存后，本会话优先回应，使用本会话设置。').waitFor();
      expect(requests).toEqual([{ method: 'preview', body: { set: { private_may_stay_silent: false }, clear: [] } }]);
      await panel.getByRole('button', { name: '确认保存' }).click();
      await panel.getByText('当前优先回应 · 本会话设置').waitFor();
      expect(requests[1]).toEqual({ method: 'save', body: { set: { private_may_stay_silent: false }, clear: [], preview_token: 'test-token' } });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }, 20000);
  }

  it('成员看不到修改入口', async (ctx) => {
    if (error) { console.warn(error); ctx.skip(); return; }
    const { page } = await openChat('member', 1440);
    expect(await page.getByRole('button', { name: '会话设置' }).count()).toBe(0);
  }, 20000);
});
