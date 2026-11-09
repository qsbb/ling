/**
 * 「她想撤回这条」的回归测试（用户需求：撤回提议必须能被看见、能确认）。
 *
 * 这条链路原本是**断的**：后端在 `turn.completed` 里下发 `recall_proposals`，
 * 前端 `applyFinal` 一个字都不读，于是她提议撤回之后，界面上永远不会出现
 * 任何东西——提议在库里发霉，功能等于没做。
 *
 * 这里钉住六件事，每一条都能独立失败：
 *
 * 1. 流里带一条提议 → 出现卡片（挂在**她想撤的那条**下面）；
 * 2. 同一个 `proposal_id` 的 final 重复到达 → 只有一张卡片；
 * 3. `confirm` 成功 → 「已撤回」且按钮消失；
 * 4. `confirm` 失败 → 显示后端 `note` 原文，**不出现 `reason_code`**；
 * 5. `decline` → 「好，不撤了」；
 * 6. 超过 120 秒 → 按钮不可点 + 「已经来不及撤了」（而不是"能点但失败"）。
 *
 * 另外覆盖：列表接口失败只降级卡片区、不影响聊天；375/1440 无横向溢出
 * （交给 `recall.browser.test.tsx`）。
 *
 * 与仓库其它测试一致：不引入 DOM 测试库，用 `react-dom/server` 渲染成字符串
 * 断言结构；纯逻辑（时钟、去重、状态机）直接调函数。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { MessageList } from './components/MessageList';
import { RecallCard } from './components/RecallCard';
import {
  mergeRecallProposals,
  proposalFromServer,
  proposalFromStream,
  recallCardState,
  recallClockSource,
  recallElapsedMs,
  recallExpired,
  recallRemainingSeconds,
  proposalsFromFinal,
  recallStatusText,
  RECALL_WINDOW_MS,
  withStatus,
  type RecallProposalView,
} from './recall';
import type { RecallProposal } from './types';
import type { UiMessage } from './useChat';

vi.mock('./personaName', () => ({ usePersonaName: () => '灵' }));

const T0 = Date.parse('2026-11-09T02:00:00Z');

function proposalView(patch: Partial<RecallProposalView> = {}): RecallProposalView {
  return {
    proposalId: 'recall_1',
    messageId: 'msg_1',
    note: '她要撤回这条，确认吗？',
    createdAt: new Date(T0).toISOString(),
    serverNowMs: T0,
    anchorLocalMs: T0,
    status: 'proposed',
    failureNote: null,
    ...patch,
  };
}

function messageFixture(patch: Partial<UiMessage> & { key: string }): UiMessage {
  const { key, ...rest } = patch;
  return {
    key,
    messageId: key,
    turnId: null,
    clientTurnId: null,
    role: 'actor',
    system: false,
    text: '内容',
    state: 'completed',
    createdAt: new Date(T0).toISOString(),
    modelLabel: null,
    errorCode: null,
    errorMessage: null,
    errorDetail: null,
    errorTraceId: null,
    retriable: false,
    local: false,
    attempt: 1,
    silenceReason: null,
    senderName: null,
    senderId: null,
    recalledAt: null,
    recalledByActor: false,
    ...rest,
  };
}

function renderCard(
  proposal: RecallProposalView,
  opts: { excerpt?: string | null; nowMs?: number } = {},
): string {
  const fixed = opts.nowMs ?? T0 + 5_000;
  return renderToStaticMarkup(
    <RecallCard
      proposal={proposal}
      excerpt={opts.excerpt === undefined ? '刚才那句说错了' : opts.excerpt}
      onConfirm={() => undefined}
      onDecline={() => undefined}
      clock={() => fixed}
    />,
  );
}

function renderList(
  messages: UiMessage[],
  proposals: RecallProposalView[],
  extra: { nowMs?: number; unavailable?: boolean } = {},
): string {
  const fixed = extra.nowMs ?? T0 + 5_000;
  return renderToStaticMarkup(
    <MessageList
      messages={messages}
      activeTurn={null}
      live={null}
      loading={false}
      onRetry={() => undefined}
      onResend={() => undefined}
      onStop={() => undefined}
      onOpenSettings={() => undefined}
      recallProposals={proposals}
      recallUnavailable={extra.unavailable ?? false}
      onReloadRecall={() => undefined}
      onConfirmRecall={() => undefined}
      onDeclineRecall={() => undefined}
      recallClock={() => fixed}
    />,
  );
}

function countOf(html: string, needle: string): number {
  return html.split(needle).length - 1;
}

/* ------------------------------------------------------------------ 时钟 */

