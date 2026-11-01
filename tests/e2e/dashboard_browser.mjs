/**
 * 总览页（/dashboard）的浏览器实测：桌面 1440×900 + 手机 375×667。
 *
 * 这个脚本存在的理由：`/dashboard` 曾经把 `DashboardPage` 渲染进
 * `<header class="app-header">`，主区又因为三元链没有 `dashboard` 分支而落到
 * `<PersonaPage/>`。单测和 typecheck 全程是绿的——没有任何测试看过真实布局，
 * 所以 1440 下 12 列网格每列只有 8.05px、文字每行 2–3 字、主区是「角色设定」，
 * 都没人发现。这里用真 Chromium 断言布局本身。
 *
 * 特点：
 * - 全部 `/v1/**` 由 Playwright 拦截并 mock（含 `/v1/dashboard` 的 9 个分段），
 *   因此**不需要后端起在某个端口**，结果也稳定可复现；
 * - 只要求一个静态可访问的前端地址（`vite dev` 或 `vite preview` 的隔离端口），
 *   登录态同样由 mock 提供；
 * - BASE_URL 必须显式给出、必须指向本机，且不能是共享的 8080。
 *
 * 每个视口断言：
 * 1. 页头里没有总览（`header.app-header .dash` 为 null、页头文本不含「今天的安排」），
 *    总览在 `<main class="app-main">` 里，且整页只有一个 `<main>`；
 * 2. 标题是「总览」，9 个分段（此刻/情绪/可用性/主动联系/今天的安排/最近记住的事/
 *    近期生活/关系/渠道与模型）都在且可见；
 * 3. 不出现 PersonaPage 的特征文本（系统提示（人格设定）/ 叙事边界 / 角色设定 / 保存设定）；
 * 4. `document.documentElement.scrollWidth <= window.innerWidth`（无横向溢出）；
 * 5. 卡片网格列宽合理：桌面 12 列每列 ≥ 60px、卡片宽 ≥ 300px、卡片之间不重叠、
 *    手机单列且卡片占满内容宽；
 * 6. 每张卡片都标了「更新于」（快照不许说成实时）；
 * 7. 情绪是中文（平静，不是 calm）；日程带日期、昨天的日程标「不是今天」；
 *    已取消 / 送不出去的承诺标出状态（F4/F5）；
 * 8. 手机上「详细」「刷新」命中区 ≥44×44，桌面仍是原来的小字号文字链（L2）；
 * 9. 卡片与「详细」hover 后 computed style 有变化（L3/F6）；
 * 10. 导航点「总览」能回到总览页（导航没被接错位置；用 testid 定位，不依赖顶栏类名）；
 * 11. 点「刷新」会真的再取一次 `/v1/dashboard`。
 *
 * 另外三组场景：
 * - 未知路径 `/qa-no-such-route`（桌面 + 手机）：渲染 `data-testid="unknown-route"`、
 *   没有 textarea/聊天布局、点「回到总览」真的到 `/dashboard`（M1）；
 * - 分段级 `unavailable`：卡内告警条与空态指纹不同，且不触发整页 `.dash-error`（L1）；
 * - 账号页 `/settings/account`：新的登录名默认空白、无常驻提示、按"名字+当前密码"启用（F11）。
 *
 * 用法：
 *   cd clients/web-console && pnpm exec vite --port 47901 --strictPort &
 *   BASE_URL=http://127.0.0.1:47901 node tests/e2e/dashboard_browser.mjs
 *
 * 环境变量：BASE_URL（必填）、SHOTS_DIR（默认临时目录，不写仓库）、CHROME_PATH。
 * 任一断言失败以非 0 退出。
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

import { resolveShotsDir } from './lib/isolation.mjs';

const RAW_BASE = process.env.BASE_URL;
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
// 截图默认写临时目录：不默认往仓库的 shots/ 里写，免得覆盖别人的证据。
const SHOTS_INFO = resolveShotsDir('dashboard');
const SHOTS = SHOTS_INFO.dir;
console.log(
  `截图目录：${SHOTS}` +
    (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定）' : '（未设 SHOTS_DIR，默认临时目录，不写仓库）'),
);

function failConfig(message) {
  console.error(`配置错误：${message}`);
  console.error('用法：BASE_URL=http://127.0.0.1:<隔离端口> node tests/e2e/dashboard_browser.mjs');
  process.exit(2);
}

if (!RAW_BASE) failConfig('必须显式设置 BASE_URL，不能使用默认值');
let BASE;
try {
  BASE = new URL(RAW_BASE);
} catch {
  failConfig(`BASE_URL 不是合法 URL：${RAW_BASE}`);
}
const basePort = BASE.port ? Number(BASE.port) : BASE.protocol === 'https:' ? 443 : 80;
if (!['127.0.0.1', 'localhost', '[::1]', '::1'].includes(BASE.hostname)) {
  failConfig(`BASE_URL 必须指向本机隔离实例，收到 ${BASE.hostname}`);
}
if (basePort === 8080) {
  failConfig('BASE_URL 不能是共享的 8080；请另起静态端口（例如 47901）');
}
if (!Number.isInteger(basePort) || basePort <= 0) {
  failConfig(`BASE_URL 端口不合法：${RAW_BASE}`);
}

/**
 * 解析 playwright：`clients/web-console` 是它的正式安装位置（devDependency + lockfile），
 * 先从这里取，取不到再退回普通模块解析。
 */
