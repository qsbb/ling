import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { LifeDay, futureSegments } from './components/LifeDay';
import { LifeTimeline } from './components/LifeTimeline';
import { activityName, factOriginLabel, formatRecordRange, mergeDayTimeline, visibleRange } from './components/LifeTimeline';
import type { LifeTimelineFact, LifeTimelineOut, LifeTimelineSegment } from './types';

vi.useFakeTimers();
vi.setSystemTime(new Date('2026-11-08T00:10:00Z'));

const DAY = '2026-11-08';

const TIMELINE: LifeTimelineOut = {
  schema_version: 'v1', state: 'ready', plan_state: 'ready', revision: 1,
  last_error_code: null, display_date: DAY, display_timezone: 'Asia/Shanghai',
  display_timezone_source: 'preference', display_window_start_at: '2026-11-07T16:00:00Z',
  display_window_end_at: '2026-11-08T16:00:00Z', actor_timezone: 'Asia/Shanghai',
  actor_timezone_source: 'preference', plan_day: DAY,
  plan_window_start_at: '2026-11-07T16:00:00Z', plan_window_end_at: '2026-11-08T16:00:00Z',
  outline_due_at: '', timezone_source: 'preference', generated_at: null,
  segments: [{
    schema_version: 'v1', segment_id: 'reading', plan_id: 'p1', plan_day: DAY,
    parent_id: null, layer: 'minute', is_leaf: true, status: 'current', revision: 1,
    source: 'model', anchor_kind: '', commitment_level: 'tentative', place_id: null,
    title: '读一会儿书', start_at: '2026-11-08T00:20:00Z', end_at: '2026-11-08T01:20:00Z',
    display_start_at: '2026-11-08T00:20:00Z', display_end_at: '2026-11-08T01:20:00Z',
    continues_from_previous_day: false, continues_next_day: false,
  }], facts: [], intents: [], horizon_unplanned: false, dream: null,
  superseded_segment_count: 0, hidden_segment_count: 0, hidden_fact_count: 0,
  hidden_intent_count: 0, has_private_content: false, note: null,
};

function fact(overrides: Partial<LifeTimelineFact> & Pick<LifeTimelineFact, 'activity_id'>): LifeTimelineFact {
  return {
    schema_version: 'v1', kind: null, state: 'completed', origin: 'tool_receipt',
    evidence_kind: 'observed', scene_valid_until: null,
    started_at: null, ended_at: null, place_id: null, ...overrides,
  };
}

function segment(overrides: Partial<LifeTimelineSegment> & Pick<LifeTimelineSegment, 'segment_id'>): LifeTimelineSegment {
  const base = TIMELINE.segments[0]!;
  return { ...base, ...overrides };
}

describe('生活首屏', () => {
  /**
   * 没有 running 情景时必须是"未知"。
   *
   * 三种输入各一条，因为三种**错误的实现**会让它们分别出错：
   * - 无事实 → 有的实现会拿计划时段当"她正在做"；
   * - 有 completed 事实 → 有的实现会把"刚做完"当成"正在做"；
   * - running 但情景已过期 → 有的实现只看 state、不看有效期。
   * 只测其中一种，另外两种错误照样绿。
   */
  it('没有有效情景时保持未知，不从计划或已结束的活动推断', () => {
    const expired = fact({
      activity_id: 'expired', kind: 'reading', state: 'running',
      started_at: '2026-11-08T00:00:00Z', scene_valid_until: '2026-11-08T00:05:00Z',
    });
    const completed = fact({
      activity_id: 'done', kind: 'reading', state: 'completed',
      started_at: '2026-11-08T00:00:00Z', ended_at: '2026-11-08T00:05:00Z',
    });
    for (const facts of [[], [completed], [expired]]) {
      const html = renderToStaticMarkup(h(LifeDay, {
        timeline: { ...TIMELINE, facts },
        now: new Date('2026-11-08T00:10:00Z'),
      }));
      expect(html).toContain('data-activity="unknown"');
      expect(html).toContain('暂无当前活动记录');
      expect(html).not.toContain('正在读书');
    }
  });

  it('有效情景才说"正在做"，并给出开始时刻与有效期', () => {
    const live = fact({
      activity_id: 'live', kind: 'reading', state: 'running',
      started_at: '2026-11-08T00:05:00Z', scene_valid_until: '2026-11-08T00:35:00Z',
    });
    const html = renderToStaticMarkup(h(LifeDay, {
      timeline: { ...TIMELINE, facts: [live] },
      now: new Date('2026-11-08T00:10:00Z'),
    }));
    expect(html).toContain('data-activity="running"');
    expect(html).toContain('正在读书');
    expect(html).toContain('08:05 起');
    expect(html).toContain('情景有效至 08:35');
  });

  /**
   * 「接下来」只指**还没开始**的那一段。
   *
   * 这里把 now 放在段落中间：此时 00:20–01:20 这一段正在进行。
   * 正确的实现不该把它列进「接下来」（此刻那一块已经承担了它），
   * 而错误实现（只按 end > now 过滤）会把它重现一次。
   */
  it('接下来只列尚未开始的时段，不重复正在进行的那一段', () => {
    const now = new Date('2026-11-08T00:40:00Z');   // 落在 00:20–01:20 之内
    const live = fact({
      activity_id: 'live', kind: 'reading', state: 'running',
      started_at: '2026-11-08T00:30:00Z', scene_valid_until: '2026-11-08T01:00:00Z',
    });
    const later = segment({
      segment_id: 'later', title: '晚饭',
      display_start_at: '2026-11-08T02:00:00Z', display_end_at: '2026-11-08T03:00:00Z',
    });
    const html = renderToStaticMarkup(h(LifeDay, {
      timeline: { ...TIMELINE, segments: [...TIMELINE.segments, later], facts: [live] },
      now,
    }));
    expect(html).toContain('data-testid="life-next"');
    expect(html).toContain('晚饭');
    // 正在进行的那一段不应出现在「接下来」里。
    const nextBlock = html.slice(html.indexOf('data-testid="life-next"'));
    expect(nextBlock.slice(0, nextBlock.indexOf('</section>'))).not.toContain('读一会儿书');
  });

  it('今天排除父层、过期与非当前计划', () => {
    const seg = TIMELINE.segments[0]!;
    const filtered = futureSegments({ ...TIMELINE, segments: [seg,
      { ...seg, segment_id: 'past', display_end_at: '2026-11-08T00:00:00Z' },
      { ...seg, segment_id: 'parent', is_leaf: false },
      { ...seg, segment_id: 'cancelled', status: 'cancelled' },
    ] }, new Date('2026-11-08T00:10:00Z'));
    expect(filtered.map((item) => item.segment_id)).toEqual(['reading']);
  });
});