describe('过期判定：用服务端时钟，不用浏览器绝对时间', () => {
  it('服务端锚点 + 短区间本地差值：119 秒仍可撤，120 秒即过期', () => {
    const view = proposalView();
    expect(recallElapsedMs(view, T0 + 119_000)).toBe(119_000);
    expect(recallExpired(view, T0 + 119_000)).toBe(false);
    expect(recallExpired(view, T0 + RECALL_WINDOW_MS)).toBe(true);
    expect(recallRemainingSeconds(view, T0 + 1_000)).toBe(119);
    expect(recallRemainingSeconds(view, T0 + 130_000)).toBe(0);
  });

  it('浏览器时钟被调快/调慢都不影响：只用它算"拿到响应之后过了多久"', () => {
    // 假设本机时钟比服务端快 3 小时：anchorLocalMs 与 nowMs 都在这套时钟上，
    // 差值仍然正确；createdAt / serverNowMs 是服务端时钟，两边不混算。
    const skew = 3 * 3600_000;
    const view = proposalView({ anchorLocalMs: T0 + skew, serverNowMs: T0 });
    expect(recallElapsedMs(view, T0 + skew + 30_000)).toBe(30_000);
    expect(recallExpired(view, T0 + skew + 30_000)).toBe(false);
    expect(recallExpired(view, T0 + skew + 121_000)).toBe(true);
  });

  it('拿不到服务端时间时退回本机时钟，并如实标注', () => {
    const view = proposalView({ serverNowMs: null });
    expect(recallClockSource(view)).toBe('browser');
    expect(recallExpired(view, T0 + 60_000)).toBe(false);
    expect(recallExpired(view, T0 + 200_000)).toBe(true);
    expect(renderCard(view, { nowMs: T0 + 5_000 })).toContain('按本机时间估算');
  });

  it('created_at 解析不了时按已过期处理（保守，不让点）', () => {
    const view = proposalView({ createdAt: '不是时间' });
    expect(recallExpired(view, T0)).toBe(true);
  });

  it('created_at 比服务端"现在"还晚（亚秒舍入）不算过期', () => {
    const view = proposalView({ createdAt: new Date(T0 + 500).toISOString() });
    expect(recallElapsedMs(view, T0)).toBe(0);
    expect(recallExpired(view, T0)).toBe(false);
  });

  it('卡片状态机：proposed 到点自动变 expired，终态不被时间改写', () => {
    expect(recallCardState(proposalView(), T0 + 10_000)).toBe('proposed');
    expect(recallCardState(proposalView(), T0 + 130_000)).toBe('expired');
    for (const status of ['confirmed', 'declined', 'failed', 'gone'] as const) {
      expect(recallCardState(proposalView({ status }), T0 + 999_000)).toBe(status);
    }
  });
});

/* -------------------------------------------------------------- 去重合并 */

