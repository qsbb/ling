/**
 * 夜间回顾的真实浏览器验收。
 *
 * 判据在看实现之前就写下来（见下），用真 Chromium + 真 Vite 起页面，
 * 只 mock `/v1` 的网络层——这样验的是"界面遇到这份契约数据会长成什么样"，
 * 而不是"组件按我写的样子渲染"。
 *
 * 五条真实分支，每条都要能独立说清：
 * A. 准备（读取中）：只有加载提示，不提前宣称"没有回顾"。
 * B. 可读 `ok + ready`：条目能读、原话能展开、来源可核对。
 * C. `skipped + ready`：**不显示为失败**，条目照常可读（本文件最重要的一条）。
 * D. 空 `nights=[]`：一条直接的空状态，不说失败。
 * E. 读取失败：说明失败并可重试，不降级成空状态。
 * F. 原话不可再用 `empty`：说清是来源已经不能用，不是读取失败。
 *
 * 截图落在 `LING_SLEEP_SHOT_DIR`（默认 /tmp/ling-sleep-review-shots），
 * 报告里给出绝对路径。
 */
import { mkdir } from 'node:fs/promises';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { chromium, type Browser, type BrowserContext, type Locator, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const WAIT = 8_000;
// 全量并行跑时首屏渲染可能超过 vitest 默认的 5s，与其它浏览器用例一致给宽限。
const CASE_TIMEOUT = 45_000;
const SHOTS = process.env.LING_SLEEP_SHOT_DIR ?? '/tmp/ling-sleep-review-shots';
const REVIEW = '/v1/memory/sleep-review';
const STAMP = '2026-11-22';

let server: ViteDevServer | undefined;
let browser: Browser;
let cacheDir: string | undefined;
let baseUrl: string;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  await mkdir(SHOTS, { recursive: true });
  cacheDir = await mkdtemp(join(tmpdir(), 'ling-sleep-vite-'));
  server = await createServer({
    root: ROOT, configFile: false, plugins: [react()], cacheDir, logLevel: 'silent',
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === 'string') throw new Error('Vite 未给出测试端口');
  baseUrl = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch();
}, 90_000);

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
const error = (code: string, message: string, status: number) => json({
  schema_version: 'v1', error: { code, message, retriable: status >= 500, detail: null, trace_id: 'trace-mock' },
}, status);

function item(id: string, text: string, originals: { speaker: string; text: string; truncated?: boolean }[]) {
  return {
    memory_id: id, text, window_id: `win_${id}`, turn_id: `turn_${id}`,
    original: originals.map(o => ({ speaker: o.speaker, text: o.text, truncated: o.truncated ?? false })),
  };
}

/** 一晚 skipped + ready：没有可合并的东西，但有可回顾的来源。 */
const SKIPPED_READY = {
  sleep_date: STAMP,
  consolidation_state: 'skipped',
  reason: `预计入睡（${STAMP}）后整理过：没有达到合并条件的记忆`,
  groups_merged: 0,
  rows_archived: 0,
  review_status: 'ready',
  items: [
    item('mem_garden', '每周给阳台的山茶浇水', [
      { speaker: 'user', text: '我每周给阳台的山茶浇水' },
    ]),
    item('mem_bridge', '参观过城西的石桥', [
      { speaker: 'user', text: '今天去看了城西那座石桥' },
      { speaker: 'assistant', text: '城西的石桥，记住了' },
    ]),
  ],
};

const OK_READY = {
  sleep_date: '2026-11-21',
  consolidation_state: 'ok',
  reason: '预计入睡（2026-11-21）后整理：合并 2 组、归档 3 条',
  groups_merged: 2,
  rows_archived: 3,
  review_status: 'ready',
  items: [item('mem_tea', '更喜欢喝白茶', [{ speaker: 'user', text: '我更喜欢喝白茶', truncated: true }])],
};

const EMPTY_SCOPE = {
  sleep_date: '2026-11-20',
  consolidation_state: 'ok',
  reason: '预计入睡（2026-11-20）后整理：合并 1 组、归档 1 条',
  groups_merged: 1,
  rows_archived: 1,
  review_status: 'empty',
  items: [],
};

