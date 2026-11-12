/**
 * 「她这次看到了什么」的真实浏览器验证。
 *
 * 仓库没有 jsdom，SSR 只能断言"画出来是什么"，测不到点击、Esc、焦点回归
 * 和 375px 的横向溢出。这里用 playwright 起一个**真的 Vite 开发服务器** +
 * 真 Chromium，`/v1/**` 全部由 `page.route` 模拟（不碰真后端、不写库）。
 *
 * 覆盖：
 * - 点击入口 → 真的发出 `GET /v1/messages/{id}/prompt` → 抽屉打开；
 * - 焦点进抽屉、Esc / 遮罩 / X / 再点入口四种收起方式，焦点回到入口；
 * - 404（已归档的消息）→「这次没有记录」；
 * - 非 owner 不渲染入口；
 * - 375 与 1440 的抽屉宽度与页面横向溢出（数字由断言给出，不手写）。
 *
 * 没有 playwright 浏览器时这一组标成 skipped（不是静默 passed）并打印原因：
 * `pnpm vitest run` 在没装浏览器的机器上仍然全绿，但不会假装测过。
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const CREATED = '2026-11-07T04:23:00Z';

let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let baseUrl = '';
let launchError: string | null = null;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  try {
    server = await createServer({
      configFile: false,
      root: ROOT,
      plugins: [react()],
      logLevel: 'silent',
      server: { host: '127.0.0.1', port: 0 },
    });
    await server.listen();
    const address = server.httpServer?.address();
    if (!address || typeof address === 'string') {
      throw new Error('Vite 没有给出可用的监听地址');
    }
    baseUrl = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch();
  } catch (error) {
    launchError = error instanceof Error ? error.message : String(error);
  }
}, 90_000);

afterEach(async () => {
  while (contexts.length > 0) {
    await contexts.pop()?.close();
  }
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

/**
 * 没有浏览器时把这个用例标成 skipped（而不是静默 passed）：
 * "4 passed" 里混着一条没跑过的浏览器测试，比直接写 skipped 更容易骗人。
 */
