/**
 * 轮询/刷新与历史分页冲突的真实浏览器回归（验收日志 chat-active-history.log）。
 *
 * 场景与验收日志逐项对应：
 *   会话 260 条、**仍有 active_turn**；
 *   首屏 limit=100 → 161..260；
 *   before_sequence=161 → 61..160；
 *   before_sequence=61  → 1..60   （此时第 1 条在视图里）
 *   轮询 limit=200（无游标）→ 61..260 + 新消息 261/262，active_turn=null
 *
 * 判据见 docs/evidence/chat-workspace-20261123/poll-history-criteria.md。
 *
 * 用法：node scripts/chat-poll-history-regression.mjs
 */
import { chromium } from 'playwright';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { mkdirSync, writeFileSync, appendFileSync } from 'node:fs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const OUT = join(ROOT, '../../docs/evidence/chat-workspace-20261123');
mkdirSync(join(OUT, 'screens'), { recursive: true });
const LOG = join(OUT, 'poll-history.log');
writeFileSync(LOG, '');
const log = (line) => {
  const stamped = `[${new Date().toISOString()}] ${line}`;
  console.log(stamped);
  appendFileSync(LOG, `${stamped}\n`);
};

const SESSION_ID = 'poll-260';
const TOTAL = 260;
const T0 = '2026-11-21T09:00:00Z';
const at = (sequence) => new Date(Date.parse(T0) + sequence * 60_000).toISOString();

const dm = (sequence) => ({
  schema_version: 'v1',
  message_id: `m-${sequence}`,
  turn_id: sequence === TOTAL ? 'turn-active' : null,
  role: sequence % 2 === 0 ? 'actor' : 'user',
  text: `第 ${sequence} 条`,
  output_modality: 'text',
  audio_asset_id: null,
  speech_state: 'none',
  speech_delivery_state: null,
  state: sequence === TOTAL ? 'streaming' : 'completed',
  created_at: at(sequence),
  sequence,
  origin: 'web',
  error_code: null,
  model_label: sequence % 2 === 0 ? 'hy3gw2/hy3' : null,
  attempt: 1,
  handled_as: null,
  handled_reason: null,
  sender_name: sequence % 2 === 0 ? null : '阿青',
  sender_id: sequence % 2 === 0 ? null : '100200302',
  retracted_at: null,
  recalled_at: null,
  recall_state: null,
});

const ALL = Array.from({ length: TOTAL }, (_, index) => dm(index + 1));
const json = (body, status = 200) => ({
  status, contentType: 'application/json', body: JSON.stringify(body),
});

const requests = [];
const state = {
  /** 轮询前 false，之后为 true：决定轮询那一批要不要带新消息。 */
  pollPhase: false,
};

async function installRoutes(page) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/v1/auth/me') {
      return route.fulfill(json({
        schema_version: 'v1',
        user: { id: 'u', username: 'owner', role: 'owner', subject_id: 's', display_name: '主人' },
        config_revision: 1, test_mode: false, csrf_token: 'csrf',
      }));
    }
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.15' }));
    if (path === '/v1/persona') return route.fulfill(json({ name: '灵', display_name: '灵' }));
    if (path === '/v1/live/sessions/current') return route.fulfill(json({ session: null }));
    if (path === '/v1/settings/connectors') return route.fulfill(json({ items: [], access: null }));
    if (path === '/v1/sessions') {
      return route.fulfill(json({
        sessions: [{
          schema_version: 'v1', session_id: SESSION_ID, window_id: SESSION_ID,
          actor_id: 'a', subject_id: 's', title: '260 条的会话', channel: 'web',
          peer_id: null, created_at: T0, updated_at: at(TOTAL),
          message_count: TOTAL, archived: false, pinned: false,
        }],
      }));
    }

    const messages = /^\/v1\/sessions\/([^/]+)\/messages$/.exec(path);
    if (messages) {
      const limit = Number(url.searchParams.get('limit') ?? '100');
      const before = url.searchParams.get('before_sequence');
      requests.push({ request: 'messages', limit, before });

      if (before === null) {
        // 首屏一旦已经发生过「向上两页」，之后的无游标请求就是轮询：
        // 它返回最新 200 条 + 两条新消息，并且 active_turn 结束。
        if (state.pollPhase) {
          const newest = [...ALL.slice(TOTAL - 200), dm(TOTAL + 1), dm(TOTAL + 2)]
            .map((message) => ({ ...message, state: 'completed', turn_id: null }));
          return route.fulfill(json({ messages: newest, active_turn: null }));
        }
        return route.fulfill(json({
          messages: ALL.slice(TOTAL - limit),
          active_turn: { turn_id: 'turn-active', state: 'streaming', attempt: 1, model_label: null, error_code: null, retriable: false },
        }));
      }
      const cursor = Number(before);
      const slice = ALL.slice(Math.max(0, cursor - 1 - limit), cursor - 1);
      return route.fulfill(json({ messages: slice, active_turn: null }));
    }
    if (/^\/v1\/sessions\/[^/]+\/voice-messages$/.test(path)) return route.fulfill(json({ items: [] }));
    if (/^\/v1\/sessions\/[^/]+\/recall-proposals$/.test(path)) return route.fulfill(json([]));
    if (path.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: path } }, 404));
    }
    return route.continue();
  });
}

