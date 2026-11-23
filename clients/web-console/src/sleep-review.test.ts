/**
 * 夜间回顾的回归测试。
 *
 * 这一块最容易出的两类错，测试都盯着：
 *
 * 1. **把 `skipped` 画成失败**。后端 `consolidation_state=skipped` 表示"这晚
 *    没有够旧的记忆需要合并"，来源索引照样建立，于是可以 `ready`。一次测试里
 *    `skipped + ready` 同时成立，并断言它**不带**警示标记——这是本文件最核心
 *    的一条，因为把它画成红色是三秒就能犯的错。
 * 2. **把"没读到"说成"没有"**。读失败必须说失败并能重试；`nights` 为空才是
 *    空状态；`empty`/`unavailable` 各自有自己的解释，不能并成一句"没有内容"。
 *
 * 渲染方式与 `memory.test.ts` 一致：`react-dom/server` 静态渲染做结构断言，
 * 不需要 DOM 库。交互（展开原话）交给真 Chromium 的浏览器用例。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  SleepReviewNightCard,
  SleepReviewSection,
  consolidationCopy,
  errorReason,
  reviewStatusCopy,
  reasonDetail,
  sleepDateLabel,
  sourceSessionHref,
  speakerLabel,
} from './components/SleepReview';
import { memoryTabFromHref, memoryTabHref } from './components/MemoryPage';
import type { SleepReviewNight, SleepReviewsResponse } from './types';

const STAMP = '2026-11-22';

function night(overrides: Partial<SleepReviewNight> = {}): SleepReviewNight {
  return {
    sleep_date: STAMP,
    consolidation_state: 'ok',
    reason: `预计入睡（${STAMP}）后整理：合并 2 组、归档 3 条`,
    groups_merged: 2,
    rows_archived: 3,
    review_status: 'ready',
    items: [
      {
        memory_id: 'mem_garden',
        text: '每周给阳台的山茶浇水',
        window_id: 'win_a',
        turn_id: 'turn_a',
        original: [{ speaker: 'user', text: '我每周给阳台的山茶浇水', truncated: false }],
      },
    ],
    ...overrides,
  };
}

function response(nights: SleepReviewNight[]): SleepReviewsResponse {
  return { schema_version: 'v1', nights };
}

function render(element: React.ReactElement): string {
  return renderToStaticMarkup(element);
}

/* ==================================================================== 分区分支 */

describe('夜间回顾分区', () => {
  it('读取中：只说在读取，不提前说没有回顾', () => {
    const html = render(h(SleepReviewSection, {
      state: { status: 'loading', data: null, error: null },
      onReload: () => {},
    }));
    expect(html).toContain('正在读取夜间回顾');
    expect(html).not.toContain('还没有可看的夜间回顾');
  });

  it('读取失败：说失败并且能重试，不降级成空状态', () => {
    const html = render(h(SleepReviewSection, {
      state: {
        status: 'error',
        data: null,
        error: { reason: '服务端错误', hint: '', retriable: true, code: 'internal', detail: null, traceId: null },
      },
      onReload: () => {},
    }));
    // 正向：失败被说出来了，并给了重试。
    expect(html).toContain('夜间回顾没读出来');
    expect(html).toContain('data-testid="sleep-retry"');
    // 关键的负向断言：绝不能把"没读到"说成"没有回顾"。
    expect(html).not.toContain('还没有可看的夜间回顾');
  });

  it('nights 为空：给一条直接的空状态，不说失败', () => {
    const html = render(h(SleepReviewSection, {
      state: { status: 'ok', data: response([]), error: null },
      onReload: () => {},
    }));
    expect(html).toContain('还没有可看的夜间回顾');
    expect(html).not.toContain('夜间回顾没读出来');
    expect(html).not.toContain('sleep-retry');
  });

  it('有内容：按服务端给的顺序渲染，不再前端排序', () => {
    const newer = night({ sleep_date: '2026-11-22' });
    const older = night({ sleep_date: '2026-11-21' });
    const html = render(h(SleepReviewSection, {
      state: { status: 'ok', data: response([newer, older]), error: null },
      onReload: () => {},
    }));
    const first = html.indexOf('2026 年 11 月 22 日');
    const second = html.indexOf('2026 年 11 月 21 日');
    expect(first).toBeGreaterThan(-1);
    expect(second).toBeGreaterThan(-1);
    expect(first).toBeLessThan(second);
  });
});

/* ==================================================================== 两条轴 */

