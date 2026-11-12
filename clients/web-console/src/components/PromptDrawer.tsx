/**
 * 对话页右侧抽屉：「她这次看到了什么」。
 *
 * 用户要的是"她这次回复时到底看到了什么"，不是"系统怎么运行的"。所以这里
 * 只做三件事：按后端给的顺序原样展示注入内容、把机器键换成后端给的人话
 * 标题、把长段落折起来。**不在前端重新编排提示词**——那样看到的就不是她
 * 真正看到的那一份了。
 *
 * 几条硬约束：
 * - 三态（available / expired / unavailable）互不串：后两者各有一句人话，
 *   绝不渲染成"没有条目"的空白；404（消息不存在或不是你的）单独一句
 *   「这次没有记录」。
 * - 未知 `key` / 未知 `state` 不吞：未知分区照样按 label 渲染；未知状态
 *   落到"没有保存下来"而不是空白。
 * - 打开时背景**不做全屏禁用**：不锁 body/html 滚动、不给 app 加 inert，
 *   遮罩只是变淡的一层（`pointer-events: none`），背后的对话仍然可读、可滚。
 *   点"外面"由文档级的 click 捕获处理：关掉抽屉，并拦住这次点击落到背后的
 *   控件上（否则会一边收起一边误触）。
 *
 * 交互（X / Esc / 点遮罩 / 再点入口）与焦点回归由 ChatPage 持有开关、
 * 本组件负责键盘与焦点：入口元素由 ChatPage 在点击时记下，关闭后还焦点。
 * 遮罩不拦指针，所以入口仍然点得到——"同一条再点 = 收起"是真的能点。
 * 仓库没有 jsdom，真实按键由 `prompt.browser.test.tsx` 在浏览器里验证，
 * 纯逻辑（折叠判定、发言人回落、404 映射）在这里导出给单测。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import type {
  PromptItem,
  PromptModelInfo,
  PromptSection,
  PromptSnapshotOut,
  PromptSnapshotState,
} from '../types';
import '../prompt-drawer.css';

export const PROMPT_ENTRY_LABEL = '她这次看到了什么';
export const PROMPT_DRAWER_TITLE = PROMPT_ENTRY_LABEL;
export const PROMPT_DRAWER_ID = 'prompt-drawer-panel';
export const PROMPT_DRAWER_TITLE_ID = 'prompt-drawer-title';

/** 折叠时先露出的条数。0 条会让人以为这一段是空的。 */
export const PROMPT_COLLAPSED_PREVIEW = 2;
/** 超过这个条数的分区默认折叠（`history` 无论多少都折叠）。 */
export const PROMPT_COLLAPSE_ITEM_THRESHOLD = 12;

/** 三态的人话。`available` 没有提示语，正常渲染内容。 */
export const PROMPT_STATE_NOTICE: Record<PromptSnapshotState, string | null> = {
  available: null,
  expired: '这次记录已经过期（保留 7 天）',
  unavailable: '这次没有保存下来',
};

/** 404 的落点文案：消息不存在、已归档、或不是你的消息都归到这里。 */
export const PROMPT_MISSING_TEXT = '这次没有记录';

const ROLE_LABELS: Record<string, string> = {
  system: '系统',
  user: '用户',
  actor: '她',
};

/**
 * 只有部署所有者能看完整提示词（后端对其他人一律 404）。
 * 角色名与后端 `users.role`（owner / member）一致，不做模糊匹配。
 */
export function canOpenPromptSnapshot(role: string | null | undefined): boolean {
  return role === 'owner';
}

/**
 * 同一个入口再点一次是收起，不是重新打开。
 * 抽成纯函数是因为仓库没有 jsdom，真实点击留给浏览器测试。
 */
export function promptEntryAction(
  openMessageKey: string | null,
  messageKey: string,
): 'open' | 'close' {
  return openMessageKey === messageKey ? 'close' : 'open';
}

/**
 * 分区里的条目。契约保证是数组，但旧服务端/半上线期间缺字段时不要整页白屏：
 * 缺了就按空数组渲染，下面会显示「这一段没有内容」，而不是崩掉。
 */
