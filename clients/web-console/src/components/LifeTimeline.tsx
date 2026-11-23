/**
 * 生活时间轴（`/life?tab=now` 顶部那一块）。**独立组件**，不复用「最近做过的事」
 * 那个活动列表：活动的语义是"已发生的进程"，时间轴是"这一天的计划投影"，
 * 两者混成一个列表，就等于把打算说成事实。
 *
 * 三组硬约束（来自后端契约 docs/API-WEB-V1.md §11，逐条对应实现）：
 *
 * 1. **"今天"只听后端的**：`display_date` / `display_timezone` 是展示日，
 *    `plan_day` / `actor_timezone` 是她的计划日。两边日期或时区不同时，
 *    界面要**解释**（见 `timezoneGapNote`），不能抹平成一个"今天"。
 *    时间显示一律用 `Intl` 的 `timeZone` 参数，**不读浏览器时区**。
 * 2. **画位置用 `display_*`，说真话用 `start_at/end_at`**：位置按裁到展示日
 *    窗口的区间算，tooltip 里给真实起止；跨午夜的段在段首/段尾标
 *    「← 前一天 / → 次日」。
 * 3. **计划不是事实**：`segments` 全是"打算/可能"（虚线、浅色），`facts` 才是
 *    有证据的已发生（实心块）。`evidence_kind=clock_only` 只说"时间到了"，
 *    只有 `observed` / `user_confirmed` 才能说"做过"。
 *
 * 状态、梦、意向各有自己的三/四态文案，见下面的映射表——它们之间不能互相
 * 顶替（"读不到"、"没安排"、"还没生成"、"有记录但你看不到"是四件事）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  DayIntentSummary,
  DreamCard,
  LifeTimelineFact,
  LifeTimelineOut,
  LifeTimelineSegment,
  OutlineJobOut,
  TimelineState,
} from '../types';
import { AlertIcon, ChevronIcon, RefreshIcon } from './Icons';
import '../timeline.css';

/* ------------------------------------------------------------ 纯函数（可测） */

/**
 * 时区名做一次校验：非法时**不静默退回浏览器时区**，退到 UTC。
 *
 * 退回本机时区是最坏的一种"修"：界面看起来正常，但"她那边 09:00"已经被
 * 静默改写了，而且没有任何标记。退 UTC 至少是同一个基准（后端的时间戳就是
 * UTC），配合界面上照常显示的时区名，能看出哪里不对。
 */
export function safeTimeZone(timeZone: string | null | undefined): string {
  const raw = String(timeZone ?? '').trim();
  if (!raw) return 'UTC';
  try {
    new Intl.DateTimeFormat('zh-CN', { timeZone: raw }).format(new Date());
    return raw;
  } catch {
    return 'UTC';
  }
}

function toDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `HH:mm`（按指定 IANA 时区；**不用**浏览器本地时区）。 */
export function formatInZone(value: string | null | undefined, timeZone: string): string {
  const date = toDate(value);
  if (!date) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: safeTimeZone(timeZone),
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date);
}

/** `MM-DD HH:mm`（按指定 IANA 时区）。跨天的 tooltip 必须带日期。 */
export function formatDayTimeInZone(
  value: string | null | undefined,
  timeZone: string,
): string {
  const date = toDate(value);
  if (!date) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: safeTimeZone(timeZone),
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })
    .format(date)
    .replace(/\//g, '-');
}

/** 带年份的时间，用于可能跨年的历史生成记录。 */
export function formatFullDayTimeInZone(
  value: string | null | undefined,
  timeZone: string,
): string {
  const date = toDate(value);
  if (!date) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: safeTimeZone(timeZone),
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })
    .format(date)
    .replace(/\//g, '-');
}

/** 跨夏令时变化时补充两端的 UTC 偏移，避免相同时钟读数造成歧义。 */
function formatUnplannedRange(start: string, end: string, timeZone: string): string {
  const from = formatDayTimeInZone(start, timeZone);
  const to = formatDayTimeInZone(end, timeZone);
  const startDate = toDate(start);
  const endDate = toDate(end);
  if (!startDate || !endDate) return `${from || '—'}–${to || '—'}`;

  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone: safeTimeZone(timeZone),
    hour: '2-digit',
    timeZoneName: 'shortOffset',
  });
  const offsetAt = (date: Date) =>
    formatter.formatToParts(date).find((part) => part.type === 'timeZoneName')?.value;
  const startOffset = offsetAt(startDate);
  const endOffset = offsetAt(endDate);
  if (startOffset && endOffset && startOffset !== endOffset) {
    return `${from} (${startOffset})–${to} (${endOffset})`;
  }
  return `${from}–${to}`;
}

export function formatRangeInZone(
  start: string | null | undefined,
  end: string | null | undefined,
  timeZone: string,
): string {
  const from = formatInZone(start, timeZone);
  const to = formatInZone(end, timeZone);
  if (!from && !to) return '';
  return `${from || '—'}–${to || '—'}`;
}

function minutesBetween(startIso: string, endIso: string): number {
  const start = toDate(startIso);
  const end = toDate(endIso);
  if (!start || !end) return 0;
  return Math.max(0, Math.round((end.getTime() - start.getTime()) / 60_000));
}

/** 计划段高度：按时间比例，但给下限（可读）与上限（不撑爆一屏）。 */
export const SEGMENT_MIN_HEIGHT = 56;
export const SEGMENT_MAX_HEIGHT = 88;
export const SEGMENT_PX_PER_MINUTE = 0.2;

export function segmentHeightPx(segment: LifeTimelineSegment): number {
  const minutes = minutesBetween(segment.display_start_at, segment.display_end_at);
  const scaled = minutes * SEGMENT_PX_PER_MINUTE;
  return Math.round(Math.min(SEGMENT_MAX_HEIGHT, Math.max(SEGMENT_MIN_HEIGHT, scaled)));
}

export type SegmentPhase = 'past' | 'now' | 'upcoming';

/** 已过去 / 此刻 / 未到。**按裁剪后的展示区间**判，只用于画状态。 */
export function segmentPhase(segment: LifeTimelineSegment, now: Date): SegmentPhase {
  const start = toDate(segment.display_start_at);
  const end = toDate(segment.display_end_at);
  if (!start || !end) return 'upcoming';
  if (end.getTime() <= now.getTime()) return 'past';
  if (start.getTime() <= now.getTime()) return 'now';
  return 'upcoming';
}

export interface SegmentNode {
  segment: LifeTimelineSegment;
  children: SegmentNode[];
}

/**
 * 用 `parent_id` 把父子段折成树。父段不在响应里的子段当根处理——宁可多画
 * 一条，也不要因为"父段被受众裁掉"就静默丢掉整棵子树。
 */
