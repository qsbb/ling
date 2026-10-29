/**
 * W0 Web 浏览器实测（桌面 1440×900 + 手机 375×667）。
 *
 * 覆盖 Web 端的核心验收门槛：
 *  登录 → 发送 → 逐段流式 → 停止 → 失败提示与重试 → 刷新历史 →
 *  会话切换 → 设置发布生效 → 手机尺寸可用（无遮挡）。
 *
 * 用法（后端需已运行在 BASE_URL）：
 *   node w0_browser.mjs
 *
 * 输出：逐项 PASS/FAIL 与截图路径；任一项失败则以非 0 退出。
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:8080';

// 先等就绪再开跑。
// 不等的话，服务刚重启时会出"假失败"：登录拿到 200、紧接着的
// PUT /v1/settings/draft 却 401（cookie 尚未生效/进程还在初始化），
// 失败注入发不出去 → 重试按钮永远不出现 → 超时。
// 这种失败看起来像产品缺陷，实际是测试竞态，浪费排查时间。
async function waitReady(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${BASE_URL}/readyz`);
      if (r.ok) return true;
    } catch {
      /* 还没起来 */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}
const EXECUTABLE = process.env.CHROME_PATH ||
  process.env.CHROME_PATH;
const USER = process.env.LING_USER || 'ling';
const PASS = process.env.LING_PASS || 'ling-dev-password';
const SHOTS = process.env.SHOTS_DIR || path.resolve('shots');
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
function record(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
}

/**
 * 因**环境**原因无法验证的项（例如模型供应商此刻不可用）。
 *
 * 与 PASS 分开记：既不能算通过（那是假绿），也不该算失败（界面没坏）。
 * 汇总里单列出来，谁看都知道"这条没验成，原因是环境"。
 */
function skip(name, why) {
  results.push({ name, ok: null, detail: why });
  console.log(`SKIP  ${name} — ${why}`);
}
async function shot(page, name) {
  const file = path.join(SHOTS, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  return file;
}

async function login(page) {
  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  // 等**界面就绪**，不用固定延时。
  // 实测：解析 285KB JS + React 挂载约 2.1s，固定等 600ms 会错过登录框，
  // 于是后续 fill 全部落空、`hasComposer` 为 0 → "手机：登录成功" 假失败。
  // 固定延时在不同视口/设备像素比下本来就会漂。
  await page
    .waitForFunction(
      () =>
        !!document.querySelector('input[type="password"]') ||
        !!document.querySelector('textarea'),
      { timeout: 20000 },
    )
    .catch(() => {});
  await page.waitForTimeout(200);
  const pwd = page.locator('input[type="password"]').first();
  if (await pwd.count() && await pwd.isVisible().catch(() => false)) {
    await page.locator('input[name="username"], input').first().fill(USER);
    await pwd.fill(PASS);
    await page.locator('button[type="submit"]').first().click();
    // 等登录完成：密码框消失 或 输入框出现
    await page.waitForFunction(
      () => !document.querySelector('input[type="password"]'),
      { timeout: 15000 },
    ).catch(() => {});
    await page.waitForTimeout(800);
  }

  // 登录后默认落到「总览」（她此刻怎样），而这一组用例测的是聊天链路，
  // 所以显式进聊天页。
  //
  // **用应用内导航（点 ling 标识），不要 page.goto**：goto 会整页重载，
  // SPA 里已加载的会话列表全丢，composer 要等重新拉完才可用——实测
  // 发送按钮 30 秒都点不到。点链接则只切路由。
  // **不能用"有没有 textarea"判断是否在聊天页**：总览页上聊天组件仍挂在
  // DOM 里（实测 3 个 textarea），判断会永远为假，于是停在不带发送按钮的
  // 总览页，后续点击超时。用发送按钮本身作为信号。
  if ((await page.locator('button[aria-label="发送消息"]').count()) === 0) {
    const brand = page.locator('button.brand').first();
    if (await brand.count()) await brand.click();
    await page
      .waitForFunction(() => !!document.querySelector('textarea'), { timeout: 20000 })
      .catch(() => {});
    // 等发送按钮真的可点（会话就绪之前它是 disabled）
    await page
      .waitForSelector('button[aria-label="发送消息"]:not([disabled])', { timeout: 20000 })
      .catch(() => {});
    await page.waitForTimeout(400);
  }

  const stillLogin = await page.locator('input[type="password"]').count();
  const hasComposer = await page.locator('textarea').count();
  return stillLogin === 0 && hasComposer > 0;
}

/**
 * 等待服务端空闲（没有进行中的轮次）。
 *
 * fake provider 很快，真实模型可能要几十秒；脚本不能假设固定时长。
 */
async function waitUntilIdle(page, sessionId, { timeout = 180000 } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const active = await page.evaluate(async (sid) => {
      const r = await fetch(`/v1/sessions/${sid}/messages`, { credentials: 'same-origin' });
      if (!r.ok) return null;
      return (await r.json()).active_turn;
    }, sessionId);
    if (!active) return true;
    await page.waitForTimeout(1000);
  }
  return false;
}

