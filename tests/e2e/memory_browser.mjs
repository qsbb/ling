/**
 * 记忆内容页（/memory）的浏览器实测：**真 API + 真 PostgreSQL + 真 Chromium**。
 *
 * 与 `dashboard_browser.mjs` 的分工：那边全部 `/v1/**` 用 mock，验证布局；
 * 这边要验证「撤回一条后默认列表少一条」「include=all 看得到墓碑」「forget 后清空」
 * 这类**跨请求的状态变化**，mock 会把它们变成自证，所以坚持打真接口。
 *
 * 隔离（硬约束）：
 * - 数据库是本地 PG 127.0.0.1:55432 上的**一次性库**：每次运行 `CREATE DATABASE`
 *   `ling_memory_e2e_<运行 id>`（UTC 时间戳 + pid + 随机），**只 DROP 本次自己建的
 *   那个库**；绝不 DROP 同名但非本次创建的库，也绝不碰 `ling` / `ling_test`。
 *   `E2E_DB_NAME` 可指定库名；若该库已存在（可能是上一次崩溃留下的，也可能是另一个
 *   并行实例的），脚本不会删它，而是自动加后缀换名——先建后删是并行的前提。
 *   边界实现见 `tests/e2e/lib/isolation.mjs`（含 `guard-check` 自检）。
 * - API 与前端都跑在随机高位端口（避开 8080 与 55432），通过 `--strictPort` 起；
 *   结束时 kill 进程组，并用 `lsof` 复查端口确实没有人监听。
 * - API 的 `LING_DATA_DIR` 指向临时目录，不读仓库 `.data/`（那里有真实根密钥）。
 * - 截图 / 日志默认写临时目录（`<tmp>/ling-e2e/memory-<运行 id>/`），**不默认写
 *   仓库里的 `shots/`**：那会覆盖别人的证据。只有显式设置 `SHOTS_DIR` 才写指定
 *   位置；脚本开头与结尾都打印最终输出目录。
 *
 * 覆盖（对应 TASK103 的验收清单）：
 * 1. 真实数据渲染：核心记忆 2 条、记忆列表 3 条，含绝对时间 / origin / 置信度；
 * 2. 撤回一条 → 默认列表从 4 条变 3 条，且墓碑从默认视图消失；
 * 3. 勾选「显示已撤回 / 显示已归档」→ include 取值与墓碑可见性按契约变化（= all）；
 * 4. 纠正（supersede）→ 旧条目出现「已被纠正」，新条目出现在列表里；
 * 5. forget 二次确认（未勾选时按钮禁用）→ 返回影响行数、明确「无法撤销」→ 列表与核心记忆清空；
 * 6. `?tab=settings` 深链 → 调参 tab；旧路径 `/settings/memory` 也开在调参 tab；
 *    文案必须写「生效状态：已接线」，并写清两点例外（rerank_top_n 未配重排模型时不生效、
 *    max_retention_days 是硬上限），且**不得**出现「未接线」「接线由另一个任务修复中」；
 * 7. 375×667 与 320×667：单列、无横向溢出；遍历页面上**所有**可交互元素
 *    （button/a/input/select/textarea/summary/[role=button]）逐个断言 ≥44×44，
 *    覆盖默认视图、展开详情、撤回面板、遗忘二次确认与调参 tab。
 * 8. 375 / 320 的**末行字符数**：逐字符 `Range` 矩形聚类，量记忆正文（短 / 中 /
 *    98 字长文）、诚实边界、筛选说明、页脚、调参 tab 的末行；断言 ≥4 字符。
 *    320 档的 98 字长文正是 TASK119 复核点名的「人。」孤字行。
 * 9. 页脚文案随分段状态走（该组用 `route.fulfill` 构造，用例名标 **mock-403** /
 *    **mock-500**）：403 态不得出现「重试按钮就在那个分段里」（403 分段
 *    data-retriable=false、块内 0 个 button）；500 可重试时才说。
 * 10. **real-403**：`UPDATE users SET must_change_password=true` 触发真后端账号闸
 *    （`src/ling/api/deps.py`），断言与 mock 版一致的 UI 行为，并验证复位后恢复。
 * 11. **real-500**：临时把 `core_memories` 表改名 → 真后端未捕获异常 500，断言
 *    可重试、不说成权限问题；表改回来之后点段内「重试」真能重新取到数据。
 *    这两条是把「真 403 / 真 500」从口头说法变成可复跑的证据：mock 用例只证明
 *    UI 分支，real 用例证明后端真的会那样返回。
 *
 * 用法（默认自己起服务）：
 *   node tests/e2e/memory_browser.mjs
 *
 * 也可以指向已经在跑的一套（QA 想手动控制时）：
 *   BASE_URL=http://127.0.0.1:<前端端口> API_BASE=http://127.0.0.1:<API端口> \
 *   node tests/e2e/memory_browser.mjs
 *
 * 环境变量：
 *   BASE_URL / API_BASE   已就绪的前端 / API（同时给出才生效；此模式不建库）
 *   SHOTS_DIR             截图/日志目录（默认临时目录，不写仓库）
 *   E2E_DB_NAME           指定测试库名（默认自动生成；已存在则加后缀换名，不删它）
 *   CHROME_PATH           chromium headless shell 路径（默认走 playwright 自带）
 *   LING_PGPORT           PostgreSQL 端口（默认 55432）
 *   KEEP_SERVERS=1        结束时保留进程与测试库（调试用，会打印端口与库名）
 *   E2E_MUTATION=small-touch  自检用：把触控目标压小，验证 44×44 审计确实会失败
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
  execDbStatements,
  makeRunId,
  onTermination,
  resolveShotsDir,
} from './lib/isolation.mjs';
import { loadChromium } from './lib/playwright.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const RUN_ID = makeRunId();
const SHOTS_INFO = resolveShotsDir('memory', RUN_ID);
/** 截图 / 日志目录：默认临时目录；只有 SHOTS_DIR 才写别处。 */
const SHOTS = SHOTS_INFO.dir;
const PG_PORT = Number(process.env.LING_PGPORT || 55432);
/** 本次运行创建的库名；只有它会被 DROP（边界见 lib/isolation.mjs）。 */
let DB_NAME = null;
const PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python');
const USERNAME = 'mem_e2e';
const PASSWORD = 'mem-e2e-password';
const FORBIDDEN_PORTS = new Set([8080, 55432]);

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
  child.__label = label;
  child.__logPath = logPath;
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
    console.error('先确认本地 PG 在跑：scripts/dev_pg.sh start');
    process.exit(2);
  }
}

/** 在本次自己的库上执行固定 SQL（real-403 / real-500 的故障注入）。 */
function execDb(statements) {
  assert(DB_NAME, '没有自建库时不能执行 SQL（BASE_URL/API_BASE 复用模式）');
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
let apiBase = process.env.API_BASE || null;
let webBase = process.env.BASE_URL || null;
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

  const env = {
    ...process.env,
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
  const child = startProcess('api', PYTHON, ['-m', 'ling'], { cwd: REPO_ROOT, env, logPath: apiLog });
  servers.push({ child, label: `api:${apiPort}` });
  apiBase = `http://127.0.0.1:${apiPort}`;

  await waitUntil(
    async () => {
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
      const { status } = await httpJson(`${webBase}/`).catch(() => ({ status: 0 }));
      return status === 200;
    },
    `前端 ${webBase} 就绪（日志 ${webLog}）`,
    45_000,
    250,
  );
  ok('前端启动（vite dev，代理 /v1）', `${webBase} → ${apiBase}`);
}

/* --------------------------------------------------------------- API 直连 */

let cookieJar = '';

async function apiFetch(pathname, init = {}) {
  const headers = { ...(init.headers || {}) };
  if (cookieJar) headers.cookie = cookieJar;
  const response = await fetch(`${apiBase}${pathname}`, { ...init, headers });
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
  long: `E2E长文本：${'无空格长串' + 'a'.repeat(60) + '/'.repeat(20) + 'x'.repeat(60)}`,
  // 中长正文（51 字）：用来覆盖「短 / 中 / 长」三档里的「中」。
  medium:
    'E2E：上个月从城西搬到城东，离地铁近了两站路，早上能多睡一会儿，但晚上回来会经过一段很吵的施工路段。',
  // TASK119 复核点名的那类 98 字长文：320 下曾排成 7 行、末行只剩「人。」。
  // 这里固定成 98 个字符（含标点），末尾「人。」，逐字符量末行。
  long98:
    '她记得那天的风很大，我们一起沿着湖边的路慢慢走过去，一直走到结冰的岸边才肯停下来歇一歇，看远处的雾从水面上一层一层散开，看比夏天更安静的树林和空荡的湖岸线，也看看那些仍然愿意在冷风里慢慢出门的人。',
};

async function createMemory(kind, text, origin = 'user_statement') {
  return apiJson('/v1/memory', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      kind,
      text,
      origin,
      visibility: 'subject_private',
      confidence: 0.8,
      source_event_ids: ['e2e:seed'],
    }),
  });
}