function sectionItems(section: PromptSection): PromptItem[] {
  return Array.isArray(section.items) ? section.items : [];
}

/** `history`、后端建议折叠、或条数超过阈值 —— 三者任一即默认折叠。 */
export function sectionCollapsedByDefault(section: PromptSection): boolean {
  if (section.collapsed === true) return true;
  if (section.key === 'history') return true;
  return sectionItems(section).length > PROMPT_COLLAPSE_ITEM_THRESHOLD;
}

/** 折叠时只给开头几条；展开时全给。 */
export function visiblePromptItems(section: PromptSection, expanded: boolean): PromptItem[] {
  const items = sectionItems(section);
  return expanded ? items : items.slice(0, PROMPT_COLLAPSED_PREVIEW);
}

/**
 * 一条注入项的"谁在说"。
 *
 * 有 `sender` 就用它（群聊里真正的发言人）；否则按 `role` 翻成人话。
 * 两者都没有就返回 null——宁可只显示内容，也不编一个发言人。
 */
export function promptItemSpeaker(item: PromptItem): string | null {
  const sender = (item.sender ?? '').trim();
  if (sender) return sender;
  const role = (item.role ?? '').trim();
  if (!role) return null;
  return ROLE_LABELS[role] ?? role;
}

/**
 * 未知的 state 不吞：落到"没有保存下来"，而不是一块空白。
 *
 * `available` 的提示语就是 `null`（正常渲染内容），所以这里**不能**用
 * `??`——那会把 `null` 当成"没有这个键"，让所有正常快照都显示成
 * 「这次没有保存下来」。用 `hasOwnProperty` 判断键是否存在。
 */
export function promptStateNotice(state: PromptSnapshotState): string | null {
  if (state && Object.prototype.hasOwnProperty.call(PROMPT_STATE_NOTICE, state)) {
    return PROMPT_STATE_NOTICE[state] ?? null;
  }
  return PROMPT_STATE_NOTICE.unavailable;
}

/** 模型标识：`provider/model`；缺一半就只给有的那一半；全缺给 null。 */
export function promptModelLabel(model: PromptModelInfo | null): string | null {
  if (!model) return null;
  const provider = (model.provider_id ?? '').trim();
  const modelId = (model.model_id ?? '').trim();
  if (provider && modelId) return `${provider}/${modelId}`;
  return modelId || provider || null;
}

/** 千分位：token 数是要被"看一眼估大小"的数字，不该数零。 */
export function formatTokenCount(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value);
}

/* ------------------------------------------------------------ 加载与状态 */

export type PromptView =
  | { kind: 'loading' }
  | { kind: 'missing' }
  | { kind: 'error'; error: ExplainedError }
  | { kind: 'snapshot'; snapshot: PromptSnapshotOut };

/**
 * 取快照并把它折成界面的四态。
 *
 * 404 = 「这次没有记录」（消息不存在/已归档/不是你的）。**不能**把 404 当成
 * "读取失败"去劝人重试——重试一百次也还是没有。
 */
export async function loadPromptSnapshot(
  messageId: string | null,
  fetcher: (id: string) => Promise<PromptSnapshotOut> = (id) => api.messagePrompt(id),
): Promise<PromptView> {
  if (!messageId) return { kind: 'missing' };
  try {
    const snapshot = await fetcher(messageId);
    return { kind: 'snapshot', snapshot };
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.code === 'not_found')) {
      return { kind: 'missing' };
    }
    return { kind: 'error', error: explainError(error) };
  }
}

/* ------------------------------------------------------------ 渲染 */

function PromptItemRow({ item }: { item: PromptItem }) {
  const speaker = promptItemSpeaker(item);
  return (
    <p
      className="prompt-drawer__item"
      data-testid="prompt-item"
      title={item.at ? formatSessionTime(item.at) : undefined}
    >
      {speaker ? <span className="prompt-drawer__item-speaker">{speaker}：</span> : null}
      <span className="prompt-drawer__item-content">{item.content}</span>
    </p>
  );
}

