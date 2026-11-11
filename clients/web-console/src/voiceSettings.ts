/**
 * 语音设置（⑤ 对话与主动性）的**纯逻辑**：档位文案、三家的字段归属、
 * 切 kind 的清理、请求体组装、上传失败的人话。
 *
 * 单独成文件而不是塞进组件：这几条都是"错了不会报错、只会把值写到别处"的
 * 规则，必须能用 `renderToStaticMarkup` 之外的纯函数测试逐条钉住，
 * 而且交互测试与静态渲染共用**同一份**判定。
 *
 * ## 这一层**不做**音色合法性校验
 *
 * "kind 与 id 配套"只有一份判定：后端 `ports/config.py::voice_requirement_issue()`，
 * 设置页校验与运行时降级都调它。前端再造一套，迟早出现"保存时说没问题、
 * 跑起来降级"。所以这里只有一条**前端专属**的、无法由后端替代的检查：
 * `blockingSaveReason()` —— 把显然填不完整的表单**拦在请求之前**给提示，
 * 它给的理由与后端 `voice_requirement_issue()` 的措辞保持同一口径，
 * 但**不是**判定来源：后端返回的错误照样按 `error.field` 标到输入框。
 */
import type { ApiError } from './api';
import { connectorErrorField } from './api';
import type { VoiceKind, VoiceWillingness } from './types';

/* ------------------------------------------------------------------ 档位 */

export const VOICE_WILLINGNESS_ORDER: readonly VoiceWillingness[] = [
  'reserved',
  'natural',
  'expressive',
];

export interface WillingnessOption {
  value: VoiceWillingness;
  /** 档位名（短，给 radio 的标题用）。 */
  label: string;
  /**
   * 一句人话说明，**逐字**取自后端
   * `ports/config.py::VOICE_WILLINGNESS_PROMPTS`（§17.4 原文）。
   *
   * 为什么照抄服务端那张表而不是自己写文案：那张表就是"这一档到底告诉了她
   * 什么"的唯一来源（`DialoguePolicy.voice_prompt()` 直接返回它）。前端另写
   * 一段营销词，就会出现"页面说她更愿意开口、提示词说的却是另一回事"，
   * 而这种不一致没有任何测试能发现——因为两边各自都是"对的"。
   */
  detail: string;
}

export const VOICE_WILLINGNESS_OPTIONS: readonly WillingnessOption[] = [
  {
    value: 'reserved',
    label: '偏向文字',
    detail:
      '你通常偏向文字。对方希望听你说、安慰或情绪表达用声音更合适时，' +
      '可以自然选择语音；无需为用语音凑理由。',
  },
  {
    value: 'natural',
    label: '自然选择',
    detail:
      '你根据当下交流自然选择文字或语音。短句、亲近交流或声音更能传达情绪时可用语音；' +
      '需要复制、列信息或不方便收听时偏向文字。',
  },
  {
    value: 'expressive',
    label: '更愿意开口',
    detail:
      '在适合听语音的情境下，你更愿意开口说话。仍需顾及对方是否方便、内容是否适合阅读；' +
      '不要每条都发语音，也不必为了使用语音改变要说的话。',
  },
];

/** 三档共同的底线：任何一档都不是"禁止"或"必须"语音。 */
export const WILLINGNESS_COMMON_NOTE =
  '这是倾向，不是概率：三档之间没有百分比，也不会按随机数决定。对方明确说「请文字」「不方便听」时，任何一档都以他为准。';

export function willingnessLabel(value: VoiceWillingness | undefined): string {
  const found = VOICE_WILLINGNESS_OPTIONS.find((option) => option.value === value);
  return found ? found.label : '自然选择';
}

/** 把服务端读回的值收敛成三档之一。脏值 / 缺值 → `natural`（与后端同默认）。 */
export function normalizeWillingness(raw: unknown): VoiceWillingness {
  return raw === 'reserved' || raw === 'natural' || raw === 'expressive' ? raw : 'natural';
}