describe('活动名字只来自 facts[].kind', () => {
  it('已知类型给中文名', () => {
    expect(activityName('writing')).toBe('写作');
    expect(activityName('reading')).toBe('读书');
  });

  /**
   * 三条"说不出名字"的输入必须都落到「活动记录」。
   * 只测空串会让"把 null 拼成 'null'"这种实现照样通过。
   */
  it('缺失、空串、无法映射都显示「活动记录」', () => {
    expect(activityName(undefined)).toBe('活动记录');
    expect(activityName(null)).toBe('活动记录');
    expect(activityName('')).toBe('活动记录');
    expect(activityName('   ')).toBe('活动记录');
  });

  /**
   * 未知类型**不**原样露出。
   *
   * `kind` 在后端是自由字符串；把 `wood_working` 这种内部代码摆到界面上，
   * 读者看到的不是"她做了什么"，而是我们的字段名。说不出就回落到「活动记录」。
   */
  it('未知类型回落到「活动记录」，不把内部代码摆到界面上', () => {
    expect(activityName('wood_working')).toBe('活动记录');
    expect(activityName('some_future_kind')).toBe('活动记录');
  });

  it('常见类型给中文名（含接电话这类非"做"的动词）', () => {
    expect(activityName('call')).toBe('接电话');
    expect(activityName('cooking')).toBe('做饭');
    expect(activityName('nap')).toBe('小睡');
  });

  /**
   * 首屏不再读 `/v1/life/activity`：给一条 kind=null 的事实，
   * 界面必须显示「活动记录」，而不是拿 history 里的名字补上。
   */
  it('首屏不靠 history 补名字', () => {
    const unknown = fact({
      activity_id: 'unknown-kind', kind: null, state: 'completed',
      started_at: '2026-11-08T00:01:00Z', ended_at: '2026-11-08T00:05:00Z',
    });
    const html = renderToStaticMarkup(h(LifeDay, {
      timeline: { ...TIMELINE, facts: [unknown] },
      now: new Date('2026-11-08T00:10:00Z'),
    }));
    expect(html).toContain('活动记录');
    expect(html).not.toContain('写作');
  });
});