/** 两个窗口：回顾里两条记忆各自来自其中之一。 */
const sessions = [
  {
    session_id: 'win_mem_garden', window_id: 'win_mem_garden', actor_id: 'a', subject_id: 's',
    title: '阳台的花', created_at: STAMP, updated_at: STAMP, message_count: 1,
    archived: false, display_name: '阳台的花', channel: 'web', peer_id: null, pinned: false,
  },
  {
    session_id: 'win_mem_bridge', window_id: 'win_mem_bridge', actor_id: 'a', subject_id: 's',
    title: '城西的石桥', created_at: STAMP, updated_at: STAMP, message_count: 1,
    archived: false, display_name: '城西的石桥', channel: 'web', peer_id: null, pinned: false,
  },
];

interface Options {
  review?: unknown | 'fail' | 'hang';
}

async function openReview(options: Options = {}): Promise<{ page: Page; reads: string[]; queries: string[] }> {
  const context = await browser.newContext({
    viewport: { width: 1366, height: 900 }, reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  contexts.push(context);
  const page = await context.newPage();
  page.setDefaultTimeout(WAIT);
  page.setDefaultNavigationTimeout(2 * WAIT);
  const reads: string[] = [];
  const queries: string[] = [];

  await context.route('**/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() !== 'GET') return route.fulfill(error('not_found', '未声明的写接口', 404));
    reads.push(path);
    if (path === REVIEW) queries.push(url.search.replace(/^\?/, ''));
    switch (path) {
      case '/v1/auth/me':
        return route.fulfill(json({
          schema_version: 'v1', test_mode: false, csrf_token: 'test-csrf', config_revision: 4,
          user: {
            id: 'u', username: 'test-user', role: 'owner', subject_id: 's',
            display_name: '测试用户', must_change_password: false,
          },
        }));
      case '/v1/version': return route.fulfill(json({ current: '0.0.0' }));
      // 会话列表与消息：跳转后聊天页要靠它们真的落到目标会话上，
      // 只断言 URL 变了证明不了"到了正确的那个会话"。
      case '/v1/sessions': return route.fulfill(json({ sessions: sessions }));
      case '/v1/consents': return route.fulfill(json({ schema_version: 'v1', known_scopes: [], consents: [] }));
      case '/v1/memory/purge/status': return route.fulfill(json({ items: [], note: '' }));
      case REVIEW: {
        if (options.review === 'fail') return route.fulfill(error('internal', '模拟读取失败', 500));
        if (options.review === 'hang') return; // 永不 fulfill：停在读取中
        return route.fulfill(json({ schema_version: 'v1', nights: options.review ?? [] }));
      }
    }
    // `/v1/sessions/{id}/messages`：每个窗口给一条能辨认的消息。
    const messageMatch = /^\/v1\/sessions\/([^/]+)\/messages$/.exec(path);
    if (messageMatch) {
      const windowId = decodeURIComponent(messageMatch[1]!);
      const session = sessions.find(s => s.session_id === windowId);
      if (!session) return route.fulfill(error('not_found', '没有这个会话', 404));
      return route.fulfill(json({
        messages: [{
          message_id: `msg_${windowId}`, role: 'user', text: `来自 ${windowId} 的一句话`,
          sequence: 1, turn_id: `turn_${windowId}`, state: 'completed', created_at: STAMP,
          retracted_at: null, retriable: false,
        }],
        active_turn: null,
      }));
    }
    return route.fulfill(error('not_found', `${request.method()} ${path}`, 404));
  });

  await page.goto(`${baseUrl}/memory?tab=review`, { waitUntil: 'domcontentloaded' });
  return { page, reads, queries };
}


/** 这一仓库的浏览器用例都用 vitest 的 expect 配合显式取值，没有 Playwright 断言插件。 */
async function textOf(locator: Locator): Promise<string> {
  await locator.first().waitFor({ state: 'visible', timeout: WAIT });
  return (await locator.first().textContent()) ?? '';
}

async function countOf(locator: Locator): Promise<number> {
  return locator.count();
}

async function shot(page: Page, name: string): Promise<string> {
  const path = join(SHOTS, name);
  await page.screenshot({ path, fullPage: true });
  return path;
}

describe('夜间回顾（真浏览器）', () => {
  it('A 准备：读取中只显示加载，不宣称没有回顾', async () => {
    const { page } = await openReview({ review: 'hang' });
    const review = page.getByTestId('sleep-review');
    expect(await textOf(review)).toContain('正在读取夜间回顾');
    // 负向：加载中不能说成空状态。
    expect(await countOf(review.getByTestId('sleep-empty'))).toBe(0);
    await shot(page, 'A-preparing.png');
  }, CASE_TIMEOUT);

  it('B 可读 ok+ready：条目可读，原话可展开核对', async () => {
    const { page, reads, queries } = await openReview({ review: [SKIPPED_READY, OK_READY] });
    const review = page.getByTestId('sleep-review');
    await review.getByText('每周给阳台的山茶浇水').first().waitFor({ state: 'visible' });
    const whole = await textOf(review);
    expect(whole).toContain('2026 年 11 月 22 日');
    expect(whole).toContain('整理完成');
    expect(whole).toContain('合并 2 组、归档 3 条');
    // 原话默认折叠，展开后能读到原句。
    const summary = review.getByText(/^原话 \d+ 条$/).first();
    await summary.click();
    await review.getByText('我每周给阳台的山茶浇水').first().waitFor({ state: 'visible' });
    // 截断的原话必须标注。
    expect(await textOf(review)).toContain('此处截断');
    // 回顾接口只按当前身份取，不带任何 subject/limit 参数，也不带缓存绕过之外的东西。
    expect(reads.filter(p => p === REVIEW).length).toBeGreaterThan(0);
    expect(queries.filter(q => q.length > 0)).toEqual([]);
    // 截图留在**展开**状态：这才是用户核对原话时的画面。
    await shot(page, 'B-ready-original-expanded.png');
    await summary.click();
    await shot(page, 'B-ready.png');
  }, CASE_TIMEOUT);

  it('C skipped+ready：不显示为失败，条目照常可读', async () => {
    const { page } = await openReview({ review: [SKIPPED_READY] });
    const review = page.getByTestId('sleep-review');
    const state = page.getByTestId('sleep-consolidation');
    await state.first().waitFor({ state: 'visible' });
    expect(await state.first().getAttribute('data-state')).toBe('skipped');
    const stateText = await textOf(state);
    expect(stateText).toContain('当晚不需要合并');
    // 关键负向：不是失败文案，也没有警示样式。
    const whole = await textOf(review);
    expect(whole).not.toContain('整理没做完');
    expect(await state.first().getAttribute('class')).not.toContain('--warn');
    // 正向：条目确实可读。
    await review.getByText('参观过城西的石桥').first().waitFor({ state: 'visible' });
    expect(await countOf(review.getByTestId('sleep-review-note'))).toBe(0);
    await shot(page, 'C-skipped-ready.png');
  }, CASE_TIMEOUT);

  it('D 空：一条直接的空状态', async () => {
    const { page } = await openReview({ review: [] });
    const empty = page.getByTestId('sleep-empty');
    expect(await textOf(empty)).toContain('还没有可看的夜间回顾');
    expect(await countOf(page.getByTestId('sleep-retry'))).toBe(0);
    await shot(page, 'D-empty.png');
  }, CASE_TIMEOUT);

  it('E 读取失败：说失败、可重试，不降级成空状态', async () => {
    const { page } = await openReview({ review: 'fail' });
    const review = page.getByTestId('sleep-review');
    await page.getByTestId('sleep-retry').waitFor({ state: 'visible' });
    expect(await textOf(review)).toContain('夜间回顾没读出来');
    // 负向：不能把"没读到"说成"没有"。
    expect(await countOf(page.getByTestId('sleep-empty'))).toBe(0);
    await shot(page, 'E-failed.png');
  }, CASE_TIMEOUT);

  it('F 原话不可再用：empty 说清是来源已不能用，不是失败', async () => {
    const { page } = await openReview({ review: [EMPTY_SCOPE] });
    const note = page.getByTestId('sleep-review-note');
    const noteText = await textOf(note);
    expect(noteText).toContain('没有可读的条目');
    expect(noteText).toContain('撤回');
    expect(await countOf(page.getByTestId('sleep-items'))).toBe(0);
    expect(await textOf(page.getByTestId('sleep-review'))).not.toContain('夜间回顾没读出来');
    await shot(page, 'F-original-gone.png');
  }, CASE_TIMEOUT);

  it('H 来源会话：两条不同窗口的原话各跳到自己的会话', async () => {
    const { page } = await openReview({ review: [SKIPPED_READY] });

    // 先展开第一条的原话，点它的入口。
    const gardenRow = page.getByTestId('sleep-item-0');
    await gardenRow.getByText(/^原话 \d+ 条$/).click();
    const gardenOpen = gardenRow.getByTestId('sleep-open-source');
    await gardenOpen.first().waitFor({ state: 'visible' });
    expect(await gardenOpen.first().getAttribute('data-window-id')).toBe('win_mem_garden');
    await gardenOpen.first().click();
    // 真的落到那个会话：URL 与页面上的消息都要对得上。
    await page.waitForURL(/\/s\/win_mem_garden$/);
    await page.getByText('来自 win_mem_garden 的一句话').waitFor({ state: 'visible' });
    await shot(page, 'H-source-garden.png');

    // 回来看第二条，必须跳到**另一个**窗口，不是第一条那个。
    await page.goBack();
    await page.waitForURL(/\?tab=review$/);
    const bridgeRow = page.getByTestId('sleep-item-1');
    await bridgeRow.getByText(/^原话 \d+ 条$/).click();
    const bridgeOpen = bridgeRow.getByTestId('sleep-open-source');
    await bridgeOpen.first().waitFor({ state: 'visible' });
    expect(await bridgeOpen.first().getAttribute('data-window-id')).toBe('win_mem_bridge');
    await bridgeOpen.first().click();
    await page.waitForURL(/\/s\/win_mem_bridge$/);
    await page.getByText('来自 win_mem_bridge 的一句话').waitFor({ state: 'visible' });
    await shot(page, 'H-source-bridge.png');
  }, CASE_TIMEOUT);

  it('I 没有原话就没有入口', async () => {
    const { page } = await openReview({
      review: [{
        ...SKIPPED_READY,
        items: [{
          memory_id: 'mem_summary', text: '只有摘要没有原话', window_id: 'win_summary',
          turn_id: 'turn_summary', original: [],
        }],
      }],
    });
    const review = page.getByTestId('sleep-review');
    await review.getByText('这条没有可核对的原话。', { exact: true }).waitFor({ state: 'visible' });
    expect(await countOf(page.getByTestId('sleep-open-source'))).toBe(0);
    await shot(page, 'I-no-original-no-entry.png');
  }, CASE_TIMEOUT);

  it('G 入口：夜间回顾是独立分区，切走再回来仍有内容', async () => {
    const { page } = await openReview({ review: [SKIPPED_READY] });
    const nav = page.getByRole('navigation', { name: '记忆分区', exact: true });
    const link = nav.getByRole('link', { name: '夜间回顾', exact: true });
    expect(await link.getAttribute('aria-current')).toBe('location');
    await nav.getByRole('link', { name: '记忆库', exact: true }).click();
    await page.waitForURL(/\/memory$/);
    await nav.getByRole('link', { name: '夜间回顾', exact: true }).click();
    await page.waitForURL(/\?tab=review$/);
    expect(await textOf(page.getByTestId('sleep-review'))).toContain('每周给阳台的山茶浇水');
    await shot(page, 'G-navigation.png');
  }, CASE_TIMEOUT);
});
