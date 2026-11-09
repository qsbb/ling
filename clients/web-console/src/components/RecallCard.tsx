/**
 * 「她想撤回这条」的确认卡片。
 *
 * ## 为什么必须出现在界面上
 *
 * 她不能自己撤回（不可逆动作要人批准）：她只能在回复里写标记，后端把它落成
 * 一条 `proposed` 提议。**这条提议如果没人看见，就等于她永远没法收回说错的
 * 话**——功能等于没做。所以卡片挂在"她想撤的那条消息"下面，紧挨着它，
 * 而不是塞进某个设置页。
 *
 * ## 几条硬约束
 *
 * - **过期不装作能点**：平台窗口 120 秒，超时按钮直接消失、文案
 *   「已经来不及撤了」。让用户点一个注定失败的按钮，比一开始就说"来不及"更糟。
 * - **不显示原因码**：失败时显示后端 `note` 的人话原文；`reason_code` 只给日志。
 * - **不可逆要有二段**：点「撤回」后按钮立刻禁用并显示「正在撤回…」，防止
 *   双击变成两次请求（后端也有条件更新兜底，但界面不该鼓励这种操作）。
 * - **状态变化要能播报**：`aria-live="polite"` 让读屏用户听到"已撤回"，
 *   而不是视觉上悄悄变了。
 */
import { useEffect, useState } from 'react';
import {
  recallCardState,
  recallClockSource,
  recallStatusText,
  type RecallCardStatus,
  type RecallProposalView,
} from '../recall';

export interface RecallCardProps {
  proposal: RecallProposalView;
  /** 被撤那条的正文摘要（后端不提供正文，由调用方从消息列表里取）。 */
  excerpt: string | null;
  /** 点「撤回」。调用方把状态推进到 pending → confirmed/failed/expired。 */
  onConfirm: (proposal: RecallProposalView) => void;
  /** 点「算了」。 */
  onDecline: (proposal: RecallProposalView) => void;
  /**
   * 当前时刻（毫秒）。默认 `Date.now`；单测注入固定时钟。
   *
   * 写成"函数"而不是数字：数字每次渲染都变，会导致"父渲染 → 子 effect 重跑
   * → 再渲染"的抖动；函数只在 tick 里调用，稳定。
   */
  clock?: () => number;
}

/** 倒计时每秒走一格就够——「还能撤 118 秒」不需要毫秒。 */
const TICK_MS = 1_000;

/**
 * 只在**还能点**的时候走秒。
 *
 * 终态卡片开着定时器只会白白重渲染；页面在后台时浏览器会节流，
 * 切回来时立刻重算一次，正好是想要的。
 */
function useRecallTick(active: boolean, clock: () => number): number {
  const [now, setNow] = useState<number>(() => clock());
  useEffect(() => {
    if (!active) return;
    setNow(clock());
    const timer = window.setInterval(() => setNow(clock()), TICK_MS);
    return () => window.clearInterval(timer);
    // `clock` 由调用方保持稳定（默认 `Date.now`）；变化时重开定时器也安全。
  }, [active, clock]);
  return active ? now : clock();
}

const STATUS_LABEL: Record<RecallCardStatus, string> = {
  proposed: '等你确认',
  pending: '正在撤回',
  confirmed: '已撤回',
  declined: '已放弃',
  expired: '已过期',
  failed: '没撤成',
  gone: '已处理',
};

export function RecallCard({ proposal, excerpt, onConfirm, onDecline, clock = Date.now }: RecallCardProps) {
  const active = proposal.status === 'proposed';
  const now = useRecallTick(active, clock);
  const effective = recallCardState(proposal, now);
  const actionable = effective === 'proposed';
  const busy = effective === 'pending';
  const text = recallStatusText(effective, proposal, now);
  const browserClock = recallClockSource(proposal) === 'browser';

  return (
    <div
      className="recall-card"
      data-status={effective}
      data-testid="recall-card"
      data-proposal-id={proposal.proposalId}
      role="group"
      aria-label={`她想撤回这条消息：${text}`}
    >
      <div className="recall-card__head">
        <span className="recall-card__title">她想撤回这条</span>
        <span className="recall-card__state">{STATUS_LABEL[effective]}</span>
      </div>

      {excerpt ? (
        // 引用被撤那条的摘要：让人确认"要撤的到底是哪一句"。
        // 截断交给 CSS（两行），正文本身不被改写。
        <p className="recall-card__excerpt" data-testid="recall-excerpt">
          {excerpt}
        </p>
      ) : (
        <p className="recall-card__excerpt recall-card__excerpt--missing">
          那条消息不在这一屏里了，仍然可以撤回。
        </p>
      )}

      {/* 状态行：变化要能播报（"已撤回" / "已经来不及撤了"）。 */}
      <p className="recall-card__status" aria-live="polite" data-testid="recall-status">
        {text}
        {browserClock && actionable ? (
          // 拿不到服务端时间时如实说明：判定可能偏几十秒，别装作很确定。
          <span className="recall-card__hint">（按本机时间估算）</span>
        ) : null}
      </p>

      {effective === 'failed' && proposal.failureNote ? (
        <p className="recall-card__note" role="note" data-testid="recall-note">
          {proposal.failureNote}
        </p>
      ) : null}

      {actionable || busy ? (
        <div className="recall-card__actions">
          <button
            type="button"
            className="button button--small button--primary"
            data-testid="recall-confirm"
            disabled={busy}
            onClick={() => onConfirm(proposal)}
          >
            {busy ? '正在撤回…' : '撤回'}
          </button>
          <button
            type="button"
            className="button button--small"
            data-testid="recall-decline"
            disabled={busy}
            onClick={() => onDecline(proposal)}
          >
            算了
          </button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * 列表接口失败时的降级卡片。
 *
 * 只占卡片区：聊天本身照常（撤回提议读不到，不影响收发消息）。
 */
export function RecallUnavailableCard({ onRetry }: { onRetry?: () => void }) {
  return (
    <div
      className="recall-card recall-card--unavailable"
      role="status"
      data-testid="recall-unavailable"
    >
      <span>暂时读不到撤回提议。</span>
      {onRetry ? (
        <button type="button" className="button button--small" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}
