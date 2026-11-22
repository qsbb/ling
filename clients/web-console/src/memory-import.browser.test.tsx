/**
 * 独立浏览器验收：记忆库 → 导入旧记忆。
 *
 * 判据见 docs/evidence/astrbot-memory-import-web-acceptance.md；这里只测
 * **公开行为**（可访问名称、testid、真实发出的请求），不读内部状态。
 *
 * 全部 /v1 请求由 route 拦截，未知路由一律 404 —— 任何未声明的调用都不会
 * 悄悄落到真实后端。
 */
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const WAIT = 8_000;
const CASE_TIMEOUT = 60_000;
const STAMP = '2026-09-01T10:00:00Z';
const SOURCE = 'astrbot_memory_companion';

let server: ViteDevServer | undefined;
let browser: Browser;
let cacheDir: string | undefined;
let baseUrl: string;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), 'ling-import-vite-'));
  server = await createServer({
    root: ROOT, configFile: false, plugins: [react()], cacheDir, logLevel: 'silent',
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === 'string') throw new Error('Vite 未给出测试端口');
  baseUrl = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch();
}, 120_000);

afterEach(async () => {
  while (contexts.length) await contexts.pop()?.close();
});
afterAll(async () => {
  try {
    await browser?.close();
  } finally {
    try {
      await server?.close();
    } finally {
      if (cacheDir) await rm(cacheDir, { recursive: true, force: true });
    }
  }
});

const json = (body: unknown, status = 200) => ({ status, json: body });
const apiError = (code: string, message: string, status: number, detail: string | null = null) => json({
  schema_version: 'v1',
  error: { code, message, retriable: status >= 500, detail, trace_id: 'trace-mock' },
}, status);

function memory(id: string, text: string, importSource?: Record<string, unknown>) {
  return {
    memory_id: id, text, kind: 'assertion', origin: 'user_statement', visibility: 'subject_private',
    confidence: 0.8, retracted: false, retract_reason: null, supersedes: null, superseded_by: null,
    source_event_ids: [`evt_${id}`], valid_until: null, created_at: STAMP, archived_at: null,
    ...(importSource ? { import_source: importSource } : {}),
  };
}

const DESTINATION = { subject_id: 'subj_owner', display_name: '小林', qq: '100200300', bot_qq: '200300400' };

function preview(overrides: Record<string, unknown> = {}) {
  return {
    total: 5, accepted: 5, duplicates: 0, skipped: 0, skip_reasons: {},
    destination: DESTINATION,
    sample: [
      { source_id: 'mm_1', kind: 'episode', text: '深夜聊项目进度', occurred_at: '2026-08-30T21:15:00Z', created_at: STAMP, valid_until: null },
      { source_id: 'mm_2', kind: 'assertion', text: '不吃香菜', occurred_at: null, created_at: STAMP, valid_until: null },
    ],
    confirmation_token: '1700000000.deadbeef',
    expires_at: '2026-11-21T11:14:00Z',
    ...overrides,
  };
}

interface MockOptions {
  mobile?: boolean;
  memories?: ReturnType<typeof memory>[];
  previewBody?: Record<string, unknown>;
  commitBody?: Record<string, unknown>;
  commitError?: { detail: string; status: number; code: string; message: string };
  previewError?: { detail: string; status: number; code: string; message: string };
  morePages?: boolean;
  waitForPage?: (offset: number) => Promise<void>;
}