/** 取当前会话 id（从 URL 或接口）。 */
async function currentSessionId(page) {
  const fromUrl = await page.evaluate(() => {
    const m = location.pathname.match(/\/s\/([^/?]+)/);
    return m ? m[1] : null;
  });
  if (fromUrl) return fromUrl;
  return page.evaluate(async () => {
    const r = await fetch('/v1/sessions', { credentials: 'same-origin' });
    if (!r.ok) return null;
    const d = await r.json();
    return d.sessions?.[0]?.session_id ?? null;
  });
}

/** 打开会话侧栏（窄屏默认隐藏，需要点菜单按钮）。 */
async function openSidebar(page) {
  const menu = page.locator('button[aria-label="打开会话列表"]').first();
  if (await menu.count() && await menu.isVisible().catch(() => false)) {
    await menu.click();
    await page.waitForTimeout(500);
  }
}

async function composer(page) {
  return page.locator('textarea[aria-label="消息输入框"], textarea').first();
}

async function sendMessage(page, text) {
  const box = await composer(page);
  await box.waitFor({ state: 'visible', timeout: 15000 });
  await box.click();
  await box.fill(text);
  await page.locator('button[aria-label="发送消息"]').first().click();
}

/** 等待流式结束：读取消息区最后一个角色气泡的文本稳定下来。 */
async function waitForReply(page, { timeout = 30000 } = {}) {
  const start = Date.now();
  let last = '';
  let stableCount = 0;
  while (Date.now() - start < timeout) {
    const text = await page.evaluate(() => {
      const nodes = Array.from(document.querySelectorAll('[data-role="actor"], .message.actor, .msg-actor'));
      return nodes.length ? nodes[nodes.length - 1].textContent || '' : '';
    });
    // **空气泡不算"稳定"**。角色气泡在第一个 token 到达前是空的，
    // 原来的写法会把空字符串判成"连续稳定 3 次"直接返回，于是
    // "收到回复"这条用例偶发拿到长度 0（实测出现 1/20 的概率）。
    // 空的时候继续等，把稳定判定的预算留给真正有内容的时候。
    if (!text) {
      last = '';
      stableCount = 0;
      await page.waitForTimeout(250);
      continue;
    }
    if (text === last) {
      stableCount += 1;
      if (stableCount >= 3) return text;
    } else {
      stableCount = 0;
    }
    last = text;
    await page.waitForTimeout(250);
  }
  return last;
}

/**
 * 从**服务端**取最后一条角色消息的正文。
 *
 * 为什么不只看 DOM：流式过程中气泡里会先出现「正在回应…」占位符，
 * `waitForReply` 会把这个占位符当成回复返回——于是用例"通过"，
 * 但通过的是占位符不是回复（实测抓到过长度 14 的假通过）。
 * 轮次结束后以服务端落库的文本为准，断言的才是真的。
 */
