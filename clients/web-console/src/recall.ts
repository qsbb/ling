/**
 * 「她想撤回这条」的渲染逻辑（纯函数，可单测）。
 *
 * ## 为什么单独一个模块
 *
 * 撤回是**不可逆**动作，而它的三个判据都不能靠肉眼：
 *
 * 1. **过期**：平台窗口是 120 秒，超时点下去必然失败。判定要用**服务端时钟**
 *    ——浏览器时钟可能时区设错、休眠后漂移、被手动改过，用它减服务端的
 *    `created_at` 会把"还来得及"算成"来不及"，或者反过来让用户点一个注定
 *    失败的按钮。这里的做法是：拿响应头 `Date` 当锚点，加上"从拿到响应到现在
 *    过了多久"（`Date.now()` 的差值，只用于短区间，绝对时刻不参与）。
 * 2. **去重**：同一轮 final 可能重复到达（幂等重放、断线重连），同一
 *    `proposal_id` 出现两张卡片会让用户以为她提了两次。
 * 3. **终态不许被翻案**：已经确认/拒绝/过期的卡片，不能因为下一次列表刷新又
 *    变回"待确认"——那等于让人重复点一个已经执行过的不可逆动作。
 *
 * 这三条都在这里用纯函数表达，`useChat` 与卡片组件只负责调用。
 */
import type { RecallProposal, RecallProposalRef } from './types';

/** 平台允许撤回的时间窗（与后端 `domains/conversation/recall.py` 同一数值）。 */
export const RECALL_WINDOW_MS = 120_000;

/**
 * 卡片的可见状态。
 *
 * - `proposed`：可以点（还在窗口内）；
 * - `pending`：点了，请求在飞；
 * - `confirmed`：真的撤回了；
 * - `declined`：用户说"算了"；
 * - `expired`：超过平台窗口 / 服务端明确回了 expired——**按钮不可点**；
 * - `failed`：撤不成（渠道不支持、没拿到回执、目标已不在），显示后端人话。
 */
export type RecallCardStatus =
  | 'proposed'
  | 'pending'
  | 'confirmed'
  | 'declined'
  | 'expired'
  | 'failed'
  /** 服务端说"没有这条提议了"（已被别处处理/清理）：卡片留着但不可点。 */
  | 'gone';

/** 已经处理完的状态：列表刷新不许把它们改回 `proposed`。 */
const TERMINAL: ReadonlySet<RecallCardStatus> = new Set([
  'confirmed',
  'declined',
  'expired',
  'failed',
  'gone',
]);

export interface RecallProposalView {
  proposalId: string;
  /** 她想撤的那条本地消息 id（卡片挂在这条消息下面）。 */
  messageId: string;
  /** 后端的人话说明（"她要撤回这条，确认吗？"）。 */
  note: string;
  /** 提议落库时刻（服务端时间，ISO 串）。 */
  createdAt: string;
  /**
   * 拿到这条提议时服务端的"现在"（HTTP `Date` 头，毫秒）。
   * `null` = 拿不到（代理剥掉 / 非标准响应），此时退回浏览器时钟并标注。
   */
  serverNowMs: number | null;
  /** 拿到这条提议时的本地时刻；只用来算"又过了多久"，不参与绝对时间。 */
  anchorLocalMs: number;
  status: RecallCardStatus;
  /** 确认失败时后端给的人话（`note` 原文，界面不显示 `reason_code`）。 */
  failureNote: string | null;
}

