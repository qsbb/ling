import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { ActivityResponse, LifeTimelineOut } from '../types';
import { explainError, type ExplainedError } from '../errors';
import { navigate } from '../route';
import { lifeTabFromHref, lifeTabHref } from '../tabs';
import { LifePage } from './LifePage';
import { LifeTimelinePanel } from './LifeTimeline';
import { LifeDay } from './LifeDay';
import { AlertIcon, ArrowLeftIcon, RefreshIcon, SettingsIcon } from './Icons';
import '../life-day.css';

export function LifeNowPage({ onBack }: { onBack: () => void }) {
  const [tab, setTab] = useState(() => lifeTabFromHref(window.location.href));
  const [activity, setActivity] = useState<ActivityResponse | null>(null);
  const [timeline, setTimeline] = useState<LifeTimelineOut | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);

  useEffect(() => {
    const sync = () => setTab(lifeTabFromHref(window.location.href));
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, []);

  const openTab = (next: 'now' | 'rhythm') => {
    navigate(lifeTabHref(next, window.location.href));
    setTab(next);
  };

  const load = useCallback(async () => {
    setError(null);
    const [activityResult, timelineResult] = await Promise.allSettled([api.activity(), api.lifeTimeline()]);
    if (activityResult.status === 'fulfilled') setActivity(activityResult.value);
    if (timelineResult.status === 'fulfilled') setTimeline(timelineResult.value);
    const cause = activityResult.status === 'rejected' ? activityResult.reason
      : timelineResult.status === 'rejected' ? timelineResult.reason : null;
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
        <button className="life-rhythm-link" onClick={() => openTab('rhythm')}>
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

      <LifeDay activity={activity} timeline={timeline} />

      <details className="life-full-schedule" data-testid="life-full-schedule">
        <summary>看看完整安排</summary>
        <div className="life-full-schedule__body">
          <LifeTimelinePanel
            title="今天的安排"
            timeline={timeline}
            onTimelineChange={setTimeline}
          />
        </div>
      </details>
    </div>
  );
}
