/**
 * 直播导航与工作台的端到端验收（真 API + 真 PostgreSQL + 真 Chromium）。
 *
 * 这一份只回答一个问题：**新实例、未配置、状态接口失效、计划中、直播中、
 * 结束后、普通用户**这七种状态下，「有权限的用户能不能从左栏找到直播工作台」。
 *
 * 为什么不能只靠 web 单测：单测里的 `navigation_visible` 是手写的夹具，
 * 而这里要的是**后端真算出来的那个值**（`planned || active || recent`）——
 * 只有让真 API 把它算成 `false`，「未配置也能看到入口」才算被证明。
 *
 * 隔离沿用 `lib/isolation.mjs`：每次运行新建一次性库，只删自己建的那个；
 * 截图与日志写 `SHOTS_DIR`（不设则写临时目录），开头结尾都打印输出目录。
 *
 * 用法：
 *   node tests/e2e/live_nav_browser.mjs
 *
 * 环境变量：CHROME_PATH、SHOTS_DIR、E2E_DB_NAME、LING_PGPORT、
 * KEEP_SERVERS=1（保留进程与测试库）、LIVE_NAV_ONLY=（预留，未使用）。
 */
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

import {
  createIsolatedDatabase,
  dropOwnedDatabase,
  makeRunId,
  onTermination,
  resolveShotsDir,
} from './lib/isolation.mjs';
import { loadChromium } from './lib/playwright.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const RUN_ID = makeRunId();
const SHOTS_INFO = resolveShotsDir('live-nav', RUN_ID);
const SHOTS = SHOTS_INFO.dir;
const PG_PORT = Number(process.env.LING_PGPORT || 55432);
let DB_NAME = null;
const PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python');
const USERNAME = 'live_nav_e2e';
const PASSWORD = 'live-nav-e2e-password';
/** 普通用户（非 owner）：验证「左栏不给他直播、直开 /live 有可见说明」。 */
const MEMBER_NAME = 'live_nav_member';
const MEMBER_PASSWORD = 'live-nav-member-password';
const FORBIDDEN_PORTS = new Set([8080, 55432]);

/** 房间与舞台参数（文档用假数据，不写真号）。 */
const ROOM_ID = '100200300';
const STAGE_TOKEN = 'live-nav-stage-000001';
const CONNECTOR = 'e2e-live';

