/**
 * 记忆库星图视图：把 `/v1/memory` 返回的 `MemoryRecord[]` 投影到「时间 × 类型」星图，
 * 并用十二月令环连接同月记录、用事件显微镜拆解单条记忆的来龙去脉。
 *
 * 时间口径是这个文件最重要的一条边界：
 * - 已核验的 `occurred_at` 是事情发生的时间。
 * - `created_at` 只说明「这条记忆被记下」；
 * - 导入条目缺 `occurred_at` 时**不拿记下时间顶替**，放进「发生日期未知」列。
 * 星图横轴按实际时间间隔连续排布，不改成等宽月份表格。
 */
import { useMemo, useRef, useState } from 'react';
import type { MemoryRecord } from '../types';
import '../memory-star-map.css';

export interface StarBeat {
  /** 这一幕在讲时间、正文、依据还是演进。 */
  kind: 'time' | 'content' | 'evidence' | 'evolution';
  time: string;
  tag: string;
  title: string;
  detail: string;
  inner: string;
}

export type StarLaneId = 'episode' | 'assertion' | 'procedure' | 'other';
export type StarDateKind = '发生' | '记下' | '未知';
export type StarCausalKind = 'correction' | 'source';
export type StarMonthArcKind = 'same-year' | 'cross-year';

export interface StarNode {
  row: MemoryRecord;
  lane: StarLaneId;
  xPx: number;
  yPx: number;
  month: number | null;
  year: number | null;
  /** 用来排横轴的时间；未知发生日期的导入条目为 null。 */
  timestamp: number | null;
  /** 本地活动或原始导入记录提供的发生时间。 */
  occurredAt: string | null;
  /** 记下时间原值，只来自 `created_at`。 */
  recordedAt: string | null;
  dateKind: StarDateKind;
  beats: StarBeat[];
}

export interface StarChasm {
  xPx: number;
  widthPx: number;
  days: number;
  targetId: string;
  label: string;
}

export interface StarCausalEdge {
  from: string;
  to: string;
  reason: '纠正链' | '同源事件';
  kind: StarCausalKind;
  /** 这条连线凭什么成立：纠正关系，或共享的具体来源事件 id。 */
  evidence: string;
}

export interface StarLunarArc {
  month: number;
  from: string;
  to: string;
  /** 用于无障碍与详情跳转的完整说明。 */
  label: string;
  kind: StarMonthArcKind;
  fromYear: number;
  toYear: number;
}

export type StarAffinityLevel = 'strong' | 'medium' | 'weak';

export interface StarAffinityLink {
  id: string;
  level: StarAffinityLevel;
  /** 为什么算相关：同源事件 / 纠正链 / 同月。 */
  reason: string;
  /** 连线依据，详情和镜片里直接显示。 */
  evidence: string;
  kind: StarCausalKind | StarMonthArcKind;
}

export interface StarAffinity {
  selectedId: string;
  links: StarAffinityLink[];
  counts: { strong: number; medium: number; weak: number };
}

export interface StarYearTick {
  year: number;
  xPx: number;
}

export interface StarChartModel {
  canvasWidth: number;
  totalHeight: number;
  nodes: StarNode[];
  nodeMap: Record<string, StarNode>;
  lanes: Array<{ id: StarLaneId; label: string; count: number; topPx: number; heightPx: number }>;
  yearTicks: StarYearTick[];
  chasms: StarChasm[];
  causalEdges: StarCausalEdge[];
  lunarArcs: StarLunarArc[];
  monthCounts: Record<number, number>;
  /** 同一年里出现两次以上的月份。 */
  sameYearMonths: number[];
  /** 出现在两个以上年份的月份；只有这些月份会显示「跨年」弧线。 */
  crossYearMonths: number[];
  /** 每个月出现过的年份，月份环用它说明「同年」还是「跨年」。 */
  monthYears: Record<number, number[]>;
  /** 发生日期未知的导入条目数量。 */
  unknownDateCount: number;
  /** 有日期记录的横轴起止；没有日期时为 null。 */
  timeline: { start: number; end: number } | null;
}

export const STAR_CARD_W = 160;
export const STAR_CARD_H = 86;

const LANE_LABELS: Record<StarLaneId, string> = {
  episode: '经历与感受',
  assertion: '事实与知识',
  procedure: '做法',
  other: '计划与素材',
};

const DAY_MS = 86_400_000;

export const MONTH_SEASONS: Record<number, string> = {
  1: '隆冬一月',
  2: '早春二月',
  3: '三月',
  4: '春深四月',
  5: '初夏五月',
  6: '盛夏六月',
  7: '七月',
  8: '夏末八月',
  9: '换季九月',
  10: '晚秋十月',
  11: '霜降十一月',
  12: '冬至十二月',
};