async function loadChromium() {
  const attempts = [];
  const fromWebConsole = createRequire(
    path.join(REPO_ROOT, 'clients', 'web-console', 'package.json'),
  );
  try {
    const mod = fromWebConsole('playwright');
    const version = fromWebConsole('playwright/package.json')?.version ?? 'unknown';
    return { chromium: mod.chromium, origin: `clients/web-console (playwright ${version})` };
  } catch (error) {
    attempts.push(`clients/web-console → ${error?.code || error?.message}`);
  }
  try {
    const mod = await import('playwright');
    return { chromium: mod.chromium, origin: "本地 node_modules（import('playwright')）" };
  } catch (error) {
    attempts.push(`import('playwright') → ${error?.code || error?.message}`);
  }
  console.error('配置错误：找不到 playwright。');
  for (const line of attempts) console.error(` - ${line}`);
  console.error('修复：cd clients/web-console && pnpm install（playwright 已列为 devDependency）');
  process.exit(2);
}

const { chromium, origin: PLAYWRIGHT_ORIGIN } = await loadChromium();
const EXECUTABLE = process.env.CHROME_PATH || undefined;
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
function record(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
}

async function waitFor(predicate, timeout = 15000, interval = 120) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      if (await predicate()) return true;
    } catch {
      /* 页面还在导航/重渲染，下一轮再试 */
    }
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
  return false;
}

/* ------------------------------------------------------------------ mocks */