async function lastActorText(page, sessionId) {
  return page.evaluate(async (sid) => {
    const r = await fetch(`/v1/sessions/${sid}/messages`, { credentials: 'same-origin' });
    if (!r.ok) return '';
    const data = await r.json();
    const actors = (data.messages || []).filter((m) => m.role === 'actor');
    return actors.length ? actors[actors.length - 1].text || '' : '';
  }, sessionId);
}

/**
 * 按**内容**去服务端找承载这条消息的会话，并等它产生角色回复。
 *
 * 为什么不用 URL 里的 sessionId：移动端发送前停在总览，发送瞬间会新建
 * 会话并跳转；读 URL 的时机稍早就会拿到"空会话"，于是永远读到 0 条消息
 * （实测就这么假失败过）。以服务端数据为准，时序问题就不存在了。
 */
async function waitForReplyByText(page, sentText, { timeout = 60000 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const found = await page.evaluate(async (needle) => {
      const r = await fetch('/v1/sessions', { credentials: 'same-origin' });
      if (!r.ok) return null;
      const list = (await r.json()).sessions || [];
      for (const s of list.slice(0, 5)) {
        const mr = await fetch(`/v1/sessions/${s.session_id}/messages`, {
          credentials: 'same-origin',
        });
        if (!mr.ok) continue;
        const msgs = (await mr.json()).messages || [];
        if (!msgs.some((m) => m.role === 'user' && (m.text || '').includes(needle))) continue;
        const actors = msgs.filter((m) => m.role === 'actor');
        return {
          sessionId: s.session_id,
          actorCount: actors.length,
          last: actors.length ? actors[actors.length - 1].text || '' : '',
          total: msgs.length,
        };
      }
      return null;
    }, sentText);
    if (found && found.last.length > 0) return found;
    await page.waitForTimeout(1000);
  }
  return null;
}

