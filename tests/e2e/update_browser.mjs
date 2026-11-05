/**
 * 关于与更新页的浏览器实测（桌面 1440×900 + 手机 375×667）。
 *
 * 特点：
 * - 全部 /v1/** 请求由 Playwright 拦截并 mock，**绝不真正 POST 更新**，
 *   也不会触发后端、部署脚本或真实更新；
 * - 只要求一个静态可访问的前端地址（例如 `vite preview` 的隔离端口），
 *   登录态也由 mock 提供；
 * - BASE_URL 必须显式给出、必须指向本机，且不能是共享的 8080。
 *
 * 覆盖：
 * - 没有 Release / 未启用 / 确认 tag 不一致与完全一致 / 只 POST 一次；
 * - 进度 / 断连等待恢复 / 失败 / needs_manual / 刷新后恢复任务；
 * - 旧 succeeded 之后第二次更新；predeploy failed 后重新检查再确认；
 * - POST 响应丢失（已落地）恢复且仍只 POST 一次；
 * - POST 响应丢失且未落地时旧 succeeded 不被当成本次成功（最终明确“无法确认”）；
 * - 初次加载断连后恢复；409 当前版本变化后带新 expected 重新确认；
 * - succeeded 但 commit 不匹配时不误报完成；
 * - succeeded 且带 backup_warnings：结果区显著位置提示“备份不完整 / 媒体归档可能
 *   漏文件 / 恢复需人工核对 / 见 verify-warnings.txt”，不在折叠区；无告警时不显示；
 * - 门禁原因 manual_required / protocol_unsupported：状态栏专用文案、安装按钮禁用、
 *   没有忽略/取消/强制/自动重试入口；
 * - 提交路径 503 `provider_unavailable`（detail = manual_required / protocol_unsupported）：
 *   显示门禁文案，页面上不出现「模型供应商」「密钥」；
 * - 键盘取消/提交、桌面与手机无横向溢出、截图。
 *
 * 用法：
 *   cd clients/web-console && pnpm build
 *   pnpm preview --port <随机隔离端口> --strictPort &
 *   BASE_URL=http://127.0.0.1:<端口> node tests/e2e/update_browser.mjs
 *
 * 环境变量：BASE_URL（必填）、SHOTS_DIR、CHROME_PATH。
 * 可选 SCENARIOS（逗号分隔的场景 key，例如 `SCENARIOS=gate-manual-required`）
 * 只用于定向复跑；正式回归不要设置，跑完整套件。
 *
 * Playwright 解析顺序：先取 `clients/web-console` 的 devDependency（正式安装位置，
 * 与 lockfile 一致），再退回从本目录正常解析。`clients/web-console` 里的 playwright
 * 版本要与本机已缓存的 chromium 版本对齐，否则不设 CHROME_PATH 时启动会失败。
 * 任一断言失败以非 0 退出。
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

import { resolveShotsDir } from './lib/isolation.mjs';

const RAW_BASE = process.env.BASE_URL;
// 截图默认写临时目录：不默认往仓库的 shots/ 里写，免得覆盖别人的证据。
const SHOTS_INFO = resolveShotsDir('update');
const SHOTS = SHOTS_INFO.dir;
console.log(
  `截图目录：${SHOTS}` +
    (SHOTS_INFO.fromEnv ? '（SHOTS_DIR 显式指定）' : '（未设 SHOTS_DIR，默认临时目录，不写仓库）'),
);
const TARGET_COMMIT = 'c0ffee1234567890abcdef1234567890abcdef12';
const CURRENT_COMMIT = '7b2370238f0b7e9da1b9e3f3f0b4a4d7f0b8c9a1';
const SECOND_TARGET = '1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d';
const NEW_CURRENT_COMMIT = '9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c';

function failConfig(message) {
  console.error(`配置错误：${message}`);
  console.error(
    '用法：BASE_URL=http://127.0.0.1:<隔离端口> node tests/e2e/update_browser.mjs',
  );
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
  failConfig('BASE_URL 不能是共享的 8080；请另起静态预览端口（例如 4391）');
}
if (!Number.isInteger(basePort) || basePort <= 0) {
  failConfig(`BASE_URL 端口不合法：${RAW_BASE}`);
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');

/**
 * 解析 playwright：`clients/web-console` 是它的正式安装位置（devDependency + lockfile），
 * 所以优先从这里取；取不到才退回普通模块解析（例如本目录自带的 node_modules）。
 * 两条都失败就明确说清修复办法，不要让 ESM 抛一个裸的 ERR_MODULE_NOT_FOUND。
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

async function waitFor(predicate, timeout = 15000, interval = 150) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
  return false;
}

async function waitForText(page, text, timeout = 15000) {
  return waitFor(async () => ((await page.textContent('body')) || '').includes(text), timeout);
}

async function bodyText(page) {
  return (await page.textContent('body')) || '';
}

async function shot(page, name, fullPage = false) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage });
}

async function scrollToJob(page) {
  await page
    .locator('[data-testid="update-job"]')
    .scrollIntoViewIfNeeded()
    .catch(() => {});
}

async function assertNoOverflow(page, label) {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    innerWidth: window.innerWidth,
  }));
  record(
    `${label}：无横向溢出`,
    metrics.scrollWidth <= metrics.innerWidth + 1,
    `${metrics.scrollWidth} > ${metrics.innerWidth}`,
  );
}

function deepEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

/** 失败态标题/详情；避免用会命中“更新完成或失败”提示的宽泛子串。 */
function waitingBodyContainsFailure(body) {
  return body.includes('更新失败') || body.includes('需要人工处理');
}

async function textOf(page, testId) {
  const loc = page.locator(`[data-testid="${testId}"]`);
  if ((await loc.count()) === 0) return '';
  return (await loc.innerText().catch(() => '')) || '';
}

async function jobState(page) {
  return page
    .locator('[data-testid="update-job"]')
    .getAttribute('data-job-state')
    .catch(() => null);
}

async function jobRequestId(page) {
  return page
    .locator('[data-testid="update-job"]')
    .getAttribute('data-request-id')
    .catch(() => null);
}

/* ------------------------------------------------------------------ mocks */

function defaultVersion() {
  return {
    schema_version: 'v1',
    current: '0.1.0',
    commit: CURRENT_COMMIT,
    web_manifest_sha256: 'a'.repeat(64),
  };
}

function updatedVersion() {
  return { ...defaultVersion(), current: '0.2.0', commit: TARGET_COMMIT };
}

function secondUpdatedVersion() {
  return { ...defaultVersion(), current: '0.3.0', commit: SECOND_TARGET };
}

function eligibleRelease() {
  return {
    schema_version: 'v1',
    status: 'ok',
    current: '0.1.0',
    latest: 'v0.2.0',
    published_at: '2026-10-30T00:00:00Z',
    html_url: 'https://github.com/qsbb/ling/releases/tag/v0.2.0',
    is_newer: true,
    release_id: 42,
    target_commit: TARGET_COMMIT,
    update_eligible: true,
    update_reason: null,
  };
}

function nextRelease() {
  return {
    ...eligibleRelease(),
    current: '0.2.0',
    latest: 'v0.3.0',
    html_url: 'https://github.com/qsbb/ling/releases/tag/v0.3.0',
    release_id: 43,
    target_commit: SECOND_TARGET,
  };
}

function noRelease() {
  return {
    schema_version: 'v1',
    status: 'no_release',
    current: '0.1.0',
    latest: null,
    published_at: null,
    html_url: null,
    is_newer: null,
    release_id: null,
    target_commit: null,
    update_eligible: false,
    update_reason: null,
  };
}

function enabledStatus() {
  return {
    schema_version: 'v1',
    enabled: true,
    reason: null,
    executor: { online: true, last_seen_at: '2026-10-30T10:00:00Z' },
    current_job: null,
  };
}

/** 门禁原因态：通道启用、执行器在线，但服务端判定不能发起更新。 */
function gateStatus(reason) {
  return {
    schema_version: 'v1',
    enabled: true,
    reason,
    executor: { online: true, last_seen_at: '2026-10-30T10:00:00Z' },
    current_job: null,
  };
}

function makeJob(overrides = {}) {
  return {
    request_id: 'req-1',
    state: 'queued',
    stage: 'queued',
    target_tag: 'v0.2.0',
    target_commit: TARGET_COMMIT,
    created_at: '2026-10-30T10:00:00Z',
    updated_at: '2026-10-30T10:01:00Z',
    error_code: null,
    message: null,
    ...overrides,
  };
}

function makeState(overrides = {}) {
  return {
    version: defaultVersion(),
    release: eligibleRelease(),
    status: enabledStatus(),
    job: null,
    posts: [],
    /** 永久失败（401/409/...）。 */
    postFailure: null,
    /** 只失败一次；可带 { after(state) } 模拟“确认后版本变化”。 */
    postFailureOnce: null,
    /** /v1/version 与 /v1/version/update 同时断连。 */
    offline: false,
    pollCount: 0,
    onPoll: null,
    versionRequests: 0,
    checkRequests: 0,
    statusRequests: 0,
    jobSeq: 1,
    violations: [],
    /* 可控延迟/旧副本：hold* 计数表示接下来多少次请求要先挂起。 */
    holdVersion: 0,
    holdStatus: 0,
    heldVersion: 0,
    heldStatus: 0,
    gate: Promise.resolve(),
    releaseGate: () => {},
    /** 状态接口返回的旧副本任务；null 表示用 state.job。 */
    staleStatusJob: null,
    /** POST 行为：normal / lost-landed-succeeded / lost-landed-failed / lost-landed-running / lost-landed-running-stale / lost-no-land / conflict-in-progress。 */
    postMode: 'normal',
    postVersion: null,
    postStatusJob: null,
    ...overrides,
  };
}