export interface PromptSectionBlockProps {
  section: PromptSection;
  expanded: boolean;
  onToggle: () => void;
}

/**
 * 一个分区。`expanded` 是**生效值**（默认折叠由父组件算），这样测试能直接
 * 渲染"折叠/展开"两态，不必模拟点击。
 */
export function PromptSectionBlock({ section, expanded, onToggle }: PromptSectionBlockProps) {
  const allItems = sectionItems(section);
  const collapsible = allItems.length > PROMPT_COLLAPSED_PREVIEW;
  const shown = collapsible ? visiblePromptItems(section, expanded) : allItems;
  const label = (section.label ?? '').trim() || section.key;
  const tokens = section.tokens;
  return (
    <section
      className="prompt-drawer__section"
      data-testid="prompt-section"
      data-key={section.key}
      data-collapsed={collapsible && !expanded ? 'true' : 'false'}
    >
      <header className="prompt-drawer__section-head">
        <h3 className="prompt-drawer__section-title">{label}</h3>
        {typeof tokens === 'number' && Number.isFinite(tokens) ? (
          <span className="prompt-drawer__section-tokens" data-testid="prompt-section-tokens">
            约 {formatTokenCount(tokens)} tokens
          </span>
        ) : null}
      </header>
      {shown.length > 0 ? (
        shown.map((item, index) => (
          <PromptItemRow key={`${section.key}-${index}`} item={item} />
        ))
      ) : (
        <p className="prompt-drawer__empty-section">这一段没有内容</p>
      )}
      {collapsible ? (
        <button
          type="button"
          className="prompt-drawer__more"
          data-testid="prompt-section-toggle"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? '收起' : `展开全部（${allItems.length} 条）`}
        </button>
      ) : null}
    </section>
  );
}

function PromptSummary({ snapshot }: { snapshot: PromptSnapshotOut }) {
  const modelLabel = promptModelLabel(snapshot.model);
  const profile = (snapshot.model?.profile ?? '').trim();
  return (
    <dl className="prompt-drawer__summary" data-testid="prompt-summary">
      {modelLabel ? (
        <div className="prompt-drawer__summary-item">
          <dt>模型</dt>
          <dd className="prompt-drawer__summary-model" title={modelLabel}>
            {modelLabel}
          </dd>
        </div>
      ) : null}
      {profile ? (
        <div className="prompt-drawer__summary-item">
          <dt>职责</dt>
          <dd>{profile}</dd>
        </div>
      ) : null}
      {typeof snapshot.token_total === 'number' && Number.isFinite(snapshot.token_total) ? (
        <div className="prompt-drawer__summary-item">
          <dt>总 token</dt>
          <dd data-testid="prompt-token-total">约 {formatTokenCount(snapshot.token_total)} tokens</dd>
        </div>
      ) : null}
      {snapshot.created_at ? (
        <div className="prompt-drawer__summary-item">
          <dt>创建于</dt>
          <dd>{formatSessionTime(snapshot.created_at)}</dd>
        </div>
      ) : null}
    </dl>
  );
}

export interface PromptDrawerViewProps {
  view: PromptView;
  /** 分区展开状态：键是 `index:section.key`。缺省 = 按默认折叠规则。 */
  expanded?: Record<string, boolean>;
  onToggleSection?: (key: string) => void;
  onRetry?: () => void;
}

/** 分区在展开表里的键：`key` 可能重复，带上下标才稳定。 */
export function promptSectionKey(index: number, section: PromptSection): string {
  return `${index}:${section.key}`;
}