export function buildSegmentTree(segments: readonly LifeTimelineSegment[]): SegmentNode[] {
  const byId = new Map(segments.map((segment) => [segment.segment_id, segment]));
  const nodes = new Map<string, SegmentNode>(
    segments.map((segment) => [segment.segment_id, { segment, children: [] }]),
  );
  const roots: SegmentNode[] = [];
  for (const segment of segments) {
    const node = nodes.get(segment.segment_id);
    if (!node) continue;
    const parentId = segment.parent_id;
    const parent = parentId ? nodes.get(parentId) : undefined;
    if (parent && parentId && byId.has(parentId) && parentId !== segment.segment_id) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const byTime = (a: SegmentNode, b: SegmentNode) =>
    a.segment.display_start_at.localeCompare(b.segment.display_start_at) ||
    a.segment.display_end_at.localeCompare(b.segment.display_end_at) ||
    a.segment.segment_id.localeCompare(b.segment.segment_id);
  roots.sort(byTime);
  for (const node of nodes.values()) node.children.sort(byTime);
  return roots;
}

function overlaps(
  startIso: string | null,
  endIso: string | null,
  start: string,
  end: string,
): boolean {
  const factStart = toDate(startIso);
  const segStart = toDate(start);
  const segEnd = toDate(end);
  if (!segStart || !segEnd) return false;
  // 没有开始时刻的活动无法对齐到时间轴：不猜它落在哪一段里。
  if (!factStart) return false;
  const factEnd = toDate(endIso) ?? new Date();
  return factStart.getTime() < segEnd.getTime() && factEnd.getTime() > segStart.getTime();
}

/**
 * 与这一段重叠的事实。已经落在子段里的事实归子段——否则父子段会各显示一遍。
 */
export function factsForSegment(
  facts: readonly LifeTimelineFact[],
  segment: LifeTimelineSegment,
  children: readonly LifeTimelineSegment[] = [],
): LifeTimelineFact[] {
  return facts.filter(
    (fact) =>
      overlaps(fact.started_at, fact.ended_at, segment.display_start_at, segment.display_end_at) &&
      !children.some((child) =>
        overlaps(fact.started_at, fact.ended_at, child.display_start_at, child.display_end_at),
      ),
  );
}

/* ------------------------------------------------------ 当天的合并视图 */

/** 合并后的一条记录：事实 + 它落在哪一段（`null` = 没有宿主）。 */
export interface DayRecord {
  fact: LifeTimelineFact;
  /** 宿主叶段 id。`null` = 找不到归属，它自己占一行。 */
  segmentId: string | null;
}

export interface DayTimeline {
  /** 有证据的记录，按 `activity_id` 唯一。 */
  records: DayRecord[];
  /** `clock_only`：只证明时间到了，收进可展开的「时间检查」。 */
  checks: LifeTimelineFact[];
  /** 分段归属：segment_id → 该段名下的记录。 */
  bySegment: Map<string, DayRecord[]>;
  /** 没有宿主的记录。**保留自己的开始时间**，不塞进任何段。 */
  loose: DayRecord[];
}

/**
 * 活动记录的措辞。
 *
 * `kind` 是活动自己的类型；旧后端没有这个字段、给了 null、或给了映射表外的值，
 * 三种情况都只能说「活动记录」——**不编名字**，也不拿 history 去补。
 */
export function activityName(kind: string | null | undefined): string {
  const raw = String(kind ?? '').trim();
  if (!raw) return '活动记录';
  const known = ACTIVITY_KIND[raw];
  if (known) return known;
  /*
   * 未知类型一律回落「活动记录」。
   *
   * `kind` 在后端是自由字符串，不是枚举；把未知值原样显示会把内部代码
   * （`call`、`wood_working` 之类）直接摆到读者面前——那不是在说"她做了什么"，
   * 而是在展示我们的字段。说不出就说不说。
   */
  return '活动记录';
}

const ACTIVITY_KIND: Record<string, string> = {
  writing: '写作',
  write: '写作',
  reading: '读书',
  read: '读书',
  resting: '休息',
  rest: '休息',
  walking: '散步',
  walk: '散步',
  thinking: '想事情',
  working: '工作',
  work: '工作',
  eating: '吃饭',
  meal: '吃饭',
  cooking: '做饭',
  sleep: '睡觉',
  nap: '小睡',
  listening: '听音乐',
  music: '听音乐',
  call: '接电话',
  chatting: '聊天',
  browsing: '上网',
  cleaning: '打扫',
  exercising: '运动',
};

type DisplayWindow = Pick<LifeTimelineOut, 'display_window_start_at' | 'display_window_end_at'>;

interface VisibleRange {
  start: string | null;
  end: string | null;
  crossesForward: boolean;
  crossesBackward: boolean;
  endsAtWindowEnd: boolean;
}

/**
 * 一条记录在**展示窗口内**能看见的区间。
 *
 * 跨午夜时不再给出 `23:30–01:30` 这种回绕读数：今天这一侧裁到当日 24:00，
 * 跨入今天的一侧从窗口起点起算。真实起止由调用方另外给出。
 */
export function visibleRange(
  fact: Pick<LifeTimelineFact, 'started_at' | 'ended_at'>,
  timeline: DisplayWindow,
): VisibleRange {
  const start = toDate(fact.started_at);
  const end = toDate(fact.ended_at);
  const windowStart = toDate(timeline.display_window_start_at);
  const windowEnd = toDate(timeline.display_window_end_at);
  if (!start || !end || !windowStart || !windowEnd || end <= windowStart || start >= windowEnd) {
    return {
      start: fact.started_at, end: fact.ended_at,
      crossesForward: false, crossesBackward: false, endsAtWindowEnd: false,
    };
  }
  const crossesBackward = start < windowStart;
  const crossesForward = end > windowEnd;
  return {
    start: crossesBackward ? timeline.display_window_start_at : fact.started_at,
    end: crossesForward ? timeline.display_window_end_at : fact.ended_at,
    crossesForward,
    crossesBackward,
    endsAtWindowEnd: end >= windowEnd,
  };
}

/**
 * 把计划段与事实合成一条当天的序列。
 *
 * 唯一目标是那条不变量：**每个 `activity_id` 在当天页面里只呈现一次**。
 * 之前三处各画一遍（段内、右列全量、首屏「今天留下的片段」），没有一处知道
 * 别处也画了；在这里统一裁决，渲染层不再自己决定"要不要显示"。
 *
 * 归属规则，按顺序：
 * 1. `clock_only` 不参与归属——它只证明时间到了，全部进「时间检查」；
 * 2. 按**开始时刻**找唯一宿主叶段（包含该时刻的那一段）。放在某个计划旁边
 *    只是排版依据，**不代表计划被执行了**，措辞上不许混为一谈；
 * 3. 开始时刻不在任何叶段内时，只有与该段重叠才挂上去，取最早的一个
 *    （按时间排，不随响应顺序变化）；
 * 4. 都找不到 → 独立记录，保留自己的开始时间。
 */
export function mergeDayTimeline(
  timeline: Pick<LifeTimelineOut, 'segments' | 'facts'>,
): DayTimeline {
  const checks: LifeTimelineFact[] = [];
  const live = timeline.segments.filter((segment) => segment.status === 'current');
  const leaves = live.filter((segment) => segment.is_leaf);

  const records: DayRecord[] = [];
  const bySegment = new Map<string, DayRecord[]>();
  const loose: DayRecord[] = [];

  for (const fact of timeline.facts) {
    if (fact.evidence_kind === 'clock_only') {
      checks.push(fact);
      continue;
    }
    const start = toDate(fact.started_at);
    let host: LifeTimelineSegment | null = null;
    if (start) {
      host =
        leaves.find((segment) => {
          const from = toDate(segment.display_start_at);
          const to = toDate(segment.display_end_at);
          return Boolean(from && to && from.getTime() <= start.getTime() && start.getTime() < to.getTime());
        }) ?? null;
      if (!host) {
        // 开始时刻不在段内：重叠也算归属，但只取最早的一段，避免随顺序漂移。
        const end = toDate(fact.ended_at) ?? start;
        const reach = Math.max(end.getTime(), start.getTime() + 1);
        host =
          leaves
            .filter((segment) => {
              const from = toDate(segment.display_start_at);
              const to = toDate(segment.display_end_at);
              return Boolean(from && to && from.getTime() < reach && to.getTime() > start.getTime());
            })
            .sort((a, b) => a.display_start_at.localeCompare(b.display_start_at))[0] ?? null;
      }
    }
    const record: DayRecord = { fact, segmentId: host ? host.segment_id : null };
    records.push(record);
    if (host) {
      const bucket = bySegment.get(host.segment_id);
      if (bucket) bucket.push(record);
      else bySegment.set(host.segment_id, [record]);
    } else {
      loose.push(record);
    }
  }

  checks.sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)));
  loose.sort(sortByStart);
  for (const bucket of bySegment.values()) bucket.sort(sortByStart);
  return { records, checks, bySegment, loose };
}

function sortByStart(a: DayRecord, b: DayRecord): number {
  const left = String(a.fact.started_at ?? '');
  const right = String(b.fact.started_at ?? '');
  return left.localeCompare(right) || a.fact.activity_id.localeCompare(b.fact.activity_id);
}

/** 此刻的情景：`running` **且** 有效期未过。过期即未知，不念旧账。 */
export function currentScene(
  facts: readonly LifeTimelineFact[],
  now: Date,
): LifeTimelineFact | null {
  return (
    facts.find((fact) => {
      if (fact.state !== 'running') return false;
      const until = toDate(fact.scene_valid_until);
      return Boolean(until && until.getTime() > now.getTime());
    }) ?? null
  );
}

/**
 * 下一项计划：**开始时刻晚于现在**的叶段。
 *
 * 已经开始的那一段不在这里再说一遍——它由此刻那一块承担。已经过去的也不选。
 */
export function nextPlannedSegment(
  segments: readonly LifeTimelineSegment[],
  now: Date,
): LifeTimelineSegment | null {
  const nowMs = now.getTime();
  return (
    segments
      .filter((segment) => segment.status === 'current' && segment.is_leaf)
      .filter((segment) => {
        const start = toDate(segment.display_start_at);
        return Boolean(start && start.getTime() > nowMs);
      })
      .sort((a, b) => a.display_start_at.localeCompare(b.display_start_at))[0] ?? null
  );
}

/**
 * 事实的措辞。**证据不足就不能说"做过"**：
 * `clock_only` 只证明时间到了，`scene_valid_until=null` 表示此刻未知。
 */