function armGate(state) {
  state.heldVersion = 0;
  state.heldStatus = 0;
  state.gate = new Promise((resolve) => {
    state.releaseGate = resolve;
  });
}

function newJob(state) {
  return makeJob({
    request_id: `req-${++state.jobSeq}`,
    state: 'queued',
    stage: 'queued',
    target_tag: state.release.latest ?? 'v0.2.0',
    target_commit: state.release.target_commit ?? TARGET_COMMIT,
  });
}

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
        user: {
          id: 'u-mock',
          username: 'ling',
          display_name: 'Ling',
          must_change_password: false,
        },
        config_revision: 1,
        test_mode: false,
        csrf_token: 'mock-csrf',
      });
    }

    if (pathname === '/v1/version' && method === 'GET') {
      if (state.offline) return route.abort('failed');
      state.versionRequests += 1;
      if (state.holdVersion > 0) {
        state.holdVersion -= 1;
        state.heldVersion += 1;
        const captured = JSON.parse(JSON.stringify(state.version));
        await state.gate;
        return fulfillJson(route, 200, captured);
      }
      return fulfillJson(route, 200, state.version);
    }

    if (pathname === '/v1/version/check-release' && method === 'GET') {
      if (state.offline) return route.abort('failed');
      state.checkRequests += 1;
      // 当前版本一律以 /v1/version 为准，避免“只刷新 Release、拿着旧 version 继续 409”。
      return fulfillJson(route, 200, { ...state.release, current: state.version.current });
    }

    if (pathname === '/v1/version/update' && method === 'GET') {
      if (state.offline) return route.abort('failed');
      state.statusRequests += 1;
      // 只有已经存在任务时才推进轮询脚本；首次加载不算一次轮询。
      if (state.job) {
        state.pollCount += 1;
        if (state.onPoll) state.onPoll(state, state.pollCount);
      }
      const visibleJob = state.staleStatusJob !== null ? state.staleStatusJob : state.job;
      if (state.holdStatus > 0) {
        state.holdStatus -= 1;
        state.heldStatus += 1;
        const captured = JSON.parse(
          JSON.stringify({ ...state.status, current_job: visibleJob }),
        );
        await state.gate;
        return fulfillJson(route, 200, captured);
      }
      return fulfillJson(route, 200, { ...state.status, current_job: visibleJob });
    }

    if (pathname === '/v1/version/update-requests' && method === 'POST') {
      const headers = request.headers();
      const raw = request.postData() ?? '';
      let body = null;
      try {
        body = JSON.parse(raw);
      } catch {
        body = null;
      }
      state.posts.push({ headers, body, raw });
      if (!/^application\/json/i.test(headers['content-type'] || '')) {
        state.violations.push('POST 缺少 Content-Type: application/json');
      }
      if (headers['x-ling-update'] !== '1') {
        state.violations.push('POST 缺少 X-Ling-Update: 1');
      }
      if (state.postMode === 'conflict-in-progress') {
        return fulfillJson(route, 409, {
          error: {
            code: 'conflict',
            message: 'update in progress',
            retriable: false,
            detail: 'update_in_progress',
          },
        });
      }
      if (state.postMode === 'lost-landed-succeeded') {
        state.version = state.postVersion ?? updatedVersion();
        state.job = makeJob({
          request_id: `req-${++state.jobSeq}`,
          state: 'succeeded',
          stage: 'complete',
          target_tag: state.release.latest ?? 'v0.2.0',
          target_commit: state.release.target_commit ?? TARGET_COMMIT,
        });
        return route.abort('failed');
      }
      if (state.postMode === 'lost-landed-failed') {
        state.job = makeJob({
          request_id: `req-${++state.jobSeq}`,
          state: 'failed',
          stage: 'deploy',
          error_code: 'deploy_failed',
          message: '容器退出码 1',
          target_tag: state.release.latest ?? 'v0.2.0',
          target_commit: state.release.target_commit ?? TARGET_COMMIT,
        });
        return route.abort('failed');
      }
      if (state.postMode === 'lost-landed-running' || state.postMode === 'lost-landed-running-stale') {
        state.job = makeJob({
          request_id: `req-${++state.jobSeq}`,
          state: 'running',
          stage: 'fetch',
          target_tag: state.release.latest ?? 'v0.2.0',
          target_commit: state.release.target_commit ?? TARGET_COMMIT,
        });
        if (state.postMode === 'lost-landed-running-stale') {
          state.staleStatusJob = makeJob({
            request_id: 'req-old',
            state: 'succeeded',
            stage: 'complete',
          });
        }
        return route.abort('failed');
      }
      if (state.postMode === 'lost-no-land') {
        return route.abort('failed');
      }
      const failure = state.postFailureOnce ?? state.postFailure;
      if (failure) {
        if (state.postFailureOnce) state.postFailureOnce = null;
        if (typeof failure.after === 'function') failure.after(state);
        if (failure.network) {
          // 模拟“请求已落地但响应丢失”：可选地把任务写进状态。
          if (failure.landed) state.job = newJob(state);
          return route.abort('failed');
        }
        if (failure.landed) state.job = newJob(state);
        return fulfillJson(route, failure.status, failure.body);
      }
      // 关键：这里只返回 mock 任务，不触发任何真实更新。
      state.job = newJob(state);
      return fulfillJson(route, 201, state.job);
    }

    return fulfillJson(route, 404, {
      error: {
        code: 'not_found',
        message: `mock 未处理的接口 ${method} ${pathname}`,
        retriable: false,
      },
    });
  });
}

async function newHarness(browser, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    // 浏览器对 mock 的断连/409 会打资源加载错误，这不是前端异常；
    // 未捕获的 JS 异常仍由 pageerror 单独记录。
    if (/favicon/i.test(text) || /Failed to load resource/i.test(text)) return;
    consoleErrors.push(text);
  });
  page.on('pageerror', (error) => {
    const text = String(error);
    pageErrors.push(text);
    consoleErrors.push(text);
  });
  const navigations = [];
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations.push(frame.url());
  });
  return { context, page, consoleErrors, pageErrors, navigations };
}

async function openAbout(page) {
  await page.goto(`${BASE.origin}/settings/about`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="check-release"]', { timeout: 20000 });
}

async function runCheck(page) {
  await page.locator('[data-testid="check-release"]').click();
  await page.waitForSelector('[data-testid="release-result"]', { timeout: 10000 });
}

async function fillConfirmToExact(page, tag = 'v0.2.0') {
  await page.locator('[data-testid="update-install"]').click();
  await page.waitForSelector('[data-testid="update-confirm"]');
  await page.locator('[data-testid="update-confirm-input"]').fill(tag);
}

/* -------------------------------------------------------------- scenarios */

async function scenarioNoRelease(page, state, prefix) {
  await openAbout(page);
  await runCheck(page);
  const body = await bodyText(page);
  record(`${prefix}：没有 Release 时明确说明`, body.includes('当前没有 Release'));
  record(
    `${prefix}：没有 Release 不制造安装目标`,
    (await page.locator('[data-testid="update-install"]').count()) === 0,
    await textOf(page, 'update-target-none'),
  );
  record(`${prefix}：没有 Release 不显示完成/失败`, !body.includes('更新完成') && !body.includes('更新失败'));
  record(`${prefix}：打开页面不会自动检查 Release`, state.checkRequests === 1, `check=${state.checkRequests}`);
  record(
    `${prefix}：页头不再声称“只读、不下载重启”`,
    !body.includes('只读展示当前运行版本') && !body.includes('不会自动检查、下载或重启'),
  );
  record(`${prefix}：不再出现“只读检查”的解释性提示`, !body.includes('不会下载或应用更新'));
  await assertNoOverflow(page, `${prefix}：没有 Release`);
  await shot(page, `${prefix}-no-release`);
}

