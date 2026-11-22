import { useEffect, useState } from 'react';
import type { ActivityResponse, LifeTimelineOut, LifeTimelineSegment } from '../types';
import {
  activityName,
  currentScene,
  formatInZone,
  isLiveNow,
  mergeDayTimeline,
  nextPlannedSegment,
} from './LifeTimeline';

/*
 * 活动类型的映射只在 LifeTimeline 里存一份（`activityName`）。这里保留
 * `activityKind` 这个名字给旧调用点，实现直接委托——两份映射表迟早会漂。
 */

const INTENT_STATE: Record<string, string> = {
  proposed: '一个念头',
  planned: '想安排在',
  confirmed: '已确认这天',
};

/** @deprecated 用 `activityName`：它能在类型缺失/null/未知时说「活动记录」。 */
export function activityKind(kind: string): string {
  return activityName(kind);
}

export function futureSegments(timeline: LifeTimelineOut, now = new Date()): LifeTimelineSegment[] {
  const time = now.getTime();
  return timeline.segments
    .filter((segment) => segment.is_leaf && segment.status === 'current')
    .filter((segment) => new Date(segment.display_end_at).getTime() > time)
    .sort((a, b) => a.display_start_at.localeCompare(b.display_start_at));
}

export function dayLabel(date: string): string {
  const [year, month, day] = date.split('-');
  return year && month && day ? `${Number(month)}月${Number(day)}日` : date;
}

/** 一幅安静的书桌，只表达生活场景，不替未知的活动作证。 */
export function LifeIllustration({ active }: { active: boolean }) {
  return (
    <svg className="life-scene" viewBox="0 0 360 250" fill="none" aria-hidden="true">
      <ellipse cx="186" cy="214" rx="142" ry="17" fill="#d7cbb5" opacity=".36" />
      <path d="M247 31v94" stroke="#c9baa1" strokeWidth="1.5" />
      <rect x="219" y="12" width="94" height="103" rx="47" fill="#ebe3d3" />
      <circle cx="272" cy="57" r="23" fill="#f4ca83" className="life-scene__sun" />
      <path d="M220 99c21-14 38-6 49 6 17-12 32-10 44-3v13h-93Z" fill="#d4d7be" />
      <path d="M67 177h226v10H67zM82 187h8v34h-8zM269 187h8v34h-8z" fill="#ae8769" />
      <path d="M62 173c0-4 4-7 8-7h215c5 0 9 3 9 7v5H62Z" fill="#d4b18d" />
      <path d="m117 132 64 6-7 31-64-6Z" fill="#faf6e9" stroke="#bdab8f" />
      <path d="m181 138 54-9 7 31-68 9Z" fill="#fffaf0" stroke="#bdab8f" />
      <path d="m126 143 38 4m-40 4 32 3m37-6 29-5m-27 12 34-6" stroke="#d2c3aa" strokeLinecap="round" />
      <path d="m200 128 22-23" stroke="#647a63" strokeWidth="3" strokeLinecap="round" />
      <path d="M94 166v-18h23v18" fill="#eee6d3" stroke="#b9a98c" />
      <path d="M117 152c11-2 11 13 0 11" stroke="#b9a98c" strokeWidth="2" />
      <path d="M103 141c-5-8 5-9 1-16m6 16c-4-6 5-8 2-13" stroke="#c7b99e" strokeLinecap="round" className="life-scene__steam" />
      <path d="M281 164v-40" stroke="#8a795f" strokeWidth="3" />
      <path d="M264 104h34l12 28h-58Z" fill={active ? '#e7bb75' : '#ddd5c3'} />
      <ellipse cx="282" cy="162" rx="23" ry="4" fill="#a98e6b" />
      <path d="M37 221h289" stroke="#d4c6ae" strokeLinecap="round" />
    </svg>
  );
}

/**
 * 一个会"自己走"的现在。
 *
 * 页面原来只在渲染那一刻取一次 `new Date()`，所以情景到期后**不会**自己变：
 * 校验过的表现是"有效期还剩 20 秒时显示正在读书，过了 8 秒还在显示，
 * 刷新才变成未知"。这不是数据问题——接口给的 `scene_valid_until` 一直是对的，
 * 是界面没有在到期那一刻重新算。
 *
 * 这里按**最近的到期时刻**安排一次定时器：到期瞬间重算一次，然后把清理掉。
 * 不用固定间隔轮询：那种写法要么太密（每秒重绘整页），要么在到期后仍空转。
 */
