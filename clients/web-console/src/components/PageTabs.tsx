/**
 * 领域页的 tab 条（人设页、生活页共用）。
 *
 * 为什么需要 tab：设置瘦身（DESIGN §2）把 ④记忆与学习、⑤对话与主动性、
 * ⑧生活与活动 三份调参迁回了它们所属的领域页。迁回之后那三页各自有两种内容
 * （「看」与「调」），tab 是唯一不把两件事堆在同一屏里的做法。
 *
 * 记忆页有自己的 `.mem-tabs`（`MemoryPage.tsx` 被并行任务锁着，本轮不动它）：
 * 结构与这里等价（role="tablist" + aria-selected + `?tab=` 深链），
 * 只是类名前缀不同。合并两份要等记忆页解锁之后。
 *
 * 两个硬约定：
 * 1. **tab 是 URL，不是内部状态**：`onSelect` 由调用方 `navigate(...)` 实现，
 *    这样刷新/后退/分享链接都停在同一个 tab（DESIGN §1 明确不引 hash 路由）。
 * 2. **面板用 `hidden` 而不是卸载**：`aria-controls` 指向的面板必须一直在 DOM 里，
 *    否则辅助技术会拿到一个指向空处的 `aria-controls`（记忆页也是这么做的）。
 *    内容本身仍按需渲染（未激活的 tab 不跑自己的请求）。
 */
import type { ReactElement, ReactNode } from 'react';
import '../settings.css';

export interface PageTabSpec<T extends string> {
  key: T;
  label: string;
  /** E2E / 单测的公开选择器；改名必须同步改测试。 */
  testid: string;
}

interface PageTabsProps<T extends string> {
  /** `aria-label`，例如「人设页分区」。 */
  label: string;
  tabs: readonly PageTabSpec<T>[];
  current: T;
  onSelect: (tab: T) => void;
}

export function PageTabs<T extends string>({
  label,
  tabs,
  current,
  onSelect,
}: PageTabsProps<T>): ReactElement {
  return (
    <div className="domain-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          role="tab"
          id={`domain-tab-${tab.key}`}
          aria-selected={current === tab.key}
          aria-controls={`domain-panel-${tab.key}`}
          className={`domain-tab${current === tab.key ? ' domain-tab--active' : ''}`}
          data-testid={tab.testid}
          onClick={() => onSelect(tab.key)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

/**
 * 一个 tab 面板。未激活时 `hidden`（内容也不渲染）——`hidden` 必须留着，
 * 它让 `aria-controls` 始终指向一个存在的元素。
 */
export function PageTabPanel<T extends string>({
  tab,
  current,
  children,
}: {
  tab: T;
  current: T;
  children: ReactNode;
}): ReactElement {
  return (
    <div
      id={`domain-panel-${tab}`}
      className="domain-tabpanel"
      role="tabpanel"
      aria-labelledby={`domain-tab-${tab}`}
      hidden={current !== tab}
    >
      {current === tab ? children : null}
    </div>
  );
}