/** 以"今天"为基准生成本地时间 ISO：日期相关断言不能写死，否则隔天就漂。 */
function localIso(daysOffset, hour, minute = 0) {
  const d = new Date();
  d.setDate(d.getDate() + daysOffset);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

/** 前端渲染的「月-日 时:分」（本地时区），用于断言日期真的显示出来了。 */
function formatLocalDayTime(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 9 个分段都非空的总览载荷，形状照抄真实 `/v1/dashboard` 响应。 */
function dashboardPayload() {
  const asOf = '2026-10-30T05:48:02.650639+00:00';
  const yesterdayPlanStart = localIso(-1, 15, 0);
  const todayPlanStart = localIso(0, 20, 0);
  return {
    schema_version: 'v1',
    generated_at: '2026-10-30T05:48:02.665518+00:00',
    timezone: 'Asia/Shanghai',
    sections: {
      activity: {
        status: 'ok',
        as_of: asOf,
        data: { activity_id: 'act_1', kind: 'reading', state: 'running', place_id: 'home', origin: 'simulated', started_at: asOf },
      },
      affect: { status: 'ok', as_of: asOf, data: { emotion: 'calm', intensity: 0.4, cause: '刚看完一章书', updated_at: asOf } },
      presence: {
        status: 'ok',
        as_of: asOf,
        data: { availability: 'available', energy: 82, social_load: 12, describe: '状态正常，可以随时说话' },
      },
      proactive: {
        status: 'ok',
        as_of: asOf,
        data: {
          pending_count: 2,
          delivery_enabled: false,
          gate_reason: 'OneBot 没有声明支持主动推送（LING_ONEBOT_PROACTIVE=0）',
          top: { opportunity_id: 'opp_1', kind: 'greeting', state: 'drafted', draft_text: '在做什么呢？' },
        },
      },
      agenda: {
        status: 'ok',
        as_of: asOf,
        data: {
          plans: [
            // 昨天的日程：后端 `upcoming()` 不按"今天"过滤，照样会返回
            {
              plan_id: 'plan_old',
              title: '给无人机小兔子做保养',
              start_at: yesterdayPlanStart,
              end_at: yesterdayPlanStart,
              commitment_level: 'tentative',
              state: 'planned',
            },
            {
              plan_id: 'plan_today',
              title: '在埃利蒙德3区散步',
              start_at: todayPlanStart,
              end_at: todayPlanStart,
              commitment_level: 'confirmed',
              state: 'planned',
            },
          ],
          commitments: [
            // 送不出去 / 已取消：界面必须标出来，不能当成待履行
            { commitment_id: 'cmt_undeliverable', text: '提醒我周五交那份材料', due_at: localIso(-3, 15, 22), state: 'undeliverable' },
            { commitment_id: 'cmt_cancelled', text: '周末提醒你买书', due_at: localIso(365, 12, 0), state: 'cancelled' },
            { commitment_id: 'cmt_active', text: '帮你看一下那份稿子', due_at: localIso(2, 12, 0), state: 'active' },
          ],
          goals: [{ goal_id: 'goal_1', title: '把书稿写完', state: 'active' }],
        },
      },
      memories: {
        status: 'ok',
        as_of: asOf,
        data: { items: [{ memory_id: 'mem_1', kind: 'preference', text: '喜欢喝常温的美式', origin: 'user_statement', confidence: 0.8 }] },
      },
      timeline: {
        status: 'ok',
        as_of: asOf,
        data: { items: [{ experience_id: 'exp_1', kind: 'activity', summary: '在埃利蒙德3区散步', origin: 'simulated', occurred_at: '2026-10-30T03:02:00+00:00' }] },
      },
      relationship: {
        status: 'ok',
        as_of: asOf,
        data: { familiarity: 0.9, trust: 0.54, boundaries: [], last_interaction_at: '2026-10-27T15:25:13+00:00' },
      },
      connectivity: {
        status: 'ok',
        as_of: asOf,
        data: {
          channels: [
            { channel: 'qqbot', configured: true, reason: '', proactive_ready: false, proactive_reason: '需要单独申请主动推送权限' },
            { channel: 'onebot', configured: false, reason: '未配置 LING_ONEBOT_*' },
          ],
          bound_model_roles: ['dialogue.deep', 'dialogue.fast'],
        },
      },
    },
  };
}

const EXPECTED_SECTIONS = [
  '此刻',
  '情绪',
  '可用性',
  '主动联系',
  '今天的安排',
  '最近记住的事',
  '近期生活',
  '关系',
  '渠道与模型',
];

const PERSONA_TEXT = ['系统提示（人格设定）', '叙事边界', '角色设定', '保存设定'];

function fulfillJson(route, status, body) {
  return route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function attachMocks(context, state) {
  await context.route('**/v1/**', async (route) => {
    const request = route.request();
    const method = request.method();
    const pathname = new URL(request.url()).pathname;

    if (pathname === '/v1/auth/me' && method === 'GET') {
      return fulfillJson(route, 200, {
        schema_version: 'v1',
        user: { id: 'u-mock', username: 'ling', role: 'owner', subject_id: 'subj-mock', display_name: 'Ling', must_change_password: false },
        config_revision: 1,
        test_mode: false,
        csrf_token: 'mock-csrf',
      });
    }

    if (pathname === '/v1/dashboard' && method === 'GET') {
      state.dashboardRequests += 1;
      return fulfillJson(route, 200, state.dashboard);
    }

    if (pathname === '/v1/sessions' && method === 'GET') {
      return fulfillJson(route, 200, { sessions: [] });
    }

    // 其余接口一律 404：本脚本只关心总览页，别的页面渲染成什么不重要。
    return fulfillJson(route, 404, {
      error: { code: 'not_found', message: `mock 未处理的接口 ${method} ${pathname}`, retriable: false },
    });
  });
}

/* -------------------------------------------------------------- 断言工具 */

async function bodyText(page) {
  return (await page.textContent('body')) || '';
}

async function shot(page, name, fullPage = false) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage });
}