/**
 * 意愿档位**当前能不能真的保存**。
 *
 * ## 这个常量治的是哪一类问题
 *
 * `GET /v1/settings/config` 一直会读出 `voice_willingness`，服务层也认它
 * （`settings/service.py::_DIALOGUE_ENUM_KEYS`），但写入入口 `DialoguePolicyIn`
 * 曾经**没有声明这个字段**——而 `save_draft` 用 `model_dump(exclude_none=True)`，
 * 前端传上去会被 pydantic **静默丢弃**：选了档位、保存成功、刷新回到 natural，
 * 界面上一切正常，没有任何错误。
 *
 * 这就是本项目反复出现的那类问题：控件在、能点、值从来没送到后端。
 *
 * ## 现在为什么是 true
 *
 * `d1564c8` 把 `voice_willingness` 补进了 `DialoguePolicyIn`，并加了一条
 * 三档往返测试（三档都试，因为只测一档的话"恒写 natural"的实现也能过）。
 * 所以这一档现在是真的会存下来的，页面上那句"改了不会生效"的警告必须撤掉
 * ——留着它反而成了另一种假话。
 *
 * 这个常量仍然保留：它和 `WILLINGNESS_NOT_PERSISTED` 一起构成"页面必须如实
 * 说明能力边界"的机制。哪天又出现同类缺口，把它调回 false 即可，界面会自动
 * 恢复警告，不需要改组件。
 */
export const WILLINGNESS_PERSISTS = true;

/** 档位存不下时页面要说的话。`WILLINGNESS_PERSISTS` 为 false 时才显示。 */
export const WILLINGNESS_NOT_PERSISTED =
  '服务端当前的写入入口还不接收这个档位：选它不会报错，但刷新后会回到原来的值。';

/**
 * 音色草稿：输入框里都是**字符串**（受控 input 的值就是字符串），
 * 到 `voicePayloadFrom` 才转成请求体要的类型。
 *
 * 为什么不用数字类型存 `speed`/`vol`/`pitch`：受控 input 的值永远是字符串，
 * 存成数字就要在每次按键时来回转，`""`（清空）会变成 `0` 或 `NaN`，
 * 而 `NaN` 序列化成 JSON 会变成 `null`——后端把它读成"没填"，界面上却还显示
 * 用户敲过的字符。字符串存、出口处转，是唯一能同时保住"清空"和"非法输入"的
 * 存法。
 */
export interface VoiceDraft {
  kind: string | null;
  voice_id: string;
  reference_asset_id: string;
  speed: string;
  vol: string;
  pitch: string;
  emotion: string;
}

/** 音色字段（不含 `kind` 本身）。 */
export type VoiceField =
  | 'voice_id'
  | 'reference_asset_id'
  | 'speed'
  | 'vol'
  | 'pitch'
  | 'emotion';

/** 空草稿：默认值与后端 `VoiceSetting` 的默认值一致（speed/vol=1、pitch=0）。 */
export const EMPTY_VOICE_DRAFT: VoiceDraft = {
  kind: null,
  voice_id: '',
  reference_asset_id: '',
  speed: '1',
  vol: '1',
  pitch: '0',
  emotion: '',
};

/**
 * 每家拥有哪几个字段。
 *
 * **没有统一 `voice_key`**（§17.2 撤销了跨厂商映射表）：同一个"温柔"在三家是
 * 三个不同的值。FishAudio 只有 `reference_id`；MiMo 只有参考音频。所以这里
 * 是"归属"，不是"偏好"——一个字段不属于某家，就是**没有**，不是"用默认值"。
 */
const FIELDS_BY_KIND: Record<string, readonly VoiceField[]> = {
  minimax_tts: ['voice_id', 'speed', 'vol', 'pitch', 'emotion'],
  fishaudio_tts: ['voice_id'],
  mimo_tts: ['reference_asset_id'],
};

/** 一家的展示信息。`value` 与后端 `ProviderKind` 的字面量一致。 */
export interface VoiceKindOption {
  value: VoiceKind;
  label: string;
  detail: string;
}

/**
 * 三家的选项。**顺序与后端 `VOICE_KINDS` 无关**——这里按"用户最容易理解的
 * 先后"排：手填 ID 的两家在前（不需要上传素材），MiMo（要上传）在后。
 *
 * `detail` 说的是"这家要你准备什么"，不是营销词：用户在选之前要知道自己
 * 有没有那个东西（一个 voice_id，或一段参考音频）。
 */
