import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { MemoryRecord } from '../types';
import { memoryOccurredAt, type StarAffinity, type StarChartModel } from './MemoryStarMap';

export interface MemoryFocusLensProps {
  model: StarChartModel;
  affinity: StarAffinity;
  row: MemoryRecord;
  kindText: (kind: string) => string;
  originText: (origin: string) => string;
  statusText: (row: MemoryRecord) => string | null;
  onSelectMemory: (id: string) => void;
  onOpenDetail: () => void;
  onClose: () => void;
}

const LEVEL_LABEL = { strong: '强关联', medium: '同月' } as const;

/**
 * 放大镜：贴在选中卡片下方（空间不够时翻到上方）。
 *
 * 不占底栏、不占侧栏；只显示强/中关联，弱关联在画布上淡出。
 * 位置用画布像素坐标计算，滚动时重新量卡片位置，避免用 index 猜。
 */
export function MemoryFocusLens({
  model,
  affinity,
  row,
  kindText,
  originText,
  statusText,
  onSelectMemory,
  onOpenDetail,
  onClose,
}: MemoryFocusLensProps): React.ReactElement {
  const lensRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 8, top: 8 });
  const [expanded, setExpanded] = useState(false);

  const links = useMemo(
    () => affinity.links
      .map((link) => ({ link, row: model.nodeMap[link.id]?.row }))
      .filter((item): item is { link: StarAffinity['links'][number]; row: MemoryRecord } => Boolean(item.row)),
    [affinity, model],
  );

  useLayoutEffect(() => {
    const node = model.nodeMap[affinity.selectedId];
    const lens = lensRef.current;
    if (!node || !lens) return;
    const viewport = lens.closest('.mem-star-viewport') as HTMLElement | null;
    const width = Math.min(340, Math.max(280, (viewport?.clientWidth ?? model.canvasWidth) * 0.42));
    const height = Math.min(lens.offsetHeight || 300, 320);
    // 用可见视口定位，而不是整张画布：画布比视口宽，按画布定位会飘到视口外。
    const scrollLeft = viewport?.scrollLeft ?? 0;
    const left = Math.max(scrollLeft + 8, scrollLeft + (viewport?.clientWidth ?? model.canvasWidth) - width - 12);
    const scrollTop = viewport?.scrollTop ?? 0;
    const visibleTop = scrollTop + 8;
    const visibleBottom = scrollTop + (viewport?.clientHeight ?? model.totalHeight) - height - 8;
    // 右侧镜片不能盖住强/中关联卡片：垂直方向避开它们的区间，避不开就贴顶。
    const avoid = affinity.links
      .map((link) => model.nodeMap[link.id])
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => [item.yPx - 10, item.yPx + 92] as const);
    let top = Math.max(visibleTop, Math.min(node.yPx - 40, visibleBottom));
    for (const [a, b] of avoid) {
      if (top < b && top + height > a) {
        top = b + 8 <= visibleBottom ? b + 8 : a - height - 8;
      }
    }
    top = Math.max(visibleTop, Math.min(top, visibleBottom));
    setPosition({ left, top });
  }, [affinity.selectedId, model, links.length]);

  const status = statusText(row);
  const strong = affinity.counts.strong;
  const medium = affinity.counts.medium;
  const shownLinks = links.slice(0, 2);
  const hiddenLinks = links.length - shownLinks.length;

  return (
    <div
      ref={lensRef}
      className="mem-lens mem-lens--side"
      style={{ left: `${position.left}px`, top: `${position.top}px` }}
      role="dialog"
      aria-label={`记忆放大镜：${row.text}`}
      data-testid="mem-focus-lens"
    >
      <div className="mem-lens__head">
        <span>放大镜</span>
        <span className="mem-lens__counts">{strong} 强 · {medium} 同月</span>
        <button type="button" className="mem-lens__close" aria-label="收起放大镜" onClick={onClose}>×</button>
      </div>

      <div className="mem-lens__kind">
        {kindText(row.kind)}{status ? ` · ${status}` : ''}
        {row.origin === 'model_inference' ? ' · 推测，不是事实' : ''}
      </div>
      <h3 className="mem-lens__title">{row.text}</h3>
      <p className="mem-lens__when">
        {memoryOccurredAt(row) ? `发生 ${formatShort(memoryOccurredAt(row))}` : '发生日期未知'}
        {' · '}记下 {formatShort(row.created_at)}
      </p>

      {links.length ? (
        <div className="mem-lens__links" aria-label="关联记忆">
          {shownLinks.map(({ link, row: other }) => (
            <button
              key={link.id}
              type="button"
              className={`mem-lens__link mem-lens__link--${link.level}${link.kind === 'correction' ? ' mem-lens__link--correction' : ''}`}
              onClick={() => onSelectMemory(link.id)}
              title={link.evidence}
            >
              <span className="mem-lens__level">{LEVEL_LABEL[link.level as 'strong' | 'medium']}</span>
              <span className="mem-lens__link-text">{other.text}</span>
              <span className="mem-lens__reason">{link.reason} · {link.evidence}</span>
            </button>
          ))}
          {hiddenLinks > 0 ? <span className="mem-lens__more">还有 {hiddenLinks} 条，打开详情继续看</span> : null}
        </div>
      ) : (
        <p className="mem-lens__empty">没有强关联，也没有同一天/近三天的记录。</p>
      )}

      <div className={`mem-lens__excerpt${expanded ? ' is-open' : ''}`}>
        <p>{row.text}</p>
        <button type="button" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起原文' : '展开原文'}
        </button>
      </div>

      <div className="mem-lens__foot">
        <span className="mem-lens__origin">{originText(row.origin)}</span>
        <button type="button" className="mem-lens__open" onClick={onOpenDetail}>看完整详情 ↗</button>
      </div>
    </div>
  );
}

function formatShort(iso: string | null | undefined): string {
  if (!iso) return '未记录';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '未记录';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())}`;
}
