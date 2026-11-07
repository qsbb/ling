/**
 * 模型设置页的共用零件：角色元数据、草稿比较、状态点、开关。
 *
 * 这一页把「供应商」和「职责绑定」拆成两个视图，但两边编辑的是**同一份草稿**
 * ——所以比较/复制逻辑必须只有一份，否则一个视图说"没改"、另一个说"改了
 * 1 处"，用户会不知道该信谁。
 */

import type {
  BindingMode,
  ProfileDraftInput,
  ProfileResolution,
  ProviderDraftInput,
  ProviderModel,
  ProviderView,
  SettingsConfig,
  SlotView,
} from '../../types';

export interface RoleMeta {
  key: string;
  label: string;
  /** 模型行里的职责小标签，最多两个字。 */
  short: string;
  hint: string;
}

/**
 * 设置页主视图里真会生效的四个职责。
 *
 * `dialogue.deep` **不在这里**：全仓没有代码按那个角色请求模型，列着它
 * 等于承诺"绑上会更聪明"（服务端也把它放进了 `reserved_profiles`）。
 * 这份清单还会再与服务端返回的 `known_profiles` 求交集，两边各自的增删
 * 都不会让界面多出一行没用的东西。
 */
export const PRIMARY_ROLES: RoleMeta[] = [
  {
    key: 'dialogue.fast',
    label: '日常对话',
    short: '日常',
    hint: '她平时回消息用的模型，最常走的一条路',
  },
  {
    key: 'dialogue.group',
    label: '群聊',
    short: '群聊',
    hint: '群里的回复单独走一条路由',
  },
  {
    key: 'perception.vision',
    label: '看图片',
    short: '图片',
    hint: '把图片转成描述，她才能"看见"你发的东西',
  },
  {
    key: 'memory.extract',
    label: '记忆提炼',
    short: '记忆',
    hint: '从对话里抽长期记忆，决定她记不记得住',
  },
];

/** 不常改、但改不了会真的少能力的那几条。 */
export const EXTRA_ROLES: RoleMeta[] = [];

const ALL_ROLES = [...PRIMARY_ROLES, ...EXTRA_ROLES];

export function roleMeta(key: string): RoleMeta | undefined {
  return ALL_ROLES.find((role) => role.key === key);
}

export function roleLabel(key: string): string {
  return roleMeta(key)?.label ?? key;
}

export function roleShort(key: string): string {
  return roleMeta(key)?.short ?? key;
}

/** 供应商类型：给人看的名字，不是协议名。 */
export const PROVIDER_KIND_OPTIONS = [
  { value: 'openai_compatible', label: 'OpenAI 兼容' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'chat_responses', label: 'chat responses' },
  { value: 'local', label: '本地服务' },
  { value: 'fake', label: '本地测试' },
] as const;

export function kindLabel(kind: string): string {
  return PROVIDER_KIND_OPTIONS.find((item) => item.value === kind)?.label ?? kind;
}