function parseMs(value: string): number | null {
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * 从提议落库到现在过了多少毫秒。
 *
 * 服务端锚点可用时：`(serverNow − created) + (now − anchorLocal)`。
 * 前半段是服务端自己的两个时刻之差（时钟歪了也互相抵消），后半段是短区间
 * 的本地差值（两分钟内 `Date.now()` 足够稳）。这样**绝对时钟从不参与判断**。
 *
 * 拿不到服务端时间时退回 `now − created`：不理想，但总比"永远可点"安全——
 * 调用方用 `recallClockSource` 把这件事说出来。
 */
export function recallElapsedMs(view: RecallProposalView, nowMs: number): number {
  const created = parseMs(view.createdAt);
  // 解析不了就等于"不知道多久了"：按已过期处理（保守），不让点。
  if (created === null) return RECALL_WINDOW_MS;
  if (view.serverNowMs === null) return nowMs - created;
  const atReceipt = view.serverNowMs - created;
  const sinceReceipt = nowMs - view.anchorLocalMs;
  // 负数 = created_at 比服务端"现在"还晚（亚秒舍入 / 两端毫秒不同步）。
  // 这种情况一定还在窗口内，夹到 0，别把它算成"刚过期"。
  return Math.max(0, atReceipt) + Math.max(0, sinceReceipt);
}

/** 这条提议用的是哪边的时钟——只用于提示文案，不改变判定。 */
export function recallClockSource(view: RecallProposalView): 'server' | 'browser' {
  return view.serverNowMs === null ? 'browser' : 'server';
}

export function recallExpired(view: RecallProposalView, nowMs: number): boolean {
  return recallElapsedMs(view, nowMs) >= RECALL_WINDOW_MS;
}

/** 还能撤几秒（向上取整，最小 0）；只在窗口内才有意义。 */
export function recallRemainingSeconds(view: RecallProposalView, nowMs: number): number {
  const remaining = RECALL_WINDOW_MS - recallElapsedMs(view, nowMs);
  return remaining <= 0 ? 0 : Math.ceil(remaining / 1000);
}

/**
 * 卡片此刻该显示成什么样。
 *
 * 终态原样返回；`proposed` 到了窗口外自动变 `expired`——**渲染层自己判**，
 * 不靠服务端再下发一次。否则页面开着不动，那张卡片会一直显示"可点"。
 */
export function recallCardState(view: RecallProposalView, nowMs: number): RecallCardStatus {
  if (view.status !== 'proposed') return view.status;
  return recallExpired(view, nowMs) ? 'expired' : 'proposed';
}

/** 卡片上那句状态文案（人话，不出现原因码）。 */
export function recallStatusText(status: RecallCardStatus, view?: RecallProposalView, nowMs?: number): string {
  switch (status) {
    case 'proposed': {
      const seconds = view && nowMs !== undefined ? recallRemainingSeconds(view, nowMs) : null;
      return seconds === null ? '还在撤回时限内' : `还能撤（还有 ${seconds} 秒）`;
    }
    case 'pending':
      return '正在撤回…';
    case 'confirmed':
      return '已撤回';
    case 'declined':
      return '好，不撤了';
    case 'expired':
      return '已经来不及撤了';
    case 'failed':
      return view?.failureNote ?? '没能撤回';
    case 'gone':
      return '这条提议已经处理过了';
  }
}

/**
 * 合并一批提议，**按 `proposal_id` 去重**。
 *
 * 规则：
 * - 已有的终态（确认/拒绝/过期/失败）不被新来的 `proposed` 覆盖；
 * - 已存在的 `pending` 也保留（请求在飞时列表刷新不该把按钮弹回来）；
 * - 新的 id 追加在后面（先出现的在上）。
 */
export function mergeRecallProposals(
  existing: RecallProposalView[],
  incoming: RecallProposalView[],
): RecallProposalView[] {
  const byId = new Map<string, RecallProposalView>();
  const order: string[] = [];
  for (const view of existing) {
    if (!byId.has(view.proposalId)) order.push(view.proposalId);
    byId.set(view.proposalId, view);
  }
  for (const view of incoming) {
    const current = byId.get(view.proposalId);
    if (current === undefined) {
      byId.set(view.proposalId, view);
      order.push(view.proposalId);
      continue;
    }
    if (TERMINAL.has(current.status) || current.status === 'pending') continue;
    // 还是 proposed：用新数据（`created_at` 更权威，锚点也更新）。
    byId.set(view.proposalId, view);
  }
  return order.map((id) => byId.get(id) as RecallProposalView);
}

/** 列表接口的一项 → 视图。`serverNowMs` 来自响应头，`nowMs` 是本地时刻。 */
export function proposalFromServer(
  proposal: RecallProposal,
  serverNowMs: number | null,
  nowMs: number,
): RecallProposalView {
  return {
    proposalId: proposal.proposal_id,
    messageId: proposal.message_id,
    note: proposal.note || '她要撤回这条，确认吗？',
    createdAt: proposal.created_at,
    serverNowMs,
    anchorLocalMs: nowMs,
    status: 'proposed',
    failureNote: null,
  };
}

/**
 * 流里的一条提议 → 视图。
 *
 * 流里**没有** `created_at`（那是落库之后才有的字段），只能用她这条回复的
 * 落库时刻当代理——提议正是在那一轮里写下来的，两者相差不到一秒。随后
 * `useChat` 会再拉一次列表把权威值换进来，所以这里只求"卡片立刻出现"。
 */
export function proposalFromStream(
  ref: RecallProposalRef,
  fallbackCreatedAt: string,
  serverNowMs: number | null,
  nowMs: number,
): RecallProposalView {
  return {
    proposalId: ref.proposal_id,
    messageId: ref.message_id,
    note: ref.note || '她要撤回这条，确认吗？',
    createdAt: fallbackCreatedAt,
    serverNowMs,
    anchorLocalMs: nowMs,
    status: 'proposed',
    failureNote: null,
  };
}

/**
 * 从 `turn.completed` 的 `recall_proposals` 建出一批视图。
 *
 * 这一步是**原来断掉的那一环**：`applyFinal` 曾经完全不读这个字段，于是
 * 提议在库里发霉。抽成纯函数是为了让"字段 → 卡片"这段能被单测直接钉住，
 * 而不是只能靠肉眼看 SSE 日志。
 *
 * 入参做防御性过滤：字段缺失、`proposal_id` 不是字符串、`message_id` 空的
 * 条目一律丢掉——宁可少画一张卡，也不要画一张点不动的。
 */
export function proposalsFromFinal(
  refs: unknown,
  fallbackCreatedAt: string,
  serverNowMs: number | null,
  nowMs: number,
): RecallProposalView[] {
  if (!Array.isArray(refs)) return [];
  const out: RecallProposalView[] = [];
  for (const raw of refs) {
    if (!raw || typeof raw !== 'object') continue;
    const ref = raw as Partial<RecallProposalRef>;
    if (typeof ref.proposal_id !== 'string' || !ref.proposal_id) continue;
    if (typeof ref.message_id !== 'string' || !ref.message_id) continue;
    out.push(
      proposalFromStream(
        {
          proposal_id: ref.proposal_id,
          message_id: ref.message_id,
          ...(typeof ref.note === 'string' ? { note: ref.note } : {}),
        },
        fallbackCreatedAt,
        serverNowMs,
        nowMs,
      ),
    );
  }
  return out;
}

/** 把一条提议推进到某个终态（不可变更新）。 */
export function withStatus(
  view: RecallProposalView,
  status: RecallCardStatus,
  failureNote: string | null = null,
): RecallProposalView {
  return { ...view, status, failureNote };
}
