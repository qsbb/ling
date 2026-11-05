/**
 * 记忆内容页（`/memory`）。
 *
 * 页面上有四件不同性质的事，必须各自可见、互不掩盖：
 *
 * 1. **核心记忆**（`GET/PUT/DELETE /v1/core-memory`）：每轮常驻，20 条上限；
 *    同 label 覆盖而不是新增；删除是软删（active=false），没有恢复入口。
 * 2. **记忆列表**（`GET /v1/memory`）：按 kind 分段、时间倒序；默认**不显示**
 *    已撤回/已归档，墓碑要显式 `include=retracted|archived|all` 才取回。
 * 3. **单条操作**：撤回（必填理由）、纠正（supersede，旧条目变「已被纠正」）。
 * 4. **危险区**：遗忘全部（`POST /v1/memory/forget`）——二次确认 + 服务端返回的
 *    影响行数 + 明说不可撤销。
 * 5. **「记忆与学习」调参 tab**（`?tab=settings`）：原 `/settings/memory` 的 4 个参数。
 *
 * 边界（读契约冻结时验收方点名的缺口，**不假装支持**）：
 * - **没有单条详情端点**（`GET /v1/memory/{id}` 不存在）：每行的「详情」只用列表
 *   已返回的字段渲染，不做二次请求，也不编造字段。
 * - **没有分页**（没有 offset/cursor，只有 limit 1..200）：界面只说「最多显示 N 条」，
 *   截断时明确告诉人「更早的条目在接口层面不可达」，不画假翻页。
 * - 无 `memory_episodes` / 实体 / 向量 / 检索日志读取端点：一律不做。
 *
 * 另一条硬要求：**任何一块读失败都不许盖住整页**。核心记忆 403 时列表照常显示，
 * 反之亦然——分段级错误条与整页错误面板是两种不同的东西。
 */
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { navigate, useLocationHref } from '../route';
import type {
  CoreMemoryInput,
  CoreMemoryItem,
  CoreMemoryListResponse,
  MemoryInclude,
  MemoryListResponse,
  MemoryRecord,
} from '../types';
import { AlertIcon, PencilIcon, RefreshIcon } from './Icons';
import { PolicyForm, type PolicyField } from './PolicyForm';
import '../memory.css';

/* ==================================================================== 文案表 */

/** kind 的顺序与说法。`episode` 是发生过的事，`assertion` 是事实，`procedure` 是做法。 */
const KIND_ORDER = ['episode', 'assertion', 'procedure'] as const;

const KIND_LABEL: Record<string, string> = {
  episode: '情节 · 发生过什么',
  assertion: '断言 · 关于你的事实',
  procedure: '程序 · 做事的方式',
};

const KIND_SHORT: Record<string, string> = {
  episode: '情节',
  assertion: '断言',
  procedure: '程序',
};

/** 认不出来的 kind 原样显示，不吞、不瞎翻译。 */
export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? (kind || '未分类');
}

export function kindShort(kind: string): string {
  return KIND_SHORT[kind] ?? (kind || '未分类');
}

/**
 * 来源（`contracts/v1.Origin` 的闭集）。
 *
 * `model_inference` 单独标「推测（不是事实）」：它可能落在列表里（历史数据），
 * 但绝不能看起来跟「你说的」一样可信。
 */
const ORIGIN_LABEL: Record<string, string> = {
  simulated: '模拟',
  user_statement: '你说的',
  external_observation: '观察到的',
  verified_source: '查到的',
  tool_receipt: '有回执',
  model_inference: '模型推测',
};

export function originLabel(origin: string): string {
  return ORIGIN_LABEL[origin] ?? (origin || '来源未知');
}

const VISIBILITY_LABEL: Record<string, string> = {
  actor_private: '角色私有',
  subject_private: '仅本人',
  window_private: '仅本窗口',
  group_scope: '群内可见',
  public: '公开',
  admin_only: '仅管理员',
};

export function visibilityLabel(visibility: string): string {
  return VISIBILITY_LABEL[visibility] ?? (visibility || '未知');
}

const SCOPE_LABEL: Record<string, string> = {
  private: '私聊',
  group: '群聊',
  global: '全局',
};

export function scopeLabel(scope: string): string {
  return SCOPE_LABEL[scope] ?? (scope || '未知');
}

/** 核心记忆的写入来源：当前只有 `manual`（核心记忆写入接口不带 origin 参数）。 */
export function coreOriginLabel(originKind: string): string {
  if (originKind === 'manual') return '手动写入';
  return originKind;
}

/** 列表单次条数：契约只允许 1..200。给三个常用档位，不做「下一页」。 */
export const LIMIT_OPTIONS = [50, 100, 200] as const;

/* ==================================================================== 时间 */

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

