/**
 * 总览新增两块摘要（`today_outline` / `intent_teaser`）的回归测试。
 *
 * 为什么单独一个文件：`dashboard.test.ts` 盯的是"哪条路由渲染哪个页面"与
 * 旧卡片（日程/承诺），这两块是新契约（docs/API-WEB-V1.md §11.9），要钉的是
 * 三条新行为：
 *
 * 1. `relation` 三值（now / next / anchor）各有各的中文，**不能合并**；
 * 2. 两块各有自己的 `status`，一块坏不影响另一块（后端 `_guard` 逐块兜底）；
 * 3. 空态要分状态：没安排 / 还没生成 / 读不到，不能都说成"没内容"。
 *
 * 与仓库其它测试一样用 SSR 断言结构；375/1440 的横向溢出交给
 * `life-timeline.browser.test.tsx`。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  intentTeaserSourceLabel,
  intentTeaserStateLabel,
  LifeOverviewCards,
  outlineStateLabel,
  relationLabel,
  RELATION_LABEL,
} from './components/DashboardPage';
import type { DashboardSection, IntentTeaserOut, TodayOutlineOut } from './types';

/* ------------------------------------------------------------------ 夹具 */

const TODAY: TodayOutlineOut = {
  schema_version: 'v1',
  state: 'ready',
  display_date: '2026-11-08',
  display_timezone: 'Asia/Shanghai',
  plan_day: '2026-11-07',
  actor_timezone: 'America/New_York',
  items: [
    {
      schema_version: 'v1',
      segment_id: 'seg-now',
      relation: 'now',
      title: '整理邮件',
      start_at: '2026-11-08T00:00:00Z',
      end_at: '2026-11-08T01:00:00Z',
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-next',
      relation: 'next',
      title: '出门散步',
      start_at: '2026-11-08T02:00:00Z',
      end_at: '2026-11-08T03:00:00Z',
    },
    {
      schema_version: 'v1',
      segment_id: 'seg-anchor',
      relation: 'anchor',
      title: '约定通话',
      start_at: '2026-11-08T04:00:00Z',
      end_at: '2026-11-08T05:00:00Z',
    },
  ],
  note: null,
};

const INTENT: IntentTeaserOut = {
  schema_version: 'v1',
  horizon_unplanned: true,
  items: [
    {
      schema_version: 'v1',
      intention_id: 'intent-1',
      title: '想去看海',
      target_start_day: '2026-11-09',
      target_end_day: '2026-11-09',
      state: 'planned',
      source_kind: 'user',
    },
    {
      schema_version: 'v1',
      intention_id: 'intent-2',
      title: '整理旧照片',
      target_start_day: '2026-11-10',
      target_end_day: '2026-11-12',
      state: 'confirmed',
      source_kind: 'recurrence',
    },
  ],
  note: null,
};

function section(
  status: DashboardSection['status'],
  data: unknown,
): DashboardSection {
  return {
    status,
    as_of: '2026-11-08T00:10:00Z',
    data: data as Record<string, unknown> | null,
  };
}

function renderCards(
  today: DashboardSection,
  intent: DashboardSection,
): string {
  return renderToStaticMarkup(
    h(LifeOverviewCards, {
      today,
      intent,
      onOpenLife: () => undefined,
    }),
  );
}

/** 取出某张卡片（`data-testid="dash-<title>"`）的 HTML 片段。 */
function card(html: string, title: string): string {
  const marker = `data-testid="dash-${title}"`;
  const at = html.indexOf(marker);
  expect(at, `找不到卡片 ${title}`).toBeGreaterThanOrEqual(0);
  const start = html.lastIndexOf('<section', at);
  const end = html.indexOf('</section>', at);
  expect(end).toBeGreaterThan(start);
  return html.slice(start, end + '</section>'.length);
}

/* ------------------------------------------------------------------ 测试 */

describe('today_outline：relation 文案', () => {
  it('now / next / anchor 三种关系分别有中文，且不合并', () => {
    expect(RELATION_LABEL.now).toBe('此刻可能在做');
    expect(RELATION_LABEL.next).toBe('接下来');
    expect(RELATION_LABEL.anchor).toBe('已约定');
    expect(relationLabel('now')).toBe('此刻可能在做');
    expect(relationLabel('next')).toBe('接下来');
    expect(relationLabel('anchor')).toBe('已约定');
    // 未知值原样显示，不静默吞成"未知"。
    expect(relationLabel('later')).toBe('later');
  });

  it('每条摘要按自己的 relation 渲染标签，并带 data-relation', () => {
    const html = card(renderCards(section('ok', TODAY), section('ok', INTENT)), '今天的节奏');
    expect(html).toContain('data-relation="now"');
    expect(html).toContain('data-relation="next"');
    expect(html).toContain('data-relation="anchor"');
    expect(html).toContain('此刻可能在做');
    expect(html).toContain('接下来');
    expect(html).toContain('已约定');
    expect(html).toContain('整理邮件');
    expect(html).toContain('出门散步');
  });

  it('时间按区块自带的 display_timezone 显示（不是浏览器时区）', () => {
    const html = card(renderCards(section('ok', TODAY), section('ok', INTENT)), '今天的节奏');
    // 00:00Z -> 上海 08:00；02:00Z -> 10:00。
    expect(html).toContain('08:00–09:00');
    expect(html).toContain('10:00–11:00');
  });

  it('两侧日期不同时给出解释，两个日期都显示', () => {
    const html = card(renderCards(section('ok', TODAY), section('ok', INTENT)), '今天的节奏');
    expect(html).toContain('你的今天 2026-11-08');
    expect(html).toContain('她的计划日 2026-11-07');
    expect(html).toContain('两边日期不同');
  });

  it('可用的空态说明"这一天还没有安排"，不是"读不到"', () => {
    const emptyToday: TodayOutlineOut = {
      ...TODAY,
      state: 'empty',
      items: [],
      note: '这一天还没有安排',
    };
    const html = card(renderCards(section('empty', emptyToday), section('ok', INTENT)), '今天的节奏');
    expect(html).toContain('这一天还没有安排');
    expect(html).not.toContain('暂时读不到这块数据');
  });
});

