/**
 * 人设页卡片列表的**真实浏览器**验证（playwright + 真 Vite 开发服务器）。
 *
 * 为什么不能只靠 SSR：这次改版的几条要求全是交互——
 * 「无人设时按钮在页面中间」、「有人设时右上角创建」、「点卡片进编辑」、
 * 「⋮ 菜单里的设为当前 / 删除」、「删除要二次确认」、「空输入框不预填」。
 * SSR 只能断言静态标记，点不到也填不了。
 *
 * `/v1/**` 全部由 `page.route` 模拟（一个内存假后端）：**不碰真后端、不写库**。
 * 没有 playwright 浏览器时整组标成 skipped（不是静默 passed）并打印原因。
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const CREATED = '2026-10-22T04:23:11+08:00';

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

function skip(ctx: { skip: () => void }): boolean {
  if (!launchError) return false;
  console.warn(`[persona.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 假后端 */

interface FakePersona {
  id: string;
  name: string;
  display_name: string;
  system_prompt: string;
  traits: Record<string, unknown>;
  boundaries: string[];
  scenario_prompts: Record<string, string>;
  revision: number;
  published: boolean;
  created_at: string;
  tool_count?: number;
  skill_count?: number;
}

const SCENARIOS = [
  {
    key: 'group',
    label: '群聊',
    when: '在群里说话时',
    text: '群里默认：少说一句',
    default_text: '群里默认：少说一句',
    custom: false,
  },
];

function seed(displayName: string, prompt: string, extra: Partial<FakePersona> = {}): FakePersona {
  const id = extra.id ?? displayName;
  return {
    id,
    name: id,
    display_name: displayName,
    system_prompt: prompt,
    traits: {},
    boundaries: [],
    scenario_prompts: {},
    revision: 3,
    published: true,
    created_at: CREATED,
    ...extra,
  };
}

interface Backend {
  calls: string[];
  personas: FakePersona[];
  activeId: string;
  /** 置为非空时，DELETE 这个 id 会 409（模拟"正在生效的人格不能删"）。 */
  refuseDelete: string | null;
}

/**
 * 等条件成立（最多 5s）。
 *
 * 不用 `expect(locator).toHaveCount()`：那是 `@playwright/test` 的 matcher，
 * 仓库依赖里只有浏览器驱动的 `playwright`，vitest 的 `expect` 没有这一套。
 */