async function scenarioDisabled(page, state, prefix) {
  state.status = {
    schema_version: 'v1',
    enabled: false,
    reason: 'disabled',
    executor: { online: false, last_seen_at: null },
    current_job: null,
  };
  await openAbout(page);
  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：目标可见但更新未启用时按钮禁用`, (await install.count()) === 1 && (await install.isDisabled()));
  const body = await bodyText(page);
  record(`${prefix}：未启用显示原因`, body.includes('未启用') && body.includes('更新通道未启用'));
  record(`${prefix}：执行器离线明确标注`, body.includes('离线'));
  record(
    `${prefix}：禁用原因就在按钮旁`,
    (await textOf(page, 'update-blocked')).includes('未启用'),
  );
  await assertNoOverflow(page, `${prefix}：未启用`);
  await shot(page, `${prefix}-disabled`);
}

async function scenarioInstallFlow(page, state, prefix, navigations) {
  state.onPoll = (s, count) => {
    if (count === 1) s.job = makeJob({ state: 'running', stage: 'build' });
  };
  await openAbout(page);
  await runCheck(page);

  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：eligible 时出现安装按钮`, (await install.count()) === 1 && (await install.isEnabled()));
  await install.click();
  await page.waitForSelector('[data-testid="update-confirm"]');

  let body = await bodyText(page);
  record(
    `${prefix}：确认区展示当前→目标 version/短 SHA`,
    body.includes('0.1.0') && body.includes('v0.2.0') && body.includes('7b23702') && body.includes('c0ffee1'),
  );
  record(`${prefix}：确认区说明会短暂停机`, body.includes('短暂停机'));
  record(
    `${prefix}：确认区说明失败可能需人工恢复且不自动回滚数据库`,
    body.includes('人工处理') && body.includes('不会自动回滚') && body.includes('数据库不会被自动恢复'),
  );

  const submit = page.locator('[data-testid="update-submit"]');
  record(`${prefix}：未输入 tag 时禁止提交`, await submit.isDisabled());
  const input = page.locator('[data-testid="update-confirm-input"]');
  record(
    `${prefix}：打开确认后输入框自动获得焦点`,
    await waitFor(() => input.evaluate((el) => el === document.activeElement), 3000),
  );
  await input.fill('v0.2.1');
  record(`${prefix}：tag 不一致时禁止提交`, await submit.isDisabled());
  record(
    `${prefix}：tag 不一致有明确提示`,
    (await page.locator('[data-testid="update-tag-mismatch"]').count()) === 1,
  );

  // 键盘取消：Esc 关闭确认区且不提交。
  await input.press('Escape');
  record(`${prefix}：Esc 关闭确认区`, (await page.locator('[data-testid="update-confirm"]').count()) === 0);
  record(`${prefix}：键盘取消不会提交`, state.posts.length === 0, `posts=${state.posts.length}`);

  await fillConfirmToExact(page);
  record(`${prefix}：tag 完全一致时允许提交`, await submit.isEnabled());
  await page.locator('[data-testid="update-confirm-input"]').scrollIntoViewIfNeeded();
  await shot(page, `${prefix}-confirm-exact`, true);
  await assertNoOverflow(page, `${prefix}：确认区`);

  navigations.length = 0;
  // 同步派发两次点击：验证 submitting 锁不依赖 React 渲染时序。
  await page.evaluate(() => {
    const button = document.querySelector('[data-testid="update-submit"]');
    if (!(button instanceof HTMLElement)) throw new Error('缺少确认按钮');
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });

  record(`${prefix}：确认后发出更新请求`, await waitFor(() => state.posts.length >= 1, 10000));
  const first = state.posts[0];
  record(`${prefix}：请求带 Content-Type application/json`, /^application\/json/i.test(first?.headers['content-type'] || ''));
  record(`${prefix}：请求带 X-Ling-Update: 1`, first?.headers['x-ling-update'] === '1');
  record(
    `${prefix}：请求体带确认 tag 与 expected 快照`,
    deepEqual(first?.body, {
      confirm_tag: 'v0.2.0',
      expected: {
        release_id: 42,
        tag: 'v0.2.0',
        commit: TARGET_COMMIT,
        current_version: '0.1.0',
        current_commit: CURRENT_COMMIT,
      },
    }),
    JSON.stringify(first?.body),
  );

  await page.waitForTimeout(3600);
  record(`${prefix}：双击确认也只 POST 一次`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(`${prefix}：提交后进入进度态`, await waitForText(page, '正在更新', 10000));
  const progressBody = await bodyText(page);
  record(`${prefix}：进度显示阶段与目标`, progressBody.includes('构建') && progressBody.includes('v0.2.0'));
  await assertNoOverflow(page, `${prefix}：进度`);
  await shot(page, `${prefix}-progress`);

  // 模拟断连：不能显示完成或失败，必须等待恢复。
  state.offline = true;
  // 任务进行中时，"等待恢复"由任务卡承担（不再重复一条全局横幅）。
  const waiting = await waitForText(page, '与服务端的连接已断开', 15000);
  record(`${prefix}：断连时显示等待服务恢复`, waiting);
  const waitingTitle = (await page.locator('[data-testid="update-job"] strong').innerText().catch(() => '')) || '';
  record(
    `${prefix}：断连不等于完成或失败`,
    waitingTitle.includes('等待恢复') &&
      (await page.locator('[data-testid="update-reload"]').count()) === 0 &&
      !waitingBodyContainsFailure(await bodyText(page)),
  );
  record(`${prefix}：断连状态标题正确`, waitingTitle.includes('与服务端的连接已断开'), waitingTitle);
  await shot(page, `${prefix}-waiting`);

  // 恢复连接：任务 succeeded 且提交匹配后才算完成。
  state.offline = false;
  state.job = makeJob({ state: 'succeeded', stage: 'complete' });
  state.version = updatedVersion();
  const reloadButton = page.locator('[data-testid="update-reload"]');
  const completed = await reloadButton
    .waitFor({ state: 'visible', timeout: 25000 })
    .then(() => true)
    .catch(() => false);
  record(`${prefix}：恢复后 succeeded 且 commit 匹配才显示更新完成`, completed);
  record(`${prefix}：完成标题明确`, (await page.locator('[data-testid="update-job"] strong').innerText()) === '更新完成');
  record(`${prefix}：完成后提供显式重新加载按钮`, completed && (await reloadButton.count()) === 1);
  record(`${prefix}：完成不触发自动刷新`, navigations.length === 0, navigations.join(','));
  record(
    `${prefix}：成功后要求重新检查才能再次安装`,
    (await page.locator('[data-testid="update-recheck-inline"]').count()) === 1 &&
      ((await textOf(page, 'update-blocked')) || '').includes('重新检查'),
  );
  record(`${prefix}：完成后标注这是最近一次结果`, (await bodyText(page)).includes('这是最近一次更新的结果'));
  await page.waitForTimeout(4000);
  record(`${prefix}：完成之后也不反复刷新`, navigations.length === 0, navigations.join(','));
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：完成`);
  await shot(page, `${prefix}-done`);
}

async function scenarioFailed(page, state, prefix) {
  state.onPoll = (s, count) => {
    if (count === 1) s.job = makeJob({ state: 'running', stage: 'backup' });
    if (count === 2) {
      s.job = makeJob({
        state: 'failed',
        stage: 'deploy',
        error_code: 'deploy_failed',
        message: '容器退出码 1',
      });
    }
  };
  await openAbout(page);
  await runCheck(page);
  await fillConfirmToExact(page);
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：失败场景发出请求`, await waitFor(() => state.posts.length === 1, 10000));
  record(`${prefix}：失败后显示更新失败`, await waitForText(page, '更新失败', 20000));
  const body = await bodyText(page);
  record(`${prefix}：失败指向宿主日志`, body.includes('查看宿主更新日志'));
  record(`${prefix}：失败保留错误码与信息`, body.includes('deploy_failed') && body.includes('容器退出码 1'));
  record(`${prefix}：失败不承诺自动回滚`, body.includes('不会自动回滚'));
  record(`${prefix}：失败不显示更新完成`, !body.includes('更新完成'));
  record(
    `${prefix}：失败不显示重新加载按钮`,
    (await page.locator('[data-testid="update-reload"]').count()) === 0,
  );
  record(
    `${prefix}：失败后安装按钮被“需重新检查”禁用`,
    (await page.locator('[data-testid="update-install"]').isDisabled()) &&
      ((await textOf(page, 'update-blocked')) || '').includes('重新检查'),
  );
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：失败`);
  await shot(page, `${prefix}-failed`);
}

async function scenarioFailedRetry(page, state, prefix) {
  state.onPoll = (s, count) => {
    if (count === 1) s.job = makeJob({ state: 'running', stage: 'preflight' });
    if (count === 2) {
      s.job = makeJob({
        state: 'failed',
        stage: 'preflight',
        error_code: 'preflight_failed',
        message: '构建环境不可用',
      });
    }
  };
  await openAbout(page);
  await runCheck(page);
  await fillConfirmToExact(page);
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：predeploy 失败前只提交一次`, await waitFor(() => state.posts.length === 1, 10000));
  record(`${prefix}：predeploy 失败显示失败`, await waitForText(page, '更新失败', 20000));

  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：失败后安装仍被过期结果挡住`, await install.isDisabled());
  await page.locator('[data-testid="update-recheck-inline"]').click();
  record(`${prefix}：重新检查是显式动作`, await waitFor(() => state.checkRequests === 2, 10000), `check=${state.checkRequests}`);
  record(`${prefix}：重新检查会同时刷新当前版本`, state.versionRequests >= 2, `version=${state.versionRequests}`);
  record(
    `${prefix}：重新检查后允许再次确认`,
    await waitFor(async () => (await install.isEnabled().catch(() => false)) === true, 10000),
  );

  await fillConfirmToExact(page);
  // 键盘提交：输入完整 tag 后按 Enter。
  await page.locator('[data-testid="update-confirm-input"]').press('Enter');
  record(`${prefix}：重新确认后第二次 POST`, await waitFor(() => state.posts.length === 2, 10000), `posts=${state.posts.length}`);
  record(
    `${prefix}：第二次请求沿用同一目标`,
    deepEqual(state.posts[1]?.body?.expected, state.posts[0]?.body?.expected),
  );
  record(`${prefix}：第二次提交进入进度态`, await waitForText(page, '更新已排队', 10000));
  record(`${prefix}：没有多余的第三次 POST`, state.posts.length === 2, `posts=${state.posts.length}`);
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：失败重试`);
  await shot(page, `${prefix}-retry`);
}

async function scenarioHistoricalSuccess(page, state, prefix) {
  await openAbout(page);
  record(`${prefix}：旧 succeeded 任务如实恢复`, await waitForText(page, '更新完成', 10000));
  const body = await bodyText(page);
  record(`${prefix}：旧成功标注为历史结果`, body.includes('这是最近一次更新的结果'));
  record(`${prefix}：旧成功不显示安装按钮`, (await page.locator('[data-testid="update-install"]').count()) === 0);
  record(`${prefix}：旧成功不阻止再次检查`, body.includes('先检查最新 Release'));
  record(`${prefix}：历史结果没有自动提交`, state.posts.length === 0);
  record(
    `${prefix}：没有备份告警时不显示提示`,
    (await page.locator('[data-testid="backup-warnings"]').count()) === 0,
  );

  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(
    `${prefix}：历史成功不再永久禁用安装`,
    (await install.count()) === 1 && (await install.isEnabled()),
    await textOf(page, 'update-blocked'),
  );
  await fillConfirmToExact(page, 'v0.3.0');
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：第二次更新发出请求`, await waitFor(() => state.posts.length === 1, 10000));
  record(
    `${prefix}：第二次更新使用新目标与当前版本快照`,
    deepEqual(state.posts[0]?.body, {
      confirm_tag: 'v0.3.0',
      expected: {
        release_id: 43,
        tag: 'v0.3.0',
        commit: SECOND_TARGET,
        current_version: '0.2.0',
        current_commit: TARGET_COMMIT,
      },
    }),
    JSON.stringify(state.posts[0]?.body),
  );
  record(
    `${prefix}：第二次更新能走到完成`,
    await page
      .locator('[data-testid="update-reload"]')
      .waitFor({ state: 'visible', timeout: 30000 })
      .then(() => true)
      .catch(() => false),
  );
  record(`${prefix}：第二次更新仍只 POST 一次`, state.posts.length === 1);
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：旧成功再更新`);
  await shot(page, `${prefix}-second-update`);
}