/**
 * 记录的可见区间文案。
 *
 * 跨午夜时，被截断的那一端写成 **24:00**（而不是 23:59）：读者要知道
 * "它一直持续到午夜"，而不是"它在 23:59 结束了"。
 */
export function formatRecordRange(visible: VisibleRange, timeZone: string): string {
  const from = formatInZone(visible.start, timeZone);
  const to = visible.endsAtWindowEnd ? '24:00' : formatInZone(visible.end, timeZone);
  if (!from && !to) return '';
  return `${from || '—'}–${to || '—'}`;
}

/** 这条记录此刻还算不算"正在进行"：`running` 且有效期未过。 */
export function isLiveNow(fact: LifeTimelineFact, now: Date): boolean {
  if (fact.state !== 'running') return false;
  const until = toDate(fact.scene_valid_until);
  return Boolean(until && until.getTime() > now.getTime());
}

/**
 * 记录的措辞。比 `evidenceLabel` 多一层：**正在进行的不叫"做过"**。
 *
 * 没有结束时刻的 running 记录，情景还有效时说「进行中」；情景过期后它只是
 * 一条停在过去的记录，措辞退回「记录在此停下」——此刻那一块已经说未知了，
 * 这里不能替它作证。
 */
export function factStateLabel(fact: LifeTimelineFact, now: Date): string {
  if (fact.state === 'running') return isLiveNow(fact, now) ? '进行中' : '记录在此停下';
  return evidenceLabel(fact.evidence_kind);
}

export function evidenceLabel(evidenceKind: string): string {
  switch (evidenceKind) {
    case 'clock_only':
      return '时间到了';
    case 'observed':
      return '做过';
    case 'user_confirmed':
      return '做过（你确认过）';
    default:
      return `有活动记录（证据：${evidenceKind || '未标注'}）`;
  }
}

/**
 * 计划段上的承诺标签。**只有** `anchor_kind=commitment` 且
 * `commitment_level=confirmed` 才能说「已约好」。
 */
export function commitmentTag(segment: LifeTimelineSegment): string | null {
  if (segment.anchor_kind === 'commitment') {
    return segment.commitment_level === 'confirmed' ? '已约好' : '固定时段（未确认）';
  }
  if (segment.anchor_kind === 'sleep') {
    // 预计休息 ≠ 她睡着了。措辞上不给她安一个状态。
    return '预计休息';
  }
  if (segment.commitment_level === 'confirmed') return '你确认过的计划';
  return null;
}

export const LAYER_LABEL: Record<string, string> = {
  block: '大块',
  hour: '小时',
  minute: '分钟',
};

const SOURCE_LABEL: Record<string, string> = {
  model: '模型排的',
  rule: '规则排的',
  baseline: '基线',
  user: '你定的',
};

export function sourceLabel(source: string): string {
  return SOURCE_LABEL[source] ?? source;
}

/** 活动的来源（与生活页旧列表同一套中文；未知值原样显示）。 */
const FACT_ORIGIN_LABEL: Record<string, string> = {
  simulated: '模拟日常',
  // 后端写在 `activities.origin` 上的真实值是 `user_stated`（见 infra/models.py 的
  // 注释）。这里同时收下 `user_statement`：那张表历史上两种拼法都出现过，
  // 漏一个就会把内部代码 `user_stated` 直接显示给读者。
  user_stated: '你说的',
  user_statement: '你说的',
  external_observation: '观察到的',
  verified_source: '查到的',
  tool_receipt: '工具回执',
  model_inference: '推测',
};

/**
 * 来源措辞。**未知来源不原样露出**：`origin` 是自由字符串，
 * 把 `foo_bar` 摆到界面上说明不了任何事，只会像一处没做完的占位。
 */
export function factOriginLabel(origin: string): string {
  const raw = String(origin ?? '').trim();
  if (!raw) return '来源未标注';
  return FACT_ORIGIN_LABEL[raw] ?? '其他来源';
}

export interface StateCopy {
  label: string;
  hint: string;
}

/**
 * `state` 六值各自的文案。六种**必须**分开：下一步动作完全不同
 * （等生成 / 接受兜底 / 查原因码 / 重试 / 真的没安排）。
 */
export const TIMELINE_STATE_COPY: Record<TimelineState, StateCopy> = {
  ready: {
    label: '大纲已生成',
    hint: '今天的安排已就绪，可以查看各时段的计划。',
  },
  degraded: {
    label: '来自规则兜底',
    hint: '模型暂未生成完整日程，当前展示按作息规则保留的安排。',
  },
  failed: {
    label: '生成失败',
    hint: '最近一次生成没有完成，详细原因见生成详情。',
  },
  pending: {
    label: '还没生成',
    hint: '日程正在等待生成，时间见生成详情。',
  },
  empty: {
    label: '这一天还没有安排',
    hint: '这一天尚未生成日程。',
  },
  unavailable: {
    label: '暂时无法读取',
    hint: '暂时无法取得日程，请重试。',
  },
};

/** 原因码只对降级/失败有意义；其余状态不展示，免得把说明当故障。 */
export function reasonCode(timeline: Pick<LifeTimelineOut, 'state' | 'last_error_code'>): string {
  if (timeline.state !== 'degraded' && timeline.state !== 'failed') return '';
  return String(timeline.last_error_code ?? '').trim();
}

const TIMEZONE_SOURCE_LABEL: Record<string, string> = {
  preference: '你设的偏好',
  env: '环境变量',
  default: '默认值',
};

function timezoneSourceLabel(source: string): string {
  return TIMEZONE_SOURCE_LABEL[source] ?? source;
}

/**
 * 两侧日期/时区不一致时的解释句；一致（或只差在来源）时返回 null。
 *
 * 不能只写「今天」：展示时区是**你**的今天，计划日是**她**的今天，
 * 多用户共享角色时这两个可以不是同一天。
 */
export function timezoneGapNote(
  timeline: Pick<
    LifeTimelineOut,
    'display_date' | 'display_timezone' | 'plan_day' | 'actor_timezone'
  >,
): string | null {
  const sameDay = timeline.display_date === timeline.plan_day;
  const sameZone = timeline.display_timezone === timeline.actor_timezone;
  if (sameDay && sameZone) return null;
  if (!sameDay) {
    return `你的今天（${timeline.display_timezone}）是 ${timeline.display_date}，她那边（${timeline.actor_timezone}）的计划日是 ${timeline.plan_day}——两者不是同一天；时间轴按「你的今天」裁剪，跨过去的段只会显示一半。`;
  }
  return `两边的时区不同（你 ${timeline.display_timezone} / 她 ${timeline.actor_timezone}），今天恰好落在同一个日历日 ${timeline.display_date}；同一时刻在两边的钟点上不一样。`;
}

export function intentStateLabel(state: string): string {
  switch (state) {
    case 'proposed':
      return '候选';
    case 'planned':
      return '想/考虑';
    case 'confirmed':
      // 用户确认的是**目标/日期**，时段还没定——不能说"已约好"。
      return '已确认想在这个日期段做（时段仍待定）';
    default:
      return state;
  }
}

export const INTENT_SOURCE_LABEL: Record<string, string> = {
  model: '模型提的',
  user: '你提的',
  actor: '她提的',
  recurrence: '按周期',
};

export function intentSourceLabel(kind: string): string {
  return INTENT_SOURCE_LABEL[kind] ?? kind;
}

export function intentDayRange(intent: Pick<DayIntentSummary, 'target_start_day' | 'target_end_day'>): string {
  if (intent.target_start_day === intent.target_end_day) return intent.target_start_day;
  return `${intent.target_start_day} → ${intent.target_end_day}`;
}

/** 梦的三种样子：没有记录 / 有记录但看不到 / 看得到。**不能合并**。 */
export type DreamView = 'none' | 'restricted' | 'content';

export function dreamView(dream: DreamCard | null | undefined): DreamView {
  if (!dream) return 'none';
  return dream.restricted ? 'restricted' : 'content';
}

/** 受众裁决隐藏了哪些内容（空数组 ≠ 没有内容，靠计数区分）。 */
export function hiddenSummary(
  timeline: Pick<
    LifeTimelineOut,
    'has_private_content' | 'hidden_segment_count' | 'hidden_fact_count' | 'hidden_intent_count'
  >,
): string | null {
  if (!timeline.has_private_content) return null;
  const parts: string[] = [];
  if (timeline.hidden_segment_count > 0) parts.push(`计划段 ${timeline.hidden_segment_count} 条`);
  if (timeline.hidden_fact_count > 0) parts.push(`活动 ${timeline.hidden_fact_count} 条`);
  if (timeline.hidden_intent_count > 0) parts.push(`意向 ${timeline.hidden_intent_count} 条`);
  if (parts.length === 0) return '有内容因受众裁决被隐藏（梦的正文可能也在其中）。';
  return `有私密内容被隐藏：${parts.join('、')}。它们不在下面的列表里，不代表不存在。`;
}