async function waitUntil(check: () => Promise<boolean>, what: string): Promise<void> {
  const deadline = Date.now() + 5000;
  for (;;) {
    if (await check()) return;
    if (Date.now() > deadline) throw new Error(`等待超时：${what}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

function json(body: unknown, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/**
 * 列表项：**按后端真实的 `PersonaListItem` 形状返回**——刻意不带正文
 * （`system_prompt`），也没有 `created_at`。前端必须靠 `has_content` /
 * `system_prompt_chars` 说明卡片状态，靠 `GET /v1/personas/{id}` 取摘要。
 * 这里故意贴着后端的裁剪来测，而不是回一个"什么都有"的假对象。
 */
function listItem(persona: FakePersona, backend: Backend) {
  return {
    persona_id: persona.id,
    revision: persona.revision,
    name: persona.name,
    display_name: persona.display_name,
    is_active: persona.id === backend.activeId,
    has_content: persona.system_prompt.trim() !== '',
    system_prompt_chars: persona.system_prompt.length,
    trait_count: Object.keys(persona.traits).length,
    boundary_count: persona.boundaries.length,
    scenario_count: Object.keys(persona.scenario_prompts).length,
    published: persona.published,
    ...(persona.created_at ? { created_at: persona.created_at } : {}),
    ...(persona.tool_count !== undefined ? { tool_count: persona.tool_count } : {}),
    ...(persona.skill_count !== undefined ? { skill_count: persona.skill_count } : {}),
  };
}

/** 详情：完整的 `PersonaOut`（正文 + 场景对照）。 */
function detail(persona: FakePersona, backend: Backend) {
  return { ...listItem(persona, backend), system_prompt: persona.system_prompt, traits: persona.traits, boundaries: persona.boundaries, scenario_prompts: persona.scenario_prompts, scenarios: SCENARIOS };
}

async function openPersona(
  initial: FakePersona[],
  options: { activeId?: string; refuseDelete?: string } = {},
): Promise<{ page: Page; backend: Backend; createBodies: Array<Record<string, unknown>> }> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    reducedMotion: 'reduce',
  });
  contexts.push(context);
  const page = await context.newPage();

  const createBodies: Array<Record<string, unknown>> = [];
  const backend: Backend = {
    calls: [],
    personas: initial.map((item) => ({ ...item })),
    activeId: options.activeId ?? initial[0]?.id ?? '',
    refuseDelete: options.refuseDelete ?? null,
  };

  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (!path.startsWith('/v1/')) return route.continue();
    backend.calls.push(`${method} ${path}`);

    if (path === '/v1/auth/me') {
      return route.fulfill(
        json({
          schema_version: 'v1',
          user: {
            id: 'u1',
            username: 'demo',
            role: 'owner',
            subject_id: 's1',
            display_name: '主人',
          },
          config_revision: 1,
          test_mode: false,
          csrf_token: 'csrf',
        }),
      );
    }
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.3' }));

    const body = request.postDataJSON?.() as Record<string, unknown> | null;

    if (path === '/v1/personas' && method === 'GET') {
      return route.fulfill(
        json({
          items: backend.personas.map((item) => listItem(item, backend)),
          active_persona_id: backend.activeId,
          total: backend.personas.length,
        }),
      );
    }
    if (path === '/v1/persona' && method === 'GET') {
      const active = backend.personas.find((item) => item.id === backend.activeId);
      if (!active) {
        return route.fulfill(
          json({ error: { code: 'not_found', message: '还没有人设', retriable: false } }, 404),
        );
      }
      return route.fulfill(json({ ...detail(active, backend), active: true, exists: true }));
    }
    if (path === '/v1/actor/timeline') return route.fulfill(json({ events: [] }));

    if (path === '/v1/personas' && method === 'POST') {
      createBodies.push((body as Record<string, unknown>) ?? {});
      const display = String(body?.display_name ?? '');
      const created: FakePersona = {
        ...seed(display || 'new', String(body?.system_prompt ?? '')),
        id: `p-new-${backend.personas.length + 1}`,
        display_name: display,
        traits: (body?.traits as Record<string, unknown>) ?? {},
        boundaries: (body?.boundaries as string[]) ?? [],
        scenario_prompts: (body?.scenario_prompts as Record<string, string>) ?? {},
        revision: 1,
      };
      backend.personas.push(created);
      return route.fulfill(json(detail(created, backend), 201));
    }

    const idMatch = path.match(/^\/v1\/personas\/([^/]+)(\/activate)?$/);
    if (idMatch) {
      const id = decodeURIComponent(idMatch[1]!);
      const index = backend.personas.findIndex((item) => item.id === id);
      const found = backend.personas[index];
      if (!found) {
        return route.fulfill(
          json({ error: { code: 'not_found', message: '目标不存在', retriable: false } }, 404),
        );
      }
      if (idMatch[2] && method === 'POST') {
        backend.activeId = id;
        return route.fulfill(json({ persona_id: id }));
      }
      if (method === 'GET' && !idMatch[2]) {
        return route.fulfill(json(detail(found, backend)));
      }
      if (method === 'PUT') {
        const updated = { ...found, ...(body as Partial<FakePersona>), revision: found.revision + 1 };
        backend.personas[index] = updated;
        return route.fulfill(json(detail(updated, backend)));
      }
      if (method === 'DELETE') {
        if (backend.refuseDelete === id) {
          return route.fulfill(
            json(
              {
                error: {
                  code: 'conflict',
                  message: '正在使用的人格不能删除，请先切换到其他人格',
                  retriable: false,
                },
              },
              409,
            ),
          );
        }
        backend.personas.splice(index, 1);
        return route.fulfill({ status: 204, body: '' });
      }
    }
    // 兜底给 404，而不是让 `/v1/persona` 掉进 index.html（那会变成 JSON 解析错误）。
    return route.fulfill(
      json({ error: { code: 'not_found', message: `${method} ${path}`, retriable: false } }, 404),
    );
  });

  await page.goto(`${baseUrl}/persona`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('persona-tab-profile').waitFor();
  return { page, backend, createBodies };
}

/* ------------------------------------------------------------ 空状态 */

describe('无人设时的空状态', () => {
  it('页面中间显示「暂无人设」，新建按钮在中间而不是右上角', async (ctx) => {
    if (skip(ctx)) return;
    const { page, backend } = await openPersona([]);

    expect(await page.getByTestId('persona-empty').innerText()).toContain('暂无人设');
    expect(await page.getByTestId('persona-empty').innerText()).toContain('不会注入任何角色设定');
    // 固定文案不许再出现错字"没有人生成"（那是"无人设"的笔误）。
    expect(await page.getByTestId('persona-empty').innerText()).not.toContain('人生成');

    // 右上角的「创建人格」不出现；唯一入口是空状态里的「新建人设」。
    expect(await page.getByTestId('persona-create').count()).toBe(0);

    const box = await page.getByTestId('persona-empty-create').boundingBox();
    const panel = await page.getByTestId('persona-empty').boundingBox();
    const viewport = page.viewportSize()!;
    expect(box).not.toBeNull();
    expect(panel).not.toBeNull();
    if (!box || !panel) return;
    // 水平居中：按钮中心与空状态中心相差不超过 2px。
    const buttonCenter = box.x + box.width / 2;
    const panelCenter = panel.x + panel.width / 2;
    expect(Math.abs(buttonCenter - panelCenter)).toBeLessThanOrEqual(2);
    // 而且它不在右边缘：按钮右缘离视口右缘至少还有 1/5 屏宽。
    expect(viewport.width - (box.x + box.width)).toBeGreaterThan(viewport.width / 5);
    // 也不贴着页顶：空状态是居中在页面里，而不是顶上一行。
    expect(box.y).toBeGreaterThan(120);
    expect(backend.calls).toContain('GET /v1/personas');
  }, 30_000);

  it('点「新建人设」进入创建表单；空字段带 placeholder，输入框 value 为空', async (ctx) => {
    if (skip(ctx)) return;
    const { page, createBodies } = await openPersona([]);
    await page.getByTestId('persona-empty-create').click();
    await page.getByTestId('persona-form').waitFor();

    const prompt = page.getByTestId('persona-field-system-prompt');
    // 关键回归：不许预填任何"默认人设"。
    expect(await prompt.inputValue()).toBe('');
    expect(await prompt.getAttribute('placeholder')).toContain('不填则不注入人设');
    expect(await page.getByTestId('persona-field-name').inputValue()).toBe('');
    expect(await page.getByTestId('persona-field-boundaries').inputValue()).toBe('');
    // traits：框里是合法的 `{}`，但**不是用户写的**——placeholder 在，提醒"没写过"。
    expect(await page.getByTestId('persona-field-traits').inputValue()).toBe('{}');
    expect(await page.getByTestId('persona-field-traits').getAttribute('placeholder')).toBeTruthy();

    await page.getByTestId('persona-field-name').fill('xinxia');
    await page.getByTestId('persona-field-display-name').fill('心夏 1022-kimi');
    await page.getByRole('button', { name: '创建', exact: true }).click();

    await page.getByTestId('persona-grid').waitFor();
    expect(await page.getByTestId('persona-grid').innerText()).toContain('心夏 1022-kimi');
    // 创建请求体里不许有 `name`（后端 `PersonaCreate` 没有这个字段；
    // pydantic 会静默忽略未知键，所以"发错了"在界面上完全看不出来）。
    expect(createBodies).toHaveLength(1);
    expect(Object.keys(createBodies[0] ?? {})).not.toContain('name');
    // 没写系统提示词 → 那个键也不发（空就是空，不让后端拿到一个 `''`）。
    expect(Object.keys(createBodies[0] ?? {})).not.toContain('system_prompt');
    // 没写系统提示词 → 卡片上"未设置人设"，而不是摘要或空白。
    expect(await page.getByTestId('persona-grid').innerText()).toContain('未设置人设');
    // 有人设之后，右上角出现「创建人格」。
    expect(await page.getByTestId('persona-create').count()).toBe(1);
  }, 30_000);
});

/* ------------------------------------------------------------ 卡片列表 */

describe('有人设时的卡片列表', () => {
  it('右上角「创建人格」；当前生效的卡片带「当前使用」，其余没有', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openPersona(
      [
        seed('心夏 1022-kimi', '你是心夏。说话慢一点。', { id: 'p1', tool_count: 12, skill_count: 4 }),
        seed('另一套', '', { id: 'p2' }),
      ],
      { activeId: 'p1' },
    );

    await page.getByTestId('persona-grid').waitFor();
    expect(await page.getByTestId('persona-create').count()).toBe(1);
    expect(await page.getByTestId('persona-active-p1').count()).toBe(1);
    expect(await page.getByTestId('persona-active-p2').count()).toBe(0);

    const p1 = await page.getByTestId('persona-card-p1').innerText();
    expect(p1).toContain('使用 12 个工具');
    expect(p1).toContain('4 个技能');
    expect(p1).toContain('创建时间：2026/10/22 04:23:11');

    // 空 system_prompt：灰色提示，不是空白摘要。
    const empty = await page.getByTestId('persona-summary-p2').innerText();
    expect(empty).toContain('未设置人设');

    // 两行截断真的生效（-webkit-line-clamp: 2），长摘要不会把卡片撑高。
    const clamp = await page
      .getByTestId('persona-card-p1')
      .evaluate((element) => {
        const summary = element.querySelector('.persona-card__summary');
        if (!summary) return null;
        const style = getComputedStyle(summary);
        return { lineClamp: style.webkitLineClamp, overflow: style.overflow };
      });
    expect(clamp?.lineClamp).toBe('2');
    expect(clamp?.overflow).toBe('hidden');
  }, 30_000);

  it('宽屏 2 列、窄屏 1 列', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openPersona([seed('A', 'x', { id: 'a' }), seed('B', 'y', { id: 'b' })]);
    await page.getByTestId('persona-grid').waitFor();

    const columns = async () =>
      page.getByTestId('persona-grid').evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    await page.setViewportSize({ width: 1280, height: 900 });
    expect((await columns()).split(' ').length).toBe(2);
    await page.setViewportSize({ width: 480, height: 900 });
    expect((await columns()).split(' ').length).toBe(1);
  }, 30_000);

  it('点卡片进编辑，改完保存打到 PUT /v1/personas/{id}', async (ctx) => {
    if (skip(ctx)) return;
    const { page, backend } = await openPersona([
      seed('心夏', '旧的人设', { id: 'p1' }),
      seed('另一套', '别动这套', { id: 'p2' }),
    ]);

    await page.getByTestId('persona-edit-p1').click();
    await page.getByTestId('persona-form').waitFor();
    expect(await page.getByTestId('persona-field-system-prompt').inputValue()).toBe('旧的人设');
    // 场景覆盖沿用 scenarioDrafts 模式：没写过就是空 + 默认文本当 placeholder。
    expect(await page.getByTestId('scenario-input-group').inputValue()).toBe('');
    expect(await page.getByTestId('scenario-input-group').getAttribute('placeholder')).toBe(
      '群里默认：少说一句',
    );

    await page.getByTestId('persona-field-system-prompt').fill('新的人设');
    await page.getByTestId('scenario-input-group').fill('群里只接技术问题');
    await page.getByRole('button', { name: '保存设定' }).click();

    await page.getByTestId('persona-grid').waitFor();
    expect(backend.calls).toContain('PUT /v1/personas/p1');
    // **绝不能**打到"当前那套"的老端点上。
    expect(backend.calls.filter((call) => call === 'PUT /v1/persona')).toEqual([]);
    expect(backend.personas.find((item) => item.id === 'p1')?.system_prompt).toBe('新的人设');
    expect(backend.personas.find((item) => item.id === 'p1')?.scenario_prompts).toEqual({
      group: '群里只接技术问题',
    });
    // 保存后列表是**重读**回来的：卡片摘要要等这次请求回来才变。网格节点
    // 在切回列表的第一帧就存在了，所以不能只 `waitFor` 网格，得等内容本身。
    await waitUntil(
      async () => (await page.getByTestId('persona-summary-p1').innerText()).includes('新的人设'),
      '列表重读后卡片摘要更新',
    );
  }, 30_000);

  it('清空系统提示词能存下去（空就是空，不会被补回默认值）', async (ctx) => {
    if (skip(ctx)) return;
    const { page, backend } = await openPersona([seed('心夏', '旧的人设', { id: 'p1' })]);
    await page.getByTestId('persona-edit-p1').click();
    await page.getByTestId('persona-form').waitFor();
    await page.getByTestId('persona-field-system-prompt').fill('');
    await page.getByRole('button', { name: '保存设定' }).click();
    await page.getByTestId('persona-grid').waitFor();
    // 摘要来自"重读列表 + 重取详情"，是一跳；先等钱货两清再断言。
    await waitUntil(
      async () => backend.personas[0]?.system_prompt === '',
      '保存后后端里的正文被清空',
    );
    // 内容本身也要等：`waitUntil(后端已清空)` 只保证**第一次**重读那批请求
    // 出发时后端清了；请求还在路上时卡片上仍是上一轮的摘要。断言必须盯内容。
    await waitUntil(
      async () =>
        (await page.getByTestId('persona-summary-p1').innerText()).includes('未设置人设'),
      '列表重读后卡片变成"未设置人设"',
    );

    // 再打开一次：框里**还是空的**，没被任何默认值填回来。
    await page.getByTestId('persona-edit-p1').click();
    await page.getByTestId('persona-form').waitFor();
    expect(await page.getByTestId('persona-field-system-prompt').inputValue()).toBe('');
  }, 30_000);

  it('⋮ → 设为当前：调 activate，徽章跟着换卡片', async (ctx) => {
    if (skip(ctx)) return;
    const { page, backend } = await openPersona(
      [seed('A', 'a', { id: 'a' }), seed('B', 'b', { id: 'b' })],
      { activeId: 'a' },
    );

    await page.getByTestId('persona-menu-b').click();
    await page.getByTestId('persona-activate-b').click();

    await waitUntil(
      async () => (await page.getByTestId('persona-active-b').count()) === 1,
      'B 卡片带上「当前使用」徽章',
    );
    expect(await page.getByTestId('persona-active-a').count()).toBe(0);
    expect(backend.calls).toContain('POST /v1/personas/b/activate');
    expect(backend.activeId).toBe('b');
  }, 30_000);

  it('⋮ → 删除：先二次确认，取消就不发请求；确认才 DELETE', async (ctx) => {
    if (skip(ctx)) return;
    const { page, backend } = await openPersona(
      [seed('A', 'a', { id: 'a' }), seed('B', 'b', { id: 'b' })],
      { activeId: 'a' },
    );

    await page.getByTestId('persona-menu-b').click();
    await page.getByTestId('persona-delete-b').click();
    await page.getByTestId('persona-delete-confirm').waitFor();
    expect(backend.calls.filter((call) => call.startsWith('DELETE'))).toEqual([]);

    await page.getByRole('button', { name: '取消' }).click();
    await waitUntil(
      async () => (await page.getByTestId('persona-delete-confirm').count()) === 0,
      '取消后确认框关闭',
    );
    expect(backend.calls.filter((call) => call.startsWith('DELETE'))).toEqual([]);

    await page.getByTestId('persona-menu-b').click();
    await page.getByTestId('persona-delete-b').click();
    await page.getByTestId('persona-delete-confirm-ok').click();

    await waitUntil(
      async () => (await page.getByTestId('persona-card-b').count()) === 0,
      '删除后 B 卡片消失',
    );
    expect(backend.calls).toContain('DELETE /v1/personas/b');
  }, 30_000);

  it('删除正在生效的人格被后端拒绝时，把原因显示在确认框里且不关框', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openPersona([seed('A', 'a', { id: 'a' })], {
      activeId: 'a',
      refuseDelete: 'a',
    });

    await page.getByTestId('persona-menu-a').click();
    await page.getByTestId('persona-delete-a').click();
    await page.getByTestId('persona-delete-confirm-ok').click();

    await page.getByTestId('persona-delete-error').waitFor();
    // 后端拒绝的那句话必须原样出现在框里——只写"状态冲突"等于什么都没说。
    expect(await page.getByTestId('persona-delete-error').innerText()).toContain(
      '正在使用的人格不能删除，请先切换到其他人格',
    );
    expect(await page.getByTestId('persona-delete-confirm').count()).toBe(1);
    expect(await page.getByTestId('persona-card-a').count()).toBe(1);
  }, 30_000);
});