describe('去重：同一个 proposal_id 只能有一张卡片', () => {
  it('同一批里重复的 id 只留一条', () => {
    const merged = mergeRecallProposals([], [proposalView(), proposalView()]);
    expect(merged).toHaveLength(1);
  });

  it('重复 final 再来一遍不会多出卡片', () => {
    const first = mergeRecallProposals([], [proposalView()]);
    const second = mergeRecallProposals(first, [proposalView()]);
    expect(second).toHaveLength(1);
  });

  it('终态不被新的 proposed 翻回待确认（不许让人重复点不可逆动作）', () => {
    const confirmed = [withStatus(proposalView(), 'confirmed')];
    const merged = mergeRecallProposals(confirmed, [proposalView()]);
    expect(merged[0]?.status).toBe('confirmed');
  });

  it('pending 也不被列表刷新弹回可点', () => {
    const pending = [withStatus(proposalView(), 'pending')];
    expect(mergeRecallProposals(pending, [proposalView()])[0]?.status).toBe('pending');
  });

  it('不同 id 都保留，顺序按先出现的在前', () => {
    const merged = mergeRecallProposals(
      [proposalView({ proposalId: 'a' })],
      [proposalView({ proposalId: 'b' }), proposalView({ proposalId: 'a' })],
    );
    expect(merged.map((item) => item.proposalId)).toEqual(['a', 'b']);
  });

  it('流里的一条提议用回复时刻当代理 created_at', () => {
    const view = proposalFromStream(
      { proposal_id: 'recall_x', message_id: 'msg_x', note: '要撤回吗' },
      new Date(T0).toISOString(),
      T0,
      T0,
    );
    expect(view.proposalId).toBe('recall_x');
    expect(view.createdAt).toBe(new Date(T0).toISOString());
    expect(view.status).toBe('proposed');
  });

  it('列表接口的一项转成视图时保留服务端 created_at', () => {
    const row: RecallProposal = {
      schema_version: 'v1',
      proposal_id: 'recall_s',
      message_id: 'msg_s',
      window_id: 'win_1',
      note: '她要撤回这条，确认吗？',
      created_at: new Date(T0 - 30_000).toISOString(),
    };
    const view = proposalFromServer(row, T0, T0);
    expect(view.createdAt).toBe(row.created_at);
    expect(recallElapsedMs(view, T0)).toBe(30_000);
  });
});

/* ------------------------------------------- final 事件 → 卡片（断点本身） */

describe('applyFinal 读 recall_proposals：原来断掉的那一环', () => {
  const FALLBACK = new Date(T0).toISOString();

  it('final 带一条提议 → 出卡片', () => {
    const views = proposalsFromFinal(
      [{ proposal_id: 'recall_1', message_id: 'msg_1', note: '她要撤回这条，确认吗？' }],
      FALLBACK,
      T0,
      T0,
    );
    expect(views).toHaveLength(1);
    expect(views[0]).toMatchObject({
      proposalId: 'recall_1',
      messageId: 'msg_1',
      status: 'proposed',
    });
    // 渲染出来确实有卡片
    const html = renderList(
      [messageFixture({ key: 'msg_1', text: '那句说错的话' })],
      views,
    );
    expect(countOf(html, 'data-testid="recall-card"')).toBe(1);
  });

  it('同一个 proposal_id 的 final 重复到达 → 只有一张卡片', () => {
    const once = proposalsFromFinal(
      [{ proposal_id: 'recall_1', message_id: 'msg_1' }],
      FALLBACK,
      T0,
      T0,
    );
    const twice = mergeRecallProposals(
      mergeRecallProposals([], once),
      proposalsFromFinal([{ proposal_id: 'recall_1', message_id: 'msg_1' }], FALLBACK, T0, T0),
    );
    expect(twice).toHaveLength(1);
    const html = renderList([messageFixture({ key: 'msg_1', text: '那句话' })], twice);
    expect(countOf(html, 'data-testid="recall-card"')).toBe(1);
  });

  it('字段缺失/形状不对的条目被丢掉（宁可少画，也不画一张点不动的卡）', () => {
    expect(proposalsFromFinal(undefined, FALLBACK, T0, T0)).toEqual([]);
    expect(proposalsFromFinal([], FALLBACK, T0, T0)).toEqual([]);
    expect(
      proposalsFromFinal(
        [
          null,
          'nope',
          { proposal_id: '', message_id: 'msg_1' },
          { proposal_id: 'recall_1' },
          { proposal_id: 42, message_id: 'msg_1' },
          { proposal_id: 'recall_ok', message_id: 'msg_2' },
        ],
        FALLBACK,
        T0,
        T0,
      ).map((view) => view.proposalId),
    ).toEqual(['recall_ok']);
  });

  it('流里没有 created_at 时用回复时刻当代理，锚点是服务端 Date 头', () => {
    // 真实时序：她那条回复落库 → 几秒后流结束（Date 头就是"现在"）。
    const replyAt = new Date(T0 - 4_000).toISOString();
    const views = proposalsFromFinal(
      [{ proposal_id: 'recall_1', message_id: 'msg_1' }],
      replyAt,
      T0,
      T0,
    );
    // 代理 created_at + 服务端锚点：拿到响应那一刻已经过了 4 秒。
    expect(recallElapsedMs(views[0] as RecallProposalView, T0)).toBe(4_000);
    expect(recallClockSource(views[0] as RecallProposalView)).toBe('server');
    // 随后列表接口会用权威 created_at 覆盖它（这里模拟那一跳）。
    const authoritative = proposalFromServer(
      {
        schema_version: 'v1',
        proposal_id: 'recall_1',
        message_id: 'msg_1',
        window_id: 'win_1',
        note: '她要撤回这条，确认吗？',
        created_at: new Date(T0 - 2_000).toISOString(),
      },
      T0,
      T0,
    );
    const merged = mergeRecallProposals(views, [authoritative]);
    expect(recallElapsedMs(merged[0] as RecallProposalView, T0)).toBe(2_000);
  });
});

