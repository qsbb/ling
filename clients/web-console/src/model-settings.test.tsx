/**
 * 「模型与生成」页的渲染断言：三种来源互不串、行尾永远有「实际生效」。
 *
 * 这里保护的是**语义**，不是样式：
 *
 * - 「单独指定」「继承」「已回退」「不可用」四种状态各自的标签与边框语义
 *   必须一一对应，不能出现"看着是继承、其实写了显式值"；
 * - 行尾的「实际生效」**必须来自服务端**（`profile_resolutions`），前端不
 *   根据 profiles/slots 再推一遍——两份算法迟早不一致，而那种不一致查不出来；
 * - 可绑清单以服务端 `known_profiles` 为准：界面多一行绑了没用的，比少一行
 *   更糟（用户会以为自己配好了）。
 *
 * 与 `connectors.test.tsx` 一致，不引入 DOM 测试库：用 `react-dom/server`
 * 渲染成字符串断言。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  FOLLOW_SLOT,
  RolesPanel,
  parseRoleSelectValue,
  roleSelectValue,
} from './components/model-settings/RolesPanel';
import { ProvidersPanel } from './components/model-settings/ProvidersPanel';
import {
  effectiveNote,
  kindLabel,
  profileEquals,
  providerEquals,
  providerShapeIssues,
  providersForChatSlot,
  providersForProfile,
  rowStateOf,
  visibleRoleKeys,
} from './components/model-settings/shared';
import type {
  ProfileDraftInput,
  ProfileResolution,
  ProviderDraftInput,
  QuickConfigPreview,
  SettingsConfig,
  SlotDraftInput,
  SlotView,
} from './types';

const PROVIDERS: ProviderDraftInput[] = [
  {
    provider_id: 'main',
    kind: 'openai_compatible',
    capabilities: ['chat'],
    base_url: 'https://192.0.2.10/v1',
    enabled: true,
    models: [
      { model_id: 'm-fast', note: '', enabled: true },
      { model_id: 'm-big', note: '', enabled: true },
    ],
    timeout_seconds: 60,
    max_output_tokens: null,
    daily_token_limit: null,
  },
];

/**
 * 一家对话 + 一家嵌入 + 一家重排。
 *
 * `capabilities` 是**服务端**给的，测试里原样带上：界面的过滤只认这个字段，
 * 不认 kind。三家的集合故意两两不相交，任何"没按能力过滤"的实现都会在
 * 下拉里多出一家。
 */
const MEMORY_PROVIDERS: ProviderDraftInput[] = [
  ...PROVIDERS,
  {
    provider_id: 'cf',
    kind: 'cloudflare_embedding',
    capabilities: ['embed'],
    base_url: null,
    enabled: true,
    extra: { account_id: 'acc-123' },
    models: [{ model_id: '@cf/baai/bge-m3', note: '', enabled: true }],
    timeout_seconds: 60,
    max_output_tokens: null,
    daily_token_limit: null,
  },
  {
    provider_id: 'sf',
    kind: 'siliconflow_rerank',
    capabilities: ['rerank'],
    base_url: 'https://api.siliconflow.cn/v1',
    enabled: true,
    models: [{ model_id: 'BAAI/bge-reranker-v2-m3', note: '', enabled: true }],
    timeout_seconds: 60,
    max_output_tokens: null,
    daily_token_limit: null,
  },
];

const SLOTS: SlotView[] = [
  {
    slot: 'fast',
    label: '快速',
    hint: '回消息最常走的一条路，要快',
    provider_id: 'main',
    model_id: 'm-fast',
    configured: true,
    usable: true,
    roles: ['dialogue.fast'],
  },
  {
    slot: 'creation',
    label: '创作',
    hint: '想清楚再写',
    provider_id: '',
    model_id: '',
    configured: false,
    usable: false,
    roles: ['memory.extract'],
  },
  {
    slot: 'vision',
    label: '识图',
    hint: '必须能看图',
    provider_id: '',
    model_id: '',
    configured: false,
    usable: false,
    roles: ['perception.vision'],
  },
  {
    // 视频是**独立**槽位：能看图片的模型大多看不了视频，用户要能分开绑。
    slot: 'video',
    label: '视频',
    hint: '必须能看懂视频',
    provider_id: '',
    model_id: '',
    configured: false,
    usable: false,
    roles: ['perception.video'],
  },
  {
    slot: 'daily',
    label: '日常',
    hint: '群里闲聊',
    provider_id: '',
    model_id: '',
    configured: false,
    usable: false,
    roles: ['dialogue.group'],
  },
];