/** 计划区在空数组时的措辞：先看"有没有被隐藏"，再看状态。 */
export function segmentEmptyText(
  timeline: Pick<LifeTimelineOut, 'state' | 'has_private_content' | 'hidden_segment_count'>,
): string {
  if (timeline.hidden_segment_count > 0) {
    return `有 ${timeline.hidden_segment_count} 条计划段因受众裁决被隐藏——看不到正文，不是没有安排。`;
  }
  if (timeline.state === 'empty') return '这一天没有计划段。';
  if (timeline.state === 'pending') return '大纲还没生成，所以还没有计划段。';
  if (timeline.state === 'failed') return '生成失败了，现在没有可展示的计划段。';
  if (timeline.state === 'degraded') return '规则兜底的大纲里没有给出计划段。';
  if (timeline.state === 'unavailable') return '';
  return '这次给出的大纲里没有计划段。';
}

/* ---------------------------------------------------------------- 子组件 */

interface RecordRowProps {
  record: DayRecord;
  now: Date;
  timeZone: string;
  timeline: DisplayWindow;
  /** 没有宿主的记录：加一层虚线和「不属于任何时段」。 */
  loose?: boolean;
  testIdPrefix: string;
}

/**
 * 一条活动记录。**唯一的呈现方式**——段内、独立列表都走这里，
 * 所以措辞、时间口径、跨午夜处理不会在两处漂移。
 */
export function RecordRow({ record, now, timeZone, timeline, loose = false, testIdPrefix }: RecordRowProps) {
  const fact = record.fact;
  const live = isLiveNow(fact, now);
  const visible = visibleRange(fact, timeline);
  const shown = formatRecordRange(visible, timeZone) || '时间未知';
  const realRange =
    fact.started_at && fact.ended_at
      ? `${formatDayTimeInZone(fact.started_at, timeZone)}–${formatDayTimeInZone(fact.ended_at, timeZone)}`
      : '';
  return (
    <li
      className={`tl-fact tl-fact--solid${live ? ' tl-fact--live' : ''}${loose ? ' tl-fact--loose' : ''}`}
      data-testid={`${testIdPrefix}-${fact.activity_id}`}
      data-evidence={fact.evidence_kind}
      data-state={fact.state}
      data-origin={fact.origin}
    >
      <span className="tl-fact__name">{activityName(fact.kind)}</span>
      <span className="tl-fact__label">{factStateLabel(fact, now)}</span>
      <span className="tl-fact__time">
        {shown}
        {visible.crossesForward ? ' · 跨到次日' : ''}
        {visible.crossesBackward ? ' · 从前一天延续' : ''}
      </span>
      <span className="tl-fact__kind">{factOriginLabel(fact.origin)}</span>
      {realRange && (visible.crossesForward || visible.crossesBackward) ? (
        <span className="tl-fact__real">真实起止 {realRange}</span>
      ) : null}
      {live && fact.scene_valid_until ? (
        <span className="tl-fact__scene">情景有效至 {formatInZone(fact.scene_valid_until, timeZone)}</span>
      ) : null}
    </li>
  );
}

function SegmentFacts({
  records,
  now,
  timeZone,
  timeline,
}: {
  records: readonly DayRecord[];
  now: Date;
  timeZone: string;
  timeline: DisplayWindow;
}) {
  if (records.length === 0) return null;
  return (
    <ul className="tl-seg__facts">
      {records.map((record) => (
        <RecordRow
          key={record.fact.activity_id}
          record={record}
          now={now}
          timeZone={timeZone}
          timeline={timeline}
          testIdPrefix="tl-fact"
        />
      ))}
    </ul>
  );
}

interface SegmentRowProps {
  node: SegmentNode;
  depth: number;
  /** 合并结果：段 id → 它名下的记录。**不再**在这里自己过滤 facts。 */
  bySegment: ReadonlyMap<string, DayRecord[]>;
  now: Date;
  timeZone: string;
  timeline: DisplayWindow;
  collapsed: ReadonlySet<string>;
  onToggle: (segmentId: string) => void;
}