/* ------------------------------------------------------------- 卡片渲染 */

describe('卡片：四态各有各的样子，且不出现原因码', () => {
  it('proposed：有正文摘要、两个按钮、剩余秒数', () => {
    const html = renderCard(proposalView());
    expect(html).toContain('她想撤回这条');
    expect(html).toContain('刚才那句说错了');
    expect(html).toContain('data-testid="recall-confirm"');
    expect(html).toContain('data-testid="recall-decline"');
    expect(html).toContain('还有 115 秒');
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-live="polite"');
    expect(html).not.toContain('disabled');
  });

  it('pending：按钮禁用并显示「正在撤回…」', () => {
    const html = renderCard(proposalView({ status: 'pending' }));
    expect(html).toContain('正在撤回…');
    expect(countOf(html, 'disabled')).toBe(2);
  });

  it('confirmed：变「已撤回」，按钮消失', () => {
    const html = renderCard(proposalView({ status: 'confirmed' }));
    expect(html).toContain('已撤回');
    expect(html).not.toContain('data-testid="recall-confirm"');
    expect(html).not.toContain('data-testid="recall-decline"');
  });

  it('declined：变「好，不撤了」', () => {
    const html = renderCard(proposalView({ status: 'declined' }));
    expect(html).toContain('好，不撤了');
    expect(html).not.toContain('data-testid="recall-confirm"');
  });

  it('expired：按钮不可点，文案「已经来不及撤了」', () => {
    const html = renderCard(proposalView(), { nowMs: T0 + 130_000 });
    expect(html).toContain('已经来不及撤了');
    expect(html).not.toContain('data-testid="recall-confirm"');
    expect(html).not.toContain('data-testid="recall-decline"');
  });

  it('failed：显示后端 note 原文，且不出现 reason_code', () => {
    const html = renderCard(
      proposalView({
        status: 'failed',
        failureNote: '这个接入方式不支持撤回',
      }),
    );
    expect(html).toContain('这个接入方式不支持撤回');
    // 原因码（大写下划线 / tool_refused 这类稳定码）不许漏到界面上
    expect(html).not.toContain('reason_code');
    expect(html).not.toContain('tool_refused');
    expect(html).not.toMatch(/\b[A-Z]{2,}_[A-Z_]+\b/);
  });

  it('摘要缺失时说明"不在这一屏"，不冒充正文', () => {
    const html = renderCard(proposalView(), { excerpt: null });
    expect(html).toContain('那条消息不在这一屏里了');
    expect(html).not.toContain('（无内容）');
  });
});

