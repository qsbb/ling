/**
 * 生活时间轴（P8）的回归测试。
 *
 * 没有 DOM 测试库（与 `dashboard.test.ts` / `memory.test.ts` 同样的取舍）：
 * 用 `react-dom/server` 把组件渲染成字符串，对**结构、类名与文案**做断言。
 * 375 / 1440 的横向溢出由 `life-timeline.browser.test.tsx` 在真 Chromium 里量。
 *
 * 这一组测试盯的是契约里最容易做错的几条（docs/API-WEB-V1.md §11）：
 * - 两套日期/时区都要显示，不同时**解释**而不是抹平；
 * - 计划（虚线）与事实（实心）在类名与证据措辞上分开；
 * - `state` 六值各有文案，`unavailable` 有重试且不能说成"没安排"；
 * - 梦的三态不能合并；`hidden_*` 计数与空数组不是一回事；
 * - 睡眠锚点只说"预计休息"；只有 confirmed+commitment 才能说"已约好"。
 */
import { readFileSync } from 'node:fs';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  buildSegmentTree,
  commitmentTag,
  DreamCardView,
  dreamView,
  evidenceLabel,
  formatInZone,
  hiddenSummary,
  LifeTimeline,
  segmentEmptyText,
  segmentHeightPx,
  segmentPhase,
  TIMELINE_STATE_COPY,
  timezoneGapNote,
  TimelineTrack,
} from './components/LifeTimeline';
import type {
  DayIntentSummary,
  DreamCard,
  LifeTimelineFact,
  LifeTimelineOut,
  LifeTimelineSegment,
  TimelineState,
} from './types';

/* ------------------------------------------------------------------ 夹具 */

const DISPLAY_TZ = 'Asia/Shanghai';
const ACTOR_TZ = 'America/New_York';

function segment(
  segmentId: string,
  overrides: Partial<LifeTimelineSegment> = {},
): LifeTimelineSegment {
  return {
    schema_version: 'v1',
    segment_id: segmentId,
    plan_id: 'plan-1',
    plan_day: '2026-11-07',
    parent_id: null,
    layer: 'block',
    is_leaf: true,
    status: 'current',
    revision: 3,
    source: 'model',
    anchor_kind: '',
    commitment_level: 'tentative',
    place_id: null,
    title: `段 ${segmentId}`,
    start_at: '2026-11-08T00:00:00Z',
    end_at: '2026-11-08T01:00:00Z',
    display_start_at: '2026-11-08T00:00:00Z',
    display_end_at: '2026-11-08T01:00:00Z',
    continues_from_previous_day: false,
    continues_next_day: false,
    ...overrides,
  };
}

function fact(
  activityId: string,
  overrides: Partial<LifeTimelineFact> = {},
): LifeTimelineFact {
  return {
    schema_version: 'v1',
    activity_id: activityId,
    state: 'completed',
    origin: 'simulated',
    evidence_kind: 'observed',
    scene_valid_until: null,
    started_at: '2026-11-08T00:05:00Z',
    ended_at: '2026-11-08T00:40:00Z',
    place_id: null,
    ...overrides,
  };
}

function intent(
  intentionId: string,
  overrides: Partial<DayIntentSummary> = {},
): DayIntentSummary {
  return {
    schema_version: 'v1',
    intention_id: intentionId,
    target_start_day: '2026-11-09',
    target_end_day: '2026-11-09',
    state: 'proposed',
    source_kind: 'model',
    visibility: 'actor_private',
    priority: 50,
    ...overrides,
  };
}