describe('合并视图：每条活动只出现一次', () => {
  /**
   * 横跨两个**兄弟叶段**的记录。
   *
   * 这是原来那套 `factsForSegment()` 漏掉的情形：它只排除父子重复，
   * 两个兄弟段各挂一次。错误实现下 `bySegment` 里会出现两条。
   */
  it('横跨两个兄弟时段只归属一个', () => {
    const left = segment({
      segment_id: 'left', title: '午饭',
      display_start_at: '2026-11-08T00:00:00Z', display_end_at: '2026-11-08T01:00:00Z',
    });
    const right = segment({
      segment_id: 'right', title: '休息',
      display_start_at: '2026-11-08T01:00:00Z', display_end_at: '2026-11-08T02:00:00Z',
    });
    const straddle = fact({
      activity_id: 'straddle', kind: 'meal', started_at: '2026-11-08T00:50:00Z',
      ended_at: '2026-11-08T01:10:00Z',
    });
    const merged = mergeDayTimeline({ segments: [left, right], facts: [straddle] });
    expect(merged.records).toHaveLength(1);
    // 落在包含**开始时刻**的那一段（left），不是两段都挂。
    expect(merged.bySegment.get('left')?.map((r) => r.fact.activity_id)).toEqual(['straddle']);
    expect(merged.bySegment.get('right')).toBeUndefined();
  });

  /**
   * `clock_only` 不进时间线主体。
   *
   * 错误实现（不过滤 evidence_kind）会把它当成一条记录归属到段里，
   * 于是"时间到了"被读成"她做了"。
   */
  it('clock_only 只进时间检查，不参与归属', () => {
    const seg = segment({
      segment_id: 'seg', title: '起床',
      display_start_at: '2026-11-08T00:00:00Z', display_end_at: '2026-11-08T02:00:00Z',
    });
    const clock = fact({
      activity_id: 'clock', kind: 'sleep', evidence_kind: 'clock_only',
      started_at: '2026-11-08T00:00:00Z', ended_at: '2026-11-08T00:30:00Z',
    });
    const merged = mergeDayTimeline({ segments: [seg], facts: [clock] });
    expect(merged.records).toHaveLength(0);
    expect(merged.checks.map((f) => f.activity_id)).toEqual(['clock']);
    expect(merged.bySegment.size).toBe(0);
  });

  /**
   * 没有宿主的记录保留自己的开始时间，不被塞进任何段。
   * 错误实现（"落到最近的一段"）会让它出现在段里，时间被段的边界改写。
   */
  it('找不到宿主时独立出现并保留开始时间', () => {
    const seg = segment({
      segment_id: 'seg', title: '上午',
      display_start_at: '2026-11-08T00:00:00Z', display_end_at: '2026-11-08T00:30:00Z',
    });
    const outside = fact({
      activity_id: 'outside', kind: 'walking',
      started_at: '2026-11-08T05:00:00Z', ended_at: '2026-11-08T05:20:00Z',
    });
    const merged = mergeDayTimeline({ segments: [seg], facts: [outside] });
    expect(merged.loose.map((r) => r.fact.activity_id)).toEqual(['outside']);
    expect(merged.loose[0]!.fact.started_at).toBe('2026-11-08T05:00:00Z');
    expect(merged.bySegment.size).toBe(0);
  });

  /**
   * 同一批事实里同一条**只**出现一次：这是整个合并层的不变量。
   * 用"每个 id 的出现次数都等于 1"来表达，而不是只断言总数。
   */
  it('每条记录在合并结果里恰好出现一次', () => {
    const seg = segment({
      segment_id: 'seg', title: '上午',
      display_start_at: '2026-11-08T00:00:00Z', display_end_at: '2026-11-08T04:00:00Z',
    });
    const facts = [
      fact({ activity_id: 'a', kind: 'writing', started_at: '2026-11-08T00:10:00Z', ended_at: '2026-11-08T00:40:00Z' }),
      fact({ activity_id: 'b', kind: 'reading', started_at: '2026-11-08T01:10:00Z', ended_at: '2026-11-08T01:40:00Z' }),
      fact({ activity_id: 'c', kind: 'resting', started_at: '2026-11-08T06:10:00Z' }),
    ];
    const merged = mergeDayTimeline({ segments: [seg], facts });
    // 按**渲染归属**统计，而不是只看 records 数组：多归属的实现会让
    // records 仍是 3 条，但同一个 id 在 bySegment 里出现两次。
    const rendered = [
      ...[...merged.bySegment.values()].flat().map((r) => r.fact.activity_id),
      ...merged.loose.map((r) => r.fact.activity_id),
      ...merged.checks.map((f) => f.activity_id),
    ];
    expect(new Set(rendered).size).toBe(rendered.length);
    for (const id of ['a', 'b', 'c']) {
      expect(rendered.filter((x) => x === id)).toHaveLength(1);
    }
  });

  /**
   * 兄弟段重叠 + 只有一个宿主：用**两段都完全覆盖**这条记录的情形，
   * 专门盯住"多归属"这个错误实现（records 数量不变，归属却多了一份）。
   */
  it('被两个兄弟段同时覆盖时，归属表里也只有一份', () => {
    const left = segment({
      segment_id: 'left2', title: 'A',
      display_start_at: '2026-11-08T00:00:00Z', display_end_at: '2026-11-08T02:00:00Z',
    });
    const right = segment({
      segment_id: 'right2', title: 'B',
      display_start_at: '2026-11-08T00:30:00Z', display_end_at: '2026-11-08T03:00:00Z',
    });
    const inside = fact({
      activity_id: 'inside', kind: 'writing',
      started_at: '2026-11-08T00:40:00Z', ended_at: '2026-11-08T00:50:00Z',
    });
    const merged = mergeDayTimeline({ segments: [left, right], facts: [inside] });
    const holders = [...merged.bySegment.entries()].filter(([, rs]) =>
      rs.some((r) => r.fact.activity_id === 'inside'));
    expect(holders.map(([id]) => id)).toEqual(['left2']);
  });
});