function parseIso(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * 绝对时间 `YYYY-MM-DD HH:mm`（本机时区）。
 *
 * 页面上**永远**显示绝对时间；相对时间只是旁边的一行辅助文字。
 * 只给「3 天前」的话，墓碑和现行记忆在对账时没法定位。
 */
export function formatAbsoluteTime(iso: string | null | undefined): string {
  const date = parseIso(iso);
  if (!date) return '时间未知';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(
    date.getHours(),
  )}:${pad2(date.getMinutes())}`;
}

export function formatRelativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  const date = parseIso(iso);
  if (!date) return '';
  const diff = now.getTime() - date.getTime();
  if (diff < 0) return '未来时间';
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

/** 置信度：只显示整数百分比，不伪造精度。 */
export function confidenceLabel(confidence: unknown): string {
  const value = Number(confidence);
  if (!Number.isFinite(value)) return '置信度未知';
  return `置信度 ${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

/* ==================================================================== 墓碑开关 */

export interface TombstoneFlags {
  /** 显示已撤回（`include=retracted`）。 */
  retracted: boolean;
  /** 显示已归档（`include=archived`）。 */
  archived: boolean;
}

/** 两个开关 → 后端的 `include` 取值。映射是契约本身，不是 UI 发明。 */
export function includeMode(flags: TombstoneFlags): MemoryInclude | null {
  if (flags.retracted && flags.archived) return 'all';
  if (flags.retracted) return 'retracted';
  if (flags.archived) return 'archived';
  return null;
}

/** 当前可见集的说明。include 的语义是「追加哪一类墓碑」，不是「只查哪一类」。 */
export function includeSummary(include: MemoryInclude | null): string {
  switch (include) {
    case 'retracted':
      return 'include=retracted：现行记忆 + 已撤回（仍不含已归档）。';
    case 'archived':
      return 'include=archived：现行记忆 + 已归档（仍不含已撤回）。';
    case 'all':
      return 'include=all：全部条目（含既撤回又归档的）。';
    default:
      return '未传 include：默认只看现行记忆（不含已撤回、不含已归档）。';
  }
}

export function tombstoneHint(include: MemoryInclude | null): string {
  return include === null
    ? '已撤回 / 已归档的条目只是「默认不可见」，数据仍在库里供审计——勾选上面的开关可以取回。'
    : '墓碑只用于对账与追溯：它们不参与召回，界面上也只能查看。';
}

/* ==================================================================== 前端筛选 */

export interface MemoryFilters {
  /** 只看某一类；`all` = 不分段筛选（分段仍然显示）。 */
  kind: 'all' | 'episode' | 'assertion' | 'procedure';
  /** 时间范围：只看 created_at 落在最近 N 天内的条目。 */
  range: 'all' | '7d' | '30d';
}

export const DEFAULT_FILTERS: MemoryFilters = { kind: 'all', range: 'all' };

/**
 * 前端筛选（kind / 时间范围）。
 *
 * **后端没有这两个参数**，所以这里只过滤「已经取回的这一批」，界面会明说
 * 「筛选后 X 条 / 已取回 Y 条」。`created_at` 为空的旧数据无法判断时间范围，
 * 只在「全部时间」下显示，不在时间范围里冒充。
 */
export function filterMemories(
  rows: MemoryRecord[],
  filters: MemoryFilters,
  now: Date = new Date(),
): MemoryRecord[] {
  const days = filters.range === 'all' ? null : filters.range === '7d' ? 7 : 30;
  const cutoff = days === null ? null : now.getTime() - days * 86_400_000;
  return rows.filter((row) => {
    if (filters.kind !== 'all' && row.kind !== filters.kind) return false;
    if (cutoff === null) return true;
    const created = parseIso(row.created_at);
    return created !== null && created.getTime() >= cutoff;
  });
}

/** 时间倒序（服务端已经是 created_at DESC，这里保证前端排序不漂）。 */
export function sortByCreatedDesc(rows: MemoryRecord[]): MemoryRecord[] {
  return [...rows].sort((a, b) => {
    const left = parseIso(a.created_at)?.getTime() ?? Number.NEGATIVE_INFINITY;
    const right = parseIso(b.created_at)?.getTime() ?? Number.NEGATIVE_INFINITY;
    return right - left;
  });
}

export interface KindGroup {
  kind: string;
  rows: MemoryRecord[];
}

/** 按 kind 分段。已知三类按固定顺序，未知 kind 排在后面（原样显示）。 */
export function groupByKind(rows: MemoryRecord[]): KindGroup[] {
  const buckets = new Map<string, MemoryRecord[]>();
  for (const row of rows) {
    const bucket = buckets.get(row.kind);
    if (bucket) bucket.push(row);
    else buckets.set(row.kind, [row]);
  }
  const groups: KindGroup[] = [];
  for (const kind of KIND_ORDER) {
    const bucket = buckets.get(kind);
    if (bucket && bucket.length > 0) {
      groups.push({ kind, rows: bucket });
      buckets.delete(kind);
    }
  }
  for (const [kind, bucket] of buckets) {
    if (bucket.length > 0) groups.push({ kind, rows: bucket });
  }
  return groups;
}

/** 截断说明：没有分页，所以必须说清「最多显示 N 条」和截掉了多少。 */
export function limitNotice(data: MemoryListResponse): string | null {
  if (!data.has_more) return null;
  return `只显示了最近 ${data.memories.length} 条；当前口径共 ${data.total} 条，还有 ${
    data.total - data.memories.length
  } 条没有显示。接口没有分页（只有 limit，最大 200），更早的条目在接口层面不可达。`;
}

/* ==================================================================== 分段状态 */

export type SectionStatus = 'loading' | 'ok' | 'error';

export interface SectionState<T> {
  status: SectionStatus;
  data: T | null;
  error: ExplainedError | null;
}

export function loadingSection<T>(): SectionState<T> {
  return { status: 'loading', data: null, error: null };
}

/* ==================================================================== 错误文案 */

/**
 * 全页统一术语：后端的 `AwarenessItemRow` / `list_awareness` 在界面上只叫
 * 「意识条目」。写成一个常量而不是散落的字面量，是为了不再混出
 * 「觉察记录」「awareness」这类同物异名。
 */
export const AWARENESS_TERM = '意识条目';

/** 错误提示要按**资源粒度**写：核心记忆与记忆列表都是账号级资源。 */
export type MemoryResource = 'core' | 'list';

export interface MemoryErrorCopy {
  /** 冒号之后的原因，不带「核心记忆 / 记忆列表」前缀。 */
  reason: string;
  hint: string;
  /** 是否给出可点的重试入口。 */
  retryable: boolean;
}

/** 改密入口的真实地址（`route.ts` 里既有 account 路由，不是虚构链接）。 */
export const ACCOUNT_SETTINGS_HREF = '/settings/account';

/**
 * 账号闸 403 的指路文案。
 *
 * 写成三段常量，是为了让「设置 → 账号」能做成**真可点**的链接（跳
 * `/settings/account`），同时纯文本版（`memoryErrorCopy().hint`）保持同一份措辞，
 * 单测和别处引用不会漂移。
 *
 * 两条事实依据：
 * - **不能承诺自动恢复**。2026-11 验收实测：停在 403 状态下监听 4 秒，
 *   `/v1/memory`、`/v1/core-memory` 请求数 0；`MemoryPage`、`App.tsx` 里也没有
 *   focus / visibilitychange 重新取数。所以恢复只能靠用户点「重新读取全部」
 *   （页脚）或重新进入本页。
 * - 真首次登录时 `App.tsx`（`must_change_password` 分支）会整页接管成改密页，
 *   `/memory` 根本挂载不了。这段 403 文案的真实受众只有「登录后会话中途被外部
 *   置位」这种边缘情况，它不是首次登录的常见路径。
 */
export const ACCOUNT_GATE_COPY = {
  prefix: '这条 403 来自账号闸：首次登录还没改初始口令，到 ',
  link: '设置 → 账号',
  suffix: ' 改完密码后，回来点页面底部的「重新读取全部」，或重新进入本页。',
} as const;

/**
 * 为什么不能直接用共享的 `explainError().reason`：
 *
 * `errors.ts` 里 `forbidden` 的通用原因是「没有权限访问该会话」——「会话」是聊天
 * 场景的资源粒度，不是记忆页的。记忆页的两块数据都不是会话级：
 * - `GET /v1/core-memory` 与 `GET /v1/memory` 都只按 `auth.user.subject_id`
 *   查（`src/ling/api/routes/manage.py`），**没有**任何 scope/consent 检查；
 * - 这两个 GET 上唯一的 403 来自 `current_auth` 的账号闸
 *   （`src/ling/api/deps.py`：`must_change_password` → code=`forbidden`、
 *   detail=`must_change_password`、message「首次登录必须先修改密码」）。
 *
 * 照抄「该会话」会把人引到「换个会话再读」这条死路上。所以这里按真实原因写
 * 账号级说明，并且**不给重试按钮**——账号状态不会因为再请求一次而改变。
 *
 * 三类失败必须能区分：
 * - 403（`forbidden`）：账号/权限问题，重试无用；
 * - 500（`internal`）：服务端故障，可重试，**不能说成权限问题**；
 * - 网络失败（`network_error`）：连不上后端，可重试。
 */
export function memoryErrorCopy(resource: MemoryResource, error: ExplainedError | null): MemoryErrorCopy {
  if (!error) {
    return { reason: '请求失败', hint: '', retryable: true };
  }
  if (error.code === 'forbidden') {
    const scope =
      resource === 'core'
        ? '核心记忆是账号级资源：后端按账号（subject）存取，不按会话分。'
        : '记忆列表是账号级资源：后端按账号（subject）查询，不按会话分。';
    const cause =
      error.detail === 'must_change_password'
        ? `${ACCOUNT_GATE_COPY.prefix}${ACCOUNT_GATE_COPY.link}${ACCOUNT_GATE_COPY.suffix}`
        : '403 表示当前账号没有这项权限，换会话、刷新页面都不会改变结果。';
    return {
      reason: '当前账号没有访问权限',
      hint: `${scope}${cause}`,
      retryable: false,
    };
  }
  if (error.code === 'internal') {
    return {
      reason: '服务端内部错误',
      hint: '这是服务端故障（HTTP 5xx），不是权限问题，也不是你的数据有问题：可以直接重试；连续失败请把下面的 trace 交给维护者查日志。',
      retryable: true,
    };
  }
  if (error.code === 'network_error') {
    return {
      reason: '无法连接服务端',
      hint: '请求没有到达后端（网络中断或后端进程没起来）：确认后端在跑、网络可用后重试。',
      retryable: true,
    };
  }
  // 其它错误原样透出，不替服务端编原因；重试与否听从 `retriable`。
  return { reason: error.reason || '请求失败', hint: error.hint, retryable: error.retriable };
}

/* ==================================================================== 页脚文案 */

export type FooterMode = 'idle' | 'retriable' | 'non-retriable';

export interface MemoryFooterNote {
  mode: FooterMode;
  /** 失败且不可重试的分段名（中文），用于文案里点名。 */
  nonRetriable: string[];
  /** 失败但可重试的分段名（中文）。 */
  retriable: string[];
  text: string;
}

const FOOT_LEAD = '「重新读取全部」会同时刷新核心记忆与记忆列表。';
/** 只在「没有失败」或「失败的分段都可重试」时成立。 */
const FOOT_RETRIABLE = '单个分段失败时，重试按钮就在那个分段里，不会清掉其它已经显示的数据。';

/**
 * 页脚文案随分段状态走，不能无条件说「重试按钮就在那个分段里」。
 *
 * 2026-11 复核实测：403 态的两个分段都是 `data-retriable="false"`、块内
 * **0 个 `<button>`**，而同屏页脚还在说「重试按钮就在那个分段里」——对 403
 * 这是假的，会让人去错误块里找一个不存在的按钮。
 *
 * 分支规则：
 * - 只要有一个失败分段不可重试（403 / 参数错误等），就不再承诺「分段里有重试
 *   按钮」，改成如实说明「该分段不提供重试，请按分段里的说明处理」；
 * - 没有失败、或失败分段都可重试（500 / 网络失败）时，原句仍然成立——页脚是
 *   跟着 `core` / `list` 状态重算的，不会在 403 态残留。
 */
export function memoryFooterNote(
  core: SectionState<CoreMemoryListResponse>,
  list: SectionState<MemoryListResponse>,
): MemoryFooterNote {
  const nonRetriable: string[] = [];
  const retriable: string[] = [];
  const sections: Array<{ name: string; state: SectionState<unknown>; resource: MemoryResource }> = [
    { name: '核心记忆', state: core, resource: 'core' },
    { name: '记忆列表', state: list, resource: 'list' },
  ];
  for (const section of sections) {
    if (section.state.status !== 'error') continue;
    if (memoryErrorCopy(section.resource, section.state.error).retryable) {
      retriable.push(section.name);
    } else {
      nonRetriable.push(section.name);
    }
  }
  if (nonRetriable.length > 0) {
    return {
      mode: 'non-retriable',
      nonRetriable,
      retriable,
      text:
        `${FOOT_LEAD}失败的「${nonRetriable.join('、')}」分段不提供重试按钮` +
        '（403 这类不可重试的错误，再请求一次也不会有不同结果）：先按该分段里写的原因处理' +
        '（若是账号闸，去「设置 → 账号」改完密码），再点这里的「重新读取全部」或重新进入本页。' +
        '重新读取不会清掉其它已经显示的数据。',
    };
  }
  return {
    mode: retriable.length > 0 ? 'retriable' : 'idle',
    nonRetriable,
    retriable,
    text: `${FOOT_LEAD}${FOOT_RETRIABLE}`,
  };
}

/* ==================================================================== 遗忘状态机 */

export type ForgetStep = 'idle' | 'confirm' | 'done';

export interface ForgetState {
  step: ForgetStep;
  /** 二次确认必须显式勾选，按钮才可用——不是点一下就执行。 */
  ack: boolean;
  pending: boolean;
  /** 服务端返回的影响行数；只有 done 时有值。 */
  count: number | null;
  error: ExplainedError | null;
}

export type ForgetAction =
  | { type: 'begin' }
  | { type: 'cancel' }
  | { type: 'ack'; value: boolean }
  | { type: 'submit' }
  | { type: 'succeeded'; count: number }
  | { type: 'failed'; error: ExplainedError }
  | { type: 'reset' };

export const INITIAL_FORGET: ForgetState = {
  step: 'idle',
  ack: false,
  pending: false,
  count: null,
  error: null,
};

/**
 * 二次确认的状态机（纯函数，便于单测）。
 *
 * 只有 `confirm + ack + !pending` 才允许提交；`submit` 只把 pending 置真，
 * 行数一律以服务端返回为准，不在前端猜。
 */
export function forgetReducer(state: ForgetState, action: ForgetAction): ForgetState {
  switch (action.type) {
    case 'begin':
      return { ...INITIAL_FORGET, step: 'confirm' };
    case 'cancel':
      return { ...INITIAL_FORGET };
    case 'ack':
      return state.step === 'confirm' ? { ...state, ack: action.value } : state;
    case 'submit':
      if (state.step !== 'confirm' || !state.ack || state.pending) return state;
      return { ...state, pending: true, error: null };
    case 'succeeded':
      return { step: 'done', ack: true, pending: false, count: action.count, error: null };
    case 'failed':
      return { ...state, pending: false, error: action.error };
    case 'reset':
      return { ...INITIAL_FORGET };
    default:
      return state;
  }
}

export function canSubmitForget(state: ForgetState): boolean {
  return state.step === 'confirm' && state.ack && !state.pending;
}

/* ==================================================================== 操作句柄 */

/**
 * 页面里的写操作。容器实现，展示组件调用。
 *
 * 失败时抛错（展示组件就近显示），成功时由容器刷新对应分段并给出提示。
 */
export interface MemoryHandlers {
  retract(memoryId: string, reason: string): Promise<void>;
  supersede(memoryId: string, text: string, reason: string): Promise<void>;
  saveCore(input: CoreMemoryInput, meta: { overwrote: boolean }): Promise<void>;
  deleteCore(item: CoreMemoryItem): Promise<void>;
  /** 返回服务端给出的影响行数。 */
  forget(): Promise<number>;
  reloadCore(): void;
  reloadList(): void;
}

export interface Notice {
  tone: 'ok' | 'warn';
  text: string;
}

/* ==================================================================== 页头与 tab */

export type MemoryTab = 'content' | 'settings';

/**
 * 从地址推导 tab。
 *
 * `?tab=settings` 是设计里的深链；旧路径 `/settings/memory` 没有 query，
 * 在 Step 4 的重定向前仍要开在调参 tab（否则书签会打开一个内容页）。
 */
export function memoryTabFromHref(href: string): MemoryTab {
  const [rawPath = '', rawQuery = ''] = href.split('?');
  const path = rawPath.replace(/\/+$/, '') || '/';
  const params = new URLSearchParams(rawQuery);
  if (params.get('tab') === 'settings') return 'settings';
  if (path === '/settings/memory') return 'settings';
  return 'content';
}

export function memoryTabHref(tab: MemoryTab): string {
  return tab === 'settings' ? '/memory?tab=settings' : '/memory';
}

/* ==================================================================== 分段外壳 */

/**
 * 错误提示的正文。只有 403 账号闸这一种原因会把「设置 → 账号」渲染成**真链接**
 * （跳 `/settings/account`，`route.ts` 里有这条路由）；其它原因保持纯文本，
 * 不放点了没用的链接。
 */
function ErrorHint({
  copy,
  error,
}: {
  copy: MemoryErrorCopy;
  error: ExplainedError | null;
}): React.ReactElement | null {
  if (!copy.hint) return null;
  if (error?.code === 'forbidden' && error.detail === 'must_change_password') {
    return (
      <p>
        {ACCOUNT_GATE_COPY.prefix}
        <a
          className="mem-error__link"
          href={ACCOUNT_SETTINGS_HREF}
          onClick={(event) => {
            event.preventDefault();
            navigate(ACCOUNT_SETTINGS_HREF);
          }}
        >
          {ACCOUNT_GATE_COPY.link}
        </a>
        {ACCOUNT_GATE_COPY.suffix}
      </p>
    );
  }
  return <p>{copy.hint}</p>;
}

function SectionError({
  title,
  resource,
  error,
  onRetry,
}: {
  title: string;
  resource: MemoryResource;
  error: ExplainedError | null;
  onRetry: () => void;
}): React.ReactElement {
  const copy = memoryErrorCopy(resource, error);
  return (
    <div
      className="mem-error"
      role="alert"
      data-testid="mem-section-error"
      data-error-code={error?.code ?? 'unknown'}
      data-retriable={copy.retryable ? 'true' : 'false'}
    >
      <AlertIcon size={14} />
      <div>
        <strong>{title}读不到：{copy.reason}</strong>
        <ErrorHint copy={copy} error={error} />
        {error?.traceId ? <p className="mem-error__trace">trace：{error.traceId}</p> : null}
        <p className="mem-error__scope">这一块失败不影响页面其它分段；其余数据仍按各自状态显示。</p>
        {copy.retryable ? null : (
          <p className="mem-error__scope" data-testid="mem-section-no-retry">
            这类错误重试不会有不同结果：先按上面的说明处理，再回来重新读取。
          </p>
        )}
      </div>
      {copy.retryable ? (
        <button type="button" className="button button--small" onClick={onRetry}>
          <RefreshIcon /> 重试
        </button>
      ) : null}
    </div>
  );
}

/** 写操作的就地错误（保存 / 删除 / 撤回 / 纠正 / 遗忘）：与分段错误同一套口径。 */
function InlineError({
  error,
  resource,
}: {
  error: ExplainedError | null;
  resource: MemoryResource;
}): React.ReactElement | null {
  if (!error) return null;
  const copy = memoryErrorCopy(resource, error);
  return (
    <div className="mem-error mem-error--inline" role="alert" data-error-code={error.code}>
      <AlertIcon size={13} />
      <div>
        <strong>{copy.reason}</strong>
        <ErrorHint copy={copy} error={error} />
      </div>
    </div>
  );
}

function LoadingLines({ label }: { label: string }): React.ReactElement {
  return (
    <div className="mem-loading" role="status" data-testid="mem-section-loading">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/* ==================================================================== 核心记忆 */

export interface CoreMemorySectionProps {
  state: SectionState<CoreMemoryListResponse>;
  handlers: MemoryHandlers;
  onReload: () => void;
}

const EMPTY_CORE_FORM = { label: '', text: '', priority: '100', scope: 'private' as const };

export function CoreMemorySection({
  state,
  handlers,
  onReload,
}: CoreMemorySectionProps): React.ReactElement {
  const items = state.data?.items ?? [];
  const limit = state.data?.limit ?? 20;
  const [form, setForm] = useState<{ label: string; text: string; priority: string; scope: 'private' | 'group' | 'global' }>(
    EMPTY_CORE_FORM,
  );
  /** 正在编辑的条目 id；null = 新增。编辑不改变「同 label 覆盖」的语义。 */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const trimmedLabel = form.label.trim();
  const conflict = useMemo(
    () => items.find((item) => item.label === trimmedLabel) ?? null,
    [items, trimmedLabel],
  );
  const atLimit = items.length >= limit;
  /** 满 20 条时，只有「覆盖一个已存在的 label」还能保存；新增 label 会 422。 */
  const blockedByLimit = atLimit && !conflict;
  const canSubmit =
    trimmedLabel.length > 0 &&
    form.text.trim().length > 0 &&
    !pending &&
    !blockedByLimit &&
    state.status === 'ok';

  const resetForm = () => {
    setForm(EMPTY_CORE_FORM);
    setEditingId(null);
  };

  const startEdit = (item: CoreMemoryItem) => {
    setForm({
      label: item.label,
      text: item.text,
      priority: String(item.priority),
      scope: item.scope === 'group' || item.scope === 'global' ? item.scope : 'private',
    });
    setEditingId(item.core_id);
    setError(null);
  };

  const submit = async () => {
    if (!canSubmit) return;
    setPending(true);
    setError(null);
    try {
      const parsed = Number(form.priority);
      const priority = Number.isFinite(parsed) ? Math.min(999, Math.max(1, Math.round(parsed))) : 100;
      await handlers.saveCore(
        { label: trimmedLabel, text: form.text, priority, scope: form.scope },
        { overwrote: conflict !== null },
      );
      resetForm();
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setPending(false);
    }
  };

  const remove = async (item: CoreMemoryItem) => {
    setPending(true);
    setError(null);
    try {
      await handlers.deleteCore(item);
      setConfirmDeleteId(null);
      if (editingId === item.core_id) resetForm();
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setPending(false);
    }
  };

  return (
    <section className="panel mem-section" aria-labelledby="mem-core-title" data-testid="mem-core">
      <header className="panel__head">
        <h2 id="mem-core-title">核心记忆</h2>
        <span className="panel__count" data-testid="mem-core-count">
          {state.status === 'ok' ? `${items.length} / ${limit} 条` : '—'}
        </span>
      </header>
      <p className="panel__note">
        这些条目会<strong>随每一轮对话进入上下文</strong>，但<strong>不保证全部注入</strong>：
        群聊里只注入作用域为「群聊 / 全局」的条目（「私聊」条目只在私聊出现）；单轮注入预算
        800 字符（含「- [label]」前缀），超出时<strong>按优先级截断</strong>；会话处于
        「完全隔离（sealed）」模式时不注入。正因为每轮都占上下文，每账号上限才是 {limit} 条。
        同一个 label 再次保存会<strong>覆盖</strong>原条目（文本、优先级、作用域都会换掉，
        不会新增第二条）。删除是软删除：只是停用，数据仍在库里，但页面没有恢复入口。
      </p>

      {state.status === 'loading' ? <LoadingLines label="正在读取核心记忆…" /> : null}

      {state.status === 'error' ? (
        <SectionError title="核心记忆" resource="core" error={state.error} onRetry={onReload} />
      ) : null}

      {state.status === 'ok' && items.length === 0 ? (
        <p className="mem-empty" data-testid="mem-core-empty">
          还没有核心记忆。可以先写一条「称呼」或「边界」，比如 label 填「称呼」、内容填「叫我小林」。
        </p>
      ) : null}

      {state.status === 'ok' && items.length > 0 ? (
        <ul className="mem-core-list" data-testid="mem-core-list">
          {items.map((item) => (
            <li key={item.core_id} className="mem-core-item" data-core-id={item.core_id}>
              <div className="mem-core-item__head">
                <span className="mem-core-item__label">{item.label}</span>
                <span className="mem-core-item__meta">
                  优先级 {item.priority} · {scopeLabel(item.scope)} · 来源 {coreOriginLabel(item.origin_kind)}
                </span>
              </div>
              <p className="mem-core-item__text">{item.text}</p>
              <p className="mem-core-item__time">
                创建 <time dateTime={item.created_at ?? undefined}>{formatAbsoluteTime(item.created_at)}</time>
                {' · '}更新{' '}
                <time dateTime={item.updated_at ?? undefined}>{formatAbsoluteTime(item.updated_at)}</time>
              </p>
              <div className="mem-core-item__actions">
                <button type="button" className="button button--small mem-action" onClick={() => startEdit(item)}>
                  <PencilIcon size={13} /> 编辑
                </button>
                <button
                  type="button"
                  className="button button--small mem-action mem-action--danger"
                  aria-expanded={confirmDeleteId === item.core_id}
                  onClick={() => setConfirmDeleteId(confirmDeleteId === item.core_id ? null : item.core_id)}
                >
                  停用…
                </button>
              </div>
              {confirmDeleteId === item.core_id ? (
                <div className="mem-confirm" role="group" aria-label={`停用核心记忆 ${item.label}`}>
                  <p className="mem-confirm__title">
                    <AlertIcon size={13} /> 确认停用核心记忆「{item.label}」？
                  </p>
                  <p className="mem-confirm__body">
                    这是<strong>软删除</strong>：条目变成 inactive，不再注入每一轮；数据库里仍保留这一行。
                    <strong>页面没有恢复入口</strong>，要恢复只能重新保存一条同 label 的条目。
                  </p>
                  <div className="mem-confirm__actions">
                    <button
                      type="button"
                      className="button button--small mem-action mem-action--danger"
                      disabled={pending}
                      onClick={() => void remove(item)}
                    >
                      {pending ? '停用中…' : '确认停用'}
                    </button>
                    <button type="button" className="button button--small" onClick={() => setConfirmDeleteId(null)}>
                      取消
                    </button>
                  </div>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="mem-core-form" data-testid="mem-core-form">
        <h3 className="mem-core-form__title">
          {editingId ? '编辑 / 覆盖核心记忆' : '新增核心记忆'}
          {atLimit ? <span className="mem-badge mem-badge--warn">已满 {limit} 条</span> : null}
        </h3>

        {atLimit ? (
          <p className="mem-note mem-note--warn">
            已达 {limit} 条上限：新增按钮已禁用。要加新条目，请先停用一条；给已有 label
            重新保存仍然是覆盖，不受上限影响。
          </p>
        ) : null}

        {editingId ? (
          <p className="mem-note">
            正在编辑已有条目；把 label 改成别的名字会变成另一条（可能撞上 20 条上限）。
          </p>
        ) : null}

        {/*
         * 核心记忆 GET 失败时表单仍然可以填（受控输入没有 disabled），但
         * `canSubmit` 要求 `state.status === 'ok'`，所以保存按钮是禁用的。
         * 这条说明必须写在界面上：否则「输入框能打字」会被误读成「能保存」。
         * 403（无权限）时不能写「等重试成功」——那种情况没有重试按钮，也重试无用。
         */}
        {state.status === 'error' ? (
          <p className="mem-note mem-note--warn" data-testid="mem-core-form-blocked">
            {memoryErrorCopy('core', state.error).retryable ? (
              <>
                核心记忆读取失败：下面的表单<strong>可以填写</strong>，但「保存核心记忆」要等上面的
                重试成功、拿到现有条目之后才会启用——否则看不到是否已满 {limit} 条、也判断不了
                同名覆盖，容易盲目写错。
              </>
            ) : (
              <>
                核心记忆读不到（当前账号没有访问权限）：下面的表单<strong>可以填写</strong>，但
                「保存核心记忆」不会启用——读不到现有条目就判断不了是否已满 {limit} 条、也判断不了
                同名覆盖；账号权限修好之前写进去只会盲写。
              </>
            )}
          </p>
        ) : null}

        <div className="mem-core-form__grid">
          <label className="mem-field">
            <span>label（同名覆盖）</span>
            <input
              type="text"
              value={form.label}
              maxLength={64}
              placeholder="例如 称呼 / 边界 / 重要约定"
              onChange={(event) => setForm((current) => ({ ...current, label: event.target.value }))}
            />
          </label>
          <label className="mem-field">
            <span>优先级（1..999，越小越靠前）</span>
            <input
              type="number"
              min={1}
              max={999}
              step={1}
              value={form.priority}
              onChange={(event) => setForm((current) => ({ ...current, priority: event.target.value }))}
            />
          </label>
          <label className="mem-field">
            <span>作用域</span>
            <select
              value={form.scope}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  scope: event.target.value === 'group' ? 'group' : event.target.value === 'global' ? 'global' : 'private',
                }))
              }
            >
              <option value="private">私聊</option>
              <option value="group">群聊</option>
              <option value="global">全局</option>
            </select>
          </label>
        </div>

        <label className="mem-field mem-field--wide">
          <span>内容（1..2000 字）</span>
          <textarea
            value={form.text}
            rows={3}
            maxLength={2000}
            placeholder="一句她每轮都该记得的话"
            onChange={(event) => setForm((current) => ({ ...current, text: event.target.value }))}
          />
        </label>

        {conflict ? (
          <p className="mem-note mem-note--warn" role="status" data-testid="mem-core-overwrite-warning">
            同名 label「{conflict.label}」已存在：保存会<strong>覆盖</strong>它的内容
            （不会新增第二条），创建时间保持 {formatAbsoluteTime(conflict.created_at)}，
            更新时间和内容会变。
          </p>
        ) : null}

        <InlineError error={error} resource="core" />

        <div className="mem-form-actions">
          <button
            type="button"
            className="button button--primary mem-action"
            disabled={!canSubmit}
            onClick={() => void submit()}
          >
            {pending ? '保存中…' : conflict ? '保存并覆盖' : '保存核心记忆'}
          </button>
          {editingId || form.label || form.text ? (
            <button type="button" className="button button--small mem-action" onClick={resetForm} disabled={pending}>
              清空表单
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}

/* ==================================================================== 记忆列表 */

export interface MemoryListSectionProps {
  state: SectionState<MemoryListResponse>;
  include: MemoryInclude | null;
  flags: TombstoneFlags;
  onFlagsChange: (flags: TombstoneFlags) => void;
  limit: number;
  onLimitChange: (limit: number) => void;
  filters: MemoryFilters;
  onFiltersChange: (filters: MemoryFilters) => void;
  handlers: MemoryHandlers;
  onReload: () => void;
}

interface RowAction {
  memoryId: string;
  mode: 'retract' | 'supersede';
  text: string;
}

export function MemoryListSection({
  state,
  include,
  flags,
  onFlagsChange,
  limit,
  onLimitChange,
  filters,
  onFiltersChange,
  handlers,
  onReload,
}: MemoryListSectionProps): React.ReactElement {
  const fetched = state.data?.memories ?? [];
  const filtered = useMemo(() => sortByCreatedDesc(filterMemories(fetched, filters)), [fetched, filters]);
  const groups = useMemo(() => groupByKind(filtered), [filtered]);
  const [action, setAction] = useState<RowAction | null>(null);
  const [reason, setReason] = useState('');
  const [newText, setNewText] = useState('');
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<ExplainedError | null>(null);

  const openAction = (row: MemoryRecord, mode: 'retract' | 'supersede') => {
    setAction({ memoryId: row.memory_id, mode, text: row.text });
    setReason('');
    setNewText(row.text);
    setActionError(null);
  };

  const closeAction = () => {
    setAction(null);
    setActionError(null);
    setPending(false);
  };

  const submitAction = async () => {
    if (!action || pending) return;
    setPending(true);
    setActionError(null);
    try {
      if (action.mode === 'retract') {
        await handlers.retract(action.memoryId, reason.trim());
      } else {
        await handlers.supersede(
          action.memoryId,
          newText.trim(),
          reason.trim() || '用户纠正',
        );
      }
      closeAction();
    } catch (cause) {
      setActionError(explainError(cause));
      setPending(false);
    }
  };

  const trimmedReason = reason.trim();
  const canConfirm =
    action !== null &&
    !pending &&
    (action.mode === 'retract' ? trimmedReason.length > 0 : newText.trim().length > 0);

  return (
    <section className="panel mem-section" aria-labelledby="mem-list-title" data-testid="mem-list">
      <header className="panel__head">
        <h2 id="mem-list-title">记忆列表</h2>
        <span className="panel__count" data-testid="mem-list-count">
          {state.status === 'ok' && state.data
            ? `${state.data.total} 条（口径）· 已取回 ${state.data.memories.length} 条`
            : '—'}
        </span>
      </header>

      <div className="mem-filters">
        <fieldset className="mem-filters__group">
          <legend>墓碑（默认不显示）</legend>
          <label className="mem-check">
            <input
              type="checkbox"
              checked={flags.retracted}
              onChange={(event) => onFlagsChange({ ...flags, retracted: event.target.checked })}
            />
            <span>显示已撤回（include=retracted）</span>
          </label>
          <label className="mem-check">
            <input
              type="checkbox"
              checked={flags.archived}
              onChange={(event) => onFlagsChange({ ...flags, archived: event.target.checked })}
            />
            <span>显示已归档（include=archived）</span>
          </label>
          <p className="mem-filters__note" data-testid="mem-include-summary">
            {includeSummary(include)}
          </p>
          <p className="mem-filters__note">{tombstoneHint(include)}</p>
        </fieldset>

        <div className="mem-filters__row">
          <label className="mem-field mem-field--inline">
            <span>类型</span>
            <select
              value={filters.kind}
              onChange={(event) => {
                const value = event.target.value;
                onFiltersChange({
                  ...filters,
                  kind:
                    value === 'episode' || value === 'assertion' || value === 'procedure'
                      ? value
                      : 'all',
                });
              }}
            >
              <option value="all">全部类型</option>
              <option value="episode">情节</option>
              <option value="assertion">断言</option>
              <option value="procedure">程序</option>
            </select>
          </label>
          <label className="mem-field mem-field--inline">
            <span>时间范围</span>
            <select
              value={filters.range}
              onChange={(event) => {
                const value = event.target.value;
                onFiltersChange({
                  ...filters,
                  range: value === '7d' || value === '30d' ? value : 'all',
                });
              }}
            >
              <option value="all">全部时间</option>
              <option value="7d">最近 7 天</option>
              <option value="30d">最近 30 天</option>
            </select>
          </label>
          <label className="mem-field mem-field--inline">
            <span>单次最多取</span>
            <select value={limit} onChange={(event) => onLimitChange(Number(event.target.value))}>
              {LIMIT_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option} 条
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="mem-filters__note">
          类型与时间范围<strong>只过滤已取回的条目</strong>（后端没有这两个参数，所以不做假筛选）：
          已取回 {fetched.length} 条，筛选后 {filtered.length} 条。
        </p>
        <p className="mem-filters__note" data-testid="mem-page-size">
          接口<strong>没有分页</strong>：本次<strong>最多显示 {limit} 条</strong>
          （可调到 200；更早的条目要多取只能调大 limit，界面不提供翻页）。
        </p>
      </div>

      {state.status === 'loading' ? <LoadingLines label="正在读取记忆列表…" /> : null}

      {state.status === 'error' ? (
        <SectionError title="记忆列表" resource="list" error={state.error} onRetry={onReload} />
      ) : null}

      {state.status === 'ok' && state.data ? (
        <>
          {limitNotice(state.data) ? (
            <p className="mem-note mem-note--warn" role="status" data-testid="mem-limit-notice">
              {limitNotice(state.data)}
            </p>
          ) : null}

          {fetched.length === 0 ? (
            <div className="mem-empty" data-testid="mem-list-empty">
              {include === null ? (
                <>
                  <p>没有现行记忆（未撤回且未归档）。</p>
                  <p>
                    如果她确实记得什么，可能是条目被撤回或归档了——勾选上面的「显示已撤回 /
                    显示已归档」取回墓碑看看。
                  </p>
                </>
              ) : (
                <>
                  <p>没有任何记忆（包括已撤回与已归档）。</p>
                  <p>她还没记住这件事；新记忆会在对话后由记忆抽取任务写入。</p>
                </>
              )}
            </div>
          ) : filtered.length === 0 ? (
            <div className="mem-empty" data-testid="mem-filter-empty">
              <p>已取回 {fetched.length} 条，但没有条目符合当前的类型 / 时间筛选。</p>
              <p>筛选只作用于这一批数据（后端没有类型与时间参数）；调宽条件或点「刷新」重取。</p>
            </div>
          ) : (
            <div className="mem-groups" data-testid="mem-groups">
              {groups.map((group) => (
                <section key={group.kind} className="mem-group" aria-label={kindLabel(group.kind)}>
                  <h3 className="mem-group__title">
                    {kindLabel(group.kind)}
                    <span className="mem-group__count">{group.rows.length} 条</span>
                  </h3>
                  <ul className="mem-list">
                    {group.rows.map((row) => {
                      const archived = row.archived_at !== null;
                      const tombstone = row.retracted || archived;
                      const isAction = action?.memoryId === row.memory_id;
                      const relative = formatRelativeTime(row.created_at);
                      return (
                        <li
                          key={row.memory_id}
                          className={`mem-item${tombstone ? ' mem-item--tombstone' : ''}`}
                          data-testid="mem-row"
                          data-memory-id={row.memory_id}
                        >
                          <div className="mem-item__head">
                            <p className="mem-item__text">{row.text}</p>
                            {tombstone ? (
                              <span className="mem-item__readonly">墓碑：只读</span>
                            ) : (
                              <div className="mem-item__actions">
                                <button
                                  type="button"
                                  className="button button--small mem-action mem-action--danger"
                                  aria-expanded={isAction && action?.mode === 'retract'}
                                  onClick={() => (isAction && action?.mode === 'retract' ? closeAction() : openAction(row, 'retract'))}
                                >
                                  撤回…
                                </button>
                                <button
                                  type="button"
                                  className="button button--small mem-action"
                                  aria-expanded={isAction && action?.mode === 'supersede'}
                                  onClick={() =>
                                    isAction && action?.mode === 'supersede'
                                      ? closeAction()
                                      : openAction(row, 'supersede')
                                  }
                                >
                                  纠正…
                                </button>
                              </div>
                            )}
                          </div>

                          <p className="mem-item__meta">
                            <span className="mem-item__time">
                              记于{' '}
                              <time dateTime={row.created_at ?? undefined}>{formatAbsoluteTime(row.created_at)}</time>
                              {relative ? <span className="mem-item__relative">（{relative}）</span> : null}
                            </span>
                            <span>来源：{originLabel(row.origin)}</span>
                            <span>{confidenceLabel(row.confidence)}</span>
                            <span className="mem-item__kind">{kindShort(row.kind)}</span>
                          </p>

                          <div className="mem-item__badges">
                            {row.retracted ? (
                              <span className="mem-badge mem-badge--tombstone">
                                已撤回{row.retract_reason ? `：${row.retract_reason}` : ''}
                              </span>
                            ) : null}
                            {row.superseded_by ? (
                              <span className="mem-badge mem-badge--superseded">
                                已被纠正（新条目 {row.superseded_by}）
                              </span>
                            ) : null}
                            {row.supersedes ? (
                              <span className="mem-badge">由这条纠正了旧条目 {row.supersedes}</span>
                            ) : null}
                            {archived ? <span className="mem-badge mem-badge--archived">已归档</span> : null}
                            {row.origin === 'model_inference' ? (
                              <span className="mem-badge mem-badge--warn">推测，不是事实</span>
                            ) : null}
                          </div>

                          <details className="mem-item__detail">
                            <summary>详情（列表数据，无单条详情接口）</summary>
                            <dl className="mem-detail">
                              <div>
                                <dt>memory_id</dt>
                                <dd>{row.memory_id}</dd>
                              </div>
                              <div>
                                <dt>kind</dt>
                                <dd>{row.kind}</dd>
                              </div>
                              <div>
                                <dt>可见范围</dt>
                                <dd>{visibilityLabel(row.visibility)}（{row.visibility}）</dd>
                              </div>
                              <div>
                                <dt>有效期至</dt>
                                <dd>{row.valid_until ? formatAbsoluteTime(row.valid_until) : '未设置'}</dd>
                              </div>
                              <div>
                                <dt>归档时间</dt>
                                <dd>{row.archived_at ? formatAbsoluteTime(row.archived_at) : '未归档'}</dd>
                              </div>
                              <div>
                                <dt>撤回理由</dt>
                                <dd>{row.retract_reason ?? '—'}</dd>
                              </div>
                              <div>
                                <dt>来源事件</dt>
                                <dd>{row.source_event_ids.length > 0 ? row.source_event_ids.join('、') : '无'}</dd>
                              </div>
                            </dl>
                          </details>

                          {isAction && action ? (
                            <div
                              className="mem-confirm mem-confirm--item"
                              role="group"
                              aria-label={action.mode === 'retract' ? '撤回这条记忆' : '纠正这条记忆'}
                            >
                              <p className="mem-confirm__title">
                                <AlertIcon size={13} />
                                {action.mode === 'retract' ? '撤回这条记忆' : '纠正这条记忆'}
                              </p>
                              {action.mode === 'retract' ? (
                                <>
                                  <p className="mem-confirm__body">
                                    撤回后它不再参与召回，也会从默认列表消失；数据不会删除，
                                    勾选「显示已撤回」仍能看到这条墓碑。
                                  </p>
                                  <label className="mem-field mem-field--wide">
                                    <span>撤回理由（必填，1..500 字，会记进墓碑）</span>
                                    <textarea
                                      value={reason}
                                      rows={2}
                                      maxLength={500}
                                      placeholder="例如：这条说法已经过时"
                                      onChange={(event) => setReason(event.target.value)}
                                    />
                                  </label>
                                </>
                              ) : (
                                <>
                                  <p className="mem-confirm__body">
                                    旧条目会被标成「已被纠正」并退出召回；新条目继承同样的类型、
                                    可见范围与来源事件。两条都会留在库里。
                                  </p>
                                  <label className="mem-field mem-field--wide">
                                    <span>新的说法（必填，1..4000 字）</span>
                                    <textarea
                                      value={newText}
                                      rows={3}
                                      maxLength={4000}
                                      onChange={(event) => setNewText(event.target.value)}
                                    />
                                  </label>
                                  <label className="mem-field mem-field--wide">
                                    <span>纠正理由（可选，默认「用户纠正」）</span>
                                    <input
                                      type="text"
                                      value={reason}
                                      maxLength={500}
                                      placeholder="用户纠正"
                                      onChange={(event) => setReason(event.target.value)}
                                    />
                                  </label>
                                </>
                              )}

                              <InlineError error={actionError} resource="list" />

                              <div className="mem-confirm__actions">
                                <button
                                  type="button"
                                  className="button button--small mem-action mem-action--danger"
                                  disabled={!canConfirm}
                                  onClick={() => void submitAction()}
                                >
                                  {pending
                                    ? '提交中…'
                                    : action.mode === 'retract'
                                      ? '确认撤回'
                                      : '确认纠正'}
                                </button>
                                <button type="button" className="button button--small mem-action" onClick={closeAction}>
                                  取消
                                </button>
                              </div>
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </>
      ) : null}
    </section>
  );
}

/* ==================================================================== 危险区 */

export interface MemoryDangerZoneProps {
  state: ForgetState;
  dispatch: (action: ForgetAction) => void;
  handlers: MemoryHandlers;
}

export function MemoryDangerZone({
  state,
  dispatch,
  handlers,
}: MemoryDangerZoneProps): React.ReactElement {
  const run = async () => {
    if (!canSubmitForget(state)) return;
    dispatch({ type: 'submit' });
    try {
      const count = await handlers.forget();
      dispatch({ type: 'succeeded', count });
    } catch (cause) {
      dispatch({ type: 'failed', error: explainError(cause) });
    }
  };

  return (
    <section className="mem-danger" aria-labelledby="mem-danger-title" data-testid="mem-danger">
      <h2 id="mem-danger-title" className="mem-danger__title">
        <AlertIcon size={14} /> 危险操作
      </h2>

      {state.step === 'idle' ? (
        <>
          <p className="mem-danger__body">
            「遗忘全部」会清空当前账号<strong>可见</strong>的记忆：所有记忆行（情节 / 断言 /
            程序三类 kind 都算）标记为已撤回、情节片段归档、核心记忆停用、{AWARENESS_TERM}失效。
            它不会删除数据库里的行（软清除，保留证据链）。
          </p>
          <p className="mem-danger__irreversible">
            ⚠ 无法撤销：接口没有恢复端点，页面也没有撤销入口。清掉之后只能在墓碑里看到
            「已撤回」，无法让它们重新参与召回。
          </p>
          <button
            type="button"
            className="button mem-action mem-danger__start"
            onClick={() => dispatch({ type: 'begin' })}
          >
            遗忘全部记忆…
          </button>
        </>
      ) : null}

      {state.step === 'confirm' ? (
        <div className="mem-danger__confirm" role="group" aria-labelledby="mem-danger-confirm-title">
          <h3 id="mem-danger-confirm-title" className="mem-confirm__title">
            二次确认：遗忘全部记忆
          </h3>
          <ul className="mem-danger__list">
            <li>记忆行（情节 / 断言 / 程序三类都有，不按 kind 过滤）→ 标记已撤回，理由记为「用户请求清除」</li>
            <li>情节片段（另一张表 MemoryEpisode）→ 归档，退出检索</li>
            <li>核心记忆 → 停用（不再每轮注入）</li>
            <li>{AWARENESS_TERM} → 失效</li>
          </ul>
          <p className="mem-danger__irreversible">
            ⚠ 该操作不可撤销。请先确认这些记忆确实不再需要。
          </p>
          <label className="mem-check mem-check--strong">
            <input
              type="checkbox"
              checked={state.ack}
              onChange={(event) => dispatch({ type: 'ack', value: event.target.checked })}
            />
            <span>我明白这会清空当前账号可见的记忆，且无法撤销</span>
          </label>

          <InlineError error={state.error} resource="list" />

          <div className="mem-confirm__actions">
            <button
              type="button"
              className="button mem-action mem-action--danger mem-danger__confirm-button"
              disabled={!canSubmitForget(state)}
              onClick={() => void run()}
            >
              {state.pending ? '正在遗忘…' : '确认遗忘全部'}
            </button>
            <button
              type="button"
              className="button mem-action"
              onClick={() => dispatch({ type: 'cancel' })}
              disabled={state.pending}
            >
              取消
            </button>
          </div>
        </div>
      ) : null}

      {state.step === 'done' ? (
        <div className="mem-danger__result" role="status" data-testid="mem-forget-result">
          <p className="mem-danger__count">
            已遗忘：服务端返回影响行数 <strong data-testid="mem-forget-count">{state.count ?? 0}</strong>
          </p>
          <p className="mem-danger__body">
            口径：服务端返回的是四类受影响行数之和——记忆行（情节 / 断言 / 程序三类 kind）+
            情节片段（MemoryEpisode）+ 核心记忆 + {AWARENESS_TERM}。四类都只做软清除（撤回 / 归档 /
            停用 / 失效），不物理删除。
          </p>
          <p className="mem-danger__irreversible">
            ⚠ 无法撤销（没有恢复接口）。默认列表与核心记忆现在应为空；
            墓碑可以用上面的「显示已撤回 / 显示已归档」查看。
          </p>
          <button type="button" className="button mem-action" onClick={() => dispatch({ type: 'reset' })}>
            知道了
          </button>
        </div>
      ) : null}
    </section>
  );
}

/* ==================================================================== 内容区（可测） */

export interface MemoryContentBodyProps {
  core: SectionState<CoreMemoryListResponse>;
  list: SectionState<MemoryListResponse>;
  include: MemoryInclude | null;
  flags: TombstoneFlags;
  onFlagsChange: (flags: TombstoneFlags) => void;
  limit: number;
  onLimitChange: (limit: number) => void;
  filters: MemoryFilters;
  onFiltersChange: (filters: MemoryFilters) => void;
  forget: ForgetState;
  onForgetAction: (action: ForgetAction) => void;
  handlers: MemoryHandlers;
  notice: Notice | null;
  onDismissNotice: () => void;
  onReloadAll: () => void;
}

/**
 * 记忆内容区（纯展示）。容器负责取数与回填，这里只决定「每种状态长什么样」。
 *
 * 单独抽出来是为了能直接测「列表成功、核心记忆失败」这种部分失败：
 * 整页不许被一个错误盖住。
 */
export function MemoryContentBody({
  core,
  list,
  include,
  flags,
  onFlagsChange,
  limit,
  onLimitChange,
  filters,
  onFiltersChange,
  forget,
  onForgetAction,
  handlers,
  notice,
  onDismissNotice,
  onReloadAll,
}: MemoryContentBodyProps): React.ReactElement {
  // 页脚只说当下成立的话：403 这类不可重试分段没有重试按钮（见 memoryFooterNote）。
  const footNote = memoryFooterNote(core, list);

  return (
    <div className="mem-content">
      {notice ? (
        <div className={`banner ${notice.tone === 'ok' ? 'banner--ok' : 'banner--warn'}`} role="status">
          <span>{notice.text}</span>
          <button type="button" className="ghost-button" onClick={onDismissNotice}>
            关闭
          </button>
        </div>
      ) : null}

      <CoreMemorySection state={core} handlers={handlers} onReload={handlers.reloadCore} />

      <MemoryListSection
        state={list}
        include={include}
        flags={flags}
        onFlagsChange={onFlagsChange}
        limit={limit}
        onLimitChange={onLimitChange}
        filters={filters}
        onFiltersChange={onFiltersChange}
        handlers={handlers}
        onReload={handlers.reloadList}
      />

      <MemoryDangerZone state={forget} dispatch={onForgetAction} handlers={handlers} />

      <p className="mem-foot" data-testid="mem-foot" data-foot-mode={footNote.mode}>
        {footNote.text}
        <button type="button" className="ghost-button" onClick={onReloadAll}>
          <RefreshIcon /> 重新读取全部
        </button>
      </p>
    </div>
  );
}

/* ==================================================================== 内容区容器 */

export function MemoryContent(): React.ReactElement {
  const [core, setCore] = useState<SectionState<CoreMemoryListResponse>>(loadingSection<CoreMemoryListResponse>());
  const [list, setList] = useState<SectionState<MemoryListResponse>>(loadingSection<MemoryListResponse>());
  const [flags, setFlags] = useState<TombstoneFlags>({ retracted: false, archived: false });
  const [limit, setLimit] = useState<number>(50);
  const [filters, setFilters] = useState<MemoryFilters>(DEFAULT_FILTERS);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [forget, dispatchForget] = useReducer(forgetReducer, INITIAL_FORGET);

  const include = includeMode(flags);

  const loadCore = useCallback(async () => {
    setCore((current) => (current.data ? current : loadingSection<CoreMemoryListResponse>()));
    try {
      const data = await api.listCoreMemory();
      setCore({ status: 'ok', data, error: null });
    } catch (cause) {
      setCore({ status: 'error', data: null, error: explainError(cause) });
    }
  }, []);

  const loadList = useCallback(async () => {
    setList((current) => (current.data ? current : loadingSection<MemoryListResponse>()));
    try {
      const data = await api.listMemories({ limit, include });
      setList({ status: 'ok', data, error: null });
    } catch (cause) {
      setList({ status: 'error', data: null, error: explainError(cause) });
    }
  }, [limit, include]);

  useEffect(() => {
    void loadCore();
  }, [loadCore]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const handlers: MemoryHandlers = useMemo(
    () => ({
      async retract(memoryId, reason) {
        await api.retractMemory(memoryId, reason);
        setNotice({
          tone: 'ok',
          text: '已撤回。它不再参与召回，也已从默认列表消失；勾选「显示已撤回」仍可查看墓碑。',
        });
        await loadList();
      },
      async supersede(memoryId, text, reason) {
        await api.supersedeMemory(memoryId, text, reason);
        setNotice({
          tone: 'ok',
          text: '已纠正。旧条目被标成「已被纠正」并退出召回，新条目已写入；两条都保留在库里。',
        });
        await loadList();
      },
      async saveCore(input, meta) {
        await api.upsertCoreMemory(input);
        setNotice({
          tone: 'ok',
          text: meta.overwrote
            ? `已覆盖核心记忆「${input.label}」（同 label 不会新增第二条）。`
            : `已保存核心记忆「${input.label}」。`,
        });
        await loadCore();
      },
      async deleteCore(item) {
        await api.deleteCoreMemory(item.core_id);
        setNotice({
          tone: 'ok',
          text: `已停用核心记忆「${item.label}」（软删除：不再注入每一轮，数据仍在库里）。`,
        });
        await loadCore();
      },
      async forget() {
        const result = await api.forgetAllMemories();
        setNotice({
          tone: 'warn',
          text: `已遗忘全部记忆：服务端返回影响 ${result.retracted} 行（记忆行 + 情节片段 + 核心记忆 + ${AWARENESS_TERM}四类合计），且无法撤销。`,
        });
        await Promise.all([loadCore(), loadList()]);
        return result.retracted;
      },
      reloadCore() {
        void loadCore();
      },
      reloadList() {
        void loadList();
      },
    }),
    [loadCore, loadList],
  );

  return (
    <MemoryContentBody
      core={core}
      list={list}
      include={include}
      flags={flags}
      onFlagsChange={setFlags}
      limit={limit}
      onLimitChange={setLimit}
      filters={filters}
      onFiltersChange={setFilters}
      forget={forget}
      onForgetAction={dispatchForget}
      handlers={handlers}
      notice={notice}
      onDismissNotice={() => setNotice(null)}
      onReloadAll={() => {
        void loadCore();
        void loadList();
      }}
    />
  );
}

/* ==================================================================== 调参 tab */

const POLICY_FIELDS: PolicyField[] = [
  {
    key: 'relative_score_floor',
    label: '召回相关性下限',
    hint: '只保留与最相关那条同量级的结果。调高 = 只想起最相关的，调低 = 更容易联想。',
    step: 'any',
    min: 0,
    max: 1,
  },
  {
    key: 'rerank_top_n',
    label: '精排头部条数',
    hint:
      '重排只看前 N 条。调高 = 更准但更慢更贵。' +
      '仅在服务端配置了重排模型（环境变量 LING_RERANK_*）时生效；没配时这一项不生效（没有重排服务可调用）。',
    step: 1,
    min: 1,
    max: 100,
  },
  {
    key: 'recency_half_life_days',
    label: '时间衰减半衰期（天）',
    hint: '越小越偏向近期的事。调大 = 更愿意提旧事。参与召回排序，不改变"能不能被想起"。',
    step: 'any',
    min: 1,
    max: 3650,
  },
  {
    key: 'max_retention_days',
    label: '最长保留天数',
    hint:
      '保留期的硬上限：超过这个天数的记忆行不再被召回/会被归档。' +
      '调小立刻对已有记忆生效；调大只影响之后写入的记忆（已写入行的保留期是写入时的快照）。' +
      '情节（Episode）的归档期由后台任务单独控制，不受这一项影响。',
    step: 1,
    min: 1,
    max: 3650,
  },
];

/**
 * 调参文案的事实依据（读 `service.py` / `kernel/context.py` / `kernel/turn.py` /
 * `worker.py` / `settings/service.py` 后确认，2026-11 接线后）：
 *
 * - 发布后的 memory 段由生产代码真的消费：
 *   `kernel/turn.py` 把本轮 `EffectiveConfig.memory` 交给 `ContextCompiler`，
 *   后者构造 `MemoryService(policy=...)`；worker 的 `memory.extract` /
 *   `memory.archive` 在处理每个事件时现读已发布修订。
 * - 轮次的配置快照在**轮次开始时**取一次 → 发布只对**下一轮**生效，
 *   进行中的轮次不会被半路换策略。worker 在**下一个事件**生效。
 * - `max_retention_days` 是硬上限（`min(行内 retention_days, 上限)`）：
 *   调小立刻对存量行生效，调大只对之后写入的行生效。
 * - `rerank_top_n` 只在配置了重排模型（`LING_RERANK_*`）时有真实效果；
 *   没配时**不生效**，必须明说——否则又是一句"存下来就等于生效"的假话。
 *
 * 所以这里可以（也应该）写「下一轮对话起生效」，但必须同时写清两点例外。
 */
export const MEMORY_POLICY_STATUS =
  '生效状态：已接线。这 4 个参数会存成草稿并发布（回滚在设置页的版本列表里），发布后由运行时读取：' +
  '对话侧在下一轮开始编译上下文时生效（进行中的轮次继续用旧策略）；' +
  '记忆提炼与归档在下一个后台事件里生效（worker 每次处理都会现读已发布修订，无需重启）。' +
  '两点例外必须知道：' +
  '① 精排头部条数（rerank_top_n）只在服务端配置了重排模型（环境变量 LING_RERANK_*）时生效，没配时这一项不生效——没有重排服务可调用，检索只用字面+向量两路融合排序；' +
  '② 最长保留天数（max_retention_days）是硬上限：调小立刻对已有记忆生效，调大只影响之后写入的记忆（已写入行的保留期是写入时的快照，不会被改写），情节（Episode）的归档期由后台任务单独控制。';

export const MEMORY_POLICY_PUBLISHED_MESSAGE =
  '草稿已发布。新策略会在下一轮对话起生效（进行中的轮次用旧策略；记忆提炼/归档在下一个后台事件生效）。' +
  '注意：精排头部条数需要服务端配置了重排模型（LING_RERANK_*）才会生效。';

export interface MemorySettingsTabProps {
  onBack: () => void;
}

export function MemorySettingsTab({ onBack }: MemorySettingsTabProps): React.ReactElement {
  return (
    <div className="mem-settings" data-testid="mem-settings">
      <div className="mem-policy-status" role="status" data-testid="mem-policy-status">
        <p className="mem-policy-status__title">调参前请先读这段</p>
        <p>{MEMORY_POLICY_STATUS}</p>
        <ul className="mem-policy-status__list">
          <li>
            <code>relative_score_floor</code> 召回相关性下限
          </li>
          <li>
            <code>rerank_top_n</code> 精排头部条数
          </li>
          <li>
            <code>recency_half_life_days</code> 时间衰减半衰期
          </li>
          <li>
            <code>max_retention_days</code> 最长保留天数
          </li>
        </ul>
        <p className="mem-policy-status__foot">
          这四个值的来源是 <code>GET /v1/settings/config</code> 的 <code>memory</code> 段，
          保存走草稿 + 发布（与设置页的模型配置同一套修订机制）。
        </p>
      </div>

      <PolicyForm
        title="记忆与学习"
        hint="改的是「她记住/学会什么」。参数会存成草稿再发布，改错了可以回滚；发布后下一轮对话起生效，生效时机与上面那两点例外仍然成立。"
        fields={POLICY_FIELDS}
        section="memory"
        onBack={onBack}
        backLabel="返回记忆内容"
        publishedMessage={MEMORY_POLICY_PUBLISHED_MESSAGE}
      />
    </div>
  );
}

/* ==================================================================== 页面 */

export function MemoryPage(): React.ReactElement {
  const href = useLocationHref();
  const tab = memoryTabFromHref(href);

  const openTab = (next: MemoryTab) => {
    if (next === tab) return;
    navigate(memoryTabHref(next));
  };

  return (
    <section className="page mem">
      <header className="mem__head">
        <div>
          <h1 className="page__title">记忆</h1>
          <p className="page__subtitle">
            这里能看、能改她记住什么：核心记忆每轮注入（群聊只注入群聊/全局条目、超出 800
            字符预算会截断、完全隔离模式不注入），长期记忆按类型分段。
            时间一律显示绝对时间（括号里是相对时间），按本机时区。
          </p>
          <p className="mem__boundary">
            诚实边界：接口没有单条详情端点（详情全部来自列表数据），也没有分页——单次最多取 200 条。
          </p>
        </div>
      </header>

      <div className="mem-tabs" role="tablist" aria-label="记忆页分区">
        <button
          type="button"
          role="tab"
          id="mem-tab-content"
          aria-selected={tab === 'content'}
          aria-controls="mem-panel-content"
          className={`mem-tab${tab === 'content' ? ' mem-tab--active' : ''}`}
          data-testid="mem-tab-content"
          onClick={() => openTab('content')}
        >
          记忆内容
        </button>
        <button
          type="button"
          role="tab"
          id="mem-tab-settings"
          aria-selected={tab === 'settings'}
          aria-controls="mem-panel-settings"
          className={`mem-tab${tab === 'settings' ? ' mem-tab--active' : ''}`}
          data-testid="mem-tab-settings"
          onClick={() => openTab('settings')}
        >
          记忆与学习（调参）
        </button>
      </div>

      <div
        id="mem-panel-content"
        role="tabpanel"
        aria-labelledby="mem-tab-content"
        hidden={tab !== 'content'}
      >
        {tab === 'content' ? <MemoryContent /> : null}
      </div>

      <div
        id="mem-panel-settings"
        role="tabpanel"
        aria-labelledby="mem-tab-settings"
        hidden={tab !== 'settings'}
      >
        {tab === 'settings' ? <MemorySettingsTab onBack={() => openTab('content')} /> : null}
      </div>
    </section>
  );
}
