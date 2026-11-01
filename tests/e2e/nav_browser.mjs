/**
 * 左导航外壳（IA Step 1）的浏览器实测：**真 API + 真 PostgreSQL + 真 Chromium**。
 *
 * 覆盖用户拍板的硬约束（DESIGN §1/§4，SHOTS_REPORT §5.1–§5.3/§5.7）：
 *
 * 桌面 1440×900
 *  1. 登录后落在 `/dashboard`，`/` 仍直达聊天；
 *  2. 六项导航都可达（总览/对话/记忆/人设/生活 + 设置），每一项都真的换页，
 *     且**同一时刻只有一个** `aria-current="page"`；
 *  3. `/settings/*` 高亮「设置」，`/s/:id` 高亮「对话」；
 *  4. 56px 图标 rail ↔ 200px 展开，`localStorage` 持久化，刷新后仍是上次那档；
 *  5. 侧栏与内容区不重叠、无横向滚动；
 *  6. 顶栏只剩 brand + 账号区（`quick-*` 已删、旧入口全在左导航）；
 *     1440 不显示聊天页那个死 `≡`，brand 也不再被挤右 46px。
 *
 * 窄屏 375×667
 *  7. 常驻「打开导航」按钮（`data-testid="nav-open"`、`aria-label="打开导航"`，
 *     **不是**「打开会话列表」）；
 *  8. 抽屉真的 portal 到 `<body>`：`.app-header` 有 `backdrop-filter`，会形成包含块，
 *     抽屉留在 header 里就只能盖住 header 那 56px。这里断言 scrim 的
 *     `getBoundingClientRect()` 就是整个视口、且抽屉是 body 的直接子节点；
 *  9. Escape 关、点 scrim 关、路由切换自动关、关闭后焦点回到「打开导航」；
 * 10. 焦点陷阱：Tab 循环留在抽屉里，不会跑到抽屉背后的正文；
 * 11. body 滚动锁：抽屉打开时 `body.overflow=hidden`，关掉即还原；
 * 12. 单一 overlay manager：导航抽屉与聊天页会话列表互斥（开一片必收另一片）；
 * 13. 聊天页原「打开会话列表」按钮与 `aria-label` 未变、仍可用（w0_browser.mjs 依赖）；
 * 14. 每页 `scrollWidth <= innerWidth`，且 `.app-header` / 侧栏 / 抽屉内
 *     **所有**可交互元素逐个断言 ≥44×44；
 * 15. 抽屉底部账号区（`nav-account`：用户名 + `nav-logout` 退出登录），
 *     且从抽屉退出登录真的回到登录页（窄屏不再只能走「设置→账号」）；
 * 16. 账号页的「退出登录」（`account-logout`）≥44×44。
 *
 * 隔离（硬约束）：
 * - 数据库是本地 PG 127.0.0.1:55432 上的**一次性库**：每次运行 `CREATE DATABASE`
 *   `ling_nav_e2e_<运行 id>`（UTC 时间戳 + pid + 随机），**只 DROP 本次自己建的
 *   那个库**；绝不 DROP 同名但非本次创建的库，也绝不碰 `ling` / `ling_test` /
 *   其它 e2e 库（先 CREATE、被占用就换名，不做「先 DROP 再 CREATE」）。
 *   `E2E_DB_NAME` 可指定库名；已存在时加后缀换名，不删它。
 * - 截图 / 日志默认写临时目录（`<tmp>/ling-e2e/nav-<运行 id>/`），**不默认写仓库**；
 *   只有显式 `SHOTS_DIR` 才写指定位置；脚本开头与结尾都打印输出目录。
 * - API 与前端都跑在随机高位端口（避开 8080 与 55432），`--strictPort` 起；
 *   结束时 kill 进程组并用 `lsof` 复查端口确实没人监听；
 * - API 的 `LING_DATA_DIR` 指向临时目录，不读仓库 `.data/`（那里有真实根密钥）。
 *
 * 用法：
 *   node tests/e2e/nav_browser.mjs
 *
 * 环境变量：CHROME_PATH（chromium headless shell）、SHOTS_DIR（默认临时目录）、
 * E2E_DB_NAME（默认自动生成）、LING_PGPORT（默认 55432）、
 * KEEP_SERVERS=1（调试时保留进程与测试库）、
 * NAV_E2E_ONLY=desktop|mobile（只跑一半，变异验证时省时间；默认两半都跑）。
 */