/* ------------------------------------------- 升级成功 + 备份告警（task-76） */

/** 成功终态但备份不完整：告警来自 current.json 的可选字段 backup_warnings。 */
function backupWarningsJob() {
  return makeJob({
    request_id: 'req-warn',
    state: 'succeeded',
    stage: 'complete',
    backup_warnings: [
      'manifest: media_state=partial（media.tar.zst 归档时有文件正在写入，可能漏掉刚上传的媒体）',
      'manifest: keys_env_set=true（备份时 LING_ROOT_KEY 非空，恢复时必须同时恢复 deploy/.env）',
    ],
  });
}

async function scenarioSuccessWithBackupWarnings(page, state, prefix, navigations, events) {
  await openAbout(page);
  record(`${prefix}：升级成功仍然如实显示完成`, await waitForText(page, '更新完成', 15000));

  const banner = page.locator('[data-testid="backup-warnings"]');
  record(
    `${prefix}：备份告警提示可见`,
    (await banner.count()) === 1 && (await banner.first().isVisible()),
    `count=${await banner.count()}`,
  );
  const text = await textOf(page, 'backup-warnings');
  record(`${prefix}：提示说明媒体可能漏文件`, text.includes('媒体归档可能漏文件'), text);
  record(`${prefix}：提示要求恢复时人工核对`, text.includes('恢复时需人工核对'));
  record(`${prefix}：提示指向 verify-warnings.txt`, text.includes('verify-warnings.txt'));
  record(`${prefix}：标题明确“升级成功但备份不完整”`, text.includes('备份不完整'));

  // 位置要求：必须在结果区里、不在折叠区、也不是“只给一个日志链接”。
  const placement = await page.evaluate(() => {
    const node = document.querySelector('[data-testid="backup-warnings"]');
    const job = document.querySelector('[data-testid="update-job"]');
    const reload = document.querySelector('[data-testid="update-reload"]');
    if (!node || !job) return { inJob: false, collapsed: true, beforeReload: false, height: 0 };
    const collapsed = Boolean(node.closest('details') || node.closest('[hidden]'));
    const beforeReload = reload
      ? Boolean(node.compareDocumentPosition(reload) & Node.DOCUMENT_POSITION_FOLLOWING)
      : false;
    return {
      inJob: job.contains(node),
      collapsed,
      beforeReload,
      height: node.getBoundingClientRect().height,
    };
  });
  record(`${prefix}：提示在结果区里`, placement.inJob);
  record(`${prefix}：提示没有被折叠/隐藏`, placement.collapsed === false);
  record(`${prefix}：提示在“重新加载”之前（显著位置）`, placement.beforeReload);
  record(`${prefix}：提示实际占据版面`, placement.height > 20, `h=${placement.height}`);
  record(
    `${prefix}：告警条目逐条列出`,
    (await banner.locator('li').count()) === 2,
    `li=${await banner.locator('li').count()}`,
  );
  record(`${prefix}：没有“忽略/取消”这类绕过入口`, (await page.locator('button').allInnerTexts()).every((t) => !/忽略|取消/.test(t)));
  record(`${prefix}：没有自动提交`, state.posts.length === 0, `posts=${state.posts.length}`);
  record(`${prefix}：无 pageerror`, events.pageErrors.length === 0, events.pageErrors[0] || '');
  await assertNoOverflow(page, `${prefix}：备份告警`);
  await scrollToJob(page);
  await shot(page, `${prefix}-backup-warnings`, true);
}

async function scenarioNeedsManual(page, state, prefix) {
  state.job = makeJob({
    state: 'needs_manual',
    stage: 'verify',
    error_code: 'migration_failed',
    message: '数据库迁移失败，需要人工确认',
  });
  await openAbout(page);
  record(`${prefix}：刷新/重开时恢复 needs_manual 任务`, await waitForText(page, '需要人工处理', 10000));
  const body = await bodyText(page);
  record(`${prefix}：人工处理指向宿主日志与手动恢复`, body.includes('宿主机') && body.includes('手动处理'));
  record(`${prefix}：人工处理说明 adopt-current`, body.includes('adopt-current'));
  record(`${prefix}：明确不自动回滚、不自动恢复数据库`, body.includes('不会自动回滚') && body.includes('数据库也不会自动恢复'));
  record(`${prefix}：needs_manual 不显示完成`, !body.includes('更新完成'));
  record(
    `${prefix}：needs_manual 不显示安装按钮`,
    (await page.locator('[data-testid="update-install"]').count()) === 0,
  );
  const buttonTexts = await page.locator('button').allInnerTexts();
  record(
    `${prefix}：needs_manual 没有忽略/取消入口`,
    !buttonTexts.some((text) => text.includes('忽略') || text.trim() === '取消'),
    buttonTexts.join(' | '),
  );

  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：有目标时 needs_manual 禁用安装`, (await install.count()) === 1 && (await install.isDisabled()));
  record(
    `${prefix}：禁用原因指向人工处理`,
    (await textOf(page, 'update-blocked')).includes('人工处理'),
  );
  record(`${prefix}：needs_manual 场景没有提交`, state.posts.length === 0);
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：人工处理`);
  await shot(page, `${prefix}-needs-manual`);
}

async function scenarioResume(page, state, prefix) {
  state.job = makeJob({ state: 'queued', stage: 'queued' });
  await openAbout(page);
  record(`${prefix}：打开页面恢复进行中的任务`, await waitForText(page, '更新已排队', 10000));

  // 刷新页面：current_job 必须仍然恢复，而不是当成本地状态丢失。
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="check-release"]', { timeout: 20000 });
  record(`${prefix}：刷新后仍恢复 current_job`, await waitForText(page, '更新已排队', 10000));

  state.pollCount = 0;
  state.onPoll = (s, count) => {
    if (count === 1) s.job = makeJob({ state: 'running', stage: 'fetch' });
    if (count === 2) {
      s.job = makeJob({ state: 'succeeded', stage: 'complete' });
      s.version = updatedVersion();
    }
  };
  record(`${prefix}：恢复后继续自动轮询`, await waitForText(page, '正在更新', 12000));
  record(
    `${prefix}：恢复轮询后能走到更新完成`,
    await page
      .locator('[data-testid="update-reload"]')
      .waitFor({ state: 'visible', timeout: 25000 })
      .then(() => true)
      .catch(() => false),
  );
  record(`${prefix}：完成后版本接口已更新`, state.versionRequests > 1, `version=${state.versionRequests}`);
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：恢复`);
  await shot(page, `${prefix}-resume`);
}

async function scenarioPostConflict(page, state, prefix) {
  state.postFailure = {
    status: 409,
    body: {
      error: {
        code: 'conflict',
        message: 'release changed',
        retriable: false,
        detail: 'release_changed',
      },
    },
  };
  await openAbout(page);
  await runCheck(page);
  await fillConfirmToExact(page);
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：409 后显示原因`, await waitForText(page, 'Release 在你确认之后发生了变化', 10000));
  record(`${prefix}：409 要求重新检查`, await waitForText(page, '重新检查 Release', 5000));
  record(
    `${prefix}：409 后安装按钮保持禁用`,
    await page.locator('[data-testid="update-install"]').isDisabled(),
  );
  record(`${prefix}：409 不自动重发`, state.posts.length === 1, `posts=${state.posts.length}`);
  await assertNoOverflow(page, `${prefix}：冲突`);
  await shot(page, `${prefix}-conflict`);
}

async function scenarioConflictVersion(page, state, prefix) {
  state.postFailureOnce = {
    status: 409,
    body: {
      error: {
        code: 'conflict',
        message: 'installed version changed',
        retriable: false,
        detail: 'installed_version_changed',
      },
    },
    // 409 发生时，服务端的当前版本已经变了。
    after: (s) => {
      s.version = { ...s.version, current: '0.1.1', commit: NEW_CURRENT_COMMIT };
      s.release = { ...s.release, current: '0.1.1' };
    },
  };
  await openAbout(page);
  await runCheck(page);
  await fillConfirmToExact(page);
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：版本变化 409 显示原因`, await waitForText(page, '当前运行的版本在确认之后发生了变化', 10000));
  record(`${prefix}：版本变化 409 只提交一次`, state.posts.length === 1, `posts=${state.posts.length}`);
  const versionBefore = state.versionRequests;
  const checksBefore = state.checkRequests;
  await page.locator('[data-testid="update-recheck"]').click();
  record(`${prefix}：409 后重新检查会刷新当前版本`, await waitFor(() => state.versionRequests > versionBefore, 10000));
  record(`${prefix}：409 后重新检查会刷新 Release`, await waitFor(() => state.checkRequests > checksBefore, 10000));
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：重新检查后恢复可安装`, await waitFor(async () => (await install.isEnabled().catch(() => false)) === true, 10000));
  await fillConfirmToExact(page);
  await page.locator('[data-testid="update-confirm-input"]').press('Enter');
  record(`${prefix}：版本变化后第二次确认`, await waitFor(() => state.posts.length === 2, 10000), `posts=${state.posts.length}`);
  record(
    `${prefix}：第二次 expected 使用新的当前版本与提交`,
    deepEqual(state.posts[1]?.body, {
      confirm_tag: 'v0.2.0',
      expected: {
        release_id: 42,
        tag: 'v0.2.0',
        commit: TARGET_COMMIT,
        current_version: '0.1.1',
        current_commit: NEW_CURRENT_COMMIT,
      },
    }),
    JSON.stringify(state.posts[1]?.body),
  );
  record(
    `${prefix}：第一次仍使用旧版本快照`,
    state.posts[0]?.body?.expected?.current_version === '0.1.0' &&
      state.posts[0]?.body?.expected?.current_commit === CURRENT_COMMIT,
  );
  await assertNoOverflow(page, `${prefix}：版本变化冲突`);
  await shot(page, `${prefix}-conflict-version`);
}

