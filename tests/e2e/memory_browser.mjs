/**
 * 记忆工作台：真实 Chromium + API + worker + PostgreSQL，不 mock `/v1`。
 * 只在自己创建的一次性回环数据库和随机端口上运行；绝不复用外部实例。
 * 覆盖记忆库/详情、核心记忆、隐私清理、学习设置与真实故障恢复。
 * 搜索、筛选竞态、键盘与移动布局的细节由独立 mock 浏览器套件覆盖。
 *
 * 用法：node tests/e2e/memory_browser.mjs
 * 可选：E2E_PYTHON=/abs/path/to/venv/bin/python（默认本 checkout 的 .venv）。
 * 测试 PG 在 127.0.0.1:$LING_PGPORT（默认 55432）；库名可用 E2E_DB_NAME 指定。
 * CHROME_PATH 覆盖 Playwright 浏览器路径；SHOTS_DIR 覆盖默认独立临时目录。
 * KEEP_SERVERS=1 保留服务及自建库供调试；默认杀进程组、复查端口、只删除自建库。
 */
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

import {
  createIsolatedDatabase,
  createdDatabases,
  dropOwnedDatabase,
  execDbStatements,
  makeRunId,
  onTermination,
  resolveShotsDir,
} from './lib/isolation.mjs';
import { loadChromium } from './lib/playwright.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
if (process.env.BASE_URL || process.env.API_BASE) {
  console.error('拒绝 BASE_URL / API_BASE：本测试会永久清理记忆，必须由脚本自行启动隔离栈。');
  process.exit(2);
}
const RUN_ID = makeRunId();
const SHOTS_INFO = resolveShotsDir('memory', RUN_ID);
/** 截图 / 日志目录：默认临时目录；只有 SHOTS_DIR 才写别处。 */
const SHOTS = SHOTS_INFO.dir;
const PG_PORT = Number(process.env.LING_PGPORT || 55432);
/** 本次运行创建的库名；只有它会被 DROP（边界见 lib/isolation.mjs）。 */
let DB_NAME = null;
const PYTHON = process.env.E2E_PYTHON || path.join(REPO_ROOT, '.venv', 'bin', 'python');
const USERNAME = 'mem_e2e';
const PASSWORD = `mem-e2e-${RUN_ID}`;
const FORBIDDEN_PORTS = new Set([8080, 55432, PG_PORT]);

console.log(
  `截图 / 日志目录：${SHOTS}` +
    (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定）' : '（未设 SHOTS_DIR，默认写临时目录，不写仓库）'),
);

/* --------------------------------------------------------------- playwright */

// 解析顺序与失败提示都在 lib/playwright.mjs：`clients/web-console` → 仓库根
// node_modules → 直连 import('playwright')。原来这里只有第一步，取不到就直接
// exit 2，即使调用方在别处装过 playwright 也用不上。
const { chromium, origin: PLAYWRIGHT_ORIGIN } = await loadChromium();
console.log(`Playwright：${PLAYWRIGHT_ORIGIN}`);
const EXECUTABLE = process.env.CHROME_PATH || undefined;

/* --------------------------------------------------------------- 小工具 */

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

function listeningOn(port, pid) {
  try {
    const out = execFileSync('lsof', ['-nP', ...(pid ? ['-a', '-p', String(pid)] : []), `-iTCP:${port}`, '-sTCP:LISTEN'], { encoding: 'utf8' });
    return out.trim();
  } catch (error) {
    if (error.status === 1) return '';
    throw error;
  }
}

function startProcess(label, command, args, options) {
  const logPath = options.logPath;
  const fd = fs.openSync(logPath, 'a');
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env,
    detached: true,
    stdio: ['ignore', fd, fd],
  });
  // 父进程不再需要这个 fd；子进程已经继承。
  fs.closeSync(fd);
  child.on('error', (error) => console.error(`[${label}] ${error.message}（日志：${logPath}）`));
  child.on('exit', (code, signal) => {
    // 0 = 正常退出；143 = 128+SIGTERM，被本脚本自己 SIGTERM 收掉的 vite/node 会这样退。
    // 这两种都是干净的收尾，不该打印「进程退出」——否则每次跑完都像起服务失败（噪声）。
    // 真正的异常退出（1/2/…）与超时被杀仍然照旧报出来。
    const normal = code === null || code === 0 || code === 143;
    if (!normal && !options.allowedExit) {
      console.error(`[${label}] 进程退出 code=${code} signal=${signal}，日志：${logPath}`);
    }
  });
  return child;
}

async function stopProcess(child, label) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
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

/* --------------------------------------------------------------- 数据库隔离 */

/**
 * 建一个只属于本次运行的库。
 *
 * **不做「先 DROP 再 CREATE」**：那会让两个并行实例互相把对方的库清空，
 * 于是报出「320px 找不到 98 字长正文条目」这类假失败。这里先 CREATE；
 * 名字被占用（上一次崩溃留下的，或另一个实例的）就换名，绝不删别人的库。
 */