/** 一天：她的计划日（11-07，纽约）比你的今天（11-08，上海）早一天。 */
const SEGMENTS: LifeTimelineSegment[] = [
  // 21:00–06:00 上海 → 展开展示日窗口后只画 00:00–06:00，段首标「← 前一天」。
  segment('seg-night', {
    title: '睡觉',
    anchor_kind: 'sleep',
    start_at: '2026-11-07T13:00:00Z',
    end_at: '2026-11-07T22:00:00Z',
    display_start_at: '2026-11-07T16:00:00Z',
    display_end_at: '2026-11-07T22:00:00Z',
    continues_from_previous_day: true,
  }),
  // 08:00–12:00：父段（大块），下面挂小时段与分钟段。
  segment('seg-block', {
    title: '上午的工作块',
    is_leaf: false,
    start_at: '2026-11-08T00:00:00Z',
    end_at: '2026-11-08T04:00:00Z',
    display_start_at: '2026-11-08T00:00:00Z',
    display_end_at: '2026-11-08T04:00:00Z',
  }),
  segment('seg-hour', {
    title: '整理邮件',
    parent_id: 'seg-block',
    layer: 'hour',
    start_at: '2026-11-08T00:00:00Z',
    end_at: '2026-11-08T01:00:00Z',
    display_start_at: '2026-11-08T00:00:00Z',
    display_end_at: '2026-11-08T01:00:00Z',
  }),
  segment('seg-minute', {
    title: '回一封急件',
    parent_id: 'seg-hour',
    layer: 'minute',
    start_at: '2026-11-08T00:15:00Z',
    end_at: '2026-11-08T00:30:00Z',
    display_start_at: '2026-11-08T00:15:00Z',
    display_end_at: '2026-11-08T00:30:00Z',
  }),
  // 10:00–11:00：已确认的承诺锚点。
  segment('seg-commit', {
    title: '约定通话',
    layer: 'hour',
    anchor_kind: 'commitment',
    commitment_level: 'confirmed',
    start_at: '2026-11-08T02:00:00Z',
    end_at: '2026-11-08T03:00:00Z',
    display_start_at: '2026-11-08T02:00:00Z',
    display_end_at: '2026-11-08T03:00:00Z',
  }),
  // 23:30–次日 01:00 上海 → 展示日只画到 24:00，段尾标「→ 次日」。归属她的 11-08。
  segment('seg-midnight', {
    title: '夜间整理',
    layer: 'hour',
    plan_day: '2026-11-08',
    start_at: '2026-11-08T15:30:00Z',
    end_at: '2026-11-08T17:00:00Z',
    display_start_at: '2026-11-08T15:30:00Z',
    display_end_at: '2026-11-08T16:00:00Z',
    continues_next_day: true,
  }),
];

const FACTS: LifeTimelineFact[] = [
  fact('fact-clock', {
    evidence_kind: 'clock_only',
    state: 'completed',
    started_at: '2026-11-08T00:00:00Z',
    ended_at: '2026-11-08T00:10:00Z',
  }),
  fact('fact-observed', {
    evidence_kind: 'observed',
    started_at: '2026-11-08T00:15:00Z',
    ended_at: '2026-11-08T00:30:00Z',
  }),
];

const INTENTS: DayIntentSummary[] = [
  intent('intent-proposed', { state: 'proposed', source_kind: 'model', target_start_day: '2026-11-09', target_end_day: '2026-11-09' }),
  intent('intent-planned', { state: 'planned', source_kind: 'user', target_start_day: '2026-11-10', target_end_day: '2026-11-10' }),
  intent('intent-confirmed', { state: 'confirmed', source_kind: 'recurrence', target_start_day: '2026-11-11', target_end_day: '2026-11-13' }),
];

function timeline(overrides: Partial<LifeTimelineOut> = {}): LifeTimelineOut {
  return {
    schema_version: 'v1',
    state: 'ready',
    plan_state: 'ready',
    revision: 3,
    last_error_code: null,
    display_date: '2026-11-08',
    display_timezone: DISPLAY_TZ,
    display_timezone_source: 'preference',
    display_window_start_at: '2026-11-07T16:00:00Z',
    display_window_end_at: '2026-11-08T16:00:00Z',
    actor_timezone: ACTOR_TZ,
    actor_timezone_source: 'preference',
    plan_day: '2026-11-07',
    plan_window_start_at: '2026-11-07T05:00:00Z',
    plan_window_end_at: '2026-11-08T05:00:00Z',
    outline_due_at: '2026-11-07T02:00:00Z',
    timezone_source: 'preference',
    generated_at: '2026-11-07T13:45:00Z',
    segments: SEGMENTS,
    facts: FACTS,
    intents: INTENTS,
    horizon_unplanned: true,
    dream: {
      schema_version: 'v1',
      status: 'ready',
      origin: 'simulated',
      visibility: 'actor_private',
      title: '一条很长的走廊',
      narrative: '她在走廊里走了很久。',
      restricted: false,
    },
    superseded_segment_count: 2,
    hidden_segment_count: 0,
    hidden_fact_count: 0,
    hidden_intent_count: 0,
    has_private_content: false,
    note: null,
    ...overrides,
  };
}

