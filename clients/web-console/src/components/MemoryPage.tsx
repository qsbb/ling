/**
 * 记忆页：先找记录，再查看来源、可见范围与纠正历史。
 * 浏览器只搜索已经加载的一批（最多 200 条），不调用对话召回，也无单条详情 API。
 * 核心记忆、学习设置和永久清理互相独立，后台清理不随页内切换而取消。
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import {
  api,
  type ForgetAcceptedResponse,
  type ForgetResponse,
  type PurgeStatusItem,
  type PurgeStatusResponse,
} from '../api';
import { explainError, type ExplainedError } from '../errors';
import { navigate, useLocationHref } from '../route';
/* eslint-disable @typescript-eslint/no-unused-vars -- re-export 的类型给测试与调用方用 */
export type { ForgetAcceptedResponse, ForgetResponse, PurgeStatusItem, PurgeStatusResponse };
/* eslint-enable @typescript-eslint/no-unused-vars */

import type {
  CoreMemoryInput,
  CoreMemoryItem,
  CoreMemoryListResponse,
  MemoryImportSourceFilter,
  MemoryInclude,
  MemoryListResponse,
  MemoryRecord,
} from '../types';
import { AlertIcon, ArrowLeftIcon, PencilIcon, RefreshIcon } from './Icons';
import { MemoryImportPanel } from './MemoryImport';
import { PolicyForm, type PolicyField } from './PolicyForm';
import '../memory.css';

/* ==================================================================== 文案表 */

const BROWSE_KINDS = [
  { value: 'all', label: '全部' },
  { value: 'assertion', label: '事实与线索' },
  { value: 'episode', label: '经历' },
  { value: 'procedure', label: '做法' },
] as const;