/** 页面上的实测数字：布局、分段、溢出都从这里取。 */
async function measure(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const cards = [...document.querySelectorAll('.dash-card')];
    const overlap = [];
    for (let i = 0; i < cards.length; i += 1) {
      for (let j = i + 1; j < cards.length; j += 1) {
        const a = cards[i].getBoundingClientRect();
        const b = cards[j].getBoundingClientRect();
        const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ox > 1 && oy > 1) overlap.push({ i, j, ox: Math.round(ox), oy: Math.round(oy) });
      }
    }
    const grid = document.querySelector('.dash__grid');
    const gridStyle = grid ? getComputedStyle(grid) : null;
    const columnWidths = gridStyle
      ? gridStyle.gridTemplateColumns.split(/\s+/).map((value) => Number.parseFloat(value)).filter((n) => Number.isFinite(n))
      : [];
    return {
      pathname: window.location.pathname,
      mainCount: document.querySelectorAll('main.app-main').length,
      dashInHeader: document.querySelectorAll('header.app-header .dash').length,
      dashInMain: document.querySelectorAll('main.app-main .dash').length,
      headerText: (document.querySelector('header.app-header')?.textContent || '').trim(),
      title: (document.querySelector('main.app-main h1')?.textContent || '').trim(),
      cardTitles: [...document.querySelectorAll('.dash-card__title')].map((el) => (el.textContent || '').trim()),
      cardWidths: cards.map((card) => Math.round(card.getBoundingClientRect().width * 100) / 100),
      visibleCards: cards.filter((card) => {
        const r = card.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }).length,
      asOfCount: [...document.querySelectorAll('.dash-card__meta')].filter((el) => (el.textContent || '').includes('更新于')).length,
      overlap,
      grid: rect('.dash__grid'),
      page: rect('.page.dash'),
      main: rect('main.app-main'),
      columnWidths,
      firstAction: rect('.dash-card__action'),
      firstRefresh: rect('.dash__refresh'),
      actionFontSize: (() => {
        const el = document.querySelector('.dash-card__action');
        return el ? getComputedStyle(el).fontSize : '';
      })(),
      gap: gridStyle ? Number.parseFloat(gridStyle.columnGap) || 0 : 0,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      stylesheets: document.styleSheets.length,
    };
  });
}

function near(a, b, tolerance = 2) {
  return Math.abs(a - b) <= tolerance;
}

/** 悬停后 computed style 有没有变化（F6/L3：此前卡片与「详细」毫无反馈）。 */
async function hoverChange(page, selector) {
  const read = () =>
    page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const cs = getComputedStyle(el);
      return [cs.backgroundColor, cs.borderTopColor, cs.boxShadow, cs.color].join('|');
    }, selector);
  const before = await read();
  await page.locator(selector).first().hover();
  await page.waitForTimeout(150);
  const after = await read();
  return { before, after, changed: before !== after };
}

