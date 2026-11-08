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
export const SEGMENT_MIN_HEIGHT = 36;
export const SEGMENT_MAX_HEIGHT = 180;
export const SEGMENT_PX_PER_MINUTE = 0.6;

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

/**
 * 事实的措辞。**证据不足就不能说"做过"**：
 * `clock_only` 只证明时间到了，`scene_valid_until=null` 表示此刻未知。
 */
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
  simulated: '模拟',
  user_statement: '你说的',
  external_observation: '观察到的',
  verified_source: '查到的',
  tool_receipt: '有回执',
  model_inference: '推测',
};

export function factOriginLabel(origin: string): string {
  return FACT_ORIGIN_LABEL[origin] ?? origin;
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
    hint: '下面是这一天的计划。计划是打算，不是已经发生的事。',
  },
  degraded: {
    label: '来自规则兜底',
    hint: '模型不可用或输出被拒收，下面的段由规则生成——内容和模型版可能不一样。',
  },
  failed: {
    label: '生成失败',
    hint: '最近一次生成失败了。原因看下面的原因码，不能说成「今天没安排」。',
  },
  pending: {
    label: '还没生成',
    hint: '计划行已经建了，大纲还没到生成时间（见「生成到期」）。',
  },
  empty: {
    label: '这一天还没有安排',
    hint: '这一天没有计划行——真的没有，不是读取失败。',
  },
  unavailable: {
    label: '暂时无法读取',
    hint: '这一次读取出错了。它不等于「今天没安排」，可以重试。',
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

function SegmentFacts({ facts, timeZone }: { facts: LifeTimelineFact[]; timeZone: string }) {
  if (facts.length === 0) return null;
  return (
    <ul className="tl-seg__facts">
      {facts.map((fact) => (
        <li
          key={fact.activity_id}
          className="tl-fact tl-fact--solid"
          data-testid={`tl-fact-${fact.activity_id}`}
          data-evidence={fact.evidence_kind}
        >
          {/* 实心 = 有证据的已发生；与虚线/浅色的计划段在类名与语义上都分开。 */}
          <span className="tl-fact__label">{evidenceLabel(fact.evidence_kind)}</span>
          <span className="tl-fact__time">
            {formatRangeInZone(fact.started_at, fact.ended_at, timeZone) || '时间未知'}
          </span>
          <span className="tl-fact__scene">
            {fact.scene_valid_until
              ? `情景有效至 ${formatInZone(fact.scene_valid_until, timeZone)}`
              : '此刻情景状态未知'}
          </span>
        </li>
      ))}
    </ul>
  );
}

interface SegmentRowProps {
  node: SegmentNode;
  depth: number;
  facts: readonly LifeTimelineFact[];
  now: Date;
  timeZone: string;
  collapsed: ReadonlySet<string>;
  onToggle: (segmentId: string) => void;
}

function SegmentRow({ node, depth, facts, now, timeZone, collapsed, onToggle }: SegmentRowProps) {
  const segment = node.segment;
  const phase = segmentPhase(segment, now);
  const ownFacts = factsForSegment(
    facts,
    segment,
    node.children.map((child) => child.segment),
  );
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
        <SegmentFacts facts={ownFacts} timeZone={timeZone} />
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
              facts={facts}
              now={now}
              timeZone={timeZone}
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
  facts: readonly LifeTimelineFact[];
  timeZone: string;
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
  facts,
  timeZone,
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
          facts={facts}
          now={now}
          timeZone={timeZone}
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
  return (
    <header className="tl-head">
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
      {gap ? (
        <p className="tl-days__note" data-testid="tl-tz-note">
          {gap}
        </p>
      ) : null}

      <p className="tl-state" data-testid="tl-state" data-state={timeline.state}>
        <strong>{copy.label}</strong>
        <span className="tl-state__hint">{copy.hint}</span>
        {code ? (
          <code className="tl-state__code" data-testid="tl-reason-code">
            原因码：{code}
          </code>
        ) : null}
        {onRetry && timeline.state === 'unavailable' ? (
          <button type="button" className="button button--small" onClick={onRetry}>
            <RefreshIcon /> 重试
          </button>
        ) : null}
      </p>
      {timeline.note && timeline.note !== copy.hint ? (
        <p className="tl-note">{timeline.note}</p>
      ) : null}

      <dl className="tl-times">
        <div className="tl-times__row">
          <dt>生成到期</dt>
          {/* 到期时刻与实际提交时刻**分开**：一个说"该什么时候生成"，
              一个说"什么时候真的生成了"。合成一句"更新于"就会两头都不准。 */}
          <dd data-testid="tl-due-at">{formatDayTimeInZone(timeline.outline_due_at, timeline.display_timezone)}</dd>
        </div>
        <div className="tl-times__row">
          <dt>实际提交</dt>
          <dd data-testid="tl-generated-at">
            {timeline.generated_at
              ? formatDayTimeInZone(timeline.generated_at, timeline.display_timezone)
              : '还没有提交过'}
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

      {hidden ? (
        <p className="tl-private" data-testid="tl-private">
          {hidden}
        </p>
      ) : null}
    </header>
  );
}

export function TimelineFactsLane({
  facts,
  hiddenCount,
  timeZone,
}: {
  facts: readonly LifeTimelineFact[];
  hiddenCount: number;
  timeZone: string;
}) {
  return (
    <section className="tl-facts" aria-label="事实层">
      <h3 className="tl-facts__title">事实（有证据的已发生）</h3>
      {facts.length === 0 ? (
        <p className="tl-facts__empty">
          {hiddenCount > 0
            ? `有 ${hiddenCount} 条活动因受众裁决被隐藏——不是没有发生。`
            : '还没有带证据的活动记录。'}
        </p>
      ) : (
        <ul className="tl-facts__list">
          {facts.map((fact) => (
            <li
              key={fact.activity_id}
              className="tl-fact tl-fact--solid"
              data-testid={`tl-fact-lane-${fact.activity_id}`}
              data-evidence={fact.evidence_kind}
            >
              <span className="tl-fact__label">{evidenceLabel(fact.evidence_kind)}</span>
              <span className="tl-fact__time">
                {formatRangeInZone(fact.started_at, fact.ended_at, timeZone) || '时间未知'}
              </span>
              <span className="tl-fact__kind">{factOriginLabel(fact.origin)}</span>
              <span className="tl-fact__scene">
                {fact.scene_valid_until
                  ? `情景有效至 ${formatDayTimeInZone(fact.scene_valid_until, timeZone)}`
                  : '此刻情景状态未知'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
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
          <div className="tl-body">
            <div className="tl-plans">
              <h3 className="tl-plans__title">计划（打算／可能，不是已经发生）</h3>
              {timeline.segments.length === 0 ? (
                <p className="tl-plans__empty">{segmentEmptyText(timeline)}</p>
              ) : (
                <TimelineTrack
                  segments={timeline.segments}
                  facts={timeline.facts}
                  timeZone={timeZone}
                  now={at}
                  defaultCollapsed={defaultCollapsed ?? []}
                />
              )}
            </div>
            <TimelineFactsLane
              facts={timeline.facts}
              hiddenCount={timeline.hidden_fact_count}
              timeZone={timeZone}
            />
          </div>
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
  /** 轮询参数留给测试钉住时间（生产用默认 1.5s / 60s）。 */
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
  pollMs,
  maxMs,
  sleep,
  makeRequestId,
}: LifeTimelinePanelProps) {
  const [timeline, setTimeline] = useState<LifeTimelineOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [view, setView] = useState<OutlineJobView>(OUTLINE_IDLE_VIEW);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTimeline(await client.lifeTimeline());
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

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