const SLOT_DRAFTS: SlotDraftInput[] = SLOTS.map((slot) => ({
  slot: slot.slot,
  provider_id: slot.provider_id,
  model_id: slot.model_id,
}));

function profile(overrides: Partial<ProfileDraftInput> & { profile: string }): ProfileDraftInput {
  return {
    provider_id: '',
    model_id: '',
    fallback_provider_id: null,
    fallback_model_id: null,
    enabled: true,
    mode: 'slot',
    ...overrides,
  };
}

function resolution(
  overrides: Partial<ProfileResolution> & { profile: string },
): ProfileResolution {
  return {
    slot: null,
    source: 'unavailable',
    effective: null,
    requested: null,
    fell_back: false,
    enabled: true,
    reason: '',
    ...overrides,
  };
}

const CONFIG: SettingsConfig = {
  schema_version: 'v1',
  revision: 3,
  test_mode: false,
  log_access_denials: true,
  memory: {
    relative_score_floor: 0.35,
    rerank_top_n: 20,
    recency_half_life_days: 120,
    max_retention_days: 3650,
  },
  life: {
    scene_ttl_minutes: 15,
    energy_decay_per_hour: 1.5,
    social_load_per_turn: 3,
    social_load_decay_per_hour: 6,
    max_live_plans: 4,
  },
  dialogue: {
    group_turn_budget: 3,
    proactive_daily_limit: 2,
    quiet_start_minute: 0,
    quiet_end_minute: 0,
    private_may_stay_silent: true,
    group_mention_requires_reply: true,
    rest_may_skip: true,
    group_reply_delivery_enabled: false,
  },
  providers: [],
  profiles: [],
  revisions: [],
  known_kinds: ['openai_compatible'],
  // 故意不含 memory.extract：那条由「记忆」组的过滤逻辑决定渲不渲染。
  known_profiles: [
    'dialogue.fast',
    'dialogue.group',
    'perception.vision',
    'perception.video',
  ],
  profile_capabilities: {
    'dialogue.fast': 'chat',
    'dialogue.group': 'chat',
    'perception.vision': 'chat',
    'perception.video': 'chat',
    'memory.extract': 'chat',
  },
  memory_sources: {},
  slots: SLOTS,
  profile_resolutions: [],
};

/**
 * 可绑六条职责的配置：用来验证嵌入/重排的能力过滤与来源展示。
 *
 * `known_profiles` 仍由服务端决定；这里照抄 `/settings/config` 在
 * 6ba679b 之后的形状。
 */
const CAPABILITY_CONFIG: SettingsConfig = {
  ...CONFIG,
  known_profiles: [
    'dialogue.fast',
    'dialogue.group',
    'perception.vision',
    'perception.video',
    'memory.extract',
    'memory.embed',
    'memory.rerank',
  ],
  profile_capabilities: {
    ...CONFIG.profile_capabilities,
    'memory.embed': 'embed',
    'memory.rerank': 'rerank',
  },
  memory_sources: {
    'memory.embed': null,
    'memory.rerank': null,
  },
};

const NOOP = () => undefined;