/* --------------------------------------------------------- 消息列表集成 */

describe('消息列表：卡片挂在被撤那条下面', () => {
  const MESSAGES = [
    messageFixture({ key: 'msg_1', text: '她刚才说的那句话' }),
    messageFixture({ key: 'msg_2', text: '后面一句' }),
  ];

  it('流里带一条提议 → 出现卡片，并引用那条的正文摘要', () => {
    const html = renderList(MESSAGES, [proposalView()]);
    expect(countOf(html, 'data-testid="recall-card"')).toBe(1);
    expect(html).toContain('她刚才说的那句话');
    // 卡片在被撤那条之后、下一条之前
    const cardAt = html.indexOf('data-testid="recall-card"');
    expect(cardAt).toBeGreaterThan(html.indexOf('她刚才说的那句话'));
    expect(cardAt).toBeLessThan(html.indexOf('后面一句'));
  });

  it('同一个 proposal_id 的 final 重复到达 → 只有一张卡片', () => {
    const merged = mergeRecallProposals(
      mergeRecallProposals([], [proposalView()]),
      [proposalView()],
    );
    const html = renderList(MESSAGES, merged);
    expect(countOf(html, 'data-testid="recall-card"')).toBe(1);
  });

  it('两条不同提议 → 两张卡片', () => {
    const html = renderList(MESSAGES, [
      proposalView({ proposalId: 'a', messageId: 'msg_1' }),
      proposalView({ proposalId: 'b', messageId: 'msg_2' }),
    ]);
    expect(countOf(html, 'data-testid="recall-card"')).toBe(2);
  });

  it('提议指向的消息不在这一页 → 挂到末尾，不丢', () => {
    const html = renderList(MESSAGES, [
      proposalView({ proposalId: 'c', messageId: 'msg_not_here' }),
    ]);
    expect(countOf(html, 'data-testid="recall-card"')).toBe(1);
    expect(html).toContain('其它还没处理的撤回提议');
  });

  it('列表接口失败 → 只有卡片区降级，消息照常渲染', () => {
    const html = renderList(MESSAGES, [], { unavailable: true });
    expect(html).toContain('暂时读不到撤回提议');
    expect(html).toContain('她刚才说的那句话');
    expect(html).toContain('后面一句');
    // 降级不能变成整页错误
    expect(html).not.toContain('message-error');
  });

  it('已撤回的消息：气泡降权 + 徽标，正文仍可读（不改成"未生成内容"）', () => {
    const html = renderList(
      [
        messageFixture({
          key: 'msg_1',
          text: '她刚才说的那句话',
          recalledAt: new Date(T0).toISOString(),
          recalledByActor: true,
        }),
      ],
      [],
    );
    expect(html).toContain('她撤回了这条');
    expect(html).toContain('message__bubble--recalled');
    expect(html).toContain('她刚才说的那句话');
    expect(html).not.toContain('已停止，未生成内容');
  });

  it('对方撤回用另一句措辞（不冒充"她撤回"）', () => {
    const html = renderList(
      [
        messageFixture({
          key: 'msg_1',
          text: '被对方撤回的',
          recalledAt: new Date(T0).toISOString(),
          recalledByActor: false,
        }),
      ],
      [],
    );
    expect(html).toContain('对方撤回了一条消息');
    expect(html).not.toContain('她撤回了这条');
  });
});

/* ---------------------------------------------------------- 文案完整性 */

describe('状态文案', () => {
  it('七种状态都有人话，且不含原因码', () => {
    for (const status of [
      'proposed',
      'pending',
      'confirmed',
      'declined',
      'expired',
      'failed',
      'gone',
    ] as const) {
      const text = recallStatusText(status, proposalView(), T0 + 1_000);
      expect(text.length).toBeGreaterThan(0);
      expect(text).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
    }
  });
});
