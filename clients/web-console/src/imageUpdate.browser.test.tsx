import { fileURLToPath } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { chromium, type Browser, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
let browser: Browser | null = null;
let server: ViteDevServer;
let baseUrl: string;
let launchError: string | null = null;

beforeAll(async () => {
  server = await createServer({
    configFile: false,
    root: ROOT,
    plugins: [react()],
    logLevel: 'silent',
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === 'string') throw new Error('Vite 未提供监听地址');
  baseUrl = `http://127.0.0.1:${address.port}`;
  try {
    browser = await chromium.launch();
  } catch (error) {
    launchError = error instanceof Error ? error.message : String(error);
  }
}, 120_000);

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

async function openUpdatePage(
  page: Page,
  state: string,
  stage: string,
  options: { queued?: boolean; noTask?: boolean } = {},
) {
  await page.route('**/*', async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path === '/v1/auth/me') {
      body = {
        schema_version: 'v1',
        user: {
          id: 'user-demo', username: 'demo', role: 'owner', subject_id: 'subject-demo',
          display_name: '用户', must_change_password: false,
        },
        config_revision: 1,
        test_mode: false,
        csrf_token: 'csrf',
      };
    } else if (path === '/v1/version/image-update') {
      body = {
        schema_version: 'v1', state: 'latest', message: '当前已是最新', checked_at: null,
        current: { version: '0.0.8', commit: null, digest: null }, newer: [], target: null,
        request: options.queued ? { request_id: '0123456789abcdef0123456789abcdef' } : null,
        task: options.noTask ? null : {
          state, stage, version: '0.0.8', failed_code: null,
          started_at: '2026-11-14T00:00:00Z', finished_at: '2026-11-14T00:05:00Z',
        },
        details: { catalog: {}, updater: {}, backup_gate: null },
      };
    } else if (path === '/v1/version') {
      body = { current: '0.0.8' };
    } else if (path === '/v1/persona') {
      body = { name: '灵', display_name: '灵' };
    } else if (path === '/v1/sessions') {
      body = { sessions: [] };
    } else if (path.startsWith('/v1/')) {
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    } else {
      return route.continue();
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(`${baseUrl}/settings/about`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('image-update-panel').waitFor();
}

describe('更新任务进度入口', () => {
  for (const noTask of [true, false]) {
    it(`排队时显示等待状态，${noTask ? '没有旧任务' : '保留旧任务'}`, async (ctx) => {
      if (!browser) {
        console.warn(`[imageUpdate.browser] 浏览器不可用：${launchError}`);
        ctx.skip();
        return;
      }
      const page = await browser.newPage();
      try {
        await openUpdatePage(page, 'succeeded', 'verify', { queued: true, noTask });
        await page.getByTestId('image-update-primary').click();
        const detail = page.getByTestId('image-update-task-detail');
        await expect.poll(() => detail.innerText()).toContain('正在等待更新器开始');
        expect(await detail.innerText()).not.toContain('用时 5 分钟');
        expect(await detail.locator('[data-progress-state="done"]').count()).toBe(0);
      } finally {
        await page.close();
      }
    }, 30_000);
  }

  for (const state of ['succeeded', 'failed_not_started', 'needs_manual']) {
    it(`${state} 的任务刷新后仍可查看阶段及固定耗时`, async (ctx) => {
      if (!browser) {
        console.warn(`[imageUpdate.browser] 浏览器不可用：${launchError}`);
        ctx.skip();
        return;
      }
      const page = await browser.newPage();
      try {
        await openUpdatePage(page, state, 'backup');
        for (let i = 0; i < 2; i += 1) {
          const toggle = page.getByTestId('image-update-last-progress');
          await toggle.click();
          await expect.poll(() => page.getByTestId('image-update-elapsed').textContent())
            .toContain('用时 5 分钟');
          if (state !== 'succeeded') {
            await expect.poll(() => page.locator('[data-progress-state="stopped"]').count()).toBe(1);
          }
          await page.reload({ waitUntil: 'domcontentloaded' });
          await page.getByTestId('image-update-panel').waitFor();
        }
      } finally {
        await page.close();
      }
    }, 30_000);
  }

  it('运行中阶段未知时不伪造阶段进度', async (ctx) => {
    if (!browser) {
      console.warn(`[imageUpdate.browser] 浏览器不可用：${launchError}`);
      ctx.skip();
      return;
    }
    const page = await browser.newPage();
    try {
      await openUpdatePage(page, 'running', 'unknown_stage');
      await page.getByTestId('image-update-primary').click();
      await expect.poll(() => page.getByTestId('image-update-task-detail').innerText())
        .toContain('阶段信息未知');
      expect(await page.locator('[data-progress-state]').count()).toBe(0);
    } finally {
      await page.close();
    }
  }, 30_000);
});