function SegmentRow({ node, depth, bySegment, now, timeZone, timeline, collapsed, onToggle }: SegmentRowProps) {
  const segment = node.segment;
  const phase = segmentPhase(segment, now);
  const ownFacts = bySegment.get(segment.segment_id) ?? [];
  const hasChildren = node.children.length > 0;
  const isCollapsed = collapsed.has(segment.segment_id);
  const tag = commitmentTag(segment);
  const layer = LAYER_LABEL[segment.layer] ?? segment.layer;

  return (
    <li
      className={`tl-item tl-item--${phase}`}
      data-testid={`tl-seg-${segment.segment_id}`}
      data-phase={phase}
      data-layer={segment.layer}
      data-status={segment.status}
    >
      <div className="tl-item__time" aria-hidden="true">
        <span>{formatInZone(segment.display_start_at, timeZone)}</span>
        <span className="tl-item__time-end">{formatInZone(segment.display_end_at, timeZone)}</span>
      </div>

      <div
        className={`tl-seg tl-seg--plan tl-seg--layer-${segment.layer} tl-seg--${phase}`}
        style={{ minHeight: `${segmentHeightPx(segment)}px` }}
        // 悬停/读屏都给**真实**起止：画位置用的是裁剪值，不是段的真身。
        title={`${segment.title}｜真实起止 ${formatDayTimeInZone(segment.start_at, timeZone)}–${formatDayTimeInZone(segment.end_at, timeZone)}（${timeZone}）｜计划日 ${segment.plan_day}`}
      >
        {segment.continues_from_previous_day ? (
          <span className="tl-seg__edge tl-seg__edge--from" data-testid="tl-continues-from">
            ← 前一天
          </span>
        ) : null}
        <div className="tl-seg__head">
          {hasChildren ? (
            <button
              type="button"
              className="tl-seg__toggle"
              aria-expanded={!isCollapsed}
              data-testid={`tl-toggle-${segment.segment_id}`}
              onClick={() => onToggle(segment.segment_id)}
            >
              <ChevronIcon size={13} />
              <span>{isCollapsed ? '展开' : '收起'}</span>
            </button>
          ) : null}
          <span className="tl-seg__title">{segment.title}</span>
          {tag ? <span className="tl-seg__tag">{tag}</span> : null}
          {phase === 'now' ? <span className="tl-seg__now">此刻</span> : null}
        </div>
        <div className="tl-seg__meta">
          <span>{layer}</span>
          <span>{sourceLabel(segment.source)}</span>
          <span className="tl-seg__range">
            {formatRangeInZone(segment.display_start_at, segment.display_end_at, timeZone)}
          </span>
        </div>
        <SegmentFacts records={ownFacts} now={now} timeZone={timeZone} timeline={timeline} />
        {segment.continues_next_day ? (
          <span className="tl-seg__edge tl-seg__edge--next" data-testid="tl-continues-next">
            → 次日
          </span>
        ) : null}
      </div>

      {hasChildren && !isCollapsed ? (
        <ol className="tl-children tl-children--depth" data-testid={`tl-children-${segment.segment_id}`}>
          {node.children.map((child) => (
            <SegmentRow
              key={child.segment.segment_id}
              node={child}
              depth={depth + 1}
              bySegment={bySegment}
              now={now}
              timeZone={timeZone}
              timeline={timeline}
              collapsed={collapsed}
              onToggle={onToggle}
            />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

export interface TimelineTrackProps {
  segments: readonly LifeTimelineSegment[];
  /** 合并结果：段 id → 记录的归属表。记录只从这里进来，不再各自过滤 facts。 */
  bySegment: ReadonlyMap<string, DayRecord[]>;
  timeZone: string;
  timeline: DisplayWindow;
  now: Date;
  /** 默认收起的父段 id（只作初始值；展开/收起由组件自己的状态管）。 */
  defaultCollapsed?: readonly string[];
}

/**
 * 竖排时间轴：非等长块（时间比例 + 下限/上限）、父段可折叠子段、
 * `block/hour/minute` 靠缩进与字号分层。
 */
export function TimelineTrack({
  segments,
  bySegment,
  timeZone,
  timeline,
  now,
  defaultCollapsed = [],
}: TimelineTrackProps) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(
    () => new Set(defaultCollapsed),
  );
  const tree = useMemo(() => buildSegmentTree(segments), [segments]);
  const toggle = (segmentId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(segmentId)) next.delete(segmentId);
      else next.add(segmentId);
      return next;
    });
  };
  return (
    <ol className="tl-track" aria-label="一天的计划时间轴">
      {tree.map((node) => (
        <SegmentRow
          key={node.segment.segment_id}
          node={node}
          depth={0}
          bySegment={bySegment}
          now={now}
          timeZone={timeZone}
          timeline={timeline}
          collapsed={collapsed}
          onToggle={toggle}
        />
      ))}
    </ol>
  );
}

export function TimelineHeader({
  timeline,
  onRetry,
}: {
  timeline: LifeTimelineOut;
  onRetry?: () => void;
}) {
  const copy = TIMELINE_STATE_COPY[timeline.state];
  const code = reasonCode(timeline);
  const gap = timezoneGapNote(timeline);
  const hidden = hiddenSummary(timeline);
  const explanation = timeline.reason_explanation ?? (
    timeline.state === 'degraded' || timeline.state === 'failed' ? '具体原因暂不明确。' : null
  );
  const lastModelOutline = formatFullDayTimeInZone(
    timeline.last_model_outline_at,
    timeline.display_timezone,
  );
  return (
    <header className="tl-head">
      <div className="tl-heading">
        <div>
          <span className="tl-day__label">当日日程</span>
          <h3 className="tl-heading__date">{timeline.display_date}</h3>
          <span className="tl-day__tz">{timeline.display_timezone}</span>
        </div>
        {timeline.state !== 'unavailable' ? (
          <span className="tl-heading__count">{timeline.segments.length} 个可见时段</span>
        ) : null}
      </div>
      {gap ? (
        <p className="tl-days__note" data-testid="tl-tz-note">
          {gap}
        </p>
      ) : null}

      <p className="tl-state" data-testid="tl-state" data-state={timeline.state}>
        <strong>{copy.label}</strong>
        <span className="tl-state__hint">{copy.hint}</span>
        {onRetry && timeline.state === 'unavailable' ? (
          <button type="button" className="button button--small" onClick={onRetry}>
            <RefreshIcon /> 重试
          </button>
        ) : null}
      </p>
      {explanation ? (
        <p className="tl-reason" data-testid="tl-reason">{explanation}</p>
      ) : null}
      <p className="tl-last-model" data-testid="tl-last-model">
        最近一次模型生成成功：{lastModelOutline || '未知'}
      </p>
      <details className="tl-diagnostics" data-testid="tl-diagnostics">
        <summary>生成详情与时区</summary>
        <div className="tl-diagnostics__body">
          <div className="tl-days">
            <div className="tl-day" data-testid="tl-display-day">
              <span className="tl-day__label">你的今天（展示日）</span>
              <strong className="tl-day__value">{timeline.display_date}</strong>
              <span className="tl-day__tz">
                {timeline.display_timezone}
                <span className="tl-day__src">· {timezoneSourceLabel(timeline.display_timezone_source)}</span>
              </span>
            </div>
            <div className="tl-day" data-testid="tl-plan-day">
              <span className="tl-day__label">她的计划日</span>
              <strong className="tl-day__value">{timeline.plan_day}</strong>
              <span className="tl-day__tz">
                {timeline.actor_timezone}
                <span className="tl-day__src">· {timezoneSourceLabel(timeline.actor_timezone_source)}</span>
              </span>
            </div>
          </div>
          {code ? (
            <code className="tl-state__code" data-testid="tl-reason-code">
              原因码：{code}
            </code>
          ) : null}
          {timeline.note && timeline.note !== copy.hint ? (
            <p className="tl-note">{timeline.note}</p>
          ) : null}

          <dl className="tl-times">
            <div className="tl-times__row">
              <dt>生成到期</dt>
              {/* 到期时间与计划最后修改时间分别展示。 */}
              <dd data-testid="tl-due-at">{formatDayTimeInZone(timeline.outline_due_at, timeline.display_timezone)}</dd>
            </div>
            <div className="tl-times__row">
              <dt>计划最近变更</dt>
              <dd data-testid="tl-generated-at">
                {timeline.generated_at
                  ? formatDayTimeInZone(timeline.generated_at, timeline.display_timezone)
                  : '未知'}
              </dd>
            </div>
            <div className="tl-times__row">
              <dt>计划版本</dt>
              <dd>
                r{timeline.revision}
                {timeline.plan_state ? ` · 库里状态 ${timeline.plan_state}` : ''}
                {timeline.superseded_segment_count > 0
                  ? ` · 有 ${timeline.superseded_segment_count} 段被新版替代`
                  : ''}
              </dd>
            </div>
          </dl>
        </div>
      </details>

      {hidden ? (
        <p className="tl-private" data-testid="tl-private">
          {hidden}
        </p>
      ) : null}
    </header>
  );
}

function UnplannedPeriods({ timeline }: { timeline: LifeTimelineOut }) {
  const unplanned = timeline.unplanned;
  if (!unplanned) {
    return (
      <section className="tl-unplanned" data-testid="tl-unplanned">
        <h4>未安排的时段</h4>
        <p>未知，当前信息不足以判断。</p>
      </section>
    );
  }
  const timeZone = timeline.display_timezone;
  return (
    <section className="tl-unplanned" data-testid="tl-unplanned">
      <h4>未安排的时段</h4>
      <p>
        计划范围 {formatUnplannedRange(unplanned.scope_start_at, unplanned.scope_end_at, timeZone)}
      </p>
      {unplanned.periods.length ? (
        <ul>
          {unplanned.periods.map((period) => (
            <li key={`${period.start_at}-${period.end_at}`}>
              {formatUnplannedRange(period.start_at, period.end_at, timeZone)}
            </li>
          ))}
        </ul>
      ) : (
        <p>这个范围内已有计划覆盖全部时段。</p>
      )}
    </section>
  );
}

/**
 * 「时间检查」：`clock_only` 的落点。
 *
 * 这些记录只证明"时间到了"，不能证明她做过什么，所以**不进时间线主体**，
 * 也不参与计划执行关系的表达。它们是可展开的一组，保留时间与来源。
 *
 * 之前这里是右列的「事实（有证据的已发生）」全量清单——那正是同一条活动
 * 在页面里出现第二次的地方。清单删掉，只留这一组"仅到时"的检查。
 */
export function TimelineChecks({
  checks,
  hiddenCount,
  timeZone,
}: {
  checks: readonly LifeTimelineFact[];
  hiddenCount: number;
  timeZone: string;
}) {
  if (checks.length === 0 && hiddenCount === 0) return null;
  const empty = checks.length === 0;
  return (
    <details className="tl-checks" data-testid="tl-checks" data-count={checks.length}>
      <summary>
        <span className="tl-checks__title">时间检查</span>
        {empty ? null : <span className="tl-checks__count">{checks.length}</span>}
        <span className="tl-checks__hint">
          {empty ? '还没有仅到时的检查。' : '只证明时间到了，不代表她做过'}
        </span>
      </summary>
      {hiddenCount > 0 ? (
        <p className="tl-checks__hidden">
          另有 {hiddenCount} 条活动因受众裁决被隐藏——不是没有发生。
        </p>
      ) : null}
      {empty ? null : (
        <ul className="tl-checks__list">
          {checks.map((fact) => (
            <li
              key={fact.activity_id}
              className="tl-check"
              data-testid={`tl-check-${fact.activity_id}`}
              data-evidence={fact.evidence_kind}
            >
              <span className="tl-check__time">
                {formatRangeInZone(fact.started_at, fact.ended_at, timeZone) || '时间未知'}
              </span>
              <span className="tl-check__origin">{factOriginLabel(fact.origin)}</span>
              <span className="tl-check__label">{activityName(fact.kind)}</span>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

export function IntentSection({
  intents,
  horizonUnplanned,
  hiddenCount,
}: {
  intents: readonly DayIntentSummary[];
  horizonUnplanned: boolean;
  hiddenCount: number;
}) {
  return (
    <section className="tl-intents" aria-label="未来 7 天的意向" data-testid="tl-intents">
      <h3 className="tl-intents__title">未来 7 天</h3>
      {/*
        这句话必须和意向一起出现：未来日**没有生成大纲**，这里的条目是
        意向/候选，不是那一天的安排。少了这句，列表看上去就像日程表。
      */}
      {horizonUnplanned ? (
        <p className="tl-intents__horizon" data-testid="tl-horizon">
          未来日还未生成大纲／这些只是意向：不是 D+1…D+7 的日程，也没有约定任何具体时段。
        </p>
      ) : null}
      {intents.length === 0 ? (
        <p className="tl-intents__empty">
          {hiddenCount > 0
            ? `有 ${hiddenCount} 条意向因受众裁决被隐藏——不是没有。`
            : '未来 7 天还没有有来源的意向。'}
        </p>
      ) : (
        <ul className="tl-intents__list">
          {intents.map((intent) => (
            <li key={intent.intention_id} className="tl-intent" data-testid={`tl-intent-${intent.intention_id}`}>
              <span className="tl-intent__days">{intentDayRange(intent)}</span>
              <span className="tl-intent__state">{intentStateLabel(intent.state)}</span>
              <span className="tl-intent__source">{intentSourceLabel(intent.source_kind)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * 梦卡片，三态：
 * - `dream=null` → 没有记录（事实）；
 * - `restricted=true` → 有记录，但正文是角色私有（权限）；
 * - 其余 → 能看到标题/正文，且 `origin=simulated` 必须标「模拟梦景」。
 *
 * 前两者**不能合并**：一句"没有梦"会把"你看不到"说成"不存在"。
 */
export function DreamCardView({ dream }: { dream: DreamCard | null | undefined }) {
  const view = dreamView(dream);
  return (
    <section className="tl-dream" aria-label="梦" data-testid="tl-dream" data-dream-view={view}>
      <h3 className="tl-dream__title">梦</h3>
      {view === 'none' ? (
        <p className="tl-dream__none">梦没有记录。</p>
      ) : null}
      {view === 'restricted' && dream ? (
        <>
          <div className="tl-dream__head">
            {/* 来源不影响可见性：模拟梦景这个事实照样要标出来。 */}
            {dream.origin === 'simulated' ? (
              <span className="tl-dream__badge" data-testid="tl-dream-simulated">
                模拟梦景
              </span>
            ) : null}
            <span className="tl-dream__meta">记录存在（状态 {dream.status}）</span>
          </div>
          <p className="tl-dream__restricted" data-testid="tl-dream-restricted">
            梦是角色私有内容，你看不到正文。
          </p>
        </>
      ) : null}
      {view === 'content' && dream ? (
        <>
          <div className="tl-dream__head">
            {dream.origin === 'simulated' ? (
              <span className="tl-dream__badge" data-testid="tl-dream-simulated">
                模拟梦景
              </span>
            ) : null}
            <span className="tl-dream__meta">状态 {dream.status}</span>
          </div>
          {dream.status === 'no_dream' ? (
            <p className="tl-dream__none">这一晚的报告写着：没有梦。</p>
          ) : null}
          {dream.title ? <p className="tl-dream__heading">{dream.title}</p> : null}
          {dream.narrative ? (
            <p className="tl-dream__text">{dream.narrative}</p>
          ) : dream.status !== 'no_dream' ? (
            <p className="tl-dream__none">正文是空的——记录有过，但没留下内容。</p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

/* ---------------------------------------------------------------- 主体 */

export interface LifeTimelineProps {
  timeline: LifeTimelineOut;
  /** 判断"已过去/此刻/未到"的参照时刻（测试可钉住）。 */
  now?: Date;
  /** 默认收起的父段 id。 */
  defaultCollapsed?: readonly string[];
  /** `unavailable` 时的重试入口（页面必须给）。 */
  onRetry?: () => void;
}

/** 时间轴本体（纯展示，不取数）。 */
export function LifeTimeline({ timeline, now, defaultCollapsed, onRetry }: LifeTimelineProps) {
  const at = now ?? new Date();
  const timeZone = timeline.display_timezone;
  // 合并一次，下面所有渲染都从这里取：记录只出现一次，是这一层的责任。
  const merged = mergeDayTimeline(timeline);
  return (
    <section className="tl" data-testid="life-timeline" data-state={timeline.state}>
      <TimelineHeader timeline={timeline} onRetry={onRetry} />
      {timeline.state === 'unavailable' ? (
        /*
         * 读不到时**不画空白的时间轴**：一个空的"计划"区会被读成"她今天没安排"。
         * 这里只留状态说明与重试（头部里），不渲染任何"没有内容"的措辞。
         */
        <p className="tl-unavailable" data-testid="tl-unavailable">
          这一次读取出错，时间轴内容拿不到。这不代表她今天没有安排——请稍后重试。
        </p>
      ) : (
        <>
          <div className="tl-plans">
            <h3 className="tl-plans__title">今日计划</h3>
            {/*
              这句话必须留着：活动落在某个时段旁边只是排版依据，
              它**不表示**计划被执行了。没有这句，挨着放就会被读成"这一段做成了"。
            */}
            <p className="tl-plans__hint">时段表示预计安排；挨着的活动只是按时间排在一起，不代表计划被执行。</p>
            {timeline.segments.length === 0 ? (
              <p className="tl-plans__empty">{segmentEmptyText(timeline)}</p>
            ) : (
              <TimelineTrack
                segments={timeline.segments}
                bySegment={merged.bySegment}
                timeZone={timeZone}
                timeline={timeline}
                now={at}
                defaultCollapsed={defaultCollapsed ?? []}
              />
            )}
            {merged.loose.length > 0 ? (
              <section className="tl-loose" data-testid="tl-loose">
                <h4 className="tl-loose__title">不属于任何时段</h4>
                <ul className="tl-loose__list">
                  {merged.loose.map((record) => (
                    <RecordRow
                      key={record.fact.activity_id}
                      record={record}
                      now={at}
                      timeZone={timeZone}
                      timeline={timeline}
                      loose
                      testIdPrefix="tl-fact-loose"
                    />
                  ))}
                </ul>
              </section>
            ) : null}
            <UnplannedPeriods timeline={timeline} />
          </div>
          <TimelineChecks
            checks={merged.checks}
            hiddenCount={timeline.hidden_fact_count}
            timeZone={timeZone}
          />
          <IntentSection
            intents={timeline.intents}
            horizonUnplanned={timeline.horizon_unplanned}
            hiddenCount={timeline.hidden_intent_count}
          />
          <DreamCardView dream={timeline.dream} />
        </>
      )}
    </section>
  );
}

/* -------------------------------------------------- 手动生成日程（纯函数） */

/** 轮询作业的间隔。1.5 秒：后端一次生成要几秒到十几秒，再密就是打空。 */
export const OUTLINE_POLL_MS = 1500;

/** 轮询上限 60 秒。超过就**不再说"正在生成"**，改成"稍后刷新看看"——不假装成功。 */
export const OUTLINE_POLL_MAX_MS = 60_000;

/** 作业的终态：到了就不必再轮询（`blocked` 也终态：它不会自己往下走）。 */
const OUTLINE_TERMINAL_STATES: ReadonlySet<string> = new Set([
  'ready',
  'degraded',
  'failed',
  'unchanged',
  'blocked',
]);

export function isOutlineTerminal(state: string): boolean {
  return OUTLINE_TERMINAL_STATES.has(state);
}

/** 生成成功（拿到一版计划）。`unchanged` 不算：它什么都没做。 */
export function isOutlineSuccess(state: string): boolean {
  return state === 'ready' || state === 'degraded';
}

/**
 * 按钮该显示「生成今天的日程」还是「重新生成」。
 *
 * - `ready` / `degraded`：库里有这一天的计划 → 再点就是**重排**（`force:true`，
 *   会推倒还没开始的段，所以要走二次确认）。
 * - `empty` / `pending` / `failed`：还没有可用计划 → 普通的「生成」。
 * - 其余（`unavailable` 等，也就是**读不到**）：不给按钮。读不到不等于没有
 *   计划，这种情况下点一下可能白花一次生成额度。
 *
 * `plan_state=null`（没有计划行）按 `empty` 处理：那正是需要生成的场景。
 */
export function scheduleActionFor(
  planState: string | null | undefined,
  state: string | null | undefined,
): 'generate' | 'regenerate' | 'none' {
  if (state === 'unavailable') return 'none';
  if (planState === 'ready' || planState === 'degraded') return 'regenerate';
  if (!planState || planState === 'empty' || planState === 'pending' || planState === 'failed') {
    return 'generate';
  }
  return 'none';
}

/** 不用 `crypto.randomUUID`：它在非安全上下文里不存在，点了会静默失效。 */
export function newRequestId(): string {
  const random = Math.random().toString(36).slice(2, 10);
  return `outline-${Date.now().toString(36)}-${random}`;
}

/**
 * 提交失败 → 人话。
 *
 * 409 用**后端给的 `note`**：人话是后端写的（"今天的生成次数用完了…"），
 * 前端再抄一份就会两边不一致；拿不到才兜底。`reason_code` 一律不显示。
 */
export function submitFailureCopy(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return '点太快了，等几秒再试';
    if (error.status === 409) return error.message || '现在还不能生成这一天的日程';
    if (error.status === 422) return '只能生成今天或明天的日程，刷新页面后再试';
    if (error.status === 404 || error.status === 403) {
      return '没有权限生成日程；只有主人可以触发生成';
    }
  }
  return explainError(error).reason;
}

/** 轮询到的作业状态 → 人话。`reason_code` 永远不进这里。 */
export function jobStateCopy(job: Pick<OutlineJobOut, 'state' | 'note'>): string {
  switch (job.state) {
    case 'queued':
    case 'running':
      // 提交后**只能说"正在生成"**：202 只代表入队，不代表已经排好。
      // 后端给了 note 就用它（"已经排上队了，她正在准备"），不自己另写一套。
      return job.note || '已提交，正在生成…';
    case 'ready':
      return job.note || '这一天的日程已经排好了';
    case 'degraded':
      return job.note || '模型这次没用上，已按规则铺了睡眠和已确认的安排';
    case 'failed':
    case 'blocked':
      return job.note || '这一天的日程没排出来，可以再试一次';
    case 'unchanged':
      return job.note || '今天已经生成过，没有重新生成';
    default:
      return job.note || '等待结果…';
  }
}

/** 按钮上的字。生成中禁用，所以文案本身就是状态。 */
export function outlineButtonLabel(action: 'generate' | 'regenerate', busy: boolean): string {
  if (busy) return '已提交，正在生成…';
  return action === 'regenerate' ? '重新生成' : '生成今天的日程';
}

/** 重排的二次确认文案。推倒"还没开始的段"这件事必须写在按钮旁边。 */
/**
 * 提交的那一刻到 202 回来之前。
 *
 * 文案固定写「已提交，正在生成…」而不分"正在提交/正在生成"两段：中间那几十
 * 毫秒的差别对用户没有意义，换来的是状态行在两次播报之间跳一下。
 */
export const OUTLINE_SUBMITTING_NOTE = '已提交，正在生成…';

export const OUTLINE_FORCE_PROMPT =
  '重新生成会推倒还没开始的计划段，按现在的情况重排一版。已经过去的和已约好的锚点不会改。';

/** 空闲时的说明：空态/无计划行正是要点的场景，这里不能不说话。 */
export function outlineIdleHint(action: 'generate' | 'regenerate'): string {
  return action === 'regenerate'
    ? '这一天的日程已经生成过；重排会推倒还没开始的计划段，所以需要确认。'
    : '这一天还没有生成日程。生成只影响计划，不会声称她做过什么。';
}

/* ------------------------------------------------------ 作业状态机（可测） */

export type OutlineViewPhase =
  | 'idle'
  | 'submitting'
  | 'polling'
  | 'ready'
  | 'degraded'
  | 'unchanged'
  | 'failed'
  | 'timeout'
  | 'error';

export interface OutlineJobView {
  phase: OutlineViewPhase;
  /** 人话。**永远不包含 `reason_code`**。 */
  note: string;
  jobId: string | null;
  /** 提交阶段是网络/5xx 失败：可以用同一个 `request_id` 重试。 */
  retryable?: boolean;
}

export const OUTLINE_IDLE_VIEW: OutlineJobView = { phase: 'idle', note: '', jobId: null };

/** `POST`/`GET` 的响应 → 界面状态。未知状态一律按失败处理，不猜成功。 */
export function outlineViewFromJob(job: OutlineJobOut): OutlineJobView {
  const phase: OutlineViewPhase =
    job.state === 'queued' || job.state === 'running'
      ? 'polling'
      : job.state === 'ready'
        ? 'ready'
        : job.state === 'degraded'
          ? 'degraded'
          : job.state === 'unchanged'
            ? 'unchanged'
            : 'failed';
  return { phase, note: jobStateCopy(job), jobId: job.job_id };
}

/** 提交失败 → 界面状态。不显示原因码，只显示人话。 */
export function outlineViewFromError(error: unknown): OutlineJobView {
  return {
    phase: 'error',
    note: submitFailureCopy(error),
    jobId: null,
    // 网络断了/服务端 5xx：这次点击可能根本没落地，值得用同一个 id 再试。
    retryable: error instanceof ApiError ? error.status === 0 || error.status >= 500 : true,
  };
}

/** 轮询到终态（含 unchanged / failed / blocked）。 */
export function outlineViewTerminal(view: OutlineJobView): boolean {
  return (
    view.phase === 'ready' ||
    view.phase === 'degraded' ||
    view.phase === 'unchanged' ||
    view.phase === 'failed'
  );
}

export interface OutlineJobApi {
  createOutlineJob(body: {
    plan_day: string;
    force: boolean;
    request_id: string;
  }): Promise<OutlineJobOut>;
  outlineJob(jobId: string): Promise<OutlineJobOut>;
}

export interface OutlineSchedulerOptions {
  api: OutlineJobApi;
  onChange: (view: OutlineJobView) => void;
  /** 停下来之后（终态或超时）：调用方据此重读时间轴。 */
  onSettled?: () => void;
  /** 测试可钉住：默认 1500ms / 60s。 */
  pollMs?: number;
  maxMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  makeRequestId?: () => string;
}

export interface OutlineScheduler {
  readonly busy: boolean;
  /** 这次点击的幂等键；提交失败后重试要复用同一个值。 */
  readonly pendingRequestId: string | null;
  submit(planDay: string, force: boolean): Promise<OutlineJobView>;
  /** 用同一个 `request_id` 重发（只有网络/5xx 失败后才需要）。 */
  retry(): Promise<OutlineJobView | null>;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 一次点击 → 提交 → 轮询到终态的完整流程。
 *
 * 两条硬约束：
 * 1. **同一次点击只发一次**：`inFlight` 期间再点返回同一个 Promise，
 *    `request_id` 也只有一个（后端据此幂等）。
 * 2. **超时不假装成功**：60 秒还没到终态就报"还在生成，可以稍后刷新看看"。
 */
export function createOutlineScheduler(options: OutlineSchedulerOptions): OutlineScheduler {
  const pollMs = options.pollMs ?? OUTLINE_POLL_MS;
  const maxMs = options.maxMs ?? OUTLINE_POLL_MAX_MS;
  const sleep = options.sleep ?? defaultSleep;
  const now = options.now ?? (() => Date.now());
  const makeRequestId = options.makeRequestId ?? newRequestId;

  let inFlight: Promise<OutlineJobView> | null = null;
  let pending: { requestId: string; planDay: string; force: boolean } | null = null;

  const emit = (view: OutlineJobView): OutlineJobView => {
    options.onChange(view);
    return view;
  };

  const run = async (planDay: string, force: boolean, requestId: string): Promise<OutlineJobView> => {
    let job: OutlineJobOut;
    try {
      job = await options.api.createOutlineJob({ plan_day: planDay, force, request_id: requestId });
    } catch (error) {
      const view = emit(outlineViewFromError(error));
      // 网络/5xx：这次点击还没落地，保留同一个 id 供重试；确定失败就清掉。
      pending = view.retryable ? { requestId, planDay, force } : null;
      return view;
    }
    // 已经拿到作业（或后端明确说 unchanged）：这次点击的幂等键用完即弃。
    pending = null;
    let view = emit(outlineViewFromJob(job));

    if (!isOutlineTerminal(job.state) && job.job_id) {
      const deadline = now() + maxMs;
      let pollFailed = false;
      while (now() < deadline) {
        await sleep(pollMs);
        try {
          const next = await options.api.outlineJob(job.job_id);
          view = emit(outlineViewFromJob(next));
          if (isOutlineTerminal(next.state)) break;
        } catch (error) {
          // 轮询失败不猜成功：如实报错并停下，让用户自己决定刷新还是重试。
          // 也不许被下面的"还在生成"覆盖掉——超时是超时，出错是出错。
          view = emit(outlineViewFromError(error));
          pollFailed = true;
          break;
        }
      }
      if (!pollFailed && !outlineViewTerminal(view)) {
        view = emit({ phase: 'timeout', note: '还在生成，可以稍后刷新看看', jobId: job.job_id });
      }
    }

    options.onSettled?.();
    return view;
  };

  const start = (planDay: string, force: boolean, requestId: string) => {
    if (inFlight) return inFlight;
    inFlight = run(planDay, force, requestId).finally(() => {
      inFlight = null;
    });
    return inFlight;
  };

  return {
    get busy() {
      return inFlight !== null;
    },
    get pendingRequestId() {
      return pending?.requestId ?? null;
    },
    submit(planDay: string, force: boolean) {
      // 双重点击：第二次直接复用这一次的 Promise 与 request_id。
      if (inFlight) return inFlight;
      const requestId = pending?.requestId ?? makeRequestId();
      return start(planDay, force, requestId);
    },
    retry() {
      if (!pending) return Promise.resolve(null);
      return start(pending.planDay, pending.force, pending.requestId);
    },
  };
}

/* ---------------------------------------------------------------- 子组件 */

export interface GenerateScheduleButtonProps {
  /** 目标计划日（角色计划日），直接取自时间轴——**不按浏览器时区算**。 */
  planDay: string;
  action: 'generate' | 'regenerate';
  /** 已提交、正在生成：按钮禁用并显示状态。 */
  busy: boolean;
  /** 状态行文案（人话，来自作业 `note` 或失败翻译）；没有就不渲染那一行。 */
  status: string | null;
  /** 状态行是不是失败（决定配色；`aria-live` 两种都播报）。 */
  statusTone: 'info' | 'error';
  onGenerate: () => void;
  /** 重排前的确认文案（内联确认条里那句话）。 */
  forcePrompt: string;
}

/**
 * 「生成今天的日程」/「重新生成」。
 *
 * 二次确认**不用** `window.confirm`：它会被浏览器拦、读屏也读不到正文。
 * 用内联确认条，把"会推倒还没开始的段"这句话直接摆在按钮旁边。
 */
export function GenerateScheduleButton({
  planDay,
  action,
  busy,
  status,
  statusTone,
  onGenerate,
  forcePrompt,
}: GenerateScheduleButtonProps) {
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    // 状态一变（比如已经有作业在跑）就收起确认条，避免两次点击叠在一起。
    if (busy) setConfirming(false);
  }, [busy]);

  const needsConfirm = action === 'regenerate' && !busy;

  return (
    <div className="tl-gen" data-testid="tl-generate">
      <div className="tl-gen__row">
        <button
          type="button"
          className="button button--small tl-gen__button"
          data-testid="tl-generate-button"
          data-action={action}
          data-target-day={planDay}
          disabled={busy}
          aria-busy={busy}
          onClick={() => {
            if (needsConfirm) {
              setConfirming(true);
              return;
            }
            onGenerate();
          }}
        >
          {busy ? <span className="spinner" aria-hidden="true" /> : <RefreshIcon />}
          {outlineButtonLabel(action, busy)}
        </button>
        <span className="tl-gen__day" data-testid="tl-generate-day">
          {action === 'regenerate' ? '按现在的情况重排' : '目标日（她的计划日）'}：
          <strong>{planDay}</strong>
        </span>
      </div>

      {confirming && !busy ? (
        <div className="tl-gen__confirm" data-testid="tl-generate-confirm" role="group" aria-label="确认重新生成">
          <p className="tl-gen__confirm-text">{forcePrompt}</p>
          <div className="tl-gen__confirm-actions">
            <button
              type="button"
              className="button button--small"
              data-testid="tl-generate-cancel"
              onClick={() => setConfirming(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="button button--small button--primary"
              data-testid="tl-generate-confirm-ok"
              onClick={() => {
                setConfirming(false);
                onGenerate();
              }}
            >
              确认重新生成
            </button>
          </div>
        </div>
      ) : null}

      {/* 播报用 polite：生成进度不该打断读屏正在读的内容；没有话就不占位。 */}
      {status ? (
        <p
          className={`tl-gen__status tl-gen__status--${statusTone}`}
          data-testid="tl-generate-status"
          data-tone={statusTone}
          role="status"
          aria-live="polite"
        >
          {status}
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------ 取数外壳 */

/** 取数外壳依赖的接口（测试可换成假实现；生产就是 `api`）。 */
export interface TimelinePanelApi extends OutlineJobApi {
  lifeTimeline(): Promise<LifeTimelineOut>;
}

export interface LifeTimelinePanelProps {
  title?: string;
  /** 默认走真实 `api`；测试注入假实现。 */
  client?: TimelinePanelApi;
  /** 首屏已读取的时间轴；生成终态后仍由此面板重读。 */
  timeline?: LifeTimelineOut | null;
  onTimelineChange?: (timeline: LifeTimelineOut) => void;
  pollMs?: number;
  maxMs?: number;
  sleep?: (ms: number) => Promise<void>;
  makeRequestId?: () => string;
}

/**
 * 页面上真正挂的那一块：自己取数、自己重试、自己管「生成今天的日程」。
 *
 * 单独取数（不塞进 `LifeNowPage` 那个 `Promise.all`）是为了让这一块失败时
 * 只坏这一块——原来的三块状态照常显示，反之亦然。
 *
 * 生成按钮也放在这里：它要用的是**时间轴的 `plan_day`**（角色计划日），
 * 只有这一块手里有那个值。按浏览器时区算"今天"在跨日附近会落到另一个
 * 日期，后端直接 422。
 */
export function LifeTimelinePanel({
  title = '今天的时间轴',
  client = api,
  timeline: initialTimeline = null,
  onTimelineChange,
  pollMs,
  maxMs,
  sleep,
  makeRequestId,
}: LifeTimelinePanelProps) {
  const [timeline, setTimeline] = useState<LifeTimelineOut | null>(initialTimeline);
  const [loading, setLoading] = useState(initialTimeline === null);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [view, setView] = useState<OutlineJobView>(OUTLINE_IDLE_VIEW);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await client.lifeTimeline();
      setTimeline(next);
      onTimelineChange?.(next);
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setLoading(false);
    }
  }, [client, onTimelineChange]);

  useEffect(() => {
    if (initialTimeline === null) void load();
  }, [initialTimeline, load]);

  useEffect(() => {
    if (initialTimeline !== null) {
      setTimeline(initialTimeline);
      setLoading(false);
    }
  }, [initialTimeline]);

  // 调度器跟着这一块的生命周期走：卸载后不再往已消失的组件里写状态。
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const loadRef = useRef(load);
  loadRef.current = load;

  const scheduler = useMemo(
    () =>
      createOutlineScheduler({
        api: client,
        onChange: (next) => {
          if (mounted.current) setView(next);
        },
        onSettled: () => {
          // 终态后重读时间轴：计划变了，段、版本号、状态都要跟着变。
          // 卸载后不再取数：轮询可能比组件活得久（最长 60 秒）。
          if (mounted.current) void loadRef.current();
        },
        ...(pollMs === undefined ? {} : { pollMs }),
        ...(maxMs === undefined ? {} : { maxMs }),
        ...(sleep === undefined ? {} : { sleep }),
        ...(makeRequestId === undefined ? {} : { makeRequestId }),
      }),
    [client, pollMs, maxMs, sleep, makeRequestId],
  );

  const action = scheduleActionFor(timeline?.plan_state, timeline?.state);
  const idleHint =
    action === 'none'
      ? '这一次读不到时间轴，暂时不能生成；读取成功后再试。'
      : outlineIdleHint(action);
  const busy = timeline !== null && (view.phase === 'submitting' || view.phase === 'polling');

  const start = (force: boolean) => {
    if (!timeline) return;
    setView({ phase: 'submitting', note: OUTLINE_SUBMITTING_NOTE, jobId: null });
    void scheduler.submit(timeline.plan_day, force);
  };

  // 状态行只报**这次点击**的进展；空闲时说明按钮是干什么的（空态正是要点的场景）。
  const status = view.phase === 'idle' ? idleHint : view.note;
  const statusTone: 'info' | 'error' =
    view.phase === 'error' || view.phase === 'failed' || view.phase === 'timeout'
      ? 'error'
      : 'info';

  return (
    <div className="tl-panel" data-testid="life-timeline-panel">
      <h2 className="life-section tl-panel__title">{title}</h2>
      {loading && !timeline ? <p className="page__hint">正在读取时间轴…</p> : null}
      {error ? (
        <div className="banner banner--error" role="alert">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      {/*
        按钮挂在**读到的计划状态**上：空态/无计划行正是要点它的场景，
        所以拿到时间轴就渲染，不等段列表非空。
      */}
      {timeline && action !== 'none' ? (
        <GenerateScheduleButton
          planDay={timeline.plan_day}
          action={action}
          busy={busy}
          status={status}
          statusTone={statusTone}
          forcePrompt={OUTLINE_FORCE_PROMPT}
          onGenerate={() => start(action === 'regenerate')}
        />
      ) : null}

      {timeline ? (
        <LifeTimeline timeline={timeline} onRetry={() => void load()} />
      ) : null}
    </div>
  );
}