async function scenarioPostLostRecovery(page, state, prefix) {
  await openAbout(page);
  await runCheck(page);
  await fillConfirmToExact(page);
  // POST 已落地但响应丢失；随后第一次状态查询也断网。
  state.postFailureOnce = { network: true, landed: true };
  state.offline = true;
  await page.locator('[data-testid="update-submit"]').click();

  record(`${prefix}：POST 响应丢失后显示“结果未知”`, await waitFor(async () => (await page.locator('[data-testid="update-pending"]').count()) === 1, 15000));
  const body = await bodyText(page);
  record(`${prefix}：结果未知时不谎报完成`, !body.includes('更新完成') && !body.includes('更新失败'));
  record(`${prefix}：响应丢失且未恢复时没有重复提交`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(`${prefix}：提交前的任务卡片隐藏，避免旧状态冒充本次结果`, (await page.locator('[data-testid="update-job"]').count()) === 0);
  record(
    `${prefix}：提供停止自动查询与立即查询`,
    (await page.locator('[data-testid="update-pending-pause"]').count()) === 1 &&
      (await page.locator('[data-testid="update-pending-retry"]').count()) === 1,
  );
  await page.locator('[data-testid="update-pending-pause"]').click();
  record(`${prefix}：可以暂停自动查询`, await waitForText(page, '已暂停自动查询', 5000));
  await page.locator('[data-testid="update-pending-pause"]').click();
  record(`${prefix}：可以继续自动查询`, await waitForText(page, '正在确认', 5000));

  // 恢复连接：只查状态，不重发；新任务被认领后进入进度态。
  state.offline = false;
  record(`${prefix}：恢复后接上本次提交的任务`, await waitForText(page, '正在更新', 25000));
  record(`${prefix}：恢复后仍只 POST 一次`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(`${prefix}：结果未知提示消失`, (await page.locator('[data-testid="update-pending"]').count()) === 0);
  record(
    `${prefix}：恢复后任务状态来自真实 GET`,
    ['queued', 'running'].includes(await jobState(page)),
  );

  state.job = makeJob({ state: 'succeeded', stage: 'complete' });
  state.version = updatedVersion();
  record(
    `${prefix}：恢复后能核对到更新完成`,
    await page
      .locator('[data-testid="update-reload"]')
      .waitFor({ state: 'visible', timeout: 30000 })
      .then(() => true)
      .catch(() => false),
  );
  record(`${prefix}：完成前后都没有重复提交`, state.posts.length === 1);
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：响应丢失恢复`);
  await shot(page, `${prefix}-post-lost`);
}

async function scenarioPostLostNotLanded(page, state, prefix) {
  await openAbout(page);
  record(`${prefix}：提交前仍是旧的 succeeded`, await waitForText(page, '更新完成', 10000));
  await runCheck(page);
  await fillConfirmToExact(page, 'v0.3.0');
  // 响应丢失且请求没有落地：状态持续返回提交前的旧 succeeded。
  state.postFailureOnce = { network: true, landed: false };
  await page.locator('[data-testid="update-submit"]').click();

  record(`${prefix}：响应丢失后进入待确认`, await waitFor(async () => (await page.locator('[data-testid="update-pending"]').count()) === 1, 15000));
  record(
    `${prefix}：旧 succeeded 不会被当成本次成功`,
    (await page.locator('[data-testid="update-reload"]').count()) === 0 &&
      !(await page.locator('[data-testid="update-job"]').count()),
  );
  record(`${prefix}：旧任务卡片在待确认期间隐藏`, (await page.locator('[data-testid="update-job"]').count()) === 0);
  record(`${prefix}：无法确认前不会重发`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(
    `${prefix}：持续查询后明确说明无法确认`,
    await waitForText(page, '没能确认这次提交是否已送达', 25000),
  );
  const body = await bodyText(page);
  record(`${prefix}：无法确认时不显示完成`, !body.includes('更新完成'));
  record(`${prefix}：无法确认时仍只有一次 POST`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(`${prefix}：无法确认时提供重新检查入口`, (await page.locator('[data-testid="update-recheck"]').count()) === 1);
  await assertNoOverflow(page, `${prefix}：未落地`);
  await shot(page, `${prefix}-post-lost-unresolved`, true);
  const statusBeforeRecheck = state.statusRequests;
  const checksBeforeRecheck = state.checkRequests;
  await page.locator('[data-testid="update-recheck"]').click();
  record(
    `${prefix}：重新检查会读取更新状态`,
    await waitFor(() => state.statusRequests > statusBeforeRecheck, 10000),
    `status=${state.statusRequests - statusBeforeRecheck}`,
  );
  record(
    `${prefix}：重新检查会刷新 Release`,
    await waitFor(() => state.checkRequests > checksBeforeRecheck, 10000),
    `check=${state.checkRequests - checksBeforeRecheck}`,
  );
  record(
    `${prefix}：一次 baseline 检查不能解锁普通安装`,
    (await page.locator('[data-testid="update-install"]').count()) === 0 &&
      (await page.locator('[data-testid="update-pending"]').count()) === 1,
  );
  record(`${prefix}：重新检查不会自动重发`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(
    `${prefix}：重新检查后仍保留“结果未知”的风险提示`,
    await waitForText(page, '没能确认这次提交是否已送达', 5000),
  );
  await shot(page, `${prefix}-post-lost-rechecked`, true);

  // 重查本身失败时也必须保持未知，不能因为“读不到”就解锁安装。
  state.offline = true;
  await page.locator('[data-testid="update-recheck"]').click();
  await page.waitForTimeout(1200);
  record(
    `${prefix}：重查失败仍然保留待确认与风险提示`,
    (await page.locator('[data-testid="update-pending"]').count()) === 1 &&
      (await page.locator('[data-testid="update-install"]').count()) === 0,
  );
  record(`${prefix}：重查失败不会重发`, state.posts.length === 1, `posts=${state.posts.length}`);
  state.offline = false;
  await page.waitForTimeout(300);

  // 真正未送达时的人工恢复：必须显式核对状态 + 额外风险确认后才允许再 POST。
  const postsBeforeResubmit = state.posts.length;
  await page.locator('[data-testid="update-resubmit"]').click();
  record(`${prefix}：人工重新提交先要求核对状态`, await waitForText(page, '重新提交前必须确认风险', 15000));
  record(`${prefix}：风险确认前不会自动重发`, state.posts.length === postsBeforeResubmit,
    `posts=${state.posts.length}`);
  record(`${prefix}：风险确认明确说明先前请求可能已被接受`,
    (await bodyText(page)).includes('可能已经被服务端接受'));
  const resubmitSubmit = page.locator('[data-testid="update-resubmit-submit"]');
  record(`${prefix}：未勾选风险时禁止重新提交`, await resubmitSubmit.isDisabled());
  await page.locator('[data-testid="update-resubmit-risk"]').check();
  await page.locator('[data-testid="update-resubmit-input"]').fill('v0.3.0');
  record(`${prefix}：勾选风险并输入 tag 后才允许提交`, await resubmitSubmit.isEnabled());
  await shot(page, `${prefix}-manual-resubmit-confirm`, true);
  state.postMode = 'normal';
  await resubmitSubmit.click();
  record(`${prefix}：显式人工恢复后发出第二次 POST`,
    await waitFor(() => state.posts.length === postsBeforeResubmit + 1, 15000),
    `posts=${state.posts.length}`);
  record(`${prefix}：人工恢复后待确认横幅消失`,
    await waitFor(async () => (await page.locator('[data-testid="update-pending"]').count()) === 0, 10000));
  record(`${prefix}：人工恢复后接上新的任务`,
    ['queued', 'running'].includes(await jobState(page)), `jobState=${await jobState(page)}`);
  record(`${prefix}：人工恢复只多一次 POST`, state.posts.length === 2, `posts=${state.posts.length}`);
  await assertNoOverflow(page, `${prefix}：人工重新提交`);
  await shot(page, `${prefix}-manual-resubmit`, true);
}

async function scenarioInitialOffline(page, state, prefix) {
  state.offline = true;
  await openAbout(page);
  record(`${prefix}：初次加载断连时说明正在重试`, await waitForText(page, '与服务端的连接已断开', 15000));
  record(`${prefix}：断连时没有版本信息`, (await page.locator('[data-testid="about-version-facts"]').count()) === 0);
  record(
    `${prefix}：断连时不显示完成/失败`,
    (await page.locator('[data-testid="update-reload"]').count()) === 0 &&
      !(await page.locator('[data-testid="update-job"]').count()),
  );
  record(
    `${prefix}：断连时提供立即重试与暂停`,
    (await page.locator('[data-testid="update-connection-retry"]').count()) === 1 &&
      (await page.locator('[data-testid="update-connection-pause"]').count()) === 1,
  );
  await shot(page, `${prefix}-offline`);

  state.offline = false;
  record(
    `${prefix}：恢复后重新拿到版本信息`,
    await waitFor(async () => (await page.locator('[data-testid="about-version-facts"]').count()) === 1, 25000),
  );
  record(`${prefix}：恢复后更新通道状态可见`, await waitForText(page, '已启用', 15000));
  record(`${prefix}：恢复后连接提示消失`, (await page.locator('[data-testid="update-connection"]').count()) === 0);
  record(`${prefix}：恢复不等于更新成功`, !(await bodyText(page)).includes('更新完成'));
  record(`${prefix}：恢复过程没有提交`, state.posts.length === 0);
  await assertNoOverflow(page, `${prefix}：初载恢复`);
  await shot(page, `${prefix}-initial-recovered`);
}

async function scenarioCommitMismatch(page, state, prefix) {
  state.job = makeJob({ state: 'succeeded', stage: 'complete' });
  state.version = defaultVersion();
  await openAbout(page);
  record(`${prefix}：succeeded 但 commit 未变时等待生效`, await waitForText(page, '等待新版本生效', 10000));
  let body = await bodyText(page);
  record(`${prefix}：commit 不匹配不显示更新完成`, !body.includes('更新完成'));
  record(`${prefix}：commit 不匹配不提供重新加载`, (await page.locator('[data-testid="update-reload"]').count()) === 0);
  record(`${prefix}：commit 不匹配明确显示当前与目标提交`, body.includes('7b23702') && body.includes('c0ffee1'));
  record(
    `${prefix}：自动核对版本接口`,
    await waitFor(() => state.versionRequests >= 2, 12000),
    `version=${state.versionRequests}`,
  );
  record(`${prefix}：版本未变前仍不误报完成`, !(await bodyText(page)).includes('更新完成'));

  state.version = updatedVersion();
  record(
    `${prefix}：真实 GET 到目标 commit 后才显示完成`,
    await page
      .locator('[data-testid="update-reload"]')
      .waitFor({ state: 'visible', timeout: 30000 })
      .then(() => true)
      .catch(() => false),
  );
  record(`${prefix}：匹配后标题为更新完成`, (await page.locator('[data-testid="update-job"] strong').innerText()) === '更新完成');
  await scrollToJob(page);
  await assertNoOverflow(page, `${prefix}：提交不匹配`);
  await shot(page, `${prefix}-commit-mismatch`);
}

/* ------------------------------------------------------------ F1 同状态新终态 */

async function scenarioTerminalSameStateSucceeded(page, state, prefix) {
  state.version = updatedVersion();
  state.release = nextRelease();
  state.job = makeJob({ request_id: 'req-old', state: 'succeeded', stage: 'complete' });
  state.status = { ...enabledStatus(), current_job: state.job };
  state.postMode = 'lost-landed-succeeded';
  state.postVersion = secondUpdatedVersion();

  await openAbout(page);
  record(`${prefix}：旧 succeeded 如实恢复`, await waitForText(page, '更新完成', 12000));
  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：重新检查后可安装`, (await install.count()) === 1 && (await install.isEnabled()));
  await fillConfirmToExact(page, 'v0.3.0');
  await page.locator('[data-testid="update-submit"]').click();

  record(
    `${prefix}：同状态但新 request_id 的 succeeded 被接上`,
    await waitFor(async () => (await jobRequestId(page)) === 'req-2', 25000) &&
      (await waitForText(page, '更新完成', 5000)),
    `requestId=${await jobRequestId(page)}`,
  );
  record(`${prefix}：恢复只发一次 POST`, state.posts.length === 1, `posts=${state.posts.length}`);
  const disabled = await install.isDisabled().catch(() => true);
  const inline = await page.locator('[data-testid="update-recheck-inline"]').count();
  const blocked = await textOf(page, 'update-blocked');
  record(`${prefix}：新 succeeded 终态必须重新检查才可再次安装`, disabled && (inline > 0 || blocked.includes('重新检查')),
    `disabled=${disabled}, inline=${inline}, blocked=${blocked}`);
  record(`${prefix}：旧 Release 快照被标记过期`, (await bodyText(page)).includes('Release 结果已过期'));
  record(`${prefix}：没有隐式触发新的 Release 检查`, state.checkRequests === 1, `check=${state.checkRequests}`);
  record(`${prefix}：当前版本没有被旧快照回退`, (await textOf(page, 'about-version-facts')).includes('0.3.0'));
  await assertNoOverflow(page, `${prefix}：同状态 succeeded`);
  await shot(page, `${prefix}-same-state-succeeded`, true);
}

async function scenarioTerminalSameStateFailed(page, state, prefix) {
  state.job = makeJob({
    request_id: 'req-old',
    state: 'failed',
    stage: 'deploy',
    error_code: 'deploy_failed',
    message: '旧任务失败',
  });
  state.status = { ...enabledStatus(), current_job: state.job };
  state.postMode = 'lost-landed-failed';

  await openAbout(page);
  record(`${prefix}：旧 failed 如实恢复`, await waitForText(page, '更新失败', 12000));
  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：重新检查后可安装`, (await install.count()) === 1 && (await install.isEnabled()));
  await fillConfirmToExact(page, 'v0.2.0');
  await page.locator('[data-testid="update-submit"]').click();

  record(
    `${prefix}：同状态但新 request_id 的 failed 被接上`,
    await waitFor(async () => (await jobRequestId(page)) === 'req-2', 25000) &&
      (await waitForText(page, '更新失败', 5000)),
    `requestId=${await jobRequestId(page)}`,
  );
  record(`${prefix}：恢复只发一次 POST`, state.posts.length === 1, `posts=${state.posts.length}`);
  const disabled = await install.isDisabled().catch(() => true);
  const inline = await page.locator('[data-testid="update-recheck-inline"]').count();
  record(`${prefix}：同状态新 failed 终态也必须重新检查`, disabled && inline > 0,
    `disabled=${disabled}, inline=${inline}, blocked=${await textOf(page, 'update-blocked')}`);
  await assertNoOverflow(page, `${prefix}：同状态 failed`);
  await shot(page, `${prefix}-same-state-failed`, true);
}

/* --------------------------------------------------------- F2 慢响应/旧副本乱序 */

async function scenarioStaleManualRefresh(page, state, prefix) {
  state.job = makeJob({ request_id: 'req-run', state: 'running', stage: 'build' });
  state.status = { ...enabledStatus(), current_job: state.job };
  await openAbout(page);
  record(`${prefix}：恢复 running`, await waitForText(page, '正在更新', 12000));

  armGate(state);
  state.holdStatus = 1;
  state.holdVersion = 1;
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  record(
    `${prefix}：手动刷新的响应被挂起（捕获旧 running/旧版本）`,
    await waitFor(() => state.heldStatus === 1 && state.heldVersion === 1, 8000),
    `heldStatus=${state.heldStatus}, heldVersion=${state.heldVersion}`,
  );
  state.job = makeJob({ request_id: 'req-run', state: 'succeeded', stage: 'complete' });
  state.status = { ...enabledStatus(), current_job: state.job };
  state.version = updatedVersion();
  const reloadVisible = await page
    .locator('[data-testid="update-reload"]')
    .waitFor({ state: 'visible', timeout: 25000 })
    .then(() => true)
    .catch(() => false);
  record(`${prefix}：轮询先拿到 succeeded`, reloadVisible);
  state.releaseGate();
  await page.waitForTimeout(700);

  record(`${prefix}：旧手动刷新不能把任务打回 running`, (await jobState(page)) === 'succeeded',
    `jobState=${await jobState(page)}`);
  record(`${prefix}：界面不再显示正在更新`, !(await bodyText(page)).includes('正在更新'));
  record(`${prefix}：旧版本快照不能回退当前版本`, (await textOf(page, 'about-version-facts')).includes('0.2.0'),
    await textOf(page, 'about-version-facts'));
  record(
    `${prefix}：完成态仍要求重新检查`,
    (await page.locator('[data-testid="update-install"]').isDisabled().catch(() => true)) ||
      (await page.locator('[data-testid="update-recheck-inline"]').count()) > 0,
  );
  await assertNoOverflow(page, `${prefix}：旧刷新`);
  await shot(page, `${prefix}-stale-refresh`, true);
}

async function scenarioStaleRunCheck(page, state, prefix) {
  state.job = makeJob({ request_id: 'req-run', state: 'running', stage: 'build' });
  state.status = { ...enabledStatus(), current_job: state.job };
  await openAbout(page);
  record(`${prefix}：恢复 running`, await waitForText(page, '正在更新', 12000));

  armGate(state);
  state.holdVersion = 1;
  await page.locator('[data-testid="check-release"]').click();
  record(`${prefix}：runCheck 的 version 响应被挂起`,
    await waitFor(() => state.heldVersion === 1, 8000), `heldVersion=${state.heldVersion}`);
  state.job = makeJob({ request_id: 'req-run', state: 'succeeded', stage: 'complete' });
  state.status = { ...enabledStatus(), current_job: state.job };
  state.version = updatedVersion();
  record(
    `${prefix}：轮询先拿到 succeeded`,
    await page.locator('[data-testid="update-reload"]').waitFor({ state: 'visible', timeout: 25000 })
      .then(() => true)
      .catch(() => false),
  );
  state.releaseGate();
  await page.waitForTimeout(500);
  record(`${prefix}：旧 runCheck 响应不能把完成打回等待生效`,
    !(await bodyText(page)).includes('等待新版本生效'));
  const install = page.locator('[data-testid="update-install"]');
  const disabled = await install.isDisabled().catch(() => true);
  const inline = await page.locator('[data-testid="update-recheck-inline"]').count();
  record(`${prefix}：终态后的 recheck 不被 runCheck 清掉`, disabled && inline > 0,
    `disabled=${disabled}, inline=${inline}, blocked=${await textOf(page, 'update-blocked')}`);
  record(`${prefix}：旧 version 没有把当前版本回退`, (await textOf(page, 'about-version-facts')).includes('0.2.0'));

  // 再检查一次时必须用新的 current version 组装 expected，而不是挂起时的旧 0.1.0。
  await page.locator('[data-testid="update-recheck-inline"]').click();
  record(
    `${prefix}：重新检查后恢复可安装`,
    await waitFor(async () => (await install.isEnabled().catch(() => false)) === true, 12000),
  );
  await fillConfirmToExact(page, 'v0.2.0');
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：再次确认只发一次 POST`, await waitFor(() => state.posts.length === 1, 10000),
    `posts=${state.posts.length}`);
  record(
    `${prefix}：expected 使用重新检查后的当前版本`,
    state.posts[0]?.body?.expected?.current_version === '0.2.0' &&
      state.posts[0]?.body?.expected?.current_commit === TARGET_COMMIT,
    JSON.stringify(state.posts[0]?.body?.expected),
  );
  await assertNoOverflow(page, `${prefix}：旧检查`);
  await shot(page, `${prefix}-stale-runcheck`, true);
}

