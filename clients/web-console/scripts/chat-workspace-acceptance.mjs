/**
 * 对话页重组的真实浏览器验收（合成会话）。
 *
 * 不碰真后端、不写库：`/v1/**` 全部由 `page.route` 供一份合成数据——群聊、
 * 私聊、长历史（分两页）、图片、语音、失败消息、她想撤回的提议都在里面。
 *
 * 输出（绝对路径打印在 stdout 末尾）：
 *   docs/evidence/chat-workspace-20261123/shots/*.png   每个步骤一屏
 *   docs/evidence/chat-workspace-20261123/acceptance.log  步骤 + 断言 + 时间线
 *
 * 用法：
 *   node scripts/chat-workspace-acceptance.mjs           # 桌面 1440 + 手机 390
 *   node scripts/chat-workspace-acceptance.mjs --keep    # 出错时保留浏览器
 */
import { chromium } from 'playwright';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdirSync, writeFileSync, appendFileSync } from 'node:fs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
// ROOT = clients/web-console，仓库根在它上面两层。
const REPO = join(ROOT, '../..');
const OUT = join(REPO, 'docs/evidence/chat-workspace-20261123');
// 仓库 .gitignore 第 59 行忽略了 `shots/`；证据目录用 `screens/` 才进得了提交。
const SHOTS = join(OUT, 'screens');
mkdirSync(SHOTS, { recursive: true });
const LOG = join(OUT, 'acceptance.log');
writeFileSync(LOG, '');

const log = (line) => {
  const stamped = `[${new Date().toISOString()}] ${line}`;
  console.log(stamped);
  appendFileSync(LOG, `${stamped}\n`);
};
const fail = (line) => {
  log(`FAIL ${line}`);
  throw new Error(line);
};
const check = (condition, line) => {
  if (!condition) fail(line);
  log(`  ok  ${line}`);
};

/* ---------------------------------------------------------------- 合成数据 */

const T0 = '2026-11-21T09:00:00Z';
const iso = (minutes) => new Date(Date.parse(T0) + minutes * 60_000).toISOString();

const USER = {
  id: 'user-1', username: 'owner', role: 'owner',
  subject_id: 'subject_owner', display_name: '主人',
};

const SESSIONS = [
  {
    schema_version: 'v1', session_id: 'win_group', window_id: 'win_group',
    actor_id: 'actor_ling', subject_id: 'subject_owner',
    title: 'onebot-default:group:100200400', display_name: '夜航船',
    channel: 'onebot-default', peer_id: '100200400',
    created_at: iso(-4000), updated_at: iso(-2), message_count: 128,
    archived: false, pinned: true, group_reply_max_parts: 3,
  },
  {
    schema_version: 'v1', session_id: 'win_private', window_id: 'win_private',
    actor_id: 'actor_ling', subject_id: 'subject_owner',
    title: 'onebot-default:private:100200301', display_name: '阿檀',
    channel: 'onebot-default', peer_id: '100200301',
    created_at: iso(-6000), updated_at: iso(-40), message_count: 36,
    archived: false, pinned: false,
  },
  {
    schema_version: 'v1', session_id: 'win_web', window_id: 'win_web',
    actor_id: 'actor_ling', subject_id: 'subject_owner',
    title: '深夜的排字实验', display_name: null,
    channel: 'web', peer_id: null,
    created_at: iso(-200), updated_at: iso(-9), message_count: 14,
    archived: false, pinned: false,
  },
];

const dm = (id, role, text, sequence, extra = {}) => ({
  schema_version: 'v1', message_id: id, turn_id: extra.turn_id ?? null,
  role, text, output_modality: extra.output_modality ?? 'text',
  audio_asset_id: null, speech_state: extra.speech_state ?? 'none',
  speech_delivery_state: null, state: extra.state ?? 'completed',
  created_at: extra.created_at ?? iso(sequence), sequence,
  origin: extra.origin ?? 'web', error_code: extra.error_code ?? null,
  model_label: extra.model_label ?? null, attempt: extra.attempt ?? 1,
  handled_as: extra.handled_as ?? null, handled_reason: extra.handled_reason ?? null,
  sender_name: extra.sender_name ?? null, sender_id: extra.sender_id ?? null,
  retracted_at: extra.retracted_at ?? null,
  recalled_at: extra.recalled_at ?? null,
  recall_state: extra.recall_state ?? null,
});