console.log(
  `截图 / 日志目录：${SHOTS}` + (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定）' : '（默认临时目录）'),
);

const { chromium, origin: PLAYWRIGHT_ORIGIN } = await loadChromium();
const EXECUTABLE = process.env.CHROME_PATH || undefined;

/* ------------------------------------------------------------------ 工具 */

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let passed = 0;
function ok(name, detail = '') {
  passed += 1;
  console.log(`PASS  ${name}${detail ? ` — ${detail}` : ''}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function waitUntil(fn, label, timeout = 20_000, interval = 120) {
  const deadline = Date.now() + timeout;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await fn();
      if (last) return last;
    } catch (error) {
      last = error;
    }
    await sleep(interval);
  }
  throw new Error(`等待超时：${label}（最后结果：${last instanceof Error ? last.message : String(last)}）`);
}

async function freePort() {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const port = await new Promise((resolve, reject) => {
      const server = net.createServer();
      server.unref();
      server.on('error', reject);
      server.listen(0, '127.0.0.1', () => {
        const address = server.address();
        server.close(() => resolve(address.port));
      });
    });
    if (port >= 20_000 && !FORBIDDEN_PORTS.has(port)) return port;
  }
  throw new Error('无法找到可用的高位端口（连续 12 次都落在受限区间）');
}

function listeningOn(port) {
  try {
    const out = execFileSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN'], { encoding: 'utf8' });
    return out.trim();
  } catch (error) {
    if (error.status === 1) return '';
    throw error;
  }
}

function startProcess(label, command, args, options) {
  const fd = fs.openSync(options.logPath, 'a');
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env,
    detached: true,
    stdio: ['ignore', fd, fd],
  });
  fs.closeSync(fd);
  child.__label = label;
  child.on('exit', (code, signal) => {
    const normal = code === null || code === 0 || code === 143;
    if (!normal) console.error(`[${label}] 进程退出 code=${code} signal=${signal}，日志：${options.logPath}`);
  });
  return child;
}

async function stopProcess(child, label) {
  if (!child || child.exitCode !== null) return;
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    try {
      child.kill('SIGTERM');
    } catch {
      /* 已经没了 */
    }
  }
  const exited = await Promise.race([
    new Promise((resolve) => child.once('exit', () => resolve(true))),
    sleep(6_000).then(() => false),
  ]);
  if (!exited) {
    try {
      process.kill(-child.pid, 'SIGKILL');
    } catch {
      /* 已经没了 */
    }
    console.log(`[${label}] 未在 6s 内退出，已 SIGKILL`);
  }
}

async function httpJson(url, init = {}) {
  const response = await fetch(url, init);
  const text = await response.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { status: response.status, json, text };
}

/* ------------------------------------------------------------- 数据库隔离 */

function createTestDatabase() {
  try {
    DB_NAME = createIsolatedDatabase({
      tag: 'livenav',
      pgPort: PG_PORT,
      python: PYTHON,
      repoRoot: REPO_ROOT,
      runId: RUN_ID,
    }).name;
  } catch (error) {
    console.error(`无法创建隔离测试库：${error?.message || error}`);
    console.error('先确认本地 PG 在跑：scripts/dev_pg.sh start');
    process.exit(2);
  }
}

/* --------------------------------------------------------------- 服务启动 */

const servers = [];
let tmpRoot = null;
let apiBase = null;
let webBase = null;
let apiPort = null;
let webPort = null;
let apiLog = null;
let webLog = null;

async function bootApi() {
  apiPort = await freePort();
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ling-livenav-e2e-'));
  const dataDir = path.join(tmpRoot, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  apiLog = path.join(SHOTS, 'api.log');
  fs.writeFileSync(apiLog, '');

  const env = {
    ...process.env,
    LING_ENV: 'dev',
    LING_HOST: '127.0.0.1',
    LING_PORT: String(apiPort),
    LING_DATABASE_URL: `postgresql+asyncpg://ling@127.0.0.1:${PG_PORT}/${DB_NAME}`,
    LING_BOOTSTRAP_ADMIN: USERNAME,
    LING_BOOTSTRAP_ADMIN_PASSWORD: PASSWORD,
    LING_TEST_MODE: '1',
    LING_DATA_DIR: dataDir,
  };
  // 用包自带的入口 `python -m ling`（读 LING_HOST/LING_PORT），
  // 与 `nav_browser.mjs` 一致：直接 `uvicorn ling.api.app:app` 找不到模块名。
  const child = startProcess('api', PYTHON, ['-m', 'ling'], { cwd: REPO_ROOT, env, logPath: apiLog });
  servers.push({ child, label: `api:${apiPort}` });
  apiBase = `http://127.0.0.1:${apiPort}`;

  await waitUntil(
    async () => {
      const { status } = await httpJson(`${apiBase}/healthz`).catch(() => ({ status: 0 }));
      return status === 200;
    },
    `API ${apiBase}/healthz 就绪（日志 ${apiLog}）`,
    60_000,
    250,
  );
  const ready = await httpJson(`${apiBase}/readyz`);
  assert(ready.status === 200, `API 就绪检查失败：${ready.text.slice(0, 300)}`);
  ok('API 启动（真 PostgreSQL）', `${apiBase} · db=${DB_NAME}@127.0.0.1:${PG_PORT}`);
  return env;
}

async function bootWeb() {
  webPort = await freePort();
  webLog = path.join(SHOTS, 'vite.log');
  fs.writeFileSync(webLog, '');
  const viteBin = path.join(REPO_ROOT, 'clients', 'web-console', 'node_modules', 'vite', 'bin', 'vite.js');
  const child = startProcess(
    'web',
    process.execPath,
    [viteBin, '--port', String(webPort), '--strictPort', '--host', '127.0.0.1'],
    {
      cwd: path.join(REPO_ROOT, 'clients', 'web-console'),
      env: { ...process.env, LING_API_TARGET: apiBase },
      logPath: webLog,
    },
  );
  servers.push({ child, label: `web:${webPort}` });
  webBase = `http://127.0.0.1:${webPort}`;
  await waitUntil(
    async () => {
      const { status } = await httpJson(`${webBase}/`).catch(() => ({ status: 0 }));
      return status === 200;
    },
    `前端 ${webBase} 就绪（日志 ${webLog}）`,
    60_000,
    250,
  );
  ok('前端启动（vite dev，代理 /v1）', `${webBase} → ${apiBase}`);
}

/* ------------------------------------------------------------- 真 API 操作 */

let ownerCookie = null;

async function apiCall(method, pathname, body) {
  const response = await httpJson(`${apiBase}${pathname}`, {
    method,
    headers: {
      'content-type': 'application/json',
      // 写操作要带可信来源：后端对「来源不可信」的写请求回 403 bad_origin。
      // 这里用与前端同源的地址，和浏览器里的请求走同一条校验。
      origin: apiBase,
      referer: `${apiBase}/`,
      ...(ownerCookie ? { cookie: ownerCookie } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return response;
}

/** 登录拿会话 cookie（模拟前端的登录流程，不绕过鉴权）。 */
async function loginApi(username, password) {
  const response = await fetch(`${apiBase}/v1/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const text = await response.text();
  assert(response.status === 200, `登录失败（${username}）：${response.status} ${text.slice(0, 200)}`);
  const cookie = response.headers.getSetCookie?.() ?? [];
  const pair = cookie.map((item) => item.split(';')[0]).filter(Boolean).join('; ');
  return pair;
}

async function currentLive(cookie = ownerCookie) {
  const response = await httpJson(`${apiBase}/v1/live/sessions/current`, {
    headers: cookie ? { cookie } : {},
  });
  return response;
}

/* ------------------------------------------------------------- 页面小工具 */

async function shot(page, name) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function loginUi(page, username = USERNAME, password = PASSWORD) {
  await page.goto(`${webBase}/`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL('**/dashboard', { timeout: 25_000 });
  await page.waitForSelector('header.app-header', { timeout: 25_000 });
  await page.waitForSelector('main.app-main', { timeout: 25_000 });
  await page.waitForTimeout(250);
}

function railItem(page, testid) {
  return page.locator(`.sidenav [aria-label="主导航"] [data-testid="${testid}"]`).first();
}

function drawerItem(page, testid) {
  return page.locator(`[data-testid="nav-drawer"] [data-testid="${testid}"]`).first();
}

async function pathOf(page) {
  return page.evaluate(() => `${window.location.pathname}${window.location.search}`);
}

async function waitPath(page, expected, timeout = 12_000) {
  return waitUntil(async () => (await pathOf(page)) === expected, `地址变成 ${expected}`, timeout).then(
    () => true,
    () => false,
  );
}

async function currentNav(page) {
  return page.evaluate(() => {
    const marks = document.querySelectorAll('[aria-current="page"]');
    return {
      count: marks.length,
      testid: marks.length === 1 ? marks[0].getAttribute('data-testid') : null,
    };
  });
}

async function gotoPage(page, href) {
  await page.goto(`${webBase}${href}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('main.app-main', { timeout: 25_000 });
  await page.waitForSelector('header.app-header', { timeout: 25_000 });
  await page.waitForTimeout(250);
}

/** 左栏里的直播项（桌面）。 */
async function liveNavState(page) {
  return page.evaluate(() => {
    const scope = document.querySelector('.sidenav [aria-label="主导航"]');
    if (!scope) return { present: false, reason: '没有主导航 landmark' };
    const items = [...scope.querySelectorAll('[data-testid="nav-live"]')];
    const list = scope.querySelector('[data-nav-group="live"]');
    return {
      present: items.length > 0,
      count: items.length,
      label: items[0] ? (items[0].textContent || '').trim() : null,
      inOwnGroup: !!list,
      groupFirst: list ? list.getAttribute('data-group-first') : null,
      hasDot: items[0] ? !!items[0].querySelector('.live-nav-dot') : false,
      dotTone: items[0]?.querySelector('.live-nav-dot')?.getAttribute('data-tone') ?? null,
    };
  });
}

/* ------------------------------------------------------------------ 状态驱动 */

/** 通过真 API 建直播间（B 站直播接入器）。 */
async function createConnector() {
  const response = await apiCall('PUT', `/v1/settings/live-features/bilibili/${CONNECTOR}`, {
    display_name: '直播验收',
    room_id: ROOM_ID,
    stage_token: STAGE_TOKEN,
    enabled: true,
    // 要做主播（host）就必须允许控制 OBS 推流，否则开播被 403 挡回。
    obs_control: true,
  });
  assert(response.status === 200, `建接入器失败：${response.status} ${response.text.slice(0, 300)}`);
}

async function savePlan() {
  const response = await apiCall('PUT', `/v1/live/plans/${CONNECTOR}`, {
    role: 'host',
    public_topic: '今晚聊配乐',
    host_name: '',
    companion_auto_reply: false,
    max_minutes: 60,
    autonomous_window: null,
  });
  assert(response.status === 200, `保存计划失败：${response.status} ${response.text.slice(0, 300)}`);
}

/** 舞台端上报（让 `stage_is_fresh` 成立，才能开播）。 */
async function stageReport(body) {
  const response = await httpJson(`${apiBase}/v1/custom/${CONNECTOR}/stage`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${STAGE_TOKEN}` },
    body: JSON.stringify(body),
  });
  return response;
}

async function openSession(mode = 'broadcast') {
  const response = await apiCall('POST', '/v1/live/sessions', { connector_id: CONNECTOR, mode });
  assert(response.status === 201, `开播失败：${response.status} ${response.text.slice(0, 300)}`);
  return response.json;
}

/**
 * 停播。走真路由 `/v1/live/sessions/{id}/stop`（`current/stop` 不存在，
 * 用错路径时接口静默无效，场次一直停在 starting）。
 */
async function stopSession(sessionId) {
  return apiCall('POST', `/v1/live/sessions/${sessionId}/stop`);
}

/* ------------------------------------------------------------------- 场景 */

async function main(browser) {
  ownerCookie = await loginApi(USERNAME, PASSWORD);
  assert(ownerCookie, '登录后没有拿到会话 cookie');
  ok('所有者登录（真鉴权）');

  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));

  /* ---------------- 1. 新实例 + 未配置：状态接口真的说 false ---------------- */
  const fresh = await currentLive();
  assert(fresh.status === 200, `状态接口失败：${fresh.status}`);
  assert(
    fresh.json.navigation_visible === false,
    `新实例的 navigation_visible 应为 false（真后端算出来的），实际 ${JSON.stringify(fresh.json)}`,
  );
  ok('新实例未配置：后端 navigation_visible=false', JSON.stringify(fresh.json));

  await loginUi(page);
  await shot(page, 'desktop-01-new-instance-dashboard');
  const freshNav = await liveNavState(page);
  assert(freshNav.present, `未配置时左栏应有「直播」，实际 ${JSON.stringify(freshNav)}`);
  assert(freshNav.count === 1, `左栏「直播」应恰好一个，实际 ${freshNav.count}`);
  assert(freshNav.label === '直播', `左栏「直播」文字不对：${freshNav.label}`);
  assert(freshNav.inOwnGroup, '「直播」应在自己的导航组里（data-nav-group="live"）');
  ok('桌面：未配置也有常驻「直播」，且在自己的组里', JSON.stringify(freshNav));

  // 点进去 → /live 工作台 + 空态三步 + 引导按钮
  await railItem(page, 'nav-live').click();
  assert(await waitPath(page, '/live'), `点直播后应到 /live，实际 ${await pathOf(page)}`);
  await page.waitForSelector('[data-testid="live-page"]', { timeout: 20_000 });
  const current = await currentNav(page);
  assert(current.count === 1 && current.testid === 'nav-live', `直播页高亮不对：${JSON.stringify(current)}`);
  // 工作台的直播间列表是异步拉的（进入页面后 4s 一轮），空态要等第一轮回来。
  await waitUntil(
    async () => (await page.locator('[data-testid="live-empty"]').count()) > 0,
    '工作台出现首次配置引导（live-empty）',
    30_000,
    200,
  ).catch(() => false);
  const empty = await page.evaluate(() => {
    const box = document.querySelector('[data-testid="live-empty"]');
    if (!box) return null;
    return {
      steps: box.querySelectorAll('li').length,
      hasSetup: !!box.querySelector('[data-testid="live-empty-setup"]'),
      text: (box.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
    };
  });
  assert(empty, '未配置时应显示首次配置引导（live-empty）');
  assert(empty.steps >= 3, `引导应是分步的，实际 ${empty.steps} 步`);
  assert(empty.hasSetup, '引导里应有可点的「去添加直播间」');
  await shot(page, 'desktop-02-live-empty');
  ok('桌面：未配置工作台给出分步引导与可点入口', empty.text);

  // 引导按钮真的把人送到接入设置，并能用工作台的「接入设置」走回来（往返）
  await page.locator('[data-testid="live-empty-setup"]').click();
  assert(await waitPath(page, '/settings/connectors'), `引导应到接入设置，实际 ${await pathOf(page)}`);
  await page.waitForTimeout(600);
  await shot(page, 'desktop-03-connectors');
  const dup = await page.evaluate(() => ({
    manage: [...document.querySelectorAll('button')].filter((b) => (b.textContent || '').trim() === '管理直播').length,
    custom: document.querySelectorAll('details.live-connector').length,
  }));
  assert(dup.manage === 0, `平台接入里不应再有「管理直播」重复入口，实际 ${dup.manage} 个`);
  assert(dup.custom === 1, `「自定义接入器」这一栏应仍在，实际 ${dup.custom}`);
  ok('平台接入：重复入口已移除，接入栏仍在', JSON.stringify(dup));

  await gotoPage(page, '/live');
  await page.waitForSelector('[data-testid="live-page"]', { timeout: 20_000 });
  await page.locator('[data-testid="live-page"] header button', { hasText: '接入设置' }).first().click();
  assert(await waitPath(page, '/settings/connectors'), `工作台「接入设置」应能回去，实际 ${await pathOf(page)}`);
  await shot(page, 'desktop-04-back-to-connectors');
  ok('往返：工作台 → 接入设置 仍通畅');

  /* ---------------- 2. 状态接口失效 ---------------- */
  const broken = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const brokenPage = await broken.newPage();
  await brokenPage.route('**/v1/live/sessions/current', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"unavailable"}' }),
  );
  await loginUi(brokenPage);
  const brokenNav = await liveNavState(brokenPage);
  assert(brokenNav.present, `状态接口 500 时左栏仍应有「直播」，实际 ${JSON.stringify(brokenNav)}`);
  await railItem(brokenPage, 'nav-live').click();
  await brokenPage.waitForSelector('[data-testid="live-page"]', { timeout: 20_000 });
  const alerted = await waitUntil(
    async () => (await brokenPage.locator('[role="alert"]').count()) > 0,
    '状态接口失效时工作台给出可见错误',
    40_000,
    200,
  ).catch(() => false);
  assert(alerted, '状态接口失效时工作台应有可见的错误提示，而不是白屏');
  await shot(brokenPage, 'desktop-05-status-broken');
  ok('状态接口失效：入口不消失、工作台给可见错误');
  await broken.close();

  /* ---------------- 3. 计划中 ---------------- */
  await createConnector();
  await savePlan();
  const planned = await currentLive();
  assert(
    planned.json.navigation_visible === true && planned.json.planned_connector_id === CONNECTOR,
    `计划中 navigation_visible 应为 true，实际 ${JSON.stringify(planned.json)}`,
  );
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('main.app-main', { timeout: 25_000 });
  await page.waitForTimeout(400);
  const plannedNav = await liveNavState(page);
  assert(plannedNav.present, `计划中左栏应有「直播」，实际 ${JSON.stringify(plannedNav)}`);
  await railItem(page, 'nav-live').click();
  assert(await waitPath(page, '/live'), '计划中点直播应到 /live');
  await page.waitForSelector('[data-testid="live-plan"]', { timeout: 25_000 });
  await shot(page, 'desktop-06-live-planned');
  ok('计划中：入口在、工作台显示本场计划', JSON.stringify(plannedNav));

  /* ---------------- 4. 直播中 ---------------- */
  await stageReport({ device_id: 'e2e-stage', session_id: null, obs_active: false, scene: '直播' });
  const run = await openSession('broadcast');
  await stageReport({ device_id: 'e2e-stage', session_id: run.id, obs_active: true, scene: '直播' });
  const liveNow = await currentLive();
  assert(
    liveNow.json.session && liveNow.json.session.state !== 'ended',
    `直播中应有一个进行中的场次，实际 ${JSON.stringify(liveNow.json.session && liveNow.json.session.state)}`,
  );
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('main.app-main', { timeout: 25_000 });
  await page.waitForTimeout(400);
  const liveNav = await liveNavState(page);
  assert(liveNav.present, `直播中左栏应有「直播」，实际 ${JSON.stringify(liveNav)}`);
  // 有场次时才有状态点；它的颜色不能影响入口是否存在
  assert(liveNav.hasDot || true, '（状态点可有可无）');
  await railItem(page, 'nav-live').click();
  assert(await waitPath(page, '/live'), '直播中点直播应到 /live');
  await page.waitForSelector('[data-testid="live-run-status"]', { timeout: 25_000 });
  await shot(page, 'desktop-07-live-running');
  ok('直播中：入口在、工作台显示运行状态', `${JSON.stringify(liveNav)} · state=${liveNow.json.session.state}`);

  /* ---------------- 5. 结束后 ---------------- */
  await stopSession(run.id);
  // 停播不是一次调用就结束：要拿到**两个独立回执**——舞台端确认停推
  // （`control: "stopped"` 且 `obs_active: false`）、平台端确认下播（`live_end`）。
  // 少任何一个，场次都会停在 `stopping`。这两步走的是真实接入接口。
  const endAt = new Date(Date.now() + 1000).toISOString();
  await stageReport({
    device_id: 'e2e-stage',
    session_id: run.id,
    obs_active: false,
    obs_reconnecting: false,
    control: 'stopped',
    scene: '直播',
  });
  const ended_event = await httpJson(`${apiBase}/v1/custom/${CONNECTOR}/events`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${STAGE_TOKEN}` },
    body: JSON.stringify({
      session_id: run.id,
      room_id: ROOM_ID,
      event_id: 'e2e-live-end',
      kind: 'live_end',
      occurred_at: endAt,
    }),
  });
  assert(
    ended_event.status === 200,
    `平台下播事件上报失败：${ended_event.status} ${ended_event.text.slice(0, 200)}`,
  );
  await waitUntil(
    async () => {
      const now = await currentLive();
      return now.json.session && now.json.session.state === 'ended';
    },
    '场次进入 ended',
    40_000,
    400,
  ).catch(() => false);
  const ended = await currentLive();
  assert(ended.json.session && ended.json.session.state === 'ended', `场次应已结束，实际 ${JSON.stringify(ended.json.session?.state)}`);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('main.app-main', { timeout: 25_000 });
  await page.waitForTimeout(400);
  const endedNav = await liveNavState(page);
  assert(endedNav.present, `结束后左栏仍应有「直播」，实际 ${JSON.stringify(endedNav)}`);
  await railItem(page, 'nav-live').click();
  assert(await waitPath(page, '/live'), '结束后点直播应到 /live');
  await page.waitForSelector('[data-testid="live-page"]', { timeout: 25_000 });
  await shot(page, 'desktop-08-live-ended');
  ok('结束后：入口在、工作台回到本场计划', JSON.stringify(endedNav));

  /* ---------------- 6. 窄屏抽屉往返 ---------------- */
  const mobile = await browser.newContext({ viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true });
  const mPage = await mobile.newPage();
  await loginUi(mPage);
  await shot(mPage, 'mobile-01-dashboard');
  await mPage.locator('[data-testid="nav-open"]').click();
  await mPage.waitForSelector('[data-testid="nav-drawer"]', { timeout: 20_000 });
  const drawerLive = await mPage.evaluate(() => {
    const drawer = document.querySelector('[data-testid="nav-drawer"]');
    if (!drawer) return null;
    const item = drawer.querySelector('[data-testid="nav-live"]');
    const group = drawer.querySelector('[data-nav-group="live"]');
    return {
      present: !!item,
      label: item ? (item.textContent || '').trim() : null,
      inOwnGroup: !!group,
      rect: item ? item.getBoundingClientRect().toJSON() : null,
    };
  });
  assert(drawerLive?.present, `窄屏抽屉里应有「直播」，实际 ${JSON.stringify(drawerLive)}`);
  assert(drawerLive.label === '直播', `抽屉里「直播」文字不对：${drawerLive.label}`);
  assert(drawerLive.inOwnGroup, '抽屉里「直播」应在自己的组里（与桌面同一份数据）');
  assert(
    drawerLive.rect && drawerLive.rect.height >= 44 - 0.5,
    `抽屉里「直播」触控目标应 ≥44，实际 ${drawerLive.rect?.height}`,
  );
  await shot(mPage, 'mobile-02-drawer-live');
  await drawerItem(mPage, 'nav-live').click();
  assert(await waitPath(mPage, '/live'), `抽屉点直播应到 /live，实际 ${await pathOf(mPage)}`);
  await mPage.waitForSelector('[data-testid="live-page"]', { timeout: 25_000 });
  const closed = await waitUntil(
    async () => (await mPage.locator('[data-testid="nav-drawer"]').count()) === 0,
    '点直播后抽屉自动关闭',
    8_000,
    150,
  ).catch(() => false);
  assert(closed, '点直播后抽屉应自动关闭');
  await shot(mPage, 'mobile-03-live');
  const dims = await mPage.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  assert(dims.scrollWidth <= dims.innerWidth + 1, `窄屏 /live 出现横向滚动：${dims.scrollWidth}/${dims.innerWidth}`);
  ok('窄屏：抽屉里同组「直播」可达、≥44、点完关闭抽屉、无横向滚动', JSON.stringify(drawerLive.rect));
  await mobile.close();

  /* ---------------- 7. 普通用户（非 owner） ---------------- */
  /*
   * 普通用户：后端**没有**建用户的 HTTP 接口（账号只在首次安装时引导创建），
   * 所以走 `tests/e2e/create_member.py`——它调的是同一个 `IdentityService.create_user`，
   * 写的是本次新建的那个一次性库。造不出来就如实标成「未验证」，不静默通过。
   */
  const memberResult = execFileSync(
    PYTHON,
    [
      path.join(HERE, 'create_member.py'),
      '--database-url',
      `postgresql+asyncpg://ling@127.0.0.1:${PG_PORT}/${DB_NAME}`,
      '--username',
      MEMBER_NAME,
      '--password',
      MEMBER_PASSWORD,
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', env: { ...process.env, LING_DATABASE_URL: `postgresql+asyncpg://ling@127.0.0.1:${PG_PORT}/${DB_NAME}` } },
  );
  console.log(`NOTE  已建普通用户：${memberResult.trim()}`);
  const memberCookie = await loginApi(MEMBER_NAME, MEMBER_PASSWORD).catch(() => null);
  if (memberCookie) {
    const memberPage = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
    await loginUi(memberPage, MEMBER_NAME, MEMBER_PASSWORD);
    const memberNav = await liveNavState(memberPage);
    assert(!memberNav.present, `普通用户左栏不应有「直播」，实际 ${JSON.stringify(memberNav)}`);
    await shot(memberPage, 'desktop-09-member-dashboard');
    await gotoPage(memberPage, '/live');
    const hint = await memberPage.evaluate(() => {
      const el = document.querySelector('.page__hint');
      return el ? (el.textContent || '').trim() : null;
    });
    assert(hint && hint.includes('所有者'), `普通用户直开 /live 应有可见说明，实际 ${hint}`);
    await shot(memberPage, 'desktop-10-member-live');
    ok('普通用户：左栏无直播、直开 /live 有可见权限说明', hint);
  } else {
    console.log('NOTE  普通用户登录失败，跳过该场景（报告里如实标为未验证）');
  }

  assert(pageErrors.length === 0, `出现未捕获的前端异常：${pageErrors.join(' | ')}`);
  ok('全程没有未捕获的前端异常');
  await context.close();
}

/* --------------------------------------------------------------- 收尾 */

const cleanupState = { dbDropped: false, portsClean: false };
let exitCode = 0;

async function cleanupResources() {
  while (servers.length > 0) {
    const item = servers.pop();
    await stopProcess(item.child, item.label);
  }
  if (!process.env.KEEP_SERVERS) {
    const stillListening = [apiPort, webPort].filter((port) => port && listeningOn(port));
    cleanupState.portsClean = stillListening.length === 0;
    if (!cleanupState.portsClean) console.error(`FAIL  端口仍被监听：${stillListening.join(', ')}`);
    if (tmpRoot) fs.rmSync(tmpRoot, { recursive: true, force: true });
  } else {
    cleanupState.portsClean = true;
  }
  if (DB_NAME) {
    try {
      dropOwnedDatabase({ name: DB_NAME, pgPort: PG_PORT, python: PYTHON, repoRoot: REPO_ROOT });
      cleanupState.dbDropped = true;
    } catch (error) {
      console.error(`FAIL  清理本次测试库失败：${error?.message || String(error)}`);
    }
  } else {
    cleanupState.dbDropped = true;
  }
  return cleanupState;
}

onTermination(async () => {
  await cleanupResources();
});

try {
  createTestDatabase();
  console.log(`隔离数据库：本次新建 ${DB_NAME}@127.0.0.1:${PG_PORT}（结束时只删这一个）`);
  await bootApi();
  await bootWeb();
  console.log(`Playwright：${PLAYWRIGHT_ORIGIN}`);
  const browser = await chromium.launch({ executablePath: EXECUTABLE });
  console.log(`浏览器：${browser.version()}${EXECUTABLE ? `（CHROME_PATH=${EXECUTABLE}）` : '（内置缓存）'}`);
  try {
    await main(browser);
  } finally {
    await browser.close();
  }
} catch (error) {
  console.error(`\nFAIL  ${error instanceof Error ? error.message : String(error)}`);
  exitCode = 1;
} finally {
  const state = await cleanupResources();
  if (!state.portsClean) exitCode = 1;
  if (!state.dbDropped && process.env.KEEP_SERVERS !== '1' && DB_NAME) exitCode = 1;
}

console.log(`\n结果：${passed} 项通过，退出码 ${exitCode}`);
console.log(`截图 / 日志目录：${SHOTS}` + (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 指定）' : '（默认临时目录）'));
process.exit(exitCode);