/* ------------------------------------------------- F3 已落地但状态副本仍旧/人工恢复 */

async function scenarioStaleStatusCannotSecondPost(page, state, prefix) {
  const oldJob = makeJob({ request_id: 'req-old', state: 'succeeded', stage: 'complete' });
  state.version = updatedVersion();
  state.release = nextRelease();
  state.job = oldJob;
  state.status = { ...enabledStatus(), current_job: oldJob };
  state.postMode = 'lost-landed-running-stale';

  await openAbout(page);
  record(`${prefix}：旧 succeeded 恢复`, await waitForText(page, '更新完成', 12000));
  await runCheck(page);
  await fillConfirmToExact(page, 'v0.3.0');
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：POST 已落地但响应丢失，进入待确认`,
    await waitFor(async () => (await page.locator('[data-testid="update-pending"]').count()) === 1, 15000));
  record(`${prefix}：状态副本持续旧任务时最终保持“无法确认”`,
    await waitForText(page, '没能确认这次提交是否已送达', 60000));
  record(`${prefix}：恢复期间只发一次 POST`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(`${prefix}：不把旧 succeeded 当成本次成功`,
    (await page.locator('[data-testid="update-job"]').count()) === 0 &&
      (await page.locator('[data-testid="update-reload"]').count()) === 0);

  const postsBeforeRecheck = state.posts.length;
  const statusBeforeRecheck = state.statusRequests;
  const checksBeforeRecheck = state.checkRequests;
  await page.locator('[data-testid="update-recheck"]').click();
  record(`${prefix}：重查包含更新状态`,
    await waitFor(() => state.statusRequests > statusBeforeRecheck, 10000),
    `status=${state.statusRequests - statusBeforeRecheck}`);
  record(`${prefix}：重查同时刷新 Release`,
    await waitFor(() => state.checkRequests > checksBeforeRecheck, 10000));
  await page.waitForTimeout(1200);
  record(`${prefix}：重查仍 baseline 不能解锁普通安装`,
    (await page.locator('[data-testid="update-install"]').count()) === 0 &&
      (await page.locator('[data-testid="update-pending"]').count()) === 1);
  record(`${prefix}：重查不能发出第二 POST`, state.posts.length === postsBeforeRecheck,
    `posts=${state.posts.length}`);
  await shot(page, `${prefix}-stale-status-rechecked`, true);

  // 后续状态副本追上新任务：只查询就应接上同一个 request_id。
  state.staleStatusJob = null;
  await page.locator('[data-testid="update-pending-retry"]').click();
  record(`${prefix}：后续状态出现新任务时恢复 running`,
    await waitFor(async () => (await jobState(page)) === 'running', 20000),
    `jobState=${await jobState(page)}`);
  record(`${prefix}：恢复后仍只有一次 POST`, state.posts.length === 1, `posts=${state.posts.length}`);
  state.job = { ...state.job, state: 'succeeded', stage: 'complete' };
  state.status = { ...enabledStatus(), current_job: state.job };
  state.version = secondUpdatedVersion();
  record(`${prefix}：后续新任务能走到更新完成`,
    await page.locator('[data-testid="update-reload"]').waitFor({ state: 'visible', timeout: 25000 })
      .then(() => true)
      .catch(() => false));
  record(`${prefix}：全流程仍只有一次 POST`, state.posts.length === 1);
  await assertNoOverflow(page, `${prefix}：旧状态副本`);
  await shot(page, `${prefix}-stale-status-recovered`, true);
}

async function scenarioForeignTargetNotAttached(page, state, prefix) {
  const oldJob = makeJob({ request_id: 'req-old', state: 'succeeded', stage: 'complete' });
  state.version = updatedVersion();
  state.release = nextRelease();
  state.job = oldJob;
  state.status = { ...enabledStatus(), current_job: oldJob };
  state.postMode = 'lost-no-land';

  await openAbout(page);
  record(`${prefix}：旧 succeeded 恢复`, await waitForText(page, '更新完成', 12000));
  await runCheck(page);
  await fillConfirmToExact(page, 'v0.3.0');
  state.staleStatusJob = makeJob({
    request_id: 'req-other',
    state: 'succeeded',
    stage: 'complete',
    target_tag: 'v0.9.0',
    target_commit: 'f'.repeat(40),
  });
  await page.locator('[data-testid="update-submit"]').click();
  record(`${prefix}：响应丢失后进入待确认`,
    await waitFor(async () => (await page.locator('[data-testid="update-pending"]').count()) === 1, 15000));
  await page.waitForTimeout(4000);
  record(`${prefix}：不同目标的新 request_id 不被当成本次提交`,
    (await page.locator('[data-testid="update-job"]').count()) === 0 &&
      (await page.locator('[data-testid="update-reload"]').count()) === 0);
  record(`${prefix}：不同目标不会自动提交`, state.posts.length === 1, `posts=${state.posts.length}`);
  record(`${prefix}：不同目标场景仍保留未知提示`,
    await waitForText(page, '没能确认这次提交是否已送达', 60000));

  await page.locator('[data-testid="update-resubmit"]').click();
  record(`${prefix}：不同目标会阻塞人工重新提交`,
    await waitForText(page, '目标不同的更新任务', 15000), await textOf(page, 'update-resubmit-blocked'));
  record(`${prefix}：不同目标不会产生第二 POST`, state.posts.length === 1, `posts=${state.posts.length}`);
  await assertNoOverflow(page, `${prefix}：不同目标`);
  await shot(page, `${prefix}-foreign-target`, true);
}

/* ----------------------------------------------------------------- F4 文案一致 */

async function scenarioStatusReasonConsistency(page, state, prefix) {
  state.status = {
    ...enabledStatus(),
    reason: 'update_in_progress',
    current_job: null,
  };
  await openAbout(page);
  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：过期 update_in_progress 不阻断安装`,
    (await install.count()) === 1 && (await install.isEnabled().catch(() => false)));
  const note = await textOf(page, 'update-status-reason');
  record(`${prefix}：状态栏不再说“已有更新正在进行”`, note.includes('没有可核实的任务') && !note.includes('已有更新正在进行'), note);
  record(`${prefix}：按钮与状态栏不矛盾`, (await bodyText(page)).includes('安装门禁按最新任务状态判断'));
  await assertNoOverflow(page, `${prefix}：文案一致`);
  await shot(page, `${prefix}-status-reason`, true);
}