async function openDashboard(page) {
  await page.goto(`${BASE.origin}/dashboard`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.dash__grid', { timeout: 20000 });
}

/* -------------------------------------------------------------- 场景 */

async function runViewport(browser, viewport, label) {
  const state = { dashboard: dashboardPayload(), dashboardRequests: 0 };
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    // mock 的 404 会打出资源加载错误，这不是前端异常（未捕获异常由 pageerror 记录）。
    if (/favicon/i.test(text) || /Failed to load resource/i.test(text)) return;
    consoleErrors.push(text);
  });
  page.on('pageerror', (error) => {
    const text = String(error);
    pageErrors.push(text);
  });
  await attachMocks(context, state);

  await openDashboard(page);
  const m = await measure(page);
  const text = await bodyText(page);

  record(`${label}：URL 是 /dashboard`, m.pathname === '/dashboard', m.pathname);
  record(`${label}：CSS 已加载`, m.stylesheets >= 1, `styleSheets=${m.stylesheets}`);

  /* ---- 位置：总览在主区，不在页头 ---- */
  record(`${label}：整页只有一个 <main>`, m.mainCount === 1, `main=${m.mainCount}`);
  record(`${label}：页头里没有总览`, m.dashInHeader === 0, `header 内 .dash=${m.dashInHeader}`);
  record(`${label}：总览在 <main> 内`, m.dashInMain === 1, `main 内 .dash=${m.dashInMain}`);
  record(
    `${label}：页头文本只有 brand/导航/用户名`,
    !m.headerText.includes('今天的安排') && !m.headerText.includes('更新于'),
    JSON.stringify(m.headerText.slice(0, 60)),
  );

  /* ---- 内容 ---- */
  record(`${label}：标题是「总览」`, m.title === '总览', m.title || '(空)');
  const missing = EXPECTED_SECTIONS.filter((title) => !m.cardTitles.includes(title));
  record(`${label}：9 个分段都在`, m.cardTitles.length === 9 && missing.length === 0, `卡片=${m.cardTitles.length} 缺失=${missing.join('、') || '无'}`);
  record(`${label}：9 张卡片都可见`, m.visibleCards === 9, `可见=${m.visibleCards}`);
  const personaHits = PERSONA_TEXT.filter((marker) => text.includes(marker));
  record(`${label}：没有角色页文本`, personaHits.length === 0, personaHits.join('、'));
  record(
    `${label}：总览内容真的渲染了`,
    text.includes('给无人机小兔子做保养') && text.includes('喜欢喝常温的美式') && text.includes('条候选'),
  );
  record(`${label}：每张卡片都标了「更新于」`, m.asOfCount === 9, `带时间的卡片=${m.asOfCount}`);

  /* ---- F5：情绪是中文，不是原始英文枚举 ---- */
  record(
    `${label}：情绪显示中文（平静），不显示 calm`,
    text.includes('平静') && !/\bcalm\b/.test(text),
    JSON.stringify(text.match(/calm[^\n]{0,10}/)?.[0] ?? ''),
  );

  /* ---- F4：日程带日期、承诺带状态 ---- */
  const agenda = state.dashboard.sections.agenda.data;
  const yesterday = formatLocalDayTime(agenda.plans[0].start_at);
  const todayPlan = formatLocalDayTime(agenda.plans[1].start_at);
  record(
    `${label}：日程带日期（不是裸 HH:mm）`,
    text.includes(yesterday) && text.includes(todayPlan),
    `期望 ${yesterday} / ${todayPlan}`,
  );
  const flagCount = await page.locator('.dash-list__flag').count();
  record(
    `${label}：昨天的日程标「不是今天」，今天的没标`,
    flagCount === 1 && text.includes('不是今天'),
    `flag=${flagCount}`,
  );
  record(
    `${label}：已取消/送不出去的承诺标了状态`,
    text.includes('已取消') && text.includes('送不出去') && text.includes('待履行'),
  );
  // 状态不能只是"多几个字"：失效状态要用警示色，和待履行区分开
  // （dashboard.css 先于 styles.css 加载，这里顺带锁特异度是否够）。
  const stateColors = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('[data-testid="dash-今天的安排"] .dash-list__row')];
    const read = (needle) => {
      const row = rows.find((r) => (r.innerText || '').includes(needle));
      const meta = row?.querySelector('.dash-list__meta');
      return meta ? getComputedStyle(meta).color : null;
    };
    return { cancelled: read('周末提醒你买书'), active: read('帮你看一下那份稿子') };
  });
  record(
    `${label}：失效承诺用警示色（与待履行不同）`,
    stateColors.cancelled === 'rgb(125, 95, 34)' && stateColors.active !== stateColors.cancelled,
    JSON.stringify(stateColors),
  );

  /* ---- L2：手机触控目标 ≥44×44（桌面保持原密度，不做视觉放大） ---- */
  const action = m.firstAction;
  const refresh = m.firstRefresh;
  if (viewport.width < 900) {
    record(
      `${label}：「详细」命中区 ≥44×44`,
      Boolean(action) && action.width >= 44 && action.height >= 44,
      `${action?.width?.toFixed(1)}×${action?.height?.toFixed(1)}（修复前 22.5×17.6）`,
    );
    record(
      `${label}：「刷新」命中区 ≥44×44`,
      Boolean(refresh) && refresh.width >= 44 && refresh.height >= 44,
      `${refresh?.width?.toFixed(1)}×${refresh?.height?.toFixed(1)}（修复前 51.5×23.2）`,
    );
  } else {
    record(
      `${label}：桌面「详细」仍是小字号文字链（未放大视觉密度）`,
      Boolean(action) && action.height < 24 && Number.parseFloat(m.actionFontSize) <= 13,
      `${action?.width?.toFixed(1)}×${action?.height?.toFixed(1)} font=${m.actionFontSize}`,
    );
  }

  /* ---- 布局 ---- */
  record(
    `${label}：无横向溢出`,
    m.scrollWidth <= m.innerWidth,
    `scrollWidth=${m.scrollWidth} innerWidth=${m.innerWidth}`,
  );
  record(`${label}：卡片之间不重叠`, m.overlap.length === 0, JSON.stringify(m.overlap));
  record(`${label}：页面容器不是竖条`, (m.page?.width ?? 0) >= 300, `page=${m.page?.width ?? 0}px`);

  if (viewport.width >= 1200) {
    const minColumn = m.columnWidths.length ? Math.min(...m.columnWidths) : 0;
    record(
      `${label}：网格是 12 列`,
      m.columnWidths.length === 12,
      `${m.columnWidths.length} 列`,
    );
    record(
      `${label}：12 列每列 ≥ 60px`,
      m.columnWidths.length === 12 && minColumn >= 60,
      `最小列宽=${Math.round(minColumn * 100) / 100}px（修复前 8.05px）`,
    );
    const expectedWidth = m.columnWidths.reduce((sum, value) => sum + value, 0) + m.gap * 11;
    record(
      `${label}：列宽之和 = 网格宽`,
      m.grid !== null && near(expectedWidth, m.grid.width, 3),
      `${Math.round(expectedWidth * 100) / 100} vs ${m.grid?.width ?? 0}`,
    );
    record(
      `${label}：网格占满内容区宽度`,
      m.grid !== null && m.grid.width >= (m.page?.width ?? 0) - 60,
      `grid=${Math.round(m.grid?.width ?? 0)} page=${Math.round(m.page?.width ?? 0)}`,
    );
    const narrowest = m.cardWidths.length ? Math.min(...m.cardWidths) : 0;
    record(
      `${label}：卡片足够宽（不会 2–3 字换行）`,
      narrowest >= 300,
      `最窄卡片=${Math.round(narrowest * 100) / 100}px（修复前 96.25px）`,
    );
  } else {
    // 手机：单列，卡片占满内容宽
    const pageWidth = m.page?.width ?? 0;
    const widest = m.cardWidths.length ? Math.max(...m.cardWidths) : 0;
    record(`${label}：单列（卡片占满内容宽）`, near(widest, pageWidth - 28, 4), `卡片=${Math.round(widest)}px / 内容宽≈${Math.round(pageWidth - 28)}px`);
    record(`${label}：卡片宽度仍可读`, (m.cardWidths.length ? Math.min(...m.cardWidths) : 0) >= 280, `最窄=${Math.round(Math.min(...m.cardWidths))}px`);
  }

  await shot(page, `${label}-dashboard`);
  // 整页内容：`.page` 自己是滚动容器，document 的 fullPage 截不到下面几屏，
  // 所以直接截 `.dash` 这个元素（Playwright 会按元素完整尺寸出图）。
  await page.locator('.dash').screenshot({ path: path.join(SHOTS, `${label}-dashboard-full.png`) });

  /* ---- L3/F6：hover 反馈（窄屏触控环境不做 hover 断言） ---- */
  if (viewport.width >= 1200) {
    const cardHover = await hoverChange(page, '.dash-card');
    record(`${label}：卡片 hover 有反馈`, cardHover.changed, cardHover.changed ? 'style changed' : JSON.stringify(cardHover.before));
    const actionHover = await hoverChange(page, '.dash-card__action');
    record(`${label}：「详细」hover 有反馈`, actionHover.changed, actionHover.changed ? 'style changed' : JSON.stringify(actionHover.before));
  }

  /* ---- 导航：导航里的「总览」真的回到总览 ---- */
  // 用 testid 定位导航入口：Step 1/2 会把顶栏导航换成左导航，
  // 依赖顶栏 DOM 结构/按钮文字的选择器会假红。
  // 点页头 logo 回聊天（`brand-home`）。它原先的 testid 是 `nav-chat`，
  // 与左导航「对话」项重名；改名后 `nav-chat` 只剩导航项一处。
  await page.locator('[data-testid="brand-home"]').first().click();
  await waitFor(async () => (await page.evaluate(() => window.location.pathname)) === '/');
  const navClicked = await page.evaluate(() => window.location.pathname === '/');
  record(`${label}：聊天入口能回到聊天页`, navClicked);
  if (viewport.width >= 900) {
    await page.locator('[data-testid="nav-dashboard"]').first().click();
    const backOk = await waitFor(async () => {
      const probe = await measure(page);
      return probe.pathname === '/dashboard' && probe.dashInMain === 1 && probe.dashInHeader === 0;
    });
    record(`${label}：导航「总览」回到总览页且仍在主区`, backOk);
  } else {
    // 手机 375 下导航被 CSS 收起（既有设计），直接验证地址回退
    await openDashboard(page);
    record(`${label}：窄屏直接访问 /dashboard 可用`, (await measure(page)).dashInMain === 1);
  }

  /* ---- 交互：刷新会真的再取一次 ---- */
  const before = state.dashboardRequests;
  await page.locator('.dash__refresh').first().click();
  const refreshed = await waitFor(async () => state.dashboardRequests > before, 8000);
  record(`${label}：点「刷新」会重新取数`, refreshed, `请求数 ${before} → ${state.dashboardRequests}`);

  record(`${label}：没有未捕获的前端异常`, pageErrors.length === 0, pageErrors.join(' | '));
  record(`${label}：没有控制台错误`, consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));

  await context.close();
}