describe('跨午夜按展示窗口裁剪', () => {
  const timeline = { display_date: DAY, display_timezone: 'Asia/Shanghai' };

  /**
   * 23:30 起、次日 01:30 止：今天这一侧必须裁到午夜，并且**用 24:00 表达**
   * （不是 23:59——那会被读成"提前结束"）。区间不能回绕成 23:30–01:30。
   */
  it('往前跨的区间裁到 24:00，真实起止保留在另一行', () => {
    const crossed = fact({
      activity_id: 'night', kind: 'reading',
      started_at: '2026-11-08T15:30:00Z',   // 上海 23:30
      ended_at: '2026-11-08T17:30:00Z',     // 上海次日 01:30
    });
    const visible = visibleRange(crossed, timeline);
    expect(visible.crossesForward).toBe(true);
    expect(formatRecordRange(visible, 'Asia/Shanghai')).toBe('23:30–24:00');

    // 完整区间在**时间线**里（首屏那一行只给一个时刻，不给区间）。
    const html = renderToStaticMarkup(h(LifeTimeline, {
      timeline: { ...TIMELINE, facts: [crossed] },
      now: new Date('2026-11-08T16:00:00Z'),
    }));
    expect(html).toContain('23:30–24:00');
    expect(html).toContain('跨到次日');
    // 真实起止带日期，不能只剩钟点。
    expect(html).toContain('11-08 23:30');
    expect(html).toContain('11-09 01:30');
    // 首屏不再列具体时刻（只报条数），所以整页都不该出现 23:59 这种回绕读数。
    const hero = renderToStaticMarkup(h(LifeDay, {
      timeline: { ...TIMELINE, facts: [crossed] },
      now: new Date('2026-11-08T16:00:00Z'),
    }));
    expect(hero).not.toContain('23:59');
    expect(hero).toContain('今天已经留下 1 条活动记录');
  });

  /**
   * 从前一天延续进来的部分：这一侧从 00:00 起算。
   * 错误实现会画出 23:30–01:30 这种回绕读数，或者把开始时刻原样显示成 23:30。
   */
  it('从前一天延续的区间从 00:00 起算', () => {
    const carried = fact({
      activity_id: 'carried', kind: 'sleep',
      started_at: '2026-11-07T15:30:00Z',   // 上海 11-07 23:30
      ended_at: '2026-11-07T17:30:00Z',     // 上海 11-08 01:30
    });
    const visible = visibleRange(carried, timeline);
    expect(visible.crossesBackward).toBe(true);
    expect(formatRecordRange(visible, 'Asia/Shanghai')).toBe('00:00–01:30');
  });

  /**
   * 没有结束时间 = 时间点。它不参与跨天裁剪，也不该被写成区间。
   * 这一条单独存在，因为"用 started_at 和缺失的 ended_at 比日期"正好会让它出错。
   */
  it('没有结束时刻的记录只是时间点', () => {
    const point = fact({
      activity_id: 'point', kind: 'resting',
      started_at: '2026-11-08T08:00:00Z',
    });
    const visible = visibleRange(point, timeline);
    expect(visible).toEqual({ start: '2026-11-08T08:00:00Z', end: null, crossesForward: false, crossesBackward: false });
    expect(formatRecordRange(visible, 'Asia/Shanghai')).toBe('16:00–—');
  });
});

describe('来源与类型都不露内部代码', () => {
  /**
   * `origin` 在后端是自由字符串，真实值是 `user_stated`（不是 `user_statement`）。
   * 漏一个拼法，界面上就会直接出现 `user_stated` 这种字段值。
   */
  it('user_stated 显示为「你说的」', () => {
    expect(factOriginLabel('user_stated')).toBe('你说的');
    expect(factOriginLabel('user_statement')).toBe('你说的');
    expect(factOriginLabel('simulated')).toBe('模拟日常');
    expect(factOriginLabel('tool_receipt')).toBe('工具回执');
  });

  it('未知来源显示「其他来源」而不是原值', () => {
    expect(factOriginLabel('weird_origin')).toBe('其他来源');
    expect(factOriginLabel('')).toBe('来源未标注');
  });
});