export function kindLabel(kind: string): string {
  return BROWSE_KINDS.find((item) => item.value === kind)?.label ?? (kind || '未分类');
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
 *   focus / visibilitychange 重新取数。改完密码后重新进入本页。
 * - 真首次登录时 `App.tsx`（`must_change_password` 分支）会整页接管成改密页，
 *   `/memory` 根本挂载不了。这段 403 文案的真实受众只有「登录后会话中途被外部
 *   置位」这种边缘情况，它不是首次登录的常见路径。
 */
export const ACCOUNT_GATE_COPY = {
  prefix: '这条 403 来自账号闸：首次登录还没改初始口令，到 ',
  link: '设置 → 账号',
  suffix: ' 改完密码后，重新进入本页。',
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

/* ==================================================================== 删除进度 */

/**
 * 轮询上限。**必须有限**：清理卡住时无限轮询既不会让删除变快，又会一直打着后端、
 * 让用户以为"还在转"就等于"还在干活"。2 秒 × 20 次 = 40 秒，超时后如实说
 * 「仍在进行，稍后刷新查看」——清理本身由后端扫描任务兜底，不依赖这个页面开着。
 */
export const PURGE_POLL_INTERVAL_MS = 2_000;
export const PURGE_POLL_MAX_ATTEMPTS = 20;

/** 一次轮询的判定结果。`continue` 是**动作**不是字面量，避免和状态名混。 */
export type PurgePollDecision = 'continue' | 'done' | 'failed' | 'gone';

/**
 * 按墓碑查清那条记录该落到哪个状态。
 *
 * - 找不到这条墓碑（`gone`）：不说"已完成"——拿不到计数就不许编计数，
 *   也不许无限重试；
 * - `failed` 优先于其它：墓碑一旦失败就是失败，不能被后面别的记录盖过去；
 * - `state` 认不出来（后端加了新状态）：按"还在进行"处理，不谎称完成。
 */
export function decidePurgePoll(
  response: PurgeStatusResponse,
  tombstoneId: string,
): { decision: PurgePollDecision; item: PurgeStatusItem | null } {
  const items = Array.isArray(response?.items) ? response.items : [];
  const item = items.find((row) => row.tombstone_id === tombstoneId) ?? null;
  if (!item) return { decision: 'gone', item: null };
  if (item.state === 'done') return { decision: 'done', item };
  if (item.state === 'failed') return { decision: 'failed', item };
  return { decision: 'continue', item };
}

/** 一条墓碑的计数（后端字段直接映射；缺字段按 0，不猜）。 */
export interface PurgeCounts {
  memories: number;
  episodes: number;
  core: number;
  awareness: number;
  embeddings: number;
  entities: number;
  retrievalLogs: number;
  eventsScrubbed: number;
}

export function purgeCountsOf(item: PurgeStatusItem | null): PurgeCounts {
  return {
    memories: item?.memories_deleted ?? 0,
    episodes: item?.episodes_deleted ?? 0,
    core: item?.core_deleted ?? 0,
    awareness: item?.awareness_deleted ?? 0,
    embeddings: item?.embeddings_deleted ?? 0,
    entities: item?.entities_deleted ?? 0,
    retrievalLogs: item?.retrieval_logs_deleted ?? 0,
    eventsScrubbed: item?.events_scrubbed ?? 0,
  };
}

/**
 * 完成后的计数文案。
 *
 * 只说**真实删掉的条数**；计数为 0 的项不列（列一排 `0` 会把真数字淹掉）。
 * 但**合计为 0** 时必须说清楚：不能让人把"删了 0 条"读成"删除失败"，也不能
 * 把它读成"删成功了"——只能说这次没有行被删。
 */
export function describePurgeCounts(counts: PurgeCounts): string {
  const parts: string[] = [];
  if (counts.memories > 0) parts.push(`${counts.memories} 条记忆`);
  if (counts.episodes > 0) parts.push(`${counts.episodes} 条情节`);
  if (counts.core > 0) parts.push(`${counts.core} 条核心记忆`);
  if (counts.awareness > 0) parts.push(`${counts.awareness} 条${AWARENESS_TERM}`);
  if (counts.embeddings > 0) parts.push(`${counts.embeddings} 条向量`);
  if (counts.entities > 0) parts.push(`${counts.entities} 条实体关联`);
  if (counts.retrievalLogs > 0) parts.push(`${counts.retrievalLogs} 条检索记录`);
  if (counts.eventsScrubbed > 0) parts.push(`${counts.eventsScrubbed} 条待处理事件`);
  if (parts.length === 0) {
    return '清理已完成：这次没有删到任何行（库里本来就没有可删的，或者这条记录不带计数）。';
  }
  return `已删除 ${parts.join('、')}。`;
}

/** 这个返回是两段式的受理结果，还是旧版软清除的计数？ */
export function isForgetAccepted(response: ForgetResponse): response is ForgetAcceptedResponse {
  return typeof (response as ForgetAcceptedResponse)?.tombstone_id === 'string';
}

/* ==================================================================== 遗忘状态机 */

/**
 * - `idle`：首屏；
 * - `confirm`：二次确认，勾选后才能提交；
 * - `purging`：请求已受理，**正在后台清理**（轮询中，还没有真实计数）；
 * - `failed`：清理失败（只能来自 purge 墓碑的 `failed`）；
 * - `done`：清理完成且有计数；
 * - `timeout`：轮询到上限还没结束——不等了，但也不谎称完成；
 * - `unsupported`：后端还是旧版（只软清除，没有墓碑可查）。
 */
export type ForgetStep =
  | 'idle'
  | 'confirm'
  | 'purging'
  | 'failed'
  | 'done'
  | 'timeout'
  | 'unsupported';

export interface ForgetState {
  step: ForgetStep;
  /** 二次确认必须显式勾选，按钮才可用——不是点一下就执行。 */
  ack: boolean;
  /** 请求阶段进行中（POST 还没返回），或用户在失败页点了「重试删除」。 */
  pending: boolean;
  /** 已受理的墓碑 id；轮询只认这一条。 */
  tombstoneId: string | null;
  /** 请求阶段被取消并脱敏的在途事件数（**不是**删除条数）。 */
  eventsCancelled: number | null;
  /** 后台清理的计数；只有 done 时才有值。 */
  counts: PurgeCounts | null;
  /** 失败原因码（后端墓碑里的 `error_code`，不含正文）。 */
  errorCode: string | null;
  /** 旧后端软清除返回的影响行数：只用于如实说明"这个是软清除"，不当成删除计数。 */
  legacyRetracted: number | null;
  /** 请求阶段（POST 本身）失败：留在二次确认里就地显示。 */
  error: ExplainedError | null;
  /** 已发起的轮询次数，超时判定用。 */
  polls: number;
}

export type ForgetAction =
  | { type: 'begin' }
  | { type: 'cancel' }
  | { type: 'ack'; value: boolean }
  | { type: 'submit' }
  /** POST 受理成功：进入后台清理（**不是**完成）。 */
  | { type: 'accepted'; accepted: ForgetAcceptedResponse }
  /** 旧后端：只做了软清除。 */
  | { type: 'legacy'; retracted: number }
  /** 一次轮询的结果。 */
  | { type: 'poll'; decision: PurgePollDecision; item: PurgeStatusItem | null }
  /** 轮询到达上限：停止，但不是完成。 */
  | { type: 'timeout' }
  /** 请求阶段失败：回到二次确认，就地显示原因。 */
  | { type: 'failed'; error: ExplainedError }
  /** 清理失败后用户再点一次（会重新提交删除请求，拿到新墓碑）。 */
  | { type: 'retry' }
  | { type: 'reset' };

export const INITIAL_FORGET: ForgetState = {
  step: 'idle',
  ack: false,
  pending: false,
  tombstoneId: null,
  eventsCancelled: null,
  counts: null,
  errorCode: null,
  legacyRetracted: null,
  error: null,
  polls: 0,
};

const CONFIRMED = { ...INITIAL_FORGET, step: 'confirm' as const, ack: true, pending: true };

function withPollResult(state: ForgetState, action: Extract<ForgetAction, { type: 'poll' }>): ForgetState {
  if (state.step !== 'purging') return state;
  const polls = state.polls + 1;
  switch (action.decision) {
    case 'done':
      return {
        ...state,
        step: 'done',
        pending: false,
        polls,
        counts: purgeCountsOf(action.item),
        errorCode: null,
        error: null,
      };
    case 'failed':
      return {
        ...state,
        step: 'failed',
        pending: false,
        polls,
        counts: null,
        errorCode: action.item?.error_code ?? 'purge_failed',
        error: null,
      };
    case 'gone':
      // 查不到这条墓碑：不说"完成"，也不再继续问（再问也不会出现）。
      // 后端有扫描任务兜底，这里只能说"没查到状态"。
      return { ...state, step: 'timeout', pending: false, polls };
    case 'continue':
    default:
      return { ...state, polls };
  }
}

/**
 * 二次确认 + 后台清理的状态机（纯函数，便于单测）。
 *
 * 两条硬规则：
 * 1. **请求受理 ≠ 删除完成**。`accepted` 只能进 `purging`，绝不能直接进 `done`；
 *    `done` 只由 `poll(decision: 'done')` 产生，计数只从 status 里取。
 * 2. 只有 `confirm + ack + !pending` 才允许提交；`purging` 里的重复提交无效。
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
      if (!canSubmitForget(state)) return state;
      return CONFIRMED;
    case 'retry':
      // 只允许从失败态重来一次：重新走两段式（新墓碑、新代次）。
      if (state.step !== 'failed' || state.pending) return state;
      return CONFIRMED;
    case 'accepted':
      if (!state.pending) return state;
      return {
        ...state,
        step: 'purging',
        pending: false,
        ack: true,
        tombstoneId: action.accepted.tombstone_id,
        eventsCancelled: action.accepted.events_cancelled ?? 0,
        counts: null,
        errorCode: null,
        legacyRetracted: null,
        error: null,
        polls: 0,
      };
    case 'legacy':
      if (!state.pending) return state;
      return {
        ...INITIAL_FORGET,
        step: 'unsupported',
        ack: true,
        legacyRetracted: action.retracted,
      };
    case 'poll':
      return withPollResult(state, action);
    case 'timeout':
      return state.step === 'purging' ? { ...state, step: 'timeout', pending: false } : state;
    case 'failed':
      // 轮询请求本身失败 ≠ 删除失败：只说明"这次没问到"，让下一次轮询或超时收场。
      if (state.step === 'purging') return state;
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

/** 还在等后台清理（要起轮询）。 */
export function isForgetPolling(state: ForgetState): boolean {
  return state.step === 'purging';
}

/** 失败提示里能不能给「重试删除」按钮。 */
export function canRetryForget(state: ForgetState): boolean {
  return state.step === 'failed' && !state.pending;
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
  /**
   * 提交删除（两段式的请求阶段）。返回受理结果或旧后端的软清除计数；
   * **不返回**"删掉了多少"——那个数只能从 `purgeStatus()` 里拿。
   */
  forget(): Promise<ForgetResponse>;
  /** 查后台清理进度。`tombstone_id` 由调用方按受理结果匹配。 */
  purgeStatus(signal?: AbortSignal): Promise<PurgeStatusResponse>;
  reloadCore(): void;
  reloadList(): void;
}

export interface Notice {
  tone: 'ok' | 'warn';
  text: string;
}

/* ==================================================================== 页头与 tab */

export type MemoryTab = 'content' | 'core' | 'settings' | 'privacy';

const MEMORY_TABS: Array<{ id: MemoryTab; label: string }> = [
  { id: 'content', label: '记忆库' },
  { id: 'core', label: '核心记忆' },
  { id: 'settings', label: '学习设置' },
  { id: 'privacy', label: '隐私与清理' },
];

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
  const tab = params.get('tab');
  if (tab === 'core' || tab === 'settings' || tab === 'privacy') return tab;
  if (path === '/settings/memory') return 'settings';
  return 'content';
}

export function memoryTabHref(tab: MemoryTab): string {
  return tab === 'content' ? '/memory' : `/memory?tab=${tab}`;
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
  const [editorOpen, setEditorOpen] = useState(false);
  const editorRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editorOpen) editorRef.current?.focus();
  }, [editorOpen, editingId]);

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
    setEditorOpen(false);
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
    setEditorOpen(true);
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
      <header className="mem-core-head">
        <div>
          <h2 id="mem-core-title">核心记忆</h2>
          <p>适合放称呼、约定等希望她经常记得的事。</p>
        </div>
        <div className="mem-core-head__actions">
          {state.status === 'ok' ? (
            <>
              <span className="mem-muted" data-testid="mem-core-count">{items.length} / {limit} 条</span>
              <button type="button" className="ghost-button" onClick={onReload} aria-label="刷新核心记忆"><RefreshIcon size={15} /></button>
              <button type="button" className="button button--primary" aria-expanded={editorOpen && !editingId}
                disabled={atLimit || pending}
                onClick={() => {
                  if (editorOpen && !editingId) { resetForm(); return; }
                  setForm(EMPTY_CORE_FORM);
                  setEditingId(null);
                  setError(null);
                  setEditorOpen(true);
                }}>
                添加条目
              </button>
            </>
          ) : null}
        </div>
      </header>
      <details className="mem-core-rules">
        <summary>什么时候会用到这些条目？</summary>
        <p>核心记忆无需检索，但不保证全部注入。群聊里只注入作用域为「群聊 / 全局」的条目；
          完全隔离模式不注入。单轮 800 字符预算，超出时按优先级截断。</p>
        <p>保存同名标签会覆盖原条目。删除是软删除：停用后原文仍保留，页面没有恢复入口。</p>
      </details>
      {atLimit ? <p className="mem-note">已达 {limit} 条上限，可编辑或停用已有条目。</p> : null}

      {state.status === 'loading' ? <LoadingLines label="正在读取核心记忆…" /> : null}

      {state.status === 'error' ? (
        <SectionError title="核心记忆" resource="core" error={state.error} onRetry={onReload} />
      ) : null}

      {state.status === 'ok' && items.length === 0 ? (
        <p className="mem-empty" data-testid="mem-core-empty">
          还没有核心记忆。可以先写一条「称呼」或「边界」，比如 label 填「称呼」、内容填「叫我小林」。
        </p>
      ) : null}

      {editorOpen && state.status === 'ok' ? (
      <div className="mem-core-form" data-testid="mem-core-form">
        <h3 className="mem-core-form__title">
          {editingId ? '编辑 / 覆盖核心记忆' : '新增核心记忆'}
          {atLimit ? <span className="mem-badge mem-badge--warn">已满 {limit} 条</span> : null}
        </h3>

        {editingId ? (
          <p className="mem-note">
            更换标签会新建另一条记忆，不会重命名原条目。
          </p>
        ) : null}

        <div className="mem-core-form__grid">
          <label className="mem-field">
            <span>标签（同名覆盖）</span>
            <input
              ref={editorRef}
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
          <button type="button" className="button button--small mem-action" onClick={resetForm} disabled={pending}>
            取消
          </button>
        </div>
      </div>
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
                <button type="button" className="button button--small mem-action" disabled={pending} onClick={() => startEdit(item)}>
                  <PencilIcon size={13} /> 编辑
                </button>
                <button
                  type="button"
                  className="button button--small mem-action mem-action--danger"
                  aria-expanded={confirmDeleteId === item.core_id}
                  disabled={pending}
                  onClick={() => {
                    setError(null);
                    setConfirmDeleteId(confirmDeleteId === item.core_id ? null : item.core_id);
                  }}
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
                  <InlineError error={error} resource="core" />
                  <div className="mem-confirm__actions">
                    <button
                      type="button"
                      className="button button--small mem-action mem-action--danger"
                      disabled={pending}
                      onClick={() => void remove(item)}
                    >
                      {pending ? '停用中…' : '确认停用'}
                    </button>
                    <button type="button" className="button button--small" disabled={pending} onClick={() => setConfirmDeleteId(null)}>
                      取消
                    </button>
                  </div>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

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
  /** 来源筛选：null = 全部来源。 */
  source?: MemoryImportSourceFilter | null;
  onSourceChange?: (source: MemoryImportSourceFilter | null) => void;
  /** 已加载的累计条数（按来源翻页时 = offset）；翻页按钮据此取下一页。 */
  loadedCount?: number;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  importPanelOpen?: boolean;
  onToggleImport?: () => void;
  onCloseImport?: () => void;
}

/** 来源筛选的显示文案。`null` 是"不筛"，不是某一类来源。 */
export const SOURCE_OPTIONS: Array<{ value: MemoryImportSourceFilter | null; label: string }> = [
  { value: null, label: '全部来源' },
  { value: 'astrbot_memory_companion', label: 'AstrBot 记忆' },
];

export function sourceLabel(source: string | null | undefined): string {
  if (!source) return '';
  return SOURCE_OPTIONS.find((option) => option.value === source)?.label ?? source;
}

function historyStatus(row: MemoryRecord): string | null {
  if (row.superseded_by) return '已被纠正';
  if (row.retracted) return '已撤回';
  if (row.archived_at) return '已归档';
  return null;
}

function libraryEmptyNote(data: MemoryListResponse): string {
  switch (data.extraction?.state) {
    case 'purging': return '记忆正在清理。';
    case 'pending': return '对话已排队，等待提炼。';
    case 'in_flight': return '后台正在提炼，稍后可以刷新查看。';
    case 'retrying': return '最近一次提炼未完成，后台会重试。';
    case 'dead': return '最近一次提炼失败，可在日志查看原因。';
    case 'done': return '最近的提炼任务已结束，当前没有现行条目。';
    default: return data.extraction === null
      ? '当前身份还没有提炼任务。其他渠道的记录需要绑定到同一自然人。'
      : '默认不含已撤回或已归档的条目。';
  }
}

export function searchLoadedMemories(rows: MemoryRecord[], query: string): MemoryRecord[] {
  const normalize = (value: string) => value.normalize('NFKC').toLowerCase();
  const terms = normalize(query).trim().split(/\s+/u).filter(Boolean);
  return terms.length === 0 ? rows : rows.filter((row) => {
    const text = normalize(row.text);
    return terms.every((term) => text.includes(term));
  });
}

function MemoryInspector({
  row,
  loaded,
  headingRef,
  onBack,
  onSelect,
  onChanged,
  handlers,
}: {
  row: MemoryRecord;
  loaded: MemoryRecord[];
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  onBack: () => void;
  onSelect: (id: string) => void;
  onChanged: () => void;
  handlers: MemoryHandlers;
}): React.ReactElement {
  const [mode, setMode] = useState<'retract' | 'supersede' | null>(null);
  const [reason, setReason] = useState('');
  const [newText, setNewText] = useState(row.text);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const status = historyStatus(row);
  const relative = formatRelativeTime(row.created_at);
  const formRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (mode) formRef.current?.focus();
  }, [mode]);

  const updateMode = (next: typeof mode) => {
    setMode(next);
    setReason('');
    setNewText(row.text);
    setError(null);
  };

  const valid = mode === 'retract' ? Boolean(reason.trim()) : Boolean(newText.trim() && newText.trim() !== row.text.trim());

  const submit = async () => {
    if (!mode || !valid || pending || status) return;
    setPending(true);
    setError(null);
    try {
      if (mode === 'retract') await handlers.retract(row.memory_id, reason.trim());
      else await handlers.supersede(row.memory_id, newText.trim(), reason.trim() || '用户纠正');
      updateMode(null);
      onChanged();
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setPending(false);
    }
  };

  const changeLink = (id: string, label: string) => (
    <li key={id}>
      {loaded.some((item) => item.memory_id === id) ? (
        <button type="button" className="mem-text-button" disabled={pending} onClick={() => onSelect(id)}>{label}</button>
      ) : (
        <>{label} <span className="mem-muted">· 不在本批</span></>
      )}
      <code>{id}</code>
    </li>
  );

  return (
    <article className="mem-inspector" aria-labelledby="mem-detail-title" data-testid="mem-inspector">
      <button type="button" className="ghost-button mem-inspector__back" disabled={pending} onClick={onBack}>
        <ArrowLeftIcon size={15} /> 回到列表
      </button>
      <header className="mem-inspector__head">
        <h3 id="mem-detail-title" ref={headingRef} tabIndex={-1}>记忆详情</h3>
        {status ? <span className="mem-badge mem-badge--tombstone">历史记录 · 只读</span> : null}
      </header>
      <div className="mem-inspector__story">
        <div className="mem-inspector__tags">
          <span className="mem-badge">{kindLabel(row.kind)}</span>
          {row.origin === 'model_inference' ? <span className="mem-badge mem-badge--warn">推测，不是事实</span> : null}
          {status ? <span className="mem-badge mem-badge--tombstone">{status}</span> : null}
        </div>
        <p className="mem-inspector__text">{row.text}</p>
        {!status && row.can_edit !== false ? (
          <div className="mem-inspector__actions">
            <button
              type="button" className="button button--small button--primary"
              aria-expanded={mode === 'supersede'} disabled={pending}
              onClick={() => updateMode(mode === 'supersede' ? null : 'supersede')}
            ><PencilIcon size={14} /> 纠正…</button>
            <button
              type="button" className="button button--small mem-action--danger"
              aria-expanded={mode === 'retract'} disabled={pending}
              onClick={() => updateMode(mode === 'retract' ? null : 'retract')}
            >撤回…</button>
          </div>
        ) : null}
      </div>

      {mode && !status ? (
        <form
          className="mem-confirm mem-confirm--item"
          aria-label={mode === 'retract' ? '撤回这条记忆' : '纠正这条记忆'}
          onSubmit={(event) => { event.preventDefault(); void submit(); }}
        >
          <p className="mem-confirm__title">
            <AlertIcon size={14} /> {mode === 'retract' ? '撤回这条记忆' : '纠正这条记忆'}
          </p>
          {mode === 'retract' ? (
            <>
              <p className="mem-confirm__body">撤回后不再参与召回，但不会删除。可在历史记录中查看。</p>
              <label className="mem-field">
                <span>撤回理由</span>
                <textarea ref={formRef} value={reason} rows={2} maxLength={500} required disabled={pending}
                  onChange={(event) => setReason(event.target.value)} />
              </label>
            </>
          ) : (
            <>
              <p className="mem-confirm__body">用新条目替代旧说法。可见范围和来源保留，旧条目退出召回。</p>
              <label className="mem-field">
                <span>新的说法</span>
                <textarea ref={formRef} value={newText} rows={4} maxLength={4000} required disabled={pending}
                  onChange={(event) => setNewText(event.target.value)} />
              </label>
              <label className="mem-field">
                <span>纠正理由（选填）</span>
                <input value={reason} maxLength={500} placeholder="用户纠正" disabled={pending}
                  onChange={(event) => setReason(event.target.value)} />
              </label>
            </>
          )}
          <InlineError error={error} resource="list" />
          <div className="mem-confirm__actions">
            <button type="submit" className={`button button--small ${mode === 'retract' ? 'mem-action--danger' : 'button--primary'}`} disabled={!valid || pending}>
              {pending ? '提交中…' : mode === 'retract' ? '确认撤回' : '确认纠正'}
            </button>
            <button type="button" className="button button--small" disabled={pending} onClick={() => updateMode(null)}>取消</button>
          </div>
        </form>
      ) : null}

      <section className="mem-inspector__section" aria-label="来源与可见范围">
        <h4>来源与可见范围</h4>
        <dl className="mem-facts">
          <div><dt>来源</dt><dd>{originLabel(row.origin)}</dd></div>
          {row.import_source ? (
            <div className="mem-facts__wide" data-testid="mem-import-source">
              <dt>来自 AstrBot</dt>
              <dd>
                <span>{row.import_source.owner_qq} → {row.import_source.bot_qq}</span>
                <code>{row.import_source.record_id}</code>
              </dd>
            </div>
          ) : null}
          <div><dt>可见范围</dt><dd>{visibilityLabel(row.visibility)}</dd></div>
          <div><dt>记下时间</dt><dd><time dateTime={row.created_at ?? undefined}>{formatAbsoluteTime(row.created_at)}</time>{relative ? <small> {relative}</small> : null}</dd></div>
          <div><dt>有效期至</dt><dd>{row.valid_until ? formatAbsoluteTime(row.valid_until) : '未设置'}</dd></div>
        </dl>
      </section>
      {row.import_source ? (
        <section className="mem-inspector__section" aria-label="在 AstrBot 里的时间" data-testid="mem-import-times">
          <h4>在 AstrBot 里的时间</h4>
          <dl className="mem-facts">
            {row.import_source.occurred_at ? (
              <div>
                <dt>发生时间</dt>
                <dd><time dateTime={row.import_source.occurred_at}>{formatAbsoluteTime(row.import_source.occurred_at)}</time></dd>
              </div>
            ) : null}
            <div>
              <dt>原创建时间</dt>
              <dd><time dateTime={row.import_source.created_at}>{formatAbsoluteTime(row.import_source.created_at)}</time></dd>
            </div>
            <div>
              <dt>原更新时间</dt>
              <dd><time dateTime={row.import_source.updated_at}>{formatAbsoluteTime(row.import_source.updated_at)}</time></dd>
            </div>
            {row.import_source.valid_to ? (
              <div>
                <dt>原有效期至</dt>
                <dd><time dateTime={row.import_source.valid_to}>{formatAbsoluteTime(row.import_source.valid_to)}</time></dd>
              </div>
            ) : null}
            <div className="mem-facts__wide">
              <dt>原会话</dt>
              <dd><code>{row.import_source.session_id}</code> · {row.import_source.memory_type}</dd>
            </div>
          </dl>
        </section>
      ) : null}
      {row.retract_reason || row.archived_at || row.supersedes || row.superseded_by ? (
        <section className="mem-inspector__section" aria-label="变更记录">
          <h4>变更记录</h4>
          {row.retract_reason ? <p>撤回理由：{row.retract_reason}</p> : null}
          {row.archived_at ? <p>归档于 {formatAbsoluteTime(row.archived_at)}</p> : null}
          <ul className="mem-change-list">
            {row.superseded_by ? changeLink(row.superseded_by, '纠正后的条目') : null}
            {row.supersedes ? changeLink(row.supersedes, '纠正前的条目') : null}
          </ul>
        </section>
      ) : null}
      <details className="mem-inspector__technical">
        <summary>记录编号与来源事件</summary>
        <dl className="mem-facts">
          <div><dt>记忆编号</dt><dd><code>{row.memory_id}</code></dd></div>
          {row.confidence === null ? null : (
            <div><dt>系统评分</dt><dd>{confidenceLabel(row.confidence)}<small> · 不代表事实已核实</small></dd></div>
          )}
          <div className="mem-facts__wide"><dt>来源事件</dt><dd>{row.source_event_ids.length ? row.source_event_ids.join('、') : '未记录'}</dd></div>
        </dl>
      </details>
    </article>
  );
}