/* ----------------------------------------------------------- 卸载/取消不新发轮询 */

async function scenarioUnmountStopsPolling(page, state, prefix) {
  state.job = makeJob({ request_id: 'req-run', state: 'running', stage: 'build' });
  state.status = { ...enabledStatus(), current_job: state.job };
  await openAbout(page);
  record(`${prefix}：恢复 running`, await waitForText(page, '正在更新', 12000));
  await page.locator('.back-link').click();
  await page.waitForSelector('[data-testid="open-about"]', { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(1200);
  const statusAfterLeave = state.statusRequests;
  const versionAfterLeave = state.versionRequests;
  await page.waitForTimeout(4500);
  record(`${prefix}：卸载后不再轮询状态`, state.statusRequests === statusAfterLeave,
    `extra=${state.statusRequests - statusAfterLeave}`);
  record(`${prefix}：卸载后不再轮询版本`, state.versionRequests === versionAfterLeave,
    `extra=${state.versionRequests - versionAfterLeave}`);
}

/* ---------------------------------------------- 门禁原因：manual_required / protocol_unsupported */

/** 门禁态不允许出现“绕过门禁”的入口：忽略/取消/强制/自动重试。 */
async function assertNoGateBypass(page, prefix) {
  const buttons = await page.locator('button').allInnerTexts();
  record(
    `${prefix}：没有忽略/取消/强制/自动重试入口`,
    !buttons.some((text) => /忽略|取消|强制|自动重试/.test(text)),
    buttons.map((text) => text.trim()).filter(Boolean).join(' | '),
  );
  record(
    `${prefix}：没有待确认/人工重新提交面板`,
    (await page.locator('[data-testid="update-pending"]').count()) === 0 &&
      (await page.locator('[data-testid="update-resubmit-panel"]').count()) === 0 &&
      (await page.locator('[data-testid="update-pending-retry"]').count()) === 0,
  );
}

async function runGateReasonScenario(page, state, prefix, events, options) {
  const { reason, expected, shotName } = options;
  await openAbout(page);
  const noteVisible = await waitFor(
    async () => (await textOf(page, 'update-status-reason')).length > 0,
    15000,
  );
  record(`${prefix}：状态栏给出当前状态`, noteVisible);

  const statusNote = await textOf(page, 'update-status-reason');
  record(
    `${prefix}：状态栏显示专门文案`,
    expected.every((text) => statusNote.includes(text)),
    statusNote,
  );
  record(
    `${prefix}：状态栏不回显原样码`,
    !statusNote.includes(reason) && !(await bodyText(page)).includes(reason),
    `raw=${reason}`,
  );

  await runCheck(page);
  const install = page.locator('[data-testid="update-install"]');
  record(`${prefix}：有可安装目标`, (await install.count()) === 1);
  record(`${prefix}：安装按钮被门禁禁用`, await install.isDisabled());
  const blocked = await textOf(page, 'update-blocked');
  record(
    `${prefix}：按钮旁给出同一门禁原因`,
    expected.every((text) => blocked.includes(text)),
    blocked,
  );
  record(`${prefix}：按钮旁不回显原样码`, !blocked.includes(reason), blocked);

  // 禁用按钮点不开确认区：门禁不是“提示一下仍可继续”。
  await install.click({ force: true }).catch(() => {});
  await page.waitForTimeout(400);
  record(
    `${prefix}：禁用按钮不会打开确认区`,
    (await page.locator('[data-testid="update-confirm"]').count()) === 0,
  );
  await assertNoGateBypass(page, prefix);
  record(`${prefix}：全程没有更新请求`, state.posts.length === 0, `posts=${state.posts.length}`);
  record(`${prefix}：无 pageerror`, events.pageErrors.length === 0, events.pageErrors[0] || '');
  await assertNoOverflow(page, `${prefix}：门禁`);
  await shot(page, `${prefix}-${shotName}`, true);
}

async function scenarioGateManualRequired(page, state, prefix, navigations, events) {
  await runGateReasonScenario(page, state, prefix, events, {
    reason: 'manual_required',
    // 语义：执行器必须先在宿主机人工核验（adopt-current）才能解除，重试没用。
    expected: ['人工核验', 'adopt-current'],
    shotName: 'gate-manual-required',
  });
}

async function scenarioGateProtocolUnsupported(page, state, prefix, navigations, events) {
  await runGateReasonScenario(page, state, prefix, events, {
    reason: 'protocol_unsupported',
    // 语义：API 与执行器需同批升级，重试不会生效。
    expected: ['同批升级', '重试不会生效', '协议不兼容'],
    shotName: 'gate-protocol-unsupported',
  });
}

/* -------------------------------------------- 提交路径 503（provider_unavailable + 门禁 detail） */

async function runSubmitGateScenario(page, state, prefix, events, options) {
  const { detail, expected, rawCodes, shotName } = options;
  state.postFailure = {
    status: 503,
    body: {
      error: {
        code: 'provider_unavailable',
        message: 'update gate rejected',
        retriable: false,
        detail,
      },
    },
  };
  await openAbout(page);
  await runCheck(page);
  await fillConfirmToExact(page);
  await page.locator('[data-testid="update-submit"]').click();
  record(
    `${prefix}：只提交一次`,
    await waitFor(() => state.posts.length === 1, 10000),
    `posts=${state.posts.length}`,
  );

  const banner = page.locator('[data-testid="update-submit-error"]');
  record(
    `${prefix}：503 后出现提交错误横幅`,
    await banner.waitFor({ state: 'visible', timeout: 15000 }).then(() => true).catch(() => false),
  );
  const bannerText = await textOf(page, 'update-submit-error');
  record(
    `${prefix}：横幅显示门禁文案`,
    expected.every((text) => bannerText.includes(text)),
    bannerText,
  );
  record(`${prefix}：横幅提示不要重复提交`, bannerText.includes('不要重复提交'));

  const body = await bodyText(page);
  record(`${prefix}：页面不出现「模型供应商」`, !body.includes('模型供应商'));
  record(`${prefix}：页面不出现「密钥」`, !body.includes('密钥'));
  record(
    `${prefix}：不回显原样错误码`,
    rawCodes.every((code) => !body.includes(code)),
    rawCodes.join(','),
  );

  // 门禁拒绝不能自动重试：等过一个轮询周期后仍然只有一次 POST。
  await page.waitForTimeout(4200);
  record(`${prefix}：不会自动重试`, state.posts.length === 1, `posts=${state.posts.length}`);
  const buttons = await page.locator('button').allInnerTexts();
  record(
    `${prefix}：没有忽略/强制/自动重试入口`,
    !buttons.some((text) => /忽略|强制|自动重试/.test(text)),
    buttons.map((text) => text.trim()).filter(Boolean).join(' | '),
  );
  record(`${prefix}：无 pageerror`, events.pageErrors.length === 0, events.pageErrors[0] || '');
  await assertNoOverflow(page, `${prefix}：503 门禁`);
  await shot(page, `${prefix}-${shotName}`, true);
}

async function scenarioSubmitGateManualRequired(page, state, prefix, navigations, events) {
  await runSubmitGateScenario(page, state, prefix, events, {
    detail: 'manual_required',
    expected: ['人工核验', 'adopt-current'],
    rawCodes: ['manual_required', 'provider_unavailable'],
    shotName: 'submit-503-gate-manual',
  });
}

async function scenarioSubmitGateProtocolUnsupported(page, state, prefix, navigations, events) {
  await runSubmitGateScenario(page, state, prefix, events, {
    detail: 'protocol_unsupported',
    expected: ['同批升级', '重试不会生效'],
    rawCodes: ['protocol_unsupported', 'provider_unavailable'],
    shotName: 'submit-503-gate-protocol',
  });
}

/* ------------------------------------------------------------------- main */

async function runViewport(browser, viewport, name) {
  console.log(`\n==== ${name} ${viewport.width}×${viewport.height} ====`);
  const historicalJob = makeJob({
    request_id: 'req-old',
    state: 'succeeded',
    stage: 'complete',
  });
  const historicalState = () =>
    makeState({
      version: { ...defaultVersion(), current: '0.2.0', commit: TARGET_COMMIT },
      release: nextRelease(),
      status: { ...enabledStatus(), current_job: historicalJob },
      job: historicalJob,
      // 按状态推进，避免依赖轮询计数：queued → running → succeeded。
      onPoll: (s) => {
        if (s.job?.state === 'queued') {
          s.job = { ...s.job, state: 'running', stage: 'build' };
        } else if (s.job?.state === 'running') {
          s.job = { ...s.job, state: 'succeeded', stage: 'complete' };
          s.version = secondUpdatedVersion();
        }
      },
    });

  const scenarios = [
    ['no-release', scenarioNoRelease, makeState({ release: noRelease() })],
    ['disabled', scenarioDisabled, makeState()],
    ['install', scenarioInstallFlow, makeState()],
    ['failed', scenarioFailed, makeState()],
    ['failed-retry', scenarioFailedRetry, makeState()],
    ['historical-success', scenarioHistoricalSuccess, historicalState()],
    ['needs-manual', scenarioNeedsManual, makeState()],
    [
      'success-backup-warnings',
      scenarioSuccessWithBackupWarnings,
      makeState({
        // 服务端已经报告新提交 → describeJob 判“更新完成”，同时带备份告警。
        version: { ...defaultVersion(), current: '0.2.0', commit: TARGET_COMMIT },
        status: { ...enabledStatus(), current_job: backupWarningsJob() },
        job: backupWarningsJob(),
      }),
    ],
    ['resume', scenarioResume, makeState()],
    ['conflict', scenarioPostConflict, makeState()],
    ['conflict-version', scenarioConflictVersion, makeState()],
    ['post-lost', scenarioPostLostRecovery, makeState()],
    ['post-lost-old-success', scenarioPostLostNotLanded, historicalState()],
    ['initial-offline', scenarioInitialOffline, makeState()],
    ['commit-mismatch', scenarioCommitMismatch, makeState()],
    ['terminal-same-succeeded', scenarioTerminalSameStateSucceeded, makeState()],
    ['terminal-same-failed', scenarioTerminalSameStateFailed, makeState()],
    ['stale-refresh', scenarioStaleManualRefresh, makeState()],
    ['stale-runcheck', scenarioStaleRunCheck, makeState()],
    ['stale-status-no-second-post', scenarioStaleStatusCannotSecondPost, makeState()],
    ['foreign-target', scenarioForeignTargetNotAttached, makeState()],
    ['status-reason-consistency', scenarioStatusReasonConsistency, makeState()],
    ['gate-manual-required', scenarioGateManualRequired, makeState({ status: gateStatus('manual_required') })],
    [
      'gate-protocol-unsupported',
      scenarioGateProtocolUnsupported,
      makeState({ status: gateStatus('protocol_unsupported') }),
    ],
    ['submit-503-gate-manual', scenarioSubmitGateManualRequired, makeState()],
    ['submit-503-gate-protocol', scenarioSubmitGateProtocolUnsupported, makeState()],
    ['unmount-no-poll', scenarioUnmountStopsPolling, makeState()],
  ];

  const postLost = scenarios.find(([key]) => key === 'post-lost');
  if (postLost) {
    postLost[2].onPoll = (s) => {
      if (s.job?.state === 'queued') s.job = { ...s.job, state: 'running', stage: 'fetch' };
    };
  }

  const only = (process.env.SCENARIOS ?? '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  const selected = only.length
    ? scenarios.filter(([key]) => only.some((item) => key.includes(item)))
    : scenarios;
  if (only.length) console.log(`定向复跑：${selected.map(([key]) => key).join(', ') || '(无匹配)'}`);

  for (const [key, run, state] of selected) {
    const label = `${name}/${key}`;
    const { context, page, consoleErrors, pageErrors, navigations } = await newHarness(
      browser,
      viewport,
    );
    await attachMocks(context, state);
    try {
      await run(page, state, label, navigations, { pageErrors });
    } catch (error) {
      record(`${label}：脚本执行`, false, error instanceof Error ? error.message : String(error));
    } finally {
      if (state.violations.length) {
        record(`${label}：POST 请求约束`, false, state.violations.join('；'));
      }
      record(`${label}：无未捕获的前端异常`, consoleErrors.length === 0, consoleErrors[0] || '');
      await context.close();
    }
  }
}

async function waitServerReady(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${BASE.origin}/`);
      if (response.ok) return true;
    } catch {
      /* 还没起来 */
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
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
