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
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { chromium, type Browser, type BrowserContext, type Locator, type Page, type Request } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

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
  },
};

const json = (body: unknown, status = 200) => ({ status, json: body });
const error = (code: string, message: string, status: number) => json({
  schema_version: 'v1', error: { code, message, retriable: status >= 500, detail: null, trace_id: 'trace-mock' },
}, status);

interface MockOptions {
  start?: Tab;
  mobile?: boolean;
  largeBatch?: boolean;
  fail?: typeof LIST | typeof CORE | typeof CONFIG;
  waitForList?: (request: Request, include: string | null) => Promise<void> | undefined;
}

async function openWorkspace(options: MockOptions = {}) {
  const context = await browser.newContext({
    viewport: options.mobile ? { width: 375, height: 760 } : { width: 1366, height: 900 },
    hasTouch: options.mobile, isMobile: options.mobile, reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  contexts.push(context);
  const page = await context.newPage();
  page.setDefaultTimeout(WAIT);
  page.setDefaultNavigationTimeout(2 * WAIT);
  const reads: URL[] = [];
  const writes: { method: string; path: string; body: unknown }[] = [];
  let rows = [FAVORITE, WALK, RETRACTED, ARCHIVED];
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
        case LIST: {
          const limit = Number(url.searchParams.get('limit') ?? 50);
          const include = url.searchParams.get('include');
          if (!Number.isInteger(limit) || limit < 1 || limit > 200 ||
              (include !== null && !['retracted', 'archived', 'all'].includes(include))) {
            return route.fulfill(error('invalid_request', '无效的列表参数', 422));
          }
          const filtered = rows.filter(row =>
            (!row.retracted || include === 'retracted' || include === 'all') &&
            (!row.archived_at || include === 'archived' || include === 'all'));
          // 快照在请求到达时取，控制的是它何时返回，不是修改当前服务器状态。
          const payload = {
            memories: filtered.slice(0, limit), limit, include, total: filtered.length,
            has_more: filtered.length > limit, note: '',
          };
          await options.waitForList?.(request, include);
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
  return { page, reads, writes };
}

const nav = (page: Page) => page.getByRole('navigation', { name: '记忆分区', exact: true });
const tabLink = (page: Page, tab: Tab) => nav(page).getByRole('link', { name: tab, exact: true });
const search = (page: Page) => page.getByRole('searchbox', { name: '搜索已加载的记忆', exact: true });
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

async function notWiderThanMobileViewport(page: Page) {
  const widths = await page.evaluate(() => ({
    css: document.documentElement.clientWidth,
    content: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
  }));
  expect(widths.css, '应使用移动端 CSS 视口，不能靠缩放把桌面页塞进去').toBe(375);
  expect(widths.content, '页面不应横向溢出').toBeLessThanOrEqual(widths.css + 1);
}

describe('记忆工作区：独立浏览器验收', () => {
  it('只检索已加载的批次，告知范围且不把漏在批次之外的记录说成全库无结果', async () => {
    const { page, reads } = await openWorkspace({ largeBatch: true });
    await visible(row(page, FAVORITE));
    await visible(row(page, WALK));
    const initialReads = readsAt(reads, LIST).map(url => url.search);
    expect(initialReads.length).toBeGreaterThan(0);
    const limit = Number(readsAt(reads, LIST).at(-1)?.searchParams.get('limit') ?? 50);
    const count = page.getByText(new RegExp(`已加载\\s*${limit}\\s*条`)).filter({ visible: true }).first();
    await visible(count);
    await visible(scopeMessage(page));
    await hidden(row(page, OUTSIDE_BATCH));

    await search(page).fill('桂花');
    await hidden(row(page, WALK));
    await visible(row(page, FAVORITE));
    await visible(count);

    await search(page).fill('纸风车');
    await hidden(row(page, FAVORITE));
    await hidden(row(page, OUTSIDE_BATCH));
    await visible(scopeMessage(page));
    await visible(page.getByText(/无匹配|未匹配|没(?:有)?找到|未找到|没有.{0,10}(?:匹配|符合)|未发现.{0,10}(?:匹配|相关)|匹配\s*0\s*条|0\s*条匹配/)
      .filter({ visible: true }).first());
    await hidden(page.getByText(/(?:全库|整个记忆库|所有记忆).{0,16}(?:无结果|未找到|无匹配|没有|不存在)|(?:未找到|没有|不存在).{0,16}(?:全库|整个记忆库|所有记忆)/)
      .filter({ visible: true }).first());

    await search(page).fill('');
    await visible(row(page, WALK));
    expect(readsAt(reads, LIST).map(url => url.search)).toEqual(initialReads);
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
});