export function MemoryListSection({
  state, include, flags, onFlagsChange, limit, onLimitChange, filters,
  onFiltersChange, handlers, onReload, source, onSourceChange = () => {},
  loadedCount = 0, onLoadMore = () => {}, loadingMore = false, importPanelOpen = false,
  onToggleImport = () => {}, onCloseImport = () => {},
}: MemoryListSectionProps): React.ReactElement {
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const rowElement = useRef<HTMLButtonElement | null>(null);
  const restoreFocus = useRef(false);
  const data = state.status === 'ok' ? state.data : null;
  const fetched = data?.memories ?? [];
  const filtered = sortByCreatedDesc(searchLoadedMemories(
    filterMemories(fetched, filters).filter((row) => !scope || row.visibility === scope),
    query,
  ));
  const selected = selectedId ? filtered.find((row) => row.memory_id === selectedId) : filtered[0];
  const mobileInspectorOpen = Boolean(selectedId && selected);
  const matchingScopeOptions = [...new Set(fetched.map((row) => row.visibility).filter(Boolean))];
  const scopes = Object.keys(VISIBILITY_LABEL).concat(matchingScopeOptions.filter((value) => !(value in VISIBILITY_LABEL)));
  const activeFilterCount = Number(filters.range !== 'all') + Number(Boolean(scope))
    + Number(flags.retracted) + Number(flags.archived) + Number(limit !== LIMIT_OPTIONS[0]);
  const filteredByUser = Boolean(query.trim() || scope || filters.kind !== 'all' || filters.range !== 'all');

  useEffect(() => {
    if (selectedId) headingRef.current?.focus();
    else if (restoreFocus.current) {
      restoreFocus.current = false;
      const element = rowElement.current;
      if (element?.isConnected) element.focus();
      else searchRef.current?.focus();
    }
  }, [selectedId]);

  const goBack = () => {
    restoreFocus.current = true;
    setSelectedId(null);
  };

  const clearDiscovery = () => {
    setQuery('');
    setScope('');
    onFiltersChange(DEFAULT_FILTERS);
    setSelectedId(null);
    searchRef.current?.focus();
  };

  const selectedFromHistory = (id: string) => {
    if (!filtered.some((row) => row.memory_id === id)) {
      setQuery('');
      setScope('');
      onFiltersChange(DEFAULT_FILTERS);
    }
    setSelectedId(id);
  };

  return (
    <section
      /*
       窄屏下详情栏平时不显示；导入面板也挂在这一栏，所以只要面板开着就进
       「阅读态」——否则手机上点得到入口、却看不到面板。
      */
      className={`mem-workspace${mobileInspectorOpen || importPanelOpen ? ' mem-workspace--reading' : ''}`}
      aria-label="长期记忆库"
      data-testid="mem-list"
    >
      <div className="mem-index">
        <header className="mem-index__head">
          <h2>长期记忆</h2>
          <div className="mem-index__head-actions">
            <button
              type="button"
              className="button button--small"
              data-testid="mem-import-entry"
              aria-expanded={importPanelOpen}
              onClick={onToggleImport}
            >
              导入旧记忆
            </button>
            {data ? (
              <button type="button" className="ghost-button mem-index__refresh" onClick={onReload} aria-label="刷新记忆库">
                <RefreshIcon size={15} />
              </button>
            ) : null}
          </div>
        </header>
        <div className="mem-index__tools">
          <label className="mem-search">
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" width="18" height="18">
              <circle cx="10.5" cy="10.5" r="6" /><path d="m15 15 5 5" strokeLinecap="round" />
            </svg>
            <input ref={searchRef} type="search" value={query} onChange={(event) => { setQuery(event.target.value); setSelectedId(null); }}
              aria-label="搜索已加载的记忆" placeholder="搜索已加载的记忆" />
          </label>
          <button
            type="button" className="button button--small mem-index__filter" aria-expanded={showFilters} aria-controls="mem-discovery-filters"
            onClick={() => setShowFilters((value) => !value)}
          >筛选{activeFilterCount ? ` · ${activeFilterCount}` : null}</button>
        </div>
        <div className="mem-kinds" role="group" aria-label="记忆类型">
          {BROWSE_KINDS.map(({ value, label }) => (
            <button key={value} type="button" aria-pressed={filters.kind === value}
              onClick={() => { onFiltersChange({ ...filters, kind: value }); setSelectedId(null); }}>{label}</button>
          ))}
        </div>
        <div className="mem-facet-panel" id="mem-discovery-filters" hidden={!showFilters}>
          <div className="mem-facet-panel__fields">
            <label className="mem-field"><span>记下时间</span>
              <select value={filters.range} onChange={(event) => {
                const value = event.target.value;
                onFiltersChange({ ...filters, range: value === '7d' || value === '30d' ? value : 'all' });
                setSelectedId(null);
              }}>
                <option value="all">全部时间</option><option value="7d">最近 7 天</option><option value="30d">最近 30 天</option>
              </select>
            </label>
            <label className="mem-field"><span>可见范围</span>
              <select value={scope} onChange={(event) => { setScope(event.target.value); setSelectedId(null); }}>
                <option value="">全部范围</option>
                {scopes.map((option) => <option key={option} value={option}>{visibilityLabel(option)}</option>)}
              </select>
            </label>
            <label className="mem-field"><span>加载上限</span>
              <select value={limit} onChange={(event) => { onLimitChange(Number(event.target.value)); setSelectedId(null); }}>
                {LIMIT_OPTIONS.map((option) => <option key={option} value={option}>{option} 条</option>)}
              </select>
            </label>
            <label className="mem-field"><span>来源</span>
              <select
                aria-label="按来源筛选"
                data-testid="mem-source"
                value={source ?? ''}
                onChange={(event) => {
                  const value = event.target.value;
                  onSourceChange(value === 'astrbot_memory_companion' ? 'astrbot_memory_companion' : null);
                  setSelectedId(null);
                }}
              >
                {SOURCE_OPTIONS.map((option) => (
                  <option key={option.label} value={option.value ?? ''}>{option.label}</option>
                ))}
              </select>
            </label>
          </div>
          <fieldset className="mem-history-switches">
            <legend>同时加载历史条目</legend>
            <label className="mem-check"><input type="checkbox" checked={flags.retracted}
              onChange={(event) => { onFlagsChange({ ...flags, retracted: event.target.checked }); setSelectedId(null); }} />显示已撤回</label>
            <label className="mem-check"><input type="checkbox" checked={flags.archived}
              onChange={(event) => { onFlagsChange({ ...flags, archived: event.target.checked }); setSelectedId(null); }} />显示已归档</label>
          </fieldset>
        </div>

        {state.status === 'loading' ? <LoadingLines label="正在读取记忆列表…" /> : null}
        {state.status === 'error' ? (
          <SectionError title="记忆列表" resource="list" error={state.error} onRetry={onReload} />
        ) : null}
        {data ? (
          <>
            <div className="mem-index__count" role="status" data-testid="mem-list-count">
              <span>已加载 {fetched.length} 条{filteredByUser ? ` · 匹配 ${filtered.length} 条` : ''}</span>
              <span>{source ? sourceLabel(source) : include ? '包含历史' : '仅现行条目'} · 按记下时间</span>
            </div>
            {data.has_more ? (
              <p className="mem-index__boundary" data-testid="mem-limit-notice">
                还有 {Math.max(0, data.total - (source ? loadedCount : fetched.length))} 条未加载。搜索仅限本批。
                {source ? (
                  <> <button type="button" className="mem-text-button" data-testid="mem-load-more"
                    disabled={loadingMore} onClick={onLoadMore}>
                    {loadingMore ? '正在加载…' : '加载更早的记忆'}
                  </button></>
                ) : limit < 200 ? (
                  <> <button type="button" className="mem-text-button" onClick={() => { onLimitChange(200); setSelectedId(null); }}>加载最近 200 条</button></>
                ) : ' 接口最多返回 200 条，无法继续加载。'}
              </p>
            ) : null}
            {fetched.length === 0 ? (
              <div className="mem-index__empty" data-testid="mem-list-empty">
                <strong>{include ? '当前范围内没有条目' : '没有现行记忆'}</strong>
                <p>{include ? '可以调整历史记录和加载范围。' : libraryEmptyNote(data)}</p>
                <button type="button" className="mem-text-button" onClick={() => setShowFilters(true)}>查看筛选选项</button>
              </div>
            ) : filtered.length === 0 ? (
              <div className="mem-index__empty" data-testid="mem-filter-empty">
                <strong>这批记忆里没有匹配项</strong>
                <p>试试短一点的关键词，或放宽筛选。</p>
                <button type="button" className="mem-text-button" onClick={clearDiscovery}>清除搜索和筛选</button>
              </div>
            ) : (
              <ul className="mem-index__rows" aria-label="记忆列表" data-testid="mem-results">
                {filtered.map((row) => {
                  const status = historyStatus(row);
                  return (
                    <li key={row.memory_id}>
                      <button type="button" className="mem-record" aria-current={selected?.memory_id === row.memory_id ? 'true' : undefined}
                        onClick={(event) => {
                          rowElement.current = event.currentTarget;
                          if (selectedId === row.memory_id) headingRef.current?.focus();
                          else setSelectedId(row.memory_id);
                        }}>
                        <span className="mem-record__top"><span>{kindLabel(row.kind)}</span>
                          <time dateTime={row.created_at ?? undefined}>{formatAbsoluteTime(row.created_at)}</time></span>
                        <span className="mem-record__preview">{row.text}</span>
                        <span className="mem-record__meta">
                          <span>{originLabel(row.origin)}</span><span>{visibilityLabel(row.visibility)}</span>
                          {row.import_source ? <span className="mem-record__status">AstrBot</span> : null}
                          {status ? <span className="mem-record__status">{status}</span> : null}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        ) : null}
      </div>

      <div className="mem-workspace__detail">
        {selected && !importPanelOpen ? (
          <MemoryInspector key={selected.memory_id} row={selected} loaded={fetched}
            headingRef={headingRef} handlers={handlers} onBack={goBack}
            onSelect={selectedFromHistory} onChanged={goBack} />
        ) : data && !importPanelOpen ? (
          <div className="mem-inspector mem-inspector--empty">
            <span className="mem-inspector__placeholder" aria-hidden="true">◇</span>
            <strong>在列表中选择一条记忆</strong>
            <p>查看完整内容、来源和可见范围。</p>
          </div>
        ) : null}
        {importPanelOpen ? (
          <>
            {/* 阅读态会收起索引列，这里必须有回到列表的出口，否则窄屏会卡在面板里。 */}
            <button type="button" className="ghost-button mem-inspector__back" data-testid="import-back"
              onClick={() => { onCloseImport(); onReload(); }}>
              <ArrowLeftIcon size={15} /> 回到记忆库
            </button>
            <MemoryImportPanel
              // 导入完成后列表要立刻反映真实内容：否则左侧还停在导入前的批次，
              // 结果页说"新增 5 条"、列表却写着"已加载 0 条"。
              onCommitted={onReload}
              onViewImported={() => { onSourceChange('astrbot_memory_companion'); onReload(); onCloseImport(); }}
              onClose={() => { onCloseImport(); onReload(); }}
            />
          </>
        ) : null}
      </div>
    </section>
  );
}

/* ==================================================================== 危险区 */

export interface MemoryDangerZoneProps {
  state: ForgetState;
  dispatch: (action: ForgetAction) => void;
  handlers: Pick<MemoryHandlers, 'forget' | 'purgeStatus'>;
}

/**
 * 轮询后台清理进度，**有上限、可中止**。
 *
 * 为什么不能只靠"提交成功"就结束：两段式里 POST 只做同步的那一小步（代次 +1、
 * 写墓碑、取消在途事件），物理删除在后台。此时界面上说"已遗忘 N 条"就是假话。
 *
 * 三条硬约束（测试逐条钉住）：
 * - 最多 `attempts` 次；到上限就 `timeout` 停下，**不无限轮询**；
 * - 组件卸载（或换了墓碑）时清掉定时器，并用 `AbortController` 掐断在途请求——
 *   否则测试里会报 act 警告、真实页面里会对着已卸载组件 setState；
 * - 单次查询失败不立刻断定"删除失败"：那只是"这次没问到"，下一次再问，
 *   直到上限。删除是否失败只看墓碑的 `state === 'failed'`。
 */
export async function pollForgetProgress(options: {
  tombstoneId: string;
  handler: (signal?: AbortSignal) => Promise<PurgeStatusResponse>;
  intervalMs: number;
  attempts: number;
  isCancelled: () => boolean;
  onUpdate: (result: { decision: PurgePollDecision; item: PurgeStatusItem | null }) => void;
  sleep?: (ms: number) => Promise<void>;
}): Promise<void> {
  const {
    tombstoneId,
    handler,
    intervalMs,
    attempts,
    isCancelled,
    onUpdate,
    sleep = defaultSleep,
  } = options;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0) {
      await sleep(intervalMs);
      if (isCancelled()) return;
    }
    const controller = new AbortController();
    try {
      const response = await handler(controller.signal);
      if (isCancelled()) return;
      const result = decidePurgePoll(response, tombstoneId);
      onUpdate(result);
      if (result.decision !== 'continue') return;
    } catch {
      // 卸载导致的取消：直接退出，不回调。
      if (isCancelled()) return;
      // 其它失败（500 / 网络断）当"这次没问到"，继续下一次。
    }
  }
  if (!isCancelled()) onUpdate({ decision: 'gone', item: null });
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 主按钮文案（轮询中不清回"确认遗忘全部"，否则会让人以为没提交上）。 */
export function forgetButtonLabel(state: ForgetState): string {
  if (state.pending) return '正在提交…';
  return '确认遗忘全部';
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
      const response = await handlers.forget();
      if (isForgetAccepted(response)) {
        // 受理 ≠ 完成：进入后台清理后由轮询去拿真实计数。
        dispatch({ type: 'accepted', accepted: response });
      } else {
        dispatch({ type: 'legacy', retracted: Number(response?.retracted ?? 0) });
      }
    } catch (cause) {
      dispatch({ type: 'failed', error: explainError(cause) });
    }
  };

  const retry = async () => {
    if (!canRetryForget(state)) return;
    dispatch({ type: 'retry' });
    try {
      const response = await handlers.forget();
      if (isForgetAccepted(response)) {
        dispatch({ type: 'accepted', accepted: response });
      } else {
        dispatch({ type: 'legacy', retracted: Number(response?.retracted ?? 0) });
      }
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
          <p className="mem-danger__body" data-testid="mem-danger-scope">
            「遗忘全部」会<strong>从数据库里真正删除</strong>当前账号的记忆与派生数据：
            记忆、情节、核心记忆、{AWARENESS_TERM}、向量、实体关联、检索记录，
            连同 payload 里带原文的在途任务一起清掉。
          </p>
          <p className="mem-danger__body" data-testid="mem-danger-background">
            删除在后台完成，可能需要几秒到几分钟；提交后这个页面会显示清理进度和真实计数。
          </p>
          <p className="mem-danger__body" data-testid="mem-danger-tombstone">
            清完之后只留下来一条<strong>不含任何内容</strong>的删除记录（什么时候删的、删了几行），
            用来防止备份恢复后内容复活——它不存原文，也不存被删的条目。
          </p>
          <p className="mem-danger__irreversible">
            ⚠ 无法撤销：没有恢复接口，页面也没有撤销入口。删掉的内容不会再出现在列表里，
            也不会重新参与召回。
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
          <ul className="mem-danger__list" data-testid="mem-danger-confirm-list">
            <li>记忆行（情节 / 断言 / 程序三类都有，不按 kind 过滤）→ <strong>从库里删除</strong></li>
            <li>情节片段（另一张表 MemoryEpisode）→ <strong>从库里删除</strong></li>
            <li>核心记忆 → <strong>从库里删除</strong>（不再每轮注入）</li>
            <li>{AWARENESS_TERM} → <strong>从库里删除</strong></li>
            <li>向量、实体关联、检索记录 → <strong>从库里删除</strong>（检索记录里有你说的原话）</li>
            <li>在途的记忆提炼任务 → 取消，并擦掉 payload 里的原文</li>
            <li>只留下一条不含任何内容的删除记录，用于防止备份恢复后内容复活</li>
          </ul>
          <p className="mem-danger__body">
            删除在后台完成，可能需要几秒到几分钟。提交后页面会显示清理进度与真实计数。
          </p>
          <p className="mem-danger__irreversible">
            ⚠ 该操作不可撤销。请先确认这些记忆确实不再需要。
          </p>
          <label className="mem-check mem-check--strong">
            <input
              type="checkbox"
              checked={state.ack}
              onChange={(event) => dispatch({ type: 'ack', value: event.target.checked })}
              disabled={state.pending}
            />
            <span>我明白这会从数据库里永久删除当前账号的记忆，且无法撤销</span>
          </label>

          <InlineError error={state.error} resource="list" />

          <div className="mem-confirm__actions">
            <button
              type="button"
              className="button mem-action mem-action--danger mem-danger__confirm-button"
              disabled={!canSubmitForget(state)}
              onClick={() => void run()}
            >
              {forgetButtonLabel(state)}
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

      {state.step === 'purging' ? (
        <div className="mem-danger__result" role="status" data-testid="mem-forget-purging">
          <p className="mem-danger__count">
            <span className="spinner" aria-hidden="true" /> 已提交删除，正在后台清理…
          </p>
          <p className="mem-danger__body">
            记录已受理{state.eventsCancelled !== null ? `，已取消并脱敏 ${state.eventsCancelled} 条在途任务` : ''}。
            清理在后台逐表执行，完成后这里会换成真实删除条数。可以先去别的页面，清理本身不受这个页面影响。
          </p>
          <p className="mem-danger__body">
            进度查询：第 {state.polls} / {PURGE_POLL_MAX_ATTEMPTS} 次
            （最多等 {(PURGE_POLL_INTERVAL_MS * PURGE_POLL_MAX_ATTEMPTS) / 1000} 秒，不会一直轮询）。
          </p>
        </div>
      ) : null}

      {state.step === 'done' ? (
        <div className="mem-danger__result" role="status" data-testid="mem-forget-result">
          <p className="mem-danger__count" data-testid="mem-forget-count-line">
            {describePurgeCounts(state.counts ?? purgeCountsOf(null))}
          </p>
          <p className="mem-danger__body">
            口径：后台清理返回的真实删除行数，按后端墓碑记录原样显示；不把 0 的项列出来，
            也不加总成"影响行数"糊弄过去。
          </p>
          <p className="mem-danger__irreversible">
            ⚠ 无法撤销（没有恢复接口）。列表与核心记忆现在应为空；
            只留下那条不含内容的删除记录。
          </p>
          <button type="button" className="button mem-action" onClick={() => dispatch({ type: 'reset' })}>
            知道了
          </button>
        </div>
      ) : null}

      {state.step === 'failed' ? (
        <div className="mem-danger__result" role="alert" data-testid="mem-forget-failed">
          <p className="mem-danger__count">清理失败：后台删除没有跑完。</p>
          <p className="mem-danger__body">
            原因码 <code data-testid="mem-forget-error-code">{state.errorCode ?? 'purge_failed'}</code>
            （不含任何被删内容）。请求阶段已经生效——列表里已经看不到这些记忆；但库里的行可能还在，
            后台扫描也会按这条记录重试。
          </p>
          <div className="mem-confirm__actions">
            <button
              type="button"
              className="button mem-action mem-action--danger"
              onClick={() => void retry()}
              disabled={state.pending}
            >
              {state.pending ? '正在重新提交…' : '重试删除'}
            </button>
            <button type="button" className="button mem-action" onClick={() => dispatch({ type: 'reset' })}>
              关闭
            </button>
          </div>
        </div>
      ) : null}

      {state.step === 'timeout' ? (
        <div className="mem-danger__result" role="status" data-testid="mem-forget-pending">
          <p className="mem-danger__count">清理仍在进行，稍后刷新查看。</p>
          <p className="mem-danger__body">
            页面已经停止轮询（最多查 {PURGE_POLL_MAX_ATTEMPTS} 次）——这只说明"还没拿到最终结果"，
            既不代表删除失败，也不代表已完成。后台清理会继续，下次打开本页会重新查询；
            如果一直是这个状态，请让维护者看服务端的清理任务。
          </p>
          <button type="button" className="button mem-action" onClick={() => dispatch({ type: 'reset' })}>
            知道了
          </button>
        </div>
      ) : null}

      {state.step === 'unsupported' ? (
        <div className="mem-danger__result" role="alert" data-testid="mem-forget-unsupported">
          <p className="mem-danger__count">服务端还是旧版：这次只做了软清除。</p>
          <p className="mem-danger__body">
            后端返回的是旧接口的 <code>retracted</code> 计数（本次 {state.legacyRetracted ?? 0} 行），
            没有墓碑、也查不到清理进度——说明原文还留在库里。升级后端之后再执行一次「遗忘全部」才是真删。
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
  tab?: MemoryTab;
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
  source?: MemoryImportSourceFilter | null;
  onSourceChange?: (source: MemoryImportSourceFilter | null) => void;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  importPanelOpen?: boolean;
  onToggleImport?: () => void;
  onCloseImport?: () => void;
}

/**
 * 记忆内容区（纯展示）。容器负责取数与回填，这里只决定「每种状态长什么样」。
 *
 * 单独抽出来是为了能直接测「列表成功、核心记忆失败」这种部分失败：
 * 整页不许被一个错误盖住。
 */
export function MemoryContentBody({
  tab = 'content',
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
  source = null,
  onSourceChange = () => {},
  onLoadMore = () => {},
  loadingMore = false,
  importPanelOpen = false,
  onToggleImport = () => {},
  onCloseImport = () => {},
}: MemoryContentBodyProps): React.ReactElement {
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

      <div hidden={tab !== 'content'}>
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
          source={source}
          onSourceChange={onSourceChange}
          loadedCount={list.data?.memories.length ?? 0}
          onLoadMore={onLoadMore}
          loadingMore={loadingMore}
          importPanelOpen={importPanelOpen}
          onToggleImport={onToggleImport}
          onCloseImport={onCloseImport}
        />
      </div>
      <div hidden={tab !== 'core'}>
        <CoreMemorySection state={core} handlers={handlers} onReload={handlers.reloadCore} />
      </div>
      <div hidden={tab !== 'privacy'} className="mem-privacy">
        <section className="panel mem-privacy__intro" aria-labelledby="mem-privacy-title">
          <h2 id="mem-privacy-title">隐私与清理</h2>
          <dl className="mem-facts">
            <div><dt>停用核心记忆</dt><dd>停止进入对话，原文仍保留。</dd></div>
            <div><dt>撤回单条记忆</dt><dd>停止参与召回，历史记录仍可查。</dd></div>
            <div><dt>遗忘全部</dt><dd>删除当前账号的记忆及相关派生数据，不可恢复。</dd></div>
          </dl>
        </section>
        <MemoryDangerZone state={forget} dispatch={onForgetAction} handlers={handlers} />
      </div>
    </div>
  );
}

/* ==================================================================== 内容区容器 */

export function MemoryContent({ tab = 'content' }: { tab?: MemoryTab }): React.ReactElement {
  const [core, setCore] = useState<SectionState<CoreMemoryListResponse>>(loadingSection<CoreMemoryListResponse>());
  const [list, setList] = useState<SectionState<MemoryListResponse>>(loadingSection<MemoryListResponse>());
  const [flags, setFlags] = useState<TombstoneFlags>({ retracted: false, archived: false });
  const [limit, setLimit] = useState<number>(50);
  const [filters, setFilters] = useState<MemoryFilters>(DEFAULT_FILTERS);
  const [source, setSource] = useState<MemoryImportSourceFilter | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [listBusy, setListBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [forget, dispatchForget] = useReducer(forgetReducer, INITIAL_FORGET);

  const include = includeMode(flags);
  const latestQuery = useRef({ limit, include, source });
  latestQuery.current = { limit, include, source };
  const coreRequest = useRef(0);
  const listRequest = useRef(0);
  // 翻页要读"此刻已取回多少条"，但 `loadMore` 不想因此每次列表变化就换身份
  // （换了身份，调用方传下去的回调也跟着变）。
  const listRef = useRef(list);
  listRef.current = list;
  const listBusyRef = useRef(false);

  const loadCore = useCallback(async () => {
    const requestId = ++coreRequest.current;
    setCore((current) => (current.data ? current : loadingSection<CoreMemoryListResponse>()));
    try {
      const data = await api.listCoreMemory();
      if (requestId !== coreRequest.current) return;
      setCore({ status: 'ok', data, error: null });
    } catch (cause) {
      if (requestId !== coreRequest.current) return;
      setCore({ status: 'error', data: null, error: explainError(cause) });
    }
  }, []);

  /**
   * 取第一页。换了来源就从 `offset=0` 重来——来源变了，"已加载多少条"
   * 说的已经是另一个集合，接着往后续会拼出两段不相干的列表。
   */
  const loadList = useCallback(async () => {
    const requestId = ++listRequest.current;
    const query = latestQuery.current;
    listBusyRef.current = true;
    setListBusy(true);
    setList((current) => current.data?.limit === query.limit && current.data.include === query.include
      && (current.data.source ?? null) === query.source ? current : loadingSection<MemoryListResponse>());
    try {
      const data = await api.listMemories(query);
      if (requestId !== listRequest.current) return;
      setList({ status: 'ok', data, error: null });
    } catch (cause) {
      if (requestId !== listRequest.current) return;
      setList({ status: 'error', data: null, error: explainError(cause) });
    } finally {
      // Refresh takes over from any pending page and releases the control when it settles.
      if (requestId === listRequest.current) {
        listBusyRef.current = false;
        setListBusy(false);
      }
    }
  }, []);

  /**
   * 按来源翻到下一页：`offset` 用**当前已经取回的条数**，不另外记账。
   *
   * 另外记一个计数器会让"刷新后接着翻"与"来源一变重来"两件事互相打架：
   * 计数器忘了归零，就会从旧来源的长度继续取，跳掉一整页。
   */
  const loadMore = useCallback(async () => {
    const query = latestQuery.current;
    const current = listRef.current.data;
    if (!query.source || !current?.has_more || listBusyRef.current
      || current.source !== query.source || current.limit !== query.limit
      || current.include !== query.include) return;
    const offset = current.memories.length;
    const requestId = ++listRequest.current;
    listBusyRef.current = true;
    setListBusy(true);
    try {
      const next = await api.listMemories({ ...query, offset });
      if (requestId !== listRequest.current) return;
      setList((state) => state.data ? {
        status: 'ok',
        data: {
          ...next,
          memories: [...state.data.memories, ...next.memories],
          offset: state.data.offset,
        },
        error: null,
      } : { status: 'ok', data: next, error: null });
    } catch (cause) {
      if (requestId !== listRequest.current) return;
      setNotice({ tone: 'warn', text: `加载更早的记忆失败：${explainError(cause).reason}` });
    } finally {
      if (requestId === listRequest.current) {
        listBusyRef.current = false;
        setListBusy(false);
      }
    }
  }, []);

  useEffect(() => {
    if (tab === 'core') void loadCore();
  }, [loadCore, tab]);

  useEffect(() => {
    if (tab === 'content') void loadList();
  }, [loadList, limit, include, source, tab]);


  const handlers: MemoryHandlers = useMemo(
    () => ({
      async retract(memoryId, reason) {
        await api.retractMemory(memoryId, reason);
        setNotice({
          tone: 'ok',
          text: '已撤回，不再参与召回。可在「筛选 → 显示已撤回」中查看。',
        });
        await loadList();
      },
      async supersede(memoryId, text, reason) {
        await api.supersedeMemory(memoryId, text, reason);
        setNotice({
          tone: 'ok',
          text: '纠正已保存。旧条目退出召回，可在历史记录中查看。',
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
        // 请求阶段：只把"删除请求"发出去。**不在这里说"已遗忘 N 条"**——
        // 原文这时还在库里，真实计数要等后台清理（见下面的轮询 effect）。
        const result = await api.forgetAllMemories();
        setNotice({
          tone: 'warn',
          text: isForgetAccepted(result)
            ? '删除请求已受理：正在后台清理，完成后这里会显示真实的删除条数。清理期间就已经不会再召回这些记忆。'
            : '删除请求已被受理，但服务端返回的是旧版软清除计数：原文可能还在库里，见危险区的说明。',
        });
        // 读侧按代次/墓碑过滤，刷新一次就能看到"已经没了"。
        await Promise.all([loadCore(), loadList()]);
        return result;
      },
      purgeStatus(signal) {
        return api.purgeStatus(signal);
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

  /**
   * 后台清理的轮询。
   *
   * 只跟着"已受理的墓碑"走：`purging` 里 `tombstoneId` 一变（新一次删除）就重开一轮。
   * 卸载时把 `cancelled` 置真并清掉定时器——定时器不清，测试里会报 act 警告，
   * 真实页面里会对着已卸载组件 setState。
   *
   * handler 走 ref 而不是放进依赖：`handlers` 每次 `loadList` 变化都会重建，
   * 放进依赖会让轮询每刷一次列表就重启一轮，轮询计数永远到不了上限。
   */
  const tombstoneId = forget.step === 'purging' ? forget.tombstoneId : null;
  const purgePollHandlerRef = useRef(handlers.purgeStatus);
  purgePollHandlerRef.current = handlers.purgeStatus;
  useEffect(() => {
    if (!tombstoneId) return undefined;
    let cancelled = false;
    const timer = setTimeout(() => {
      void pollForgetProgress({
        tombstoneId,
        handler: purgePollHandlerRef.current,
        intervalMs: PURGE_POLL_INTERVAL_MS,
        attempts: PURGE_POLL_MAX_ATTEMPTS,
        isCancelled: () => cancelled,
        onUpdate: (result) => {
          if (cancelled) return;
          if (result.decision === 'gone') {
            // 走到上限仍没拿到这条墓碑的结果：停，但不说"完成"。
            dispatchForget({ type: 'timeout' });
            return;
          }
          dispatchForget({ type: 'poll', decision: result.decision, item: result.item });
        },
      });
    }, PURGE_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [tombstoneId]);

  return (
    <MemoryContentBody
      tab={tab}
      core={core}
      list={list.data && (list.data.limit !== limit || list.data.include !== include) ? loadingSection() : list}
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
      source={source}
      onSourceChange={setSource}
      onLoadMore={() => void loadMore()}
      loadingMore={listBusy}
      importPanelOpen={importOpen}
      onToggleImport={() => setImportOpen((open) => !open)}
      onCloseImport={() => setImportOpen(false)}
    />
  );
}

/* ==================================================================== 调参 tab */

const POLICY_FIELDS: PolicyField[] = [
  {
    key: 'max_injected_memories',
    label: '每轮最多带入的旧记忆',
    hint: '0 表示关闭自动召回。',
    step: 1,
    min: 0,
    max: 12,
  },
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
  '生效状态：已接线。参数会存成草稿并发布（回滚在设置页的版本列表里），发布后由运行时读取：' +
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
          参数来源是 <code>GET /v1/settings/config</code> 的 <code>memory</code> 段，
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
  return (
    <section className="page mem">
      <header className="mem__head">
        <h1 className="page__title">记忆</h1>
        <p className="page__subtitle">查看她记下的事，并纠正不准确的内容。</p>
      </header>
      <nav className="mem-tabs" aria-label="记忆分区">
        {MEMORY_TABS.map(({ id, label }) => (
          <a
            key={id}
            href={memoryTabHref(id)}
            className={`mem-tab${id === tab ? ' mem-tab--active' : ''}`}
            aria-current={id === tab ? 'location' : undefined}
            data-testid={`mem-tab-${id}`}
            onClick={(event) => {
              if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              event.preventDefault();
              if (id !== tab) navigate(memoryTabHref(id));
            }}
          >{label}</a>
        ))}
      </nav>
      <div hidden={tab === 'settings'}>
        <MemoryContent tab={tab} />
      </div>
      {tab === 'settings' ? <MemorySettingsTab onBack={() => navigate('/memory')} /> : null}
    </section>
  );
}
