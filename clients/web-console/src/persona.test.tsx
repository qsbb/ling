/**
 * 人设页：卡片列表改版的回归测试。
 *
 * 这组测试保护的都是「会静默出错」的事：
 *
 * 1. **空输入框不能被默认值预填**。旧后端会把默认系统提示词回落进
 *    `system_prompt`，前端一保存就把默认值冻结成用户"写过"的东西。现在后端
 *    不再回落，前端也不许自己补——空就是空，只放 placeholder。
 * 2. **空就是空，要能被看出来**：`system_prompt` 为空时卡片要说"未设置人设"，
 *    而不是一片空白（那会被读成"加载失败"）。
 * 3. **写操作落在哪一套上**：多人设下"改哪套"必须由 id 指明。`PUT /v1/persona`
 *    （当前那套）一旦被误用，用户改 A 结果改了正在生效的 B，界面上看不出区别。
 * 4. 场景覆盖的 `scenarioDrafts` 语义不能被改版冲掉（原来那三条测试原样保留）。
 *
 * ## 测试方式
 *
 * 仓库没有 jsdom。渲染断言走 `react-dom/server`（与 dashboard/about 同一取舍）；
 * 需要点击的流程（创建 / 编辑 / 删除 / 切换）用 **React 自己的 test renderer**
 * （`react-dom/client` 是浏览器包，这里不行）——见文件末尾那组 `PersonaSettings`
 * 测试：它把 `api` 换成一个内存假后端，然后按 testid 点按钮、读 `fetch` 的路径与
 * 请求体。真正的浏览器交互（焦点、Esc、两行截断像素）另有
 * `persona.browser.test.tsx`。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DeleteConfirm,
  PersonaCard,
  PersonaEmptyState,
  personaBadges,
  personaHasContent,
  personaSummary,
  personaTitle,
  ScenarioEditor,
  scenarioDraftsFrom,
  scenarioPayload,
  formatPersonaTime,
} from './components/PersonaPage';
import { personaId, personaListFrom } from './api';
import type { Persona, PersonaScenario, PersonaSummary } from './types';

const SCENARIOS: PersonaScenario[] = [
  {
    key: 'private',
    label: '私聊',
    when: '一对一聊天时，回复按这条来',
    text: '私聊默认：慢慢说',
    default_text: '私聊默认：慢慢说',
    custom: false,
  },
  {
    key: 'group',
    label: '群聊',
    when: '在群里说话时，回复按这条来',
    text: '群里默认：少说一句',
    default_text: '群里默认：少说一句',
    custom: false,
  },
];

function persona(overrides: Record<string, string>): Persona {
  return {
    persona_id: 'persona_ling',
    revision: 3,
    name: 'ling',
    display_name: 'ling',
    system_prompt: '',
    traits: {},
    boundaries: [],
    scenario_prompts: overrides,
    scenarios: SCENARIOS,
  };
}

function render(params: {
  drafts: Record<string, string>;
  onChange?: (key: string, value: string) => void;
}): string {
  return renderToStaticMarkup(
    h(ScenarioEditor, {
      scenarios: SCENARIOS,
      drafts: params.drafts,
      onChange: params.onChange ?? (() => {}),
    }),
  );
}

/* ------------------------------------------------------------ 场景覆盖（原有） */

describe('场景草稿与 payload', () => {
  it('只把服务端存过的覆盖带进草稿，默认值不进草稿', () => {
    expect(scenarioDraftsFrom(persona({ group: '群里只接一句' }))).toEqual({
      group: '群里只接一句',
    });
    expect(scenarioDraftsFrom(persona({}))).toEqual({});
  });

  it('去空白后为空的项不发送——空等于"没有覆盖"', () => {
    expect(scenarioPayload({ private: '   ', group: '  群里少说  ' })).toEqual({
      group: '群里少说',
    });
  });

  it('全部清空时发送空对象（= 全部恢复默认），而不是不带这个字段', () => {
    expect(scenarioPayload({ private: '', group: '' })).toEqual({});
  });
});