/* ------------------------------------------------- 未知路由（M1 阻断项） */

/**
 * 真浏览器访问一个不存在的地址，必须看到 `unknown-route` 面板：
 * 不能静默渲染聊天页、输入框不能存在、并且要能点回总览。
 */
async function runUnknownRoute(browser, viewport, label) {
  const state = { dashboard: dashboardPayload(), dashboardRequests: 0 };
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  await attachMocks(context, state);

  await page.goto(`${BASE.origin}/qa-no-such-route`, { waitUntil: 'domcontentloaded' });
  const shown = await page
    .waitForSelector('[data-testid="unknown-route"]', { timeout: 15000 })
    .then(() => true)
    .catch(() => false);
  record(`${label}：未知路径渲染 unknown-route`, shown);

  const probe = await page.evaluate(() => ({
    pathname: window.location.pathname,
    visible: (() => {
      const el = document.querySelector('[data-testid="unknown-route"]');
      if (!el) return false;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
    })(),
    text: document.body.innerText,
    textareas: document.querySelectorAll('textarea').length,
    chatLayouts: document.querySelectorAll('.chat-layout').length,
    dashPlaces: document.querySelectorAll('.dash').length,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  record(`${label}：unknown-route 可见`, probe.visible, JSON.stringify({ visible: probe.visible }));
  record(`${label}：URL 仍是未知地址`, probe.pathname === '/qa-no-such-route', probe.pathname);
  record(`${label}：未知页不含聊天输入框`, probe.textareas === 0, `textarea=${probe.textareas}`);
  record(`${label}：未知页不是聊天页`, probe.chatLayouts === 0, `chat-layout=${probe.chatLayouts}`);
  record(
    `${label}：未知页没有静默变成总览`,
    probe.dashPlaces === 0,
    `.dash=${probe.dashPlaces}`,
  );
  record(`${label}：未知页有文案「无法识别的路由」`, probe.text.includes('无法识别的路由'));
  record(`${label}：未知页无横向溢出`, probe.scrollWidth <= probe.innerWidth, `${probe.scrollWidth}/${probe.innerWidth}`);
  record(`${label}：未知页没有未捕获异常`, pageErrors.length === 0, pageErrors.join(' | '));

  await shot(page, `${label}-unknown-route`);

  // 可点的返回入口：回到 /dashboard，而且真的渲染总览
  await page.locator('[data-testid="unknown-route-home"]').click();
  const backHome = await waitFor(async () => {
    const at = await page.evaluate(() => window.location.pathname);
    return at === '/dashboard' && (await page.locator('.dash__grid').count()) === 1;
  }, 15000);
  record(`${label}：点「回到总览」真的回到 /dashboard`, backHome);
  await context.close();
}

/* ---------------------------------------- 分段级 unavailable 与空态（L1） */

async function runSectionUnavailable(browser) {
  const payload = dashboardPayload();
  payload.sections.affect = {
    status: 'unavailable',
    as_of: '2026-10-30T05:48:02Z',
  };
  payload.sections.memories = { status: 'empty', as_of: '2026-10-30T05:48:02Z' };
  const state = { dashboard: payload, dashboardRequests: 0 };
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await attachMocks(context, state);
  await openDashboard(page);

  const probe = await page.evaluate(() => {
    const read = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const cs = getComputedStyle(el);
      return {
        className: el.className,
        background: cs.backgroundColor,
        color: cs.color,
        borderWidth: cs.borderLeftWidth,
        icon: el.querySelectorAll('svg').length,
        text: (el.textContent || '').trim(),
      };
    };
    return {
      unavailable: read('p.dash-card__unavailable'),
      empty: read('[data-testid="dash-最近记住的事"] p.dash-card__muted'),
      wholePageError: document.querySelectorAll('.page > .dash-error').length,
    };
  });

  record('分段失败：情绪卡显示卡内告警条', probe.unavailable !== null && probe.unavailable.text.includes('暂时读不到这块数据。'));
  record(
    '分段失败：有图标 + 警示底（不再和空态同一副灰字）',
    Boolean(probe.unavailable) && probe.unavailable.icon >= 1 && probe.unavailable.background !== 'rgba(0, 0, 0, 0)',
    JSON.stringify(probe.unavailable),
  );
  record(
    '分段失败：与空态（确实没有）视觉指纹不同',
    Boolean(probe.unavailable && probe.empty) &&
      (probe.unavailable.background !== probe.empty.background || probe.unavailable.className !== probe.empty.className),
    `${JSON.stringify(probe.unavailable)} vs ${JSON.stringify(probe.empty)}`,
  );
  record('分段失败：不触发整页失败面板（不撞车）', probe.wholePageError === 0, `dash-error=${probe.wholePageError}`);
  await shot(page, 'error-section-unavailable-after');
  await context.close();
}

/* ------------------------------------------------------- 账号页（F11） */

async function runAccount(browser) {
  const state = { dashboard: dashboardPayload(), dashboardRequests: 0 };
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await attachMocks(context, state);
  await page.goto(`${BASE.origin}/settings/account`, { waitUntil: 'domcontentloaded' });
  const input = page.locator('input[placeholder^="输入新的登录名"]');
  const save = page.getByRole('button', { name: '保存登录名' });
  // SPA 首次挂载/路由解析与 domcontentloaded 之间有竞态：直接 count() 会偶发 0。
  // 只等元素出现，不改任何断言。
  await input.first().waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});
  const ready = await input.count();
  if (!ready) {
    record('账号页：找不到「新的登录名」输入框', false);
    await context.close();
    return;
  }
  const initial = await input.inputValue();
  record('账号页：新的登录名默认空白（不再预填当前值）', initial === '', `value=${JSON.stringify(initial)}`);
  record('账号页：不再常驻「登录名没有变化。」', !(await bodyText(page)).includes('登录名没有变化。'));
  record('账号页：空输入时保存按钮禁用', await save.isDisabled());
  const currentPassword = page.locator('form').first().locator('input[type="password"]');
  await input.fill('ling-renamed');
  // 只填新名字、不填当前密码仍然禁用（改任何一项都要当前密码）
  record('账号页：缺当前密码时仍禁用', await save.isDisabled());
  await input.fill('ling');
  await currentPassword.fill('ling-dev-password');
  record('账号页：填当前密码但名字没变，仍然禁用', await save.isDisabled());
  await input.fill('ling-renamed');
  record('账号页：填了不同的名字才启用', await save.isEnabled());
  await shot(page, 'account-new-name-after');
  await context.close();
}