export function PromptDrawerView({
  view,
  expanded = {},
  onToggleSection,
  onRetry,
}: PromptDrawerViewProps) {
  if (view.kind === 'loading') {
    return (
      <p className="prompt-drawer__loading" data-testid="prompt-loading">
        <span className="spinner" aria-hidden="true" />
        正在读取这次的内容…
      </p>
    );
  }

  if (view.kind === 'missing') {
    return (
      <div className="prompt-drawer__notice" data-testid="prompt-missing">
        <p className="prompt-drawer__notice-title">{PROMPT_MISSING_TEXT}</p>
        <p className="prompt-drawer__notice-hint">
          可能这条消息已经被清理，或它不属于你。
        </p>
      </div>
    );
  }

  if (view.kind === 'error') {
    return (
      <div className="prompt-drawer__notice prompt-drawer__notice--error" data-testid="prompt-error">
        <p className="prompt-drawer__notice-title">{view.error.reason}</p>
        {view.error.hint ? (
          <p className="prompt-drawer__notice-hint">{view.error.hint}</p>
        ) : null}
        {view.error.detail ? (
          <p className="prompt-drawer__notice-detail">{view.error.detail}</p>
        ) : null}
        {onRetry ? (
          <button type="button" className="button button--small" onClick={onRetry}>
            重新读取
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <PromptSnapshotBody
      snapshot={view.snapshot}
      expanded={expanded}
      {...(onToggleSection ? { onToggleSection } : {})}
    />
  );
}

export interface PromptSnapshotBodyProps {
  snapshot: PromptSnapshotOut;
  expanded?: Record<string, boolean>;
  onToggleSection?: (key: string) => void;
}

/**
 * 快照正文。三态在这里分叉：`available` 才渲染分区，另外两态只给一句人话。
 * 单独导出是为了让 SSR 测试能直接喂三态，而不必等异步加载。
 */
export function PromptSnapshotBody({
  snapshot,
  expanded = {},
  onToggleSection,
}: PromptSnapshotBodyProps) {
  const notice = promptStateNotice(snapshot.state);
  if (notice) {
    return (
      <div
        className="prompt-drawer__notice"
        data-testid={`prompt-state-${snapshot.state}`}
        data-state={snapshot.state}
      >
        <p className="prompt-drawer__notice-title">{notice}</p>
        {snapshot.state === 'expired' && snapshot.expires_at ? (
          <p className="prompt-drawer__notice-hint">
            记录保留至 {formatSessionTime(snapshot.expires_at)}。
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="prompt-drawer__snapshot" data-testid="prompt-snapshot">
      <PromptSummary snapshot={snapshot} />
      {snapshot.truncated ? (
        <p className="prompt-drawer__truncated" data-testid="prompt-truncated">
          这次内容太长，只保留了开头和最后几条
        </p>
      ) : null}
      {!Array.isArray(snapshot.sections) || snapshot.sections.length === 0 ? (
        <p className="prompt-drawer__empty-section">这次没有注入任何内容</p>
      ) : (
        snapshot.sections.map((section, index) => {
          const key = promptSectionKey(index, section);
          const defaultExpanded = !sectionCollapsedByDefault(section);
          const isExpanded = expanded[key] ?? defaultExpanded;
          return (
            <PromptSectionBlock
              key={key}
              section={section}
              expanded={isExpanded}
              onToggle={() => onToggleSection?.(key)}
            />
          );
        })
      )}
      {snapshot.tool_transcript?.length || snapshot.tool_transcript_truncated ? (
        <section className="prompt-drawer__section" aria-label="工具回灌">
          <header className="prompt-drawer__section-head">
            <h3 className="prompt-drawer__section-title">工具回灌</h3>
          </header>
          {snapshot.tool_transcript?.map((result) => (
            <details
              className="prompt-drawer__item"
              key={`${result.step}-${result.provider_call_id}`}
            >
              <summary>
                第 {result.step} 步 · {result.tool_id} · {result.outcome}
              </summary>
              <div className="prompt-drawer__item-content">{result.content}</div>
            </details>
          ))}
          {snapshot.tool_transcript_truncated ? (
            <p className="prompt-drawer__truncated">部分工具结果超过快照容量，未保留。</p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------ 抽屉 */

export interface PromptDrawerProps {
  open: boolean;
  /** 要查快照的消息 id。本地尚未落库的消息是 null → 直接「这次没有记录」。 */
  messageId: string | null;
  onClose: () => void;
}

function focusablesIn(panel: HTMLElement): HTMLElement[] {
  const selector =
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  return Array.from(panel.querySelectorAll<HTMLElement>(selector)).filter(
    (node) => node.getClientRects().length > 0,
  );
}

export function PromptDrawer({ open, messageId, onClose }: PromptDrawerProps) {
  const [view, setView] = useState<PromptView>({ kind: 'loading' });
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [reloadToken, setReloadToken] = useState(0);
  const panelRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setView({ kind: 'loading' });
    setExpanded({});
    void loadPromptSnapshot(messageId).then((next) => {
      if (!cancelled) setView(next);
    });
    return () => {
      cancelled = true;
    };
  }, [open, messageId, reloadToken]);

  // 打开时焦点进抽屉（落在关闭键上，读屏第一句就是标题）；Esc 收起。
  // Tab 在抽屉内循环：`aria-modal` 已经把"外面不许交互"告诉读屏，
  // 键盘也要做到同一件事，否则焦点会跑到背后被遮住的内容上。
  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();

    /*
     * 点外面收起。
     *
     * 遮罩是 `pointer-events: none`（见 CSS）：如果让遮罩吃掉指针，滚轮就
     * 滚不动背后的对话列表——需求明确要求"不阻塞对话页滚动"。所以用文档级
     * 捕获阶段的 click 判断"在不在抽屉里"：
     * - 在抽屉里：什么都不做，按钮自己处理；
     * - 在入口上：放行，入口的 onClick 会切到收起（同一条再点 = 收起）；
     * - 其它地方：收起，并 stopPropagation/preventDefault，免得这次点击
     *   又激活了背后被遮住的控件。
     */
    const onDocumentClick = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (panelRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest('[data-testid="prompt-entry"]')) return;
      event.stopPropagation();
      event.preventDefault();
      onClose();
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusables = focusablesIn(panel);
      if (focusables.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;
      if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (
        event.shiftKey &&
        (document.activeElement === first || document.activeElement === panel)
      ) {
        event.preventDefault();
        last.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('click', onDocumentClick, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('click', onDocumentClick, true);
    };
  }, [open, onClose]);

  const toggleSection = useCallback((key: string) => {
    setExpanded((current) => {
      const next = { ...current };
      if (next[key]) delete next[key];
      else next[key] = true;
      return next;
    });
  }, []);

  if (!open) return null;

  return (
    <div className="prompt-drawer" data-testid="prompt-drawer-overlay">
      {/*
        遮罩只是"变淡"与"点外收起"的视觉提示，不接指针（pointer-events:
        none）：滚轮/触控滚动仍然落在背后的对话列表上。真正的收起判定在
        上面的 document click 捕获里。`aria-hidden` 因为它不是控件。
      */}
      <div className="prompt-drawer__scrim" data-testid="prompt-scrim" aria-hidden="true" />
      <aside
        className="prompt-drawer__panel"
        id={PROMPT_DRAWER_ID}
        role="dialog"
        aria-modal="true"
        aria-labelledby={PROMPT_DRAWER_TITLE_ID}
        data-testid="prompt-drawer"
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="prompt-drawer__head">
          <div className="prompt-drawer__head-text">
            <h2 className="prompt-drawer__title" id={PROMPT_DRAWER_TITLE_ID}>
              {PROMPT_DRAWER_TITLE}
            </h2>
            <p className="prompt-drawer__sub">这一轮注入给她的完整内容与上下文</p>
          </div>
          <button
            type="button"
            className="prompt-drawer__close"
            data-testid="prompt-drawer-close"
            aria-label="收起"
            ref={closeRef}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="prompt-drawer__body" data-testid="prompt-drawer-body">
          <PromptDrawerView
            view={view}
            expanded={expanded}
            onToggleSection={toggleSection}
            onRetry={() => setReloadToken((value) => value + 1)}
          />
        </div>
      </aside>
    </div>
  );
}