function createTestDatabase() {
  try {
    const created = createIsolatedDatabase({
      tag: 'memory',
      pgPort: PG_PORT,
      python: PYTHON,
      repoRoot: REPO_ROOT,
      runId: RUN_ID,
    });
    DB_NAME = created.name;
  } catch (error) {
    console.error(`无法创建隔离测试库：${error?.message || error}`);
    console.error('请确认 E2E_PYTHON 可运行并装有项目依赖、隔离 PG 已启动：scripts/dev_pg.sh start');
    process.exit(2);
  }
}

/** 在本次自己的库上执行固定 SQL（real-403 / real-500 的故障注入）。 */
function execDb(statements) {
  assert(createdDatabases().includes(DB_NAME), '只允许在本次自建库执行 SQL');
  return execDbStatements({
    database: DB_NAME,
    statements,
    pgPort: PG_PORT,
    python: PYTHON,
    repoRoot: REPO_ROOT,
  });
}

/* --------------------------------------------------------------- 服务启动 */

const servers = [];
let tmpRoot = null;
let apiBase = null;
let webBase = null;
let serviceEnv = null;
let apiPort = null;
let webPort = null;
let apiLog = null;
let webLog = null;

async function bootApi() {
  apiPort = await freePort();
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ling-mem-e2e-'));
  const dataDir = path.join(tmpRoot, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  apiLog = path.join(SHOTS, 'api.log');
  fs.writeFileSync(apiLog, '');

  assert(createdDatabases().includes(DB_NAME), '只允许在本次自建库启动 API');
  serviceEnv = {
    ...Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('LING_'))),
    // 外部 venv 可能 editable 安装了另一个 checkout；总是加载当前工作树源码。
    PYTHONPATH: path.join(REPO_ROOT, 'src'),
    LING_ENV: 'dev',
    LING_HOST: '127.0.0.1',
    LING_PORT: String(apiPort),
    LING_DATABASE_URL: `postgresql+asyncpg://ling@127.0.0.1:${PG_PORT}/${DB_NAME}`,
    LING_BOOTSTRAP_ADMIN: USERNAME,
    LING_BOOTSTRAP_ADMIN_PASSWORD: PASSWORD,
    LING_TEST_MODE: '1',
    // 不读仓库 .data/（那里有真实根密钥）；临时目录里自带的密钥只服务本次测试。
    LING_DATA_DIR: dataDir,
    LING_COOKIE_NAME: 'mem_e2e_sid',
    LING_ONEBOT_WS_PORT: '',
    LING_QQ_ENABLED: '0',
    LING_QQBOT_ENABLED: '0',
    LING_UPDATE_ENABLED: '0',
    LING_ACCESS_MODE: 'off',
    LING_USER_TIMEZONE: 'Asia/Shanghai',
    LING_WEB_DIST: '',
  };
  const child = startProcess('api', PYTHON, ['-m', 'ling'], { cwd: REPO_ROOT, env: serviceEnv, logPath: apiLog });
  servers.push({ child, label: `api:${apiPort}` });
  apiBase = `http://127.0.0.1:${apiPort}`;

  await waitUntil(
    async () => {
      if (!child.pid || !listeningOn(apiPort, child.pid)) return false;
      const { status } = await httpJson(`${apiBase}/healthz`).catch(() => ({ status: 0 }));
      return status === 200;
    },
    `API ${apiBase}/healthz 就绪（日志 ${apiLog}）`,
    45_000,
    250,
  );
  const ready = await httpJson(`${apiBase}/readyz`);
  assert(ready.status === 200, `API 就绪检查失败：${ready.text.slice(0, 300)}`);
  ok('API 启动（真 PostgreSQL）', `${apiBase} · db=${DB_NAME}@127.0.0.1:${PG_PORT}`);
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
      if (!child.pid || !listeningOn(webPort, child.pid)) return false;
      const { status } = await httpJson(`${webBase}/`).catch(() => ({ status: 0 }));
      return status === 200;
    },
    `前端 ${webBase} 就绪（日志 ${webLog}）`,
    45_000,
    250,
  );
  ok('前端启动（vite dev，代理 /v1）', `${webBase} → ${apiBase}`);
}

async function bootWorker() {
  assert(serviceEnv && createdDatabases().includes(DB_NAME), '只允许在本次自建库启动 worker');
  const logPath = path.join(SHOTS, 'worker.log');
  const child = startProcess('worker', PYTHON, ['-m', 'ling.worker'], { cwd: REPO_ROOT, env: serviceEnv, logPath });
  servers.push({ child, label: 'worker' });
  await waitUntil(() => child.exitCode === null && child.signalCode === null &&
    fs.readFileSync(logPath, 'utf8').includes('worker 启动 env='), `worker 就绪（日志 ${logPath}）`, 30_000);
}

/* --------------------------------------------------------------- API 直连 */

let cookieJar = '';

async function apiFetch(pathname, init = {}) {
  const headers = { ...(init.headers || {}) };
  if (cookieJar) headers.cookie = cookieJar;
  const response = await fetch(`${apiBase}${pathname}`, { ...init, headers, redirect: 'error' });
  const setCookies = typeof response.headers.getSetCookie === 'function' ? response.headers.getSetCookie() : [];
  if (setCookies.length > 0) {
    cookieJar = setCookies.map((value) => value.split(';')[0]).join('; ');
  }
  return response;
}