describe('整理维度与回顾维度分开', () => {
  it('skipped + ready：正常结果，有可读条目且不带警示', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({
        consolidation_state: 'skipped',
        groups_merged: 0,
        rows_archived: 0,
        reason: `预计入睡（${STAMP}）后整理过：没有达到合并条件的记忆`,
        review_status: 'ready',
      }),
    }));
    // 整理维度：说清"不需要合并"，而不是"失败"。
    expect(html).toContain('当晚不需要合并');
    expect(html).toContain('data-state="skipped"');
    // 不带警示样式类（警示只属于 failed）。
    expect(html).not.toContain('sleep-night__state--warn');
    // 回顾维度：条目照常可读。
    expect(html).toContain('每周给阳台的山茶浇水');
    expect(html).not.toContain('sleep-review-note');
  });

  it('failed：整理出错带警示，但条目仍然显示', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({ consolidation_state: 'failed', reason: '整理失败（TimeoutError）' }),
    }));
    expect(html).toContain('整理没做完');
    expect(html).toContain('sleep-night__state--warn');
    expect(html).toContain('每周给阳台的山茶浇水');
  });

  it('ok + ready：合并计数出现在整理维度', () => {
    const html = render(h(SleepReviewNightCard, { night: night() }));
    expect(html).toContain('整理完成');
    expect(html).toContain('合并 2 组、归档 3 条');
    expect(html).not.toContain('sleep-night__state--warn');
  });

  /**
   * `empty` 与 `unavailable` 必须给出不同解释：两者的补救动作不同，并成一句
   * "没有内容"会让旧记录看起来像"我的记忆被回忆弄丢了"。
   */
  it('empty 与 unavailable 说清各自原因，且都不是失败', () => {
    const empty = render(h(SleepReviewNightCard, {
      night: night({ review_status: 'empty', items: [] }),
    }));
    const unavailable = render(h(SleepReviewNightCard, {
      night: night({ review_status: 'unavailable', items: [] }),
    }));
    expect(empty).toContain('没有可读的条目');
    expect(empty).toContain('撤回');
    expect(unavailable).toContain('没有留下可读的回顾');
    expect(unavailable).toContain('旧版本的记录');
    // 两者都不是失败文案。
    expect(empty).not.toContain('整理没做完');
    expect(unavailable).not.toContain('整理没做完');
    // 也不能说成"读取失败"。
    expect(empty).not.toContain('夜间回顾没读出来');
    expect(unavailable).not.toContain('夜间回顾没读出来');
  });
});

/* ==================================================================== 原话 */

describe('原话可核对', () => {
  it('有原话时给展开入口，并写明条数', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({
        items: [{
          memory_id: 'mem_a', text: '她记得我喜欢的茶是白茶', window_id: 'w', turn_id: 't',
          original: [
            { speaker: 'user', text: '我喜欢喝白茶', truncated: false },
            { speaker: 'assistant', text: '记住你喜欢白茶了', truncated: false },
          ],
        }],
      }),
    }));
    expect(html).toContain('<details');
    expect(html).toContain('原话 2 条');
    expect(html).toContain('我喜欢喝白茶');
    expect(html).toContain('记住你喜欢白茶了');
  });

  it('截断的原话明确标注，不假装完整', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({
        items: [{
          memory_id: 'mem_b', text: '一条摘要', window_id: 'w', turn_id: 't',
          original: [{ speaker: 'user', text: '很长的原话', truncated: true }],
        }],
      }),
    }));
    expect(html).toContain('此处截断');
  });

  it('没有原话时说清楚，不渲染空的原话区', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({
        items: [{ memory_id: 'mem_c', text: '只有摘要', window_id: 'w', turn_id: 't', original: [] }],
      }),
    }));
    expect(html).toContain('没有可核对的原话');
    expect(html).not.toContain('<details');
  });
});

/* ==================================================================== 来源会话 */

