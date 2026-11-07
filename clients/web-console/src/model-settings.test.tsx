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
import { effectiveNote, profileEquals, rowStateOf } from './components/model-settings/shared';
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
  known_profiles: ['dialogue.fast', 'dialogue.group', 'perception.vision'],
  slots: SLOTS,
  profile_resolutions: [],
};

const NOOP = () => undefined;

function render(overrides: {
  profiles?: ProfileDraftInput[];
  resolutions?: ProfileResolution[];
  quickConfig?: QuickConfigPreview | null;
  config?: SettingsConfig;
} = {}): string {
  return renderToStaticMarkup(
    <RolesPanel
      config={overrides.config ?? CONFIG}
      providers={PROVIDERS}
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
  it('四个槽位都在，配过的那张写明模型与"谁跟着它"', () => {
    const html = render();
    for (const label of ['快速', '创作', '识图', '日常']) {
      expect(html).toContain(label);
    }
    expect(html).toContain('m-fast');
    expect(html).toContain('日常对话 跟着它');
    // 没配的槽位不能显示成一个假模型
    expect(html).toContain('还没有配模型');
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