function skip(ctx: { skip: () => void }): boolean {
  if (!launchError) return false;
  console.warn(`[prompt.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟数据 */

const USER = (role: 'owner' | 'member') => ({
  id: 'user-1',
  username: 'demo-user',
  role,
  subject_id: 'subject-owner',
  display_name: '主人',
});

const SESSION = {
  schema_version: 'v1',
  session_id: 's1',
  actor_id: 'actor_ling',
  subject_id: 'subject-owner',
  window_id: 's1',
  title: '新的对话',
  created_at: CREATED,
  updated_at: CREATED,
  message_count: 20,
  archived: false,
};

function chatMessage(
  messageId: string,
  role: 'user' | 'actor',
  text: string,
  sequence: number,
  turnId: string | null = 'turn-1',
) {
  return {
    schema_version: 'v1',
    message_id: messageId,
    turn_id: turnId,
    role,
    text,
    state: 'completed',
    created_at: CREATED,
    sequence,
    origin: 'web',
    error_code: null,
    model_label: null,
  };
}

const MESSAGES = [
  chatMessage('msg-user', 'user', '在吗', 1),
  chatMessage('msg-actor', 'actor', '在。', 2),
  chatMessage('msg-archived', 'actor', '这条已经归档了', 3, null),
  ...Array.from({ length: 18 }, (_, index) =>
    chatMessage(`fill-${index}`, 'user', `填充第 ${index + 1} 条，让对话列表能滚动`, 4 + index),
  ),
];

const PROMPT = {
  state: 'available',
  message_id: 'msg-actor',
  turn_id: 'turn-1',
  created_at: CREATED,
  expires_at: '2026-11-14T04:23:00Z',
  model: { provider_id: 'hy3gw2', model_id: 'hy3', profile: 'chat' },
  token_total: 12345,
  truncated: true,
  sections: [
    {
      key: 'system',
      label: '她是谁',
      role: 'system',
      tokens: 1024,
      collapsed: false,
      items: [{ role: 'system', sender: null, content: '你是灵。\n保持安静。', at: null }],
    },
    {
      key: 'memory',
      label: '她记得的事',
      role: null,
      tokens: 321,
      collapsed: false,
      items: [{ role: null, sender: '记忆管家', content: '你喜欢在晚上聊天。', at: null }],
    },
    {
      key: 'history',
      label: '最近聊过什么',
      role: null,
      tokens: 4321,
      collapsed: false,
      items: Array.from({ length: 14 }, (_, index) => ({
        role: index % 2 === 0 ? 'user' : 'actor',
        sender: null,
        content: `历史第 ${index + 1} 条`,
        at: null,
      })),
    },
    {
      key: 'input',
      label: '你这次说的话',
      role: null,
      tokens: 12,
      collapsed: false,
      items: [{ role: 'user', sender: null, content: '在吗', at: null }],
    },
  ],
};

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return {
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  };
}

interface Harness {
  page: Page;
  promptRequests: string[];
}

async function openChat(
  role: 'owner' | 'member',
  viewport: { width: number; height: number },
  options: { expectEntry?: boolean; prompt?: Record<string, unknown> } = {},
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  // 关掉动画：滑入动画会让 getBoundingClientRect 读到动画中途的位置，
  // 测量就不再是"稳定态宽度"。顺带也验证了 prefers-reduced-motion 分支。
  const context = await browser.newContext({ viewport, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const promptRequests: string[] = [];

  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === '/v1/auth/me') {
      return route.fulfill(
        json({ schema_version: 'v1', user: USER(role), config_revision: 1, test_mode: false, csrf_token: 'csrf' }),
      );
    }
    if (path === '/v1/version') {
      return route.fulfill(json({ current: '0.0.3' }));
    }
    if (path === '/v1/persona') {
      return route.fulfill(json({ name: '灵', display_name: '灵' }));
    }
    if (path === '/v1/sessions') {
      return route.fulfill(json({ sessions: [SESSION] }));
    }
    if (path === '/v1/sessions/s1/messages') {
      return route.fulfill(json({ messages: MESSAGES, active_turn: null }));
    }
    if (path.startsWith('/v1/messages/') && path.endsWith('/prompt')) {
      promptRequests.push(path);
      if (path === '/v1/messages/msg-archived/prompt') {
        return route.fulfill(json({ error: { code: 'not_found', message: '目标不存在', retriable: false } }, 404));
      }
      return route.fulfill(json(options.prompt ?? PROMPT));
    }
    if (path.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: path, retriable: false } }, 404));
    }
    return route.continue();
  });

  await page.goto(`${baseUrl}/s/s1`, { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="message-scroll"]').waitFor();
  if (options.expectEntry ?? true) {
    await page.locator('[data-testid="prompt-entry"]').first().waitFor();
  }
  return { page, promptRequests };
}

async function openDrawer(page: Page): Promise<void> {
  await page.getByTestId('prompt-entry').first().click();
  await page.getByTestId('prompt-drawer').waitFor();
  await page.locator('[data-testid="prompt-snapshot"]').waitFor();
}

async function activeTestId(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.getAttribute('data-testid') ?? null);
}

/**
 * 抽屉打开时，入口中心点上**命中的真实元素**是什么。
 *
 * 只断言"Playwright 点得到"是不够的：入口被她的话挤到右侧之后，曾经正好被
 * 从右边滑出的面板整个压住（1440 下入口占 1244–1338，面板从 950.4 起）——
 * 点得"到"是靠 Playwright 自己滚/重试碰运气，而 `document.elementFromPoint`
 * 会老老实实返回面板。这条断言让遮挡在下一次布局改动时立刻红。
 */
interface HitReport {
  entryCenter: { x: number; y: number };
  hit: string | null;
  isEntry: boolean;
  panelLeft: number;
}

async function entryHitPoint(page: Page, index = 0): Promise<HitReport> {
  return page.evaluate((i) => {
    const entry = document.querySelectorAll<HTMLElement>('[data-testid="prompt-entry"]')[i];
    if (!entry) throw new Error(`没有第 ${i} 个 prompt-entry`);
    const rect = entry.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;
    const hit = document.elementFromPoint(x, y);
    const panel = document.querySelector('[data-testid="prompt-drawer"]');
    return {
      entryCenter: { x, y },
      hit: hit ? `${hit.tagName.toLowerCase()}.${String(hit.className)}`.trim() : null,
      isEntry: hit !== null && hit.closest('[data-testid="prompt-entry"]') === entry,
      panelLeft: panel ? panel.getBoundingClientRect().left : Number.NaN,
    };
  }, index);
}

/**
 * 断言"抽屉打开时入口仍然真的露在外面、点得到"：
 * - 中心点命中入口本身（或它的子节点），不是任何 `prompt-drawer__*`；
 * - 入口整块不越过面板左边缘（视觉上不压在面板上）。
 */
async function expectEntryReachable(page: Page, index = 0): Promise<HitReport> {
  const report = await entryHitPoint(page, index);
  const entry = await page
    .getByTestId('prompt-entry')
    .nth(index)
    .evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return { left: rect.left, right: rect.right };
    });
  expect(report.isEntry).toBe(true);
  expect(entry.right).toBeLessThanOrEqual(report.panelLeft + 0.5);
  // 明确再挡一次"是抽屉的某一层吃了指针"
  expect(report.hit ?? '').not.toContain('prompt-drawer__');
  return report;
}


/* ------------------------------------------------------------ 测试 */

describe('真实浏览器：入口、请求、抽屉', () => {
  const TIMEOUT = 20_000;

  it(
    '点击入口 → 请求发出 → 抽屉打开，分区顺序 / token / 折叠 / 截断都对',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page, promptRequests } = await openChat('owner', { width: 1440, height: 900 });
      expect(promptRequests).toEqual([]);

      // 入口只在她（actor）的消息下：msg-actor + msg-archived 两条
      expect(await page.getByTestId('prompt-entry').count()).toBe(2);

      await openDrawer(page);
      expect(promptRequests).toEqual(['/v1/messages/msg-actor/prompt']);

      const dialog = page.getByTestId('prompt-drawer');
      expect(await dialog.getAttribute('role')).toBe('dialog');
      expect(await dialog.getAttribute('aria-modal')).toBe('true');

      const keys = await page
        .getByTestId('prompt-section')
        .evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-key')));
      expect(keys).toEqual(['system', 'memory', 'history', 'input']);

      // 折叠默认生效：history 只露 2 条，并给「展开全部（14 条）」
      expect(await page.getByText('历史第 1 条').count()).toBe(1);
      expect(await page.getByText('历史第 14 条').count()).toBe(0);
      const toggle = page.getByTestId('prompt-section-toggle');
      expect(await toggle.innerText()).toBe('展开全部（14 条）');
      await toggle.click();
      expect(await page.getByText('历史第 14 条').count()).toBe(1);
      expect(await toggle.innerText()).toBe('收起');
      await toggle.click();
      expect(await page.getByText('历史第 14 条').count()).toBe(0);

      expect(await page.getByTestId('prompt-token-total').innerText()).toContain('12,345');
      expect(await page.getByTestId('prompt-truncated').innerText()).toContain(
        '只保留了开头和最后几条',
      );
      expect(await page.getByText('记忆管家：').count()).toBe(1);
      // 换行保留在文本节点里（white-space: pre-wrap）
      expect(await page.getByText('你是灵。').innerText()).toContain('\n');
      expect(await dialog.getByRole('region', { name: '工具回灌' }).count()).toBe(0);
    },
    TIMEOUT,
  );

  it('显示实际回灌步骤、失败结果和截断提示', async (ctx) => {
    if (skip(ctx)) return;
    const content = '{"outcome":"failed","error":"<b>资料未找到</b>"}';
    const { page } = await openChat('owner', { width: 1440, height: 900 }, {
      prompt: {
        ...PROMPT,
        tool_transcript: [{
          step: 2,
          provider_call_id: 'call-fixture',
          tool_id: 'knowledge.search',
          outcome: 'failed',
          content,
          tokens: 12,
        }],
        tool_transcript_truncated: true,
      },
    });
    await openDrawer(page);

    const tool = page.getByRole('region', { name: '工具回灌' });
    await tool.getByText('第 2 步 · knowledge.search · failed').click();
    expect(await tool.getByText(content).isVisible()).toBe(true);
    expect(await tool.locator('b').count()).toBe(0);
    expect(await tool.getByText('部分工具结果超过快照容量，未保留。').isVisible()).toBe(true);
  }, TIMEOUT);

  it(
    '四种收起方式：Esc / X / 遮罩 / 再点入口；焦点进抽屉、关闭后回入口',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page } = await openChat('owner', { width: 1440, height: 900 });

      // 1) Esc：焦点一开始就在关闭键上；收起后回到入口
      await openDrawer(page);
      expect(await activeTestId(page)).toBe('prompt-drawer-close');
      await page.keyboard.press('Escape');
      await page.getByTestId('prompt-drawer').waitFor({ state: 'detached' });
      await expect.poll(() => activeTestId(page)).toBe('prompt-entry');

      // 2) X
      await openDrawer(page);
      await page.getByTestId('prompt-drawer-close').click();
      await page.getByTestId('prompt-drawer').waitFor({ state: 'detached' });

      // 3) 点遮罩区域（面板左侧）收起——遮罩本身 pointer-events:none，
      //    真正的判定在 document click 捕获里，所以用坐标点而不是点元素。
      await openDrawer(page);
      await page.getByTestId('prompt-scrim').waitFor();
      await page.mouse.click(200, 300);
      await page.getByTestId('prompt-drawer').waitFor({ state: 'detached' });

      // 4) 再点入口 = 收起。遮罩不接指针，所以入口是真的点得到。
      await openDrawer(page);
      /*
       * 防回归：点之前先证明入口真的露在面板外面。
       * 她的话贴右（own = role === 'actor'），入口也跟着贴右；曾经抽屉打开时
       * 面板正好把入口整个盖住，这一条会直接指出"命中 prompt-drawer__body"。
       */
      const reachable = await expectEntryReachable(page);
      console.info(
        `[prompt.browser] 抽屉打开时入口中心 (${reachable.entryCenter.x.toFixed(1)}, ` +
          `${reachable.entryCenter.y.toFixed(1)}) 命中 ${reachable.hit}，` +
          `面板 left=${reachable.panelLeft.toFixed(1)}`,
      );
      await page.getByTestId('prompt-entry').first().click();
      await page.getByTestId('prompt-drawer').waitFor({ state: 'detached' });
      // 收起后让位内边距也要撤掉，对话区回到满宽
      expect(
        await page.evaluate(
          () =>
            getComputedStyle(document.querySelector('[data-testid="message-scroll"]') as HTMLElement)
              .paddingRight,
        ),
      ).toBe('40px');
    },
    TIMEOUT,
  );

  it(
    '404（已归档的消息）→「这次没有记录」；非 owner 没有入口',
    async (ctx) => {
      if (skip(ctx)) return;
      const owner = await openChat('owner', { width: 1440, height: 900 });
      await owner.page.getByTestId('prompt-entry').nth(1).click();
      await owner.page.getByTestId('prompt-missing').waitFor();
      expect(await owner.page.getByTestId('prompt-missing').innerText()).toContain('这次没有记录');
      expect(owner.promptRequests).toEqual(['/v1/messages/msg-archived/prompt']);
      expect(await owner.page.locator('[data-testid="prompt-section"]').count()).toBe(0);

      const member = await openChat('member', { width: 1440, height: 900 }, { expectEntry: false });
      expect(await member.page.getByTestId('prompt-entry').count()).toBe(0);
    },
    /*
     * 这一条开两个浏览器上下文，而**每个上下文的首屏都要在 Vite 冷启动 +
     * 全量并行（十几个浏览器测试同时在跑）下实例化一份新的依赖图**——
     * 单独跑约 4.5s，全量跑到这一条时实测要 15s 上下，最坏一次卡在等
     * 依赖图就吃掉了 20s。非 owner 用例本身值得留，所以抬高这一条的上限，
     * 而不是删掉它或悄悄放宽全局超时。
     */
    45_000,
  );

  it(
    '1440 / 375 宽度与横向溢出，且背景没有被锁死',
    async (ctx) => {
      if (skip(ctx)) return;
      const { page } = await openChat('owner', { width: 1440, height: 900 });
      await openDrawer(page);

      const measure = () =>
        page.evaluate(() => {
          const panel = document.querySelector('[data-testid="prompt-drawer"]') as HTMLElement;
          const drawerBody = document.querySelector(
            '[data-testid="prompt-drawer-body"]',
          ) as HTMLElement;
          const rect = panel.getBoundingClientRect();
          return {
            innerWidth: window.innerWidth,
            docClientWidth: document.documentElement.clientWidth,
            docScrollWidth: document.documentElement.scrollWidth,
            bodyClientWidth: document.body.clientWidth,
            bodyScrollWidth: document.body.scrollWidth,
            panelLeft: rect.left,
            panelRight: rect.right,
            panelWidth: rect.width,
            panelScrollWidth: panel.scrollWidth,
            panelClientWidth: panel.clientWidth,
            innerScrollWidth: drawerBody.scrollWidth,
            innerClientWidth: drawerBody.clientWidth,
            bodyOverflowY: getComputedStyle(document.body).overflowY,
            htmlOverflowY: getComputedStyle(document.documentElement).overflowY,
            messagesOverflowY: getComputedStyle(
              document.querySelector('.messages') as HTMLElement,
            ).overflowY,
            scrim: getComputedStyle(
              document.querySelector('[data-testid="prompt-scrim"]') as HTMLElement,
            ).backgroundColor,
            scrollerPaddingRight: getComputedStyle(
              document.querySelector('[data-testid="message-scroll"]') as HTMLElement,
            ).paddingRight,
          };
        });

      const desktop = await measure();
      expect(desktop.panelWidth).toBeGreaterThanOrEqual(420);
      expect(desktop.panelWidth).toBeLessThanOrEqual(560);
      expect(desktop.panelWidth).toBeCloseTo(1440 * 0.34, 1);
      // 贴着右边缘：left = 视口 - 宽度，right = 视口
      expect(desktop.panelLeft).toBeCloseTo(1440 - desktop.panelWidth, 1);
      expect(desktop.panelRight).toBeCloseTo(1440, 1);
      expect(desktop.docScrollWidth).toBeLessThanOrEqual(desktop.docClientWidth);

      /*
       * 抽屉打开时对话区让位：消息滚动容器的右内边距 == 面板宽度，
       * 且入口真的露在面板左边（命中入口，不是 prompt-drawer__*）。
       */
      expect(parseFloat(desktop.scrollerPaddingRight)).toBeCloseTo(desktop.panelWidth, 1);
      await expectEntryReachable(page);

      // 背景没有被锁死：body/html 都没有 overflow:hidden，对话列表仍可滚
      expect(desktop.bodyOverflowY).not.toBe('hidden');
      expect(desktop.htmlOverflowY).not.toBe('hidden');
      expect(desktop.messagesOverflowY).toBe('auto');
      // 遮罩很淡：背后的内容仍然读得清
      expect(desktop.scrim).toBe('rgba(38, 34, 29, 0.14)');

      // 背景真的还能滚：把对话列表滚到一个中间位置，滚轮落在面板左边，
      // 列表应该继续滚动（遮罩 pointer-events:none，不接滚轮）。
      await page.evaluate(() => {
        (document.querySelector('.messages') as HTMLElement).scrollTop = 300;
      });
      await page.mouse.move(500, 400);
      await page.mouse.wheel(0, 240);
      await expect
        .poll(() =>
          page.evaluate(() => (document.querySelector('.messages') as HTMLElement).scrollTop),
        )
        .toBeGreaterThan(300);

      console.info(
        `[prompt.browser] 1440: panel ${desktop.panelWidth.toFixed(2)}px ` +
          `(right ${desktop.panelRight.toFixed(2)}), doc ${desktop.docScrollWidth}/${desktop.docClientWidth}`,
      );

      // 同一个抽屉直接缩到 375：占满宽度，且里外都没有横向溢出
      await page.setViewportSize({ width: 375, height: 667 });
      await expect.poll(() => measure().then((m) => m.panelWidth)).toBe(375);
      const mobile = await measure();
      expect(mobile.panelLeft).toBe(0);
      expect(mobile.panelWidth).toBe(375);
      expect(mobile.docScrollWidth).toBeLessThanOrEqual(mobile.docClientWidth);
      expect(mobile.bodyScrollWidth).toBeLessThanOrEqual(mobile.bodyClientWidth);
      expect(mobile.panelScrollWidth).toBeLessThanOrEqual(mobile.panelClientWidth);
      expect(mobile.innerScrollWidth).toBeLessThanOrEqual(mobile.innerClientWidth);
      console.info(
        `[prompt.browser] 375: panel ${mobile.panelWidth} (left ${mobile.panelLeft}), ` +
          `doc ${mobile.docScrollWidth}/${mobile.docClientWidth}, ` +
          `body ${mobile.bodyScrollWidth}/${mobile.bodyClientWidth}, ` +
          `drawer inner ${mobile.innerScrollWidth}/${mobile.innerClientWidth}`,
      );
    },
    TIMEOUT,
  );
});
