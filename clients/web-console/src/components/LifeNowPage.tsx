import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { ActivityResponse, GoalsResponse, LifeTimelineOut, ProactiveResponse } from '../types';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import { navigate, useLocationHref } from '../route';
import { lifeTabFromHref, lifeTabHref } from '../tabs';
import { LifePage } from './LifePage';
import { LifeTimelinePanel } from './LifeTimeline';
import { activityKind, LifeDay } from './LifeDay';
import { AlertIcon, ArrowLeftIcon, RefreshIcon, SettingsIcon } from './Icons';
import { ProactiveDraft } from './ProactiveDraft';
import { ProactiveHistory } from './ProactiveHistory';
import '../life-day.css';

const ACTIVITY_STATES: Record<string, string> = {
  proposed: '待开始', running: '在做', paused: '暂停',
  completed: '已结束', failed: '未完成', cancelled: '已取消',
};

function LifeRecords({ activity }: { activity: ActivityResponse | null }) {
  const [records, setRecords] = useState<{ goals: GoalsResponse; proactive: ProactiveResponse } | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [goals, proactive] = await Promise.all([api.goals(), api.proactive()]);
      setRecords({ goals, proactive });
    } catch (cause) {
      setError(explainError(cause));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const proactive = records?.proactive;
  const history = activity?.history ?? [];
  const goals = records?.goals.goals ?? [];
  return (
    <div className="life-full-schedule__body life-records">
      {error ? (
        <div className="banner banner--error" role="alert">
          <span>{error.reason}</span>
          <button type="button" className="button button--small" onClick={() => void load()}>重试</button>
        </div>
      ) : null}
      {!records && !error ? <p className="page__hint">正在读取生活记录…</p> : null}

      <h2 className="life-section">活动记录</h2>
      {history.length > 0 ? (
        <ul className="life-list">
          {history.map((item) => (
            <li key={item.activity_id} className="life-list__row">
              <span className="life-list__main">{activityKind(item.kind)}{item.note ? ` · ${item.note}` : ''}</span>
              <span className="life-list__meta">
                {ACTIVITY_STATES[item.state] ?? item.state}
                {item.origin === 'simulated' ? ' · 模拟' : ''}
                {item.started_at ? ` · ${formatSessionTime(item.started_at)}` : ''}
              </span>
            </li>
          ))}
        </ul>
      ) : <p className="page__hint">{activity ? '还没有活动记录。' : '暂时读不到活动记录。'}</p>}

      {proactive ? (
        <>
          <h2 className="life-section">主动联系</h2>
          {!proactive.gate && proactive.gate_reasons.length > 0 ? (
            <p className="page__hint">{proactive.gate_reasons.join('；')}</p>
          ) : null}
          {proactive.pending.length ? (
            <>
              {proactive.delivery_enabled === false ? (
                <p className="page__hint">{proactive.note || '主动发送关闭，以下内容仅供查看。'}</p>
              ) : null}
              <ul className="life-list">
                {proactive.pending.map((item) => (
                  <li key={item.opportunity_id} className="life-list__row">
                    <span className="life-list__main"><ProactiveDraft item={item} /></span>
                    <span className="life-list__meta">
                      {item.reason}<br />{item.kind} · 到 {formatSessionTime(item.expires_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : <p className="page__hint">没有待发的主动机会。</p>}
          <ProactiveHistory records={proactive.recent ?? []} />

          <h2 className="life-section">目标</h2>
          {goals.length ? (
            <ul className="life-list">
              {goals.map((goal) => (
                <li key={goal.goal_id} className="life-list__row">
                  <span className="life-list__main">{goal.title}</span>
                  <span className="life-list__meta">
                    {goal.horizon} · {goal.state} · {Math.round(goal.progress * 100)}%
                  </span>
                </li>
              ))}
            </ul>
          ) : <p className="page__hint">还没有目标。</p>}
        </>
      ) : null}
    </div>
  );
}

export function LifeNowPage({ onBack, liveActivity }: { onBack: () => void; liveActivity?: string }) {
  const href = useLocationHref();
  const tab = lifeTabFromHref(href);
  const [activity, setActivity] = useState<ActivityResponse | null>(null);
  const [timeline, setTimeline] = useState<LifeTimelineOut | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [timelineFailed, setTimelineFailed] = useState(false);
  const [showRecords, setShowRecords] = useState(false);

  const openTab = (next: 'now' | 'rhythm') => {
    navigate(lifeTabHref(next, href + window.location.hash));
  };

  const load = useCallback(async () => {
    setError(null);
    // 时间线是首屏唯一的数据来源：活动名字来自 facts[].kind，
    // **不**拿 /v1/life/activity 的最近 10 条去补（那份按 updated_at 排序、
    // 与窗口无关，补出来的名字会和时间线的时间对不上）。
    const [activityResult, timelineResult] = await Promise.allSettled([api.activity(), api.lifeTimeline()]);
    if (activityResult.status === 'fulfilled') setActivity(activityResult.value);
    setTimeline(timelineResult.status === 'fulfilled' ? timelineResult.value : null);
    setTimelineFailed(timelineResult.status === 'rejected');
    // 首屏只因为**时间线**失败而报错；活动接口失败只影响「更多生活记录」。
    const cause = timelineResult.status === 'rejected' ? timelineResult.reason : null;
    setError(cause === null ? null : explainError(cause));
  }, []);

  useEffect(() => {
    if (tab === 'now') void load();
  }, [load, tab]);

  if (tab === 'rhythm') {
    return <LifePage onBack={() => openTab('now')} backLabel="返回生活" />;
  }

  return (
    <div className="page life-page">
      <header className="life-page__header">
        <button className="button button--small button--ghost" type="button" onClick={onBack}>
          <ArrowLeftIcon />返回
        </button>
        <h1 className="page__title">生活</h1>
        <button className="life-rhythm-link" type="button" onClick={() => openTab('rhythm')}>
          <SettingsIcon size={15} />生活节奏
        </button>
      </header>

      {error ? (
        <div className="banner banner--error" role="alert">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button className="button button--small" onClick={() => void load()}><RefreshIcon /> 重试</button>
        </div>
      ) : null}

      <LifeDay
        timeline={timeline}
        timelineFailed={timelineFailed}
        fallbackCurrent={activity?.current ?? null}
      />

      {liveActivity ? <button type="button" className="button button--small" data-testid="life-live"
        onClick={() => navigate('/live')}>{liveActivity} <span aria-hidden="true">›</span></button> : null}

      <details className="life-full-schedule" data-testid="life-full-schedule">
        <summary>看看完整安排</summary>
        <div className="life-full-schedule__body">
          {timeline ? (
            <LifeTimelinePanel title="今天的安排" timeline={timeline} onTimelineChange={setTimeline} />
          ) : (
            <p className="page__hint">{timelineFailed ? '暂时读不到时间轴，请重试。' : '正在读取时间轴…'}</p>
          )}
        </div>
      </details>
      <details className="life-full-schedule life-records-disclosure" open={showRecords}
        onToggle={(event) => setShowRecords(event.currentTarget.open)}>
        <summary>更多生活记录</summary>
        {showRecords ? <LifeRecords activity={activity} /> : null}
      </details>
    </div>
  );
}