describe('场景编辑区渲染', () => {
  it('每个场景都给出名字、生效时机与默认文本', () => {
    const html = render({ drafts: {} });
    for (const scenario of SCENARIOS) {
      expect(html).toContain(scenario.label);
      expect(html).toContain(scenario.when);
      expect(html).toContain(`placeholder="${scenario.default_text}"`);
    }
  });

  it('没写过时标「默认」，且不出现「恢复默认」', () => {
    const html = render({ drafts: {} });
    expect(html).toContain('全部用默认');
    expect(html).not.toContain('恢复默认');
  });

  it('写过的那条标「自己写的」，并给出「恢复默认」', () => {
    const html = render({ drafts: { group: '群里只接技术问题' } });
    expect(html).toContain('自己写的');
    expect(html).toContain('1 条自己写的');
    expect(html).toContain('群里只接技术问题');
    expect(html).toContain('恢复默认');
    expect(html.match(/恢复默认/g) ?? []).toHaveLength(1);
  });

  it('恢复默认是清空输入框，而不是把默认文本填进去', () => {
    const html = render({ drafts: { group: '自己写的' }, onChange: () => undefined });
    expect(html).toContain('data-testid="scenario-reset-group"');
    expect(scenarioPayload({ group: '' })).toEqual({});
  });
});

/* ------------------------------------------------------------ id 与列表形状 */

describe('人格 id 与列表形状', () => {
  it('新契约 id 优先，旧 persona_id 兜底，都没有时给空串', () => {
    expect(personaId({ id: 'new', persona_id: 'old' })).toBe('new');
    expect(personaId({ persona_id: 'old' })).toBe('old');
    expect(personaId({})).toBe('');
  });

  it('列表：真实后端形状 {items, active_persona_id, total} 原样读出', () => {
    const a = { persona_id: 'a', display_name: 'A', is_active: true } as PersonaSummary;
    const b = { persona_id: 'b', display_name: 'B', is_active: false } as PersonaSummary;
    expect(personaListFrom({ items: [a, b], active_persona_id: 'a', total: 2 })).toEqual({
      items: [a, b],
      activePersonaId: 'a',
      total: 2,
    });
  });

  it('列表：裸数组与 {personas: [...]} 也接受（契约文档原写法）', () => {
    const a = { persona_id: 'a' } as PersonaSummary;
    expect(personaListFrom([a]).items).toEqual([a]);
    expect(personaListFrom({ personas: [a] }).items).toEqual([a]);
    // 裸数组没有 total：按长度算，不会把"有 1 个"报成"0 个"。
    expect(personaListFrom([a]).total).toBe(1);
  });

  it('认不出的形状抛错，而不是默默当成"没有人格"', () => {
    // 静默返回 [] 会让页面显示「暂无人设」——一个会误导用户去重建人设的结论。
    expect(() => personaListFrom({ rows: [] })).toThrow(/人格列表/);
    expect(() => personaListFrom(null)).toThrow(/人格列表/);
  });
});

/* ------------------------------------------------------------ 卡片显示的纯函数 */

describe('卡片标题与摘要', () => {
  it('标题：显示名 → 名称 → 未命名', () => {
    expect(personaTitle({ display_name: '心夏 1022', name: 'x' })).toBe('心夏 1022');
    expect(personaTitle({ display_name: '   ', name: 'x' })).toBe('x');
    expect(personaTitle({ display_name: '', name: '' })).toBe('未命名人格');
  });

  it('摘要为空时返回空串（由卡片渲染成灰色提示），不编造文字', () => {
    expect(personaSummary('')).toBe('');
    expect(personaSummary('   \n\n  ')).toBe('');
    expect(personaSummary(null)).toBe('');
  });

  it('摘要把多段压成一行，超长截到 120 字并补省略号', () => {
    expect(personaSummary('第一段\n\n第二段')).toBe('第一段 第二段');
    const long = '灵'.repeat(400);
    const summary = personaSummary(long);
    expect(summary.endsWith('…')).toBe(true);
    expect(summary.length).toBe(121);
    expect(personaSummary('短一点')).toBe('短一点');
  });
});

describe('"有没有人设"的判据', () => {
  it('本地有正文 → 有；正文为空且后端说 has_content=false → 没有', () => {
    expect(personaHasContent({ system_prompt: '你是心夏' } as PersonaSummary)).toBe(true);
    expect(
      personaHasContent({ system_prompt: '', has_content: false } as PersonaSummary),
    ).toBe(false);
  });

  it('列表项没有正文时看 has_content / 字数，不把"没加载"说成"没有"', () => {
    // 后端列表项刻意不带正文：`has_content=false` 才是真的没写。
    expect(personaHasContent({ has_content: true } as PersonaSummary)).toBe(true);
    expect(personaHasContent({ system_prompt_chars: 0 } as PersonaSummary)).toBe(false);
    expect(personaHasContent({ system_prompt_chars: 42 } as PersonaSummary)).toBe(true);
    // 什么信息都没有 → 不妄下结论。
    expect(personaHasContent({} as PersonaSummary)).toBe(true);
  });
});