export const VOICE_KIND_OPTIONS: readonly VoiceKindOption[] = [
  {
    value: 'minimax_tts',
    label: 'MiniMax',
    detail: '手填 voice_id；可另行调整语速、音量、音高与情绪。',
  },
  {
    value: 'fishaudio_tts',
    label: 'FishAudio',
    detail: '手填 reference_id，只此一项；这家没有语速/音量/音高参数。',
  },
  {
    value: 'mimo_tts',
    label: '小米 MiMo',
    detail: '上传一段参考音频，用它的音色说话；不需要填 ID。',
  },
];

/** 这家拥有哪些字段。未选 kind → 一个都没有。 */
export function fieldsForKind(kind: string | null): readonly VoiceField[] {
  if (kind === null) return [];
  return FIELDS_BY_KIND[kind] ?? [];
}

/** 那个"按 ID 指声音"的字段在这家叫什么（后端报错也用这套词）。 */
export function voiceIdLabel(kind: string | null): string {
  return kind === 'fishaudio_tts' ? 'reference_id' : 'voice_id';
}

/**
 * 切 kind：**真的把旧家的字段清掉**，不只是不显示。
 *
 * 只隐藏不清的后果很具体：切到 mimmax → 填了 speed=1.4 → 切到 fishaudio
 * （界面不再显示 speed）→ 保存。草稿里那个 1.4 还在，而 `voicePayloadFrom`
 * 若按"草稿里有什么就发什么"就会把它发给 FishAudio——那家在协议上根本没有
 * 这个参数。两道闸都要有：这里清，出口再按归属过滤一次。
 *
 * ## 三种情况
 *
 * - **切到同一家**：原样返回（引用相等），不清用户刚填的东西。
 * - **切到不使用音色的 `null`**：整个回到空草稿。
 * - **换家**：`voice_id` **保留**（两个手填 ID 的家里它都是"指哪个声音"，
 *   用户换家时通常还是想指同一个名字），其余全部回到各自默认值。
 *   清成默认值而不是留空串：留空串会让"重新选回 minimax"看到 speed 输入框是
 *   空的，而它其实有个合法默认值 1——空框看着像"必填没填"。
 */
export function switchVoiceKind(draft: VoiceDraft, kind: string | null): VoiceDraft {
  if (draft.kind === kind) return draft;
  if (kind === null) return { ...EMPTY_VOICE_DRAFT, kind: null };
  const owned = fieldsForKind(kind);
  /*
   * 规则只有一条：**新家拥有的字段 → 回到这一家的默认值；不拥有的 → 空串**。
   *
   * 为什么"拥有就回默认值"而不是"留空串"：留空会让用户切回这家时看到 speed
   * 输入框是空的，而它其实有一个合法默认值 1——空框看着像"必填没填"，用户会
   * 去补一个本来不用填的东西。
   *
   * 为什么"不拥有就空串"而不是留着旧值：那是这一轮要防的串稿。留着的话
   * `voicePayloadFrom` 万一没过滤干净就会把它发给一家协议里根本没这参数的
   * 供应商。两道闸都要有——这里清，出口再按归属过滤一次。
   *
   * 例外只有一个：**`voice_id` 在"新家也要 ID"时保留**。两家手填 ID 的家里
   * 它都是"指哪个声音"，换家时用户通常还想指同一个名字。MiMo 不用 ID，
   * 所以切到 mimo 时它被清空。
   */
  const next: VoiceDraft = { ...EMPTY_VOICE_DRAFT, kind };
  const has = (field: VoiceField): boolean => owned.includes(field);
  next.voice_id = has('voice_id') ? draft.voice_id : '';
  next.reference_asset_id = has('reference_asset_id') ? draft.reference_asset_id : '';
  // 数字与 emotion 一律回到该家默认值（拥有时）或空串（不拥有时）。
  next.speed = has('speed') ? EMPTY_VOICE_DRAFT.speed : '';
  next.vol = has('vol') ? EMPTY_VOICE_DRAFT.vol : '';
  next.pitch = has('pitch') ? EMPTY_VOICE_DRAFT.pitch : '';
  next.emotion = '';
  return next;
}

/** 字符串 → 有限数字，非法/空回落 `fallback`。**绝不产出 `NaN`**。 */
function numberOr(raw: string, fallback: number): number {
  const value = Number(raw.trim());
  return raw.trim() === '' || !Number.isFinite(value) ? fallback : value;
}

