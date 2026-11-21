import type { ActivityResponse, LifeTimelineOut, LifeTimelineSegment } from '../types';
import { formatInZone } from './LifeTimeline';

const KIND: Record<string, string> = {
  writing: '写作',
  reading: '读书',
  resting: '休息',
  walking: '散步',
  thinking: '想事情',
  working: '工作',
  eating: '吃饭',
  listening: '听音乐',
};

const INTENT_STATE: Record<string, string> = {
  proposed: '一个念头',
  planned: '想安排在',
  confirmed: '已确认这天',
};

export function activityKind(kind: string): string {
  return KIND[kind] ?? kind.replaceAll('_', ' ');
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

interface LifeDayProps {
  activity: ActivityResponse | null;
  timeline: LifeTimelineOut | null;
}

/** 同一份时间轴的阅读视图；完整层级、事实和诊断仍交给 LifeTimeline。 */
export function LifeDay({ activity, timeline }: LifeDayProps) {
  const current = activity?.current?.state === 'running' ? activity.current : null;
  const future = timeline ? futureSegments(timeline) : [];
  const facts = timeline?.facts.filter((fact) => fact.state === 'completed' && fact.evidence_kind !== 'clock_only') ?? [];
  const factTitles = facts.map((fact) => timeline?.segments.find((segment) => segment.segment_id === fact.activity_id)?.title ?? '一段日常');
  const intents = timeline?.intents ?? [];

  return (
    <>
      <section className={`life-hero ${current ? 'life-hero--active' : ''}`} data-testid="life-presence" data-activity={current ? 'running' : 'unknown'}>
        <div className="life-hero__copy">
          <div className="life-hero__eyebrow"><span className="life-hero__dot" />此刻的她</div>
          <h1>{current ? `正在${activityKind(current.kind)}` : '这一刻，还没有她的消息'}</h1>
          <p>{current ? current.note : '她这会儿在做什么，我还不知道。'}</p>
          <div className="life-hero__meta">
            {current?.started_at && timeline ? <span>从 {formatInZone(current.started_at, timeline.display_timezone)} 开始</span> : null}
            {current?.origin === 'simulated' ? <span>模拟日常</span> : null}
          </div>
        </div>
        <LifeIllustration active={Boolean(current)} />
      </section>

      <section className="life-today" aria-labelledby="life-today-title">
        <div className="life-section-heading">
          <h2 id="life-today-title">今天</h2>
          {timeline ? <time>{dayLabel(timeline.display_date)}</time> : null}
        </div>
        {future.length > 0 ? (
          <ol className="life-day-list" data-testid="life-upcoming">
            {future.slice(0, 4).map((segment) => (
              <li key={segment.segment_id}>
                <time>{formatInZone(segment.display_start_at, timeline!.display_timezone)}</time>
                <span className="life-day-list__line"><i /></span>
                <div><strong>{segment.title}</strong><span>{formatInZone(segment.display_end_at, timeline!.display_timezone)} 前</span></div>
              </li>
            ))}
          </ol>
        ) : timeline ? (
          <p className="life-day-empty" data-testid="life-day-empty">
            {timeline.state === 'empty' ? '今天还没有排日程。' : timeline.segments.length ? '今天的安排里，没有接下来的时段。' : '今天的安排还没有展开。'}
          </p>
        ) : <p className="life-day-empty">今天的安排正在读取。</p>}
        {factTitles.length ? (
          <div className="life-happened" data-testid="life-happened">
            <h3>今天留下的片段</h3>
            <p>{factTitles.slice(0, 3).join(' · ')}</p>
          </div>
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