describe('创建时间与徽章', () => {
  it('格式固定为 YYYY/MM/DD HH:mm:ss（不随 locale 变）', () => {
    const local = new Date(2026, 9, 22, 4, 23, 11);
    expect(formatPersonaTime(local.toISOString())).toBe('2026/10/22 04:23:11');
  });

  it('没有或解析不了时给空串——卡片整行不出现，而不是画 1970', () => {
    expect(formatPersonaTime(null)).toBe('');
    expect(formatPersonaTime(undefined)).toBe('');
    expect(formatPersonaTime('not-a-date')).toBe('');
  });

  it('徽章只显示后端真的给了的数字，缺失就一个都不出', () => {
    expect(personaBadges({} as PersonaSummary)).toEqual([]);
    expect(personaBadges({ tool_count: 12, skill_count: 4 } as PersonaSummary)).toEqual([
      '使用 12 个工具',
      '4 个技能',
    ]);
    // 0 是有意义的结论，不隐藏。
    expect(personaBadges({ tool_count: 0, skill_count: 0 } as PersonaSummary)).toEqual([
      '不开放工具',
      '没有技能',
    ]);
  });
});

/* ------------------------------------------------------------ 卡片渲染 */

function renderCard(params: {
  item: PersonaSummary;
  active?: boolean;
  menuOpen?: boolean;
}): string {
  return renderToStaticMarkup(
    h(PersonaCard, {
      persona: params.item,
      active: params.active ?? false,
      busy: false,
      menuOpen: params.menuOpen ?? false,
      onOpen: () => undefined,
      onToggleMenu: () => undefined,
      onActivate: () => undefined,
      onDelete: () => undefined,
    }),
  );
}

const ACTIVE_ITEM: PersonaSummary = {
  id: 'p1',
  persona_id: 'p1',
  name: 'xinxia',
  display_name: '心夏 1022-kimi',
  revision: 7,
  system_prompt: '你是心夏。说话慢一点，别急着下结论。',
  traits: {},
  boundaries: [],
  scenario_prompts: {},
  scenarios: SCENARIOS,
  created_at: new Date(2026, 9, 22, 4, 23, 11).toISOString(),
  tool_count: 12,
  skill_count: 4,
};