async function seedData() {
  await createMemory('assertion', SEED.cite);
  await createMemory('assertion', SEED.cat);
  await createMemory('episode', SEED.walk);
  await createMemory('procedure', SEED.procedure);
  await apiJson('/v1/core-memory', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ label: 'E2E称呼', text: '叫我小林', priority: 10, scope: 'private' }),
  });
  await apiJson('/v1/core-memory', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ label: 'E2E边界', text: '别翻旧账', priority: 20, scope: 'private' }),
  });

  const list = await apiJson('/v1/memory');
  assert(list.memories.length === 4, `种子数据条数不对：${list.memories.length}`);
  assert(list.memories.some((row) => row.created_at), '种子数据没有 created_at（契约缺字段？）');
  const core = await apiJson('/v1/core-memory');
  assert(core.items.length === 2, `核心记忆种子条数不对：${core.items.length}`);
  ok('种子数据写入（走真 API，只写测试库）', '4 条记忆 + 2 条核心记忆');
}

/* --------------------------------------------------------------- 浏览器辅助 */

async function loginUi(page) {
  await page.goto(`${webBase}/`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"]').fill(USERNAME);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL('**/dashboard', { timeout: 20_000 });
}

async function openMemory(page, href = '/memory') {
  await page.goto(`${webBase}${href}`, { waitUntil: 'domcontentloaded' });
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-list"]').count()) > 0,
    '记忆页列表分段挂载',
  );
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-list-count"]').innerText()) !== '—',
    '记忆列表加载完成',
  );
}

function rows(page) {
  return page.locator('[data-testid="mem-row"]');
}

async function rowTexts(page) {
  return rows(page).allTextContents();
}

/** 视口截图（页面在 .page 内部滚动，fullPage 截不到下面的分段）。 */
async function shot(page, name) {
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file });
  return file;
}

/** 元素截图：给局部证据（危险区、列表、调参面板）。 */
async function shotElement(locator, name) {
  const file = path.join(SHOTS, `${name}.png`);
  await locator.screenshot({ path: file });
  return file;
}

function assertNoOverflow(width, where) {
  assert(
    width.scrollWidth <= width.innerWidth + 1,
    `${where} 出现横向溢出：scrollWidth=${width.scrollWidth} > innerWidth=${width.innerWidth}`,
  );
}

async function measureOverflow(page) {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    bodyScrollWidth: document.body.scrollWidth,
  }));
}

/* ------------------------------------------------------ 触控目标全量审计 */

const MIN_TOUCH = 44;
const INTERACTIVE_SELECTOR = 'button, a, input, select, textarea, summary, [role="button"]';

/**
 * 遍历记忆页根节点（`.page.mem`）里的**所有**可交互元素，逐个断言
 * `getBoundingClientRect()` 宽高 ≥44。
 *
 * 两点口径：
 * - 复选框/单选框的原生框只有 20×20，但真正的可点击区域是包着它的
 *   `<label>`（点整行都能勾选）——所以按 label 量，同时把原生框尺寸一起报出来。
 * - 隐藏 tabpanel（`hidden`）里的内容不算「页面上的可交互元素」，跳过。
 *
 * 失败时打印 tag / class / type / 尺寸 / 文本，方便直接定位 CSS 漏了谁。
 */
async function auditTouchTargets(page, label) {
  const report = await page.evaluate((selector) => {
    const root = document.querySelector('.page.mem');
    if (!root) return { missingRoot: true, items: [] };
    const items = [];
    for (const el of root.querySelectorAll(selector)) {
      if (el.closest('[hidden]')) continue;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const type = (el.getAttribute('type') || '').toLowerCase();
      let target = el;
      let measured = 'self';
      if (el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio')) {
        const wrapper = el.closest('label');
        if (wrapper) {
          target = wrapper;
          measured = 'label';
        }
      }
      const rect = target.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      const own = el.getBoundingClientRect();
      items.push({
        tag: el.tagName.toLowerCase(),
        type,
        className: typeof el.className === 'string' ? el.className : '',
        text: ((el.textContent || el.value || '').trim() || el.getAttribute('aria-label') || '').slice(0, 40),
        width: rect.width,
        height: rect.height,
        measured,
        ownWidth: own.width,
        ownHeight: own.height,
      });
    }
    return { missingRoot: false, items };
  }, INTERACTIVE_SELECTOR);

  assert(!report.missingRoot, `${label}：找不到记忆页根节点 .page.mem`);
  // 内容页默认 20+ 个、调参 tab 9 个；这里只防「选择器选空了」的假绿。
  assert(report.items.length >= 6, `${label}：只找到 ${report.items.length} 个可交互元素，审计范围不对`);

  const bad = report.items.filter(
    (item) => item.width < MIN_TOUCH - 0.01 || item.height < MIN_TOUCH - 0.01,
  );
  if (bad.length > 0) {
    const lines = bad
      .map(
        (item) =>
          `  - <${item.tag}${item.type ? ` type="${item.type}"` : ''} class="${item.className}"> ` +
          `${item.width.toFixed(1)}×${item.height.toFixed(1)}「${item.text}」` +
          (item.measured === 'label'
            ? `（原生框 ${item.ownWidth.toFixed(1)}×${item.ownHeight.toFixed(1)}，按 label 计）`
            : ''),
      )
      .join('\n');
    throw new Error(
      `${label}：${bad.length}/${report.items.length} 个可交互元素小于 ${MIN_TOUCH}×${MIN_TOUCH}\n${lines}`,
    );
  }
  ok(`${label}：${report.items.length} 个可交互元素全部 ≥${MIN_TOUCH}×${MIN_TOUCH}`);
}

/** 修前/修后的关键尺寸证据（只用于 375px 的报告行）。 */
async function keyTouchSizes(page) {
  return page.evaluate(() => {
    const pick = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return `${selector}=missing`;
      const rect = el.getBoundingClientRect();
      return `${rect.width.toFixed(1)}×${rect.height.toFixed(1)}`;
    };
    return {
      summary: pick('.mem-item__detail summary'),
      ghost: pick('.mem-foot .ghost-button'),
      coreInput: pick('.mem-core-form input[type="text"]'),
      coreSelect: pick('.mem-core-form select'),
      filterSelect: pick('.mem-filters .mem-field select'),
    };
  });
}

/* ------------------------------------------------------ 末行字符数测量 */

const MIN_TAIL = 4;

/**
 * 要量末行的块（QA_TASK119 的 Low-1 口径：末行不得只剩 1–3 字）。
 * 每个块在页面上可能有多个实例（4 条筛选说明、7 条正文…），全部逐个量。
 */
const TAIL_BLOCKS = {
  content: [
    { block: '诚实边界 .mem__boundary', selector: '.mem__boundary' },
    { block: '筛选说明 .mem-filters__note', selector: '.mem-filters__note' },
    { block: '记忆正文 .mem-item__text', selector: '.mem-item__text' },
    { block: '页脚 .mem-foot', selector: '.mem-foot' },
  ],
  settings: [
    { block: '调参 tab .mem-tab', selector: '.mem-tab', includeButtons: true },
    { block: '调参说明 .mem-policy-status p', selector: '.mem-policy-status p' },
    { block: '调参列表项 .mem-policy-status li', selector: '.mem-policy-status li' },
  ],
};

/**
 * 逐字符测量末行：对每个可见文本节点里的每个非空白字符取一个 `Range` 矩形，
 * 按 `top` 聚成视觉行（同一行同一字号 ⇒ 同一 top），最后一行的字符数就是尾行长度。
 *
 * 只统计**渲染出来的文字**：不加省略号、不做 JS 截断，量到的就是用户看到的。
 * `button`/`a` 里的文字默认排除（页脚按钮不应算进正文末行），tab 例外。
 * 同时回读 font-size / line-height / text-wrap / 宽度，写进报告当证据。
 */