const NOW = new Date('2026-11-08T00:10:00Z');

function renderTimeline(overrides: Partial<LifeTimelineOut> = {}, props: { defaultCollapsed?: string[] } = {}): string {
  return renderToStaticMarkup(
    h(LifeTimeline, { timeline: timeline(overrides), now: NOW, ...props }),
  );
}

/** 取某个 `data-testid` 开头的元素片段（到下一个同类标签结束），用于定位断言。 */
function sliceByTestId(html: string, testId: string, tag = 'li'): string {
  const at = html.indexOf(`data-testid="${testId}"`);
  expect(at, `找不到 data-testid=${testId}`).toBeGreaterThanOrEqual(0);
  const start = html.lastIndexOf(`<${tag}`, at);
  const end = html.indexOf(`</${tag}>`, at);
  expect(end).toBeGreaterThan(start);
  return html.slice(start, end + tag.length + 3);
}

const CSS = readFileSync(new URL('./timeline.css', import.meta.url), 'utf8');

/* -------------------------------------------------------------- 顶部与状态 */

describe('时间轴顶部：两套日期 / 时区 / 时间戳', () => {
  it('同时显示「你的今天」与「她的计划日」，日期不同时给出解释', () => {
    const html = renderTimeline();
    expect(html).toContain('你的今天（展示日）');
    expect(html).toContain('2026-11-08');
    expect(html).toContain(DISPLAY_TZ);
    expect(html).toContain('她的计划日');
    expect(html).toContain('2026-11-07');
    expect(html).toContain(ACTOR_TZ);
    // 不是同一个日期 -> 必须解释，而不是只写"今天"。
    const note = sliceByTestId(html, 'tl-tz-note', 'p');
    expect(note).toContain('不是同一天');
    expect(note).toContain(DISPLAY_TZ);
    expect(note).toContain(ACTOR_TZ);
  });

  it('generated_at 与 outline_due_at 分开显示，不合并成一句「更新于」', () => {
    const html = renderTimeline();
    expect(html).toContain('生成到期');
    expect(html).toContain('实际提交');
    // outline_due_at = 11-07T02:00Z -> 上海 10:00；generated_at 13:45Z -> 上海 21:45。
    expect(sliceByTestId(html, 'tl-due-at', 'dd')).toContain('11-07 10:00');
    expect(sliceByTestId(html, 'tl-generated-at', 'dd')).toContain('11-07 21:45');
    expect(html).not.toMatch(/更新于[^<]*<[^>]*>[^<]*生成到期/);
  });

  it('两侧时区相同、日期相同则不需要解释句', () => {
    expect(
      timezoneGapNote({
        display_date: '2026-11-08',
        display_timezone: 'Asia/Shanghai',
        plan_day: '2026-11-08',
        actor_timezone: 'Asia/Shanghai',
      }),
    ).toBeNull();
  });
});

