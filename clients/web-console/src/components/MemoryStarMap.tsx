/**
 * 记忆库星图视图：把 `/v1/memory` 返回的 `MemoryRecord[]` 投影到「时间 × 类型」星图，
 * 并用十二月令环连接跨年同月共振、用事件显微镜拆解单条记忆的来龙去脉。
 */
import { useMemo, useRef, useState } from 'react';
import type { MemoryRecord } from '../types';
import '../memory-star-map.css';

export interface StarBeat {
  time: string;
  tag: string;
  title: string;
  detail: string;
  inner: string;
}

export interface StarNode {
  row: MemoryRecord;
  lane: StarLaneId;
  xPx: number;
  yPx: number;
  month: number | null;
  year: number | null;
  timestamp: number | null;
  dateKind: '发生' | '记下' | null;
  beats: StarBeat[];
}

export type StarLaneId = 'episode' | 'assertion' | 'procedure' | 'other';

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
  reason: string;
}

export interface StarLunarArc {
  month: number;
  from: string;
  to: string;
  label: string;
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
  recurringMonths: number[];
}

export const STAR_CARD_W = 138;
export const STAR_CARD_H = 82;

const LANE_LABELS: Record<StarLaneId, string> = {
  episode: '经历',
  assertion: '事实与线索',
  procedure: '做法',
  other: '未分类',
};

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