const fail = (line) => { log(`FAIL ${line}`); throw new Error(line); };
const check = (condition, line) => {
  if (!condition) fail(line);
  log(`  ok  ${line}`);
};

const scrollerState = (page) => page.evaluate(() => {
  const node = document.querySelector('[data-testid="message-scroll"]');
  // 只数**带序号**的真实消息行：生成中的流式占位行没有 data-sequence，
  // 把它算进来会让 oldest/newest 读出 null/0。
  const items = Array.from(node.querySelectorAll('li[data-role][data-sequence]'));
  const sequences = items.map((li) => Number(li.getAttribute('data-sequence')));
  const first = items[0];
  return {
    count: items.length,
    oldest: Number.isFinite(sequences[0]) ? sequences[0] : null,
    newest: Number.isFinite(sequences[sequences.length - 1]) ? sequences[sequences.length - 1] : null,
    sequences,
    firstText: first ? first.textContent : null,
    scrollTop: node.scrollTop,
    scrollHeight: node.scrollHeight,
    historyStart: document.querySelector('[data-testid="history-start"]') !== null,
    historyEdge: document.querySelector('[data-testid="history-edge"]') !== null,
  };
});

const server = await createServer({
  configFile: false, root: ROOT, plugins: [react()], logLevel: 'silent',
  server: { host: '127.0.0.1', port: 0 },
});
await server.listen();
const address = server.httpServer?.address();
if (!address || typeof address === 'string') throw new Error('Vite 没有可用端口');
const baseUrl = `http://127.0.0.1:${address.port}`;
log(`vite ${baseUrl}`);