async function open(options: MockOptions = {}) {
  const context = await browser.newContext({
    viewport: options.mobile ? { width: 375, height: 760 } : { width: 1366, height: 900 },
    hasTouch: options.mobile, isMobile: options.mobile, reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  contexts.push(context);
  const page = await context.newPage();
  page.setDefaultTimeout(WAIT);
  const reads: URL[] = [];
  const imports: { path: string; contentType: string | null; token: string | null; bytes: number }[] = [];

  await context.route('**/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === 'POST' && path.startsWith('/v1/memory/import/')) {
      const body = request.postDataBuffer();
      imports.push({
        path,
        contentType: request.headers()['content-type'] ?? null,
        token: request.headers()['x-ling-import-token'] ?? null,
        bytes: body?.length ?? 0,
      });
      if (path.endsWith('/preview')) {
        if (options.previewError) {
          const e = options.previewError;
          return route.fulfill(apiError(e.code, e.message, e.status, e.detail));
        }
        return route.fulfill(json(preview(options.previewBody)));
      }
      if (options.commitError) {
        const e = options.commitError;
        return route.fulfill(apiError(e.code, e.message, e.status, e.detail));
      }
      return route.fulfill(json({
        total: 5, imported: 5, duplicates: 0, skipped: 0, skip_reasons: {},
        destination: DESTINATION, ...options.commitBody,
      }));
    }
    if (request.method() === 'GET') {
      reads.push(url);
      switch (path) {
        case '/v1/auth/me':
          return route.fulfill(json({
            schema_version: 'v1', test_mode: false, csrf_token: 't', config_revision: 1,
            user: { id: 'u', username: 'test', role: 'owner', subject_id: 'subj_owner', display_name: '小林', must_change_password: false },
          }));
        case '/v1/version': return route.fulfill(json({ current: '0.0.0' }));
        case '/v1/sessions': return route.fulfill(json({ sessions: [] }));
        case '/v1/memory/purge/status': return route.fulfill(json({ items: [], note: '' }));
        case '/v1/memory': {
          const limit = Number(url.searchParams.get('limit') ?? 50);
          const source = url.searchParams.get('source');
          const offset = Number(url.searchParams.get('offset') ?? 0);
          const rows = options.memories ?? [];
          if (source && source !== SOURCE) return route.fulfill(apiError('invalid_request', '来源不支持', 422));
          await options.waitForPage?.(offset);
          const sliced = rows.slice(offset, offset + limit);
          const hasMore = options.morePages ? offset + limit < rows.length + limit : rows.length > offset + limit;
          return route.fulfill(json({
            memories: sliced, limit, include: null, total: options.morePages ? rows.length + limit : rows.length,
            has_more: hasMore, note: '', ...(source ? { source, offset } : {}),
          }));
        }
      }
    }
    return route.fulfill(apiError('not_found', `${request.method()} ${path}`, 404));
  });

  await page.goto(`${baseUrl}/memory`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('mem-import-entry').waitFor({ state: 'visible' });
  return { page, reads, imports };
}

const FILE = { name: 'old-memories.jsonl', mimeType: 'application/x-ndjson', buffer: Buffer.from('{"format":"ling-astrbot-memory-jsonl"}\n') };

async function previewFile(page: Page, file = FILE) {
  await page.getByTestId('mem-import-entry').click();
  await page.getByTestId('import-file-input').setInputFiles(file);
  await page.getByTestId('import-panel').waitFor({ state: 'visible' });
}

describe('导入旧记忆', () => {
  it('A1/B1/B2/B3/B4：空库也有入口，预览先讲清目标、来源与可导入条数', async () => {
    const { page, imports } = await open({ memories: [] });
    expect(await page.getByTestId('mem-import-entry').isVisible()).toBe(true);
    await previewFile(page);
    expect(await page.getByTestId('import-accepted-value').innerText()).toContain('5');
    expect(await page.getByTestId('import-destination-name').innerText()).toContain('小林');
    expect(await page.getByTestId('import-destination-name').innerText()).toContain('subj_owner');
    const pair = await page.getByTestId('import-source-pair').innerText();
    expect(pair).toContain('100200300');
    expect(pair).toContain('200300400');
    expect(await page.getByTestId('import-sample').count()).toBe(2);
    // 请求体必须是文件原始字节，且 Content-Type 正确
    const [firstImport] = imports;
    expect(firstImport?.contentType).toBe('application/x-ndjson');
    expect(firstImport?.bytes).toBe(FILE.buffer.length);
  }, CASE_TIMEOUT);

  it('识别 AstrBot 原生导出并引导生成可导入的文件', async () => {
    const { page, imports } = await open({
      mobile: true,
      previewError: {
        code: 'invalid_request', status: 422, detail: 'astrbot_native_archive',
        message: '请用 AstrBot 插件的 SQLite 备份生成 Ling 迁移文件',
      },
    });
    await previewFile(page, {
      name: 'memory-companion-20261121-013110.jsonl',
      mimeType: 'text/plain',
      buffer: Buffer.from('{"record_type":"header","format":"astrbot-memory-jsonl","version":1}\n'),
    });
    const error = page.getByTestId('import-error');
    expect(await error.innerText()).toContain('AstrBot 原生导出');
    expect(await error.innerText()).toContain('SQLite 备份');
    expect(await error.getByRole('link', { name: '迁移指南' }).getAttribute('href'))
      .toContain('/docs/ASTRBOT-MEMORY-IMPORT.md');
    expect(await page.getByRole('button', { name: '再试一次' }).count()).toBe(0);
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: '换个文件' }).click();
    expect(await chooser).toBeDefined();
    expect(imports).toHaveLength(1);
  }, CASE_TIMEOUT);

  it('全局上传上限的 413 提示分批转换', async () => {
    const { page } = await open({
      previewError: {
        code: 'invalid_request', status: 413,
        detail: 'Content-Length=33554433 超过上限 33554432 字节',
        message: '请求体过大',
      },
    });
    await previewFile(page);
    expect(await page.getByTestId('import-error').innerText()).toContain('超出服务端上传上限');
    expect(await page.getByTestId('import-error').innerText()).toContain('--limit');
    expect(await page.getByRole('button', { name: '换个文件' }).isVisible()).toBe(true);
  }, CASE_TIMEOUT);

  it('C1：重复与跳过放在次一级；D1：零条时没有确认按钮并给出下一步', async () => {
    const { page } = await open({
      memories: [],
      previewBody: {
        accepted: 0, total: 6, duplicates: 0, skipped: 6,
        skip_reasons: { outside_private_pair: 6 },
        confirmation_token: null, expires_at: null, sample: [],
      },
    });
    await previewFile(page);
    expect(await page.getByTestId('import-confirm').count()).toBe(0);
    const zero = await page.getByTestId('import-zero').innerText();
    expect(zero).toContain('私聊');
    expect(zero).toContain('QQ 绑定');
    expect(await page.getByTestId('import-secondary').isVisible()).toBe(true);
    expect(await page.getByTestId('import-skip-reasons').innerText()).toContain('不属于所选 QQ 私聊');
  }, CASE_TIMEOUT);

  it('E1：显示服务端 imported，而不是预览的 accepted', async () => {
    const { page, imports } = await open({
      memories: [],
      previewBody: { accepted: 5 },
      commitBody: { imported: 4, duplicates: 1 },
    });
    await previewFile(page);
    await page.getByTestId('import-confirm').click();
    await page.getByTestId('import-result-imported').waitFor();
    const imported = await page.getByTestId('import-result-imported').innerText();
    expect(imported).toContain('4');
    expect(imported).not.toContain('5');
    expect(await page.getByTestId('import-result-duplicates').innerText()).toContain('1');
    // 确认请求必须带令牌
    const commit = imports.find((call) => call.path.endsWith('/commit'));
    expect(commit?.token).toBe('1700000000.deadbeef');
    expect(await page.getByTestId('import-accepted-diff').innerText()).toContain('预览时可导入 5 条');
  }, CASE_TIMEOUT);

  it('F1/F2：查看 AstrBot 记忆切到来源，并且能按 offset 翻页', async () => {
    const rows = Array.from({ length: 3 }, (_, i) => memory(`mem_${i}`, `导入条目 ${i}`, {
      source: SOURCE, record_id: `mm_${i}`, memory_type: 'manual_memory', platform: 'aiocqhttp',
      session_id: 'aiocqhttp:FriendMessage:100200300', bot_qq: '200300400', owner_qq: '100200300',
      occurred_at: null, created_at: STAMP, updated_at: STAMP, valid_from: null, valid_to: null,
    }));
    const { page, reads } = await open({ memories: rows, morePages: true });
    await previewFile(page);
    await page.getByTestId('import-confirm').click();
    await page.getByTestId('import-view-imported').waitFor();
    await page.getByTestId('import-view-imported').click();
    await page.waitForTimeout(600);

    const sourceReads = reads.filter((url) => url.searchParams.get('source') === SOURCE);
    expect(sourceReads.length).toBeGreaterThan(0);
    expect(sourceReads.at(0)?.searchParams.get('view')).toBe('library');
    expect(sourceReads.at(0)?.searchParams.get('offset')).toBe('0');
    expect(await page.getByTestId('mem-source').inputValue()).toBe(SOURCE);

    const before = reads.length;
    await page.getByTestId('mem-load-more').click();
    await page.waitForTimeout(600);
    const next = reads.slice(before).find((url) => url.searchParams.get('source') === SOURCE);
    expect(next?.searchParams.get('offset')).toBe('3');
  }, CASE_TIMEOUT);

  it('刷新第一页会结束旧翻页状态，下一次仍能加载剩余记忆', async () => {
    const rows = Array.from({ length: 52 }, (_, index) => memory(`mem_${index}`, `导入条目 ${index}`));
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    const { page, reads } = await open({
      memories: rows,
      async waitForPage(offset) {
        if (offset > 0) await held;
      },
    });
    await page.getByRole('button', { name: '筛选', exact: true }).click();
    await page.getByTestId('mem-source').selectOption(SOURCE);
    const more = page.getByTestId('mem-load-more');
    await more.click();
    try {
      await expect.poll(() => reads.some((url) => url.searchParams.get('offset') === '50')).toBe(true);
      const firstPage = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return response.ok() && url.pathname === '/v1/memory' && url.searchParams.get('offset') === '0';
      });
      await page.getByRole('button', { name: '刷新记忆库', exact: true }).click();
      await firstPage;
      await expect.poll(() => more.isEnabled()).toBe(true);
    } finally {
      release();
    }
    expect(await page.getByTestId('mem-results').locator('li').count()).toBe(50);
    await more.click();
    await expect.poll(() => page.getByTestId('mem-results').locator('li').count()).toBe(52);
    expect(await more.count()).toBe(0);
  }, CASE_TIMEOUT);

  it('零新增时分别报告重复和跳过', async () => {
    const { page } = await open({
      previewBody: { total: 3, accepted: 1, skipped: 2, skip_reasons: { expired: 2 } },
      commitBody: { total: 3, imported: 0, duplicates: 1, skipped: 2, skip_reasons: { expired: 2 } },
    });
    await previewFile(page);
    await page.getByTestId('import-confirm').click();
    expect(await page.getByTestId('import-result-duplicates').innerText()).toBe('1');
    expect(await page.getByTestId('import-result-skipped').innerText()).toBe('2');
    const result = await page.getByTestId('import-zero-result').innerText();
    expect(result).toContain('可展开查看跳过原因');
    expect(result).not.toContain('已经在库里了');
  }, CASE_TIMEOUT);

  it('G1/G2：详情显示来自 AstrBot；occurred_at 为空时不伪造发生时间', async () => {
    const rows = [memory('mem_imported', '不吃香菜', {
      source: SOURCE, record_id: 'mm_2', memory_type: 'manual_memory', platform: 'aiocqhttp',
      session_id: 'aiocqhttp:FriendMessage:100200300', bot_qq: '200300400', owner_qq: '100200300',
      occurred_at: null, created_at: STAMP, updated_at: STAMP, valid_from: null, valid_to: null,
    })];
    const { page } = await open({ memories: rows });
    await page.getByTestId('mem-results').locator('button.mem-record').first().click();
    await page.getByTestId('mem-import-source').waitFor();
    expect(await page.getByTestId('mem-import-source').innerText()).toContain('来自 AstrBot');
    const times = await page.getByTestId('mem-import-times').innerText();
    expect(times).toContain('原创建时间');
    expect(times).not.toContain('发生时间');
  }, CASE_TIMEOUT);

  it('H1：预览失效时保留文件，一步重新预览', async () => {
    const context = await browser.newContext({ viewport: { width: 1366, height: 900 }, reducedMotion: 'reduce' });
    contexts.push(context);
    const page = await context.newPage();
    page.setDefaultTimeout(WAIT);
    let previewCalls = 0;
    const sentTokens: (string | undefined)[] = [];
    await context.route('**/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'POST' && url.pathname.endsWith('/preview')) {
        previewCalls += 1;
        return route.fulfill(json(preview({ confirmation_token: `token-${previewCalls}` })));
      }
      if (request.method() === 'POST' && url.pathname.endsWith('/commit')) {
        sentTokens.push(request.headers()['x-ling-import-token']);
        if (sentTokens.at(-1) === 'token-1') {
          return route.fulfill(apiError('conflict', '预览已失效，请重新预览', 409, 'preview_invalid'));
        }
        return route.fulfill(json({ total: 5, imported: 5, duplicates: 0, skipped: 0, skip_reasons: {}, destination: DESTINATION }));
      }
      if (request.method() === 'GET') {
        if (url.pathname === '/v1/auth/me') {
          return route.fulfill(json({
            schema_version: 'v1', test_mode: false, csrf_token: 't', config_revision: 1,
            user: { id: 'u', username: 'test', role: 'owner', subject_id: 'subj_owner', display_name: '小林', must_change_password: false },
          }));
        }
        if (url.pathname === '/v1/memory') {
          return route.fulfill(json({ memories: [], limit: 50, include: null, total: 0, has_more: false, note: '' }));
        }
        if (url.pathname === '/v1/version') return route.fulfill(json({ current: '0.0.0' }));
        if (url.pathname === '/v1/sessions') return route.fulfill(json({ sessions: [] }));
        if (url.pathname === '/v1/memory/purge/status') return route.fulfill(json({ items: [], note: '' }));
      }
      return route.fulfill(apiError('not_found', 'unknown', 404));
    });
    await page.goto(`${baseUrl}/memory`, { waitUntil: 'domcontentloaded' });
    await page.getByTestId('mem-import-entry').click();
    await page.getByTestId('import-file-input').setInputFiles(FILE);
    await page.getByTestId('import-confirm').click();
    await page.getByTestId('import-error').waitFor();
    expect(await page.getByTestId('import-error').getAttribute('data-error-detail')).toBe('preview_invalid');
    expect(await page.getByTestId('import-confirm').count()).toBe(0);
    expect(await page.getByTestId('import-expiry').count()).toBe(0);
    const before = previewCalls;
    // 不需要重新选文件：点「重新预览」就够
    await page.getByRole('button', { name: '重新预览' }).click();
    await page.getByTestId('import-confirm').waitFor();
    expect(previewCalls).toBe(before + 1);
    await page.getByTestId('import-confirm').click();
    await page.getByTestId('import-result-imported').waitFor();
    expect(sentTokens).toEqual(['token-1', 'token-2']);
  }, CASE_TIMEOUT);

  it('I2：手机 375 宽预览无横向溢出，且能走完导入', async () => {
    const { page } = await open({ mobile: true, memories: [] });
    await page.getByTestId('mem-import-entry').click();
    await page.getByTestId('import-panel').waitFor({ state: 'visible' });
    await page.getByTestId('import-file-input').setInputFiles(FILE);
    await page.getByTestId('import-accepted-value').waitFor();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.getByTestId('import-confirm').click();
    await page.getByTestId('import-result-imported').waitFor();
    expect(await page.getByTestId('import-result-imported').innerText()).toContain('5');
  }, CASE_TIMEOUT);
});