async function runDesktop(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const consoleErrors = [];
  // 未登录时前端会探测 /v1/auth/me 并收到 401，这是预期行为，不计为前端异常
  const isExpectedAuthProbe = (text) => /401 \(Unauthorized\)/.test(text);
  page.on('console', (m) => {
    if (m.type() === 'error' && !isExpectedAuthProbe(m.text())) consoleErrors.push(m.text());
  });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));

  const loggedIn = await login(page);
  record('桌面：登录成功', loggedIn);
  await shot(page, 'desktop-01-chat');

  // 测试模式断言只在 fake provider 下有意义：
  // - test_mode=true 时必须显示横幅（不能把假回复当模型能力）
  // - test_mode=false 时必须不显示（真实模型不应被标成测试）
  const mode = await page.evaluate(async () => {
    const r = await fetch('/v1/auth/me', { credentials: 'same-origin' });
    if (!r.ok) return null;
    return (await r.json()).test_mode;
  });
  // 必须按横幅元素判断：历史消息里可能残留 fake 时代的"测试模式"字样
  const bannerVisible = await page.evaluate(() => {
    const el = document.querySelector('.test-banner, [role="status"]');
    if (!el) return false;
    return (el.textContent || '').includes('测试模式');
  });
  if (mode === true) {
    record('桌面：测试模式横幅可见', bannerVisible);
  } else {
    record('桌面：真实模式不显示测试横幅', bannerVisible === false, `test_mode=${mode}`);
  }

  // 1) 发送 + 流式
  await sendMessage(page, '你好，我想试试流式回复');
  await page.waitForTimeout(400);
  const mid = await page.textContent('body');
  const streamingVisible = /生成中|正在输入|停止/.test(mid || '');
  record('桌面：发送后进入生成状态', streamingVisible);
  const reply = await waitForReply(page);
  record('桌面：收到逐段流式回复', reply.length > 5, `长度 ${reply.length}`);
  if (mode === true) {
    record('桌面：回复带测试模式标记', /测试模式/.test(reply));
  } else {
    record('桌面：真实模型回复不带 fake 标记', !/测试模式·确定性/.test(reply));
  }
  await shot(page, 'desktop-02-reply');

  // 2) 刷新后历史仍在
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  const afterReload = await page.textContent('body');
  record('桌面：刷新后历史恢复', /你好，我想试试流式回复/.test(afterReload || ''));
  await shot(page, 'desktop-03-reload');

  // 3) 停止生成（先确保上一轮已结束，否则会与停止冲突）
  const sidForStop = await currentSessionId(page);
  if (sidForStop) await waitUntilIdle(page, sidForStop);
  await sendMessage(page, '讲一个很长的故事吧，越长越好');
  await page.waitForTimeout(900);
  const stopBtn = page.locator('button:has-text("停止")').first();
  const canStop = await stopBtn.count() > 0 && await stopBtn.isVisible().catch(() => false);
  record('桌面：生成中可见停止按钮', canStop);
  if (canStop) {
    await stopBtn.click();
    // 轮询到"已停止/已完成"出现，而不是固定等待：
    // 真实模型可能已经跑完（显示已完成），也可能走 5 秒兜底（显示已停止），
    // 固定时长要么太短要么错过观察窗口。
    let settled = '';
    let stillGenerating = true;
    for (let i = 0; i < 16; i += 1) {
      await page.waitForTimeout(600);
      const actorText = await page.evaluate(() => {
        const els = Array.from(document.querySelectorAll('[data-role="actor"]'));
        const last = els[els.length - 1];
        return last ? (last.textContent || '') : '';
      });
      const placeholder =
        (await page.locator('.composer__input').getAttribute('placeholder')) || '';
      stillGenerating = /正在回应|生成中/.test(placeholder);
      if (!stillGenerating && /已完成|已停止|继续这一轮/.test(actorText)) {
        settled = actorText.slice(-40);
        break;
      }
    }
    record(
      '桌面：停止后离开生成状态',
      !stillGenerating && settled !== '',
      stillGenerating ? '仍在生成' : `已收敛：${settled}`,
    );
  }
  await shot(page, 'desktop-04-stopped');

  // 4) 会话切换
  const sidForSwitch = await currentSessionId(page);
  if (sidForSwitch) await waitUntilIdle(page, sidForSwitch);
  await openSidebar(page);
  const newChatBtn = page.locator('[data-testid="new-session"], button:has-text("新建对话")').first();
  const canNew = await newChatBtn.count() > 0;
  if (canNew) {
    await newChatBtn.click();
    await page.waitForTimeout(1500);
    // 只看消息区：侧栏会列出历史会话标题，不能拿整页文本判断
    const fresh = await page.evaluate(() => {
      const area = document.querySelector('.messages, [aria-label="对话"]');
      return area ? area.textContent || '' : '';
    });
    const hasBubbles = await page.evaluate(
      () => document.querySelectorAll('[data-role="user"], [data-role="actor"]').length,
    );
    record('桌面：新建会话后是空白对话', hasBubbles === 0, `剩余气泡 ${hasBubbles}`);
    await shot(page, 'desktop-05-new-session');
  } else {
    record('桌面：新建会话按钮存在', false, '未找到新对话按钮');
  }

  record('桌面：无未捕获的前端异常', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '));
  await context.close();
}