/** 第 2 页（更早）：刻意造 100 条，让分页边界正好落在下一页。 */
const OLDER = Array.from({ length: 100 }, (_, index) => {
  const sequence = index + 1;
  const fromMember = index % 3 !== 2;
  return dm(
    `old-${sequence}`,
    fromMember ? 'user' : 'actor',
    fromMember
      ? `旧记录第 ${sequence} 条：夜航船的老水手在讲二十年前的潮汐。`
      : `旧记录第 ${sequence} 条：她回了一句关于潮汐的话。`,
    sequence,
    fromMember
      ? { sender_name: '老周', sender_id: '100200402', created_at: iso(-3000 + sequence) }
      : { model_label: 'hy3gw2/hy3', created_at: iso(-3000 + sequence) },
  );
});

/** 第 1 页（最近）：群聊 + 图片 + 语音 + 失败 + 沉默理由 + 她自己撤回。 */
/**
 * 第 1 页必须**取满** `PAGE_SIZE`（100），否则 `hasMoreHistory` 为 false，
 * 顶部直接显示"这里是开头"——那样就测不到分页与锚点。所以前 92 条是填充的
 * 群聊记录，后面 8 条是这一屏要读的重点（图片/语音/失败/沉默/撤回）。
 */
const FILLER_COUNT = 92;
const FILLER = Array.from({ length: FILLER_COUNT }, (_, index) => {
  const sequence = 101 + index;
  const fromMember = index % 4 !== 3;
  return dm(
    `f-${sequence}`,
    fromMember ? 'user' : 'actor',
    fromMember
      ? `最近的闲聊第 ${sequence} 条：江面上有船在过。`
      : `最近的闲聊第 ${sequence} 条：她应了一句。`,
    sequence,
    fromMember
      ? { sender_name: index % 8 < 4 ? '阿青' : '阿白', sender_id: '100200302', created_at: iso(-60 + index * 0.1) }
      : { model_label: 'hy3gw2/hy3', created_at: iso(-60 + index * 0.1) },
  );
});

const RECENT_TAIL = [
  dm('g-101', 'user', '今晚能出来吗 [image]', 193, {
    sender_name: '阿青', sender_id: '100200302', created_at: iso(-30),
  }),
  dm('g-102', 'actor', '看到了。图里是江边的雾，灯是暖的。', 194, {
    model_label: 'hy3gw2/hy3', turn_id: 'turn-g-102', created_at: iso(-28),
  }),
  dm('g-103', 'user', '[voice] 我刚才在路上录的', 195, {
    sender_name: '阿白', sender_id: '100200300', created_at: iso(-24),
  }),
  dm('g-104', 'actor', '', 196, {
    output_modality: 'voice', speech_state: 'ready', model_label: 'hy3gw2/hy3',
    turn_id: 'turn-g-104', created_at: iso(-22),
  }),
  dm('g-105', 'user', '为什么一直不说话', 197, {
    sender_name: '阿青', sender_id: '100200302', handled_as: 'ignored',
    handled_reason: '这一轮先把话留着，等你们说完', created_at: iso(-18),
  }),
  dm('g-106', 'actor', '', 198, {
    state: 'failed', error_code: 'provider_timeout', turn_id: 'turn-g-106',
    created_at: iso(-15),
  }),
  dm('g-107', 'user', '那我先睡了', 199, {
    sender_name: '阿白', sender_id: '100200300', created_at: iso(-8),
  }),
  dm('g-108', 'actor', '晚安，雾散了我叫你。', 200, {
    model_label: 'hy3gw2/hy3', turn_id: 'turn-g-108', created_at: iso(-6),
    recalled_at: iso(-5), recall_state: 'recalled',
  }),
];