describe('intent_teaser：未来意向', () => {
  it('列出 1–2 条，并写明「未来日还未生成大纲」', () => {
    const html = card(renderCards(section('ok', TODAY), section('ok', INTENT)), '未来意向');
    expect(html).toContain('未来日还未生成大纲');
    expect(html).toContain('这些只是意向');
    expect(html).toContain('想去看海');
    expect(html).toContain('整理旧照片');
    expect(html).toContain('2026-11-10 → 2026-11-12');
  });

  it('confirmed 只表示确认了目标/日期，不说「已约好」', () => {
    expect(intentTeaserStateLabel('proposed')).toBe('候选');
    expect(intentTeaserStateLabel('planned')).toBe('想/考虑');
    expect(intentTeaserStateLabel('confirmed')).toBe(
      '已确认想在这个日期段做（时段仍待定）',
    );
    const html = card(renderCards(section('ok', TODAY), section('ok', INTENT)), '未来意向');
    expect(html).not.toContain('已约好');
    expect(intentTeaserSourceLabel('recurrence')).toBe('按周期');
  });

  it('空态说"还没有有来源的意向"，同时仍带 horizon 说明', () => {
    const emptyIntent: IntentTeaserOut = {
      ...INTENT,
      items: [],
      note: '未来 7 天还没有有来源的意向',
    };
    const html = card(renderCards(section('ok', TODAY), section('empty', emptyIntent)), '未来意向');
    expect(html).toContain('未来 7 天还没有有来源的意向');
    expect(html).toContain('未来日还未生成大纲');
  });
});

describe('两块各自的 status（一块坏不影响整页）', () => {
  it('today_outline 坏 → 只有它显示"读不到"，未来意向照常显示', () => {
    const html = renderCards(section('unavailable', null), section('ok', INTENT));
    const today = card(html, '今天的节奏');
    const intent = card(html, '未来意向');
    expect(today).toContain('暂时读不到这块数据');
    expect(intent).not.toContain('暂时读不到这块数据');
    expect(intent).toContain('想去看海');
    // 读不到的卡片不能带"更新时间"（没有可信时刻）。
    expect(today).not.toContain('更新于');
  });

  it('intent_teaser 坏 → 只有它显示"读不到"，今天的节奏照常显示', () => {
    const html = renderCards(section('ok', TODAY), section('unavailable', null));
    const today = card(html, '今天的节奏');
    const intent = card(html, '未来意向');
    expect(intent).toContain('暂时读不到这块数据');
    expect(today).not.toContain('暂时读不到这块数据');
    expect(today).toContain('整理邮件');
  });

  it('两块都坏也不炸：各自显示读不到', () => {
    const html = renderCards(section('unavailable', null), section('unavailable', null));
    expect(html.match(/暂时读不到这块数据/g)?.length).toBe(2);
  });

  it('空态是"查到了但没有"，不会显示成"读不到"', () => {
    const emptyIntent: IntentTeaserOut = { ...INTENT, items: [], note: null };
    const html = renderCards(section('ok', TODAY), section('empty', emptyIntent));
    expect(html).not.toContain('暂时读不到这块数据');
    expect(html).toContain('未来 7 天还没有有来源的意向');
  });
});

describe('查看生活入口', () => {
  it('「今天的节奏」卡片上有「查看生活」按钮（不是只写着"详细"）', () => {
    const html = card(renderCards(section('ok', TODAY), section('ok', INTENT)), '今天的节奏');
    expect(html).toContain('查看生活');
  });

  it('状态文案六值各有中文', () => {
    expect(outlineStateLabel('ready')).toBe('已生成');
    expect(outlineStateLabel('degraded')).toBe('规则兜底');
    expect(outlineStateLabel('failed')).toBe('生成失败');
    expect(outlineStateLabel('pending')).toBe('还没生成');
    expect(outlineStateLabel('empty')).toBe('这一天还没有安排');
    expect(outlineStateLabel('unavailable')).toBe('读不到');
  });
});