function formatShortDate(iso: string | null | undefined): string {
  if (!iso) return '时间未知';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '时间未知';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function laneOfKind(kind: string): StarLaneId {
  if (kind === 'episode' || kind === 'assertion' || kind === 'procedure') return kind;
  return 'other';
}

function splitSentences(text: string): string[] {
  const parts = text
    .split(/(?<=[。！？；\n])/u)
    .map((s) => s.trim())
    .filter(Boolean);
  return parts.length > 0 ? parts : [text];
}

export function buildMemoryMicroBeats(
  row: MemoryRecord,
  kindText: (kind: string) => string,
  originText: (origin: string) => string,
  visibilityText: (visibility: string) => string,
): StarBeat[] {
  const beats: StarBeat[] = [];
  const occurred = parseRecordDate(row.import_source?.occurred_at) ? row.import_source?.occurred_at : null;
  const primaryStamp = occurred ?? row.created_at;

  beats.push({
    time: primaryStamp ? formatShortDate(primaryStamp).slice(11) || '记下时刻' : '时刻未知',
    tag: `${kindText(row.kind)} · ${originText(row.origin)}`,
    title: occurred ? '事件实际发生与记录起点' : '记忆被写入库中的时刻',
    detail: occurred
      ? `这条记录原始发生于 ${formatShortDate(occurred)}，随后在 ${formatShortDate(row.created_at)} 汇入当前记忆库。`
      : `这条记忆于 ${formatShortDate(row.created_at)} 被记下，可见范围为「${visibilityText(row.visibility)}」。`,
    inner: row.origin === 'model_inference'
      ? '由模型推断得出，阅读时需与用户亲口陈述区分。'
      : `以「${originText(row.origin)}」口径进入长期记忆。`,
  });

  const sentences = splitSentences(row.text);
  beats.push({
    time: '核心内容',
    tag: row.confidence === null ? '正文陈述' : `置信度 ${Math.round(Math.max(0, Math.min(1, row.confidence)) * 100)}%`,
    title: sentences[0] && sentences[0].length <= 28 ? sentences[0] : '她记下的那句话',
    detail: row.text,
    inner: sentences.length > 1
      ? `整条陈述包含 ${sentences.length} 个层次，构成这条记忆的主体语义。`
      : '简短直接的一条长期记忆。',
  });

  if (row.source_event_ids.length > 0 || row.import_source) {
    const srcText = row.import_source
      ? `来自旧会话 ${row.import_source.session_id}（${row.import_source.owner_qq} → ${row.import_source.bot_qq}，类型 ${row.import_source.memory_type}）。`
      : `可追溯到 ${row.source_event_ids.length} 条对话来源事件：${row.source_event_ids.join('、')}。`;
    beats.push({
      time: '来源追溯',
      tag: row.import_source ? '导入线索' : `事件 × ${row.source_event_ids.length}`,
      title: row.import_source ? '从历史渠道迁移而来' : '由具体对话事件提炼而成',
      detail: srcText,
      inner: '来源编号把这条记忆锚定回原本发生的那场对话。',
    });
  }

  if (row.supersedes || row.superseded_by || row.retracted || row.archived_at) {
    const evolutionParts: string[] = [];
    if (row.supersedes) evolutionParts.push(`它纠正了更早的条目 ${row.supersedes}`);
    if (row.superseded_by) evolutionParts.push(`它已被新条目 ${row.superseded_by} 替代`);
    if (row.retracted) evolutionParts.push(`已撤回${row.retract_reason ? `（理由：${row.retract_reason}）` : ''}`);
    if (row.archived_at) evolutionParts.push(`归档于 ${formatShortDate(row.archived_at)}`);
    beats.push({
      time: '演进状态',
      tag: '变更轨迹',
      title: row.superseded_by ? '这条说法后来被改写了' : row.retracted ? '已从活跃召回中撤回' : '处于归档或接续状态',
      detail: evolutionParts.join('；') + '。',
      inner: '保留变更记录，能让观看者看清记忆是如何随时间修正的。',
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
      const occurred = parseRecordDate(row.import_source?.occurred_at);
      const date = occurred ?? parseRecordDate(row.created_at);
      const dateKind = occurred ? '发生' as const : date ? '记下' as const : null;
      return { row, date, ts: date?.getTime() ?? null, dateKind };
    })
    .sort((a, b) => (a.ts ?? Number.MAX_SAFE_INTEGER) - (b.ts ?? Number.MAX_SAFE_INTEGER));

  const validTs = dated.filter((d) => d.ts !== null).map((d) => d.ts as number);
  const minTs = validTs.length > 0 ? Math.min(...validTs) : 0;
  const maxTs = validTs.length > 0 ? Math.max(...validTs) : 1;
  const span = Math.max(1, maxTs - minTs);

  const canvasWidth = Math.max(660, Math.min(1120, rows.length * 48));
  const xStart = 72;
  const xEnd = canvasWidth - STAR_CARD_W - 14;
  const xRange = Math.max(200, xEnd - xStart);

  const laneIds: StarLaneId[] = ['episode', 'assertion', 'procedure'];
  if (rows.some((r) => laneOfKind(r.kind) === 'other')) laneIds.push('other');

  const laneSlotEnds: Record<StarLaneId, number[]> = {
    episode: [],
    assertion: [],
    procedure: [],
    other: [],
  };

  const monthCounts: Record<number, number> = {};
  const preliminary: Array<{
    row: MemoryRecord;
    lane: StarLaneId;
    xPx: number;
    slot: number;
    month: number | null;
    year: number | null;
    ts: number | null;
    dateKind: StarNode['dateKind'];
  }> = [];

  dated.forEach((item) => {
    const lane = laneOfKind(item.row.kind);
    const timePosition = item.ts !== null
      ? (minTs === maxTs ? xStart : xStart + ((item.ts - minTs) / span) * xRange)
      : xEnd;
    const xPx = Math.round(Math.max(xStart, Math.min(xEnd, timePosition)));

    // 节点已按横坐标递增，每个槽只需和上一张卡片比较。
    const slotEnds = laneSlotEnds[lane];
    const availableSlot = slotEnds.findIndex((end) => xPx - end >= STAR_CARD_W + 12);
    const assignedSlot = availableSlot === -1 ? slotEnds.length : availableSlot;
    slotEnds[assignedSlot] = xPx;

    const month = item.date ? item.date.getMonth() + 1 : null;
    const year = item.date ? item.date.getFullYear() : null;
    if (month !== null) monthCounts[month] = (monthCounts[month] ?? 0) + 1;

    preliminary.push({
      row: item.row,
      lane,
      xPx,
      slot: assignedSlot,
      month,
      year,
      ts: item.ts,
      dateKind: item.dateKind,
    });
  });

  const topHeader = 38;
  const rowPitch = STAR_CARD_H + 14;
  let cursorY = topHeader;
  const lanes = laneIds.map((id) => {
    const slotCount = laneSlotEnds[id].length;
    const heightPx = slotCount ? 24 + slotCount * rowPitch : 64;
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
    timestamp: item.ts,
    dateKind: item.dateKind,
    beats: buildMemoryMicroBeats(item.row, kindText, originText, visibilityText),
  }));

  const nodeMap: Record<string, StarNode> = Object.fromEntries(nodes.map((node) => [node.row.memory_id, node]));

  const chasms: StarChasm[] = [];
  const DAY_MS = 86_400_000;
  for (let i = 0; i < dated.length - 1; i += 1) {
    const cur = dated[i];
    const next = dated[i + 1];
    if (!cur || !next || cur.ts === null || next.ts === null) continue;
    const diffDays = Math.round((next.ts - cur.ts) / DAY_MS);
    if (diffDays >= 45 && chasms.length < 3) {
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
  }

  const yearSet = Array.from(new Set(nodes.map((n) => n.year).filter((y): y is number => y !== null))).sort((a, b) => a - b);
  const yearTicks: StarYearTick[] = yearSet.map((yr) => {
    const yrNodes = nodes.filter((n) => n.year === yr);
    const avgX = yrNodes.reduce((acc, cur) => acc + cur.xPx + STAR_CARD_W * 0.5, 0) / Math.max(1, yrNodes.length);
    return { year: yr, xPx: Math.round(avgX) };
  });

  const edgeSeen = new Set<string>();
  const causalEdges: StarCausalEdge[] = [];
  const pushEdge = (a: string, b: string, reason: string) => {
    if (a === b || !Object.hasOwn(nodeMap, a) || !Object.hasOwn(nodeMap, b)) return;
    const key = [a, b].sort().join('::');
    if (edgeSeen.has(key)) return;
    edgeSeen.add(key);
    causalEdges.push({ from: a, to: b, reason });
  };

  for (const a of nodes) {
    if (a.row.superseded_by) pushEdge(a.row.memory_id, a.row.superseded_by, '纠正链');
    if (a.row.supersedes) pushEdge(a.row.supersedes, a.row.memory_id, '纠正链');
  }

  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    if (!a) continue;
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      if (!b) continue;
      const sharedEvent = a.row.source_event_ids.find((ev) => b.row.source_event_ids.includes(ev));
      if (sharedEvent) {
        pushEdge(a.row.memory_id, b.row.memory_id, '同源事件');
        continue;
      }
      const first = a.row.import_source;
      const second = b.row.import_source;
      if (first?.session_id && second
        && first.session_id === second.session_id
        && first.source === second.source
        && first.platform === second.platform
        && first.owner_qq === second.owner_qq
        && first.bot_qq === second.bot_qq) {
        pushEdge(a.row.memory_id, b.row.memory_id, '同段旧会话');
      }
    }
  }

  const lunarArcs: StarLunarArc[] = [];
  const recurringMonths: number[] = [];
  for (let mo = 1; mo <= 12; mo += 1) {
    const moNodes = nodes.filter((n) => n.month === mo);
    if (moNodes.length >= 2) {
      recurringMonths.push(mo);
      for (let i = 0; i < moNodes.length - 1; i += 1) {
        const fromNode = moNodes[i];
        const toNode = moNodes[i + 1];
        if (!fromNode || !toNode) continue;
        const yearSpan = fromNode.year !== toNode.year ? `跨年共振 · ${fromNode.year}→${toNode.year}` : '同月共振';
        lunarArcs.push({
          month: mo,
          from: fromNode.row.memory_id,
          to: toNode.row.memory_id,
          label: `${mo}月${yearSpan}`,
        });
      }
    }
  }

  return {
    canvasWidth,
    totalHeight,
    nodes,
    nodeMap,
    lanes,
    yearTicks,
    chasms,
    causalEdges,
    lunarArcs,
    monthCounts,
    recurringMonths,
  };
}