/* ------------------------------------------------------------------ main */

async function waitServerReady(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${BASE.origin}/`);
      if (response.ok) return true;
    } catch {
      /* 还没起来 */
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
}

if (!(await waitServerReady())) {
  console.error(`静态前端未就绪：${BASE.origin}`);
  process.exit(1);
}

console.log(`隔离实例：${BASE.origin}（非 8080 共享实例）`);
console.log(`Playwright：${PLAYWRIGHT_ORIGIN}`);
const browser = await chromium.launch({ executablePath: EXECUTABLE });
console.log(`浏览器：${browser.version()}${EXECUTABLE ? `（CHROME_PATH=${EXECUTABLE}）` : '（内置缓存）'}`);
try {
  await runViewport(browser, { width: 1440, height: 900 }, 'desktop');
  await runViewport(browser, { width: 375, height: 667 }, 'mobile');
  await runUnknownRoute(browser, { width: 1440, height: 900 }, 'desktop');
  await runUnknownRoute(browser, { width: 375, height: 667 }, 'mobile');
  await runSectionUnavailable(browser);
  await runAccount(browser);
} finally {
  await browser.close();
}

const failed = results.filter((result) => result.ok === false);
const passed = results.filter((result) => result.ok === true);
console.log('\n==== 汇总 ====');
console.log(`通过 ${passed.length}/${results.length}；截图目录 ${SHOTS}`);
if (failed.length) {
  console.log('失败项：');
  for (const item of failed) console.log(` - ${item.name}${item.detail ? `：${item.detail}` : ''}`);
  process.exit(1);
}
console.log('全部通过');
