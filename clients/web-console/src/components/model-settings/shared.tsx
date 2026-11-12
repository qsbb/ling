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
  MemorySource,
  ProviderView,
  SettingsConfig,
  SlotDraftInput,
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
 * 设置页主视图里真会生效的职责。
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
    // 与「看图片」分开列：能看图片的模型大多看不了视频。合成一条，
    // 用户就只能二选一——为了图片配便宜的、视频跟着废，或者为了视频配贵的。
    key: 'perception.video',
    label: '看视频',
    short: '视频',
    // 文案要短：行内这一列只有 ~340px，长句会折出「图共用」3 字孤行（验收 G1）。
    hint: '把视频转成描述；她主动想看时才读，不和看图共用',
  },
  {
    key: 'memory.extract',
    label: '记忆提炼',
    short: '记忆',
    hint: '从对话里抽长期记忆，决定她记不记得住',
  },
  {
    key: 'speech.asr',
    label: '语音识别',
    short: '识别',
    hint: '把收到的语音转成文字，交给对话模型理解',
  },
];

/**
 * 记忆检索的两条职责：它们不是 chat 协议，也没有默认槽位。
 *
 * 列在这里而不是和 PRIMARY_ROLES 混在一起，是为了让分组文案能说清
 * "这两条只在检索时触发"。是否真的渲染仍由服务端 `known_profiles` 决定。
 */
export const EXTRA_ROLES: RoleMeta[] = [
  {
    key: 'memory.embed',
    label: '嵌入',
    short: '嵌入',
    hint: '把文字存成向量；换个说法也能想起来，靠的是它',
  },
  {
    key: 'memory.rerank',
    label: '重排',
    short: '重排',
    hint: '先粗筛再精排，让真正相关的那条排在前面',
  },
];

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
  // 非对话能力：请求体与返回结构都和 chat 不同，所以单独列出来，
  // 不混进「OpenAI 兼容」里让用户换个地址就以为能用。
  { value: 'cloudflare_embedding', label: '嵌入（Cloudflare）' },
  { value: 'siliconflow_rerank', label: '重排（SiliconFlow）' },
  { value: 'siliconflow_asr', label: '语音识别（SiliconFlow）' },
  { value: 'dashscope_asr', label: '语音识别（百炼 Qwen）' },
] as const;

export function kindLabel(kind: string): string {
  return PROVIDER_KIND_OPTIONS.find((item) => item.value === kind)?.label ?? kind;
}

/**
 * 槽位下拉里一个可选项（已经按"能不能选"分好组，渲染层不再判断能力）。
 *
 * `provider_label` 就是分组头：`供应商 · 协议`。**不选**的分组头不带模型 ID，
 * 这样"同名模型分属两家"在界面上也能分清。
 */
export interface SlotOption {
  value: string;
  provider_id: string;
  provider_label: string;
  provider_enabled: boolean;
  model_id: string;
  model_enabled: boolean;
  /** 能不能**新选**它（当前已选但不可用的项仍然返回，只是不许再选）。 */
  selectable: boolean;
}

export interface SlotOptions {
  options: SlotOption[];
  /** 可以新选的条数；0 = 下拉禁用（但已选项仍显示）。 */
  selectableCount: number;
  /** 显示在禁用/空态下的那句人话；空串 = 不需要提示。 */
  emptyHint: string;
}

/**
 * 默认槽位的可选项 —— **纯函数**，便于单测与"同名模型"用例。
 *
 * 规则（与设计 §1 一致）：
 * - 只列能提供 `chat` 的供应商（`providersForChatSlot`，当前已绑的那家保留）；
 * - 停用的供应商或其模型、以及不提供 chat 的**历史**供应商：选项保留显示，
 *   但 `selectable=false`（界面上 `disabled`），并说明怎么换；
 * - 没有供应商 / 没有启用的对话模型 → `selectableCount=0`，给"先添加模型"。
 */