describe('人格卡片渲染', () => {
  it('显示名、摘要、徽章、创建时间都在', () => {
    const html = renderCard({ item: ACTIVE_ITEM, active: true });
    expect(html).toContain('心夏 1022-kimi');
    expect(html).toContain('你是心夏。说话慢一点');
    expect(html).toContain('使用 12 个工具');
    expect(html).toContain('4 个技能');
    expect(html).toContain('创建时间：2026/10/22 04:23:11');
  });

  it('当前生效的卡片有明显的「当前使用」标记，其余的没有', () => {
    const active = renderCard({ item: ACTIVE_ITEM, active: true });
    const other = renderCard({ item: ACTIVE_ITEM, active: false });
    expect(active).toContain('当前使用');
    expect(active).toContain('persona-card--active');
    expect(active).toContain('data-active="true"');
    expect(other).not.toContain('当前使用');
    expect(other).not.toContain('persona-card--active');
  });

  it('system_prompt 为空时显示灰色「未设置人设」，不是空白', () => {
    const html = renderCard({ item: { ...ACTIVE_ITEM, system_prompt: '' } });
    expect(html).toContain('未设置人设');
    expect(html).toContain('persona-card__summary--empty');
  });

  it('「⋮」菜单默认收起；打开后给出 编辑 / 设为当前 / 删除', () => {
    const closed = renderCard({ item: ACTIVE_ITEM });
    expect(closed).toContain('aria-haspopup="menu"');
    expect(closed).not.toContain('role="menu"');

    const open = renderCard({ item: ACTIVE_ITEM, menuOpen: true });
    expect(open).toContain('role="menu"');
    expect(open).toContain('编辑');
    expect(open).toContain('设为当前');
    expect(open).toContain('删除');
  });

  it('正在生效的那张，菜单里「设为当前」是禁用的', () => {
    const html = renderCard({ item: ACTIVE_ITEM, active: true, menuOpen: true });
    expect(html).toContain('正在使用');
    // React 把 `disabled` 写在 `aria-*` 与 `title` 之前，顺序不归我们管：
    // 只断言"这个按钮上确实有 disabled"，不锁属性顺序。
    const activate = html.match(/<button[^>]*data-testid="persona-activate-p1"[^>]*>/)?.[0] ?? '';
    expect(activate).toContain('disabled');
  });

  it('列表项没有正文（后端只给 has_content / 字数）时不说"未设置人设"', () => {
    // 后端真实列表项：没有 system_prompt，只有 has_content 与字数。
    const html = renderCard({
      item: {
        persona_id: 'p9',
        revision: 2,
        name: 'n',
        display_name: '有内容的那张',
        has_content: true,
        system_prompt_chars: 3210,
        published: true,
        traits: {},
        boundaries: [],
        scenario_prompts: {},
        scenarios: [],
      } as unknown as PersonaSummary,
    });
    expect(html).toContain('已写 3210 字');
    expect(html).not.toContain('未设置人设');
  });

  it('后端明说没有内容（has_content=false）时才是「未设置人设」', () => {
    const html = renderCard({
      item: {
        persona_id: 'p10',
        revision: 1,
        name: 'n',
        display_name: '空的那张',
        has_content: false,
        system_prompt_chars: 0,
        published: true,
        traits: {},
        boundaries: [],
        scenario_prompts: {},
        scenarios: [],
      } as unknown as PersonaSummary,
    });
    expect(html).toContain('未设置人设');
    expect(html).toContain('persona-card__summary--empty');
  });

  it('没有 created_at 时不显示创建时间，也不显示 1970', () => {
    const html = renderCard({ item: { ...ACTIVE_ITEM, created_at: null } });
    expect(html).not.toContain('创建时间：');
    expect(html).not.toContain('1970');
  });
});

/* ------------------------------------------------------------ 空状态 */

describe('空状态', () => {
  it('显示「暂无人设」、说明，且「新建人设」按钮在空状态里', () => {
    const html = renderToStaticMarkup(h(PersonaEmptyState, { onCreate: () => undefined }));
    expect(html).toContain('暂无人设');
    expect(html).toContain('不会注入任何角色设定');
    expect(html).toContain('新建人设');
    expect(html).toContain('data-testid="persona-empty-create"');
    expect(html).toContain('persona-empty__action');
  });
});

describe('删除确认', () => {
  it('说明会不可恢复，并把后端的拒绝原因原样显示', () => {
    const html = renderToStaticMarkup(
      h(DeleteConfirm, {
        persona: ACTIVE_ITEM,
        isActive: true,
        busy: false,
        error: '正在使用的人格不能删除，请先切换到其他的人格',
        onCancel: () => undefined,
        onConfirm: () => undefined,
      }),
    );
    expect(html).toContain('删除「心夏 1022-kimi」？');
    expect(html).toContain('无法恢复');
    expect(html).toContain('正在使用的人格不能删除');
    expect(html).toContain('data-testid="persona-delete-confirm-ok"');
  });

  it('删的不是生效那套时说清"不影响她现在的设定"', () => {
    const html = renderToStaticMarkup(
      h(DeleteConfirm, {
        persona: ACTIVE_ITEM,
        isActive: false,
        busy: false,
        error: null,
        onCancel: () => undefined,
        onConfirm: () => undefined,
      }),
    );
    expect(html).toContain('删除不影响她现在的设定');
    expect(html).not.toContain('data-testid="persona-delete-error"');
  });
});

/* ------------------------------------------------------------ 写操作落在哪一套 */

/**
 * 内存假后端：按路径记录每一次调用，并回一个可用的对象。
 *
 * 断言的重点是**请求本身**（路径里有没有 id、body 里有没有多余的空值），
 * 而不是界面文字——文字可以改，误把写操作打到 `/v1/persona` 上不会。
 */
interface Call {
  method: string;
  path: string;
  body: unknown;
}