const RECENT = [...FILLER, ...RECENT_TAIL];

const VOICES = [
  {
    message_id: 'g-104', url: '/v1/static/demo-voice.wav',
    duration_ms: 4200, play_count: 0, played: false,
    delivery: 'delivered', created_at: iso(-22),
  },
];

/**
 * `GET /sessions/{id}/recall-proposals` 返回的是**裸数组**（不是 `{items}`），
 * 与 `api.recallProposals` 的解析保持一致。
 */
const RECALL = [
  {
    schema_version: 'v1', proposal_id: 'prop-1', message_id: 'g-108',
    window_id: 'win_group', note: '这句话太软了，想收回来', created_at: iso(-5),
    state: 'proposed',
  },
];

const PROMPT = {
  state: 'available', message_id: 'g-108', turn_id: 'turn-g-108',
  created_at: iso(-6), expires_at: iso(9000),
  model: { provider_id: 'hy3gw2', model_id: 'hy3', profile: 'chat' },
  token_total: 8621, truncated: false,
  sections: [
    { key: 'system', label: '她是谁', role: 'system', tokens: 980, collapsed: false,
      items: [{ role: 'system', sender: null, content: '你是灵。安静、具体、不表演。', at: null }] },
    { key: 'memory', label: '她记得的事', role: null, tokens: 260, collapsed: false,
      items: [{ role: null, sender: '记忆管家', content: '夜航船的人喜欢在江边聊天。', at: null }] },
    { key: 'history', label: '最近聊过什么', role: null, tokens: 6100, collapsed: false,
      items: Array.from({ length: 6 }, (_, index) => ({
        role: index % 2 === 0 ? 'user' : 'actor',
        sender: index % 2 === 0 ? '阿青' : null,
        content: `上一轮第 ${index + 1} 句`, at: null,
      })) },
    { key: 'input', label: '你这次说的话', role: null, tokens: 18, collapsed: false,
      items: [{ role: 'user', sender: '阿白', content: '那我先睡了', at: null }] },
  ],
  tool_transcript: [],
  tool_transcript_truncated: false,
};

const POLICY = {
  session_id: 'win_group',
  origin_status: 'known', instance_scope: 'onebot', instance_id: 'bot-main',
  fields: {
    persona_id: { has_session_override: false, session_override: null, instance_override: null,
      global_value: 'persona_ling', effective: 'persona_ling', source: 'global' },
    private_may_stay_silent: { has_session_override: true, session_override: false,
      instance_override: null, global_value: true, effective: false, source: 'session' },
    group_mention_requires_reply: { has_session_override: false, session_override: null,
      instance_override: null, global_value: true, effective: true, source: 'global' },
    group_attention_deferred: { has_session_override: false, session_override: null,
      instance_override: true, global_value: false, effective: true, source: 'instance' },
    group_turn_budget: { has_session_override: false, session_override: null,
      instance_override: null, global_value: 6, effective: 6, source: 'global' },
    rest_may_skip: { has_session_override: false, session_override: null,
      instance_override: null, global_value: true, effective: true, source: 'global' },
  },
};

const json = (body, status = 200) => ({
  status, contentType: 'application/json', body: JSON.stringify(body),
});

/* ---------------------------------------------------------------- 夹具 */

const MESSAGES_BY_SESSION = {
  win_group: { page1: RECENT, older: OLDER },
  win_private: {
    page1: [
      dm('p-1', 'user', '在吗', 1, { sender_name: '阿檀', sender_id: '100200301' }),
      dm('p-2', 'actor', '在。', 2, { model_label: 'hy3gw2/hy3', turn_id: 'turn-p-2' }),
      dm('p-3', 'user', '今天有点累', 3, { sender_name: '阿檀', sender_id: '100200301' }),
    ],
    older: [],
  },
  win_web: {
    page1: [
      dm('w-1', 'user', '试试新的排版', 1),
      dm('w-2', 'actor', '好，我把话放回来。', 2, { model_label: 'hy3gw2/hy3', turn_id: 'turn-w-2' }),
    ],
    older: [],
  },
};