function parseRecordDate(raw: string | null | undefined): Date | null {
  if (!raw) return null;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

function formatShortDate(iso: string | null | undefined): string {
  if (!iso) return '时间未知';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '时间未知';
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function formatClock(iso: string | null | undefined): string {
  if (!iso) return '时刻未知';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '时刻未知';
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function sessionScope(row: MemoryRecord): '群聊' | '私聊' | null {
  const session = row.import_source?.session_id ?? '';
  if (row.visibility === 'group_scope' || /groupmessage/i.test(session)) return '群聊';
  if (row.visibility === 'window_private' || /friendmessage|privatemessage/i.test(session)) return '私聊';
  return null;
}

function laneOfKind(kind: string): StarLaneId {
  if (kind === 'episode' || kind === 'impression') return 'episode';
  if (kind === 'assertion' || kind === 'knowledge') return 'assertion';
  return kind === 'procedure' ? 'procedure' : 'other';
}

function splitSentences(text: string): string[] {
  const parts = text
    .split(/(?<=[。！？；\n])/u)
    .map((s) => s.trim())
    .filter(Boolean);
  return parts.length > 0 ? parts : [text];
}

export function memoryOccurredAt(row: MemoryRecord): string | null {
  return row.occurred_at ?? row.import_source?.occurred_at ?? null;
}

function dateStamp(row: MemoryRecord): { occurred: Date | null; recorded: Date | null; kind: StarDateKind; timestamp: number | null } {
  const occurred = parseRecordDate(memoryOccurredAt(row));
  const recorded = parseRecordDate(row.created_at);
  if (occurred) {
    return { occurred, recorded, kind: '发生', timestamp: occurred.getTime() };
  }
  // 导入条目没有发生时间时，记下时间不能代替发生时间参与横轴排序。
  if (row.import_source) {
    return { occurred: null, recorded, kind: '未知', timestamp: null };
  }
  if (recorded) {
    return { occurred: null, recorded, kind: '记下', timestamp: recorded.getTime() };
  }
  return { occurred: null, recorded: null, kind: '未知', timestamp: null };
}

export function buildMemoryMicroBeats(
  row: MemoryRecord,
  kindText: (kind: string) => string,
  originText: (origin: string) => string,
  visibilityText: (visibility: string) => string,
): StarBeat[] {
  const beats: StarBeat[] = [];
  const occurredIso = memoryOccurredAt(row);
  const occurred = parseRecordDate(occurredIso);
  const recordedIso = row.created_at ?? row.import_source?.created_at ?? null;
  const recorded = parseRecordDate(recordedIso);
  const imported = Boolean(row.import_source);

  if (occurred) {
    beats.push({
      kind: 'time',
      time: occurredIso ? formatClock(occurredIso) : '发生时刻',
      tag: '发生 · 记下',
      title: recorded ? '事情发生在前，记忆在后' : '事情发生的时间',
      detail: recorded
        ? `这条记忆描述的事发生在 ${formatShortDate(occurredIso)}；它于 ${formatShortDate(recordedIso)} 被记下。`
        : `这条记忆描述的事发生在 ${formatShortDate(occurredIso)}；记下时间没有单独留下。`,
      inner: '发生时间和记下时间是两个字段：星图把这条放在发生时间上，而不是导入或写入的时间上。',
    });
  } else if (imported) {
    beats.push({
      kind: 'time',
      time: recordedIso ? `记于 ${formatClock(recordedIso)}` : '时间未知',
      tag: '发生未知 · 记下',
      title: '只有记下时间，没有发生时间',
      detail: recorded
        ? `原记录没有发生时间；这条记忆于 ${formatShortDate(recordedIso)} 被记下。它因此停在星图的「发生日期未知」列。`
        : '原记录没有发生时间，也没有可用的记下时间。',
      inner: '缺失的发生时间不会被记下时间顶替，否则会把一件更早的事误读成最近发生。',
    });
  } else {
    beats.push({
      kind: 'time',
      time: recordedIso ? `记于 ${formatClock(recordedIso)}` : '时间未知',
      tag: '记下',
      title: '这条记忆被记下的时刻',
      detail: recorded
        ? `这条记忆于 ${formatShortDate(recordedIso)} 被记下，可见范围为「${visibilityText(row.visibility)}」。`
        : '这条记忆没有留下发生时间或记下时间。',
      inner: '普通记忆只有记下时间；它不自动等于事情发生的时间。',
    });
  }

  const sentences = splitSentences(row.text);
  beats.push({
    kind: 'content',
    time: '核心内容',
    tag: row.confidence === null ? '正文陈述' : `置信度 ${Math.round(Math.max(0, Math.min(1, row.confidence)) * 100)}%`,
    title: sentences[0] && sentences[0].length <= 28 ? sentences[0] : '她记下的那句话',
    detail: row.text,
    inner: sentences.length > 1
      ? `整条陈述包含 ${sentences.length} 个层次，构成这条记忆的主体语义。`
      : '简短直接的一条长期记忆。',
  });

  const evidence: string[] = [];
  if (row.source_event_ids.length > 0) {
    evidence.push(`来源事件：${row.source_event_ids.join('、')}。这些编号能把它锚定回具体的对话事件。`);
  }
  if (row.import_source) {
    evidence.push(`导入记录 ${row.import_source.record_id}，原会话 ${row.import_source.session_id}（${row.import_source.owner_qq} → ${row.import_source.bot_qq}，类型 ${row.import_source.memory_type}）。`);
  }
  if (row.supersedes) {
    evidence.push(`它纠正了更早的条目 ${row.supersedes}。`);
  }
  if (row.superseded_by) {
    evidence.push(`它已被新条目 ${row.superseded_by} 替代。`);
  }
  if (row.retracted) {
    evidence.push(`已撤回${row.retract_reason ? `（理由：${row.retract_reason}）` : ''}。`);
  }
  if (row.archived_at) {
    evidence.push(`归档于 ${formatShortDate(row.archived_at)}。`);
  }
  beats.push({
    kind: 'evidence',
    time: '依据',
    tag: row.origin === 'model_inference' ? '模型推断' : `${originText(row.origin)} · ${kindText(row.kind)}`,
    title: evidence.length ? '这条记忆凭什么成立' : '没有记录可核对的来源',
    detail: evidence.length ? evidence.join('\n') : `这条记忆没有来源事件或导入记录；可核对的是它于 ${formatShortDate(recordedIso)} 被记下，以及可见范围「${visibilityText(row.visibility)}」。`,
    inner: row.origin === 'model_inference'
      ? '这是模型推断，不是本人亲口陈述；阅读时要与事实区分。'
      : '来源事件、纠正链与导入记录是三种不同强度的依据；星图只按具体来源事件或纠正关系连线。',
  });

  if (row.supersedes || row.superseded_by || row.retracted || row.archived_at) {
    const evolution: string[] = [];
    if (row.supersedes) evolution.push(`纠正了更早的条目 ${row.supersedes}`);
    if (row.superseded_by) evolution.push(`被新条目 ${row.superseded_by} 替代`);
    if (row.retracted) evolution.push(`已撤回${row.retract_reason ? `（${row.retract_reason}）` : ''}`);
    if (row.archived_at) evolution.push(`归档于 ${formatShortDate(row.archived_at)}`);
    beats.push({
      kind: 'evolution',
      time: '前后经过',
      tag: '变更轨迹',
      title: row.superseded_by ? '这条说法后来被改写了' : row.retracted ? '已从活跃召回中撤回' : '处于归档或接续状态',
      detail: `${evolution.join('；')}。`,
      inner: '保留变更记录，才能看清一条记忆是怎样被修正的。',
    });
  }

  return beats;
}

export function buildStarChartModel(
  rows: MemoryRecord[],
  kindText: (kind: string) => string,
  originText: (origin: string) => string,
  visibilityText: (visibility: string) => string,
): StarChartModel {
  const dated = rows
    .map((row) => {
      const stamp = dateStamp(row);
      return { row, ...stamp };
    })
    .sort((a, b) => (a.timestamp ?? Number.MAX_SAFE_INTEGER) - (b.timestamp ?? Number.MAX_SAFE_INTEGER));

  const withTimestamp = dated.filter((d) => d.timestamp !== null);
  const unknown = dated.filter((d) => d.timestamp === null);
  const validTs = withTimestamp.map((d) => d.timestamp as number);
  const minTs = validTs.length > 0 ? Math.min(...validTs) : 0;
  const maxTs = validTs.length > 0 ? Math.max(...validTs) : 1;
  const span = Math.max(1, maxTs - minTs);
  const unknownColumn = unknown.length ? STAR_CARD_W + 32 : 0;
  const spanDays = span / 86_400_000;
  const spanWidth = spanDays > 120 ? Math.min(1800, 760 + Math.round(spanDays * 1.35)) : 760;
  const countWidth = rows.length * 64;
  const canvasWidth = Math.min(2200, Math.max(spanWidth, countWidth) + unknownColumn);
  const xStart = 96;
  const rightPad = STAR_CARD_W + 18;
  const xEnd = canvasWidth - rightPad - unknownColumn;
  const unknownX = canvasWidth - rightPad;
  const xRange = Math.max(240, xEnd - xStart);

  const laneIds: StarLaneId[] = ['episode', 'assertion', 'procedure'];
  if (rows.some((r) => laneOfKind(r.kind) === 'other')) laneIds.push('other');

  const laneSlotEnds: Record<StarLaneId, number[]> = {
    episode: [],
    assertion: [],
    procedure: [],
    other: [],
  };
  const monthCounts: Record<number, number> = {};
  const monthYears: Record<number, Set<number>> = {};
  const preliminary: Array<{
    row: MemoryRecord;
    lane: StarLaneId;
    xPx: number;
    slot: number;
    month: number | null;
    year: number | null;
    timestamp: number | null;
    occurredAt: string | null;
    recordedAt: string | null;
    dateKind: StarDateKind;
  }> = [];

  const place = (item: typeof dated[number]) => {
    const lane = laneOfKind(item.row.kind);
    const timePosition = item.timestamp === null
      ? unknownX
      : minTs === maxTs
        ? xStart + xRange * 0.5
        : xStart + ((item.timestamp - minTs) / span) * xRange;
    const xPx = item.timestamp === null
      ? unknownX
      : Math.round(Math.max(xStart, Math.min(xEnd, timePosition)));

    // 节点按横坐标递增；每个槽只和槽内上一张卡片比较，保证卡片不重叠。
    const slotEnds = laneSlotEnds[lane];
    const availableSlot = slotEnds.findIndex((end) => xPx - end >= STAR_CARD_W + 12);
    const assignedSlot = availableSlot === -1 ? slotEnds.length : availableSlot;
    slotEnds[assignedSlot] = xPx;

    // 发生日期未知的导入条目不属于任何月份；不能用记下时间把它算进月令环。
    const month = item.timestamp === null || !item.recorded
      ? null
      : item.occurred
        ? item.occurred.getMonth() + 1
        : item.recorded.getMonth() + 1;
    const year = item.timestamp === null || !item.recorded
      ? null
      : item.occurred
        ? item.occurred.getFullYear()
        : item.recorded.getFullYear();
    if (month !== null) {
      monthCounts[month] = (monthCounts[month] ?? 0) + 1;
      if (year !== null) {
        monthYears[month] = monthYears[month] ?? new Set<number>();
        monthYears[month].add(year);
      }
    }

    preliminary.push({
      row: item.row,
      lane,
      xPx,
      slot: assignedSlot,
      month,
      year,
      timestamp: item.timestamp,
      occurredAt: memoryOccurredAt(item.row),
      recordedAt: item.row.created_at ?? null,
      dateKind: item.kind,
    });
  };

  withTimestamp.forEach((item) => place(item));
  unknown.forEach((item) => place(item));

  const topHeader = 38;
  const dense = rows.length > 28;
  const rowPitch = STAR_CARD_H + (dense ? 8 : 14);
  let cursorY = topHeader;
  const lanes = laneIds.map((id) => {
    const slotCount = laneSlotEnds[id].length;
    const heightPx = slotCount ? 28 + slotCount * rowPitch : 64;
    const laneObj = {
      id,
      label: LANE_LABELS[id],
      count: rows.filter((r) => laneOfKind(r.kind) === id).length,
      topPx: cursorY,
      heightPx,
    };
    cursorY += heightPx;
    return laneObj;
  });
  const totalHeight = cursorY;
  const laneTopMap = new Map(lanes.map((l) => [l.id, l.topPx]));

  const nodes: StarNode[] = preliminary.map((item) => ({
    row: item.row,
    lane: item.lane,
    xPx: item.xPx,
    yPx: (laneTopMap.get(item.lane) ?? topHeader) + 14 + item.slot * rowPitch,
    month: item.month,
    year: item.year,
    timestamp: item.timestamp,
    occurredAt: item.occurredAt,
    recordedAt: item.recordedAt,
    dateKind: item.dateKind,
    beats: buildMemoryMicroBeats(item.row, kindText, originText, visibilityText),
  }));

  const nodeMap: Record<string, StarNode> = Object.fromEntries(nodes.map((node) => [node.row.memory_id, node]));

  const chasms: StarChasm[] = [];
  for (let i = 0; i < withTimestamp.length - 1; i += 1) {
    const cur = withTimestamp[i];
    const next = withTimestamp[i + 1];
    if (!cur || !next || cur.timestamp === null || next.timestamp === null) continue;
    const diffDays = Math.round((next.timestamp - cur.timestamp) / DAY_MS);
    if (diffDays < 45) continue;
    const n1 = nodeMap[cur.row.memory_id];
    const n2 = nodeMap[next.row.memory_id];
    if (!n1 || !n2) continue;
    const midX = (n1.xPx + STAR_CARD_W * 0.5 + n2.xPx + STAR_CARD_W * 0.5) / 2;
    chasms.push({
      xPx: Math.round(midX - 15),
      widthPx: 30,
      days: diffDays,
      targetId: next.row.memory_id,
      label: `相隔 ${diffDays} 天`,
    });
  }
  const visibleChasms = chasms
    .sort((a, b) => b.days - a.days || a.xPx - b.xPx)
    .slice(0, 3)
    .sort((a, b) => a.xPx - b.xPx);

  const yearSet = Array.from(new Set(nodes.map((n) => n.year).filter((y): y is number => y !== null))).sort((a, b) => a - b);
  const yearTicks: StarYearTick[] = [];
  for (const year of yearSet) {
    const yrNodes = nodes.filter((n) => n.year === year);
    const avgX = yrNodes.reduce((acc, cur) => acc + cur.xPx + STAR_CARD_W * 0.5, 0) / Math.max(1, yrNodes.length);
    let xPx = Math.round(Math.max(xStart, Math.min(xEnd + STAR_CARD_W * 0.5, avgX)));
    const previous = yearTicks.at(-1);
    if (previous && xPx - previous.xPx < 42) xPx = previous.xPx + 42;
    yearTicks.push({ year, xPx: Math.min(xPx, canvasWidth - 26) });
  }

  const edgeSeen = new Set<string>();
  const causalEdges: StarCausalEdge[] = [];
  const pushEdge = (a: string, b: string, reason: StarCausalEdge['reason'], kind: StarCausalKind, evidence: string) => {
    if (a === b || !Object.hasOwn(nodeMap, a) || !Object.hasOwn(nodeMap, b)) return;
    const key = [a, b].sort().join('::');
    if (edgeSeen.has(key)) return;
    edgeSeen.add(key);
    causalEdges.push({ from: a, to: b, reason, kind, evidence });
  };

  for (const node of nodes) {
    if (node.row.superseded_by) {
      pushEdge(node.row.memory_id, node.row.superseded_by, '纠正链', 'correction', `旧条目被 ${node.row.superseded_by} 替代`);
    }
    if (node.row.supersedes) {
      pushEdge(node.row.supersedes, node.row.memory_id, '纠正链', 'correction', `旧条目 ${node.row.supersedes} 被这条替代`);
    }
  }

  // 只连接共享的具体来源事件。同地点、同主题、同群或同一段旧会话都不足以证明是同一件事。
  const events = new Map<string, string[]>();
  for (const node of nodes) {
    for (const eventId of node.row.source_event_ids) {
      const list = events.get(eventId) ?? [];
      list.push(node.row.memory_id);
      events.set(eventId, list);
    }
  }
  for (const [eventId, ids] of events) {
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = ids[i];
        const b = ids[j];
        if (a && b) pushEdge(a, b, '同源事件', 'source', eventId);
      }
    }
  }

  const byMonth = new Map<number, StarNode[]>();
  for (const node of nodes) {
    if (node.month === null) continue;
    const list = byMonth.get(node.month) ?? [];
    list.push(node);
    byMonth.set(node.month, list);
  }
  const lunarArcs: StarLunarArc[] = [];
  const sameYearMonths: number[] = [];
  const crossYearMonths: number[] = [];
  for (const [month, monthNodes] of byMonth) {
    monthNodes.sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0));
    const years = Array.from(new Set(monthNodes.map((n) => n.year).filter((y): y is number => y !== null))).sort();
    const perYear = new Map<number, number>();
    for (const node of monthNodes) {
      if (node.year !== null) perYear.set(node.year, (perYear.get(node.year) ?? 0) + 1);
    }
    if (years.length >= 2) crossYearMonths.push(month);
    if (Array.from(perYear.values()).some((count) => count >= 2)) sameYearMonths.push(month);

    for (let i = 0; i < monthNodes.length - 1; i += 1) {
      const fromNode = monthNodes[i];
      const toNode = monthNodes[i + 1];
      if (!fromNode || !toNode || fromNode.year === null || toNode.year === null) continue;
      const kind: StarMonthArcKind = fromNode.year === toNode.year ? 'same-year' : 'cross-year';
      lunarArcs.push({
        month,
        from: fromNode.row.memory_id,
        to: toNode.row.memory_id,
        kind,
        fromYear: fromNode.year,
        toYear: toNode.year,
        label: kind === 'cross-year'
          ? `${month}月跨年 ${fromNode.year}→${toNode.year}`
          : `${fromNode.year}·${month}月同月`,
      });
    }
  }

  return {
    canvasWidth,
    totalHeight,
    nodes,
    nodeMap,
    lanes,
    yearTicks,
    chasms: visibleChasms,
    causalEdges,
    lunarArcs,
    monthCounts,
    sameYearMonths,
    crossYearMonths,
    monthYears: Object.fromEntries(Object.entries(monthYears).map(([month, years]) => [month, Array.from(years).sort()])),
    unknownDateCount: unknown.length,
    timeline: withTimestamp.length ? { start: minTs, end: maxTs } : null,
  };
}