function useSceneClock(now: Date | undefined, expiries: readonly (string | null | undefined)[]): Date {
  const fixed = now !== undefined;
  const [tick, setTick] = useState(0);
  // 每次 tick 重新取一次"现在"；固定 now 时直接用它。
  const current = fixed ? now : new Date();

  // 只有到期时刻的集合变化才需要重排定时器（拼成字符串作依赖，避免数组身份每次渲染都变）。
  const expiryKey = expiries.map((value) => value ?? '').join('|');

  useEffect(() => {
    // 测试/验收钉住了 now 时，时间由外部决定，不挂定时器。
    if (fixed) return undefined;
    const upcoming = expiryKey
      .split('|')
      .filter(Boolean)
      .map((value) => new Date(value).getTime())
      .filter((time) => Number.isFinite(time))
      .filter((time) => time > Date.now())
      .sort((a, b) => a - b)[0];
    if (upcoming === undefined) return undefined;
    // 多等 1ms：到期时刻本身仍算有效（比较用 `>`），早一毫秒会漏掉这一帧。
    const delay = Math.max(0, upcoming - Date.now()) + 1;
    const timer = setTimeout(() => setTick((value) => value + 1), delay);
    return () => clearTimeout(timer);
  }, [fixed, expiryKey, tick]);

  return current;
}

interface LifeDayProps {
  timeline: LifeTimelineOut | null;
  timelineFailed?: boolean;
  /**
   * 时间轴拿不到时的兜底来源：`/v1/activity` 的 current。
   *
   * 只在**没有时间线**时用它——时间线是首选依据（它带 scene_valid_until，
   * 能判断情景是否过期）。活动接口没有有效期字段，所以这条路径上情景的有效性
   * 无从确认；界面据此只写"正在做"，**不写**有效期。
   */
  fallbackCurrent?: ActivityResponse['current'] | null;
  /** 判断"此刻"的参照时刻（测试可钉住）。 */
  now?: Date;
}

/**
 * 首屏阅读视图：此刻 → 接下来 → 今天的安排与已留下的记录。
 *
 * 三处口径在这里收敛成一处：**活动记录只从 `mergeDayTimeline()` 来**。
 * 以前这一块自己按 `state === 'completed'` 另筛了一遍「今天留下的片段」，
 * 与时间线里那两处的口径都不一样——同一件事在页面上有三种说法。
 */