describe('来源会话入口', () => {
  /** 两条记忆来自不同窗口——这正是"只看文本分不出来"的场景。 */
  function twoWindows(): SleepReviewNight {
    return night({
      items: [
        {
          memory_id: 'mem_garden', text: '每周给阳台的山茶浇水',
          window_id: 'win_garden', turn_id: 'turn_garden',
          original: [{ speaker: 'user', text: '我每周给阳台的山茶浇水', truncated: false }],
        },
        {
          memory_id: 'mem_bridge', text: '参观过城西的石桥',
          window_id: 'win_bridge', turn_id: 'turn_bridge',
          original: [{ speaker: 'user', text: '今天去看了城西那座石桥', truncated: false }],
        },
      ],
    });
  }

  it('有可读原话时，每条原话都带来源入口，且指向各自的窗口', () => {
    const html = render(h(SleepReviewNightCard, { night: twoWindows() }));
    // 每条原话各一个入口。
    expect(html.split('data-testid="sleep-open-source"').length - 1).toBe(2);
    // 按**行**切开来断言：只看整页里出现过两个窗口名是不够的——
    // 两条都指向同一个窗口时，两个名字照样都在（行容器上带着自己的
    // data-window-id），那样断言会在错误实现下依然通过。
    const rows = html.split('data-testid="sleep-item-').slice(1);
    expect(rows).toHaveLength(2);
    const [first, second] = rows;
    expect(first).toContain('data-window-id="win_garden"');
    expect(first).toContain('data-testid="sleep-open-source"');
    expect(second).toContain('data-window-id="win_bridge"');
    expect(second).toContain('data-testid="sleep-open-source"');
    // 第一行的入口不能指到第二个窗口，反之亦然。
    expect(first).not.toContain('data-window-id="win_bridge"');
    expect(second).not.toContain('data-window-id="win_garden"');
  });

  it('地址沿用既有 /s/{window_id} 路由并做编码', () => {
    expect(sourceSessionHref('win_garden')).toBe('/s/win_garden');
    // 需要转义的 id 走同一条 routePath，与聊天页内部跳转一致。
    expect(sourceSessionHref('win a/b')).toBe('/s/win%20a%2Fb');
  });

  /**
   * 没有可读原话时不给入口：只凭一条摘要跳进会话，用户到了那边也找不到
   * 对应的句子，等于把一个找不到的目标伪装成可核对。
   */
  it('没有原话时不渲染来源入口', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({
        items: [{ memory_id: 'mem_c', text: '只有摘要', window_id: 'win_c', turn_id: 't', original: [] }],
      }),
    }));
    expect(html).toContain('没有可核对的原话');
    expect(html).not.toContain('data-testid="sleep-open-source"');
  });

  it('原话不可再用的那一晚不给入口', () => {
    const html = render(h(SleepReviewNightCard, {
      night: night({ review_status: 'empty', items: [] }),
    }));
    expect(html).not.toContain('data-testid="sleep-open-source"');
  });
});

/* ==================================================================== 文案与工具 */

describe('文案与工具', () => {
  it('日期按字符串切分，不受时区推移', () => {
    expect(sleepDateLabel('2026-11-22')).toBe('2026 年 11 月 22 日');
    expect(sleepDateLabel('2026-01-05')).toBe('2026 年 1 月 5 日');
    // 非日期形状原样返回，不猜。
    expect(sleepDateLabel('unknown')).toBe('unknown');
  });

  it('说话人只翻已知值', () => {
    expect(speakerLabel('user')).toBe('你');
    expect(speakerLabel('assistant')).toBe('她');
    expect(speakerLabel('group_member_7')).toBe('group_member_7');
  });

  it('整理维度只有 failed 带警示', () => {
    expect(consolidationCopy('ok', { groups_merged: 1, rows_archived: 1 }).tone).toBe('neutral');
    expect(consolidationCopy('skipped', { groups_merged: 0, rows_archived: 0 }).tone).toBe('neutral');
    expect(consolidationCopy('failed', { groups_merged: 0, rows_archived: 0 }).tone).toBe('warn');
  });

  /**
   * 状态行已经说了"合并 2 组、归档 3 条"，reason 再重复一遍是同一个信息说两次。
   * 只有 failed 的 reason 带出错原因，是状态行给不出的。
   */
  it('reason 只在它说出状态行没说的东西时显示', () => {
    expect(reasonDetail(night({ consolidation_state: 'ok' }))).toBeNull();
    expect(reasonDetail(night({ consolidation_state: 'skipped' }))).toBeNull();
    expect(reasonDetail(night({ consolidation_state: 'failed', reason: '整理失败（TimeoutError）' })))
      .toBe('整理失败（TimeoutError）');
  });

  it('ok 的 reason 不再重复状态行已经给出的计数', () => {
    const html = render(h(SleepReviewNightCard, { night: night() }));
    // "合并 2 组、归档 3 条"只出现一次。
    expect(html.split('合并 2 组、归档 3 条').length - 1).toBe(1);
  });

  it('回顾维度 ready 不额外加解释', () => {
    expect(reviewStatusCopy('ready').note).toBeNull();
    expect(reviewStatusCopy('empty').note).not.toBe(reviewStatusCopy('unavailable').note);
  });

  it('错误原因按错误码给短句', () => {
    const base = { reason: 'x', hint: '', retriable: true, detail: null, traceId: null };
    expect(errorReason({ ...base, code: 'forbidden' })).toContain('权限');
    expect(errorReason({ ...base, code: 'network_error' })).toContain('连不上');
    expect(errorReason(null)).toBe('请求失败。');
  });
});

/* ==================================================================== 入口 */

describe('分区入口', () => {
  it('夜间回顾有独立深链且回得了默认页', () => {
    expect(memoryTabHref('review')).toBe('/memory?tab=review');
    expect(memoryTabFromHref('/memory?tab=review')).toBe('review');
    // 默认仍是记忆库，旧路径仍开在记忆库。
    expect(memoryTabFromHref('/memory')).toBe('content');
    expect(memoryTabFromHref('/memory?tab=bogus')).toBe('content');
  });
});