export function slotOptions(
  providers: ProviderDraftInput[],
  currentProviderId: string,
  currentModelId: string,
): SlotOptions {
  const candidates = providersForChatSlot(providers, currentProviderId);
  const options: SlotOption[] = [];
  for (const provider of candidates) {
    const isCurrentProvider = provider.provider_id === currentProviderId;
    const groupSelectable =
      provider.enabled !== false && !bindingCapabilityMismatch(provider, 'chat');
    const providerEnabled = provider.enabled !== false;
    const label = `${provider.provider_id}${providerEnabled ? '' : '（已停用）'} · ${kindLabel(
      provider.kind,
    )}`;
    for (const model of provider.models ?? []) {
      options.push({
        value: `${provider.provider_id}::${model.model_id}`,
        provider_id: provider.provider_id,
        provider_label: label,
        provider_enabled: providerEnabled,
        model_id: model.model_id,
        model_enabled: model.enabled,
        selectable: groupSelectable && model.enabled,
      });
    }
    // 当前绑着的那家如果连"组"都不可选，也要保证已选项在列表里（历史可见）。
    const hasCurrent = options.some(
      (item) => isCurrentProvider && item.model_id === currentModelId,
    );
    if (isCurrentProvider && currentModelId && !hasCurrent) {
      options.push({
        value: `${provider.provider_id}::${currentModelId}`,
        provider_id: provider.provider_id,
        provider_label: label,
        provider_enabled: providerEnabled,
        model_id: currentModelId,
        model_enabled: false,
        selectable: false,
      });
    }
  }
  const selectableCount = options.filter((item) => item.selectable).length;
  const emptyHint =
    providers.length === 0 || candidates.length === 0
      ? '先添加供应商和模型'
      : selectableCount === 0
        ? '先给供应商添加一个启用的对话模型'
        : '';
  return { options, selectableCount, emptyHint };
}

/** 按 `provider_id` 把可选项分组，保持供应商原有顺序（不重排）。 */
export function groupSlotOptions(options: SlotOption[]): {
  providerId: string;
  label: string;
  items: SlotOption[];
}[] {
  const groups: { providerId: string; label: string; items: SlotOption[] }[] = [];
  for (const option of options) {
    const last = groups[groups.length - 1];
    if (last && last.providerId === option.provider_id) {
      last.items.push(option);
    } else {
      groups.push({
        providerId: option.provider_id,
        label: option.provider_label,
        items: [option],
      });
    }
  }
  return groups;
}

/** 槽位下拉的选中值：结构化的 `(provider_id, model_id)` 对。 */
export function slotSelectValue(providerId: string, modelId: string): string {
  if (!providerId || !modelId) return '';
  return `${providerId}::${modelId}`;
}

/** 选中值 → 结构化对；空值/不完整 → null（调用方据此清空槽位）。 */
export function parseSlotSelectValue(
  value: string,
): { providerId: string; modelId: string } | null {
  if (!value) return null;
  const index = value.indexOf('::');
  if (index <= 0) return null;
  const providerId = value.slice(0, index);
  const modelId = value.slice(index + 2);
  if (!providerId || !modelId) return null;
  return { providerId, modelId };
}

/** 草稿里的槽位值是否与已保存的一致（"未保存"标记的唯一判据）。 */
export function slotDraftDirty(
  draft: SlotDraftInput | undefined,
  saved: SlotView | undefined,
): boolean {
  const draftProvider = draft?.provider_id ?? '';
  const draftModel = draft?.model_id ?? '';
  return (
    draftProvider !== (saved?.provider_id ?? '') ||
    draftModel !== (saved?.model_id ?? '')
  );
}