/** `https://192.0.2.10:3002/v1` → `192.0.2.10:3002`。 */
export function hostOf(baseUrl: string | null): string {
  if (!baseUrl) return '未填写地址';
  return baseUrl
    .replace(/^https?:\/\//, '')
    .replace(/\/+$/, '')
    .split('/')[0] ?? '';
}

export function providerGlyph(providerId: string): string {
  const first = Array.from(providerId.trim())[0];
  return first ? first.toUpperCase() : '?';
}

export function toProviderDraft(view: ProviderView | ProviderDraftInput): ProviderDraftInput {
  return {
    provider_id: view.provider_id,
    kind: view.kind,
    base_url: view.base_url,
    enabled: view.enabled,
    extra: 'extra' in view ? view.extra : undefined,
    models: ('models' in view ? view.models ?? [] : []).map((model) => ({ ...model })),
    timeout_seconds: view.timeout_seconds ?? 60,
    max_output_tokens: view.max_output_tokens ?? null,
    daily_token_limit: view.daily_token_limit ?? null,
  };
}

export function toProfileDraft(view: SettingsConfig['profiles'][number]): ProfileDraftInput {
  return {
    profile: view.profile,
    provider_id: view.provider_id,
    model_id: view.model_id,
    fallback_provider_id: view.fallback_provider_id,
    fallback_model_id: view.fallback_model_id,
    enabled: view.enabled,
    mode: view.mode ?? 'explicit',
  };
}

export function emptyProfile(
  profile: string,
  providerId: string,
  modelId: string,
  mode: BindingMode = 'explicit',
): ProfileDraftInput {
  return {
    profile,
    provider_id: providerId,
    model_id: modelId,
    fallback_provider_id: null,
    fallback_model_id: null,
    enabled: true,
    mode,
  };
}

/**
 * 服务端**没有**这条职责的记录时的草稿。
 *
 * 默认按「跟随槽位」：这是改版后的正常状态，不是"没配"。用 `explicit` 当默认
 * 会让界面上每个没动过的职责都显示成"选了空模型"。
 */
export function missingProfile(profile: string): ProfileDraftInput {
  return emptyProfile(profile, '', '', 'slot');
}

/** 槽位键 → 中文名。来源是服务端返回的 `slots`，不是前端手抄的常量。 */
export function slotLabel(slots: SlotView[] | undefined, key: string | null): string {
  if (!key) return '';
  return slots?.find((slot) => slot.slot === key)?.label ?? key;
}

/** 一行职责在界面上属于哪一态。四种，决定边框、标签、颜色。 */
export type RoleRowState = 'explicit' | 'inherit' | 'fell_back' | 'unavailable';

export function rowStateOf(resolution: ProfileResolution | undefined): RoleRowState {
  if (!resolution || resolution.source === 'unavailable') return 'unavailable';
  if (resolution.fell_back) return 'fell_back';
  return resolution.source === 'explicit' ? 'explicit' : 'inherit';
}

/** 行尾一句话：这个模型是怎么来的，或者为什么没有。 */
export function effectiveNote(
  resolution: ProfileResolution | undefined,
  slots: SlotView[] | undefined,
): string {
  if (!resolution) return '正在计算…';
  if (resolution.source === 'unavailable') return resolution.reason;
  if (resolution.source === 'explicit') return '你单独选的';
  const label = slotLabel(slots, resolution.slot);
  if (resolution.fell_back) {
    return `你选的 ${resolution.requested?.model_id ?? '那个模型'} 不可用，暂时用「${label}」`;
  }
  return `跟随「${label}」`;
}

/** 模型清单的稳定比较（顺序有意义：用户看到的就是这个顺序）。 */
export function modelsEqual(
  left: ProviderModel[] | undefined,
  right: ProviderModel[] | undefined,
): boolean {
  const a = left ?? [];
  const b = right ?? [];
  if (a.length !== b.length) return false;
  return a.every((model, index) => {
    const other = b[index];
    if (!other) return false;
    return (
      model.model_id === other.model_id &&
      model.note === other.note &&
      model.enabled === other.enabled
    );
  });
}

export function providerEquals(a: ProviderDraftInput, b: ProviderDraftInput): boolean {
  return (
    a.provider_id === b.provider_id &&
    a.kind === b.kind &&
    (a.base_url ?? '') === (b.base_url ?? '') &&
    a.enabled === b.enabled &&
    modelsEqual(a.models, b.models) &&
    a.timeout_seconds === b.timeout_seconds &&
    (a.max_output_tokens ?? null) === (b.max_output_tokens ?? null) &&
    (a.daily_token_limit ?? null) === (b.daily_token_limit ?? null)
  );
}

export function profileEquals(a: ProfileDraftInput, b: ProfileDraftInput): boolean {
  // `mode` 必须参与比较：只把 mode 从 slot 改成 explicit（而 provider/model
  // 恰好没动）也是一次真实改动，漏判会让「保存」按钮一直是灰的。
  return (
    a.profile === b.profile &&
    a.mode === b.mode &&
    a.provider_id === b.provider_id &&
    a.model_id === b.model_id &&
    (a.fallback_provider_id ?? '') === (b.fallback_provider_id ?? '') &&
    (a.fallback_model_id ?? '') === (b.fallback_model_id ?? '') &&
    a.enabled === b.enabled
  );
}

/**
 * 这个模型**实际在服务哪些职责**（用于模型行右侧的职责标签）。
 *
 * 用服务端的解析结果，不是"谁的 provider_id/model_id 等于它"：
 * 跟随槽位的职责自己那两个字段是空的，按字段比对会让一个正被「快速」
 * 用着的模型显示成"未绑定职责"——用户于是放心把它停用，然后对话就断了。
 */
export function rolesForModel(
  resolutions: ProfileResolution[],
  providerId: string,
  modelId: string,
): ProfileResolution[] {
  return resolutions.filter(
    (resolution) =>
      resolution.effective?.provider_id === providerId &&
      resolution.effective?.model_id === modelId,
  );
}

export function formatAgo(iso: string | undefined, now = Date.now()): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return '';
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时前`;
  const date = new Date(then);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

export function StatusDot({ tone }: { tone: 'ok' | 'warn' | 'danger' | 'idle' }) {
  return <span className={`ms-dot ms-dot--${tone}`} aria-hidden="true" />;
}

export function MsSwitch({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`ms-switch${checked ? ' ms-switch--on' : ''}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="ms-switch__thumb" />
    </button>
  );
}

/** 角色图标：不用 emoji，用细线 SVG，和全站图标一致。 */
export function RoleIcon({ role, size = 16 }: { role: string; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 20 20',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.5,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
  if (role === 'perception.vision') {
    return (
      <svg {...common}>
        <rect x="2.5" y="4" width="15" height="12" rx="2" />
        <circle cx="7.2" cy="8.2" r="1.2" />
        <path d="M3.2 14.4l3.8-3.6 3 2.8 2.6-2.4 4.2 3.6" />
      </svg>
    );
  }
  if (role === 'dialogue.deep') {
    return (
      <svg {...common}>
        <path d="M4 15.5c2.6 0 3.2-1.4 3.2-3.4V7.2c0-1.6 1-2.7 2.6-2.7 1.5 0 2.4.9 2.4 2.2 0 1.2-.8 2-2 2" />
        <path d="M12.2 12.8h3.3M13.8 11.2v3.2" />
      </svg>
    );
  }
  if (role === 'memory.extract') {
    return (
      <svg {...common}>
        <path d="M6.5 3.5h7v13h-7z" />
        <path d="M8.5 7h3M8.5 10h3M8.5 13h2" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M3.5 9.6c0-3 2.6-5.3 6.3-5.3 3.6 0 6.2 2.3 6.2 5.3s-2.6 5.3-6.2 5.3c-.8 0-1.6-.1-2.3-.3l-3.2 1.2 1-2.7c-1.1-.9-1.8-2.1-1.8-3.5z" />
    </svg>
  );
}

export type PanelMessage = { kind: 'ok' | 'error' | 'warn'; text: string; detail?: string };

export function MessageBanner({ message }: { message: PanelMessage }) {
  return (
    <div className={`ms-banner ms-banner--${message.kind}`} role="status">
      <div>{message.text}</div>
      {message.detail ? <div className="ms-banner__detail">{message.detail}</div> : null}
    </div>
  );
}