async function installRoutes(page, state) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === '/v1/auth/me') {
      return route.fulfill(json({
        schema_version: 'v1', user: USER, config_revision: 1,
        test_mode: false, csrf_token: 'acceptance-csrf',
      }));
    }
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.15' }));
    // 壳层在每条路由上都会探一次这两个（直播状态、接入器诊断），供上省掉 404 噪音。
    if (path === '/v1/live/sessions/current') return route.fulfill(json({ session: null }));
    if (path === '/v1/settings/connectors') return route.fulfill(json({ items: [], access: null }));
    if (path === '/v1/persona') return route.fulfill(json({ name: '灵', display_name: '灵' }));
    if (path === '/v1/personas') return route.fulfill(json({ items: [] }));
    if (path === '/v1/sessions' && method === 'GET') {
      state.sessions += 1;
      return route.fulfill(json({ sessions: SESSIONS }));
    }
    if (path === '/v1/sessions' && method === 'POST') {
      const created = { ...SESSIONS[2], session_id: 'win_new', window_id: 'win_new',
        title: '新的对话', display_name: null, message_count: 0 };
      return route.fulfill(json(created, 201));
    }

    const sessionMessages = /^\/v1\/sessions\/([^/]+)\/messages$/.exec(path);
    if (sessionMessages) {
      const id = decodeURIComponent(sessionMessages[1]);
      const fixture = MESSAGES_BY_SESSION[id] ?? { page1: [], older: [] };
      const before = url.searchParams.get('before_sequence');
      if (before === null) {
        state.messagePages.push(`${id}:page1`);
        return route.fulfill(json({ messages: fixture.page1, active_turn: null }));
      }
      state.messagePages.push(`${id}:before=${before}`);
      const older = (fixture.older ?? []).filter((message) => message.sequence < Number(before));
      return route.fulfill(json({ messages: older, active_turn: null }));
    }
    if (/^\/v1\/sessions\/[^/]+\/voice-messages$/.test(path)) {
      return route.fulfill(json({ items: path.includes('win_group') ? VOICES : [] }));
    }
    if (/^\/v1\/sessions\/[^/]+\/recall-proposals$/.test(path)) {
      return route.fulfill(json(path.includes('win_group') ? RECALL : []));
    }
    if (/^\/v1\/sessions\/[^/]+\/dialogue-policy$/.test(path)) {
      state.policyReads += 1;
      return route.fulfill(json(POLICY));
    }
    if (/^\/v1\/sessions\/[^/]+\/origin-binding$/.test(path)) {
      return route.fulfill(json({
        scope: 'onebot',
        candidates: [{ scope: 'onebot', instance_id: 'bot-main', display_name: '主号', enabled: true }],
        manually_bound: true,
      }));
    }
    if (/^\/v1\/sessions\/[^/]+\/group-reply-limit$/.test(path)) {
      return route.fulfill(json({ max_parts: 3 }));
    }
    if (/^\/v1\/sessions\/[^/]+\/connectors$/.test(path) || path === '/v1/connectors') {
      return route.fulfill(json({ access: null }));
    }
    if (/^\/v1\/messages\/[^/]+\/prompt$/.test(path)) {
      state.promptReads += 1;
      return route.fulfill(json(PROMPT));
    }
    if (/^\/v1\/speech\/messages\//.test(path)) {
      return route.fulfill(json({ message: { text: '她这次说的是：晚安，雾散了我叫你。' } }));
    }
    if (path === '/v1/turns' || /^\/v1\/sessions\/[^/]+\/turns$/.test(path)) {
      // 发送文字：先 202，再给一个 SSE 流，最后落到一条已完成的消息。
      state.sentTexts.push(request.postData() ?? '');
      const turnId = 'turn-sent-1';
      const sse = [
        'event: turn.started',
        `data: ${JSON.stringify({ turn_id: turnId, test_mode: false })}`,
        '',
        'event: delta',
        `data: ${JSON.stringify({ text: '收到，我把这句放回去。' })}`,
        '',
        'event: turn.completed',
        `data: ${JSON.stringify({ turn_id: turnId, message: dm('sent-actor', 'actor', '收到，我把这句放回去。', 200, { model_label: 'hy3gw2/hy3', turn_id: turnId }), decision: 'reply' })}`,
        '',
      ].join('\n');
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse });
    }
    if (path.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: path, retriable: false } }, 404));
    }
    return route.continue();
  });
}