async function runMobile(browser) {
  const context = await browser.newContext({
    viewport: { width: 375, height: 667 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  const loggedIn = await login(page);
  record('手机：登录成功', loggedIn);
  await shot(page, 'mobile-01-chat');

  const sidMobile = await currentSessionId(page);
  if (sidMobile) await waitUntilIdle(page, sidMobile);
  const mobileText = '手机上也要能聊天';
  await sendMessage(page, mobileText);
  // 以服务端为准：按消息内容找到承载它的会话，再等角色回复出现。
  // 不依赖 URL 或 DOM 的时序（移动端发送会新建会话并跳转，读早一步就空）。
  const hit = await waitForReplyByText(page, mobileText);
  const sidAfter = hit ? hit.sessionId : null;
  let reply = hit ? hit.last : '';

  // 模型供应商偶发不可用（真实的 429/超时），真实用户会点重试。
  // 这里也重试一次，避免把"环境抖动"记成"界面坏了"——但**只重试一次**，
  // 重试还失败就如实判失败，不能把持续性故障赖过去。
  if (reply.length === 0) {
    const retryBtn = page.locator('button:has-text("重试")').first();
    if (await retryBtn.count()) {
      await retryBtn.click().catch(() => {});
      const again = await waitForReplyByText(page, mobileText);
      reply = again ? again.last : '';
    }
  }

  const diag = sidAfter
    ? await page.evaluate(async (sid) => {
        const r = await fetch(`/v1/sessions/${sid}/messages`, { credentials: 'same-origin' });
        const d = await r.json();
        const msgs = d.messages || [];
        return {
          total: msgs.length,
          last: msgs.length ? `${msgs[msgs.length - 1].role}/${msgs[msgs.length - 1].state}` : 'none',
          active: d.active_turn || null,
        };
      }, sidAfter)
    : { total: 0, last: 'no-session', active: null };
  const pageText = (await page.locator('body').textContent()) || '';
  const providerDown = /供应商不可用|model_unavailable/.test(pageText);
  if (reply.length > 10) {
    record('手机：发送并收到回复', true, `长度 ${reply.length}`);
  } else if (providerDown) {
    // 环境问题：模型供应商此刻不可用。界面本身是对的（它显示了可执行的错误
    // 提示与重试按钮），所以既不算通过也不算失败——单列 SKIP。
    skip('手机：发送并收到回复', '模型供应商不可用（环境问题，界面已给出可执行提示）');
  } else {
    record(
      '手机：发送并收到回复',
      false,
      `长度 ${reply.length}；服务端消息 ${diag.total} 条，最后一条 ${diag.last}，进行中 ${diag.active || '无'}`,
    );
  }
  await shot(page, 'mobile-02-reply');

  await openSidebar(page);
  const newBtnMobile = page.locator('[data-testid="new-session"]').first();
  const mobileCanNew = await newBtnMobile.count() > 0 && await newBtnMobile.isVisible().catch(() => false);
  record('手机：会话列表可打开且能新建', mobileCanNew);
  const scrim = page.locator('button[aria-label="关闭会话列表"]').first();
  if (await scrim.count()) { await scrim.click().catch(() => {}); await page.waitForTimeout(400); }

  // 输入区不被遮挡、可点击
  const box = await composer(page);
  const boxVisible = await box.isVisible().catch(() => false);
  const bbox = boxVisible ? await box.boundingBox() : null;
  const inViewport = bbox ? (bbox.y + bbox.height) <= 667 && bbox.y >= 0 : false;
  record('手机：输入框在视口内且可交互', boxVisible && inViewport,
    bbox ? `y=${Math.round(bbox.y)} h=${Math.round(bbox.height)}` : 'no bbox');

  // 横向不溢出
  const overflowX = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  record('手机：无横向溢出', overflowX <= 1, `overflowX=${overflowX}`);

  await context.close();
}

async function runFailureRetry(browser) {
  // 用一个"坏供应商"验证错误提示与重试：通过 API 发布失败配置，再在 UI 上操作。
  // 结束后必须恢复原配置：这个脚本可能跑在真实模型实例上，不能把它留在测试模式。
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await login(page);

  const cookies = await context.cookies();
  const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join('; ');
  const original = await fetch(`${BASE_URL}/v1/settings/config`, {
    headers: { Cookie: cookieHeader },
  }).then((r) => r.json());
  const restore = async () => {
    if (original && typeof original.revision === 'number') {
      await fetch(`${BASE_URL}/v1/settings/rollback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
        body: JSON.stringify({ to_revision: original.revision }),
      }).catch(() => {});
    }
    // 校验：回滚目标本身可能是上次失败留下的污染版本
    // （绑到 broken 会让后续所有轮次撞熔断，表现为"整页都坏了"）。
    const now = await fetch(`${BASE_URL}/v1/settings/config`, {
      headers: { Cookie: cookieHeader },
    }).then((r) => r.json()).catch(() => null);
    const stillBroken = JSON.stringify(now || {}).includes('"provider_id":"broken"')
      && !JSON.stringify(now || {}).includes('"provider_id":"fake"');
    if (stillBroken || !now) {
      await fetch(`${BASE_URL}/v1/settings/draft`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
        body: JSON.stringify({
          providers: [{ provider_id: 'fake', kind: 'fake', enabled: true }],
          profiles: [{ profile: 'dialogue.fast', provider_id: 'fake', model_id: 'deterministic' }],
          test_mode: true,
        }),
      }).catch(() => {});
      await fetch(`${BASE_URL}/v1/settings/publish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
        body: JSON.stringify({}),
      }).catch(() => {});
    }
  };

  try {
  const draft = await fetch(`${BASE_URL}/v1/settings/draft`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
    body: JSON.stringify({
      providers: [
        { provider_id: 'fake', kind: 'fake', enabled: true },
        { provider_id: 'broken', kind: 'fake', enabled: true, extra: { mode: 'failing' } },
      ],
      profiles: [{ profile: 'dialogue.fast', provider_id: 'broken', model_id: 'deterministic' }],
      test_mode: true,
    }),
  }).then((r) => r.json());
  await fetch(`${BASE_URL}/v1/settings/publish`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
    body: JSON.stringify({ revision: draft.revision }),
  });

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await sendMessage(page, '这条应该失败');
  await page.waitForTimeout(3000);
  const body = await page.textContent('body');
  const hasError = /失败|错误|不可用|重试/.test(body || '');
  record('桌面：模型故障有可执行提示', hasError);
  await shot(page, 'desktop-06-failure');

  const retryBtn = page.locator('button:has-text("重试")').first();
  const hasRetry = await retryBtn.count() > 0 && await retryBtn.isVisible().catch(() => false);
  record('桌面：失败后可见重试按钮', hasRetry);

  // 恢复良好配置后重试
  const draft2 = await fetch(`${BASE_URL}/v1/settings/draft`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
    body: JSON.stringify({
      profiles: [{ profile: 'dialogue.fast', provider_id: 'fake', model_id: 'deterministic' }],
    }),
  }).then((r) => r.json());
  await fetch(`${BASE_URL}/v1/settings/publish`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Cookie: cookieHeader },
    body: JSON.stringify({ revision: draft2.revision }),
  });

  if (hasRetry) {
    // 熔断器在连续失败后会冷却 20 秒（这是有意的保护：不反复打挂掉的供应商）。
    // 恢复配置后必须等冷却结束，否则重试会立刻被判"已熔断"。
    await page.waitForTimeout(21000);
    await retryBtn.click();
    const reply = await waitForReply(page, { timeout: 60000 });
    record('桌面：重试后收到正常回复', reply.length > 10, `长度 ${reply.length}`);
    await shot(page, 'desktop-07-retried');
  }
  } finally {
    // 必须在 finally 里：中途任何断言失败或异常都不能把实例留在熔断状态，
    // 否则后续跑批会集体假失败（实测踩过）。
    await restore();
    await context.close();
  }
}