import { spawn, execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
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

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
const RUN_ID = makeRunId();
const SHOTS_INFO = resolveShotsDir('nav', RUN_ID);
/** 截图 / 日志目录：默认临时目录；只有 SHOTS_DIR 才写别处。 */
const SHOTS = SHOTS_INFO.dir;
const PG_PORT = Number(process.env.LING_PGPORT || 55432);
/** 本次运行创建的库名；只有它会被 DROP（边界见 lib/isolation.mjs）。 */
let DB_NAME = null;
const PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python');
const USERNAME = 'nav_e2e';
const PASSWORD = 'nav-e2e-password';
const FORBIDDEN_PORTS = new Set([8080, 55432]);

console.log(
  `截图 / 日志目录：${SHOTS}` +
    (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定）' : '（未设 SHOTS_DIR，默认写临时目录，不写仓库）'),
);

/* --------------------------------------------------------------- playwright */

async function loadChromium() {
  const fromWebConsole = createRequire(path.join(REPO_ROOT, 'clients', 'web-console', 'package.json'));
  try {
    const mod = fromWebConsole('playwright');
    return {
      chromium: mod.chromium,
      origin: `clients/web-console (playwright ${fromWebConsole('playwright/package.json')?.version})`,
    };
  } catch (error) {
    console.error(`配置错误：找不到 playwright（${error?.message}）`);
    console.error('修复：cd clients/web-console && pnpm install');
    process.exit(2);
  }
}

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
  const logPath = options.logPath;
  const fd = fs.openSync(logPath, 'a');
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env,
    detached: true,
    stdio: ['ignore', fd, fd],
  });
  fs.closeSync(fd);
  child.__label = label;
  child.__logPath = logPath;
  child.on('exit', (code, signal) => {
    // 正常退出：0；被我们自己 SIGTERM 收掉的 143 也算正常。
    // 以前只放过 0，于是每次干净收尾都会打印一行「进程退出 code=143」，
    // 看起来像起服务失败，实际什么都没坏。
    const normal = code === null || code === 0 || code === 143;
    if (!normal) {
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

/* ------------------------------------------------------------- 数据库隔离 */

/** 先 CREATE、被占用就换名；只删本次自己建的库（见 lib/isolation.mjs）。 */
function createTestDatabase() {
  try {
    const created = createIsolatedDatabase({
      tag: 'nav',
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
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ling-nav-e2e-'));
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
    LING_COOKIE_NAME: 'nav_e2e_sid',
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

/* ------------------------------------------------------------- 页面小工具 */

async function shot(page, name) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function loginUi(page) {
  await page.goto(`${webBase}/`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"]').fill(USERNAME);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL('**/dashboard', { timeout: 20_000 });
  // 等外壳就绪（页头 + 主区），**不**等某个具体导航入口——入口缺失时应该由
  // 下面针对性的断言报「缺 nav-open」，而不是在这里变成一句 waitForSelector 超时。
  await page.waitForSelector('header.app-header', { timeout: 20_000 });
  await page.waitForSelector('main.app-main', { timeout: 20_000 });
  await page.waitForTimeout(200);
}

/**
 * 桌面 rail 里的导航项。
 *
 * 作用域写成「`.sidenav` 里的 `aria-label="主导航"` landmark」而不是直接
 * `[data-testid=...]`：testid 是实现细节，同名 testid 可能出现在别处
 * （Step 1 的 brand 就是 `nav-chat`）；`aria-label="主导航"` 是真正的
 * 无障碍契约，它保证「这一项确实在主导航里」，而不是页头某个碰巧同名的按钮。
 * 现在 brand 已改名为 `brand-home`，但作用域仍然保留——它防的是下一次
 * 「把导航项挪进页头」的回归，而不是这一次的重名。
 */
function railItem(page, testid) {
  return page.locator(`.sidenav [aria-label="主导航"] [data-testid="${testid}"]`).first();
}

/** 抽屉里的导航项。 */
function drawerItem(page, testid) {
  return page.locator(`[data-testid="nav-drawer"] [data-testid="${testid}"]`).first();
}

async function pathOf(page) {
  return page.evaluate(() => `${window.location.pathname}${window.location.search}`);
}

async function waitPath(page, expected, timeout = 10_000) {
  return waitUntil(async () => (await pathOf(page)) === expected, `地址变成 ${expected}`, timeout).then(
    () => true,
    () => false,
  );
}

/** 同一时刻只有一个 aria-current="page"，返回它的 testid（没有则 null）。 */
async function currentNav(page) {
  return page.evaluate(() => {
    const marks = document.querySelectorAll('[aria-current="page"]');
    return {
      count: marks.length,
      testid: marks.length === 1 ? marks[0].getAttribute('data-testid') : null,
      text: marks.length === 1 ? (marks[0].textContent || '').trim() : '',
    };
  });
}

async function overflow(page) {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
}

async function gotoPage(page, href) {
  await page.goto(`${webBase}${href}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('main.app-main', { timeout: 20_000 });
  await page.waitForSelector('header.app-header', { timeout: 20_000 });
  await page.waitForTimeout(300);
}

/* ------------------------------------------------------------ 触控目标审计 */

const MIN_TOUCH = 44;
const INTERACTIVE = 'button, a, input, select, textarea, summary, [role="button"]';

/**
 * 遍历外壳（`.app-header` / 桌面侧栏 / 抽屉）里的**所有**可交互元素，
 * 逐个断言 `getBoundingClientRect()` 宽高 ≥44。
 *
 * 只量外壳，不量页内：页面正文的触控目标由各自的 E2E 负责（memory/dashboard）。
 * 隐藏元素（`display:none`，例如窄屏被收起的顶栏快捷入口）不算「屏幕上的按钮」。
 */
async function auditShellTouchTargets(page, label) {
  const report = await page.evaluate((selector) => {
    const scopes = ['.app-header', '.sidenav', '[data-testid="nav-drawer"]'];
    const items = [];
    const seen = new Set();
    for (const scope of scopes) {
      const root = document.querySelector(scope);
      if (!root) continue;
      for (const el of root.querySelectorAll(selector)) {
        if (seen.has(el)) continue;
        seen.add(el);
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        items.push({
          tag: el.tagName.toLowerCase(),
          className: typeof el.className === 'string' ? el.className : '',
          text: ((el.textContent || '').trim() || el.getAttribute('aria-label') || '').slice(0, 30),
          width: rect.width,
          height: rect.height,
          scope,
        });
      }
    }
    return { items };
  }, INTERACTIVE);

  // 下限按最小外壳形态定：抽屉关闭的聊天页只有「打开导航 / 打开会话列表 / ling」
  // 三个可点元素（顶栏快捷入口在窄屏是 display:none，已按隐藏过滤）。
  assert(report.items.length >= 3, `${label}：只找到 ${report.items.length} 个外壳可交互元素，审计范围不对`);
  const bad = report.items.filter((item) => item.width < MIN_TOUCH - 0.01 || item.height < MIN_TOUCH - 0.01);
  if (bad.length > 0) {
    const lines = bad
      .map(
        (item) =>
          `  - <${item.tag} class="${item.className}"> ${item.width.toFixed(2)}×${item.height.toFixed(2)}「${item.text}」（${item.scope}）`,
      )
      .join('\n');
    throw new Error(`${label}：${bad.length}/${report.items.length} 个外壳可交互元素小于 ${MIN_TOUCH}×${MIN_TOUCH}\n${lines}`);
  }
  const widest = report.items
    .map((item) => `${item.tag}${item.className ? `.${item.className.split(' ')[0]}` : ''} ${Math.round(item.width)}×${Math.round(item.height)}`)
    .slice(0, 6)
    .join('、');
  ok(`${label}：外壳 ${report.items.length} 个可交互元素全部 ≥${MIN_TOUCH}×${MIN_TOUCH}`, widest);
}

/* ---------------------------------------------------------------- 桌面 */

async function runDesktop(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  await loginUi(page);
  ok('桌面：登录后落在 /dashboard', (await pathOf(page)) === '/dashboard', await pathOf(page));

  // 侧栏默认展开 200px，与主区不重叠，不产生横向滚动
  const layout = await page.evaluate(() => {
    const rect = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
    };
    return {
      side: rect('.sidenav'),
      main: rect('main.app-main'),
      expanded: document.querySelector('.sidenav')?.getAttribute('data-expanded'),
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    };
  });
  assert(layout.side, '桌面没有渲染左导航');
  assert(layout.main, '桌面没有主区');
  assert(layout.expanded === 'true', `默认应为展开，实际 data-expanded=${layout.expanded}`);
  assert(Math.abs(layout.side.width - 200) <= 1, `展开宽度应为 200px，实际 ${layout.side.width}`);
  ok('桌面：侧栏默认展开 200px', `${layout.side.width}×${layout.side.height}`);
  assert(layout.side.right <= layout.main.x + 0.5, `侧栏与主区重叠：侧栏右缘 ${layout.side.right} > 主区左缘 ${layout.main.x}`);
  ok('桌面：侧栏与内容区不重叠、无横向滚动', `侧栏右缘 ${Math.round(layout.side.right)} / 主区左缘 ${Math.round(layout.main.x)} / scrollWidth=${layout.scrollWidth}`);

  // 顺序（浏览器里再断一次，和单测互为独立实现）
  const railOrder = await page.evaluate(() =>
    // 只在「主导航」landmark 里数：折叠按钮自己也是 nav-*，但它在导航区之外
    [...document.querySelectorAll('.sidenav [aria-label="主导航"] [data-testid^="nav-"]')].map((el) =>
      el.getAttribute('data-testid'),
    ),
  );
  assert(
    JSON.stringify(railOrder) ===
      JSON.stringify(['nav-dashboard', 'nav-chat', 'nav-memory', 'nav-persona', 'nav-life', 'nav-settings', 'nav-version']),
    `左导航顺序不对：${railOrder.join(' → ')}`,
  );
  ok('桌面：导航顺序 总览→对话→记忆→人设→生活→设置→版本', railOrder.join(' → '));

  await shot(page, 'desktop-01-dashboard');

  // 六项可达
  const routes = [
    ['nav-dashboard', '/dashboard'],
    ['nav-chat', '/'],
    ['nav-memory', '/memory'],
    ['nav-persona', '/persona'],
    ['nav-life', '/life'],
    ['nav-settings', '/settings'],
  ];
  for (const [testid, href] of routes) {
    await railItem(page, testid).click();
    const arrived = await waitPath(page, href);
    assert(arrived, `点 ${testid} 后应到 ${href}，实际 ${await pathOf(page)}`);
    const current = await currentNav(page);
    assert(current.count === 1, `${testid}：aria-current="page" 应恰好 1 个，实际 ${current.count}`);
    assert(current.testid === testid, `${testid}：当前项标成了 ${current.testid}`);
    const dims = await overflow(page);
    assert(dims.scrollWidth <= dims.innerWidth, `${href} 出现横向滚动：${dims.scrollWidth}/${dims.innerWidth}`);
    await shot(page, `desktop-page-${testid.replace('nav-', '')}`);
  }
  ok('桌面：六项导航都可达、URL 正确、当前项唯一');

  // /settings/* 与 /s/:id 的高亮
  await gotoPage(page, '/settings/model');
  let current = await currentNav(page);
  assert(current.count === 1 && current.testid === 'nav-settings', `/settings/model 应高亮设置，实际 ${JSON.stringify(current)}`);
  await gotoPage(page, '/settings/account');
  current = await currentNav(page);
  assert(current.count === 1 && current.testid === 'nav-settings', `/settings/account 应高亮设置，实际 ${JSON.stringify(current)}`);
  ok('桌面：/settings/* 让「设置」高亮（子页不再无归属）');
  await shot(page, 'desktop-02-settings-highlight');

  // 旧路径 /settings/memory 的例外：URL 前缀是 /settings，显示的是记忆页内容，
  // 所以高亮「记忆」——按内容归类，而不是按地址栏前缀归类。
  await gotoPage(page, '/settings/memory');
  current = await currentNav(page);
  assert(current.count === 1 && current.testid === 'nav-memory', `/settings/memory 应高亮记忆，实际 ${JSON.stringify(current)}`);
  ok('桌面：旧路径 /settings/memory 高亮「记忆」（按内容，不按前缀）');
  await shot(page, 'desktop-04-memory-settings');

  // 进一次聊天页：`GET /v1/sessions` 会给该主体确保一个默认会话窗口，
  // 之后才有真实的 `/s/:id` 可以验证高亮（不是编一个不存在的 id）。
  await gotoPage(page, '/');
  const sessionId = await waitUntil(
    async () =>
      page.evaluate(async () => {
        const response = await fetch('/v1/sessions', { credentials: 'same-origin' });
        const data = await response.json();
        return data.sessions?.[0]?.session_id ?? null;
      }),
    '存在至少一个会话',
    15_000,
  );
  if (sessionId) {
    await gotoPage(page, `/s/${sessionId}`);
    current = await currentNav(page);
    assert(current.count === 1 && current.testid === 'nav-chat', `/s/${sessionId} 应高亮对话，实际 ${JSON.stringify(current)}`);
    ok('桌面：/s/:id 让「对话」高亮', `/s/${sessionId}`);
  } else {
    assert(false, '没有会话可用于 /s/:id 高亮验证');
  }

  /*
   * L1：1440 不许再出现聊天页那个「死 ≡」。
   *
   * 它在 >900 被 nav.css 隐藏（不是删除：≤900 它是打开会话列表的真入口）。
   * 同时量 brand 的 x：修之前 ≡ 占着 x=16–50，brand 被挤到 x=62；隐藏后
   * 聊天页与非聊天页的 brand 应该落在同一个位置。
   */
  assert(
    (await page.locator('.app-header__menu').count()) === 1,
    '聊天页应该仍渲染「打开会话列表」按钮（>900 只是隐藏，不是删掉）',
  );
  assert(
    !(await page.locator('.app-header__menu').isVisible()),
    '1440 仍能看到聊天页的 ≡（.app-header__menu 必须在 >900 隐藏）',
  );
  const brandXChat = await page.evaluate(() =>
    Math.round(document.querySelector('.app-header .brand').getBoundingClientRect().x),
  );
  await gotoPage(page, '/dashboard');
  const brandXHome = await page.evaluate(() =>
    Math.round(document.querySelector('.app-header .brand').getBoundingClientRect().x),
  );
  assert(
    brandXChat === brandXHome,
    `聊天页 brand 仍被挤偏：聊天页 x=${brandXChat} / 其他页 x=${brandXHome}`,
  );
  ok(
    '桌面：>900 不显示聊天页的 ≡，brand 不再右移',
    `menu isVisible=false · brand x=${brandXChat}`,
  );

  // 折叠持久化：展开(200) → 折叠(56) → 刷新仍是 56 → 展开(200) → 刷新仍是 200
  const readWidth = () =>
    page.evaluate(() => {
      const el = document.querySelector('.sidenav');
      return { width: el.getBoundingClientRect().width, expanded: el.getAttribute('data-expanded') };
    });
  const readStored = () => page.evaluate(() => window.localStorage.getItem('ling.nav.expanded'));

  await page.locator('[data-testid="nav-toggle"]').click();
  await waitUntil(async () => (await readWidth()).width <= 57, '侧栏收到 56px', 5_000);
  const collapsed = await readWidth();
  assert(collapsed.expanded === 'false', `折叠后 data-expanded=${collapsed.expanded}`);
  assert(Math.abs(collapsed.width - 56) <= 1, `折叠宽度应为 56px，实际 ${collapsed.width}`);
  assert((await readStored()) === '0', `localStorage 应记 '0'，实际 ${await readStored()}`);
  ok('桌面：点折叠收成 56px 图标 rail，并写进 localStorage', `${collapsed.width}px`);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.sidenav', { timeout: 20_000 });
  await page.waitForTimeout(300);
  const afterReload = await readWidth();
  assert(afterReload.expanded === 'false' && Math.abs(afterReload.width - 56) <= 1, `刷新后应仍是折叠，实际 ${JSON.stringify(afterReload)}`);
  ok('桌面：刷新后折叠状态保持（localStorage 持久化）', `${afterReload.width}px`);
  await shot(page, 'desktop-03-rail-collapsed');

  await page.locator('[data-testid="nav-toggle"]').click();
  await waitUntil(async () => (await readWidth()).width >= 199, '侧栏展开到 200px', 5_000);
  assert((await readStored()) === '1', `localStorage 应记 '1'，实际 ${await readStored()}`);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.sidenav', { timeout: 20_000 });
  await page.waitForTimeout(300);
  const expandedAgain = await readWidth();
  assert(expandedAgain.expanded === 'true' && Math.abs(expandedAgain.width - 200) <= 1, `刷新后应仍展开，实际 ${JSON.stringify(expandedAgain)}`);
  ok('桌面：展开状态同样持久化（默认展开不变）', `${expandedAgain.width}px`);

  // 顶栏收尾：quick-* 已删（入口一个不少），账号区仍在，brand testid 唯一
  const chrome = await page.evaluate(() => {
    const el = document.querySelector('header.app-header');
    return el
      ? {
          brand: !!el.querySelector('[data-testid="brand-home"]'),
          logout: !!el.querySelector('[data-testid="nav-logout"]'),
          user: (el.querySelector('.app-header__user')?.textContent || '').trim(),
          quickTestids: document.querySelectorAll('[data-testid^="quick-"]').length,
          quickNav: document.querySelectorAll('[aria-label="快捷导航"]').length,
          brandCount: document.querySelectorAll('[data-testid="brand-home"]').length,
          navChatCount: document.querySelectorAll('[data-testid="nav-chat"]').length,
          navChatInRail: !!document.querySelector('.sidenav [aria-label="主导航"] [data-testid="nav-chat"]'),
        }
      : null;
  });
  assert(chrome, '找不到页头');
  assert(chrome.brand && chrome.logout && chrome.user.length > 0, `页头账号区缺失：${JSON.stringify(chrome)}`);
  assert(
    chrome.quickTestids === 0 && chrome.quickNav === 0,
    `顶栏过渡入口没删干净：quick-*=${chrome.quickTestids} 快捷导航=${chrome.quickNav}`,
  );
  assert(chrome.brandCount === 1, `brand-home 应恰好 1 个，实际 ${chrome.brandCount}`);
  assert(
    chrome.navChatCount === 1 && chrome.navChatInRail,
    `nav-chat 应只剩左导航里那一个，实际 ${chrome.navChatCount}（inRail=${chrome.navChatInRail}）`,
  );
  ok(
    '桌面：顶栏只剩 brand + 账号区（quick-* 已删、旧入口全在左导航、brand testid 唯一）',
    `user=${chrome.user}`,
  );

  assert(pageErrors.length === 0, `桌面出现未捕获异常：${pageErrors.join(' | ')}`);
  ok('桌面：没有未捕获的前端异常');

  // 桌面也必须能退出：账号区在页头（窄屏在抽屉），不许出现「桌面能退、手机不能」。
  await page.locator('[data-testid="nav-logout"]').first().click();
  const loggedOut = await page
    .waitForSelector('input[name="password"]', { timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  assert(loggedOut, '桌面点页头「退出」没有回到登录页');
  assert(
    (await page.evaluate(() => window.location.pathname)) === '/dashboard',
    `退出后应回登录页（/dashboard），实际 ${await pathOf(page)}`,
  );
  ok('桌面：页头账号区的「退出」真的回到登录页');
  await context.close();
}

/* ---------------------------------------------------------------- 窄屏 */

async function runMobile(browser) {
  const context = await browser.newContext({
    viewport: { width: 375, height: 667 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  await loginUi(page);
  assert((await pathOf(page)) === '/dashboard', `窄屏登录后应落在 /dashboard，实际 ${await pathOf(page)}`);

  const navOpen = page.locator('[data-testid="nav-open"]').first();
  assert((await navOpen.count()) > 0, '窄屏缺少常驻的 nav-open 按钮');
  assert((await navOpen.isVisible()), 'nav-open 不可见');
  const label = await navOpen.getAttribute('aria-label');
  assert(label === '打开导航', `nav-open 的 aria-label 是「${label}」，应为「打开导航」`);
  const noRail = (await page.locator('.sidenav').count()) === 0;
  assert(noRail, '窄屏不该渲染桌面 rail（会有隐藏的重复 testid）');
  ok('窄屏：常驻「打开导航」按钮（aria-label=打开导航，不是会话列表）');
  await shot(page, 'mobile-01-dashboard');

  // 打开抽屉：portal 到 body + 全屏 scrim
  await navOpen.click();
  await page.waitForSelector('[data-testid="nav-drawer"]', { timeout: 10_000 });
  const drawer = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="nav-drawer"]');
    const scrim = document.querySelector('[data-testid="nav-scrim"]');
    const panel = document.querySelector('.nav-drawer__panel');
    if (!root || !scrim || !panel) return null;
    const s = scrim.getBoundingClientRect();
    const p = panel.getBoundingClientRect();
    return {
      parentIsBody: root.parentElement === document.body,
      headerAncestor: !!root.closest('header'),
      scrim: { x: s.x, y: s.y, width: s.width, height: s.height },
      panel: { x: p.x, y: p.y, width: p.width, height: p.height },
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      bodyOverflow: getComputedStyle(document.body).overflow,
    };
  });
  assert(drawer, '抽屉 / scrim / 面板没有渲染');
  // 先断「看得见的后果」：遮罩是否真的盖满视口。抽屉如果放回 `<header>` 里，
  // `.app-header` 的 backdrop-filter 会成为包含块，`inset: 0` 只盖住页头那 56px——
  // 正文照样亮着、还能点，那才是这个约束要防的故障。
  assert(
    drawer.scrim.x <= 0.5 && drawer.scrim.y <= 0.5 && drawer.scrim.width >= drawer.innerWidth - 1 && drawer.scrim.height >= drawer.innerHeight - 1,
    `scrim 没有盖满视口：${JSON.stringify(drawer.scrim)} vs ${drawer.innerWidth}×${drawer.innerHeight}`,
  );
  // 再断结构原因：它必须是 body 的直接子节点（portal 生效），且不在 header 里。
  assert(drawer.parentIsBody, '抽屉不是 <body> 的直接子节点（portal 没生效）');
  assert(!drawer.headerAncestor, '抽屉被渲染进 <header> 里了');
  ok(
    '窄屏：抽屉 portal 到 body，scrim 盖满整个视口',
    `scrim ${Math.round(drawer.scrim.width)}×${Math.round(drawer.scrim.height)}`,
  );
  assert(drawer.bodyOverflow === 'hidden', `抽屉打开时 body 滚动锁没生效：overflow=${drawer.bodyOverflow}`);
  ok('窄屏：抽屉打开时 body 滚动锁生效');
  await shot(page, 'mobile-02-drawer');

  // 外壳触控目标（抽屉开着时一次量全：页头 + 抽屉）
  await auditShellTouchTargets(page, '窄屏 375（抽屉打开）');

  // 焦点陷阱：Tab 多次不跑出抽屉
  await page.evaluate(() => {
    const first = document.querySelector('[data-testid="nav-drawer"] .nav-drawer__close');
    if (first instanceof HTMLElement) first.focus();
  });
  let escaped = false;
  for (let i = 0; i < 14; i += 1) {
    await page.keyboard.press('Tab');
    const inside = await page.evaluate(() => {
      const root = document.querySelector('[data-testid="nav-drawer"]');
      return root ? root.contains(document.activeElement) : false;
    });
    if (!inside) escaped = true;
  }
  assert(!escaped, 'Tab 把焦点带出了抽屉（焦点陷阱失效）');
  ok('窄屏：焦点陷阱把 Tab 关在抽屉里（14 次 Tab 都没跑出去）');

  // Escape 关闭 + 焦点还原
  await page.keyboard.press('Escape');
  await waitUntil(async () => (await page.locator('[data-testid="nav-drawer"]').count()) === 0, 'Escape 关闭抽屉', 5_000);
  // 焦点还原是异步的（等抽屉卸载后再还，避免被卸载时的清理抢走），所以等一小会
  const focus = await waitUntil(
    async () => {
      const info = await page.evaluate(() => ({
        testid: document.activeElement?.getAttribute('data-testid') ?? null,
        tag: document.activeElement?.tagName ?? null,
        cls: typeof document.activeElement?.className === 'string' ? document.activeElement.className : '',
      }));
      return info.testid === 'nav-open' ? info : null;
    },
    '焦点回到 nav-open',
    3_000,
  ).catch(async () => ({
    ...(await page.evaluate(() => ({
      testid: document.activeElement?.getAttribute('data-testid') ?? null,
      tag: document.activeElement?.tagName ?? null,
      cls: typeof document.activeElement?.className === 'string' ? document.activeElement.className : '',
    }))),
    failed: true,
  }));
  assert(!focus.failed, `关闭后焦点应回到 nav-open，实际是 <${String(focus.tag).toLowerCase()} class="${focus.cls}">`);
  ok('窄屏：关闭后焦点还原到「打开导航」', `<${String(focus.tag).toLowerCase()} data-testid="nav-open">`);
  const unlocked = await page.evaluate(() => getComputedStyle(document.body).overflow);
  assert(unlocked !== 'hidden', `关闭后 body 滚动锁没还原：overflow=${unlocked}`);
  ok('窄屏：Escape 关闭抽屉，焦点回到「打开导航」，滚动锁还原');

  // 点 scrim 关闭
  await navOpen.click();
  await page.waitForSelector('[data-testid="nav-drawer"]', { timeout: 10_000 });
  await page.locator('[data-testid="nav-scrim"]').click({ position: { x: 340, y: 620 } });
  await waitUntil(async () => (await page.locator('[data-testid="nav-drawer"]').count()) === 0, '点 scrim 关闭抽屉', 5_000);
  ok('窄屏：点 scrim 关闭抽屉');

  /*
   * 抽屉里的六项都可达 + 路由切换自动关闭 + 当前项高亮。
   *
   * 窄屏没有常驻导航，当前项只能在抽屉里看——所以每一步都「开抽屉 → 断言当前项
   * 唯一且对得上 → 点目标 → 断言地址与自动关闭 → 再开一次确认高亮已经跟过来」。
   */
  const drawerRoutes = [
    ['nav-chat', '/'],
    ['nav-memory', '/memory'],
    ['nav-persona', '/persona'],
    ['nav-life', '/life'],
    ['nav-settings', '/settings'],
    ['nav-dashboard', '/dashboard'],
  ];
  let expectedCurrent = 'nav-dashboard';
  for (const [testid, href] of drawerRoutes) {
    await navOpen.click();
    await page.waitForSelector('[data-testid="nav-drawer"]', { timeout: 10_000 });
    let current = await currentNav(page);
    assert(
      current.count === 1 && current.testid === expectedCurrent,
      `开抽屉时（当前在 ${await pathOf(page)}）应高亮 ${expectedCurrent}，实际 ${JSON.stringify(current)}`,
    );
    await drawerItem(page, testid).click();
    const arrived = await waitPath(page, href);
    assert(arrived, `抽屉里点 ${testid} 后应到 ${href}，实际 ${await pathOf(page)}`);
    const closed = await waitUntil(
      async () => (await page.locator('[data-testid="nav-drawer"]').count()) === 0,
      `点 ${testid} 后抽屉自动关闭`,
      5_000,
    );
    assert(closed, `点 ${testid} 后抽屉没有自动关闭`);
    expectedCurrent = testid;

    // 再开一次：高亮已经跟到新页面，且仍然只有一个
    await navOpen.click();
    await page.waitForSelector('[data-testid="nav-drawer"]', { timeout: 10_000 });
    current = await currentNav(page);
    assert(
      current.count === 1 && current.testid === testid,
      `${href} 的当前项不对：${JSON.stringify(current)}`,
    );
    await page.keyboard.press('Escape');
    await waitUntil(async () => (await page.locator('[data-testid="nav-drawer"]').count()) === 0, 'Escape 关闭抽屉', 5_000);

    const dims = await overflow(page);
    assert(dims.scrollWidth <= dims.innerWidth + 1, `${href} 出现横向滚动：${dims.scrollWidth}/${dims.innerWidth}`);
  }
  ok('窄屏：抽屉六项都可达、当前项唯一且跟随路由、切换后自动关闭、每页无横向滚动');
  await shot(page, 'mobile-03-memory');

  assert(pageErrors.length === 0, `窄屏出现未捕获异常：${pageErrors.join(' | ')}`);
  ok('窄屏：没有未捕获的前端异常');

  // 窄屏的设置子页：同一条「打开导航」在非聊天页也在（SHOTS_REPORT §5.3 的修复点）
  await gotoPage(page, '/settings/memory');
  assert(
    (await page.locator('[data-testid="nav-open"]').first().isVisible()),
    '/settings/memory 页没有「打开导航」按钮',
  );
  await shot(page, 'mobile-05-memory-settings');
  ok('窄屏：非聊天页（设置子页）同样有「打开导航」');

  // 聊天页：原「打开会话列表」按钮与 aria-label 未变、仍可用；与导航抽屉互斥
  await gotoPage(page, '/');
  const sessionButton = page.locator('button[aria-label="打开会话列表"]').first();
  assert((await sessionButton.count()) > 0, '聊天页丢了「打开会话列表」按钮（w0_browser.mjs 会红）');
  assert(await sessionButton.isVisible(), '聊天页「打开会话列表」按钮不可见');
  await sessionButton.click();
  await page.waitForSelector('[data-testid="new-session"]', { timeout: 10_000 });
  ok('窄屏：聊天页「打开会话列表」按钮仍可用（aria-label 未改）');
  await shot(page, 'mobile-04-chat-sessions');

  const sessionScrim = page.locator('button[aria-label="关闭会话列表"]').first();
  assert((await sessionScrim.count()) > 0, '会话列表的 scrim 不见了');

  /*
   * 互斥：会话列表开着时「打开导航」→ 会话列表收起、抽屉打开。
   *
   * 注意触发方式：会话列表的 scrim 盖住了整个视口（含页头），所以「打开导航」
   * 按钮此刻**物理上点不到**——真实用户得先关掉会话列表。这里用元素级
   * `click()` 触发同一个 React 事件，验证的是 overlay manager 的互斥行为本身
   * （两片浮层永远不会同时打开），而不是触控几何。几何那一半由下面
   * 「点不开被遮住的按钮」这条反证一起覆盖。
   */
  const covered = await page.evaluate(() => {
    const button = document.querySelector('[data-testid="nav-open"]');
    if (!button) return null;
    const rect = button.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return {
      tag: hit?.tagName.toLowerCase() ?? null,
      className: typeof hit?.className === 'string' ? hit.className : '',
      // 会话列表（panle 或 scrim）盖在上面：命中点落在 .sidebar 里、且不是按钮本身
      insideSidebar: !!hit?.closest('.sidebar'),
      isButtonItself: hit === button,
    };
  });
  assert(covered, '找不到 nav-open');
  assert(
    covered.insideSidebar && !covered.isButtonItself,
    `会话列表打开时 nav-open 应被盖住，实际命中 ${covered.tag}.${covered.className}`,
  );
  ok(
    '窄屏：会话列表打开时「打开导航」被浮层盖住（不会误触）',
    `命中 ${covered.tag}.${String(covered.className).split(' ')[0]}`,
  );

  await page.evaluate(() => document.querySelector('[data-testid="nav-open"]')?.click());
  await page.waitForSelector('[data-testid="nav-drawer"]', { timeout: 10_000 });
  const sessionVisible = await page.evaluate(() => {
    const el = document.querySelector('.sidebar');
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return getComputedStyle(el).visibility !== 'hidden' && rect.width > 0 && el.classList.contains('sidebar--open');
  });
  assert(!sessionVisible, '导航抽屉与会话列表同时开着（overlay 没有互斥）');
  ok('窄屏：打开导航时聊天会话列表自动收起（单一 overlay manager）');

  // 反向：抽屉开着时打开会话列表 → 抽屉收起（同样用元素级 click）
  await page.evaluate(() => document.querySelector('button[aria-label="打开会话列表"]')?.click());
  await waitUntil(async () => (await page.locator('[data-testid="nav-drawer"]').count()) === 0, '打开会话列表时抽屉收起', 5_000);
  await page.waitForSelector('[data-testid="new-session"]', { timeout: 10_000 });
  ok('窄屏：打开会话列表时导航抽屉自动收起（反向互斥）');

  // 会话列表开着 → 路由变化（点会话/新建）也要收；再回到会话列表可用状态
  // scrim 铺满视口，它的几何中心落在面板上；点远离面板的那一侧才是「点遮罩」
  await page
    .locator('button[aria-label="关闭会话列表"]')
    .first()
    .click({ position: { x: 340, y: 600 } });
  await page.waitForTimeout(400);

  // 窄屏页头触控目标（抽屉关闭态单独再量一次，覆盖 nav-open / 会话按钮 / brand）
  await page.waitForSelector('button[aria-label="打开会话列表"]', { timeout: 10_000 });
  await auditShellTouchTargets(page, '窄屏 375（抽屉关闭）');

  /*
   * M1：移动端的账号归宿。
   *
   * 1. 账号页的「退出登录」要 ≥44×44（它是三跳路径的最后一跳，原先 85.09×39.59）；
   * 2. 抽屉底部要有账号区（用户名 + 退出登录），并且能从那里真的退出登录；
   * 3. 页头在 375 不渲染账号区——`nav-logout` 在 DOM 里只能有一个。
   */
  await gotoPage(page, '/settings/account');
  const accountLogout = page.locator('[data-testid="account-logout"]');
  assert(
    (await accountLogout.count()) === 1 && (await accountLogout.isVisible()),
    '账号页找不到可见的「退出登录」按钮',
  );
  const accountBox = await accountLogout.boundingBox();
  assert(
    accountBox && accountBox.width >= 44 && accountBox.height >= 44,
    `账号页「退出登录」小于 44×44：${accountBox?.width?.toFixed(2)}×${accountBox?.height?.toFixed(2)}`,
  );
  ok(
    '窄屏：账号页「退出登录」≥44×44',
    `${accountBox.width.toFixed(2)}×${accountBox.height.toFixed(2)}`,
  );
  await shot(page, 'mobile-07-account');
  // 退出按钮在账号页最下面：滚到它再拍一张，作为「命中区真的够大」的视觉证据
  await accountLogout.scrollIntoViewIfNeeded();
  await shot(page, 'mobile-07-account-logout');

  await gotoPage(page, '/dashboard');
  assert(
    (await page.locator('[data-testid="nav-logout"]').count()) === 0,
    '窄屏页头不该渲染 nav-logout（会与抽屉里的那个重名）',
  );
  await page.locator('[data-testid="nav-open"]').first().click();
  await page.waitForSelector('[data-testid="nav-drawer"]', { timeout: 10_000 });
  const account = page.locator('[data-testid="nav-drawer"] [data-testid="nav-account"]');
  assert((await account.count()) === 1 && (await account.isVisible()), '抽屉底部没有账号区');
  const accountText = ((await account.textContent()) || '').trim();
  assert(accountText.includes(USERNAME), `抽屉账号区没显示用户名：${accountText}`);
  const drawerLogout = page.locator('[data-testid="nav-drawer"] [data-testid="nav-logout"]');
  assert(
    (await drawerLogout.count()) === 1 && (await drawerLogout.isVisible()),
    '抽屉底部没有可见的「退出登录」',
  );
  const logoutBox = await drawerLogout.boundingBox();
  assert(
    logoutBox && logoutBox.width >= 44 && logoutBox.height >= 44,
    `抽屉「退出登录」小于 44×44：${logoutBox?.width?.toFixed(2)}×${logoutBox?.height?.toFixed(2)}`,
  );
  await shot(page, 'mobile-06-drawer-account');
  ok(
    '窄屏：抽屉底部账号区显示用户名，退出登录 ≥44×44',
    `「${accountText.replace(/\s+/g, ' ')}」· ${logoutBox.width.toFixed(0)}×${logoutBox.height.toFixed(0)}`,
  );

  await drawerLogout.click();
  const drawerLoggedOut = await page
    .waitForSelector('input[name="password"]', { timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  assert(drawerLoggedOut, '从抽屉点「退出登录」没有回到登录页');
  assert(
    (await page.evaluate(() => window.location.pathname)) === '/dashboard',
    `抽屉退出后应回登录页（/dashboard），实际 ${await pathOf(page)}`,
  );
  ok('窄屏：从抽屉退出登录成功，回到登录页');

  await context.close();
}

/* ---------------------------------------------------------------- main */

let exitCode = 0;

/**
 * 收尾：kill 服务进程组 → lsof 复查端口 → **只删本次自建的库**。
 * 正常结束、断言失败、Ctrl-C/SIGTERM 都走这里（幂等）。
 */
const cleanupState = { done: false, portsClean: true, dbDropped: false, dbError: null };
async function cleanupResources() {
  if (cleanupState.done) return cleanupState;
  cleanupState.done = true;

  if (process.env.KEEP_SERVERS === '1') {
    console.log(
      `KEEP_SERVERS=1：API ${apiBase} / Web ${webBase} 保留` +
        (DB_NAME ? `、测试库 ${DB_NAME}（调试完请手动删）` : ''),
    );
    return cleanupState;
  }

  for (const server of servers.reverse()) {
    await stopProcess(server.child, server.label);
  }
  for (const port of [apiPort, webPort]) {
    if (!port) continue;
    const still = listeningOn(port);
    if (still) {
      console.error(`端口 ${port} 仍在监听：\n${still}`);
      cleanupState.portsClean = false;
    } else {
      console.log(`端口 ${port} 已释放`);
    }
  }

  // 先 kill 服务再删库：DROP 只对本次 CREATE DATABASE 亲手建的名字执行。
  if (DB_NAME) {
    try {
      dropOwnedDatabase({ name: DB_NAME, pgPort: PG_PORT, python: PYTHON, repoRoot: REPO_ROOT });
      cleanupState.dbDropped = true;
    } catch (error) {
      cleanupState.dbError = error?.message || String(error);
      console.error(`\nFAIL  清理本次测试库失败：${cleanupState.dbError}`);
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
    // 调试/变异验证时可以只跑一半：NAV_E2E_ONLY=desktop|mobile
    const only = process.env.NAV_E2E_ONLY || '';
    if (only !== 'mobile') await runDesktop(browser);
    if (only !== 'desktop') await runMobile(browser);
  } finally {
    await browser.close();
  }
} catch (error) {
  console.error(`\nFAIL  ${error instanceof Error ? error.message : String(error)}`);
  exitCode = 1;
} finally {
  const state = await cleanupResources();
  if (!state.portsClean) exitCode = 1;
  if (state.dbError) exitCode = 1;
  if (!state.dbDropped && process.env.KEEP_SERVERS !== '1' && DB_NAME) exitCode = 1;
}

console.log(`\n结果：${passed} 项通过，退出码 ${exitCode}`);
console.log(
  `截图 / 日志目录：${SHOTS}` +
    (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定，会写到那里）' : '（默认临时目录；设 SHOTS_DIR 可改）'),
);
process.exit(exitCode);
