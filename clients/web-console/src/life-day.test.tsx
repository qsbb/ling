import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { activityKind, futureSegments, LifeDay } from './components/LifeDay';
import type { ActivityResponse, LifeTimelineOut } from './types';

vi.useFakeTimers();
vi.setSystemTime(new Date('2026-11-08T00:10:00Z'));

const TIMELINE: LifeTimelineOut = {
  schema_version: 'v1', state: 'ready', plan_state: 'ready', revision: 1,
  last_error_code: null, display_date: '2026-11-08', display_timezone: 'Asia/Shanghai',
  display_timezone_source: 'preference', display_window_start_at: '2026-11-07T16:00:00Z',
  display_window_end_at: '2026-11-08T16:00:00Z', actor_timezone: 'Asia/Shanghai',
  actor_timezone_source: 'preference', plan_day: '2026-11-08',
  plan_window_start_at: '2026-11-07T16:00:00Z', plan_window_end_at: '2026-11-08T16:00:00Z',
  outline_due_at: '', timezone_source: 'preference', generated_at: null,
  segments: [{
    schema_version: 'v1', segment_id: 'reading', plan_id: 'p1', plan_day: '2026-11-08',
    parent_id: null, layer: 'minute', is_leaf: true, status: 'current', revision: 1,
    source: 'model', anchor_kind: '', commitment_level: 'tentative', place_id: null,
    title: '读一会儿书', start_at: '2026-11-08T00:20:00Z', end_at: '2026-11-08T01:20:00Z',
    display_start_at: '2026-11-08T00:20:00Z', display_end_at: '2026-11-08T01:20:00Z',
    continues_from_previous_day: false, continues_next_day: false,
  }], facts: [], intents: [], horizon_unplanned: false, dream: null,
  superseded_segment_count: 0, hidden_segment_count: 0, hidden_fact_count: 0,
  hidden_intent_count: 0, has_private_content: false, note: null,
};

describe('生活首屏', () => {
  it('无明确 running 活动时保持未知，不从计划推断她正在做什么', () => {
    for (const current of [null, { activity_id: 'a', kind: 'reading', state: 'completed', origin: 'simulated', started_at: null, ended_at: null, completion_evidence: null }]) {
      const html = renderToStaticMarkup(h(LifeDay, { activity: { current, history: [], note: '' }, timeline: TIMELINE }));
      expect(html).toContain('data-activity="unknown"');
      expect(html).toContain('这一刻，还没有她的消息');
      expect(html).not.toContain('正在读书');
    }
  });

  it('只有 running 才展示明确活动，非事实计划仍留在今天', () => {
    const activity: ActivityResponse = { current: {
      activity_id: 'a', kind: 'read', state: 'running', origin: 'simulated',
      started_at: '2026-11-08T00:05:00Z', ended_at: null, completion_evidence: null, note: '窗边有点风。',
    }, history: [], note: '' };
    const html = renderToStaticMarkup(h(LifeDay, { activity, timeline: TIMELINE }));
    expect(html).toContain('data-activity="running"');
    expect(html).toContain('正在读书');
    expect(html).toContain('窗边有点风。');
    expect(html.indexOf('life-presence')).toBeLessThan(html.indexOf('life-today-title'));
    expect(html).toContain('<strong>读一会儿书</strong>');
    expect(activityKind('writing')).toBe('写作');
  });

  it('已发生的片段只匹配同一个活动，并排除时钟推演', () => {
    const observed = {
      schema_version: 'v1', activity_id: 'act-observed', origin: 'tool_receipt',
      state: 'completed', evidence_kind: 'observed', scene_valid_until: null,
      started_at: '2026-11-08T00:01:00Z', ended_at: '2026-11-08T00:05:00Z', place_id: null,
    };
    const completed = {
      activity_id: observed.activity_id, kind: 'write', state: 'completed', origin: 'tool_receipt',
      started_at: observed.started_at, ended_at: observed.ended_at, completion_evidence: '已保存',
    };
    const html = renderToStaticMarkup(h(LifeDay, {
      activity: {
        current: null, note: '',
        history: [completed, { ...completed, activity_id: 'act-clock', kind: 'sleep', origin: 'simulated' }],
      },
      timeline: { ...TIMELINE, facts: [observed, { ...observed, activity_id: 'act-clock', evidence_kind: 'clock_only' }] },
    }));
    expect(html).toContain('今天留下的片段');
    expect(html).toContain('08:05 · </time>写作');
    expect(html).not.toContain('睡觉');
  });

  it('今天排除父层、过期与非当前计划', () => {
    const segment = TIMELINE.segments[0]!;
    const filtered = futureSegments({ ...TIMELINE, segments: [segment,
      { ...segment, segment_id: 'past', display_end_at: '2026-11-08T00:00:00Z' },
      { ...segment, segment_id: 'parent', is_leaf: false },
      { ...segment, segment_id: 'cancelled', status: 'cancelled' },
    ] }, new Date('2026-11-08T00:10:00Z'));
    expect(filtered.map((item) => item.segment_id)).toEqual(['reading']);
  });
});