function render(overrides: {
  profiles?: ProfileDraftInput[];
  resolutions?: ProfileResolution[];
  quickConfig?: QuickConfigPreview | null;
  config?: SettingsConfig;
  providers?: ProviderDraftInput[];
} = {}): string {
  return renderToStaticMarkup(
    <RolesPanel
      config={overrides.config ?? CONFIG}
      providers={overrides.providers ?? PROVIDERS}
      profiles={overrides.profiles ?? []}
      slots={SLOT_DRAFTS}
      resolutions={overrides.resolutions ?? []}
      slotViews={SLOTS}
      quickConfig={overrides.quickConfig ?? null}
      quickConfigBusy={false}
      health={null}
      dirtyCount={0}
      saving={false}
      onPatchProfile={NOOP}
      onPatchSlot={NOOP}
      onDiscard={NOOP}
      onSave={NOOP}
      onRequestQuickConfig={NOOP}
      onCloseQuickConfig={NOOP}
      onApplyQuickConfig={NOOP}
    />,
  );
}

/** 「供应商」视图的渲染夹具：只关心当前这一家的表单。 */
function renderProvider(provider: ProviderDraftInput, knownKinds: string[] = []): string {
  return renderToStaticMarkup(
    <ProvidersPanel
      config={{
        ...CONFIG,
        known_kinds:
          knownKinds.length > 0
            ? knownKinds
            : ['openai_compatible', 'cloudflare_embedding', 'siliconflow_rerank', 'local', 'fake'],
      }}
      providers={[provider]}
      resolutions={[]}
      selectedId={provider.provider_id}
      saving={false}
      dirtyCount={1}
      onSelect={NOOP}
      onPatch={NOOP}
      onAddProvider={NOOP}
      onDiscard={NOOP}
      onSave={NOOP}
      onSecretsChanged={NOOP}
      onShowHistory={NOOP}
      testMode={false}
      logDenials={true}
      onTestMode={NOOP}
      onLogDenials={NOOP}
      usage={{}}
    />,
  );
}

describe('下拉值与草稿的互转', () => {
  it('跟随槽位 → FOLLOW；单独指定 → provider::model', () => {
    expect(roleSelectValue(undefined)).toBe(FOLLOW_SLOT);
    expect(roleSelectValue(profile({ profile: 'dialogue.fast' }))).toBe(FOLLOW_SLOT);
    expect(
      roleSelectValue(
        profile({ profile: 'dialogue.fast', mode: 'explicit', provider_id: 'main', model_id: 'm-fast' }),
      ),
    ).toBe('main::m-fast');
  });

  it('模型 ID 里带 `::` 也能切开：只按第一个分隔符', () => {
    expect(parseRoleSelectValue('main::vendor::model')).toEqual({
      providerId: 'main',
      modelId: 'vendor::model',
    });
  });

  it('FOLLOW → null（调用方据此清掉单独指定）', () => {
    expect(parseRoleSelectValue(FOLLOW_SLOT)).toBeNull();
  });
});