async function measureTails(page, specs) {
  return page.evaluate((blocks) => {
    const isHanzi = (ch) => /[\u3400-\u4dbf\u4e00-\u9fff]/.test(ch);
    const measure = (el, includeButtons) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!includeButtons && parent && parent.closest('button, a')) return NodeFilter.FILTER_REJECT;
          if (parent && parent.closest('script, style, [hidden]')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      const chars = [];
      let node = walker.nextNode();
      while (node) {
        const text = node.nodeValue;
        for (let index = 0; index < text.length; index += 1) {
          if (/\s/.test(text[index])) continue;
          const range = document.createRange();
          range.setStart(node, index);
          range.setEnd(node, index + 1);
          const rect = range.getBoundingClientRect();
          if (rect.width === 0 && rect.height === 0) continue;
          chars.push({ ch: text[index], top: rect.top, left: rect.left });
        }
        node = walker.nextNode();
      }
      chars.sort((left, right) => left.top - right.top || left.left - right.left);
      const lines = [];
      for (const char of chars) {
        const line = lines.find((candidate) => Math.abs(candidate.top - char.top) <= 2);
        if (line) line.chars.push(char.ch);
        else lines.push({ top: char.top, chars: [char.ch] });
      }
      const tail = lines.length > 0 ? lines[lines.length - 1].chars : [];
      const style = getComputedStyle(el);
      return {
        lines: lines.length,
        total: chars.length,
        tailChars: tail.length,
        tailHanzi: tail.filter(isHanzi).length,
        tailText: tail.join(''),
        fontSize: style.fontSize,
        lineHeight: style.lineHeight,
        textWrap: style.textWrap,
        width: Math.round(el.getBoundingClientRect().width * 10) / 10,
        text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 70),
      };
    };

    const out = [];
    for (const spec of blocks) {
      document.querySelectorAll(spec.selector).forEach((el, index) => {
        if (el.closest('[hidden]')) return;
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        out.push({ block: spec.block, index, ...measure(el, Boolean(spec.includeButtons)) });
      });
    }
    return out;
  }, specs);
}

/**
 * 断言一组块的末行都 ≥4 字符。
 *
 * 例外：`E2E长文本：…` 是**人为构造的无空格长串**（60 个 a + 60 个 x，靠
 * `overflow-wrap: anywhere` 硬断），不是中文排版场景；它的尾行长度由 ASCII
 * 字宽决定，量出来只作参考，不参与断言（正文的自然语言三档：短 / 中 / 98 字）。
 */
function assertTails(entries, width, where) {
  assert(entries.length > 0, `${width}px ${where}：没有测到任何文本块`);
  const natural = entries.filter(
    (entry) => !(entry.block === '记忆正文 .mem-item__text' && entry.text.startsWith('E2E长文本')),
  );
  assert(natural.length > 0, `${width}px ${where}：自然语言文本块为空`);
  const bad = natural.filter((entry) => entry.tailChars < MIN_TAIL);
  const describe = (entry) =>
    `${entry.block}#${entry.index} 末行 ${entry.tailChars} 字符「${entry.tailText}」` +
    `（${entry.lines} 行 / ${entry.total} 字符 / ${entry.fontSize} / ${entry.textWrap} / 宽 ${entry.width}）`;
  if (bad.length > 0) {
    throw new Error(
      `${width}px ${where}：${bad.length}/${natural.length} 个文本块末行 < ${MIN_TAIL} 字符（孤字行）：\n` +
        bad.map((entry) => `  - ${describe(entry)}`).join('\n'),
    );
  }
  const min = natural.reduce((left, right) => (left.tailChars <= right.tailChars ? left : right));
  const long98 = natural.find((entry) => entry.block === '记忆正文 .mem-item__text' && entry.total === 98);
  ok(
    `${width}px ${where}：${natural.length} 个文本块末行全部 ≥${MIN_TAIL} 字符`,
    `最短=${min.tailChars}「${min.tailText}」(${min.block})` +
      (long98 ? ` · 98 字长正文 ${long98.lines} 行，末行 ${long98.tailChars} 字符「${long98.tailText}」` : ''),
  );
  return { entries, natural, min, long98: long98 ?? null };
}

/** 把测量结果落盘，报告直接引用这两个 JSON（`<输出目录>/tail-<宽>-<视图>.json`）。 */
function writeTailEvidence(width, view, entries) {
  const file = path.join(SHOTS, `tail-${width}-${view}.json`);
  fs.writeFileSync(
    file,
    `${JSON.stringify({ width, view, minTail: MIN_TAIL, measuredAt: new Date().toISOString(), entries }, null, 2)}\n`,
  );
  return file;
}

/**
 * 扫「可达范围」：往真实列表里临时插一个不可见的 `.mem-item`，用同样的
 * selectors/字体量不同长度（4…N 个汉字）的正文末行，找出
 * - 每行容量 perLine（= 容器宽 / 单字宽）；
 * - `balance` 还能生效的最大长度（约 6 行）；
 * - 这个范围内末行的最小值；
 * - 第一个末行 <4 字符的长度（物理极限从哪开始）。
 *
 * 断言只压「4 … 6×perLine」这段可达范围；更长的正文 Chromium 会跳过 balance，
 * 纯 CSS 无法在不截断/不省略的前提下保证尾行长度（报告里如实写明）。
 * 插入的探针 `visibility:hidden`（保留布局、不影响截图），量完立即移除。
 */
async function auditTailRange(page, width) {
  const data = await page.evaluate((minTail) => {
    const host = document.querySelector('.mem-list');
    if (!host) return { error: '找不到 .mem-list，无法做尾行范围扫描' };
    const item = document.createElement('li');
    item.className = 'mem-item';
    item.style.visibility = 'hidden';
    const probe = document.createElement('p');
    probe.className = 'mem-item__text';
    item.appendChild(probe);
    host.appendChild(item);

    const measure = () => {
      const node = probe.firstChild;
      if (!node) return { lines: 0, tailChars: 0 };
      const chars = [];
      for (let index = 0; index < node.nodeValue.length; index += 1) {
        const range = document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + 1);
        const rect = range.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        chars.push({ top: rect.top, left: rect.left, width: rect.width, ch: node.nodeValue[index] });
      }
      chars.sort((left, right) => left.top - right.top || left.left - right.left);
      const lines = [];
      for (const char of chars) {
        const line = lines.find((candidate) => Math.abs(candidate.top - char.top) <= 2);
        if (line) line.chars.push(char.ch);
        else lines.push({ top: char.top, chars: [char.ch] });
      }
      return {
        lines: lines.length,
        tailChars: lines.length > 0 ? lines[lines.length - 1].chars.length : 0,
        charWidth: chars.length > 0 ? chars[0].width : 0,
      };
    };

    probe.textContent = '她';
    const single = measure();
    const contentWidth = probe.getBoundingClientRect().width;
    const perLine = single.charWidth > 0 ? Math.floor((contentWidth + 0.5) / single.charWidth) : 0;
    const sixLineLimit = perLine * 6;
    const fontSize = getComputedStyle(probe).fontSize;

    const scan = [];
    const failed = [];
    for (let length = minTail; length <= sixLineLimit + 24; length += 1) {
      probe.textContent = '她'.repeat(length);
      const stats = measure();
      scan.push({ length, lines: stats.lines, tailChars: stats.tailChars });
      if (stats.tailChars < minTail) failed.push({ length, lines: stats.lines, tailChars: stats.tailChars });
    }
    item.remove();

    const inRange = scan.filter((entry) => entry.length <= sixLineLimit);
    const minInRange = inRange.reduce((left, right) => (left.tailChars <= right.tailChars ? left : right));
    const firstFailure = failed.length > 0 ? failed[0] : null;
    return {
      contentWidth: Math.round(contentWidth * 10) / 10,
      charWidth: Math.round(single.charWidth * 100) / 100,
      fontSize,
      perLine,
      sixLineLimit,
      checked: inRange.length,
      minInRange,
      firstFailure,
      failures: failed.slice(0, 12),
      scan,
    };
  }, MIN_TAIL);

  assert(!data.error, `${width}px 尾行范围扫描失败：${data.error}`);
  const file = path.join(SHOTS, `tail-range-${width}.json`);
  fs.writeFileSync(file, `${JSON.stringify({ width, minTail: MIN_TAIL, ...data }, null, 2)}\n`);

  const within = data.scan.filter((entry) => entry.length <= data.sixLineLimit);
  assert(
    within.every((entry) => entry.tailChars >= MIN_TAIL),
    `${width}px 可达范围（4–${data.sixLineLimit} 字）内仍有末行 <${MIN_TAIL} 字符：` +
      `${within.filter((entry) => entry.tailChars < MIN_TAIL).slice(0, 5)
        .map((entry) => `${entry.length} 字→末行 ${entry.tailChars}`)
        .join('，')}`,
  );
  ok(
    `${width}px 尾行可达范围：4–${data.sixLineLimit} 字（每行 ${data.perLine} 字）内 ${within.length} 个长度末行全部 ≥${MIN_TAIL} 字符`,
    `实测最小=${data.minInRange.tailChars}（${data.minInRange.length} 字，${data.minInRange.lines} 行）· ` +
      `物理极限从 ${data.firstFailure ? `${data.firstFailure.length} 字（末行 ${data.firstFailure.tailChars} 字符）` : '未出现'} 开始`,
  );
}