export interface MonthWheelProps {
  monthCounts: Record<number, number>;
  recurringMonths: number[];
  activeMonth: number | null;
  onSelectMonth: (month: number | null) => void;
}

export function MonthWheel({
  monthCounts,
  recurringMonths,
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
  const isRecurring = activeMonth ? recurringMonths.includes(activeMonth) : false;

  return (
    <div className="mem-wheel" data-testid="mem-month-wheel">
      <div className="mem-wheel__head">
        <span>十二月令环</span>
        <span className="mem-wheel__hint">同月共振</span>
      </div>
      <svg className="mem-wheel__svg" viewBox="-88 -88 176 176" role="group" aria-label="按月份查看跨年共振">
        <circle cx={0} cy={0} r={56} fill="none" stroke="var(--line-strong)" strokeWidth={1} />
        <circle cx={0} cy={0} r={20} fill="none" stroke="var(--line)" strokeDasharray="2 3" />

        {recurringMonths.map((mo) => {
          const p = posMap[mo];
          if (!p) return null;
          const sx = Math.cos(p.angle) * 20;
          const sy = Math.sin(p.angle) * 20;
          const isAct = activeMonth === mo;
          return (
            <line
              key={`spoke-${mo}`}
              x1={sx}
              y1={sy}
              x2={p.x}
              y2={p.y}
              stroke={isAct ? 'var(--mem-lunar)' : 'rgba(44, 100, 127, 0.25)'}
              strokeWidth={isAct ? 1.8 : 1}
              strokeDasharray={isAct ? undefined : '2 2'}
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
          const hasPair = recurringMonths.includes(mo);

          return (
            <g
              key={mo}
              className={`mem-wheel__node${isEmpty ? ' mem-wheel__node--empty' : ''}${isActive ? ' mem-wheel__node--active' : ''}`}
              data-month={mo}
              role={isEmpty ? undefined : 'button'}
              tabIndex={isEmpty ? -1 : 0}
              aria-label={isEmpty ? `${mo}月无记录` : `${mo}月（${count} 条记忆）`}
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
                r={isActive ? 5.5 : hasPair ? 4.2 : isEmpty ? 2 : 3}
                fill={isActive ? 'var(--mem-lunar)' : hasPair ? '#528ba6' : isEmpty ? 'var(--ink-faint)' : 'var(--ink-soft)'}
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
      <p className="mem-wheel__caption">
        {activeMonth ? (
          <>
            <strong>{MONTH_SEASONS[activeMonth] ?? `${activeMonth}月`}</strong>
            <span>
              {isRecurring ? ` · 同月 ${activeCount} 条共振` : ` · 当月 ${activeCount} 条`}
            </span>
          </>
        ) : (
          '全年'
        )}
      </p>
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
  return model.lunarArcs.flatMap((arc, index) => {
    if (arc.month !== activeMonth) return [];
    const curve = lunarCurve(model, arc, index);
    if (!curve) return [];
    const width = 12 + [...arc.label].reduce((sum, char) => sum + (char.charCodeAt(0) < 128 ? 7 : 10), 0);
    // 从弧线附近找落点，让卡片和已排好的标签保留各自的空间。
    for (const dy of [0, -20, 20, -40, 40, -60, 60]) {
      for (const t of [.5, .32, .68, .18, .82]) {
        const a = (1 - t) ** 2;
        const b = 2 * t * (1 - t);
        const c = t ** 2;
        const x = a * curve.x1 + b * curve.mx + c * curve.x2;
        const y = a * curve.y1 + b * curve.my + c * curve.y2 + dy;
        const rect = { left: x - width / 2 - 3, right: x + width / 2 + 3, top: y - 10, bottom: y + 10 };
        if (rect.left < 67 || rect.right > model.canvasWidth || rect.top < 38 || rect.bottom > model.totalHeight) continue;
        if (occupied.some((item) => rect.left < item.right && item.left < rect.right
          && rect.top < item.bottom && item.top < rect.bottom)) continue;
        occupied.push(rect);
        return [{ arc, x, y, width }];
      }
    }
    return [];
  });
}

export interface MemoryStarMapCanvasProps {
  model: StarChartModel;
  selectedId: string | null;
  activeMonth: number | null;
  onSelectMemory: (id: string, month: number | null, trigger: HTMLButtonElement) => void;
  kindText: (kind: string) => string;
  originText: (origin: string) => string;
  statusText: (row: MemoryRecord) => string | null;
}

export function MemoryStarMapCanvas({
  model,
  selectedId,
  activeMonth,
  onSelectMemory,
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
      className="mem-star-canvas"
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
          title={`本批记忆相隔 ${chasm.days} 天，查看下一条`}
          onClick={(event) => {
            const target = model.nodeMap[chasm.targetId];
            onSelectMemory(chasm.targetId, target?.month ?? null, event.currentTarget);
          }}
        >
          <span className="mem-star-chasm__label">{chasm.label}</span>
        </button>
      ))}

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
          return (
            <path
              key={`c-${edge.from}-${edge.to}`}
              d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
              className={`mem-star-arc${isAct ? ' mem-star-arc--causal' : ''}`}
              data-reason={edge.reason}
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
          return (
            <path
              key={`m-${arc.month}-${arc.from}-${arc.to}`}
              d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
              className={`mem-star-arc mem-star-arc--lunar${isAct ? ' mem-star-arc--lunar-active' : ''}`}
              data-month={arc.month}
              data-from={arc.from}
              data-to={arc.to}
            />
          );
        })}
      </svg>

      {lunarLabels.length ? (
        <div className="mem-star-arc-labels" aria-hidden="true">
          {lunarLabels.map(({ arc, x, y, width }) => (
            <span key={`lbl-m-${arc.from}-${arc.to}`} className="mem-star-pill mem-star-pill--lunar"
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
          const status = statusText(node.row);
          const cls = [
            'mem-star-node',
            isSel ? 'mem-star-node--selected' : '',
            !isSel && isMonthHit ? 'mem-star-node--month' : '',
            !isSel && !isMonthHit && isNb ? 'mem-star-node--neighbor' : '',
          ].filter(Boolean).join(' ');

          return (
            <li key={id} className="mem-star-node-li" data-lane={node.lane}
              style={{ left: `${node.xPx}px`, top: `${node.yPx}px`, width: STAR_CARD_W, height: STAR_CARD_H }}>
              <button
                type="button"
                className={cls}
                data-memory-id={id}
                aria-current={isSel ? 'true' : undefined}
                onClick={(event) => onSelectMemory(id, node.month, event.currentTarget)}
              >
                <span className="mem-star-node__top">
                  <span className="mem-star-node__mo">
                    {node.year && node.month
                      ? `${String(node.year).slice(2)}·${String(node.month).padStart(2, '0')} ${node.dateKind}`
                      : '未记时间'}
                  </span>
                  <span>{kindText(node.row.kind)}</span>
                </span>
                <span className="mem-star-node__title">{node.row.text}</span>
                <span className="mem-star-node__foot">
                  <span title={status ? originText(node.row.origin) : undefined}>{status ?? originText(node.row.origin)}</span>
                  <span className="mem-star-node__beats">{node.beats.length}幕 ↗</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export interface MemoryMicroscopeBeatsProps {
  row: MemoryRecord;
  beats: StarBeat[];
  connectedEntries: Array<{ row: MemoryRecord; reason: string; kind: 'causal' | 'lunar' }>;
  onSelectMemory: (id: string) => void;
}

export function connectedStarEntries(model: StarChartModel, selectedId: string): MemoryMicroscopeBeatsProps['connectedEntries'] {
  const connected: MemoryMicroscopeBeatsProps['connectedEntries'] = [];
  const add = (from: string, to: string, reason: string, kind: 'causal' | 'lunar') => {
    const other = from === selectedId ? to : to === selectedId ? from : null;
    const node = other ? model.nodeMap[other] : undefined;
    if (node) connected.push({ row: node.row, reason, kind });
  };
  for (const edge of model.causalEdges) add(edge.from, edge.to, edge.reason, 'causal');
  for (const arc of model.lunarArcs) add(arc.from, arc.to, arc.label, 'lunar');
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

      {connectedEntries.length > 0 ? (
        <div className="mem-micro__links">
          <div className="mem-micro__links-title">相关连线</div>
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
                <div className="mem-theater__meta">{formatShortDate(row.created_at)} · {beats.length} 幕全景分镜</div>
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