/**
 * 总览页：登录后的首屏。
 *
 * 这一页之前完全没有 E2E 覆盖——登录辅助会直接跳到聊天，总览坏了也测不出来。
 * 它现在是默认落地页，必须锁住三个最容易坏的属性：
 * 页面能开、六块都在、**空态和故障态长得不一样**。
 */
async function runDashboard(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on('pageerror', (e) => consoleErrors.push(String(e)));

  // **不能用 login() 辅助**：它为了让后续用例能操作聊天，会在登录后点
  // 标识跳进聊天页——落地页当场就看不到了。这里自己做一次干净登录。
  await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
  await page
    .waitForFunction(() => !!document.querySelector('input[type="password"]'), { timeout: 20000 })
    .catch(() => {});
  await page.locator('input[name="username"], input').first().fill(USER);
  await page.locator('input[type="password"]').first().fill(PASS);
  await page.locator('button[type="submit"]').first().click();
  await page
    .waitForFunction(() => !document.querySelector('input[type="password"]'), { timeout: 15000 })
    .catch(() => {});
  await page.waitForTimeout(1200);
  record('总览：登录成功', (await page.locator('input[type="password"]').count()) === 0);

  // 登录后应当**落在总览**，而不是直接把聊天框推到人脸上。
  //
  // 断言必须发生在**任何兜底导航之前**。之前写成"不是总览就点进去，
  // 然后断言 URL 是总览"——那个断言永远为真，等于没测（负例验过：把登录
  // 落地页改回聊天，它照样 PASS）。
  const landedUrl = page.url();
  record('总览：这里是默认落地页', /\/dashboard/.test(landedUrl), landedUrl);

  // 兜底只为继续测其它项，不掩盖上面的失败
  if (!/\/dashboard/.test(page.url())) {
    const navBtn = page.locator('button.header-link:has-text("总览")').first();
    if (await navBtn.count()) await navBtn.click();
  }
  await page.waitForTimeout(1500);

  const body = (await page.textContent('body')) || '';
  const titles = ['此刻', '情绪', '主动联系', '最近记住的事', '关系', '渠道与模型'];
  const missing = titles.filter((x) => !body.includes(x));
  record('总览：六块卡片都在', missing.length === 0, missing.join(','));

  // 每块都要能标出"更新于"，否则等于假装实时
  record('总览：标注了数据更新时间', /更新于\s*\d{1,2}:\d{2}/.test(body));

  // 空态必须是"确实没有"，不能和"读不到"混成一句
  const hasEmpty = /当前没有登记中的活动|暂时没有状态记录|当前没有待处理的候选/.test(body);
  const hasUnavailable = /暂时读不到这块数据/.test(body);
  record('总览：空态与故障态可区分', hasEmpty || hasUnavailable,
    hasEmpty ? '空态' : hasUnavailable ? '故障态' : '两者都没有');

  // 常见故障：整页变成错误页
  record('总览：没有整页错误', !/读取失败|无法加载/i.test(body));

  // 顶栏能回到聊天
  const brand = page.locator('button.brand').first();
  if (await brand.count()) await brand.click();
  await page.waitForSelector('button[aria-label="发送消息"]:not([disabled])', { timeout: 15000 }).catch(() => {});
  record('总览：点标识能进聊天', (await page.locator('button[aria-label="发送消息"]').count()) > 0);

  record('总览：无未捕获的前端异常', consoleErrors.length === 0, consoleErrors[0] || '');
  await shot(page, 'dashboard-01-overview');
  await context.close();
}