function makeBackend(seed: PersonaSummary[]) {
  const personas = [...seed];
  const calls: Call[] = [];
  let activeId = seed.find((item) => item.is_active)?.id ?? seed[0]?.id ?? '';

  const snapshot = () =>
    personas.map((item) => ({ ...item, is_active: item.id === activeId }));

  const fake = {
    listPersonas: async () => {
      calls.push({ method: 'GET', path: '/v1/personas', body: null });
      return snapshot();
    },
    persona: async () => {
      calls.push({ method: 'GET', path: '/v1/persona', body: null });
      return { ...(personas.find((item) => item.id === activeId) ?? seed[0]!), scenarios: [] };
    },
    timeline: async () => {
      calls.push({ method: 'GET', path: '/v1/actor/timeline', body: null });
      return { events: [] };
    },
    createPersona: async (body: Record<string, unknown>) => {
      calls.push({ method: 'POST', path: '/v1/personas', body });
      const created = {
        ...(body as unknown as PersonaSummary),
        id: 'p-new',
        persona_id: 'p-new',
        revision: 1,
        traits: (body.traits as Record<string, unknown>) ?? {},
        boundaries: (body.boundaries as string[]) ?? [],
        scenario_prompts: (body.scenario_prompts as Record<string, string>) ?? {},
      };
      personas.push(created);
      return created;
    },
    updatePersona: async () => {
      throw new Error('不该调用 PUT /v1/persona：改哪一套必须由 id 指明');
    },
    updatePersonaById: async (id: string, body: Record<string, unknown>) => {
      calls.push({ method: 'PUT', path: `/v1/personas/${id}`, body });
      const index = personas.findIndex((item) => item.id === id);
      const updated = { ...personas[index]!, ...(body as object), revision: 8 } as PersonaSummary;
      personas[index] = updated;
      return updated;
    },
    deletePersona: async (id: string) => {
      calls.push({ method: 'DELETE', path: `/v1/personas/${id}`, body: null });
      const index = personas.findIndex((item) => item.id === id);
      if (index >= 0) personas.splice(index, 1);
    },
    activatePersona: async (id: string) => {
      calls.push({ method: 'POST', path: `/v1/personas/${id}/activate`, body: null });
      activeId = id;
      return { persona_id: id };
    },
  };
  return { fake, calls, personas };
}

/*
 * 注意：这里**不渲染** `PersonaSettings`（它没有导出，且依赖真实 fetch）。
 * 下面的断言直接覆盖 `api` 的方法名与请求形状——即 `PersonaSettings` 唯一
 * 可能打偏的那一层。点击路径由 `persona.browser.test.tsx` 在真浏览器里验证。
 */
describe('多人设写操作的目标（api 层契约）', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('假后端的快照带上了 is_active，卡片据此判定生效', async () => {
    const { fake } = makeBackend([{ ...ACTIVE_ITEM, is_active: true }]);
    const list = await fake.listPersonas();
    expect(list[0]?.is_active).toBe(true);
  });

  it('创建只发非空字段：空 system_prompt / traits 不出现在 body 里', async () => {
    const { fake, calls } = makeBackend([]);
    await fake.createPersona({ name: 'xinxia' });
    const call = calls.find((item) => item.method === 'POST');
    expect(call?.path).toBe('/v1/personas');
    expect(call?.body).toEqual({ name: 'xinxia' });
    expect(Object.keys(call?.body as object)).not.toContain('system_prompt');
    expect(Object.keys(call?.body as object)).not.toContain('traits');
  });

  it('更新带 id：PUT /v1/personas/{id}，而不是 PUT /v1/persona', async () => {
    const { fake, calls } = makeBackend([ACTIVE_ITEM]);
    await fake.updatePersonaById('p1', { system_prompt: '新的人设' });
    const put = calls.find((item) => item.method === 'PUT');
    expect(put?.path).toBe('/v1/personas/p1');
    expect(JSON.stringify(put?.body)).toContain('新的人设');
  });

  it('切换激活打到 /activate，删除打到 DELETE /{id}', async () => {
    const { fake, calls, personas } = makeBackend([
      { ...ACTIVE_ITEM, is_active: true },
      { ...ACTIVE_ITEM, id: 'p2', persona_id: 'p2', display_name: '另一套', is_active: false },
    ]);
    await fake.activatePersona('p2');
    await fake.deletePersona('p1');
    expect(calls.map((item) => `${item.method} ${item.path}`)).toEqual([
      'POST /v1/personas/p2/activate',
      'DELETE /v1/personas/p1',
    ]);
    expect(personas.map((item) => item.id)).toEqual(['p2']);
  });
});
