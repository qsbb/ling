/**
 * 独立验收判据（先定行为，再看候选实现）：
 * 1. 有命中也有漏在批次外的记录：能查、能看，计数是已加载量；未命中不等于全库无结果。
 * 2. 四个链接可导航且有唯一当前项。长期库、核心、学习和隐私各自有正向内容；核心按需取数。
 * 3. 先填再取消不写；确认才写正确的记录与内容。切分区回来仍有未提交草稿。
 * 4. 桌面用 Tab/Enter 找到并打开记录；触屏窄屏能查看并返回，不横向撑开。
 * 5. 两种历史筛选都能取数；旧请求最后结束时，不能拿走新筛选已经显示的归档结果。
 * 6. 长期、核心、学习读取分别失败时，当前分区能解释失败，其他分区仍可读。
 * 只用公开 /v1 契约与约定的可访问名称/test id。mock 不验证真实后端或真实持久化。
 */
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { chromium, type Browser, type BrowserContext, type Locator, type Page, type Request } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import type { MemoryObject, MemoryRecord } from './types';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const WAIT = 8_000;
const CASE_TIMEOUT = 45_000;
const LIST = '/v1/memory';
const CORE = '/v1/core-memory';
const CONFIG = '/v1/settings/config';
const STAMP = '2026-11-03T00:00:00Z';
const TABS = {
  '记忆库': '/memory',
  '核心记忆': '/memory?tab=core',
  '学习设置': '/memory?tab=settings',
  '隐私与清理': '/memory?tab=privacy',
} as const;
type Tab = keyof typeof TABS;