describe('四种来源的判定', () => {
  it('source / fell_back 决定行状态', () => {
    expect(rowStateOf(undefined)).toBe('unavailable');
    expect(rowStateOf(resolution({ profile: 'a', source: 'unavailable' }))).toBe('unavailable');
    expect(rowStateOf(resolution({ profile: 'a', source: 'explicit' }))).toBe('explicit');
    /*
     * 旧版兼容必须**单独一档**：它既不是用户选的，也不是槽位来的，而是
     * "没配时借主对话模型代跑"的历史行为。混进「继承」会让人以为是自己配的
     * 槽位在生效；混进「不可用」又会让人去修一个其实在正常工作的地方。
     */
    expect(rowStateOf(resolution({ profile: 'a', source: 'legacy_fallback' }))).toBe('legacy');
    expect(rowStateOf(resolution({ profile: 'a', source: 'slot' }))).toBe('inherit');
    expect(
      rowStateOf(resolution({ profile: 'a', source: 'slot', fell_back: true })),
    ).toBe('fell_back');
  });

  it('行尾说明说清"怎么来的"或"为什么没有"', () => {
    expect(
      effectiveNote(
        resolution({
          profile: 'dialogue.fast',
          source: 'explicit',
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
        SLOTS,
      ),
    ).toBe('你单独选的');

    expect(
      effectiveNote(resolution({ profile: 'dialogue.fast', source: 'slot', slot: 'fast' }), SLOTS),
    ).toBe('跟随「快速」');

    // 「已回退」必须同时说出**你选的是什么**——否则用户不知道被丢掉的是哪个
    const note = effectiveNote(
      resolution({
        profile: 'dialogue.fast',
        source: 'slot',
        slot: 'fast',
        fell_back: true,
        requested: { provider_id: 'main', model_id: 'm-big' },
      }),
      SLOTS,
    );
    expect(note).toContain('m-big');
    expect(note).toContain('快速');

    expect(
      effectiveNote(
        resolution({ profile: 'memory.extract', source: 'unavailable', reason: '「创作」槽位还没有配模型' }),
        SLOTS,
      ),
    ).toBe('「创作」槽位还没有配模型');

    expect(effectiveNote(undefined, SLOTS)).toBe('正在计算…');
  });
});

describe('职责行渲染', () => {
  it('「跟随默认」是下拉第一项，并且写明跟的是哪个槽位', () => {
    const html = render({
      profiles: [profile({ profile: 'dialogue.fast' })],
      resolutions: [
        resolution({
          profile: 'dialogue.fast',
          source: 'slot',
          slot: 'fast',
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
      ],
    });
    expect(html).toContain('跟随默认 · 快速');
    expect(html).toContain('ms-badge--inherit');
    expect(html).toContain('继承');
  });

  it('单独指定：实线（不是 inherit 类）+ 橄榄绿标签', () => {
    const html = render({
      profiles: [
        profile({
          profile: 'dialogue.fast',
          mode: 'explicit',
          provider_id: 'main',
          model_id: 'm-fast',
        }),
      ],
      resolutions: [
        resolution({
          profile: 'dialogue.fast',
          source: 'explicit',
          slot: 'fast',
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
      ],
    });
    expect(html).toContain('ms-badge--explicit');
    expect(html).toContain('单独指定');
    expect(html).not.toContain('ms-select--inherit');
  });

  it('不可用时行尾写「不可用」并附原因', () => {
    const html = render({
      profiles: [profile({ profile: 'perception.vision' })],
      resolutions: [
        resolution({
          profile: 'perception.vision',
          source: 'unavailable',
          slot: 'vision',
          reason: '「识图」槽位还没有配模型',
        }),
      ],
    });
    expect(html).toContain('不可用');
    expect(html).toContain('「识图」槽位还没有配模型');
    expect(html).toContain('ms-badge--unavailable');
  });

  it('已回退：红色 + 明确写出被丢掉的那个模型', () => {
    const html = render({
      profiles: [
        profile({
          profile: 'dialogue.fast',
          mode: 'explicit',
          provider_id: 'main',
          model_id: 'm-big',
        }),
      ],
      resolutions: [
        resolution({
          profile: 'dialogue.fast',
          source: 'slot',
          slot: 'fast',
          fell_back: true,
          requested: { provider_id: 'main', model_id: 'm-big' },
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
      ],
    });
    expect(html).toContain('已回退');
    expect(html).toContain('ms-badge--fell_back');
    expect(html).toContain('m-big');
    // 实际跑的是槽位那个
    expect(html).toContain('m-fast');
  });

  it('只渲染服务端说可绑的职责', () => {
    const html = render({
      resolutions: [
        resolution({ profile: 'dialogue.fast', source: 'slot', slot: 'fast' }),
        resolution({ profile: 'memory.extract', source: 'unavailable', reason: 'x' }),
      ],
    });
    expect(html).toContain('dialogue.fast');
    expect(html).not.toContain('memory.extract');
    expect(html).not.toContain('dialogue.deep');
  });

  it('每个职责都有「实际生效」这一列', () => {
    const html = render({
      resolutions: [
        resolution({
          profile: 'dialogue.fast',
          source: 'slot',
          slot: 'fast',
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
      ],
    });
    expect(html).toContain('实际生效');
  });
});

describe('默认槽位卡片', () => {
  it('五个槽位都在，配过的那张写明模型与"谁跟着它"', () => {
    const html = render();
    for (const label of ['快速', '创作', '识图', '视频', '日常']) {
      expect(html).toContain(label);
    }
    expect(html).toContain('m-fast');
    expect(html).toContain('日常对话 跟着它');
    // 没配的槽位不能显示成一个假模型
    expect(html).toContain('还没有配模型');
  });

  it('「视频」是独立槽位：卡片自己写着"谁跟着它"，不和识图混用', () => {
    const html = render();
    // 槽位卡片的 hint 来自服务端 `SLOT_HINTS`，不是前端编的
    expect(html).toContain('必须能看懂视频');
    // 职责行「看视频」由 PRIMARY_ROLES ∩ known_profiles 决定
    expect(html).toContain('看视频');
    expect(html).toContain('perception.video');
  });
});

describe('摘要计数与徽标必须一一对应（验收 M2/R4）', () => {
  it('旧版兼容**单列**，不再算进「继承」', () => {
    const html = render({
      resolutions: [
        resolution({
          profile: 'dialogue.fast',
          source: 'explicit',
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
        resolution({ profile: 'dialogue.group', source: 'slot', slot: 'daily' }),
        resolution({ profile: 'perception.vision', source: 'legacy_fallback', slot: 'creation' }),
        resolution({ profile: 'perception.video', source: 'unavailable', reason: 'x' }),
      ],
    });
    // 4 行 = 1 单独指定 + 1 继承 + 1 旧版兼容 + 1 不可用
    expect(html).toContain('4 个职责');
    expect(html).toContain('1 个单独指定');
    expect(html).toContain('1 个继承');
    expect(html).toContain('1 个旧版兼容');
    expect(html).toContain('1 个不可用');
    // 徽标里也各有一个
    expect(html).toContain('旧版兼容');
  });

  it('没有旧版兼容时不显示这一项（不写 0 占位）', () => {
    const html = render({
      resolutions: [resolution({ profile: 'dialogue.fast', source: 'explicit' })],
    });
    expect(html).not.toContain('个旧版兼容');
  });

  it('「可见职责」= 服务端 known_profiles ∩ 分组：tab 与列表同一个数', () => {
    // CAPABILITY_CONFIG 的 known_profiles 是 7 条，与列表行数一致
    expect(visibleRoleKeys(CAPABILITY_CONFIG.known_profiles)).toHaveLength(7);
    // 服务端没说的角色不渲染（多一行绑了没用，比少一行更糟）
    expect(visibleRoleKeys(['dialogue.fast'])).toEqual(['dialogue.fast']);
    expect(visibleRoleKeys([])).toEqual([]);
    expect(visibleRoleKeys(undefined)).toEqual([]);
    // 拼错/未知的角色名不占一行
    expect(visibleRoleKeys(['dialogue.fast', 'no.such.role'])).toEqual(['dialogue.fast']);
  });
});

describe('行尾说明不留 1–3 字孤行（验收 R1/R3）', () => {
  it('旧版兼容说明缩短：「（旧版行为）」交给徽标说，行尾不重复', () => {
    const html = render({
      resolutions: [
        resolution({ profile: 'dialogue.fast', source: 'legacy_fallback', slot: 'creation' }),
      ],
    });
    expect(html).toContain('没配模型，暂时借主对话的模型代跑');
    expect(html).not.toContain('（旧版行为）');
  });
});

describe('快速配置二次确认', () => {
  const preview: QuickConfigPreview = {
    schema_version: 'v1',
    rows: [
      {
        profile: 'dialogue.fast',
        slot: 'fast',
        slot_label: '快速',
        provider_id: 'main',
        model_id: 'm-fast',
        change: '覆盖你选的 m-big',
        change_kind: 'override',
      },
      {
        profile: 'memory.extract',
        slot: 'creation',
        slot_label: '创作',
        provider_id: null,
        model_id: null,
        change: '仍然不可用：「创作」槽位还没有配模型',
        change_kind: 'unavailable',
      },
    ],
    profiles: [
      profile({ profile: 'dialogue.fast' }),
      profile({ profile: 'memory.extract' }),
    ],
    summary: { total: 2, overrides: 1, unchanged: 0, repairs: 0, still_unavailable: 1 },
  };

  it('逐条列出「职责 → 槽位 → 具体模型 → 变化」，并把要覆盖的说在前面', () => {
    const html = render({ quickConfig: preview });
    expect(html).toContain('快速配置');
    // 桌面端列宽不够时让表格横向滚动，而不是把 daily-chat 拆成 daily-/chat
    expect(html).toContain('ms-quick-wrap');
    expect(html).toContain('覆盖你选的 m-big');
    expect(html).toContain('仍然不可用');
    expect(html).toContain('会覆盖 1 条你单独选的');
    expect(html).toContain('覆盖 2 个职责');
    // 写入后还要按保存——这句不能省，否则用户以为按完就生效了
    expect(html).toContain('保存');
  });
});

describe('草稿比较', () => {
  it('只改 mode 也算一次改动', () => {
    const a = profile({ profile: 'dialogue.fast' });
    const b = profile({
      profile: 'dialogue.fast',
      mode: 'explicit',
      provider_id: 'main',
      model_id: 'm-fast',
    });
    expect(profileEquals(a, b)).toBe(false);
  });

  it('完全相同则是没改', () => {
    const a = profile({ profile: 'dialogue.fast', mode: 'explicit', provider_id: 'main', model_id: 'm' });
    expect(profileEquals(a, { ...a })).toBe(true);
  });

  it('extra 里的账号 ID 也算改动，否则 Cloudflare 的表单保存不了', () => {
    const base = PROVIDERS[0]!;
    expect(providerEquals(base, { ...base, extra: { account_id: 'acc-123' } })).toBe(false);
    // `undefined` 与空对象是同一个意思：没改。
    expect(providerEquals({ ...base, extra: undefined }, { ...base, extra: {} })).toBe(true);
    // 键顺序不影响比较结果。
    expect(
      providerEquals(
        { ...base, extra: { account_id: 'acc', region: 'eu' } },
        { ...base, extra: { region: 'eu', account_id: 'acc' } },
      ),
    ).toBe(true);
  });
});

describe('两种新供应商类型的必填项', () => {
  it('Cloudflare 嵌入：account_id 与 base_url 二选一，两个都没有才报错', () => {
    expect(kindLabel('cloudflare_embedding')).toBe('嵌入（Cloudflare）');

    const missing = providerShapeIssues({
      provider_id: 'cf',
      kind: 'cloudflare_embedding',
    });
    expect(missing.map((issue) => issue.field)).toContain(
      'providers.cf.extra.account_id',
    );

    // 账号 ID 存在 extra.account_id（对应 LING_EMBEDDING_ACCOUNT_ID）。
    expect(
      providerShapeIssues({
        provider_id: 'cf',
        kind: 'cloudflare_embedding',
        extra: { account_id: 'acc-123' },
      }),
    ).toEqual([]);

    // 只给完整 base_url 也合法：环境变量那套一直支持这种配法，
    // 前端不能比后端更严。
    expect(
      providerShapeIssues({
        provider_id: 'cf',
        kind: 'cloudflare_embedding',
        base_url: 'https://proxy.example/cf/ai/v1',
      }),
    ).toEqual([]);
  });

  it('SiliconFlow 重排：只要 base_url，不套用 Cloudflare 的 account_id 规则', () => {
    expect(kindLabel('siliconflow_rerank')).toBe('重排（SiliconFlow）');

    const missing = providerShapeIssues({
      provider_id: 'sf',
      kind: 'siliconflow_rerank',
    });
    expect(missing.map((issue) => issue.field)).toEqual(['providers.sf.base_url']);
    expect(missing.some((issue) => issue.field.includes('account_id'))).toBe(false);

    expect(
      providerShapeIssues({
        provider_id: 'sf',
        kind: 'siliconflow_rerank',
        base_url: 'https://api.siliconflow.cn/v1',
      }),
    ).toEqual([]);
  });

  it('表单：Cloudflare 显示账号 ID 输入；SiliconFlow 不显示它', () => {
    const cf = MEMORY_PROVIDERS.find((item) => item.provider_id === 'cf')!;
    const cfHtml = renderProvider({ ...cf, extra: {} });
    expect(cfHtml).toContain('嵌入（Cloudflare）');
    expect(cfHtml).toContain('账号 ID（Account ID）');
    // 两样都没填时给出"发布前还差…"，但不拦保存草稿。
    expect(cfHtml).toContain('发布前还差');

    const sf = MEMORY_PROVIDERS.find((item) => item.provider_id === 'sf')!;
    const sfHtml = renderProvider(sf);
    expect(sfHtml).toContain('重排（SiliconFlow）');
    expect(sfHtml).not.toContain('账号 ID（Account ID）');
  });
});

/** 从渲染结果里切出某个下拉的内容：用来断言"选项里有没有某一家"。 */
function selectMarkup(html: string, label: string): string {
  const start = html.indexOf(`aria-label="${label}"`);
  const end = html.indexOf('</select>', start);
  return start >= 0 && end > start ? html.slice(start, end) : '';
}

describe('按能力过滤绑定下拉', () => {
  it('每个职责只列具备该能力的供应商', () => {
    // 这是这轮设计的核心收益：选不到调不通的组合。
    expect(
      providersForProfile(MEMORY_PROVIDERS, 'chat', null).map((item) => item.provider_id),
    ).toEqual(['main']);
    expect(
      providersForProfile(MEMORY_PROVIDERS, 'embed', null).map((item) => item.provider_id),
    ).toEqual(['cf']);
    expect(
      providersForProfile(MEMORY_PROVIDERS, 'rerank', null).map((item) => item.provider_id),
    ).toEqual(['sf']);
  });

  it('渲染出来的下拉也按能力分开：嵌入里没有对话供应商', () => {
    const html = render({ providers: MEMORY_PROVIDERS, config: CAPABILITY_CONFIG });
    const embed = selectMarkup(html, '嵌入 用哪个模型');
    expect(embed).toContain('cf');
    expect(embed).not.toContain('main');
    expect(embed).not.toContain('sf');

    const chat = selectMarkup(html, '日常对话 用哪个模型');
    expect(chat).toContain('main');
    expect(chat).not.toContain('cf');
    expect(chat).not.toContain('sf');

    const rerank = selectMarkup(html, '重排 用哪个模型');
    expect(rerank).toContain('sf');
    expect(rerank).not.toContain('main');
    expect(rerank).not.toContain('cf');
  });

  it('默认槽位只收对话供应商；当前已绑的那家保留（不能悄悄消失）', () => {
    expect(providersForChatSlot(MEMORY_PROVIDERS, '').map((item) => item.provider_id)).toEqual([
      'main',
    ]);
    // 历史槽位指向嵌入供应商：保留它，让界面能标警告。
    expect(providersForChatSlot(MEMORY_PROVIDERS, 'cf').map((item) => item.provider_id)).toEqual([
      'main',
      'cf',
    ]);
  });

  it('能力未知（新建 / 刚改类型）时失败关闭，不猜它能做什么', () => {
    const cf = MEMORY_PROVIDERS.find((item) => item.provider_id === 'cf')!;
    const unknown: ProviderDraftInput = { ...cf, capabilities: undefined };
    expect(providersForProfile([unknown], 'embed', null)).toEqual([]);
    // 但当前已绑的那家仍然保留，否则历史配置会从界面消失。
    expect(providersForProfile([unknown], 'embed', 'cf')).toHaveLength(1);
  });
});

describe('历史绑定与能力不匹配', () => {
  it('已绑的对话供应商出现在嵌入职责里时，保留选项并标警告', () => {
    const html = render({
      providers: MEMORY_PROVIDERS,
      config: CAPABILITY_CONFIG,
      profiles: [
        profile({
          profile: 'memory.embed',
          mode: 'explicit',
          provider_id: 'main',
          model_id: 'm-fast',
        }),
      ],
      resolutions: [
        resolution({
          profile: 'memory.embed',
          source: 'explicit',
          effective: { provider_id: 'main', model_id: 'm-fast' },
        }),
      ],
    });

    // 不能悄悄消失：当前绑定的选项还在，用户能看到自己绑了什么。
    expect(html).toContain('main::m-fast');
    // 但必须说清为什么不匹配、发布会被拦下。
    expect(html).toContain('只提供「对话」');
    expect(html).toContain('这个职责需要「嵌入」');
    expect(html).toContain('发布会被服务端拦下');
  });
});

describe('memory_sources：嵌入/重排的配置来自哪', () => {
  it('env：说清来自环境变量，并提示改成网页绑定后 env 不再生效', () => {
    const html = render({
      providers: MEMORY_PROVIDERS,
      config: {
        ...CAPABILITY_CONFIG,
        memory_sources: { 'memory.embed': 'env', 'memory.rerank': null },
      },
      profiles: [],
      resolutions: [
        resolution({
          profile: 'memory.embed',
          source: 'unavailable',
          reason: '这个职责只能单独指定模型：它没有默认槽位',
        }),
        resolution({
          profile: 'memory.rerank',
          source: 'unavailable',
          reason: '重排还没有配',
        }),
      ],
    });

    expect(html).toContain('来自环境变量，改成网页绑定后环境变量不再生效');
    expect(html).toContain('ms-badge--env');
    // 环境变量正在提供能力，不能把这一行报成「不可用」并附上"只能单独指定"。
    expect(html).not.toContain('这个职责只能单独指定模型');
    // 标题计数也要把它算成"来自环境变量"，不能对着正常部署说不可用。
    expect(html).toContain('1 个来自环境变量');
  });

  it('binding / null：分别说成网页绑定与没有配置', () => {
    const bound = render({
      providers: MEMORY_PROVIDERS,
      config: {
        ...CAPABILITY_CONFIG,
        memory_sources: { 'memory.embed': 'binding', 'memory.rerank': null },
      },
      profiles: [
        profile({
          profile: 'memory.embed',
          mode: 'explicit',
          provider_id: 'cf',
          model_id: '@cf/baai/bge-m3',
        }),
      ],
      resolutions: [
        resolution({
          profile: 'memory.embed',
          source: 'explicit',
          effective: { provider_id: 'cf', model_id: '@cf/baai/bge-m3' },
        }),
        resolution({
          profile: 'memory.rerank',
          source: 'unavailable',
          reason: '重排还没有配',
        }),
      ],
    });
    expect(bound).toContain('来自网页绑定');
    expect(bound).toContain('没有配置：这项能力现在是关的');

    const none = render({
      providers: MEMORY_PROVIDERS,
      config: {
        ...CAPABILITY_CONFIG,
        memory_sources: { 'memory.embed': null, 'memory.rerank': null },
      },
      resolutions: [
        resolution({
          profile: 'memory.embed',
          source: 'unavailable',
          reason: '这个职责只能单独指定模型：它没有默认槽位',
        }),
        resolution({
          profile: 'memory.rerank',
          source: 'unavailable',
          reason: '重排还没有配',
        }),
      ],
    });
    expect(none).toContain('没有配置：这项能力现在是关的');
  });
});

// api 在组件里没有直接用到（草稿由 SettingsPage 传下来），这里只确保模块
// 被正确加载、没有循环依赖导致的运行时错误。
describe('模块装载', () => {
  it('api 可达', async () => {
    vi.resetModules();
    const mod = await import('./api');
    expect(typeof mod.api.settingsResolve).toBe('function');
    expect(typeof mod.api.quickConfigPreview).toBe('function');
  });
});