/* --------------------------------------------------------------- 主流程 */

let browser = null;
let exitCode = 0;

/**
 * 收尾：关浏览器 → kill 服务进程组 → lsof 复查端口 → **只删本次自建的库**。
 * 正常结束、断言失败、Ctrl-C/SIGTERM 都走这里（幂等）。
 */
const cleanupState = { done: false, portsClean: true, dbDropped: false, dbError: null };
async function cleanupResources(reason) {
  if (cleanupState.done) return cleanupState;
  cleanupState.done = true;
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
  } else {
    // 复用外部 API/前端模式：本脚本没建过库，也就没有任何库要删。
    cleanupState.dbDropped = true;
  }
  return cleanupState;
}

onTermination(async (signal) => {
  console.log(`\n收到 ${signal}：开始收尾（kill 服务、删本次自建的库）`);
  await cleanupResources(signal);
});

try {
  if (apiBase && webBase) {
    const apiReady = await httpJson(`${apiBase}/healthz`).catch(() => ({ status: 0 }));
    assert(apiReady.status === 200, `API_BASE 不可用：${apiBase}`);
    ok('复用已给出的 API / 前端', `${apiBase} ← ${webBase}`);
  } else {
    // 只给一个也不行：那样没法同时验证「经代理的前端」与「直连 API」的一致。
    assert(
      !apiBase && !webBase,
      'BASE_URL 与 API_BASE 必须同时给出，或都不给（由脚本自己起服务）',
    );
    createTestDatabase();
    await bootApi();
    await bootWeb();
  }

  await loginApi();
  await seedData();

  browser = await chromium.launch({ executablePath: EXECUTABLE });

  /* ------------------------------- 桌面 1440×900 ------------------------------ */
  const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await desktop.newPage();
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    // 未登录时 App 会探一次 /v1/auth/me（401）——登录前的预期行为，不是缺陷。
    if (text.includes('401') || text.includes('/v1/auth/me')) return;
    if (text.includes('favicon')) return;
    consoleErrors.push(text);
  });

  await loginUi(page);
  await openMemory(page);

  const desktopRows = await rowTexts(page);
  assert(desktopRows.length === 4, `默认列表应有 4 条，实际 ${desktopRows.length}：${desktopRows.join(' | ')}`);
  for (const text of [SEED.cite, SEED.cat, SEED.walk, SEED.procedure]) {
    assert(desktopRows.some((row) => row.includes(text)), `列表缺少种子记忆：${text}`);
  }
  const firstRowText = desktopRows[0];
  assert(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(firstRowText), `没有绝对时间：${firstRowText}`);
  assert(firstRowText.includes('来源：你说的'), '没有显示 origin 中文');
  assert(firstRowText.includes('置信度 80%'), '没有显示置信度');
  const coreCount = await page.locator('[data-testid="mem-core-count"]').innerText();
  assert(coreCount.includes('2 / 20'), `核心记忆计数不对：${coreCount}`);
  const coreText = await page.locator('[data-testid="mem-core"]').innerText();
  assert(coreText.includes('E2E称呼') && coreText.includes('叫我小林'), '核心记忆没有渲染种子值');
  const listSectionText = await page.locator('[data-testid="mem-list"]').innerText();
  assert(listSectionText.includes('本次最多显示 50 条'), '缺少「最多显示 N 条」的分页边界说明');
  assert(!(await page.locator('body').innerText()).includes('下一页'), '出现了假分页控件');
  ok('桌面：真实数据渲染（列表 4 条 + 核心记忆 2 条）');
  const desktopShot = await shot(page, 'memory-desktop');
  const coreShot = await shotElement(page.locator('[data-testid="mem-core"]'), 'memory-core');
  const listShot = await shotElement(page.locator('[data-testid="mem-list"]'), 'memory-list');

  /* --------------------------- 核心记忆：软删除二次确认 --------------------- */
  const coreRow = page.locator('[data-testid="mem-core-list"] li', { hasText: 'E2E边界' });
  assert((await coreRow.count()) === 1, '找不到要停用的核心记忆');
  await coreRow.getByRole('button', { name: '停用…' }).click();
  const deletePanel = coreRow.getByRole('group', { name: '停用核心记忆 E2E边界' });
  await deletePanel.waitFor({ state: 'visible' });
  const deleteText = await deletePanel.innerText();
  assert(deleteText.includes('软删除'), '停用确认没有说明是软删除');
  assert(deleteText.includes('页面没有恢复入口'), '停用确认没有说明没有恢复入口');
  const deleteShot = await shotElement(coreRow, 'memory-core-delete-confirm');
  await deletePanel.getByRole('button', { name: '取消' }).click();
  await deletePanel.waitFor({ state: 'detached' });
  assert((await coreRow.count()) === 1, '取消后核心记忆不应消失');
  await coreRow.getByRole('button', { name: '停用…' }).click();
  await coreRow.getByRole('group', { name: '停用核心记忆 E2E边界' }).getByRole('button', { name: '确认停用' }).click();
  await coreRow.waitFor({ state: 'detached', timeout: 20_000 });
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-core-count"]').innerText()).includes('1 / 20'),
    '停用后核心记忆计数变成 1 / 20',
  );
  const coreAfterDelete = await apiJson('/v1/core-memory');
  assert(coreAfterDelete.items.length === 1, '停用核心记忆后 API 仍返回 2 条');
  ok('桌面：核心记忆停用需二次确认（取消不生效，确认后软删除）');

  /* ------------------------------- 撤回一条 --------------------------------- */
  const catRow = page.locator('[data-testid="mem-row"]', { hasText: SEED.cat });
  assert((await catRow.count()) === 1, '找不到要撤回的「团子」条目');
  await catRow.getByRole('button', { name: '撤回…' }).click();
  const retractPanel = catRow.getByRole('group', { name: '撤回这条记忆' });
  await retractPanel.waitFor({ state: 'visible' });
  const retractConfirm = retractPanel.getByRole('button', { name: '确认撤回' });
  assert(await retractConfirm.isDisabled(), '没填理由时「确认撤回」必须禁用');
  const retractShot = await shotElement(catRow, 'memory-retract-panel');
  await retractPanel.locator('textarea').fill('E2E 撤回测试');
  assert(!(await retractConfirm.isDisabled()), '填了理由后「确认撤回」应可用');
  await retractConfirm.click();

  await catRow.waitFor({ state: 'detached', timeout: 20_000 });
  await waitUntil(
    async () => (await rowTexts(page)).length === 3,
    '撤回后默认列表应为 3 条',
  );
  await waitUntil(
    async () => (await page.locator('.banner--ok').innerText()).includes('已撤回'),
    '撤回成功提示',
  );
  const afterRetract = await page.locator('[data-testid="mem-list"]').innerText();
  assert(!afterRetract.includes('已撤回：E2E 撤回测试'), '墓碑不应出现在默认列表里');
  ok('桌面：撤回一条后默认列表少一条（4 → 3），墓碑默认不显示');

  const listAfterRetract = await apiJson('/v1/memory');
  assert(listAfterRetract.memories.length === 3, 'API 默认列表没有同步变成 3 条');
  assert(listAfterRetract.total === 3, 'API total 没有同步变成 3');

  /* --------------------------- include=all 看墓碑 --------------------------- */
  await page.getByLabel('显示已撤回（include=retracted）').check();
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-include-summary"]').innerText()).includes('include=retracted'),
    'include=retracted 生效',
  );
  await waitUntil(
    async () => (await rowTexts(page)).some((row) => row.includes(SEED.cat)),
    'include=retracted 后能看到撤回的条目',
  );
  const tombstoneText = (await rowTexts(page)).find((row) => row.includes(SEED.cat)) || '';
  assert(tombstoneText.includes('已撤回：E2E 撤回测试'), `墓碑没有显示撤回理由：${tombstoneText}`);
  assert(tombstoneText.includes('墓碑：只读'), '墓碑没有标只读');
  ok('桌面：include=retracted 取回墓碑并显示理由');

  await page.getByLabel('显示已归档（include=archived）').check();
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-include-summary"]').innerText()).includes('include=all'),
    'include=all 生效（两个开关都勾上）',
  );
  const tombstoneShot = await shotElement(page.locator('[data-testid="mem-list"]'), 'memory-tombstones');
  const includeAll = await apiJson('/v1/memory?include=all');
  assert(includeAll.memories.length >= 4, 'include=all 应包含默认集合之外的墓碑');
  ok('桌面：两个墓碑开关都勾选 → include=all');

  /* ------------------------------- 纠正一条 --------------------------------- */
  const procedureRow = page.locator('[data-testid="mem-row"]', { hasText: SEED.procedure });
  assert((await procedureRow.count()) === 1, '找不到要纠正的条目');
  await procedureRow.getByRole('button', { name: '纠正…' }).click();
  const supersedePanel = procedureRow.getByRole('group', { name: '纠正这条记忆' });
  await supersedePanel.waitFor({ state: 'visible' });
  await supersedePanel.locator('textarea').fill(SEED.corrected);
  await supersedePanel.getByRole('button', { name: '确认纠正' }).click();

  await waitUntil(
    async () => (await rowTexts(page)).some((row) => row.includes(SEED.corrected)),
    '纠正后的新条目出现在列表里',
  );
  await waitUntil(
    async () => (await page.locator('.banner--ok').innerText()).includes('已纠正'),
    '纠正成功提示',
  );
  const oldProcedure = page.locator('[data-testid="mem-row"]', { hasText: SEED.procedure });
  await waitUntil(async () => (await oldProcedure.count()) === 1, '旧条目仍在（include=all）');
  const oldText = await oldProcedure.innerText();
  assert(oldText.includes('已被纠正'), `旧条目没有标「已被纠正」：${oldText}`);
  ok('桌面：纠正（supersede）→ 旧条目标「已被纠正」，新条目出现');

  // 回到默认视图：旧条目（已撤回）应当消失，新条目仍在
  await page.getByLabel('显示已撤回（include=retracted）').uncheck();
  await page.getByLabel('显示已归档（include=archived）').uncheck();
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-include-summary"]').innerText()).includes('未传 include'),
    '默认 include（不传）生效',
  );
  const defaultAfterSupersede = await rowTexts(page);
  assert(defaultAfterSupersede.length === 3, `默认视图应为 3 条，实际 ${defaultAfterSupersede.length}`);
  assert(!defaultAfterSupersede.some((row) => row.includes(SEED.procedure)), '被纠正的旧条目不应在默认视图里');
  assert(defaultAfterSupersede.some((row) => row.includes(SEED.corrected)), '纠正后的新条目应在默认视图里');
  ok('桌面：默认视图只剩现行记忆（纠正后的新条目）');

  /* ------------------------------- 危险区 ---------------------------------- */
  await page.getByRole('button', { name: '遗忘全部记忆…' }).click();
  const forgetPanel = page.getByRole('group', { name: '二次确认：遗忘全部记忆' });
  await forgetPanel.waitFor({ state: 'visible' });
  const forgetConfirm = page.getByRole('button', { name: '确认遗忘全部' });
  assert(await forgetConfirm.isDisabled(), '未勾选确认时「确认遗忘全部」必须禁用');
  assert((await page.locator('body').innerText()).includes('不可撤销'), '危险区没有明确「不可撤销」');
  await page.getByLabel('我明白这会清空当前账号可见的记忆，且无法撤销').check();
  assert(!(await forgetConfirm.isDisabled()), '勾选后「确认遗忘全部」应可用');
  const forgetShot = await shotElement(page.locator('[data-testid="mem-danger"]'), 'memory-forget-confirm');
  await forgetConfirm.click();

  const result = page.locator('[data-testid="mem-forget-result"]');
  await result.waitFor({ state: 'visible', timeout: 20_000 });
  const affected = Number((await page.locator('[data-testid="mem-forget-count"]').innerText()).trim());
  assert(Number.isFinite(affected) && affected > 0, `影响行数应为正数，实际 ${affected}`);
  const resultText = await result.innerText();
  assert(resultText.includes('四类受影响行数之和'), '没有说明影响行数的口径');
  assert(resultText.includes('无法撤销'), '结果里没有重申无法撤销');

  await waitUntil(
    async () => (await page.locator('[data-testid="mem-list-empty"]').count()) === 1,
    '遗忘后默认列表为空态',
  );
  await waitUntil(
    async () => (await page.locator('[data-testid="mem-core-empty"]').count()) === 1,
    '遗忘后核心记忆为空态',
  );
  assert((await rows(page).count()) === 0, '遗忘后列表仍有条目');

  const apiAfterForget = await apiJson('/v1/memory');
  assert(apiAfterForget.memories.length === 0, 'API 泄漏了遗忘后的记忆');
  assert(apiAfterForget.total === 0, 'API total 不为 0');
  const tombstones = await apiJson('/v1/memory?include=all');
  assert(tombstones.memories.length > 0, 'include=all 应仍能看到墓碑（软清除，不物理删除）');
  assert(
    tombstones.memories.every((row) => row.retracted || row.archived_at),
    'include=all 里出现了仍现行的条目（遗忘不彻底）',
  );
  const coreAfterForget = await apiJson('/v1/core-memory');
  assert(coreAfterForget.items.length === 0, '遗忘没有停用核心记忆');
  const emptyShot = await shotElement(page.locator('[data-testid="mem-list"]'), 'memory-list-empty');
  ok('桌面：forget 二次确认 → 清空列表与核心记忆，影响行数可见，墓碑仍在', `affected=${affected}`);
  const doneShot = await shotElement(page.locator('[data-testid="mem-danger"]'), 'memory-forget-done');

  /* ------------------------------- 调参 tab --------------------------------- */
  await page.goto(`${webBase}/memory?tab=settings`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="mem-policy-status"]').waitFor({ state: 'visible' });
  await waitUntil(
    async () => (await page.locator('body').innerText()).includes('召回相关性下限'),
    '调参表单加载完成',
  );
  const settingsText = await page.locator('body').innerText();
  // 生效状态已接线（另一任务已把 memory 段接进运行时）：断言真实文案，不是旧期望
  assert(settingsText.includes('生效状态：已接线'), '调参 tab 没有标注「已接线」的真实生效状态');
  assert(!settingsText.includes('未接线'), '调参文案仍写着旧的「未接线」');
  assert(!settingsText.includes('接线由另一个任务修复中'), '调参文案仍写着「接线由另一个任务修复中」');
  // 两点例外必须写在页面上：rerank 未配模型不生效、保留期是硬上限
  assert(settingsText.includes('LING_RERANK'), '没有写清 rerank_top_n 需要配置重排模型才生效');
  assert(settingsText.includes('没配时这一项不生效'), '没有写清没配重排模型时 rerank_top_n 不生效');
  assert(settingsText.includes('硬上限'), '没有写清 max_retention_days 是硬上限');
  assert(settingsText.includes('之后写入'), '没有写清保留期调大只影响之后写入的记忆');
  assert(settingsText.includes('relative_score_floor') && settingsText.includes('max_retention_days'), '四个参数键没有列出');
  const settingsShot = await shot(page, 'memory-settings');

  // 旧路径仍然开在调参 tab（Step 4 才做重定向）
  await page.goto(`${webBase}/settings/memory`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="mem-policy-status"]').waitFor({ state: 'visible' });
  ok('桌面：?tab=settings 深链与旧路径 /settings/memory 都开在调参 tab');

  // 切回内容 tab：URL 变成 /memory
  await page.locator('[data-testid="mem-tab-content"]').click();
  await page.waitForURL(`${webBase}/memory`, { timeout: 10_000 });
  await page.locator('[data-testid="mem-tab-content"]').waitFor({ state: 'visible' });

  // 调参 tab 里的「返回记忆内容」也必须回到内容 tab（不是设置中心）
  await page.locator('[data-testid="mem-tab-settings"]').click();
  await page.waitForURL(`${webBase}/memory?tab=settings`, { timeout: 10_000 });
  await page.getByRole('button', { name: '返回记忆内容' }).click();
  await page.waitForURL(`${webBase}/memory`, { timeout: 10_000 });
  ok('桌面：调参 tab ↔ 内容 tab 的 URL 同步（?tab= 查询参数，不是 hash）');

  /* ------------------------------- 为手机补数据 ------------------------------ */
  await createMemory('assertion', SEED.long);
  await createMemory('assertion', SEED.long98);
  await createMemory('assertion', SEED.medium);
  await createMemory('assertion', SEED.cite);
  await apiJson('/v1/core-memory', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ label: 'E2E称呼', text: '叫我小林', priority: 10, scope: 'private' }),
  });
  ok('手机验收前重新写入测试数据');

  /* ----------------------- 手机 375px / 320px 全量触控审计 ------------------- */

  /**
   * 一档宽度跑完整套：默认视图 → 展开详情 → 撤回面板 → 遗忘二次确认 →
   * 调参 tab。每一步都重新全量审计，因为不同状态会多出不同的控件
   * （textarea、checkbox、确认按钮、PolicyForm 的 input）。
   */
  async function auditMobileWidth(width) {
    const context = await browser.newContext({
      viewport: { width, height: 667 },
      isMobile: true,
      hasTouch: true,
      deviceScaleFactor: 2,
    });
    const page = await context.newPage();
    await loginUi(page);
    await openMemory(page);
    await waitUntil(async () => (await rowTexts(page)).length >= 2, `${width}px 列表渲染`);

    const overflow = await measureOverflow(page);
    assertNoOverflow(overflow, `${width}px 首屏`);
    assert(overflow.bodyScrollWidth <= overflow.innerWidth + 1, `${width}px body 出现横向溢出`);

    // 变异自检开关（默认关闭）：把触控目标压到 44px 以下，验证审计真的会红。
    // 用法：E2E_MUTATION=small-touch node tests/e2e/memory_browser.mjs → 必须以 exit 1 失败。
    if (process.env.E2E_MUTATION === 'small-touch') {
      await page.addStyleTag({
        content:
          '.page.mem button, .page.mem a, .page.mem select, .page.mem textarea, .page.mem summary,' +
          ' .page.mem input:not([type="checkbox"]):not([type="radio"]), .page.mem label {' +
          ' min-height: 0 !important; height: 20px !important; padding: 0 !important;' +
          ' line-height: 20px !important; }',
      });
    }

    await auditTouchTargets(page, `${width}px 默认视图`);
    if (width === 375) {
      const key = await keyTouchSizes(page);
      ok(
        '375px 关键触控尺寸',
        `summary=${key.summary} 重新读取全部=${key.ghost} 核心 input=${key.coreInput} ` +
          `核心 select=${key.coreSelect} 筛选 select=${key.filterSelect}`,
      );
    }
    // 末行字符数：默认视图（短 / 中 / 98 字长正文 + 诚实边界 + 筛选说明 + 页脚）
    const contentTails = await measureTails(page, TAIL_BLOCKS.content);
    writeTailEvidence(width, 'content', contentTails);
    assertTails(contentTails, width, '内容页');
    // 再把「还能保证到多长」量出来：断言只压可达范围，物理极限如实记录。
    await auditTailRange(page, width);

    const pageShot = await shot(page, `memory-mobile-${width}`);

    // 98 字长正文的单独证据图（Low-1 就是这条在 320 下末行只剩「人。」）
    const long98Row = page.locator('[data-testid="mem-row"]', { hasText: '空荡的湖岸线' });
    assert((await long98Row.count()) === 1, `${width}px 找不到 98 字长正文条目`);
    const long98Shot = await shotElement(long98Row, `memory-mobile-${width}-long98`);

    // 打开最长的条目详情：长 memory_id / 来源事件最容易撑破布局
    const longRow = page.locator('[data-testid="mem-row"]', { hasText: 'E2E长文本' });
    assert((await longRow.count()) === 1, `${width}px 找不到长文本条目`);
    await longRow.locator('summary').click();
    const expanded = await measureOverflow(page);
    assertNoOverflow(expanded, `${width}px 展开详情后`);
    await auditTouchTargets(page, `${width}px 展开详情后`);
    const detailShot = await shotElement(longRow, `memory-mobile-${width}-detail`);

    // 行内撤回面板：textarea 与两个按钮只在选中态出现，也要审计
    const retractRow = page.locator('[data-testid="mem-row"]', { hasText: SEED.cite });
    assert((await retractRow.count()) === 1, `${width}px 找不到「${SEED.cite}」条目`);
    await retractRow.getByRole('button', { name: '撤回…' }).click();
    await retractRow
      .getByRole('group', { name: '撤回这条记忆' })
      .waitFor({ state: 'visible' });
    await auditTouchTargets(page, `${width}px 撤回面板`);
    await retractRow.getByRole('button', { name: '取消' }).click();

    // 危险区二次确认：带 checkbox 的 label + 确认/取消按钮
    await page.getByRole('button', { name: '遗忘全部记忆…' }).click();
    await page
      .getByRole('group', { name: '二次确认：遗忘全部记忆' })
      .waitFor({ state: 'visible' });
    await auditTouchTargets(page, `${width}px 遗忘二次确认`);
    await page.getByRole('button', { name: '取消' }).click();

    // 底部「重新读取全部」在 .page 内部滚动区，视口截图截不到，单独拍元素
    const footShot = await shotElement(page.locator('.mem-foot'), `memory-mobile-${width}-foot`);

    // 调参 tab：PolicyForm 的 number input / 按钮 / 返回键同样要 ≥44
    await page.goto(`${webBase}/memory?tab=settings`, { waitUntil: 'domcontentloaded' });
    await page.locator('[data-testid="mem-policy-status"]').waitFor({ state: 'visible' });
    await waitUntil(
      async () => (await page.locator('body').innerText()).includes('召回相关性下限'),
      `${width}px 调参表单加载`,
    );
    // 上面那句文案来自静态的 mem-policy-status，表单本身还在等 GET /v1/settings/config。
    // 不等 number input 出现就审计，会只数到 3 个元素（tab×2 + 返回），误报失败。
    await waitUntil(
      async () => (await page.locator('[data-testid="mem-settings"] input[type="number"]').count()) >= 4,
      `${width}px 调参表单字段渲染`,
    );
    await auditTouchTargets(page, `${width}px 调参 tab`);

    // 末行字符数：调参 tab（tab 标题 + 生效状态说明 + 参数列表项）
    const settingsTails = await measureTails(page, TAIL_BLOCKS.settings);
    writeTailEvidence(width, 'settings', settingsTails);
    assertTails(settingsTails, width, '调参 tab');

    const settingsShot = await shot(page, `memory-settings-${width}`);

    await context.close();
    return { pageShot, long98Shot, detailShot, footShot, settingsShot };
  }

  const shots375 = await auditMobileWidth(375);
  ok('手机 375×667：单列、无横向溢出、所有可交互元素触控 ≥44×44');
  const shots320 = await auditMobileWidth(320);
  ok('手机 320×667：无横向溢出、所有可交互元素触控 ≥44×44');

  /* ------------- 部分失败（核心 403 / 500 / 网络）：文案按真实原因与可操作性 -------------
   *
   * 注意标注：这一组里的 403 / 500 都是用 `route.fulfill` 在浏览器里**伪造**的
   * 响应（用例名标 mock-403 / mock-500），只证明 UI 分支；后端是否真的会返回
   * 403/500 由下面的 real-403 / real-500 证明。
   */
  const partial = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const partialPage = await partial.newPage();
  await loginUi(partialPage);

  const sectionError = partialPage.locator('[data-testid="mem-section-error"]').first();
  const failCore = async (handler) => {
    await partialPage.unroute('**/v1/core-memory');
    await partialPage.route('**/v1/core-memory', handler);
    await partialPage.goto(`${webBase}/memory`, { waitUntil: 'domcontentloaded' });
    await sectionError.waitFor({ state: 'visible' });
    return partialPage.locator('body').innerText();
  };

  // 1) mock-403：账号级资源，不能写成「该会话」；重试无用，所以不给重试按钮
  //    （route.fulfill 伪造 403：证明 UI 分支，不证明后端行为）
  let partialText = await failCore((route) =>
    route.fulfill({
      status: 403,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        error: { code: 'forbidden', message: '没有权限读取核心记忆（E2E 模拟）', retriable: false },
      }),
    }),
  );
  await waitUntil(
    async () => (await rowTexts(partialPage)).length >= 1,
    '核心记忆 403 时列表仍渲染真实数据',
  );
  assert(partialText.includes('核心记忆读不到'), '核心记忆的错误没有显示出来');
  assert(!partialText.includes('dash-error'), '整页被一个错误盖住了');
  assert(
    partialText.includes('核心记忆读不到：当前账号没有访问权限'),
    `403 文案没有按账号粒度写：${partialText.slice(0, 400)}`,
  );
  assert(!partialText.includes('该会话'), '403 文案仍把账号级资源说成「该会话」');
  assert((await sectionError.getAttribute('data-error-code')) === 'forbidden', '403 的 code 没有透到界面');
  assert((await sectionError.getAttribute('data-retriable')) === 'false', '403 被标成了「可重试」');
  assert(
    (await sectionError.locator('button').count()) === 0,
    '403 仍然给了重试按钮：无权限重试无用，会把人引向死路',
  );
  assert(
    (await sectionError.locator('[data-testid="mem-section-no-retry"]').count()) === 1,
    '403 没有说明「重试不会改变结果」',
  );
  // 页脚不许无条件说「重试按钮就在那个分段里」：403 分段 0 个 button，这句是假的。
  assert(
    !partialText.includes('重试按钮就在那个分段里'),
    '403 态页脚仍说「重试按钮就在那个分段里」：403 分段 data-retriable=false、块内 0 个 button',
  );
  assert(
    partialText.includes('不提供重试按钮'),
    `403 态页脚没有说明「该分段不提供重试」：${partialText.slice(-260)}`,
  );
  assert(
    (await partialPage.locator('.mem-foot').getAttribute('data-foot-mode')) === 'non-retriable',
    '403 态页脚的 data-foot-mode 不是 non-retriable',
  );
  ok('mock-403：桌面 403 态页脚不再说「重试按钮就在那个分段里」（该分段 0 个重试按钮）');
  const partialShot = await shot(partialPage, 'memory-partial-failure');
  // 403 页脚单独留证（视口截图到不了页脚，.page 是内部滚动区）
  const forbiddenFootText = await partialPage.locator('.mem-foot').innerText();
  const forbiddenFootShot = await shotElement(partialPage.locator('.mem-foot'), 'memory-mock-403-foot');

  // 2) mock-500：可重试，且绝不能被说成权限问题（route.fulfill 伪造 500）
  partialText = await failCore((route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ error: { code: 'internal', message: '内部错误（E2E 模拟）', retriable: true } }),
    }),
  );
  assert(
    partialText.includes('核心记忆读不到：服务端内部错误'),
    `500 文案不对：${partialText.slice(0, 400)}`,
  );
  assert(partialText.includes('不是权限问题'), '500 没有说清不是权限问题');
  assert(!partialText.includes('没有访问权限'), '500 被说成了权限问题');
  assert((await sectionError.getAttribute('data-retriable')) === 'true', '500 没有被标成可重试');
  // 可重试时分段里确实有「重试」按钮，页脚这句话此时才成立。
  assert(
    partialText.includes('重试按钮就在那个分段里'),
    '500 可重试时页脚丢了「重试按钮就在那个分段里」的说明',
  );
  assert(
    (await partialPage.locator('.mem-foot').getAttribute('data-foot-mode')) === 'retriable',
    '500 态页脚的 data-foot-mode 不是 retriable',
  );
  assert(
    (await sectionError.locator('button').count()) === 1 &&
      (await sectionError.locator('button').innerText()).includes('重试'),
    '500 没有给重试按钮',
  );
  const retriableFootText = await partialPage.locator('.mem-foot').innerText();
  const retriableFootShot = await shotElement(partialPage.locator('.mem-foot'), 'memory-mock-500-foot');
  console.log(`403 页脚：「${forbiddenFootText.replace(/\s+/g, ' ').trim()}」`);
  console.log(`500 页脚：「${retriableFootText.replace(/\s+/g, ' ').trim()}」`);

  // 3) 网络失败：可重试，也不能混成权限问题
  partialText = await failCore((route) => route.abort('failed'));
  assert(
    partialText.includes('核心记忆读不到：无法连接服务端'),
    `网络失败文案不对：${partialText.slice(0, 400)}`,
  );
  assert(partialText.includes('请求没有到达后端'), '网络失败没有说清「没到后端」');
  assert((await sectionError.getAttribute('data-error-code')) === 'network_error', '网络失败 code 不对');
  assert(
    (await sectionError.locator('button').count()) === 1 &&
      (await sectionError.locator('button').innerText()).includes('重试'),
    '网络失败没有给重试按钮',
  );

  ok('mock-403/mock-500：核心记忆 403 时列表照常显示，三类文案可区分（403 不给重试）');

  // 4) mock-403 + detail=must_change_password：账号闸分支必须给可执行的指路。
  //    该分支的真实后端路径见后面的 real-403（SQL 置标记），这条只是 UI 分支。
  //    实测 403 态下 4 秒内 0 次记忆请求，也没有 focus/visibilitychange 重新取数，
  //    所以「本页会自动恢复」是不实陈述；改密入口也必须真可点（跳 /settings/account）。
  partialText = await failCore((route) =>
    route.fulfill({
      status: 403,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        error: {
          code: 'forbidden',
          message: '首次登录必须先修改密码',
          detail: 'must_change_password',
          retriable: false,
        },
      }),
    }),
  );
  assert(partialText.includes('首次登录还没改初始口令'), '账号闸 403 没有说明真实原因');
  assert(
    !partialText.includes('本页会自动恢复'),
    '403 仍承诺「本页会自动恢复」：实测 403 态不会自动重新取数',
  );
  assert(!partialText.includes('账号页'), '403 仍把改密入口写成不可点的「账号页」');
  assert(partialText.includes('设置 → 账号'), '403 没有指出改密入口');
  assert(
    partialText.includes('重新读取全部') && partialText.includes('或重新进入本页'),
    '403 没有给出可执行的恢复步骤（重新读取全部 / 重新进入本页）',
  );
  assert(
    !partialText.includes('重试按钮就在那个分段里'),
    '账号闸 403 态页脚仍说「重试按钮就在那个分段里」：该分段 0 个重试按钮',
  );
  const accountLink = sectionError.locator('a[href="/settings/account"]');
  assert((await accountLink.count()) === 1, '403 的「设置 → 账号」不是真链接');
  assert((await accountLink.innerText()).includes('设置 → 账号'), '改密链接的文字不对');
  const accountGateShot = await shotElement(sectionError, 'memory-account-gate');
  await accountLink.click();
  await partialPage.waitForURL('**/settings/account', { timeout: 10_000 });
  assert(
    (await partialPage.locator('body').innerText()).includes('账号'),
    '点改密链接没有到达账号页',
  );

  ok('mock-403：桌面账号闸分支给真链接与可执行步骤，不承诺自动恢复；点链接真能到账号页');
  await partial.close();

  // 4b) 同一个 mock-403 账号闸分支在 375 窄屏：链接必须真可点，且触控 ≥44×44
  const gateMobile = await browser.newContext({
    viewport: { width: 375, height: 667 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
  });
  const gatePage = await gateMobile.newPage();
  await loginUi(gatePage);
  await gatePage.route('**/v1/core-memory', (route) =>
    route.fulfill({
      status: 403,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({
        error: {
          code: 'forbidden',
          message: '首次登录必须先修改密码',
          detail: 'must_change_password',
          retriable: false,
        },
      }),
    }),
  );
  await gatePage.goto(`${webBase}/memory`, { waitUntil: 'domcontentloaded' });
  const gateSection = gatePage.locator('[data-testid="mem-section-error"]').first();
  await gateSection.waitFor({ state: 'visible' });
  const gateMobileText = await gatePage.locator('body').innerText();
  assert(!gateMobileText.includes('本页会自动恢复'), '375 的账号闸 403 仍承诺自动恢复');
  assert(
    !gateMobileText.includes('重试按钮就在那个分段里'),
    '375 的账号闸 403 页脚仍说「重试按钮就在那个分段里」（该分段 0 个重试按钮）',
  );
  const gateLink375 = gateSection.locator('a[href="/settings/account"]');
  assert((await gateLink375.count()) === 1, '375 的账号闸 403 没有可点链接');
  const gateBox = await gateLink375.boundingBox();
  assert(
    gateBox !== null && gateBox.width >= MIN_TOUCH && gateBox.height >= MIN_TOUCH,
    `375 改密链接触控 ${gateBox?.width?.toFixed(1)}×${gateBox?.height?.toFixed(1)}，小于 ${MIN_TOUCH}×${MIN_TOUCH}`,
  );
  const gateOverflow = await measureOverflow(gatePage);
  assertNoOverflow(gateOverflow, '375px 账号闸 403');
  const accountGateMobileShot = await shotElement(gateSection, 'memory-account-gate-375');
  await gateMobile.close();
  ok('mock-403：375 账号闸分支的改密链接真可点、触控 ≥44×44，且无横向溢出');

  /* ------------- 真 403 / 真 500：故障来自真后端，不是 route.fulfill -------------
   *
   * 上面那组 403 / 500 是 `route.fulfill` 在浏览器里伪造的响应：它只能证明
   * 「UI 拿到 403/500 时会怎么画」，证明不了「后端真的会返回 403/500」。
   * 这一组把两种故障真的造出来后端请求上，再断言同一套 UI 行为：
   * - real-500：把 `core_memories` 表临时改名 → 查询真的抛异常 → FastAPI 500；
   * - real-403：`UPDATE users SET must_change_password=true` → `deps.py` 的账号闸
   *   真的返回 403（唯一真实 403 来源），复位后页面能恢复。
   *
   * 两条都在 `finally` 里恢复现场（表名改回、标记复位），失败也不给下一条用例
   * 留脏状态。
   */
  const real = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const realPage = await real.newPage();
  await loginUi(realPage);
  await openMemory(realPage);
  await waitUntil(async () => (await rowTexts(realPage)).length >= 4, '真故障用例：列表先渲染 4 条');
  const realFoot = realPage.locator('[data-testid="mem-foot"]');
  const realSectionError = realPage.locator('[data-testid="mem-section-error"]');
  const clickReloadAll = () => realPage.getByRole('button', { name: '重新读取全部' }).click();

  // ---------------------------------------------------------------- real-500
  const HIDDEN_TABLE = 'core_memories__e2e_hidden';
  let coreRenamed = false;
  let real500Shot = null;
  try {
    execDb([`ALTER TABLE core_memories RENAME TO ${HIDDEN_TABLE}`]);
    coreRenamed = true;

    // 先证明「真后端真的 500」：直连 API，不经过浏览器。
    const raw500 = await apiFetch('/v1/core-memory');
    assert(raw500.status === 500, `real-500 没有出现：GET /v1/core-memory → HTTP ${raw500.status}`);
    const listStillOk = await apiFetch('/v1/memory');
    assert(listStillOk.status === 200, `real-500 连带打挂了 /v1/memory（HTTP ${listStillOk.status}）`);

    await clickReloadAll();
    await realSectionError.first().waitFor({ state: 'visible' });
    const real500Text = await realPage.locator('body').innerText();
    assert(
      real500Text.includes('核心记忆读不到：服务端内部错误'),
      `real-500 的文案不对：${real500Text.slice(0, 400)}`,
    );
    assert(real500Text.includes('不是权限问题'), 'real-500 没有说清「不是权限问题」');
    assert(!real500Text.includes('没有访问权限'), 'real-500 被说成了权限问题');
    assert(
      (await realSectionError.count()) === 1,
      `real-500 只该打挂核心记忆一个分段，实际 ${await realSectionError.count()} 个`,
    );
    assert((await realSectionError.getAttribute('data-error-code')) === 'internal', 'real-500 的 code 不是 internal');
    assert((await realSectionError.getAttribute('data-retriable')) === 'true', 'real-500 没有被标成可重试');
    assert(
      (await realSectionError.locator('button').count()) === 1 &&
        (await realSectionError.locator('button').innerText()).includes('重试'),
      'real-500 没有给重试按钮',
    );
    assert((await realFoot.getAttribute('data-foot-mode')) === 'retriable', 'real-500 页脚的 data-foot-mode 不是 retriable');
    assert(real500Text.includes('重试按钮就在那个分段里'), 'real-500 可重试时页脚丢了「重试按钮就在那个分段里」');
    // 列表没被连坐：4 条还在（真后端 500 只影响核心记忆那一个请求）。
    assert((await rowTexts(realPage)).length >= 4, 'real-500 把记忆列表也打空了');
    real500Shot = await shot(realPage, 'memory-real-500');
  } finally {
    if (coreRenamed) {
      execDb([`ALTER TABLE ${HIDDEN_TABLE} RENAME TO core_memories`]);
      coreRenamed = false;
    }
  }

  // 表改回来之后，点分段里的「重试」必须真的重新取到数据（不是只清错误）。
  await realSectionError.getByRole('button', { name: '重试' }).click();
  await waitUntil(
    async () => (await realPage.locator('[data-testid="mem-core-count"]').innerText()).includes('1 / 20'),
    'real-500 恢复后核心记忆重新渲染',
  );
  assert((await realSectionError.count()) === 0, 'real-500 恢复后仍然挂着错误分段');
  ok(
    'real-500：真后端 500（临时改名 core_memories，非 mock）→ 可重试、不说成权限问题；恢复后「重试」真重新取数',
  );

  // ---------------------------------------------------------------- real-403
  let gateSet = false;
  let real403Shot = null;
  let real403FootShot = null;
  try {
    execDb(['UPDATE users SET must_change_password = TRUE']);
    gateSet = true;

    // 真后端的证据：/v1/auth/me 报出标记，记忆端点被账号闸拦成 403（detail 指到改密）。
    const me = await apiJson('/v1/auth/me');
    assert(me?.user?.must_change_password === true, 'real-403：/v1/auth/me 没有反映 must_change_password');
    const raw403 = await apiFetch('/v1/core-memory');
    const raw403Body = await raw403.json();
    assert(raw403.status === 403, `real-403 没有出现：GET /v1/core-memory → HTTP ${raw403.status}`);
    assert(
      raw403Body?.error?.code === 'forbidden' && raw403Body?.error?.detail === 'must_change_password',
      `real-403 的错误形状不对：${JSON.stringify(raw403Body).slice(0, 300)}`,
    );

    await clickReloadAll();
    await realSectionError.first().waitFor({ state: 'visible' });
    const real403Text = await realPage.locator('body').innerText();
    assert(real403Text.includes('首次登录还没改初始口令'), 'real-403 没有说明真实原因（账号闸）');
    assert(!real403Text.includes('本页会自动恢复'), 'real-403 仍承诺「本页会自动恢复」');
    assert(
      !real403Text.includes('重试按钮就在那个分段里'),
      'real-403 页脚仍说「重试按钮就在那个分段里」：该分段 0 个重试按钮',
    );
    assert(real403Text.includes('不提供重试按钮'), 'real-403 页脚没有说明该分段不提供重试');
    assert((await realFoot.getAttribute('data-foot-mode')) === 'non-retriable', 'real-403 页脚的 data-foot-mode 不是 non-retriable');
    assert((await realSectionError.count()) === 2, `real-403 应同时打挂两个分段，实际 ${await realSectionError.count()} 个`);
    assert(
      (await realSectionError.locator('button').count()) === 0,
      'real-403 仍然给了重试按钮：账号状态不会因为再请求一次而改变',
    );
    assert((await realSectionError.first().getAttribute('data-error-code')) === 'forbidden', 'real-403 的 code 不是 forbidden');
    assert((await realSectionError.first().getAttribute('data-retriable')) === 'false', 'real-403 被标成了可重试');
    const gateLinks = realPage.locator('[data-testid="mem-section-error"] a[href="/settings/account"]');
    assert((await gateLinks.count()) === 2, `real-403 的改密链接不是两段各一个：${await gateLinks.count()}`);
    assert((await gateLinks.first().innerText()).includes('设置 → 账号'), 'real-403 的改密链接文字不对');
    real403Shot = await shot(realPage, 'memory-real-403');
    real403FootShot = await shotElement(realFoot, 'memory-real-403-foot');
  } finally {
    if (gateSet) {
      execDb(['UPDATE users SET must_change_password = FALSE']);
      gateSet = false;
    }
  }

  // 复位后点「重新读取全部」就能恢复：证明 403 只由账号闸引起，不是数据或页面坏了。
  await clickReloadAll();
  await waitUntil(async () => (await rowTexts(realPage)).length >= 4, 'real-403 复位后列表恢复');
  assert((await realSectionError.count()) === 0, 'real-403 复位后仍然挂着错误分段');
  assert(
    (await realPage.locator('[data-testid="mem-core-count"]').innerText()).includes('1 / 20'),
    'real-403 复位后核心记忆没有恢复',
  );
  ok(
    'real-403：真后端账号闸 403（SQL 置 must_change_password，非 mock）→ 不给重试、给可点改密链接；复位后恢复',
  );
  await real.close();

  await desktop.close();

  assert(consoleErrors.length === 0, `页面出现 JS/控制台错误：${consoleErrors.slice(0, 3).join(' | ')}`);
  ok('桌面与手机都没有未捕获的页面错误');

  console.log(
    `\n截图：\n  - ${desktopShot}\n  - ${coreShot}\n  - ${listShot}\n  - ${forgetShot}\n  - ${doneShot}\n  - ${settingsShot}\n  - ${shots375.pageShot}\n  - ${shots375.long98Shot}\n  - ${shots375.detailShot}\n  - ${shots375.footShot}\n  - ${shots375.settingsShot}\n  - ${shots320.pageShot}\n  - ${shots320.long98Shot}\n  - ${shots320.detailShot}\n  - ${shots320.footShot}\n  - ${shots320.settingsShot}\n  - ${deleteShot}\n  - ${retractShot}\n  - ${tombstoneShot}\n  - ${emptyShot}\n  - ${partialShot}\n  - ${forbiddenFootShot}\n  - ${retriableFootShot}\n  - ${accountGateShot}\n  - ${accountGateMobileShot}\n  - ${real500Shot}\n  - ${real403Shot}\n  - ${real403FootShot}`,
  );
} catch (error) {
  exitCode = 1;
  console.error(`\nFAIL  ${error?.message || error}`);
  console.error(error?.stack || '');
} finally {
  const state = await cleanupResources('end');
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