let server: ViteDevServer | undefined;
let browser: Browser;
let cacheDir: string | undefined;
let baseUrl: string;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), 'ling-memory-workspace-vite-'));
  server = await createServer({
    root: ROOT, configFile: false, plugins: [react()], cacheDir, logLevel: 'silent',
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === 'string') throw new Error('Vite 未给出测试端口');
  baseUrl = `http://127.0.0.1:${address.port}`;
  // 浏览器启动失败应红，不跳过整套独立验收。
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

function memory(id: string, text: string) {
  return {
    memory_id: id, text, kind: 'assertion', origin: 'user_statement', visibility: 'subject_private',
    confidence: 0.82, retracted: false, retract_reason: null as string | null,
    supersedes: null as string | null, superseded_by: null as string | null,
    source_event_ids: [`evt_${id}`], valid_until: null, created_at: STAMP,
    archived_at: null as string | null,
  };
}

const FAVORITE = memory('mem_tea', '周末想喝桂花茶');
const WALK = memory('mem_walk', '傍晚喜欢到河边散步');
const RETRACTED = { ...memory('mem_old', '以前用蓝色水杯'), retracted: true, retract_reason: '换了杯子' };
const ARCHIVED = { ...memory('mem_archived', '去年的窗边绿植'), archived_at: STAMP };
const OUTSIDE_BATCH = memory('mem_older', '小时候做过纸风车');
const CORE_ITEM = {
  core_id: 'core_boundary', label: '称呼约定', text: '请不要在群聊里喊我的真名',
  priority: 1, scope: 'private', origin_kind: 'user_statement', created_at: STAMP, updated_at: STAMP,
};
const RETENTION = 137;
const SETTINGS = {
  schema_version: 'v1', revision: 4, test_mode: false, providers: [], profiles: [],
  revisions: [], slots: [], profile_resolutions: [], known_profiles: [], known_kinds: [],
  profile_capabilities: {}, memory_sources: { 'memory.embed': 'none', 'memory.rerank': 'none' },
  memory: {
    relative_score_floor: 0.35, rerank_top_n: 7, recency_half_life_days: 19, max_retention_days: RETENTION,
    max_injected_memories: 3,
  },
};

const json = (body: unknown, status = 200) => ({ status, json: body });
const error = (code: string, message: string, status: number) => json({
  schema_version: 'v1', error: { code, message, retriable: status >= 500, detail: null, trace_id: 'trace-mock' },
}, status);

interface MockOptions {
  start?: Tab;
  mobile?: boolean;
  viewport?: { width: number; height: number };
  /** 旧用例默认切到列表；`default` 不切换，用来验证产品默认星图。 */
  view?: 'stars' | 'list' | 'default';
  largeBatch?: boolean;
  memories?: MemoryRecord[];
  fail?: typeof LIST | typeof CORE | typeof CONFIG;
  /**
   * 回到「服务端搜索之前」的行为：只按 `limit` 截断，**忽略 `q`/`offset`**。
   * 用来证明浏览器用例确实依赖服务端搜索——旧行为下它们必须红。
   */
  ignoreServerSearch?: boolean;
  waitForList?: (request: Request, include: string | null) => Promise<void> | undefined;
  /** 卡住某个搜索词的**第一次**响应，用来构造「旧查询的慢响应晚到」。 */
  holdFirst?: string;
  /** 由测试提供并释放的闸门；不传就用自己的内部闸门（永远不会放行）。 */
  holdGate?: Promise<void>;
}

async function openWorkspace(options: MockOptions = {}) {
  const context = await browser.newContext({
    viewport: options.viewport ?? (options.mobile ? { width: 375, height: 760 } : { width: 1366, height: 900 }),
    hasTouch: options.mobile, isMobile: options.mobile, reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  contexts.push(context);
  const page = await context.newPage();
  page.setDefaultTimeout(WAIT);
  page.setDefaultNavigationTimeout(2 * WAIT);
  const reads: URL[] = [];
  // `holdSearch` 用来把某个搜索词的那条响应卡在半空，构造「旧查询晚到」。
  const held = gate();
  let heldOnce = false;
  /** 被 `holdFirst` 卡住的那条请求：测试要等它真的结束，而不是等"有请求结束"。 */
  const heldRequests: Request[] = [];
  let heldSettled = false;
  let releaseSettled!: () => void;
  const settledDone = new Promise<void>(resolve => { releaseSettled = resolve; });
  const writes: { method: string; path: string; body: unknown }[] = [];
  let rows: MemoryRecord[] = options.memories ?? [FAVORITE, WALK, RETRACTED, ARCHIVED];
  const labeled = new Map<string, { object: MemoryObject; memoryId: string }>();
  if (options.largeBatch) {
    rows.push(...Array.from({ length: 202 }, (_, i) => memory(`mem_sample_${i}`, `批次样本 ${i + 1}`)), OUTSIDE_BATCH);
  }
  await context.route('**/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (method === 'GET') {
      reads.push(url);
      if (path === options.fail) return route.fulfill(error('internal', '模拟读取失败', 500));
      switch (path) {
        case '/v1/auth/me':
          return route.fulfill(json({
            schema_version: 'v1', test_mode: false, csrf_token: 'test-csrf', config_revision: 4,
            user: { id: 'u', username: 'test-user', role: 'owner', subject_id: 's', display_name: '测试用户', must_change_password: false },
          }));
        case '/v1/version': return route.fulfill(json({ current: '0.0.0' }));
        case '/v1/sessions': return route.fulfill(json({ sessions: [] }));
        case CORE: return route.fulfill(json({ items: [CORE_ITEM], limit: 20, note: '' }));
        case CONFIG: return route.fulfill(json(SETTINGS));
        case '/v1/consents': return route.fulfill(json({ schema_version: 'v1', known_scopes: [], consents: [] }));
        case '/v1/memory/purge/status': return route.fulfill(json({ items: [], note: '' }));
        case '/v1/memory/objects': return route.fulfill(json({
          objects: [...labeled.values()].map(({ object }) => object),
        }));
        case LIST: {
          const limit = Number(url.searchParams.get('limit') ?? 50);
          const include = url.searchParams.get('include');
          const q = url.searchParams.get('q');
          const offset = Number(url.searchParams.get('offset') ?? 0);
          const objectId = url.searchParams.get('object_id');
          if (!Number.isInteger(limit) || limit < 1 || limit > 200 ||
              (include !== null && !['retracted', 'archived', 'all'].includes(include)) ||
              (q !== null && q.length > 200) || !Number.isInteger(offset) || offset < 0) {
            return route.fulfill(error('invalid_request', '无效的列表参数', 422));
          }
          const filtered = rows.filter(row =>
            (!objectId || row.memory_id === labeled.get(objectId)?.memoryId) &&
            (!row.retracted || include === 'retracted' || include === 'all') &&
            (!row.archived_at || include === 'archived' || include === 'all'));
          /*
            契约（docs/API-WEB-V1.md「记忆库搜索与导入来源分页」）：`q` 按空白分词，
            每个词都要出现，忽略大小写与全半角；`total` 是全量命中数；
            `has_more = total > offset + limit`；有 `q` 时回显 `offset`。
          */
          const terms = options.ignoreServerSearch || !q
            ? []
            : q.normalize('NFKC').toLowerCase().split(/\s+/u).filter(Boolean);
          const matches = terms.length === 0 ? filtered : filtered.filter(item =>
            terms.every(term => item.text.normalize('NFKC').toLowerCase().includes(term)));
          const total = matches.length;
          const page_ = options.ignoreServerSearch ? matches.slice(0, limit) : matches.slice(offset, offset + limit);
          const payload: Record<string, unknown> = {
            memories: page_, limit, include, total,
            has_more: total > offset + limit, note: '',
          };
          if (terms.length > 0 || options.ignoreServerSearch) payload.offset = options.ignoreServerSearch ? 0 : offset;
          if (terms.length > 0) payload.q = q;
          // 快照在请求到达时取，控制的是它何时返回，不是修改当前服务器状态。
          await options.waitForList?.(request, include);
          if (q && q === options.holdFirst && !heldOnce) {
            heldOnce = true;
            heldRequests.push(request);
            await (options.holdGate ?? held.promise);
            // 落到这里 = serve 端已经放行；浏览器可能还在解析，再让测试等两帧。
            heldSettled = true;
            releaseSettled();
            try {
              return await route.fulfill(json(payload));
            } catch (e) {
              if (!request.failure() && !page.isClosed()) throw e;
              return;
            }
          }
          try {
            return await route.fulfill(json(payload));
          } catch (e) {
            // 中止旧 fetch 也是正确实现；其余拦截错误不能当成通过。
            if (!request.failure() && !page.isClosed()) throw e;
            return;
          }
        }
      }
    } else {
      const body: unknown = request.postDataJSON();
      writes.push({ method, path, body });
      if (method === 'POST' && path.endsWith('/objects') && body && typeof body === 'object'
          && 'kind' in body && typeof body.kind === 'string' && 'name' in body && typeof body.name === 'string') {
        const target = rows.find(record => path === `${LIST}/${record.memory_id}/objects`);
        if (!target || !target.text.includes(body.name)) {
          return route.fulfill(error('invalid_request', '没有原话依据', 422));
        }
        const entity_id = 'ent_browser_object';
        labeled.set(entity_id, {
          memoryId: target.memory_id,
          object: { entity_id, kind: body.kind, name: body.name, aliases: [], count: 1, last_recorded_at: STAMP },
        });
        return route.fulfill(json({ entity_id }));
      }
      if (method === 'POST' && path === `${LIST}/${FAVORITE.memory_id}/supersede` &&
          body && typeof body === 'object' && 'text' in body && typeof body.text === 'string' && body.text) {
        const corrected = { ...memory('mem_corrected', body.text), supersedes: FAVORITE.memory_id };
        rows = [corrected, ...rows.map(row => row.memory_id === FAVORITE.memory_id ?
          { ...row, retracted: true, superseded_by: corrected.memory_id } : row)];
        return route.fulfill(json({ memory_id: corrected.memory_id, supersedes: FAVORITE.memory_id }));
      }
      if (method === 'POST' && path === `${LIST}/${FAVORITE.memory_id}/retract` &&
          body && typeof body === 'object' && 'reason' in body && typeof body.reason === 'string' && body.reason) {
        const reason = body.reason;
        rows = rows.map(row => row.memory_id === FAVORITE.memory_id ?
          { ...row, retracted: true, retract_reason: reason } : row);
        return route.fulfill(json({ memory_id: FAVORITE.memory_id, retracted: true }));
      }
    }
    // 默认拒绝，未声明的 API 不会偷偷落到 Vite 代理、后台或数据库。
    return route.fulfill(error('not_found', `${method} ${path}`, 404));
  });
  await page.goto(`${baseUrl}${TABS[options.start ?? '记忆库']}`, { waitUntil: 'domcontentloaded' });
  const view = options.view ?? 'list';
  if ((options.start ?? '记忆库') === '记忆库' && view === 'list') {
    const listButton = page.getByRole('button', { name: '列表', exact: true });
    await listButton.waitFor({ state: 'visible' });
    await listButton.click();
    await expect.poll(() => listButton.getAttribute('aria-pressed'), { timeout: WAIT }).toBe('true');
  }
  return { page, reads, writes, heldRequests, heldDone: () => heldSettled, settledDone };
}

const nav = (page: Page) => page.getByRole('navigation', { name: '记忆分区', exact: true });
const tabLink = (page: Page, tab: Tab) => nav(page).getByRole('link', { name: tab, exact: true });
const search = (page: Page) => page.getByRole('searchbox', { name: '搜索记忆库', exact: true });
const results = (page: Page) => page.getByTestId('mem-results');
const row = (page: Page, item: { text: string }) => results(page).getByRole('button', { name: item.text });
const detail = (page: Page) => page.getByTestId('mem-inspector');
const detailHeading = (page: Page) => detail(page).getByRole('heading', { name: '记忆详情', exact: true });
const coreText = (page: Page) => page.getByTestId('mem-core').getByText(CORE_ITEM.text, { exact: true });
const privacyControl = (page: Page) => page.getByRole('button', { name: /遗忘全部|(?:清空|清除|删除|清理)全部/ });
const retentionField = (page: Page) => page.getByRole('spinbutton', { name: /^最长保留天数/ })
  .or(page.getByRole('textbox', { name: /^最长保留天数/ }));
const isCurrent = (value: string | null) => !!value && value !== 'false';
const readsAt = (reads: URL[], endpoint: string) => reads.filter(url => url.pathname === endpoint);

async function visible(locator: Locator) {
  await locator.waitFor({ state: 'visible', timeout: WAIT });
}
async function hidden(locator: Locator) {
  await locator.waitFor({ state: 'hidden', timeout: WAIT });
}
async function current(page: Page, name: Tab) {
  await expect.poll(() => `${new URL(page.url()).pathname}${new URL(page.url()).search}`, { timeout: WAIT })
    .toBe(TABS[name]);
  await expect.poll(async () => {
    const links = Object.keys(TABS) as Tab[];
    const states = await Promise.all(links.map(tab => tabLink(page, tab).getAttribute('aria-current')));
    return links.filter((_, index) => isCurrent(states[index] ?? null));
  }, { timeout: WAIT }).toEqual([name]);
}
async function goToTab(page: Page, name: Tab) {
  await tabLink(page, name).click();
  await current(page, name);
}

function scopeMessage(page: Page) {
  // 范围是可见文本，不接受只放在 aria-label 里的「已加载」。不锁死整句文案。
  return page.getByText(/(?:仅|只|范围|限于|搜索).{0,22}(?:已加载|本批|当前批|当前列表)|(?:已加载|本批|当前批|当前列表).{0,22}(?:内搜索|搜索范围|内查找)/)
    .filter({ visible: true }).first();
}

async function historyToggle(page: Page, name: '显示已撤回' | '显示已归档') {
  const opts = { name, exact: true };
  const control = page.getByRole('checkbox', opts).or(page.getByRole('menuitemcheckbox', opts))
    .or(page.getByRole('switch', opts)).or(page.getByRole('button', opts)).filter({ visible: true });
  if (!await control.isVisible()) await page.getByRole('button', { name: '筛选', exact: true }).click();
  await control.click();
}

function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

async function paint(page: Page) {
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

async function tabTo(page: Page, target: Locator, description: string) {
  await visible(target);
  for (let steps = 0; steps < 30 && !await target.evaluate(el => el === document.activeElement); steps++) {
    await page.keyboard.press('Tab');
  }
  expect(await target.evaluate(el => el === document.activeElement), description).toBe(true);
}

async function notWiderThanViewport(page: Page, width: number) {
  const widths = await page.evaluate(() => ({
    css: document.documentElement.clientWidth,
    content: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
  }));
  expect(widths.css, '应使用指定的 CSS 视口，不能靠缩放把桌面页塞进去').toBe(width);
  expect(widths.content, '页面不应横向溢出').toBeLessThanOrEqual(width + 1);
}
async function notWiderThanMobileViewport(page: Page) {
  await notWiderThanViewport(page, 375);
}


/* ------------------------------------------------------- 星图：虚构记忆夹具 */

function importedMemory(
  id: string,
  text: string,
  created: string,
  occurred: string | null,
  extra: Partial<MemoryRecord> = {},
): MemoryRecord {
  return {
    ...memory(id, text),
    created_at: created,
    import_source: {
      source: 'astrbot_memory_companion',
      record_id: `rec_${id}`,
      memory_type: 'event',
      platform: 'qq',
      session_id: 'session_shared',
      bot_qq: '100200300',
      owner_qq: '100200301',
      occurred_at: occurred,
      created_at: created,
      updated_at: created,
      valid_from: null,
      valid_to: null,
    },
    ...extra,
  };
}

const STAR_DRAWING = importedMemory(
  'star_drawings', '在河畔展馆领取展览明信片',
  '2026-06-01T08:00:00Z', '2024-05-12T10:00:00Z',
  { kind: 'episode', source_event_ids: ['evt_drawings'] },
);
const STAR_POSTER = importedMemory(
  'star_poster', '两年后又看到同一展览的海报',
  '2026-06-02T08:00:00Z', '2026-05-18T10:00:00Z',
  { kind: 'episode', source_event_ids: ['evt_drawings'] },
);
const STAR_MAY_A = importedMemory(
  'star_may_a', '五月喜欢坐在窗边',
  '2026-06-03T08:00:00Z', '2026-05-02T09:00:00Z',
  { kind: 'assertion', source_event_ids: ['evt_may_a'] },
);
const STAR_MAY_B = importedMemory(
  'star_may_b', '五月把杯子换成白色',
  '2026-06-04T08:00:00Z', '2026-05-25T09:00:00Z',
  { kind: 'assertion', source_event_ids: ['evt_may_b'] },
);
const STAR_SAME_PLACE = importedMemory(
  'star_same_place', '河畔展馆五月调整开放时间',
  '2026-06-05T08:00:00Z', '2026-05-10T09:00:00Z',
  { kind: 'assertion', source_event_ids: ['evt_place_only'] },
);
const STAR_UNKNOWN = importedMemory(
  'star_unknown', '小时候做过纸风车',
  '2026-09-01T08:00:00Z', null,
  { kind: 'episode', source_event_ids: ['evt_unknown'] },
);
const STAR_REGULAR: MemoryRecord = {
  ...memory('star_regular', '没有发生时间的普通记忆'),
  created_at: '2026-11-03T10:00:00Z',
  source_event_ids: ['evt_regular'],
};
const STAR_OLD: MemoryRecord = {
  ...memory('star_old', '以前记成周三闭馆'),
  created_at: '2025-01-10T10:00:00Z',
  superseded_by: 'star_new',
  source_event_ids: ['evt_old'],
};
const STAR_NEW: MemoryRecord = {
  ...memory('star_new', '后来纠正为周四闭馆'),
  created_at: '2025-02-10T10:00:00Z',
  supersedes: 'star_old',
  source_event_ids: ['evt_new'],
};
const STAR_DENSE: MemoryRecord[] = Array.from({ length: 12 }, (_, index) => ({
  ...memory(`star_dense_${index}`, `密集记录 ${index + 1}`),
  created_at: '2026-05-05T12:00:00Z',
  source_event_ids: [`evt_dense_${index}`],
}));
const STAR_ROWS: MemoryRecord[] = [
  STAR_DRAWING, STAR_POSTER, STAR_MAY_A, STAR_MAY_B, STAR_SAME_PLACE,
  STAR_UNKNOWN, STAR_REGULAR, STAR_OLD, STAR_NEW, ...STAR_DENSE,
];

const SHOTS = fileURLToPath(new URL('../../../docs/evidence/memory-star-map/', import.meta.url));

describe('记忆工作区：独立浏览器验收', () => {
  it('从记忆详情标记物件后按对象浏览，独立保留其余记录', async () => {
    const record = memory('mem_album', '阿禾把蓝封画册借给她。');
    const { page, reads, writes } = await openWorkspace({
      memories: [FAVORITE, record], viewport: { width: 320, height: 760 }, mobile: true,
    });
    await row(page, record).click();
    await detail(page).getByText('标记对象', { exact: true }).click();
    const form = detail(page).getByRole('form', { name: '标记记忆对象' });
    await form.getByLabel('对象名称').fill('红封画册');
    expect(await form.getByRole('button', { name: '保存对象' }).isEnabled()).toBe(false);
    await form.getByLabel('对象名称').fill('蓝封画册');
    await form.getByLabel('对象类型').selectOption('item');
    await form.getByRole('button', { name: '保存对象' }).click();
    await expect.poll(() => writes).toEqual([{
      method: 'POST', path: '/v1/memory/mem_album/objects', body: { kind: 'item', name: '蓝封画册' },
    }]);
    await detail(page).getByRole('button', { name: '回到列表' }).click();
    await page.getByRole('button', { name: '筛选', exact: true }).click();
    const selector = page.getByLabel('按对象看', { exact: true });
    await expect.poll(() => selector.locator('optgroup[label="物件"] option').count(), { timeout: WAIT }).toBe(1);
    await selector.selectOption('ent_browser_object');
    await visible(row(page, record));
    await expect.poll(() => readsAt(reads, LIST).at(-1)?.searchParams.get('object_id')).toBe('ent_browser_object');
    await expect.poll(() => row(page, FAVORITE).count()).toBe(0);
    await notWiderThanViewport(page, 320);
    await page.getByTestId('mem-object-active').getByRole('button', { name: '清除' }).click();
    await visible(row(page, FAVORITE));
  }, CASE_TIMEOUT);

  it('搜索走服务端：能找到第一页之外的旧条目，计数与翻页如实反映命中量', async () => {
    const { page, reads } = await openWorkspace({ largeBatch: true });
    await visible(row(page, FAVORITE));
    await visible(row(page, WALK));
    const initialReads = readsAt(reads, LIST).map(url => url.search);
    expect(initialReads.length).toBeGreaterThan(0);
    const limit = Number(readsAt(reads, LIST).at(-1)?.searchParams.get('limit') ?? 50);
    const count = page.getByText(new RegExp(`已加载\\s*${limit}\\s*条`)).filter({ visible: true }).first();
    await visible(count);
    await hidden(scopeMessage(page));
    // `largeBatch` 把 OUTSIDE_BATCH 放到了第 203 条：第一页永远看不到它。
    await hidden(row(page, OUTSIDE_BATCH));

    // ① 命中落在第一页之外：只靠本地过滤会找不到，所以这条红就说明没真的接服务端。
    await search(page).fill('纸风车');
    await visible(row(page, OUTSIDE_BATCH));
    await hidden(row(page, FAVORITE));
    await expect.poll(() => readsAt(reads, LIST).at(-1)?.searchParams.get('q'), { timeout: WAIT })
      .toBe('纸风车');
    await visible(page.getByText(/匹配\s*1\s*条/).filter({ visible: true }).first());

    // ② 多词：空格分隔且每个词都要出现。
    await search(page).fill('桂花 茶');
    await visible(row(page, FAVORITE));
    await hidden(row(page, WALK));
    await hidden(row(page, OUTSIDE_BATCH));
    await search(page).fill('桂花 风车');
    await hidden(row(page, FAVORITE));
    await hidden(row(page, OUTSIDE_BATCH));
    await visible(page.getByText(/没有匹配|匹配\s*0\s*条/).filter({ visible: true }).first());
    // 搜索"没有结果"不能被说成"整库没有记忆"。
    await hidden(page.getByText(/(?:全库|整个记忆库|所有记忆).{0,16}(?:无结果|未找到|无匹配|没有|不存在)|(?:它|她)?(?:还)?没有(?:现行)?记忆/)
      .filter({ visible: true }).first());

    // ③ 清空后回到普通列表：最后一次请求与首次进入时同一组参数（不再带 q/offset）。
    await search(page).fill('');
    await visible(row(page, WALK));
    await expect.poll(() => readsAt(reads, LIST).at(-1)?.search, { timeout: WAIT })
      .toBe(initialReads.at(-1));
  }, CASE_TIMEOUT);

  it('命中数超过一页时能继续往后翻，翻页沿用同一搜索结果', async () => {
    const { page, reads } = await openWorkspace({ largeBatch: true });
    await visible(row(page, FAVORITE));
    // 202 条「批次样本」+ 首屏四条之一，命中足够跨页。
    await search(page).fill('批次样本');
    await expect.poll(() => Number(readsAt(reads, LIST).at(-1)?.searchParams.get('q') === '批次样本'), { timeout: WAIT }).toBe(1);
    const firstPage = readsAt(reads, LIST).at(-1)!;
    expect(firstPage.searchParams.get('offset')).toBe('0');
    await expect.poll(() => page.getByTestId('mem-results').getByRole('button').count(), { timeout: WAIT })
      .toBe(Number(firstPage.searchParams.get('limit') ?? 50));

    const more = page.getByTestId('mem-load-more');
    await visible(more);
    const total = Number(await page.getByTestId('mem-list-count').innerText().then(text => /匹配\s*(\d+)\s*条/.exec(text)?.[1] ?? NaN));
    expect(total).toBeGreaterThan(Number(firstPage.searchParams.get('limit') ?? 50));
    // 第一页就要如实说「还有 152 条」：这是 `total - 已取回`，两处口径必须一致。
    await expect.poll(() => page.getByTestId('mem-limit-notice').innerText(), { timeout: WAIT })
      .toContain(`还有 ${total - Number(firstPage.searchParams.get('limit') ?? 50)} 条未取回`);
    await more.click();
    await expect.poll(() => readsAt(reads, LIST).at(-1)?.searchParams.get('offset'), { timeout: WAIT })
      .toBe(firstPage.searchParams.get('limit') ?? '50');
    // 翻页请求必须带上同一个搜索词，不能退回普通列表的第一页。
    expect(readsAt(reads, LIST).at(-1)?.searchParams.get('q')).toBe('批次样本');
    await expect.poll(() => page.getByTestId('mem-results').getByRole('button').count(), { timeout: WAIT })
      .toBeGreaterThan(Number(firstPage.searchParams.get('limit') ?? 50));

    /*
      一页 50 条、命中 202 条，所以要一直翻到底。每一页的 `offset` 都必须等于
      当时已取回的条数——这是"不许跳页也不许重复"的唯一判据。
    */
    for (let round = 0; round < 10; round++) {
      const count = Number(await page.getByTestId('mem-results').getByRole('button').count());
      if (count >= total) break;
      const button = page.getByTestId('mem-load-more');
      await visible(button);
      await button.click();
      await expect.poll(() => Number(readsAt(reads, LIST).at(-1)?.searchParams.get('offset')), { timeout: WAIT })
        .toBe(count);
      await expect.poll(() => page.getByTestId('mem-results').getByRole('button').count(), { timeout: WAIT })
        .toBeGreaterThan(count);
    }
    // 取回条数追平命中数后，「还有多少条」必须消失，不许停在一个假的"还有 0 条"。
    await expect.poll(() => page.getByTestId('mem-list-count').innerText(), { timeout: WAIT })
      .toContain(`已取回 ${total} 条`);
    await hidden(page.getByTestId('mem-limit-notice'));
    // 翻完之后列表里也没有重复行。
    const ids = await page.getByTestId('mem-results').getByRole('button').evaluateAll(
      nodes => nodes.map(node => node.textContent ?? ''),
    );
    expect(new Set(ids).size).toBe(ids.length);
  }, CASE_TIMEOUT);

  /*
    构造方式必须是「**先发的那条**卡住」：卡住后发的那条永远不构成竞态——
    它返回时已经是最新请求，写回本来就不算覆盖。
    这里是慢的「桂花茶」→ 快的「纸风车」，「纸风车」先落地，再放开「桂花茶」。
  */
  it('快速改查询时，先发那条的慢响应不能覆盖后发的结果', async () => {
    const slow = gate();
    const gatePromise = slow;
    const { page, reads, settledDone } = await openWorkspace({
      largeBatch: true, holdFirst: '桂花茶', holdGate: gatePromise.promise,
    });
    const settled = new Set<Request>();
    page.on('requestfinished', request => settled.add(request));
    page.on('requestfailed', request => settled.add(request));
    await visible(row(page, FAVORITE));
    await search(page).fill('桂花茶');
    await expect.poll(() => readsAt(reads, LIST).some(url => url.searchParams.get('q') === '桂花茶'), { timeout: WAIT })
      .toBe(true);
    await search(page).fill('纸风车');
    await expect.poll(() => readsAt(reads, LIST).some(url => url.searchParams.get('q') === '纸风车'), { timeout: WAIT })
      .toBe(true);
    // 新的那条已经落地：列表里是纸风车，不是桂花茶。
    await visible(row(page, OUTSIDE_BATCH));
    await hidden(row(page, FAVORITE));
    // 现在放开那条迟到的「桂花茶」响应：它落地就必须被丢弃。
    slow.release();
    // 必须等**那条慢请求**真的走完（fulfill 之后浏览器收到），否则观察不到覆盖。
    await Promise.race([settledDone, new Promise((_, reject) => setTimeout(() => reject(new Error('慢响应没有走完')), WAIT))]);
    await paint(page);
    await paint(page);
    await expect.poll(() => results(page).innerText().then(text => text.includes(FAVORITE.text)), { timeout: WAIT })
      .toBe(false);
    await visible(row(page, OUTSIDE_BATCH));
    await expect.poll(() => page.getByTestId('mem-search').inputValue(), { timeout: WAIT }).toBe('纸风车');
    await expect.poll(() => page.getByTestId('mem-list-count').innerText(), { timeout: WAIT })
      .toContain('匹配 1 条');
  }, CASE_TIMEOUT);

  it('原生清除按钮清空输入后，列表同步回到普通列表（不是只在 DOM 里清掉）', async () => {
    const { page, reads } = await openWorkspace({ largeBatch: true });
    await visible(row(page, FAVORITE));
    await search(page).fill('纸风车');
    await visible(row(page, OUTSIDE_BATCH));
    expect(readsAt(reads, LIST).at(-1)?.searchParams.get('q')).toBe('纸风车');
    /*
      原生清除装饰（右侧 ×）是浏览器内部绘制的，Playwright 点不到；但 Chromium
      点它会派发 `search` 事件。这里用同样的方式驱动，验的正是那条接线：
      只把 DOM 值清空、不同步状态，下面的断言就会红。
    */
    await search(page).evaluate((el: HTMLInputElement) => {
      el.value = '';
      el.dispatchEvent(new Event('search'));
    });
    await expect.poll(() => readsAt(reads, LIST).at(-1)?.searchParams.get('q'), { timeout: WAIT })
      .toBeNull();
    await visible(row(page, FAVORITE));
    await hidden(row(page, OUTSIDE_BATCH));
    await expect.poll(() => page.getByTestId('mem-search').inputValue(), { timeout: WAIT }).toBe('');
  }, CASE_TIMEOUT);

  /*
    反证：把服务端的 `q`/`offset` 关掉（回到"只按 limit 截断"的旧行为），
    "找到第一页之外的旧条目"必须失败。这就是「必须失败于接线未做」的那一条。
  */
  it('反证：服务端不认 q 时，第一页之外的那条搜不到', async () => {
    const { page } = await openWorkspace({ largeBatch: true, ignoreServerSearch: true });
    await visible(row(page, FAVORITE));
    await search(page).fill('纸风车');
    await paint(page);
    await paint(page);
    await hidden(row(page, OUTSIDE_BATCH));
  }, CASE_TIMEOUT);

  it('四个分区是可导航的独立目的地，核心只在进入该分区后读取', async () => {
    const { page, reads } = await openWorkspace();
    await visible(row(page, FAVORITE));
    await current(page, '记忆库');
    for (const name of Object.keys(TABS) as Tab[]) {
      const href = await tabLink(page, name).getAttribute('href');
      expect(href, `${name} 应有可打开的 href`).not.toBeNull();
      expect(new URL(href!, page.url()).href).toBe(`${baseUrl}${TABS[name]}`);
    }
    expect(readsAt(reads, CORE)).toHaveLength(0);
    await hidden(privacyControl(page));

    await goToTab(page, '学习设置');
    await expect.poll(() => retentionField(page).inputValue(), { timeout: WAIT }).toBe(String(RETENTION));
    await hidden(row(page, FAVORITE));
    await hidden(coreText(page));
    await hidden(privacyControl(page));
    expect(readsAt(reads, CORE)).toHaveLength(0);

    await goToTab(page, '隐私与清理');
    await visible(privacyControl(page));
    await hidden(retentionField(page));
    await hidden(row(page, FAVORITE));
    expect(readsAt(reads, CORE)).toHaveLength(0);

    await goToTab(page, '核心记忆');
    await visible(coreText(page));
    expect(readsAt(reads, CORE).length).toBeGreaterThan(0);
    await hidden(row(page, FAVORITE));
    await hidden(retentionField(page));
    await hidden(privacyControl(page));

    await page.goBack();
    await current(page, '隐私与清理');
    await visible(privacyControl(page));
    await page.reload({ waitUntil: 'domcontentloaded' });
    await current(page, '隐私与清理');
    await visible(privacyControl(page));
  }, CASE_TIMEOUT);

  it('学习设置发布每轮带入的旧记忆条数并回显零值', async () => {
    const { page } = await openWorkspace({ start: '学习设置' });
    let published = SETTINGS.memory;
    let draft = published;
    let revision = SETTINGS.revision;
    await page.route(`**${CONFIG}`, route => route.fulfill(json({ ...SETTINGS, revision, memory: published })));
    await page.route('**/v1/settings/draft', route => {
      draft = route.request().postDataJSON().memory;
      return route.fulfill(json({ revision: revision + 1, validation_errors: [] }));
    });
    await page.route('**/v1/settings/publish', route => {
      revision = route.request().postDataJSON().revision;
      published = draft;
      return route.fulfill(json({ revision }));
    });

    const field = page.getByRole('spinbutton', { name: /^每轮最多带入的旧记忆/ });
    await visible(field);
    expect(await field.inputValue()).toBe('3');
    expect(await field.getAttribute('min')).toBe('0');
    expect(await field.getAttribute('max')).toBe('12');
    await field.fill('0');
    await page.getByRole('button', { name: '保存并发布' }).click();
    await expect.poll(() => published.max_injected_memories, { timeout: WAIT }).toBe(0);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await visible(field);
    expect(await field.inputValue()).toBe('0');
  }, CASE_TIMEOUT);

  for (const action of [
    { title: '纠正', endpoint: 'supersede', label: '新的说法', field: 'text', value: '周末改喝乌龙茶' },
    { title: '撤回', endpoint: 'retract', label: '撤回理由', field: 'reason', value: '这不是我的偏好' },
  ] as const) {
    it(`${action.title}：打开和取消不写入，确认后只提交目标记录`, async () => {
      const { page, writes } = await openWorkspace();
      const open = detail(page).getByRole('button', { name: new RegExp(`^${action.title}(?:…|\\.{3})?$`) });
      const input = page.getByRole('textbox', { name: action.label, exact: true });
      const confirm = page.getByRole('button', { name: `确认${action.title}`, exact: true });
      await row(page, FAVORITE).click();
      await visible(detailHeading(page));
      await hidden(confirm);
      await open.click();
      await input.fill(action.value);
      await visible(confirm);
      expect(writes).toEqual([]);

      if (action.field === 'text') {
        await goToTab(page, '学习设置');
        await hidden(input);
        await goToTab(page, '记忆库');
        await visible(input);
        expect(await input.inputValue()).toBe(action.value);
        expect(writes).toEqual([]);
      }

      await page.getByRole('button', { name: new RegExp(`^取消(?:${action.title})?$`) }).click();
      await hidden(confirm);
      await visible(row(page, FAVORITE));
      expect(writes).toEqual([]);

      await open.click();
      await input.fill(action.value);
      const path = `${LIST}/${FAVORITE.memory_id}/${action.endpoint}`;
      const [response] = await Promise.all([
        page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === path),
        confirm.click(),
      ]);
      expect(response.status()).toBe(200);
      await hidden(confirm);
      await hidden(row(page, FAVORITE));
      await visible(row(page, action.field === 'text' ? { text: action.value } : WALK));
      expect(writes).toEqual([{ method: 'POST', path, body: expect.objectContaining({ [action.field]: action.value }) }]);
    }, CASE_TIMEOUT);
  }

  it('桌面用键盘从搜索进入搜索结果并打开匹配记忆', async () => {
    const { page, reads } = await openWorkspace();
    await visible(row(page, WALK));
    // 不直接 focus 控件、不拿点击代替键盘，也不固定 Tab 次数。
    await tabTo(page, search(page), 'Tab 应能聚焦搜索框');
    await page.keyboard.insertText('河边');
    await hidden(row(page, FAVORITE));
    await tabTo(page, row(page, WALK), 'Tab 应能从搜索框聚焦匹配的列表项');
    await page.keyboard.press('Enter');
    await visible(detailHeading(page));
    await visible(detail(page).getByText(WALK.text, { exact: true }).filter({ visible: true }).first());
    expect(reads.some(url => url.pathname === `${LIST}/${WALK.memory_id}`)).toBe(false);
  }, CASE_TIMEOUT);

  it('375 CSS px 触屏能打开详情并回到仍然可用的列表', async () => {
    const { page } = await openWorkspace({ mobile: true });
    await visible(row(page, FAVORITE));
    await notWiderThanMobileViewport(page);
    await row(page, FAVORITE).tap();
    await visible(detailHeading(page));
    await visible(detail(page).getByText(FAVORITE.text, { exact: true }).filter({ visible: true }).first());
    await notWiderThanMobileViewport(page);
    const back = page.getByRole('button', { name: '回到列表', exact: true });
    await back.tap();
    await hidden(back);
    await visible(row(page, WALK));
    await row(page, WALK).tap();
    await visible(detail(page).getByText(WALK.text, { exact: true }).filter({ visible: true }).first());
    await notWiderThanMobileViewport(page);
  }, CASE_TIMEOUT);

  it('切换历史筛选时，晚到只含撤回记录的响应不能覆盖已经显示的归档结果', async () => {
    const old = gate();
    let held: Request | undefined;
    const { page, reads } = await openWorkspace({
      waitForList: (request, include) => {
        if (include !== 'retracted' || held) return;
        held = request;
        return old.promise;
      },
    });
    const settled = new Set<Request>();
    page.on('requestfinished', request => settled.add(request));
    page.on('requestfailed', request => settled.add(request));
    try {
      await visible(row(page, FAVORITE));
      await hidden(row(page, RETRACTED));
      await hidden(row(page, ARCHIVED));
      await historyToggle(page, '显示已撤回');
      await expect.poll(() => !!held, { timeout: WAIT }).toBe(true);
      await historyToggle(page, '显示已归档');
      await visible(row(page, ARCHIVED));
      await visible(row(page, RETRACTED));
      expect(readsAt(reads, LIST).at(-1)?.searchParams.get('include')).toBe('all');
      old.release();
      await expect.poll(() => !!held && settled.has(held), { timeout: WAIT }).toBe(true);
      await paint(page);
      await visible(row(page, ARCHIVED));
      await visible(row(page, RETRACTED));

      await historyToggle(page, '显示已撤回');
      await hidden(row(page, RETRACTED));
      await visible(row(page, ARCHIVED));
      expect(readsAt(reads, LIST).at(-1)?.searchParams.get('include')).toBe('archived');
      await historyToggle(page, '显示已归档');
      await hidden(row(page, ARCHIVED));
      await visible(row(page, FAVORITE));
      expect(readsAt(reads, LIST).at(-1)?.searchParams.get('include')).toBeNull();
    } finally {
      old.release();
    }
  }, CASE_TIMEOUT);

  for (const failure of [
    { endpoint: LIST, tab: '记忆库' }, { endpoint: CORE, tab: '核心记忆' }, { endpoint: CONFIG, tab: '学习设置' },
  ] as const) {
    it(`${failure.tab}读取失败有提示，不阻塞其他分区`, async () => {
      const { page, reads } = await openWorkspace({ start: failure.tab, fail: failure.endpoint });
      await current(page, failure.tab);
      await expect.poll(() => readsAt(reads, failure.endpoint).length, { timeout: WAIT }).toBeGreaterThan(0);
      const errorHint = page.getByText(/(?:加载|读取|获取|请求).{0,18}(?:失败|出错)|(?:无法|未能).{0,8}(?:加载|读取|获取)|模拟读取失败/)
        .or(page.getByRole('button', { name: /重试/ })).filter({ visible: true }).first();
      await visible(errorHint);
      if (failure.endpoint !== LIST) {
        await goToTab(page, '记忆库');
        await page.getByRole('button', { name: '列表', exact: true }).click();
        await visible(row(page, FAVORITE));
        await row(page, FAVORITE).click();
        await visible(detailHeading(page));
      }
      if (failure.endpoint !== CORE) {
        await goToTab(page, '核心记忆');
        await visible(coreText(page));
      }
      if (failure.endpoint !== CONFIG) {
        await goToTab(page, '学习设置');
        await expect.poll(() => retentionField(page).inputValue(), { timeout: WAIT }).toBe(String(RETENTION));
      }
      await goToTab(page, '隐私与清理');
      await visible(privacyControl(page));
    }, CASE_TIMEOUT);
  }

  it('星图：连续时间轴、同年/跨年弧线、依据连线、未知发生日期与列表共用详情', async () => {
    await mkdir(SHOTS, { recursive: true });
    const { page, reads } = await openWorkspace({
      memories: STAR_ROWS, view: 'default', viewport: { width: 1440, height: 1000 },
    });
    const canvas = page.getByTestId('mem-star-canvas');
    await visible(canvas);
    /* A 版结构：月令环在左、时间轴在右、详情全宽在下方，且首屏能看到详情标题。 */
    const rail = page.locator('.mem-star-rail');
    const main = page.locator('.mem-star-main');
    const railBox = await rail.boundingBox();
    const mainBox = await main.boundingBox();
    expect(railBox && mainBox).toBeTruthy();
    expect(railBox!.x + railBox!.width).toBeLessThanOrEqual(mainBox!.x + 1);
    expect(Math.abs(railBox!.y - mainBox!.y)).toBeLessThanOrEqual(1);
    // 默认只读图：不弹镜片，也不展开详情；详情是显式深读。
    await hidden(page.getByTestId('mem-focus-lens'));
    await hidden(detail(page));
    const workspaceBox = await page.locator('[data-testid="mem-list"]').boundingBox();
    expect(workspaceBox!.width).toBeGreaterThan(mainBox!.width);
    const card = (id: string) => canvas.locator(`[data-memory-id="${id}"]`);
    const x = async (id: string) => card(id).evaluate((element) => (element.parentElement as HTMLElement).offsetLeft);

    // ⓪ 记忆范围：筛选面板能切全部 / 仅私聊 / 仅群聊。
    await page.getByRole('button', { name: /^筛选/ }).click();
    const scopeSelect = page.locator('#mem-discovery-filters select').filter({ has: page.locator('option[value="group"]') });
    await visible(scopeSelect);
    expect(await scopeSelect.locator('option').allTextContents()).toEqual(['全部记忆', '仅私聊', '仅群聊']);
    await scopeSelect.selectOption('group');
    await visible(page.getByTestId('mem-filter-empty'));
    await scopeSelect.selectOption('all');
    await page.getByRole('button', { name: /^筛选/ }).click();

    // ① 横轴连续：2024-05 → 2026-05 的间隔远大于 2026-05 → 2026-11。
    const longGap = await x('star_poster') - await x('star_drawings');
    const shortGap = await x('star_regular') - await x('star_poster');
    expect(longGap).toBeGreaterThan(shortGap * 3.5);
    expect(longGap).toBeLessThan(shortGap * 5.2);

    // ② 依据各自成线：共享具体事件与纠正链各一条；同地点/同旧会话不连线。
    expect(await canvas.locator('path[data-kind="source"]').count()).toBe(1);
    expect(await canvas.locator('path[data-kind="correction"]').count()).toBe(1);
    expect(await canvas.locator('path[data-kind="source"][data-from="star_same_place"]').count()).toBe(0);
    expect(await canvas.locator('path[data-kind="correction"][data-from="star_same_place"]').count()).toBe(0);
    expect(await canvas.locator('path[data-from="star_drawings"][data-to="star_poster"]').count()).toBe(1);

    // ③ 发生日期未知独立成列，不被记下时间顶替。
    await visible(card('star_unknown'));
    expect(await card('star_unknown').getAttribute('data-date-kind')).toBe('未知');
    expect(await x('star_unknown')).toBeGreaterThan(await x('star_regular'));
    expect(await card('star_drawings').getAttribute('data-date-kind')).toBe('发生');
    expect(await card('star_regular').getAttribute('data-date-kind')).toBe('记下');

    // ④ 月令环区分同年同月与跨年同月。
    const wheel = page.getByTestId('mem-month-wheel');
    await wheel.getByRole('button', { name: /5月（/ }).click();
    await expect.poll(() => canvas.locator('path[data-month="5"].mem-star-arc--lunar-cross-year').count())
      .toBeGreaterThan(0);
    await expect.poll(() => canvas.locator('path[data-month="5"].mem-star-arc--lunar-same-year').count())
      .toBeGreaterThan(0);
    await visible(canvas.getByText(/5月跨年 2024→2026/).first());
    await visible(canvas.getByText(/2026·5月同月/).first());
    await paint(page);
    await page.screenshot({ path: join(SHOTS, '1440-star.png'), fullPage: true });

    // ⑤ 放大镜：点卡片后强/中关联保留，弱关联淡出；同地点不误连。
    await card('star_drawings').click();
    const lens = page.getByTestId('mem-focus-lens');
    await visible(lens);
    expect(await canvas.locator('[data-memory-id="star_poster"].mem-star-node--link-strong').count()).toBe(1);
    expect(await canvas.locator('[data-memory-id="star_unknown"].mem-star-node--dim').count()).toBe(1);
    const lensText = await lens.innerText();
    expect(lensText).toContain('同源事件');
    expect(lensText).toMatch(/同一天|相邻三天|同月/);
    const lensBox = await lens.boundingBox();
    const strongBox = await card('star_poster').boundingBox();
    const drawingBox = await card('star_drawings').boundingBox();
    expect(lensBox && strongBox && drawingBox).toBeTruthy();
    // 镜片贴在选中卡片下方/上方，不能盖住强关联卡片。
    const overlapsStrong = !(lensBox!.x + lensBox!.width <= strongBox!.x
      || strongBox!.x + strongBox!.width <= lensBox!.x
      || lensBox!.y + lensBox!.height <= strongBox!.y
      || strongBox!.y + strongBox!.height <= lensBox!.y);
    expect(overlapsStrong).toBe(false);
    await paint(page);
    await page.screenshot({ path: join(SHOTS, '1440-star-lens.png'), fullPage: true });

    // ⑥ 关闭镜片恢复全图；再点卡片重新聚焦。
    await lens.getByRole('button', { name: '收起放大镜' }).click();
    await hidden(lens);
    expect(await canvas.locator('.mem-star-node--dim').count()).toBe(0);
    await card('star_drawings').click();
    await visible(lens);
    expect(await canvas.locator('.mem-star-node--dim').count()).toBeGreaterThan(0);

    // ⑦ 点“看完整详情”进入既有详情、来源与分镜。
    await lens.getByRole('button', { name: /看完整详情/ }).click();
    const inspector = detail(page);
    await visible(detailHeading(page));
    const detailText = await inspector.innerText();
    expect(detailText).toContain(STAR_DRAWING.text);
    expect(detailText).toContain('2024-05-12');
    expect(detailText).toContain('2026-06-01');
    expect(detailText).toContain('发生时间');
    expect(detailText).toContain('记下时间');
    const micro = inspector.getByTestId('mem-microscope');
    await visible(micro);
    await micro.getByRole('button', { name: /全景分镜/ }).click();
    const theater = page.getByRole('dialog', { name: '记忆全景分镜' });
    await visible(theater);
    const theaterText = await theater.innerText();
    expect(theaterText).toContain(STAR_DRAWING.text);
    expect(theaterText).toContain('evt_drawings');
    await paint(page);
    await page.screenshot({ path: join(SHOTS, '1440-theater.png'), fullPage: true });
    const theaterOpen = micro.getByRole('button', { name: /全景分镜/ });
    await page.keyboard.press('Escape');
    await hidden(theater);
    expect(await theaterOpen.evaluate((button) => document.activeElement === button)).toBe(true);

    // ⑥ 详情内的关联跳读仍走既有路径，只换详情、不依赖星图镜片。
    await inspector.getByTestId('mem-microscope').getByRole('button', { name: /同源事件.*两年后又看到/ }).click();
    await visible(inspector.getByText(STAR_POSTER.text, { exact: true }).filter({ visible: true }).first());
    // 纠正链要从旧说法那条进入，不能拿海报的详情去找纠正按钮。
    await card('star_old').click();
    await visible(lens);
    await lens.getByRole('button', { name: /看完整详情/ }).click();
    await visible(inspector.getByText(STAR_OLD.text, { exact: true }).filter({ visible: true }).first());
    await inspector.getByTestId('mem-microscope').getByRole('button', { name: /纠正链.*周四闭馆/ }).click();
    await visible(inspector.getByText(STAR_NEW.text, { exact: true }).filter({ visible: true }).first());

    // ⑦ 星图点卡片先换镜片，不偷换详情；详情入口仍保留编辑能力。
    await lens.getByRole('button', { name: /看完整详情/ }).click();
    await visible(inspector.getByRole('button', { name: '纠正…', exact: true }));
    await inspector.getByRole('button', { name: '纠正…', exact: true }).click();
    await visible(inspector.getByRole('form', { name: '纠正这条记忆' }));
    await inspector.getByRole('button', { name: '取消', exact: true }).click();
    expect(await inspector.getByRole('button', { name: /^遗忘/ }).count()).toBe(0);

    // ⑧ 搜索走服务端，星图与列表共享同一批结果；切回列表仍保留选中详情。
    await search(page).fill('纸风车');
    await expect.poll(() => readsAt(reads, LIST).at(-1)?.searchParams.get('q'), { timeout: WAIT }).toBe('纸风车');
    await visible(card('star_unknown'));
    await hidden(card('star_drawings'));
    await page.getByRole('button', { name: '列表', exact: true }).click();
    await visible(row(page, STAR_UNKNOWN));
    await hidden(row(page, STAR_DRAWING));
    await page.getByRole('button', { name: '星图', exact: true }).click();
    await visible(card('star_unknown'));

    // ⑨ 本地筛选只收窄视图，不重发搜索；清除后回到全量星图。
    await search(page).fill('');
    await visible(card('star_drawings'));
    await page.getByRole('button', { name: '事实与线索', exact: true }).click();
    await hidden(card('star_drawings'));
    await visible(card('star_may_a'));
    await page.getByRole('button', { name: '全部', exact: true }).click();
    await visible(card('star_drawings'));
  }, 90_000);

  it('星图 375 / 320：画布内部平移、详情可读、返回把焦点还给卡片', async () => {
    await mkdir(SHOTS, { recursive: true });
    for (const width of [375, 320] as const) {
      const { page } = await openWorkspace({
        memories: STAR_ROWS, view: 'stars', mobile: true,
        viewport: { width, height: 760 },
      });
      const canvas = page.getByTestId('mem-star-canvas');
      await visible(canvas);
      await notWiderThanViewport(page, width);
      const tabs = page.locator('.mem-tabs');
      expect(await tabs.locator('.mem-tab').count()).toBe(5);
      expect(await tabs.evaluate((node) => node.getBoundingClientRect().height)).toBeGreaterThan(30);
      const rail = page.locator('.mem-star-rail');
      const main = page.locator('.mem-star-main');
      const railBox = await rail.boundingBox();
      const mainBox = await main.boundingBox();
      expect(railBox && mainBox).toBeTruthy();
      expect(railBox!.y + railBox!.height).toBeLessThanOrEqual(mainBox!.y + 1);
      expect(await rail.getByText('十二月令环', { exact: true }).count()).toBeLessThanOrEqual(1);
      const viewport = page.getByRole('region', { name: '记忆星图' });
      const scroll = await viewport.evaluate((element) => ({
        client: element.clientWidth,
        content: element.scrollWidth,
      }));
      expect(scroll.content).toBeGreaterThan(scroll.client);
      await paint(page);
      await page.screenshot({ path: join(SHOTS, `${width}-star.png`), fullPage: true });

      const card = page.locator('[data-memory-id="star_drawings"]');
      await card.click();
      await visible(detailHeading(page));
      await visible(detail(page).getByText(STAR_DRAWING.text, { exact: true }).filter({ visible: true }).first());
      await notWiderThanViewport(page, width);
      await paint(page);
      await page.screenshot({ path: join(SHOTS, `${width}-detail.png`), fullPage: true });
      const back = page.getByRole('button', { name: '回到星图', exact: true });
      await back.tap();
      await hidden(back);
      await visible(canvas);
      expect(await card.evaluate((element) => document.activeElement === element)).toBe(true);
    }
  }, 90_000);
});