async function apiJson(pathname, init) {
  const response = await apiFetch(pathname, init);
  const text = await response.text();
  assert(response.ok, `${init?.method || 'GET'} ${pathname} 失败：HTTP ${response.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

async function loginApi() {
  await apiJson('/v1/auth/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username: USERNAME, password: PASSWORD }),
  });
  const me = await apiJson('/v1/auth/me');
  assert(me?.user?.username === USERNAME, 'API 登录后 /v1/auth/me 不是测试账号');
  return me;
}

const SEED = {
  cite: 'E2E：她住在杭州',
  cat: 'E2E：她养了一只叫团子的猫',
  walk: 'E2E：昨天一起在湖边散步',
  procedure: 'E2E：先看日程再决定要不要开口',
  corrected: 'E2E：先看日程，再决定要不要开口（纠正版）',
};
const RETRACT_REASON = 'E2E 撤回测试';
const CORE_LABEL = 'E2E称呼';
const CORE_UPDATED = '请叫我小林';
const DISABLED_LABEL = 'E2E边界';

function jsonWrite(method, body) {
  return { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) };
}

async function seedData() {
  const ids = {};
  for (const [key, kind] of Object.entries({ cite: 'assertion', cat: 'assertion', walk: 'episode', procedure: 'procedure' })) {
    const { memory_id: id } = await apiJson('/v1/memory', jsonWrite('POST', {
      kind, text: SEED[key], origin: 'user_statement', visibility: 'subject_private',
      confidence: 0.8, source_event_ids: ['e2e:seed'],
    }));
    assert(id, `POST /v1/memory 未返回 ${key} 的 ID`);
    ids[key] = id;
  }
  const kept = await apiJson('/v1/core-memory', jsonWrite('PUT', {
    label: CORE_LABEL, text: '叫我小林', priority: 10, scope: 'private',
  }));
  const disabled = await apiJson('/v1/core-memory', jsonWrite('PUT', {
    label: DISABLED_LABEL, text: '别翻旧账', priority: 20, scope: 'private',
  }));
  const list = await apiJson('/v1/memory');
  assert(list.total === 4 && list.memories.length === 4 && Object.values(ids).every((id) =>
    list.memories.some((row) => row.memory_id === id && row.created_at)), '真实 API 未读回 4 条种子记忆及其时间');
  const core = await apiJson('/v1/core-memory');
  assert(core.items.length === 2 && [kept, disabled].every(({ core_id }) =>
    core.items.some((item) => item.core_id === core_id)), '真实 API 未读回 2 条核心记忆');
  ok('真实 API 创建并读回种子数据', '4 条记忆 + 2 条核心记忆');
  return { ...ids, keptCore: kept.core_id, disabledCore: disabled.core_id };
}

/* --------------------------------------------------------------- 浏览器辅助 */

const exact = (name) => ({ name, exact: true });
const tabLinks = (page) => page.getByRole('navigation', exact('记忆分区'));
const results = (page) => page.getByTestId('mem-results');
const inspector = (page) => page.getByTestId('mem-inspector');
const memoryButton = (page, text) => results(page).getByRole('button').filter({ hasText: text });
const coreSection = (page) => page.getByTestId('mem-core');
const coreItem = (page, label) => page.getByTestId('mem-core-list').getByRole('listitem').filter({ hasText: label });

async function loginUi(page) {
  await page.goto(`${webBase}/`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"]').fill(USERNAME);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(`${webBase}/dashboard`);
}

async function openFromNav(page, mobile = false) {
  if (mobile) await page.getByTestId('nav-open').click();
  const nav = page.getByTestId(mobile ? 'nav-drawer' : 'sidenav');
  await nav.getByTestId('nav-memory').click();
  await page.waitForURL(`${webBase}/memory`);
  await tabLinks(page).getByRole('link', exact('记忆库')).waitFor({ state: 'visible' });
  await page.getByRole('button', exact('列表')).click();
}

async function switchTab(page, name, tab = null) {
  const link = tabLinks(page).getByRole('link', exact(name));
  await link.click();
  await page.waitForURL(`${webBase}/memory${tab ? `?tab=${tab}` : ''}`);
  await waitUntil(async () => await link.getAttribute('aria-current') === 'location', `${name}成为当前分区`);
}

async function expectRows(page, texts) {
  await waitUntil(async () => {
    const buttons = results(page).getByRole('button');
    const all = await buttons.allTextContents();
    return all.length === texts.length && texts.every((text) => all.some((row) => row.includes(text)));
  }, `列表恰有 ${texts.length} 条：${texts.join(' / ')}`);
}

async function selectMemory(page, text) {
  await memoryButton(page, text).click();
  const detail = inspector(page);
  await detail.locator('.mem-inspector__text').and(detail.getByText(text, { exact: true })).waitFor();
  return detail;
}

// 注册监听再点击，并查实际的 HTTP 方法、路径、状态，不把界面提示当写入成功的证据。
async function browserResponse(page, method, pathname, action, status = 200) {
  const [response] = await Promise.all([
    page.waitForResponse((res) => {
      const url = new URL(res.url());
      return url.origin === webBase && url.pathname === pathname && res.request().method() === method;
    }),
    action(),
  ]);
  assert(response.status() === status, `${method} ${pathname}：预期 HTTP ${status}，实际 ${response.status()}`);
  return response;
}

async function setHistory(page, label, checked, expectedInclude) {
  const control = page.getByRole('checkbox', exact(label));
  if (!await control.isVisible()) await page.getByRole('button', exact('筛选')).click();
  const response = await browserResponse(page, 'GET', '/v1/memory', () => control.setChecked(checked));
  assert(new URL(response.url()).searchParams.get('include') === expectedInclude,
    `历史开关请求 include 不对：${response.url()}`);
  assert((await response.json()).include === expectedInclude, '历史读取响应没有回显正确的 include');
}

async function shot(page, name) {
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(`截图：${file}`);
}

function capturePageErrors(page, errors) {
  page.on('pageerror', (error) => errors.push(error.message));
}

async function exerciseCore(page, ids) {
  await switchTab(page, '核心记忆', 'core');
  const kept = coreItem(page, CORE_LABEL);
  const disabled = coreItem(page, DISABLED_LABEL);
  await kept.getByText('叫我小林', { exact: true }).waitFor({ state: 'visible' });
  await disabled.getByText('别翻旧账', { exact: true }).waitFor({ state: 'visible' });
  await shot(page, 'memory-core-desktop');
  await kept.getByRole('button', exact('编辑')).click();
  const form = page.getByTestId('mem-core-form');
  await form.getByRole('textbox', { name: /内容/ }).fill(CORE_UPDATED);
  const saved = await browserResponse(page, 'PUT', '/v1/core-memory', () =>
    form.getByRole('button', exact('保存并覆盖')).click());
  assert((await saved.json()).core_id === ids.keptCore, '核心记忆同名覆盖不应换 ID');
  await kept.getByText(CORE_UPDATED, { exact: true }).waitFor({ state: 'visible' });
  const edited = (await apiJson('/v1/core-memory')).items;
  assert(edited.length === 2 && edited.some((item) => item.core_id === ids.keptCore && item.text === CORE_UPDATED),
    '核心记忆编辑未持久化，或额外新增了一条');
  ok('核心记忆：在独立分区编辑，同名覆盖持久化且保留 ID');

  const disableButton = disabled.getByRole('button', exact('停用…'));
  const confirm = disabled.getByRole('group', exact(`停用核心记忆 ${DISABLED_LABEL}`));
  await disableButton.click();
  await confirm.getByText('软删除', { exact: true }).waitFor({ state: 'visible' });
  await confirm.getByRole('button', exact('取消')).click();
  await confirm.waitFor({ state: 'hidden' });
  assert((await apiJson('/v1/core-memory')).items.some((item) => item.core_id === ids.disabledCore), '取消仍停用了核心记忆');
  await disableButton.click();
  const deleted = await browserResponse(page, 'DELETE', `/v1/core-memory/${ids.disabledCore}`, () =>
    confirm.getByRole('button', exact('确认停用')).click());
  assert((await deleted.json()).active === false, '停用响应没有返回 active=false');
  await disabled.waitFor({ state: 'detached' });
  const active = (await apiJson('/v1/core-memory')).items;
  assert(active.length === 1 && active[0].core_id === ids.keptCore, '停用后 API 没有只返回另一条核心记忆');
  execDb([`DO $$ BEGIN IF (SELECT count(*) FROM core_memories WHERE NOT active) <> 1 THEN
    RAISE EXCEPTION '停用必须保留数据库里的 inactive 行'; END IF; END $$`]);
  ok('核心记忆：取消不写入，确认后软停用，原行仍在 PostgreSQL');
}

async function exerciseHistory(page, ids) {
  await switchTab(page, '记忆库');
  const detail = await selectMemory(page, SEED.cat);
  await detail.getByRole('button', exact('撤回…')).click();
  const retract = detail.getByRole('form', exact('撤回这条记忆'));
  const retractButton = retract.getByRole('button', exact('确认撤回'));
  assert(await retractButton.isDisabled(), '没有理由时可以确认撤回');
  await retract.getByRole('textbox', exact('撤回理由')).fill(RETRACT_REASON);
  const retracted = await browserResponse(page, 'POST', `/v1/memory/${ids.cat}/retract`, () => retractButton.click());
  assert((await retracted.json()).retracted === true, '撤回响应没有确认撤回');
  const other = [SEED.cite, SEED.walk, SEED.procedure];
  await expectRows(page, other);
  const visible = await apiJson('/v1/memory');
  assert(visible.total === 3 && !visible.memories.some((row) => row.memory_id === ids.cat), '默认 API 仍泄漏已撤回记录');
  await setHistory(page, '显示已撤回', true, 'retracted');
  await expectRows(page, [...other, SEED.cat]);
  await selectMemory(page, SEED.cat);
  await detail.getByText(`撤回理由：${RETRACT_REASON}`, { exact: true }).waitFor({ state: 'visible' });
  await detail.getByText('历史记录 · 只读', { exact: true }).waitFor({ state: 'visible' });
  assert(await detail.getByRole('button', { name: /^(撤回|纠正)…$/ }).count() === 0, '历史记录仍可编辑');
  ok('长期记忆：撤回需理由，默认读不到，include=retracted 中能读到原因');

  await setHistory(page, '显示已归档', true, 'all');
  await selectMemory(page, SEED.procedure);
  await detail.getByRole('button', exact('纠正…')).click();
  const correction = detail.getByRole('form', exact('纠正这条记忆'));
  const correctButton = correction.getByRole('button', exact('确认纠正'));
  assert(await correctButton.isDisabled(), '没有新内容时可以确认纠正');
  await correction.getByRole('textbox', exact('新的说法')).fill(SEED.corrected);
  const corrected = await browserResponse(page, 'POST', `/v1/memory/${ids.procedure}/supersede`, () => correctButton.click());
  const replacement = await corrected.json();
  assert(replacement.supersedes === ids.procedure && replacement.memory_id !== ids.procedure, '纠正响应没有创建继任条目');
  await expectRows(page, [...Object.values(SEED)]);
  await selectMemory(page, SEED.procedure);
  await detail.getByText('已被纠正', { exact: true }).waitFor({ state: 'visible' });
  await detail.getByRole('button', exact('纠正后的条目')).click();
  await detail.locator('.mem-inspector__text').and(detail.getByText(SEED.corrected, { exact: true })).waitFor();
  const history = (await apiJson('/v1/memory?include=all')).memories;
  const old = history.find((row) => row.memory_id === ids.procedure);
  const next = history.find((row) => row.memory_id === replacement.memory_id);
  assert(old?.retracted && old.superseded_by === next?.memory_id && next?.supersedes === ids.procedure &&
    next.text === SEED.corrected && next.visibility === old.visibility && next.origin === old.origin &&
    next.source_event_ids.includes('e2e:seed'), '真实 API 没有保留纠正链或新条目的来源/可见范围');
  await setHistory(page, '显示已撤回', false, 'archived');
  await expectRows(page, [SEED.cite, SEED.walk, SEED.corrected]);
  await setHistory(page, '显示已归档', false, null);
  await expectRows(page, [SEED.cite, SEED.walk, SEED.corrected]);
  const active = (await apiJson('/v1/memory')).memories;
  assert(active.length === 3 && active.some((row) => row.memory_id === replacement.memory_id) &&
    !active.some((row) => row.memory_id === ids.procedure), 'API 默认视图没有用新条目替换旧条目');
  ok('长期记忆：纠正保留双向历史链，只把新条目放进默认视图；验证四种 include 状态');
}

async function exerciseRealErrors(page) {
  // 切 tab 会主动读核心记忆；等这次正常读取结束再制造故障，不能把它当刷新响应。
  await browserResponse(page, 'GET', '/v1/core-memory', () => switchTab(page, '核心记忆', 'core'));
  const kept = coreItem(page, CORE_LABEL).getByText(CORE_UPDATED, { exact: true });
  await kept.waitFor({ state: 'visible' });
  const refreshCore = () => coreSection(page).getByRole('button', exact('刷新核心记忆')).click();
  const error = coreSection(page).getByTestId('mem-section-error');
  const retry = error.getByRole('button', exact('重试'));

  execDb(['ALTER TABLE core_memories RENAME TO core_memories__e2e_hidden']);
  try {
    // 未捕获的 SQL 异常可能由 ASGI 以纯文本 500 返回，不要求 JSON。
    await browserResponse(page, 'GET', '/v1/core-memory', refreshCore, 500);
    await retry.waitFor({ state: 'visible' });
    assert(await error.getAttribute('data-error-code') === 'internal' &&
      await error.getAttribute('data-retriable') === 'true', 'real-500 没被归为可重试的内部错误');
    await shot(page, 'memory-real-500');
    const list = await apiJson('/v1/memory');
    assert(list.total === 3 && list.memories.some((row) => row.text === SEED.corrected), 'real-500 连带影响了真实记忆库 API');
  } finally {
    execDb(['ALTER TABLE core_memories__e2e_hidden RENAME TO core_memories']);
  }
  await browserResponse(page, 'GET', '/v1/core-memory', () => retry.click());
  await kept.waitFor({ state: 'visible' });
  await error.waitFor({ state: 'hidden' });
  ok('real-500：真数据库故障不连带影响记忆库；复位后点重试并读回核心记忆');

  execDb(["UPDATE users SET must_change_password = TRUE WHERE username = 'mem_e2e'"]);
  try {
    assert((await apiJson('/v1/auth/me')).user.must_change_password === true, '账号闸未开启');
    const response = await browserResponse(page, 'GET', '/v1/core-memory', refreshCore, 403);
    const { error: body } = await response.json();
    assert(body?.code === 'forbidden' && body.detail === 'must_change_password', 'real-403 不是首次改密账号闸');
    const link = error.getByRole('link', { name: /设置.*账号/ });
    await link.waitFor({ state: 'visible' });
    assert(await error.getAttribute('data-retriable') === 'false' && !await retry.count(), '账号闸仍被标为可重试');
    assert(await link.getAttribute('href') === '/settings/account', '账号闸未指向改密入口');
    await shot(page, 'memory-real-403');
    await link.click();
    await page.waitForURL(`${webBase}/settings/account`);
  } finally {
    execDb(["UPDATE users SET must_change_password = FALSE WHERE username = 'mem_e2e'"]);
  }
  await page.goto(`${webBase}/memory?tab=core`, { waitUntil: 'domcontentloaded' });
  await kept.waitFor({ state: 'visible' });
  ok('real-403：真账号闸不给重试、可点到账号设置，复位后恢复');
}

async function exerciseTypedLibrary(errors) {
  const claim = 'E2E：展馆的样张需要从短边对齐';
  const url = 'https://example.invalid/paper-guide';
  const { item_id: id } = await apiJson('/v1/knowledge', jsonWrite('POST', {
    topic: 'E2E 样张手册', claim, source_url: url, source_kind: 'verified_source',
  }));
  await apiJson('/v1/memory', jsonWrite('POST', {
    kind: 'procedure', text: claim, origin: 'verified_source', visibility: 'subject_private',
    source_event_ids: ['knowledge:' + id],
  }));
  for (const width of [1440, 375, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    try {
      const page = await context.newPage();
      capturePageErrors(page, errors);
      await loginUi(page);
      await page.goto(webBase + '/memory', { waitUntil: 'domcontentloaded' });
      await page.getByRole('button', exact('列表')).click();
      const types = page.getByRole('group', { name: '记忆类型' });
      for (const [label, kind] of [['知识', 'knowledge'], ['做法', 'procedure']]) {
        const [response] = await Promise.all([
          page.waitForResponse((res) => {
            const requestUrl = new URL(res.url());
            return requestUrl.pathname === '/v1/memory' && requestUrl.searchParams.get('kind') === kind;
          }),
          types.getByRole('button', exact(label)).click(),
        ]);
        assert(response.ok(), '服务器未接受类型筛选');
        await expectRows(page, [claim]);
        const detail = await selectMemory(page, claim);
        assert(await detail.getByRole('link', exact('查看来源资料')).getAttribute('href') === url, '来源资料不正确');
        const dimensions = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
        assert(dimensions[0] <= dimensions[1], '类型详情在 ' + width + 'px 出现横向溢出');
        if (kind === 'procedure') await shot(page, 'memory-types-' + width);
        const back = detail.getByRole('button', exact('回到列表'));
        if (await back.isVisible()) await back.click();
      }
      ok(width + 'px 类型筛选、服务端请求、资料回查和详情布局');
    } finally {
      await context.close();
    }
  }
}

async function exerciseMobile(errors) {
  const context = await browser.newContext({
    viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2,
  });
  try {
    const page = await context.newPage();
    capturePageErrors(page, errors);
    await loginUi(page);
    await openFromNav(page, true);
    await expectRows(page, [SEED.cite, SEED.walk, SEED.corrected]);
    await shot(page, 'memory-mobile-list');
    const detail = await selectMemory(page, SEED.corrected);
    await shot(page, 'memory-mobile-inspector');
    await detail.getByRole('button', exact('回到列表')).click();
    await memoryButton(page, SEED.corrected).waitFor({ state: 'visible' });
    await detail.waitFor({ state: 'hidden' });
    ok('375px 手机：从导航抽屉读真实列表，打开详情再返回');
  } finally {
    await context.close();
  }
}

async function exerciseContinuityControls(page) {
  await page.getByTestId('sidenav').getByTestId('nav-life').click();
  await page.waitForURL(`${webBase}/life`);
  await page.getByText('更多生活记录', { exact: true }).click();
  const controls = page.getByRole('region', { name: '联系与表达设置' });
  const resource = '/v1/relationship/preferences';
  await browserResponse(page, 'PUT', `${resource}/pause`, () =>
    controls.getByRole('button', { name: '暂停主动分享' }).click());
  assert((await apiJson(resource)).share_pause.paused, '未保存主动分享暂停');
  await browserResponse(page, 'PUT', `${resource}/pause`, () =>
    controls.getByRole('button', { name: '恢复主动分享' }).click());
  assert(!(await apiJson(resource)).share_pause.paused, '未保存主动分享恢复');

  const topic = '测试用青桐街旧约';
  await controls.getByLabel('不希望提起的话题').fill(topic);
  const created = await browserResponse(page, 'POST', `${resource}/boundary`, () =>
    controls.getByRole('button', { name: '添加边界' }).click());
  const { id } = await created.json();
  const row = controls.getByRole('listitem').filter({ hasText: topic });
  await browserResponse(page, 'PATCH', `${resource}/${id}`, () =>
    row.getByRole('button', { name: '停用', exact: true }).click());
  const preferences = (await apiJson(resource)).preferences;
  assert(preferences.some((item) => item.id === id && item.state === 'disabled'), '未保存边界停用');
  await shot(page, 'life-continuity-controls');
  ok('生活记录：在真实浏览器暂停并恢复分享，添加和停用话题边界');
}

// API 读不到只能证明被代次栅栏隐藏；直接确认隔离库的行数才能区分软隐藏和真删。
function expectDbRows(memories, core) {
  execDb([`DO $$ BEGIN
    IF (SELECT count(*) FROM memories) <> ${memories} OR (SELECT count(*) FROM core_memories) <> ${core} THEN
      RAISE EXCEPTION '预期 PostgreSQL 物理行数：memories=${memories}, core_memories=${core}';
    END IF;
  END $$`]);
}

async function exerciseForget(page) {
  await switchTab(page, '隐私与清理', 'privacy');
  const danger = page.getByTestId('mem-danger');
  const start = danger.getByRole('button', exact('遗忘全部记忆…'));
  const panel = danger.getByRole('group', exact('二次确认：遗忘全部记忆'));
  const confirm = panel.getByRole('button', exact('确认遗忘全部'));
  let writes = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url() === `${webBase}/v1/memory/forget`) writes += 1;
  });
  await start.click();
  await panel.waitFor({ state: 'visible' });
  assert(await confirm.isDisabled(), '未勾选永久删除确认时可提交');
  await panel.getByRole('button', exact('取消')).click();
  await panel.waitFor({ state: 'hidden' });
  assert(writes === 0 && (await apiJson('/v1/memory/purge/status')).items.length === 0, '取消仍发起了清理');
  // 四条种子 + 一条纠正记录；两条核心（含停用条目）。
  expectDbRows(5, 2);
  await start.click();
  assert(await confirm.isDisabled(), '打开新的确认时仍保留确认状态');
  await panel.getByRole('checkbox', { name: /永久删除.*无法撤销/ }).check();
  assert(await confirm.isEnabled(), '勾选后仍无法提交');
  await panel.screenshot({ path: path.join(SHOTS, 'memory-purge-confirm.png') });
  const response = await browserResponse(page, 'POST', '/v1/memory/forget', () => confirm.click());
  const accepted = await response.json();
  assert(writes === 1 && accepted.state === 'pending' && accepted.tombstone_id, 'POST 未返回单次后台清理受理记录');
  await danger.getByTestId('mem-forget-purging').waitFor({ state: 'visible' });
  expectDbRows(5, 2); // Worker 尚未启动；已受理不等于物理删除完成。
  ok('遗忘全部：取消不提交，勾选后只请求一次，pending 不冒充删除完成');

  await bootWorker();
  const done = danger.getByTestId('mem-forget-result');
  await done.waitFor({ state: 'visible', timeout: 60_000 });
  const status = (await apiJson('/v1/memory/purge/status')).items.find((item) => item.tombstone_id === accepted.tombstone_id);
  assert(status?.state === 'done' && status.completed_at && status.memories_deleted === 5 && status.core_deleted === 2,
    `后台清理未报告真实删除计数：${JSON.stringify(status)}`);
  const counts = await done.getByTestId('mem-forget-count-line').innerText();
  assert(counts.includes('5 条记忆') && counts.includes('2 条核心记忆'), `页面未显示后台删除计数：${counts}`);
  expectDbRows(0, 0);
  await shot(page, 'memory-purge-done');

  for (const query of ['', '?include=all']) {
    const list = await apiJson(`/v1/memory${query}`);
    assert(list.total === 0 && list.memories.length === 0, `清理后 API 仍能读到条目：${query}`);
  }
  assert((await apiJson('/v1/core-memory')).items.length === 0, '清理后 API 仍返回核心记忆');
  await switchTab(page, '记忆库');
  await page.getByTestId('mem-list-empty').waitFor({ state: 'visible' });
  await switchTab(page, '核心记忆', 'core');
  await page.getByTestId('mem-core-empty').waitFor({ state: 'visible' });
  ok('遗忘全部：真实 worker 完成，UI/状态 API 计数一致，PostgreSQL 真删且 include=all 读不到旧记录');
}

async function exerciseSettings(page) {
  const field = page.getByTestId('mem-settings').getByRole('spinbutton', { name: /最长保留/ });
  const response = await browserResponse(page, 'GET', '/v1/settings/config', () => switchTab(page, '学习设置', 'settings'));
  const retentionDays = (await response.json()).memory?.max_retention_days;
  await field.waitFor({ state: 'visible' });
  assert(Number.isFinite(retentionDays) && await field.inputValue() === String(retentionDays),
    '学习设置的最长保留天数与真实配置不一致');
  await shot(page, 'memory-settings-desktop');
  await page.goto(`${webBase}/memory?tab=settings`, { waitUntil: 'domcontentloaded' });
  await field.waitFor({ state: 'visible' });
  await page.goto(`${webBase}/settings/memory`, { waitUntil: 'domcontentloaded' });
  await page.waitForURL(`${webBase}/memory?tab=settings`);
  await field.waitFor({ state: 'visible' });
  await page.getByRole('button', exact('返回记忆内容')).click();
  await page.waitForURL(`${webBase}/memory`);
  await page.getByTestId('mem-list-empty').waitFor({ state: 'visible' });
  ok('学习设置：真实配置、分区和深链、旧路径重定向，以及返回记忆库');
}

let browser = null;
let exitCode = 0;

/**
 * 收尾：关浏览器 → kill 服务进程组 → lsof 复查端口 → **只删本次自建的库**。
 * 正常结束、断言失败、Ctrl-C/SIGTERM 都走这里（幂等）。
 */
let cleanupPromise = null;
function cleanupResources() {
  // 断言收尾与信号收尾等待同一次清理，不能有一方提前退出进程。
  return cleanupPromise ??= cleanup();
}

async function cleanup() {
  const cleanupState = { portsClean: true, dbDropped: false, dbError: null };
  if (browser) await browser.close().catch(() => undefined);

  if (process.env.KEEP_SERVERS === '1') {
    console.log(
      `\nKEEP_SERVERS=1：保留进程 api=${apiPort} web=${webPort}` +
        (DB_NAME ? `、测试库 ${DB_NAME}（调试完请手动删）` : ''),
    );
    return cleanupState;
  }

  for (const { child, label } of servers.reverse()) {
    await stopProcess(child, label);
  }

  // kill 之后必须复查：端口没人监听才算真的收干净（不许留孤儿占着端口）。
  const ports = [apiPort, webPort].filter(Boolean);
  let leftovers = '';
  await waitUntil(
    async () => {
      leftovers = ports.map((port) => ({ port, text: listeningOn(port) })).filter((item) => item.text.length > 0)
        .map((item) => `:${item.port}\n${item.text}`)
        .join('\n');
      return leftovers === '';
    },
    '端口释放',
    8_000,
    250,
  ).catch(() => undefined);
  if (leftovers) {
    cleanupState.portsClean = false;
    console.error(`\nFAIL  端口仍有监听进程（lsof 复查）：\n${leftovers}`);
  } else {
    console.log(`\nlsof 复查：${ports.map((port) => `:${port}`).join(' / ')} 已无监听进程`);
    if (tmpRoot) console.log(`临时数据目录（可删）：${tmpRoot}`);
    if (apiLog) console.log(`API 日志：${apiLog}`);
    if (webLog) console.log(`前端日志：${webLog}`);
  }

  // 先 kill 服务再删库：DROP 只对「本次通过 CREATE DATABASE 亲手建的」那个名字执行，
  // 别的库一律不碰（见 lib/isolation.mjs 的 createdByThisRun 边界）。
  if (DB_NAME) {
    try {
      dropOwnedDatabase({ name: DB_NAME, pgPort: PG_PORT, python: PYTHON, repoRoot: REPO_ROOT });
      cleanupState.dbDropped = true;
    } catch (error) {
      cleanupState.dbError = error?.message || String(error);
      console.error(`\nFAIL  清理本次测试库失败：${cleanupState.dbError}`);
    }
  }
  return cleanupState;
}

onTermination(async (signal) => {
  console.log(`\n收到 ${signal}：开始收尾（kill 服务、删本次自建的库）`);
  await cleanupResources();
});

try {
  createTestDatabase();
  await bootApi();
  await bootWeb();
  await loginApi();
  const ids = await seedData();

  browser = await chromium.launch({ executablePath: EXECUTABLE });
  const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await desktop.newPage();
  const errors = [];
  capturePageErrors(page, errors);
  await loginUi(page);
  await openFromNav(page);
  await expectRows(page, [SEED.cite, SEED.cat, SEED.walk, SEED.procedure]);
  const detail = await selectMemory(page, SEED.cite);
  await detail.getByText('你说的', { exact: true }).waitFor({ state: 'visible' });
  const absoluteTime = await detail.locator('time').first().innerText();
  assert(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(absoluteTime), `详情缺少绝对时间：${absoluteTime}`);
  await shot(page, 'memory-desktop');
  ok('桌面：从记忆导航进入工作台，真实列表按钮打开对应详情');

  await exerciseCore(page, ids);
  await exerciseHistory(page, ids);
  await exerciseRealErrors(page);
  await exerciseMobile(errors);
  await exerciseForget(page);
  await exerciseSettings(page);
  await exerciseTypedLibrary(errors);
  await exerciseContinuityControls(page);
  assert(errors.length === 0, `桌面或手机有未捕获的 JS 错误：${errors.join(' | ')}`);
  ok('桌面与手机没有未捕获的页面错误');
} catch (error) {
  exitCode = 1;
  console.error(`\nFAIL  ${error?.message || error}`);
  console.error(error?.stack || '');
} finally {
  const state = await cleanupResources();
  if (!state.portsClean) exitCode = 1;
  if (state.dbError) exitCode = 1;
  if (!state.dbDropped && process.env.KEEP_SERVERS !== '1' && DB_NAME) exitCode = 1;

  console.log(`\n结果：${passed} 项通过，退出码 ${exitCode}`);
  console.log(
    `截图 / 日志目录：${SHOTS}` +
      (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定，会写到那里）' : '（默认临时目录；设 SHOTS_DIR 可改）'),
  );
  process.exit(exitCode);
}