describe('state 六值文案', () => {
  const allowed: TimelineState[] = ['ready', 'degraded', 'failed', 'pending', 'empty', 'unavailable'];

  it('每个状态都有自己的标签（不共用一句"没有安排"）', () => {
    const labels = allowed.map((state) => TIMELINE_STATE_COPY[state].label);
    expect(new Set(labels).size).toBe(allowed.length);
    for (const state of allowed) {
      const html = renderTimeline({ state, segments: [], facts: [], intents: [] });
      expect(html).toContain(`data-state="${state}"`);
      expect(html).toContain(TIMELINE_STATE_COPY[state].label);
    }
  });

  it('degraded / failed 把 last_error_code 当原因码原样展示', () => {
    const degraded = renderTimeline({ state: 'degraded', last_error_code: 'degraded_late' });
    expect(degraded).toContain('原因码：degraded_late');
    expect(degraded).toContain('来自规则兜底');

    const failed = renderTimeline({ state: 'failed', last_error_code: 'model_timeout' });
    expect(failed).toContain('原因码：model_timeout');
    expect(failed).toContain('生成失败');
    // 原因码不是给人看的中文解释，不能被替换成"出错了"。
    expect(failed).not.toContain('出错了');
  });

  it('ready / empty 不显示原因码', () => {
    expect(renderTimeline({ state: 'ready' })).not.toContain('原因码：');
    expect(renderTimeline({ state: 'empty', segments: [], facts: [], intents: [] })).not.toContain('原因码：');
  });

  it('unavailable 给「重试」，且绝不显示成「今天没安排」', () => {
    const html = renderTimeline({
      state: 'unavailable',
      segments: [],
      facts: [],
      intents: [],
      generated_at: null,
    });
    expect(html).toContain('暂时无法读取');
    expect(html).toContain('重试');
    expect(sliceByTestId(html, 'tl-unavailable', 'p')).toContain('这不代表她今天没有安排');
    // 空态/空事实/空意向的措辞一个都不能出现。
    expect(html).not.toContain('这一天还没有安排');
    expect(html).not.toContain('这一天没有计划段');
    expect(html).not.toContain('还没有带证据的活动记录');
  });

  it('empty / pending / failed 的空白文案各不相同', () => {
    const empty = renderTimeline({ state: 'empty', segments: [] });
    const pending = renderTimeline({ state: 'pending', segments: [] });
    const failed = renderTimeline({ state: 'failed', segments: [] });
    expect(empty).toContain('这一天没有计划段');
    expect(pending).toContain('大纲还没生成');
    expect(failed).toContain('生成失败了');
    expect(new Set([
      segmentEmptyText({ state: 'empty', has_private_content: false, hidden_segment_count: 0 }),
      segmentEmptyText({ state: 'pending', has_private_content: false, hidden_segment_count: 0 }),
      segmentEmptyText({ state: 'failed', has_private_content: false, hidden_segment_count: 0 }),
    ]).size).toBe(3);
  });
});

/* ------------------------------------------------------------ 计划 vs 事实 */

describe('计划层与事实层分开', () => {
  it('计划是虚线/浅色的 .tl-seg--plan，事实是实心的 .tl-fact--solid', () => {
    const html = renderTimeline();
    expect(html).toContain('tl-seg--plan');
    expect(html).toContain('tl-fact--solid');
    // CSS 里确实用了虚线（类名不是唯一证据）。
    expect(CSS).toMatch(/\.tl-seg\s*\{[^}]*border: 1px dashed/);
    expect(CSS).toMatch(/\.tl-fact--solid\s*\{[^}]*border: 1px solid/);
  });

  it('clock_only 只写「时间到了」；observed / user_confirmed 才写「做过」', () => {
    expect(evidenceLabel('clock_only')).toBe('时间到了');
    expect(evidenceLabel('observed')).toBe('做过');
    expect(evidenceLabel('user_confirmed')).toContain('做过');

    const html = renderTimeline();
    expect(html).toContain('data-evidence="clock_only"');
    expect(html).toContain('时间到了');
    expect(html).toContain('data-evidence="observed"');
    expect(html).toContain('做过');
  });

  it('同一事实只挂一次（父段不再重复子段里的事实）', () => {
    const html = renderTimeline();
    const occurrences = html.split('data-testid="tl-fact-fact-clock"').length - 1;
    expect(occurrences).toBe(1);
    const observed = html.split('data-testid="tl-fact-fact-observed"').length - 1;
    expect(observed).toBe(1);
  });

  it('scene_valid_until=null 时说「此刻情景状态未知」', () => {
    const html = renderTimeline();
    expect(html).toContain('此刻情景状态未知');
    const valid = renderTimeline({
      facts: [fact('fact-valid', { scene_valid_until: '2026-11-08T01:00:00Z' })],
    });
    expect(valid).toContain('情景有效至');
  });
});