/** 草稿 → 请求体里的 `dialogue.voice`。**只发这一家拥有的字段。** */
export function voicePayloadFrom(draft: VoiceDraft): Record<string, unknown> {
  if (draft.kind === null) return { kind: null };
  const owned = fieldsForKind(draft.kind);
  const payload: Record<string, unknown> = { kind: draft.kind };
  for (const field of owned) {
    if (field === 'voice_id') {
      payload.voice_id = draft.voice_id.trim();
    } else if (field === 'reference_asset_id') {
      payload.reference_asset_id = draft.reference_asset_id.trim();
    } else if (field === 'speed') {
      payload.speed = numberOr(draft.speed, 1);
    } else if (field === 'vol') {
      payload.vol = numberOr(draft.vol, 1);
    } else if (field === 'pitch') {
      payload.pitch = Math.round(numberOr(draft.pitch, 0));
    } else if (field === 'emotion') {
      // 留空**不发这一项**（不是发空串）：后端把空串和"没填"当同一件事，
      // 但发空串会在审计里留下一次没有意义的"改了 emotion"。
      const emotion = draft.emotion.trim();
      if (emotion !== '') payload.emotion = emotion;
    }
  }
  return payload;
}

/**
 * 服务端读回的 `dialogue.voice` → 草稿。
 *
 * **不跨家预填**：库里若留着 mimimax 的 `voice_id` 而 `kind` 是 `mimo_tts`，
 * 那个 id 原样保留（后端不报错，前端也不该假装它不存在），但它不属于 mimo，
 * 界面上不会显示、请求里也不会再发出去。
 *
 * 未知 `kind` 当成没选——**不猜**。猜一个会让用户看到一堆他没选过的字段。
 */
export function voiceDraftFrom(raw: unknown): VoiceDraft {
  if (raw === null || typeof raw !== 'object') return { ...EMPTY_VOICE_DRAFT };
  const value = raw as Record<string, unknown>;
  const kind = typeof value.kind === 'string' && value.kind in FIELDS_BY_KIND ? value.kind : null;
  const text = (key: string): string =>
    typeof value[key] === 'string' ? (value[key] as string) : '';
  const numeric = (key: string, fallback: number): string =>
    typeof value[key] === 'number' && Number.isFinite(value[key])
      ? String(value[key])
      : fallback === 0
        ? '0'
        : String(fallback);
  return {
    kind,
    voice_id: text('voice_id'),
    reference_asset_id: text('reference_asset_id'),
    speed: numeric('speed', 1),
    vol: numeric('vol', 1),
    pitch: numeric('pitch', 0),
    emotion: text('emotion'),
  };
}

/**
 * 保存前在**本地**拦下的理由（`null` = 可以保存）。
 *
 * 为什么不等后端 422：`voice_requirement_issue()` 也会拦，但那是往返一次之后
 * 才知道。缺一个 `voice_id` 是本地就能判定的，让用户点保存、等一圈、再看到
 * 错误，是没必要的一次往返。
 *
 * **未选 kind 不拦**：不配音色是合法状态（后端据此降级发文字），不是"配错了"。
 */
export function blockingSaveReason(
  draft: VoiceDraft,
): { field: string; message: string } | null {
  if (draft.kind === null) return null;
  if (draft.kind === 'mimo_tts') {
    if (draft.reference_asset_id.trim() === '') {
      return {
        field: 'dialogue.voice.reference_asset_id',
        message: '音色类型是 mimo_tts，但还没上传参考音频（reference_asset_id）。',
      };
    }
    return null;
  }
  if (draft.voice_id.trim() === '') {
    const label = voiceIdLabel(draft.kind);
    return {
      field: 'dialogue.voice.voice_id',
      message: `音色类型是 ${draft.kind}，但没填 ${label}。`,
    };
  }
  return null;
}

/** 一个字段级的音色错误。`field` 是**全路径**（`dialogue.voice.voice_id`）。 */
export interface VoiceFieldIssue {
  field: string;
  message: string;
}