export interface MonthWheelProps {
  monthCounts: Record<number, number>;
  /** 出现在两个以上年份的月份。 */
  crossYearMonths: number[];
  /** 同一年里出现两次以上的月份。 */
  sameYearMonths: number[];
  /** 每个月出现过的年份。 */
  monthYears: Record<number, number[]>;
  activeMonth: number | null;
  onSelectMonth: (month: number | null) => void;
}

export function MonthWheel({
  monthCounts,
  crossYearMonths,
  sameYearMonths,
  monthYears,
  activeMonth,
  onSelectMonth,
}: MonthWheelProps): React.ReactElement {
  const posMap = useMemo(() => {
    const map: Record<number, { x: number; y: number; lx: number; ly: number; angle: number }> = {};
    for (let mo = 1; mo <= 12; mo += 1) {
      const angle = (((mo - 1) * 30 - 90) * Math.PI) / 180;
      map[mo] = {
        angle,
        x: Math.cos(angle) * 56,
        y: Math.sin(angle) * 56,
        lx: Math.cos(angle) * 74,
        ly: Math.sin(angle) * 74,
      };
    }
    return map;
  }, []);

  const activeCount = activeMonth ? (monthCounts[activeMonth] ?? 0) : 0;
  const activeYears = activeMonth ? (monthYears[activeMonth] ?? []) : [];
  const isCrossYear = activeMonth ? crossYearMonths.includes(activeMonth) : false;
  const isSameYear = activeMonth ? sameYearMonths.includes(activeMonth) : false;
  const spokeMonths = Array.from(new Set([...crossYearMonths, ...sameYearMonths])).sort((a, b) => a - b);

  return (
    <div className="mem-wheel" data-testid="mem-month-wheel">
      <svg className="mem-wheel__svg" viewBox="-88 -88 176 176" role="group" aria-label="按月份查看同月记录">
        <circle cx={0} cy={0} r={56} fill="none" stroke="var(--line-strong)" strokeWidth={1} />
        <circle cx={0} cy={0} r={20} fill="none" stroke="var(--line)" strokeDasharray="2 3" />

        {spokeMonths.map((mo) => {
          const p = posMap[mo];
          if (!p) return null;
          const sx = Math.cos(p.angle) * 20;
          const sy = Math.sin(p.angle) * 20;
          const isAct = activeMonth === mo;
          const cross = crossYearMonths.includes(mo);
          return (
            <line
              key={`spoke-${mo}`}
              className={`mem-wheel__spoke mem-wheel__spoke--${cross ? 'cross' : 'same'}`}
              x1={sx}
              y1={sy}
              x2={p.x}
              y2={p.y}
              stroke={isAct ? 'var(--mem-lunar)' : undefined}
              strokeWidth={isAct ? 1.8 : 1}
              strokeDasharray={isAct || cross ? undefined : '2 2'}
            />
          );
        })}

        <text
          x={0}
          y={4}
          textAnchor="middle"
          className="mem-wheel__center"
          fill={activeMonth ? 'var(--mem-lunar)' : 'var(--ink-soft)'}
        >
          {activeMonth ? `${activeMonth}月` : '全年'}
        </text>

        {Array.from({ length: 12 }, (_, idx) => idx + 1).map((mo) => {
          const p = posMap[mo];
          if (!p) return null;
          const count = monthCounts[mo] ?? 0;
          const isEmpty = count === 0;
          const isActive = activeMonth === mo;
          const cross = crossYearMonths.includes(mo);
          const same = sameYearMonths.includes(mo);
          const years = monthYears[mo] ?? [];

          return (
            <g
              key={mo}
              className={`mem-wheel__node${isEmpty ? ' mem-wheel__node--empty' : ''}${isActive ? ' mem-wheel__node--active' : ''}${cross ? ' mem-wheel__node--cross' : ''}${!cross && same ? ' mem-wheel__node--same' : ''}`}
              data-month={mo}
              data-kind={cross ? 'cross-year' : same ? 'same-year' : 'single'}
              role={isEmpty ? undefined : 'button'}
              tabIndex={isEmpty ? -1 : 0}
              aria-label={isEmpty
                ? `${mo}月无记录`
                : `${mo}月（${count} 条记忆${cross ? `，${years.join('、')} 年跨年同月` : same ? '，同年同月' : ''}）`}
              aria-pressed={isEmpty ? undefined : isActive}
              onClick={() => {
                if (!isEmpty) onSelectMonth(isActive ? null : mo);
              }}
              onKeyDown={(e) => {
                if (!isEmpty && (e.key === 'Enter' || e.key === ' ')) {
                  e.preventDefault();
                  onSelectMonth(isActive ? null : mo);
                }
              }}
            >
              <circle cx={p.x} cy={p.y} r={14} fill="transparent" />
              <circle
                cx={p.x}
                cy={p.y}
                r={isActive ? 5.5 : cross ? 4.4 : same ? 3.7 : isEmpty ? 2 : 3}
                fill={isActive ? 'var(--mem-lunar)' : cross ? '#2c647f' : same ? '#7d9aaa' : isEmpty ? 'var(--ink-faint)' : 'var(--ink-soft)'}
                stroke={isActive ? '#fff' : undefined}
                strokeWidth={isActive ? 1.5 : undefined}
              />
              <text x={p.lx} y={p.ly + 3} textAnchor="middle">
                {mo}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="mem-wheel__info">
        <div className="mem-wheel__head">
          <span>十二月令环</span>
          <span className="mem-wheel__hint">跨年 / 同年</span>
        </div>
        <p className="mem-wheel__caption">
          {activeMonth ? (
            <>
              <strong>{MONTH_SEASONS[activeMonth] ?? `${activeMonth}月`}</strong>
              <span>
                {isCrossYear
                  ? ` · ${activeYears.join('/')} 跨年 · ${activeCount} 条`
                  : isSameYear
                    ? ` · 同年同月 · ${activeCount} 条`
                    : ` · ${activeCount} 条`}
              </span>
            </>
          ) : (
            '点击有记录的月份查看同月呼应'
          )}
        </p>
        {activeMonth ? (
          <button
            type="button"
            className="mem-wheel__reset"
            onClick={() => onSelectMonth(null)}
          >
            回到全年
          </button>
        ) : null}
      </div>
    </div>
  );
}

function lunarCurve(model: StarChartModel, arc: StarLunarArc, index: number) {
  const a = model.nodeMap[arc.from];
  const b = model.nodeMap[arc.to];
  if (!a || !b) return null;
  const x1 = a.xPx + STAR_CARD_W / 2;
  const y1 = a.yPx + STAR_CARD_H / 2;
  const x2 = b.xPx + STAR_CARD_W / 2;
  const y2 = b.yPx + STAR_CARD_H / 2;
  return {
    x1, y1, x2, y2,
    mx: (x1 + x2) / 2,
    my: Math.max(46, Math.min(y1, y2) - 52 - (index % 2) * 16),
  };
}

function layoutLunarLabels(model: StarChartModel, activeMonth: number | null) {
  if (activeMonth === null) return [];
  const occupied = model.nodes.map(({ xPx, yPx }) => ({
    left: xPx, top: yPx, right: xPx + STAR_CARD_W, bottom: yPx + STAR_CARD_H,
  }));
  const arcs = model.lunarArcs
    .map((arc, index) => ({ arc, index }))
    .filter(({ arc }) => arc.month === activeMonth)
    // 跨年同月比同年同月更值得先标出来；空间不够时宁可少标，也不压住卡片。
    .sort((a, b) => (a.arc.kind === b.arc.kind ? a.index - b.index : a.arc.kind === 'cross-year' ? -1 : 1))
    .slice(0, 3);
  const labels: Array<{ arc: StarLunarArc; x: number; y: number; width: number }> = [];
  for (const { arc, index } of arcs) {
    const curve = lunarCurve(model, arc, index);
    if (!curve) continue;
    const width = 12 + [...arc.label].reduce((sum, char) => sum + (char.charCodeAt(0) < 128 ? 7 : 10), 0);
    let placed = false;
    for (const dy of [0, -20, 20, -40, 40, -60, 60]) {
      for (const t of [.5, .32, .68, .18, .82]) {
        const a = (1 - t) ** 2;
        const b = 2 * t * (1 - t);
        const c = t ** 2;
        const x = a * curve.x1 + b * curve.mx + c * curve.x2;
        const y = a * curve.y1 + b * curve.my + c * curve.y2 + dy;
        const rect = { left: x - width / 2 - 3, right: x + width / 2 + 3, top: y - 10, bottom: y + 10 };
        if (rect.left < 90 || rect.right > model.canvasWidth || rect.top < 38 || rect.bottom > model.totalHeight) continue;
        if (occupied.some((item) => rect.left < item.right && item.left < rect.right
          && rect.top < item.bottom && item.top < rect.bottom)) continue;
        occupied.push(rect);
        labels.push({ arc, x, y, width });
        placed = true;
        break;
      }
      if (placed) break;
    }
  }
  return labels;
}

export interface MemoryStarMapCanvasProps {
  model: StarChartModel;
  selectedId: string | null;
  activeMonth: number | null;
  onSelectMemory: (id: string, month: number | null, trigger: HTMLButtonElement) => void;
  /** 放大镜模式：选中后只保留强/中关联，其余淡出。 */
  focusMode?: boolean;
  /** 放大镜面板，挂在 canvas 坐标里，跟着卡片走。 */
  focusOverlay?: React.ReactNode;
  kindText: (kind: string) => string;
  originText: (origin: string) => string;
  statusText: (row: MemoryRecord) => string | null;
}

export function MemoryStarMapCanvas({
  model,
  selectedId,
  activeMonth,
  onSelectMemory,
  focusMode = false,
  focusOverlay,
  kindText,
  originText,
  statusText,
}: MemoryStarMapCanvasProps): React.ReactElement {
  const { canvasWidth, totalHeight } = model;
  const lunarLabels = useMemo(() => layoutLunarLabels(model, activeMonth), [model, activeMonth]);

  const causalNeighborIds = useMemo(() => {
    const set = new Set<string>();
    if (!selectedId) return set;
    model.causalEdges.forEach((edge) => {
      if (edge.from === selectedId) set.add(edge.to);
      if (edge.to === selectedId) set.add(edge.from);
    });
    return set;
  }, [model.causalEdges, selectedId]);

  const affinity = useMemo(() => (focusMode ? starAffinity(model, selectedId) : null), [focusMode, model, selectedId]);

  const monthHitIds = useMemo(() => {
    const set = new Set<string>();
    if (!activeMonth) return set;
    model.nodes.forEach((n) => {
      if (n.month === activeMonth) set.add(n.row.memory_id);
    });
    return set;
  }, [model.nodes, activeMonth]);

  return (
    <div
      className={`mem-star-canvas${model.nodes.length > 28 ? ' mem-star-canvas--dense' : ''}`}
      style={{ width: `${canvasWidth}px`, height: `${totalHeight}px` }}
      data-testid="mem-star-canvas"
    >
      <div className="mem-star-ruler" aria-hidden="true">
        {model.yearTicks.map((tick) => (
          <span key={tick.year} className="mem-star-ruler__tick" style={{ left: `${tick.xPx}px` }}>
            {tick.year}
          </span>
        ))}
      </div>

      {model.chasms.map((chasm) => (
        <button
          key={`${chasm.targetId}-${chasm.days}`}
          type="button"
          className="mem-star-chasm"
          style={{ left: `${chasm.xPx}px`, width: `${chasm.widthPx}px` }}
          title={`本批可见记录之间相隔 ${chasm.days} 天（不代表这段时间没有记忆），跳到下一条`}
          onClick={(event) => {
            const target = model.nodeMap[chasm.targetId];
            onSelectMemory(chasm.targetId, target?.month ?? null, event.currentTarget);
          }}
        >
          <span className="mem-star-chasm__label">{chasm.label}</span>
        </button>
      ))}

      {model.unknownDateCount > 0 ? (
        <div className="mem-star-unknown-band" style={{ left: `${canvasWidth - 182}px` }} aria-hidden="true">
          <span>发生日期未知</span>
        </div>
      ) : null}

      {model.lanes.map((lane) => (
        <div
          key={lane.id}
          className="mem-star-lane"
          data-lane={lane.id}
          style={{ top: `${lane.topPx}px`, height: `${lane.heightPx}px` }}
        >
          <div className="mem-star-lane__label">
            <span>{lane.label}</span>
            <small>{lane.count} 条</small>
          </div>
        </div>
      ))}

      <svg className="mem-star-arcs" viewBox={`0 0 ${canvasWidth} ${totalHeight}`} aria-hidden="true">
        {model.causalEdges.map((edge, idx) => {
          const a = model.nodeMap[edge.from];
          const b = model.nodeMap[edge.to];
          if (!a || !b) return null;
          const x1 = a.xPx + STAR_CARD_W * 0.5;
          const y1 = a.yPx + STAR_CARD_H * 0.5;
          const x2 = b.xPx + STAR_CARD_W * 0.5;
          const y2 = b.yPx + STAR_CARD_H * 0.5;
          const mx = (x1 + x2) / 2;
          const my = (y1 + y2) / 2 + (idx % 2 === 0 ? -42 : 34);
          const isAct = selectedId !== null && (edge.from === selectedId || edge.to === selectedId);
          const isDim = focusMode && !isAct;
          return (
            <path
              key={`c-${edge.from}-${edge.to}`}
              d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
              className={`mem-star-arc mem-star-arc--${edge.kind}${isAct ? ' mem-star-arc--active' : ''}${isDim ? ' mem-star-arc--dim' : ''}`}
              data-reason={edge.reason}
              data-kind={edge.kind}
              data-evidence={edge.evidence}
              data-from={edge.from}
              data-to={edge.to}
            />
          );
        })}

        {model.lunarArcs.map((arc, idx) => {
          const curve = lunarCurve(model, arc, idx);
          if (!curve) return null;
          const { x1, y1, x2, y2, mx, my } = curve;
          const isAct = activeMonth === arc.month;
          const isFocusLink = Boolean(affinity && affinity.links.some((link) => link.id === arc.to || link.id === arc.from));
          const isDim = focusMode && !isFocusLink;
          return (
            <path
              key={`m-${arc.month}-${arc.from}-${arc.to}`}
              d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
              className={`mem-star-arc mem-star-arc--lunar mem-star-arc--lunar-${arc.kind}${isAct ? ' mem-star-arc--lunar-active' : ''}${isFocusLink ? ' mem-star-arc--focus' : ''}${isDim ? ' mem-star-arc--dim' : ''}`}
              data-month={arc.month}
              data-kind={arc.kind}
              data-from={arc.from}
              data-to={arc.to}
            />
          );
        })}
      </svg>

      {lunarLabels.length ? (
        <div className="mem-star-arc-labels" aria-hidden="true">
          {lunarLabels.map(({ arc, x, y, width }) => (
            <span key={`lbl-m-${arc.from}-${arc.to}`} className={`mem-star-pill mem-star-pill--lunar mem-star-pill--${arc.kind}`}
              style={{ left: x, top: y, width }}>
              {arc.label}
            </span>
          ))}
        </div>
      ) : null}

      <ul className="mem-star-nodes" aria-label="星图记忆" data-testid="mem-results">
        {model.nodes.map((node) => {
          const id = node.row.memory_id;
          const isSel = selectedId === id;
          const isMonthHit = monthHitIds.has(id);
          const isNb = causalNeighborIds.has(id);
          const link = affinity?.links.find((item) => item.id === id);
          const isDim = Boolean(focusMode && selectedId && !isSel && !link);
          const status = statusText(node.row);
          const cls = [
            'mem-star-node',
            node.dateKind === '未知' ? 'mem-star-node--unknown-date' : '',
            isSel ? 'mem-star-node--selected' : '',
            !isSel && isMonthHit ? 'mem-star-node--month' : '',
            !isSel && !isMonthHit && isNb ? 'mem-star-node--neighbor' : '',
            link ? `mem-star-node--link-${link.level}` : '',
            isDim ? 'mem-star-node--dim' : '',
            node.row.pinned ? 'mem-star-node--pinned' : '',
            status ? 'mem-star-node--tombstone' : '',
            node.row.origin === 'model_inference' ? 'mem-star-node--inferred' : '',
          ].filter(Boolean).join(' ');
          const dateLabel = node.dateKind === '未知'
            ? '发生未知'
            : node.year && node.month
              ? `${String(node.year).slice(2)}·${String(node.month).padStart(2, '0')} ${node.dateKind}`
              : '未记时间';
          const dateTitle = node.dateKind === '未知'
            ? `发生日期未记录${node.recordedAt ? ` · 记于 ${formatShortDate(node.recordedAt)}` : ''}`
            : node.dateKind === '发生'
              ? `发生 ${formatShortDate(node.occurredAt)} · 记下 ${formatShortDate(node.recordedAt)}`
              : `记下 ${formatShortDate(node.recordedAt)}`;

          return (
            <li key={id} className="mem-star-node-li" data-lane={node.lane}
              style={{ left: `${node.xPx}px`, top: `${node.yPx}px`, width: STAR_CARD_W, height: STAR_CARD_H }}>
              <button
                type="button"
                className={cls}
                data-memory-id={id}
                data-date-kind={node.dateKind}
                data-unknown-date={node.dateKind === '未知' ? 'true' : undefined}
                data-affinity-kind={link?.kind}
                aria-current={isSel ? 'true' : undefined}
                aria-label={`${dateTitle}，${kindText(node.row.kind)}，${node.row.text}`}
                title={dateTitle}
                onClick={(event) => onSelectMemory(id, node.month, event.currentTarget)}
              >
                <span className="mem-star-node__top">
                  <span className="mem-star-node__mo">{dateLabel}</span>
                  <span className="mem-star-node__kind">
                    {node.row.pinned ? '固定' : kindText(node.row.kind)}
                  </span>
                </span>
                <span className="mem-star-node__title">{node.row.text}</span>
                <span className="mem-star-node__foot">
                  <span title={status ? originText(node.row.origin) : undefined}>{status ?? originText(node.row.origin)}</span>
                  <span className="mem-star-node__beats">{sessionScope(node.row) ? `${sessionScope(node.row)} · ` : ''}{node.beats.length}幕 ↗</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {focusOverlay}
    </div>
  );
}

export type StarConnectionKind = 'correction' | 'source' | 'same-year' | 'cross-year';

export interface MemoryMicroscopeBeatsProps {
  row: MemoryRecord;
  beats: StarBeat[];
  connectedEntries: Array<{ row: MemoryRecord; reason: string; kind: StarConnectionKind }>;
  onSelectMemory: (id: string) => void;
}

/**
 * 选中一条记忆时的关联度。
 *
 * 强：共享具体来源事件，或纠正链两端；这两类关系有可核对依据。
 * 中：同月；只说明日期关系，不说明同一件事。
 * 弱：其余全部。同地点、同主题、同群都不升级为强关联。
 *
 * 只返回直接相邻的关系，不做多跳扩散——两跳之外的“相关”没有足够依据。
 */
function dayDistance(a: StarNode, b: StarNode): number | null {
  if (a.timestamp === null || b.timestamp === null) return null;
  return Math.abs(a.timestamp - b.timestamp) / DAY_MS;
}

export function starAffinity(model: StarChartModel, selectedId: string | null): StarAffinity | null {
  if (!selectedId || !Object.hasOwn(model.nodeMap, selectedId)) return null;
  const links: StarAffinityLink[] = [];
  const seen = new Set<string>();
  const add = (otherId: string, level: StarAffinityLevel, reason: string, evidence: string, kind: StarAffinityLink['kind']) => {
    if (otherId === selectedId || seen.has(otherId) || !Object.hasOwn(model.nodeMap, otherId)) return;
    seen.add(otherId);
    links.push({ id: otherId, level, reason, evidence, kind });
  };

  for (const edge of model.causalEdges) {
    if (edge.from === selectedId) add(edge.to, 'strong', edge.reason, edge.evidence, edge.kind);
    if (edge.to === selectedId) add(edge.from, 'strong', edge.reason, edge.evidence, edge.kind);
  }
  const selectedNode = model.nodeMap[selectedId];
  const nearby: Array<{ id: string; days: number; label: string; kind: StarLunarArc['kind'] }> = [];
  for (const arc of model.lunarArcs) {
    const otherId = arc.from === selectedId ? arc.to : arc.to === selectedId ? arc.from : null;
    if (!otherId) continue;
    const other = model.nodeMap[otherId];
    const days = selectedNode && other ? dayDistance(selectedNode, other) : null;
    if (days === null) continue;
    // 同月不等于同一天。整月几十条全连会失去区分度：同日全部保留，相邻三天只取最近两条。
    if (days < 1) add(otherId, 'medium', '同一天', arc.label, arc.kind);
    else if (days <= 3) nearby.push({ id: otherId, days, label: arc.label, kind: arc.kind });
  }
  nearby.sort((a, b) => a.days - b.days).slice(0, 2)
    .forEach((item) => add(item.id, 'medium', '相邻三天', item.label, item.kind));

  const counts = { strong: 0, medium: 0, weak: 0 };
  for (const node of model.nodes) {
    if (node.row.memory_id === selectedId) continue;
    const link = links.find((item) => item.id === node.row.memory_id);
    counts[link ? link.level : 'weak'] += 1;
  }
  return { selectedId, links, counts };
}

export function connectedStarEntries(model: StarChartModel, selectedId: string): MemoryMicroscopeBeatsProps['connectedEntries'] {
  const connected: MemoryMicroscopeBeatsProps['connectedEntries'] = [];
  const seen = new Set<string>();
  const add = (from: string, to: string, reason: string, kind: StarConnectionKind) => {
    const other = from === selectedId ? to : to === selectedId ? from : null;
    const node = other ? model.nodeMap[other] : undefined;
    if (!node) return;
    const key = `${node.row.memory_id}|${kind}|${reason}`;
    if (seen.has(key)) return;
    seen.add(key);
    connected.push({ row: node.row, reason, kind });
  };
  for (const edge of model.causalEdges) {
    add(edge.from, edge.to, `${edge.reason} · ${edge.evidence}`, edge.kind);
  }
  for (const arc of model.lunarArcs) {
    add(arc.from, arc.to, arc.label, arc.kind);
  }
  return connected;
}

export function MemoryMicroscopeBeats({
  row,
  beats,
  connectedEntries,
  onSelectMemory,
}: MemoryMicroscopeBeatsProps): React.ReactElement {
  const [expandedIdx, setExpandedIdx] = useState(0);
  const theaterRef = useRef<HTMLDialogElement>(null);

  const safeIdx = Math.min(expandedIdx, Math.max(0, beats.length - 1));

  return (
    <div className="mem-micro" data-testid="mem-microscope">
      {connectedEntries.length > 0 ? (
        <div className="mem-micro__links">
          <div className="mem-micro__links-title">星图关联（{connectedEntries.length}）</div>
          {connectedEntries.map((item) => (
            <button
              key={`${item.kind}-${item.row.memory_id}`}
              type="button"
              className={`mem-micro__jump mem-micro__jump--${item.kind}`}
              onClick={() => onSelectMemory(item.row.memory_id)}
            >
              <span className="mem-micro__jump-reason">{item.reason}</span>
              <span className="mem-micro__jump-text">{item.row.text}</span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="mem-micro__head">
        <span className="mem-micro__kicker">事件微切片 · 第 {safeIdx + 1}/{beats.length} 幕</span>
        <div className="mem-micro__actions">
          {beats.length > 1 ? (
            <button
              type="button"
              className="button button--small"
              onClick={() => setExpandedIdx((cur) => (cur + 1) % beats.length)}
            >
              下一幕 ↓
            </button>
          ) : null}
          <button
            type="button"
            className="button button--small"
            onClick={() => theaterRef.current?.showModal()}
          >
            全景分镜 ⤢
          </button>
        </div>
      </div>

      <ol className="mem-beats">
        {beats.map((beat, idx) => {
          const isOpen = safeIdx === idx;
          return (
            <li key={`${beat.time}-${idx}`} className={`mem-beat${isOpen ? ' mem-beat--open' : ''}`}>
              <button
                type="button"
                className="mem-beat__trigger"
                aria-expanded={isOpen}
                onClick={() => setExpandedIdx(idx)}
              >
                <span className="mem-beat__row">
                  <span className="mem-beat__time">{beat.time}</span>
                  <span className="mem-beat__tag">{beat.tag}</span>
                </span>
                <span className="mem-beat__title">{beat.title}</span>
              </button>
              {isOpen ? (
                <div className="mem-beat__detail">
                  <p>{beat.detail}</p>
                  <p className="mem-beat__inner">{beat.inner}</p>
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>

        <dialog
          ref={theaterRef}
          className="mem-theater-overlay"
          aria-label="记忆全景分镜"
          onClick={(e) => {
            if (e.target === e.currentTarget) e.currentTarget.close();
          }}
        >
          <div className="mem-theater">
            <header className="mem-theater__head">
              <div>
                <div className="mem-theater__meta">
                  {memoryOccurredAt(row)
                    ? `发生 ${formatShortDate(memoryOccurredAt(row))} · 记下 ${formatShortDate(row.created_at)}`
                    : row.import_source
                      ? `发生日期未记录 · 记下 ${formatShortDate(row.created_at)}`
                      : `记下 ${formatShortDate(row.created_at)}`} · {beats.length} 幕全景分镜
                </div>
                <h3>{row.text}</h3>
              </div>
              <button type="button" className="button button--small" onClick={() => theaterRef.current?.close()}>
                收起分镜 ✕
              </button>
            </header>
            <div className="mem-theater__frames">
              {beats.map((beat, idx) => (
                <div key={`frame-${idx}`} className={`mem-frame${idx === safeIdx ? ' mem-frame--active' : ''}`}>
                  <div>
                    <div className="mem-frame__top">
                      <span className="mem-frame__num">FRAME 0{idx + 1} · {beat.time}</span>
                      <span className="mem-beat__tag">{beat.tag}</span>
                    </div>
                    <h4 className="mem-frame__title">{beat.title}</h4>
                    <p className="mem-frame__detail">{beat.detail}</p>
                  </div>
                  <p className="mem-frame__inner">{beat.inner}</p>
                </div>
              ))}
            </div>
          </div>
        </dialog>
    </div>
  );
}