/* ------------------------------------------------------------ 跨天与悬停 */

describe('跨午夜标记与真实起止', () => {
  it('段首标「← 前一天」、段尾标「→ 次日」', () => {
    const html = renderTimeline();
    expect(html).toContain('← 前一天');
    expect(html).toContain('→ 次日');
  });

  it('tooltip 用真实 start_at/end_at，画位置用 display_*', () => {
    const html = renderTimeline();
    // 睡觉：真实 21:00（11-07）→ 06:00（11-08），展示日只画 00:00–06:00。
    expect(html).toContain('真实起止 11-07 21:00–11-08 06:00');
    expect(html).toContain('min-height:');
    // 绘制时间轴上它从 00:00 起（裁剪值），与真实起点不同。
    const night = sliceByTestId(html, 'tl-continues-from', 'span');
    expect(night).toContain('← 前一天');
    expect(html).toContain('00:00');
  });
});

/* ---------------------------------------------------------------- 层级 */

describe('层级折叠（parent_id）', () => {
  it('buildSegmentTree 把小时/分钟挂到父段下', () => {
    const tree = buildSegmentTree(SEGMENTS);
    const block = tree.find((node) => node.segment.segment_id === 'seg-block');
    expect(block).toBeDefined();
    expect(block?.children.map((child) => child.segment.segment_id)).toEqual(['seg-hour']);
    expect(block?.children[0]?.children.map((child) => child.segment.segment_id)).toEqual([
      'seg-minute',
    ]);
  });

  it('父段有展开/收起按钮；默认展开，传 defaultCollapsed 后子段不再渲染', () => {
    const expanded = renderTimeline();
    const toggle = sliceByTestId(expanded, 'tl-toggle-seg-block', 'button');
    expect(toggle).toContain('aria-expanded="true"');
    expect(expanded).toContain('data-testid="tl-seg-seg-hour"');
    expect(expanded).toContain('data-testid="tl-seg-seg-minute"');

    const collapsed = renderTimeline({}, { defaultCollapsed: ['seg-block'] });
    const collapsedToggle = sliceByTestId(collapsed, 'tl-toggle-seg-block', 'button');
    expect(collapsedToggle).toContain('aria-expanded="false"');
    expect(collapsed).not.toContain('data-testid="tl-seg-seg-hour"');
    expect(collapsed).not.toContain('data-testid="tl-seg-seg-minute"');
  });

  it('block / hour / minute 用不同的层级类名（缩进与字号在 CSS 里）', () => {
    const html = renderTimeline();
    expect(html).toContain('tl-seg--layer-block');
    expect(html).toContain('tl-seg--layer-hour');
    expect(html).toContain('tl-seg--layer-minute');
    expect(CSS).toMatch(/\.tl-seg--layer-hour\s*\{[^}]*margin-left/);
    expect(CSS).toMatch(/\.tl-seg--layer-minute\s*\{[^}]*margin-left/);
    expect(CSS).toMatch(/\.tl-seg--layer-minute\s*\{[^}]*font-size/);
  });

  it('段高按时间比例，且有下限与上限', () => {
    // 6 小时 = 360 分钟 → 216px，封顶 180px。
    expect(segmentHeightPx(SEGMENTS.find((s) => s.segment_id === 'seg-night')!)).toBe(180);
    // 4 小时 = 240 分钟 → 144px（未封顶）。
    expect(segmentHeightPx(SEGMENTS.find((s) => s.segment_id === 'seg-block')!)).toBe(144);
    // 15 分钟 → 9px，抬到下限 36px。
    expect(segmentHeightPx(SEGMENTS.find((s) => s.segment_id === 'seg-minute')!)).toBe(36);
    const html = renderTimeline();
    expect(html).toContain('min-height:144px');
    expect(html).toContain('min-height:36px');
  });
});