/** 全路径 → 短名；不认识的一律 `null`（**不给未知字段编位置**，那比不标更误导）。 */
export function voiceFieldOf(raw: string): VoiceField | 'kind' | null {
  const name = raw.startsWith('dialogue.voice.')
    ? raw.slice('dialogue.voice.'.length)
    : raw.startsWith('voice.')
      ? raw.slice('voice.'.length)
      : raw;
  if (name === 'kind') return 'kind';
  if (name === 'voice_id') return 'voice_id';
  if (name === 'reference_asset_id') return 'reference_asset_id';
  if (name === 'speed') return 'speed';
  if (name === 'vol') return 'vol';
  if (name === 'pitch') return 'pitch';
  if (name === 'emotion') return 'emotion';
  return null;
}

/** 后端 422 上挂的 `error.field`（没有就是 null）。 */
export function voiceIssueFromError(error: unknown): VoiceFieldIssue | null {
  const field = connectorErrorField(error);
  if (field === null) return null;
  const message = (error as Partial<ApiError> | null)?.message ?? '这一项不合法';
  return { field, message };
}

/** `validation_errors`（`{field, message, severity}`）→ 音色相关的那几条。 */
export function voiceIssuesFromDraft(
  issues: readonly { field?: string; message?: string; severity?: string }[],
): VoiceFieldIssue[] {
  return issues
    .filter((issue) => (issue.severity ?? 'error') === 'error')
    .filter((issue) => typeof issue.field === 'string' && voiceFieldOf(issue.field) !== null)
    .map((issue) => ({ field: String(issue.field), message: String(issue.message ?? '') }));
}

/* ------------------------------------------------------------ 上传 */

/** 与后端 `api/routes/chat.py::MAX_VOICE_BYTES` 同值（10 MB）。 */
export const MAX_VOICE_BYTES = 10 * 1024 * 1024;

/** 与后端 `VOICE_MIME_BY_CONTAINER` 的键一致。**按内容判定，这里只用于提示。** */
export const VOICE_CONTAINERS: readonly string[] = ['wav', 'mp3', 'flac', 'ogg', 'm4a'];

/**
 * 上传失败 → 人话。
 *
 * 后端把稳定原因放在 `error.detail`（413 `voice_reference_too_large` / 422
 * `voice_reference_empty` / `voice_reference_invalid_audio` / 403 `not_owner` /
 * `bad_origin`）。**按 detail 判，不按状态码猜**：413 也可能是反代给的。
 *
 * 每条都带"下一步"，因为上传是个"改完就能立刻重试"的操作。
 */
export function explainUploadFailure(error: unknown): { reason: string; hint: string; retriable: boolean } {
  const status = (error as Partial<ApiError> | null)?.status;
  const detail = (error as Partial<ApiError> | null)?.detail ?? null;
  const message = (error as { message?: string } | null)?.message ?? '';
  // 文案里的 MB 从常量算，不手写：上限改了而文案没改，用户会按错的尺寸裁剪。
  const limitMb = String(MAX_VOICE_BYTES / (1024 * 1024));

  if (detail === 'voice_reference_too_large' || status === 413) {
    return {
      reason: `参考音频太大（上限 ${limitMb} MB）`,
      hint: `换一段更短的，或先压缩成 16kHz 单声道再传。${message ? `服务端原话：${message}` : ''}`,
      retriable: true,
    };
  }
  if (detail === 'voice_reference_empty') {
    return {
      reason: '这个文件是空的（0 字节）',
      hint: '确认选中的文件不是占位文件后重新选一次。',
      retriable: true,
    };
  }
  if (detail === 'voice_reference_invalid_audio') {
    return {
      reason: '这段内容不是可识别的音频',
      hint: `服务端按文件内容判定，不看扩展名。支持：${VOICE_CONTAINERS.join(' / ')}。改成这几种再传。`,
      retriable: true,
    };
  }
  if (detail === 'not_owner') {
    return {
      reason: '只有所有者能上传参考音频',
      hint: '用所有者账号登录后再传。',
      retriable: false,
    };
  }
  if (detail === 'bad_origin') {
    return {
      reason: '这个请求的来源不可信，被拒绝了',
      hint: '从控制台本身的地址打开页面再试（反代改了 Host 会让它判成跨站）。',
      retriable: false,
    };
  }
  return {
    reason: '上传失败',
    hint: message || '可以重试一次；持续失败请查看服务端日志。',
    retriable: true,
  };
}

/** 字节数 → 人能读的大小。上传成功后展示"传进来的是什么"。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '未知大小';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}