/** 能力 → 人话。服务端传的是 `chat` / `embed` / `rerank` 这类机器名。 */
export function capabilityLabel(capability: string): string {
  switch (capability) {
    case 'chat':
      return '对话';
    case 'embed':
      return '嵌入';
    case 'rerank':
      return '重排';
    case 'tts':
      return '语音合成';
    case 'asr':
      return '语音识别';
    default:
      return capability;
  }
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

/* ------------------------------------------------------- 供应商能力过滤 */

/**
 * 这家供应商**已知**的能力；`null` = 还不知道（草稿里刚改过 kind 或新建）。
 *
 * 能力来自 `/settings/config` 的 `capabilities`，前端**不维护 kind → 能力表**：
 * 那种表一旦和后端走散，界面就会把调不通的组合列出来。
 */
export function knownCapabilities(provider: ProviderDraftInput): string[] | null {
  return Array.isArray(provider.capabilities) ? provider.capabilities : null;
}

/**
 * 这家能不能服务需要 `capability` 的角色。
 *
 * - 角色没有能力要求（`null`/`undefined`）→ 一律放行（服务端不对它做约束）；
 * - 能力未知 → **不放行**（失败关闭）：新供应商要保存一次、改了 kind 的要
 *   保存后重新读，界面宁可不列，也不列一个可能调不通的组合。
 */
export function providerHasCapability(
  provider: ProviderDraftInput,
  capability: string | null | undefined,
): boolean {
  if (!capability) return true;
  const capabilities = knownCapabilities(provider);
  return capabilities !== null && capabilities.includes(capability);
}

/**
 * 某个职责的下拉选项：具备该能力的供应商，**加上当前已绑的那一家**。
 *
 * 当前绑定即使能力不匹配也要留着——历史配置从下拉里消失会让用户以为
 * "从来没绑过"，然后随手换一个。调用方负责给它加警告。
 */
export function providersForProfile(
  providers: ProviderDraftInput[],
  capability: string | null | undefined,
  currentProviderId: string | null | undefined,
): ProviderDraftInput[] {
  return providers.filter(
    (provider) =>
      provider.provider_id === currentProviderId ||
      providerHasCapability(provider, capability),
  );
}

/**
 * 默认槽位（快速/创作/识图/日常）只收对话供应商。
 *
 * 嵌入/重排填进槽位会"发布成功、调用必失败"，所以这里按服务端给的能力
 * 过滤；当前已绑的那家保留，供界面标警告。
 */
export function providersForChatSlot(
  providers: ProviderDraftInput[],
  currentProviderId: string,
): ProviderDraftInput[] {
  return providers.filter(
    (provider) =>
      (provider.enabled || provider.provider_id === currentProviderId) &&
      (provider.provider_id === currentProviderId ||
        providerHasCapability(provider, 'chat')),
  );
}

/** 已经绑了、但供应商标称的能力不含职责所需的那一种（历史配置）。 */
export function bindingCapabilityMismatch(
  provider: ProviderDraftInput | undefined,
  capability: string | null | undefined,
): boolean {
  if (!provider || !capability) return false;
  const capabilities = knownCapabilities(provider);
  return capabilities !== null && !capabilities.includes(capability);
}

/* ----------------------------------------------------- 供应商必填项提示 */

export interface ProviderFieldIssue {
  /** 与后端 `validate_payload` 的 field 同名，方便用户对照发布时的报错。 */
  field: string;
  message: string;
}

/**
 * 供应商表单的**提前提示**：条件和后端 `_provider_shape_issues` 一致，
 * 但后端仍是唯一真相——这里只显示"还差什么"，不阻止保存草稿。
 *
 * 为什么不比后端更严：
 * - 密钥只由后端记 warning（草稿里也没有 `has_secret`），这里不要求；
 * - Cloudflare 的 `account_id` 与 `base_url` 是**二选一**（老部署只用
 *   `LING_EMBEDDING_API_BASE`），只给 base_url 也合法；
 * - fake 是进程内实现，没有端点也不需要密钥。
 */
export function providerShapeIssues(input: {
  provider_id?: string;
  kind: string;
  base_url?: string | null;
  extra?: Record<string, unknown> | null;
}): ProviderFieldIssue[] {
  const providerId = (input.provider_id ?? '').trim() || 'new';
  const kind = input.kind;

  // fake 没有连接信息可填；后端对它是直接返回的。
  if (kind === 'fake') return [];

  const issues: ProviderFieldIssue[] = [];
  const baseUrl = (input.base_url ?? '').trim();
  const isChat =
    kind === 'openai_compatible' ||
    kind === 'anthropic' ||
    kind === 'chat_responses' ||
    kind === 'local';
  const isRerank = kind === 'siliconflow_rerank';
  const isEmbed = kind === 'cloudflare_embedding';
  const isAsr = kind === 'siliconflow_asr' || kind === 'dashscope_asr';
  const validUrl = /^https?:\/\//.test(baseUrl);

  if (isChat || isRerank) {
    if (!baseUrl) {
      issues.push({
        field: `providers.${providerId}.base_url`,
        message: isChat
          ? '兼容接口必须提供 API 地址（base_url）'
          : '重排供应商必须提供 API 地址（base_url，例如 https://api.siliconflow.cn/v1）',
      });
    } else if (!validUrl) {
      issues.push({
        field: `providers.${providerId}.base_url`,
        message: 'API 地址必须以 http:// 或 https:// 开头',
      });
    }
  }

  if (isEmbed) {
    const rawAccountId = input.extra?.account_id;
    const accountId = rawAccountId == null ? '' : String(rawAccountId).trim();
    if (!accountId && !baseUrl) {
      issues.push({
        field: `providers.${providerId}.extra.account_id`,
        message:
          'Cloudflare 嵌入需要账号 ID（extra.account_id，对应环境变量 LING_EMBEDDING_ACCOUNT_ID），或直接给出完整 API 地址',
      });
    } else if (baseUrl && !validUrl) {
      issues.push({
        field: `providers.${providerId}.base_url`,
        message: 'API 地址必须以 http:// 或 https:// 开头',
      });
    }
  }

  if (isAsr && baseUrl && !validUrl) {
    issues.push({
      field: `providers.${providerId}.base_url`,
      message: 'API 地址必须以 http:// 或 https:// 开头',
    });
  }

  return issues;
}

export function toProviderDraft(view: ProviderView | ProviderDraftInput): ProviderDraftInput {
  return {
    provider_id: view.provider_id,
    kind: view.kind,
    base_url: view.base_url,
    enabled: view.enabled,
    extra: 'extra' in view ? view.extra : undefined,
    capabilities: 'capabilities' in view ? view.capabilities : undefined,
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
export type RoleRowState =
  | 'explicit'
  | 'inherit'
  | 'fell_back'
  | 'legacy'
  | 'unavailable';

/**
 * 一行的**界面档位**：`rowStateOf` 再加一档 `env`（环境变量在提供能力）。
 *
 * 徽标和摘要计数都调它——两边各算一次，就会出现"摘要说 4 个继承、徽标里却有
 * 1 个旧版兼容"这种自相矛盾（验收实测过）。
 */
export type RoleBadgeState = RoleRowState | 'env';

export function roleBadgeState(
  resolution: ProfileResolution | undefined,
  profile: ProfileDraftInput | undefined,
  memorySource: MemorySource | undefined,
): RoleBadgeState {
  const draftExplicit = profile?.mode === 'explicit' && Boolean(profile.provider_id);
  // 环境变量提供的记忆能力没有绑定，`profile_resolutions` 会说「不可用」。
  // 那是真的（网页绑定确实没有），但能力本身是开着的——不能把这两件事
  // 混成一个红字，否则用户会去"修"一个正在工作的配置。
  const envSourced = memorySource === 'env' && !draftExplicit;
  return envSourced ? 'env' : rowStateOf(resolution);
}

/** 职责行的分组。摘要/计数/渲染都按它，**不要**在组件里再抄一份顺序。 */
export interface RoleGroup {
  title: string;
  note: string;
  keys: string[];
}

export const ROLE_GROUPS: readonly RoleGroup[] = [
  { title: '对话', note: '2 个 · 主回复链', keys: ['dialogue.fast', 'dialogue.group'] },
  {
    title: '感知',
    note: '2 个 · 发图或发视频时触发',
    keys: ['perception.vision', 'perception.video'],
  },
  // 嵌入/重排不是对话协议，也没有默认槽位：它们只在这里显式绑定，
  // 或者由环境变量提供（来源写在每一行下方）。
  {
    title: '记忆',
    note: '3 个 · 提炼与检索',
    keys: ['memory.extract', 'memory.embed', 'memory.rerank'],
  },
  { title: '语音', note: '收到语音时识别', keys: ['speech.asr'] },
];

/**
 * **界面上真的会渲染出来**的职责，按分组顺序。
 *
 * 判据与服务端的 `known_profiles` 求交集：服务端说不可绑的，界面不该出现
 * 那一行（多一行绑了没用的，比少一行更糟）。tab 上的数字、摘要里的总数、
 * 实际渲染的行都用这一份——三处各算一次，数字迟早互相矛盾。
 */
export function visibleRoleKeys(knownProfiles: readonly string[] | undefined): string[] {
  const offerable = new Set(knownProfiles ?? []);
  return ROLE_GROUPS.flatMap((group) => group.keys).filter(
    (key) => Boolean(roleMeta(key)) && offerable.has(key),
  );
}

export function rowStateOf(resolution: ProfileResolution | undefined): RoleRowState {
  if (!resolution || resolution.source === 'unavailable') return 'unavailable';
  // 旧版兼容要**单独一档**：它既不是用户选的，也不是槽位来的，而是"没配时
  // 借主对话模型代跑"的历史行为。混进「继承」会让人以为是自己配的槽位在生效。
  if (resolution.source === 'legacy_fallback') return 'legacy';
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
  if (resolution.source === 'legacy_fallback') {
    // 文案要短：这一列在 1440 下只有 ~220px，长句会折出 2 字孤行。
    // 「旧版」这件事由徽标（旧版兼容）说，行尾不重复一遍。
    return '没配模型，暂时借主对话的模型代跑';
  }
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

/**
 * `extra` 的稳定比较：键顺序不该影响"有没有改"。
 *
 * Cloudflare 的 `account_id` 就存在这里；漏掉这一项会让「保存」一直是灰的，
 * 用户填了账号 ID 却按不下去（或者按下去也没带上）。
 */
function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null';
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(',')}}`;
}

export function extraEquals(
  left: Record<string, unknown> | undefined,
  right: Record<string, unknown> | undefined,
): boolean {
  return stableJson(left ?? {}) === stableJson(right ?? {});
}

export function providerEquals(a: ProviderDraftInput, b: ProviderDraftInput): boolean {
  return (
    a.provider_id === b.provider_id &&
    a.kind === b.kind &&
    (a.base_url ?? '') === (b.base_url ?? '') &&
    a.enabled === b.enabled &&
    extraEquals(a.extra, b.extra) &&
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
  if (role === 'perception.video') {
    // 胶片框 + 播放三角：与「看图片」的相框一眼分得开。
    return (
      <svg {...common}>
        <rect x="2.5" y="5" width="15" height="10" rx="2" />
        <path d="M8.5 8.2l4 1.8-4 1.8z" />
        <path d="M5.5 5v10M14.5 5v10" opacity="0.45" />
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
  if (role === 'memory.embed') {
    // 一串点连成向量：和「记忆提炼」的文档图标分开。
    return (
      <svg {...common}>
        <circle cx="4" cy="10" r="1.3" />
        <circle cx="10" cy="5.5" r="1.3" />
        <circle cx="10" cy="14.5" r="1.3" />
        <circle cx="16" cy="10" r="1.3" />
        <path d="M5.1 9.3l3.8-2.9M5.1 10.7l3.8 2.9M11.1 6.4l3.8 2.9M11.1 13.6l3.8-2.9" />
      </svg>
    );
  }
  if (role === 'memory.rerank') {
    return (
      <svg {...common}>
        <path d="M3.5 5.5h8M3.5 10h6M3.5 14.5h4" />
        <path d="M13.5 13.5V6.5M11.5 8.5l2-2 2 2" />
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
