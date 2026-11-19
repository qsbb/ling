import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const root = fileURLToPath(new URL('..', import.meta.url));
let browser: Browser;
let server: ViteDevServer;
let address = '';
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  server = await createServer({ configFile: false, root, plugins: [react()], logLevel: 'silent', server: { host: '127.0.0.1', port: 0 } });
  await server.listen();
  const socket = server.httpServer?.address();
  if (!socket || typeof socket === 'string') throw new Error('Vite 没有可用端口');
  address = `http://127.0.0.1:${socket.port}`;
  browser = await chromium.launch();
}, 90000);

afterEach(async () => {
  while (contexts.length) await contexts.pop()?.close();
});
afterAll(async () => { await browser?.close(); await server?.close(); });

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

function session(id: string) {
  return {
    schema_version: 'v1', session_id: id, window_id: id, actor_id: 'actor',
    subject_id: 'subject', title: id, channel: 'web', peer_id: null,
    created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z',
    message_count: 1, archived: false, pinned: false,
  };
}

const json = (body: unknown, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });

async function exerciseRace(width: number, order: 'list-first' | 'stale-list-last') {
  const context = await browser.newContext({ viewport: { width, height: 850 } });
  contexts.push(context);
  const page = await context.newPage();
  const duplicateErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().includes('same key')) {
      duplicateErrors.push(message.text());
    }
  });
  const createStarted = deferred();
  const releaseCreate = deferred();
  const listStarted = deferred();
  const releaseList = deferred();
  const finalListStarted = deferred();
  const releaseFinalList = deferred();
  let armList = false;
  let createFinished = false;
  let capturedList = false;
  await page.route('**/*', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/v1/auth/me') return route.fulfill(json({ user: { id: 'u', username: 'test', role: 'owner', subject_id: 'subject', display_name: '用户' }, test_mode: false }));
    if (path === '/v1/sessions' && request.method() === 'POST') {
      createStarted.resolve();
      await releaseCreate.promise;
      createFinished = true;
      return route.fulfill(json(session('new'), 201));
    }
    if (path === '/v1/sessions' && request.method() === 'GET') {
      if (armList && !capturedList) {
        capturedList = true;
        const snapshot = order === 'list-first' ? [session('new'), session('old')] : [session('old')];
        listStarted.resolve();
        await releaseList.promise;
        return route.fulfill(json({ sessions: snapshot }));
      }
      if (createFinished && order === 'list-first') {
        finalListStarted.resolve();
        await releaseFinalList.promise;
      }
      return route.fulfill(json({ sessions: createFinished ? [session('new'), session('old')] : [session('old')] }));
    }
    if (path === '/v1/sessions/old/messages') return route.fulfill(json({ error: { code: 'server_error', message: '模拟读取失败', retriable: true } }, 500));
    if (path === '/v1/sessions/new/messages') return route.fulfill(json({ messages: [], active_turn: null }));
    if (path.startsWith('/v1/')) return route.fulfill(json({ error: { code: 'not_found', message: '目标不存在', retriable: false } }, 404));
    return route.continue();
  });
  await page.goto(`${address}/s/old`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: '重新读取' }).waitFor();
  if (width < 900) await page.getByRole('button', { name: '打开会话列表' }).click();
  await page.getByTestId('new-session').click();
  await createStarted.promise;
  armList = true;
  await page.getByRole('button', { name: '重新读取' }).click();
  await listStarted.promise;

  if (order === 'list-first') {
    releaseList.resolve();
    await page.locator('[data-session-id="new"]').waitFor({ state: 'attached' });
    releaseCreate.resolve();
    await finalListStarted.promise;
    await page.waitForTimeout(100);
    expect(await page.locator('[data-session-id="new"]').count()).toBe(1);
    expect(duplicateErrors).toEqual([]);
    releaseFinalList.resolve();
  } else {
    releaseCreate.resolve();
    await page.waitForURL('**/s/new');
    await page.locator('[data-session-id="new"]').waitFor({ state: 'attached' });
    releaseList.resolve();
    await page.waitForTimeout(100);
    expect(await page.locator('[data-session-id="new"]').count()).toBe(1);
    expect(duplicateErrors).toEqual([]);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

describe('会话列表请求交错', () => {
  for (const width of [375, 1440]) {
    it(`${width}px：列表先包含新会话时不会重复添加`, async () => {
      await exerciseRace(width, 'list-first');
    }, 20000);

    it(`${width}px：较旧列表晚返回时不会覆盖新会话`, async () => {
      await exerciseRace(width, 'stale-list-last');
    }, 20000);
  }
});

it('管理员可在侧栏改渠道会话名并保存手动顺序', async () => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 850 } });
  contexts.push(context);
  const page = await context.newPage();
  const groupKey = 'onebot-default:group:100200300';
  const group = { ...session('group'), title: groupKey, channel: 'onebot', display_name: '渠道群名' };
  const web = { ...session('web'), title: '网页会话' };
  let sessions = [web, group];
  const requests: unknown[] = [];
  await page.route('**/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/v1/auth/me') {
      return route.fulfill(json({
        user: { id: 'u', username: 'test', role: 'owner', subject_id: 'subject' },
        test_mode: false,
      }));
    }
    if (path === '/v1/sessions') return route.fulfill(json({ sessions }));
    if (path === '/v1/sessions/group' && request.method() === 'PATCH') {
      const body = request.postDataJSON() as { title: string };
      requests.push(body);
      group.display_name = body.title;
      return route.fulfill(json(group));
    }
    if (path === '/v1/sessions/group/move' && request.method() === 'POST') {
      requests.push(request.postDataJSON());
      sessions = [group, web];
      return route.fulfill({ status: 204 });
    }
    if (path === '/v1/sessions/group/messages') {
      return route.fulfill(json({ messages: [], active_turn: null }));
    }
    return route.fulfill(json({ error: { code: 'not_found', message: '目标不存在' } }, 404));
  });
  await page.goto(`${address}/s/group`, { waitUntil: 'domcontentloaded' });
  const row = page.locator('[data-session-id="group"]');
  await row.getByRole('button', { name: '改名：渠道群名' }).click();
  await row.getByRole('textbox', { name: '会话名称' }).fill('管理员群名');
  await row.getByRole('button', { name: '保存' }).click();
  await expect.poll(async () => page.getByTestId('session-title').innerText()).toBe('管理员群名');
  expect(await row.locator('.session__subtitle').innerText()).toBe(groupKey);

  await row.getByRole('button', { name: '上移：管理员群名' }).click();
  const ids = () => page.locator('[data-session-id]').evaluateAll(
    (nodes) => nodes.map((node) => node.getAttribute('data-session-id')),
  );
  await expect.poll(ids).toEqual(['group', 'web']);
  expect(requests).toEqual([{ title: '管理员群名' }, { anchor_id: 'web', before: true }]);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(ids).toEqual(['group', 'web']);
}, 20000);