export function LifeDay({ timeline, timelineFailed = false, fallbackCurrent, now }: LifeDayProps) {
  // 到期时刻跟着数据走：时间线里所有情景的有效期 + 兜底来源的那个。
  const at = useSceneClock(
    now,
    // 兜底来源（`/v1/activity` 的 current）不带有效期字段，所以没有可挂的到期时刻：
    // 那条路径上的"正在做"本来就不主张自己还在有效期内。
    timeline?.facts.map((fact) => fact.scene_valid_until) ?? [],
  );
  const merged = timeline ? mergeDayTimeline(timeline) : null;
  const scene = timeline ? currentScene(timeline.facts, at) : null;
  /*
   * 时间线读不到时，仍要回答"此刻她是不是在做着什么"——但只能给出活动接口
   * 明确说的 `running`。计划时段**永远**不能顶替它（那是打算，不是事实）。
   */
  const fallbackScene = !timeline && fallbackCurrent?.state === 'running' ? fallbackCurrent : null;
  const headlineKind = scene ? scene.kind : fallbackScene ? fallbackScene.kind : null;
  const headlineStart = scene ? scene.started_at : fallbackScene ? fallbackScene.started_at : null;
  const headlineUntil = scene ? scene.scene_valid_until : null;
  const next = timeline ? nextPlannedSegment(timeline.segments, at) : null;
  // 只数不列：内容由完整安排里那一处承担（见下面的渲染注释）。
  const recentCount = merged ? merged.records.filter((record) => !isLiveNow(record.fact, at)).length : 0;
  const intents = timeline?.intents ?? [];

  return (
    <>
      <section
        className={`life-hero ${headlineKind ? 'life-hero--active' : ''}`}
        data-testid="life-presence"
        data-activity={headlineKind ? 'running' : 'unknown'}
      >
        <div className="life-hero__copy">
          <div className="life-hero__eyebrow"><span className="life-hero__dot" />此刻的她</div>
          {headlineKind ? (
            <>
              <h1 data-testid="life-now-headline">正在{activityName(headlineKind)}</h1>
              <div className="life-hero__meta" data-testid="life-now-basis">
                {headlineStart ? (
                  <span>
                    {formatInZone(headlineStart, timeline?.display_timezone ?? browserTimeZone())} 起
                  </span>
                ) : null}
                {headlineUntil ? (
                  <span>情景有效至 {formatInZone(headlineUntil, timeline!.display_timezone)}</span>
                ) : null}
              </div>
              {/* 活动接口自带的一句近况；时间线那份事实里没有这个字段，所以只在这里出。 */}
              {fallbackScene?.note ? (
                <p className="life-hero__note" data-testid="life-now-note">{fallbackScene.note}</p>
              ) : null}
            </>
          ) : (
            <>
              <h1 data-testid="life-now-headline">暂无当前活动记录</h1>
              <div className="life-hero__meta" data-testid="life-now-basis">
                <span>情景已经过期，或者这一刻还没有提交过记录</span>
              </div>
            </>
          )}
        </div>
        <LifeIllustration active={Boolean(headlineKind)} />
      </section>

      {timeline && next ? (
        <section className="life-next" data-testid="life-next">
          <span className="life-next__label">接下来</span>
          <strong className="life-next__title">{next.title}</strong>
          <time className="life-next__time">
            {formatInZone(next.display_start_at, timeline.display_timezone)} 起
          </time>
        </section>
      ) : null}

      <section className="life-today" aria-labelledby="life-today-title">
        <div className="life-section-heading">
          <h2 id="life-today-title">今天</h2>
          {timeline ? <time>{dayLabel(timeline.display_date)}</time> : null}
        </div>
        {timelineFailed || timeline?.state === 'unavailable' ? (
          <p className="life-day-empty">暂时读不到今天的安排。</p>
        ) : timeline && timeline.segments.length === 0 ? (
          <p className="life-day-empty" data-testid="life-day-empty">
            {timeline.state === 'empty' ? '今天还没有排日程。' : '今天的安排还没有展开。'}
          </p>
        ) : timeline ? (
          /*
           * 「今天」这一块必须真的给出今天的安排：首屏只有"此刻 + 接下来"时，
           * 剩下的一天在页面上是空的——读者会以为没有安排。这里列**叶段**，
           * 父段只作分组（它没有独立的时刻表意义）。
           */
          <ol className="life-day-list" data-testid="life-plan-list">
            {leafSegments(timeline).map((segment) => {
              const phase = dayPhase(segment, at);
              return (
                <li key={segment.segment_id} data-phase={phase} data-testid={`life-plan-${segment.segment_id}`}>
                  <time>{formatInZone(segment.display_start_at, timeline.display_timezone)}</time>
                  <span className="life-day-list__line"><i /></span>
                  <div>
                    <strong>{segment.title}</strong>
                    <span>{formatInZone(segment.display_end_at, timeline.display_timezone)} 前</span>
                  </div>
                </li>
              );
            })}
          </ol>
        ) : null}
        {/*
          这里**不再列一遍活动**。首屏曾经单独渲染「今天留下的片段」，与时间线
          段内那一份、右列那一份各说一次，同一条活动在页面上出现三回。
          现在首屏只给出"今天留下了几条"这个计数，内容留给完整安排里的那唯一一处。
        */}
        {recentCount > 0 && timeline ? (
          <p className="life-happened-count" data-testid="life-happened">
            今天已经留下 {recentCount} 条活动记录，按时间排在下面的完整安排里。
          </p>
        ) : null}
      </section>

      {intents.length ? (
        <section className="life-later" aria-labelledby="life-later-title">
          <h2 id="life-later-title">之后，也许</h2>
          <div className="life-later__items">
            {intents.slice(0, 3).map((intent) => (
              <div key={intent.intention_id}>
                <span>{INTENT_STATE[intent.state] ?? intent.state}</span>
                <strong>{dayLabel(intent.target_start_day)}{intent.target_end_day !== intent.target_start_day ? ` — ${dayLabel(intent.target_end_day)}` : ''}</strong>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </>
  );
}


/** 兼容旧调用点：`activity` 参数不再参与渲染（见文件头注释）。 */
export type { ActivityResponse };

/**
 * 没有时间线时的时间格式来源。
 *
 * 只有"时间轴读不到"这一条路径会走到这里；此时**不猜用户的展示时区**
 * （后端给的 display_timezone 才是权威），退回浏览器时区并把时刻精确到分钟，
 * 这样最坏情况也只是钟点可能与偏好时区不同，而不是显示一个错误的日期。
 */
function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/** 今天的叶段，按开始时间排。父段不单独占行（它没有自己的时刻表意义）。 */
export function leafSegments(timeline: LifeTimelineOut): LifeTimelineSegment[] {
  return timeline.segments
    .filter((segment) => segment.is_leaf && segment.status === 'current')
    .slice()
    .sort((a, b) => a.display_start_at.localeCompare(b.display_start_at));
}

/** 已过去 / 此刻 / 未到，按**展示区间**判，只用于画状态。 */
export function dayPhase(segment: LifeTimelineSegment, now: Date): 'past' | 'now' | 'upcoming' {
  const start = new Date(segment.display_start_at).getTime();
  const end = new Date(segment.display_end_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return 'upcoming';
  if (end <= now.getTime()) return 'past';
  if (start <= now.getTime()) return 'now';
  return 'upcoming';
}