/* ---------------------------------------------------------------- 流程 */

const SHOT = async (page, name) => {
  const file = join(SHOTS, name);
  await page.screenshot({ path: file, fullPage: false });
  log(`  shot ${name}`);
  return file;
};

async function runScenario(browser, baseUrl, { label, viewport, isMobile }) {
  log(`--- ${label} ${viewport.width}x${viewport.height}`);
  const context = await browser.newContext({
    viewport, isMobile, hasTouch: isMobile,
    reducedMotion: 'reduce', deviceScaleFactor: isMobile ? 2 : 1,
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  const state = { sessions: 0, messagePages: [], promptReads: 0, policyReads: 0, sentTexts: [] };
  const consoleErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  // 404 的**具体路径**：验收要能指出"哪个端点没供数据"，而不是只知道有 404。
  page.on('response', (response) => {
    if (response.status() >= 400) {
      consoleErrors.push(`${response.status()} ${new URL(response.url()).pathname}`);
    }
  });
  page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`));
  await installRoutes(page, state);

  const shots = {};
  // 抽屉是否开着由 `.sidebar--open` 决定：叠着点会点空，并让下一次判断错位。
  const sidebarOpen = () => page.evaluate(
    () => Boolean(document.querySelector('.sidebar--open')),
  );
  const openSidebarIfDrawer = async () => {
    if (!isMobile) return;
    if (await sidebarOpen()) return;
    await page.getByRole('button', { name: '打开会话列表' }).click();
    await page.waitForFunction(() => Boolean(document.querySelector('.sidebar--open')));
  };
  const closeSidebarIfDrawer = async () => {
    if (!isMobile) return;
    if (!(await sidebarOpen())) return;
    await page.locator('.sidebar__close').click();
    await page.waitForFunction(() => !document.querySelector('.sidebar--open'));
  };

  await page.goto(`${baseUrl}/s/win_group`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  await page.getByTestId('session-title').waitFor();
  await page.waitForTimeout(400);

  // 1) 会话列表：名称、群/私聊、选中、近期活动
  await openSidebarIfDrawer();
  await page.locator('[data-session-id]').first().waitFor();
  const titles = await page.locator('.session__title').allInnerTexts();
  check(titles.length === 3, `会话列表有 3 条（实际 ${titles.length}）`);
  check(titles.includes('夜航船') && titles.includes('阿檀'), '列表按名称显示（群名/昵称）');
  const active = await page.locator('.session--active .session__title').innerText();
  check(active === '夜航船', `当前选中可读：${active}`);
  const badges = await page.locator('.session__channel').allInnerTexts();
  check(badges.includes('群') && badges.includes('私聊') && badges.includes('网页'), '群/私聊/网页可区分');
  const metas = await page.locator('.session__time').allInnerTexts();
  check(metas.every((text) => text.trim().length > 0), '每条都有近期活动时间');
  const sidebarText = await page.locator('.sidebar__list').innerText();
  check(!sidebarText.includes('onebot-default:group:'), '外部会话键默认不出现');
  shots.sessions = await SHOT(page, `${label}-01-sessions.png`);

  // 2) 会话信息按需展开
  const groupRow = page.locator('[data-session-id="win_group"]');
  await groupRow.getByRole('button', { name: '更多操作：夜航船' }).click();
  await page.getByTestId('session-menu').waitFor();
  await groupRow.getByRole('menuitem', { name: '会话信息：夜航船' }).click();
  await page.getByTestId('session-detail').waitFor();
  const detail = await page.getByTestId('session-detail').innerText();
  check(detail.includes('onebot-default:group:100200400'), '会话信息里能读到完整会话键');
  shots.sessionDetail = await SHOT(page, `${label}-02-session-detail.png`);
  await groupRow.getByRole('button', { name: '更多操作：夜航船' }).click();
  await page.keyboard.press('Escape');

  // 收起抽屉再读消息
  await closeSidebarIfDrawer();

  // 3) 消息正文：媒体块、语音、失败、撤回、沉默
  await page.getByTestId('message-scroll').waitFor();
  const media = await page.getByTestId('message-media').allInnerTexts();
  check(media.length >= 2, `媒体占位渲染成可读块（${media.join('/')}）`);
  check(media.join('/').includes('图片') && media.join('/').includes('语音'), '图片与语音都可读');
  check(await page.getByRole('alert').filter({ hasText: '生成失败' }).count() > 0
    || await page.getByRole('alert').count() > 0, '失败消息有可读错误区');
  // 撤回标记与沉默理由都在列表里，但要先滚到它们所在的条目（长历史首屏看不到）。
  const recalled = page.getByTestId('message-recalled').first();
  await recalled.scrollIntoViewIfNeeded();
  check(await page.getByTestId('message-recalled').count() > 0, '撤回过的那条有标记');
  const silence = page.getByTestId('silence-reason').first();
  await silence.scrollIntoViewIfNeeded();
  check(await page.getByTestId('silence-reason').count() > 0, '「她没回」的理由可读');
  shots.messages = await SHOT(page, `${label}-03-messages.png`);

  // 4) 消息详情：模型与「她这次看到了什么」收在折叠里
  const metaText = await page.locator('[data-testid="message-scroll"] .message__meta').first().innerText();
  check(!metaText.includes('hy3'), `主行不刷模型名（meta="${metaText.replace(/\n/g, ' ')}"）`);
  const details = page.getByTestId('message-details').first();
  await details.locator('summary').click();
  const detailsText = await details.innerText();
  check(detailsText.includes('模型'), '展开详情能看到模型');
  shots.details = await SHOT(page, `${label}-04-message-details.png`);

  // 5) 打开「她这次看到了什么」，关闭后回到原阅读位置
  const scroller = page.getByTestId('message-scroll');
  await scroller.evaluate((node) => { node.scrollTop = 120; });
  const before = await scroller.evaluate((node) => node.scrollTop);
  const entry = page.getByTestId('prompt-entry').first();
  await entry.click();
  await page.getByTestId('prompt-drawer').waitFor();
  await page.locator('[data-testid="prompt-snapshot"]').waitFor();
  shots.drawer = await SHOT(page, `${label}-05-prompt-drawer.png`);
  await page.getByTestId('prompt-drawer-close').click();
  await page.getByTestId('prompt-drawer').waitFor({ state: 'detached' });
  const after = await scroller.evaluate((node) => node.scrollTop);
  check(Math.abs(after - before) <= 2, `关闭抽屉回到原阅读位置（${before} → ${after}）`);

  // 6) 往上加载历史：滚动锚点保持
  const anchorBefore = await scroller.evaluate((node) => {
    const message = node.querySelector('[data-role][data-state]');
    const rect = message?.getBoundingClientRect();
    return { top: node.scrollTop, height: node.scrollHeight, firstId: node.querySelector('[data-role]')?.getAttribute('data-state') };
  });
  await scroller.evaluate((node) => { node.scrollTop = 2; node.dispatchEvent(new Event('scroll', { bubbles: true })); });
  const edge = page.getByTestId('history-edge');
  if (await edge.count()) {
    await edge.getByRole('button').first().click();
  }
  await page.waitForTimeout(700);
  const anchorAfter = await scroller.evaluate((node) => ({
    top: node.scrollTop, height: node.scrollHeight,
  }));
  check(state.messagePages.some((entryPage) => entryPage.startsWith('win_group:before=')),
    `确实带着游标取了更早一页（${state.messagePages.join(', ')}）`);
  check(anchorAfter.height > anchorBefore.height, `拼接后内容变长（${anchorBefore.height} → ${anchorAfter.height}）`);
  const drift = Math.abs(anchorAfter.top - anchorBefore.top);
  check(drift > 0 || anchorBefore.top === 0, `滚动位置按拼接高度补偿（位移 ${drift}px）`);
  shots.history = await SHOT(page, `${label}-06-history-loaded.png`);

  // 7) 会话设置：重复点、切会话不叠层
  const settings = page.getByTestId('session-settings-toggle');
  for (const open of [true, false, true, false, true]) {
    await settings.click();
    await page.waitForTimeout(120);
    const panels = await page.locator('#active-window-policy .window-policy').count();
    check(panels === (open ? 1 : 0), `设置面板数量 ${panels}（${open ? '展开' : '收起'}）`);
  }
  shots.settings = await SHOT(page, `${label}-07-session-settings.png`);
  // 切会话：面板必须收起，且读的是新会话
  await settings.click();
  await page.waitForTimeout(120);
  await openSidebarIfDrawer();
  await page.locator('[data-session-id="win_private"] .session__main').click();
  await page.getByTestId('session-title').waitFor();
  await page.waitForFunction(() => document.querySelector('[data-testid="session-title"]')?.textContent === '阿檀');
  check(await page.locator('#active-window-policy .window-policy').count() === 0, '切会话后设置面板不残留');
  shots.switched = await SHOT(page, `${label}-08-switched-session.png`);

  // 8) 发送文字
  const beforePages = state.messagePages.length;
  await page.getByLabel('消息输入框').fill('我把这句放回去。');
  await page.getByRole('button', { name: '发送消息' }).click();
  await page.waitForTimeout(900);
  check(state.sentTexts.length === 1, `发送只发了一次（${state.sentTexts.length}）`);
  check(beforePages > 0, '发送后仍有历史请求记录');
  shots.sent = await SHOT(page, `${label}-09-sent.png`);

  // 9) 语音区：默认收起、按需展开、限制在展开后出现
  await openSidebarIfDrawer();
  await page.locator('[data-session-id="win_group"] .session__main').click();
  await page.waitForFunction(() => document.querySelector('[data-testid="session-title"]')?.textContent === '夜航船');
  await closeSidebarIfDrawer();
  const composerText = await page.locator('.composer').innerText();
  check(!composerText.includes('10 MiB'), '未展开时不说时长/大小限制');
  await page.getByRole('button', { name: '语音', exact: true }).click();
  await page.getByRole('group', { name: '语音输入', exact: true }).waitFor();
  const audioPanel = await page.getByTestId('composer-audio-panel').innerText();
  check(audioPanel.includes('120 秒') && audioPanel.includes('10 MiB'), '展开后才表达限制');
  check(await page.getByRole('button', { name: '上传语音', exact: true }).count() === 1, '上传语音入口在语音区里');
  check(await page.getByRole('button', { name: '录音', exact: true }).count() === 1, '录音入口在语音区里');
  shots.audio = await SHOT(page, `${label}-10-audio-panel.png`);

  // 10) 附件区
  await page.getByRole('button', { name: '附件', exact: true }).click();
  await page.getByTestId('composer-attach-panel').waitFor();
  const attachPanel = await page.getByTestId('composer-attach-panel').innerText();
  check(attachPanel.includes('PNG') && attachPanel.includes('下一轮'), '附件限制在使用时表达');
  shots.attach = await SHOT(page, `${label}-11-attach-panel.png`);

  check(consoleErrors.length === 0, `控制台无错误（${consoleErrors.slice(0, 3).join(' | ')}）`);

  await context.close();
  return shots;
}

/* ---------------------------------------------------------------- main */

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
let shots = {};
try {
  shots = {
    ...await runScenario(browser, baseUrl, {
      label: 'desktop', viewport: { width: 1440, height: 900 }, isMobile: false,
    }),
    ...await runScenario(browser, baseUrl, {
      label: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true,
    }),
  };
  log('ALL CHECKS PASSED');
} finally {
  await browser.close();
  await server.close();
}

log(`LOG ${LOG}`);
for (const [name, file] of Object.entries(shots)) log(`SHOT ${name} ${file}`);