/* ---------------------------------------------------------------- 三态 */

describe('已过去 / 此刻 / 未到', () => {
  it('按展示区间与参照时刻判三态', () => {
    const byId = (id: string) => SEGMENTS.find((s) => s.segment_id === id)!;
    expect(segmentPhase(byId('seg-night'), NOW)).toBe('past');
    expect(segmentPhase(byId('seg-block'), NOW)).toBe('now');
    expect(segmentPhase(byId('seg-midnight'), NOW)).toBe('upcoming');

    const html = renderTimeline();
    expect(html).toContain('data-phase="past"');
    expect(html).toContain('data-phase="now"');
    expect(html).toContain('data-phase="upcoming"');
    expect(html).toContain('此刻');
  });
});

/* ------------------------------------------------------------ 锚点与承诺 */

describe('锚点措辞', () => {
  it('睡眠锚点只写「预计休息」，不写"她睡着了"', () => {
    const html = renderTimeline();
    const sleep = sliceByTestId(html, 'tl-seg-seg-night', 'li');
    expect(sleep).toContain('预计休息');
    expect(html).not.toContain('睡着了');
    const sleepSegment = SEGMENTS.find((s) => s.segment_id === 'seg-night')!;
    expect(commitmentTag(sleepSegment)).toBe('预计休息');
  });

  it('只有 commitment+confirmed 才是「已约好」', () => {
    const confirmedCommitment = segment('seg', {
      anchor_kind: 'commitment',
      commitment_level: 'confirmed',
    });
    expect(commitmentTag(confirmedCommitment)).toBe('已约好');

    const tentativeCommitment = segment('seg', {
      anchor_kind: 'commitment',
      commitment_level: 'tentative',
    });
    expect(commitmentTag(tentativeCommitment)).not.toContain('约好');

    const confirmedVariable = segment('seg', {
      anchor_kind: '',
      commitment_level: 'confirmed',
    });
    expect(commitmentTag(confirmedVariable)).not.toContain('约好');

    // 渲染层面同样成立：tentative 的 commitment 段不会出现"已约好"。
    const html = renderTimeline({
      segments: [tentativeCommitment],
      facts: [],
    });
    expect(html).not.toContain('已约好');
    expect(html).toContain('固定时段（未确认）');
  });

  it('已确认的承诺锚点渲染出「已约好」', () => {
    const html = renderTimeline();
    const commit = sliceByTestId(html, 'tl-seg-seg-commit', 'li');
    expect(commit).toContain('已约好');
  });
});

/* ---------------------------------------------------------------- 梦三态 */

describe('梦卡片三态', () => {
  it('dream=null → 梦没有记录（不是"看不到"）', () => {
    expect(dreamView(null)).toBe('none');
    const html = renderToStaticMarkup(h(DreamCardView, { dream: null }));
    expect(html).toContain('梦没有记录');
    expect(html).not.toContain('私有');
  });

  it('restricted=true → 有记录但你看不到正文（不是"没有记录"）', () => {
    const restricted: DreamCard = {
      schema_version: 'v1',
      status: 'ready',
      origin: 'simulated',
      visibility: 'actor_private',
      title: null,
      narrative: null,
      restricted: true,
    };
    expect(dreamView(restricted)).toBe('restricted');
    const html = renderToStaticMarkup(h(DreamCardView, { dream: restricted }));
    expect(html).toContain('梦是角色私有内容，你看不到正文');
    expect(html).not.toContain('梦没有记录');
  });

  it('看得到时标「模拟梦景」并给出正文', () => {
    const html = renderToStaticMarkup(
      h(DreamCardView, {
        dream: {
          schema_version: 'v1',
          status: 'ready',
          origin: 'simulated',
          visibility: 'actor_private',
          title: '一条很长的走廊',
          narrative: '她在走廊里走了很久。',
          restricted: false,
        },
      }),
    );
    expect(dreamView({ restricted: false } as DreamCard)).toBe('content');
    expect(html).toContain('模拟梦景');
    expect(html).toContain('一条很长的走廊');
    expect(html).toContain('她在走廊里走了很久。');
  });
});