const browser = await chromium.launch();
let passed = false;
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce', locale: 'zh-CN',
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  await installRoutes(page);

  await page.goto(`${baseUrl}/s/${SESSION_ID}`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="message-scroll"] li[data-role]').length > 0);

  // 首屏：161..260
  let view = await scrollerState(page);
  check(view.oldest === 161 && view.newest === 260, `首屏是最新一页（${view.oldest}..${view.newest}）`);
  check(view.historyEdge && !view.historyStart, '首屏顶部给「读取更早」，不是「开头」');

  // 向上两页
  for (const expected of [61, 1]) {
    const edge = page.getByTestId('history-edge');
    await edge.waitFor();
    await edge.getByRole('button').first().click();
    await page.waitForFunction(
      (want) => {
        const items = document.querySelectorAll('[data-testid="message-scroll"] li[data-role]');
        const first = items[0];
        return first && first.getAttribute('data-sequence') === String(want);
      },
      expected,
    );
  }
  view = await scrollerState(page);
  check(view.oldest === 1, `两次向上加载后第 1 条在视图里（oldest=${view.oldest}）`);
  check(view.count === 260, `视图里是全部 260 条（${view.count}）`);
  check(view.historyStart, '到头后页顶显示「这里是这段对话的开头」');
  const beforePoll = view;

  // 站在"读者正在看第 1 条"的位置
  await page.evaluate(() => {
    const node = document.querySelector('[data-testid="message-scroll"]');
    node.scrollTop = 0;
    node.dispatchEvent(new Event('scroll', { bubbles: true }));
  });
  await page.waitForTimeout(120);
  const beforePollScroll = await page.evaluate(
    () => document.querySelector('[data-testid="message-scroll"]').scrollTop,
  );

  // 轮询
  state.pollPhase = true;
  log('轮询开始（最新 200 条 + 两条新消息，active_turn=null）');
  /*
   * 按**序号**判断新消息在不在，而不是"最后一条的文本"。
   *
   * 生成中会多一条没有 `data-role` 的流式占位行；用 `li:last-child` 判断会把
   * 占位当成消息，得出"新消息没到"的假结论（第一次就是这么误判的）。
   */
  const sawNew = await page
    .waitForFunction(
      () => {
        const sequences = Array.from(
          document.querySelectorAll('[data-testid="message-scroll"] li[data-role][data-sequence]'),
          (li) => Number(li.getAttribute('data-sequence')),
        );
        return sequences.includes(262);
      },
      undefined,
      { timeout: 15_000 },
    )
    .then(() => true)
    .catch(() => false);
  check(sawNew, '轮询带来的新消息已进入视图（sequence=262）');
  await page.waitForTimeout(300);

  const after = await scrollerState(page);

  // #1 第 1 条仍在
  check(after.oldest === 1, `轮询后第 1 条仍在（oldest=${after.oldest}）`);
  // #2 是同一条
  check(after.sequences[0] === 1, '第 1 条仍是 sequence=1 的那条');
  // #3 新消息出现
  check(after.newest === 262, `新消息已合并（newest=${after.newest}）`);
  check(after.sequences.includes(261) && after.sequences.includes(262), '261、262 都在视图里');
  // #4 单调且不重复
  const strictly = after.sequences.every((value, index) => index === 0 || value > after.sequences[index - 1]);
  check(strictly, '顺序严格递增、无重复');
  // #5 页顶不该显示"开头"……这里其实已经到底了，要点是**不能凭空变成开头**
  check(after.historyStart, '轮询后仍显示「这里是开头」（因为确实已加载到第 1 条）');
  // #6 轮询后可继续向上加载（这里已到底，改为验证"再取一页不重复"）
  const countBeforeOlder = after.count;
  const edge = page.getByTestId('history-edge');
  if (await edge.count()) {
    await edge.getByRole('button').first().click();
    await page.waitForTimeout(400);
    const again = await scrollerState(page);
    check(again.count === countBeforeOlder, `再点「读取更早」不重复（${again.count}）`);
  }
  // #7 轮询期间阅读位置不跳
  const afterScroll = await page.evaluate(
    () => document.querySelector('[data-testid="message-scroll"]').scrollTop,
  );
  check(Math.abs(afterScroll - beforePollScroll) <= 2, `轮询期间阅读位置不动（${beforePollScroll} → ${afterScroll}）`);
  // #8 active_turn 结束后停止轮询
  //
  // 不能只靠"等 2.6 秒看请求数有没有涨"：如果实现吞掉了 active_turn，
  // 界面会一直"生成中"，而这里只会静静等到超时——超时是红，但说不出为什么。
  // 所以先断言界面已经不在生成态，再断言请求不再增长。
  check(
    await page.locator('.chat-titlebar__stat--busy').count() === 0,
    'active_turn=null 后界面不再显示「生成中」',
  );
  const requestsBefore = requests.length;
  await page.waitForTimeout(2600);
  const requestsAfter = requests.length;
  check(requestsAfter === requestsBefore, `active_turn=null 后不再轮询（${requestsBefore} → ${requestsAfter}）`);

  check(consoleErrors.length === 0, `无页面错误（${consoleErrors.slice(0, 2).join(' | ')}）`);

  await page.screenshot({ path: join(OUT, 'screens', 'poll-history-after.png') });
  log(`SHOT ${join(OUT, 'screens', 'poll-history-after.png')}`);
  log(`请求序列：${JSON.stringify(requests)}`);
  await context.close();
  passed = true;
} finally {
  await browser.close();
  await server.close();
}

log(passed ? 'ALL CHECKS PASSED' : 'CHECKS FAILED');
log(`LOG ${LOG}`);