async function runSettingsPage(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await login(page);
  const settingsBtn = page.locator('button.header-link:has-text("设置")').first();
  if (await settingsBtn.count()) {
    await settingsBtn.click();
    await page.waitForTimeout(1200);
    const body = await page.textContent('body');
    record('桌面：设置页可打开且显示职责绑定',
      /dialogue\.fast|职责|模型/.test(body || ''));
    const leaks = /sk-[A-Za-z0-9]{8,}/.test(body || '');
    record('桌面：设置页不回显密钥原文', !leaks);
    await shot(page, 'desktop-08-settings');
  } else {
    record('桌面：设置入口存在', false);
  }
  await context.close();
}

if (!(await waitReady())) {
  console.error(`服务未就绪：${BASE_URL}/readyz`);
  process.exit(1);
}

const browser = await chromium.launch({ executablePath: EXECUTABLE });
try {
  await runDesktop(browser);
  await runMobile(browser);
  await runFailureRetry(browser);
  await runSettingsPage(browser);
  await runDashboard(browser);
} finally {
  await browser.close();
}

const failed = results.filter((r) => r.ok === false);
const skipped = results.filter((r) => r.ok === null);
const passed = results.filter((r) => r.ok === true);
console.log('\n==== 汇总 ====');
console.log(`通过 ${passed.length}/${results.length}${skipped.length ? `（另有 ${skipped.length} 项因环境跳过）` : ''}`);
if (skipped.length) {
  console.log('跳过（环境原因，未验证）：');
  for (const s of skipped) console.log(` - ${s.name}：${s.detail}`);
}
if (failed.length) {
  console.log('失败项：');
  for (const f of failed) console.log(` - ${f.name} ${f.detail}`);
  process.exit(1);
}
console.log(skipped.length ? '其余全部通过' : '全部通过');