/* ---------------------------------------------------------------- 意向 */

describe('未来意向区', () => {
  it('intents 与 horizon_unplanned 一起出现，写明"未来日还未生成大纲"', () => {
    const html = renderTimeline();
    expect(html).toContain('data-testid="tl-intents"');
    expect(html).toContain('未来日还未生成大纲');
    expect(html).toContain('这些只是意向');
  });

  it('三种 state 的措辞：候选 / 想·考虑 / 已确认目标但时段待定', () => {
    const html = renderTimeline();
    expect(html).toContain('候选');
    expect(html).toContain('想/考虑');
    expect(html).toContain('已确认想在这个日期段做（时段仍待定）');
    // confirmed 也**不能**说"已约好"。
    const intents = sliceByTestId(html, 'tl-intents', 'section');
    expect(intents).not.toContain('已约好');
    expect(intents).not.toContain('约好');
  });

  it('没有意向时也不假装有；有隐藏时用计数说明', () => {
    const empty = renderTimeline({ intents: [] });
    expect(empty).toContain('未来 7 天还没有有来源的意向');
    const hidden = renderTimeline({
      intents: [],
      hidden_intent_count: 2,
      has_private_content: true,
    });
    expect(hidden).toContain('有 2 条意向因受众裁决被隐藏');
  });
});

/* ---------------------------------------------------------------- 隐私 */

describe('私密内容与空数组', () => {
  it('空数组 + hidden 计数 → 说明"被隐藏"而不是"没有内容"', () => {
    const html = renderTimeline({
      segments: [],
      facts: [],
      intents: [],
      hidden_segment_count: 3,
      hidden_fact_count: 2,
      hidden_intent_count: 1,
      has_private_content: true,
    });
    expect(html).toContain('有私密内容被隐藏');
    expect(html).toContain('计划段 3 条');
    expect(html).toContain('活动 2 条');
    expect(html).toContain('意向 1 条');
    expect(html).toContain('有 3 条计划段因受众裁决被隐藏');
    expect(html).toContain('有 2 条活动因受众裁决被隐藏');
  });

  it('hiddenSummary 在没有隐藏时返回 null', () => {
    expect(
      hiddenSummary({
        has_private_content: false,
        hidden_segment_count: 0,
        hidden_fact_count: 0,
        hidden_intent_count: 0,
      }),
    ).toBeNull();
  });
});

/* ---------------------------------------------------------------- 时区 */

describe('时区换算', () => {
  it('同一 UTC 瞬时按给定 IANA 时区显示，不读浏览器时区', () => {
    expect(formatInZone('2026-11-07T16:00:00Z', 'Asia/Shanghai')).toBe('00:00');
    expect(formatInZone('2026-11-07T16:00:00Z', 'America/New_York')).toBe('11:00');
    expect(formatInZone('2026-11-07T16:00:00Z', 'UTC')).toBe('16:00');
  });

  it('非法时区退回 UTC，而不是浏览器本地时区', () => {
    expect(formatInZone('2026-11-07T16:00:00Z', 'Not/AZone')).toBe('16:00');
  });
});

/* -------------------------------------------------------- 直接渲染子轨道 */

describe('TimelineTrack 单独渲染', () => {
  it('只有子段、父段缺失时，子段被当成根段画出来（不静默吞掉）', () => {
    const orphan = segment('seg-orphan', { parent_id: 'missing-parent' });
    const html = renderToStaticMarkup(
      h(TimelineTrack, {
        segments: [orphan],
        facts: [],
        timeZone: DISPLAY_TZ,
        now: NOW,
      }),
    );
    expect(html).toContain('data-testid="tl-seg-seg-orphan"');
  });
});
