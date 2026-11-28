import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type {
  ActivityResponse,
  GoalsResponse,
  LifeTimelineOut,
  ProactiveResponse,
  RelationshipPreferences,
} from '../types';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import { navigate, useLocationHref } from '../route';
import { lifeTabFromHref, lifeTabHref } from '../tabs';
import { LifePage } from './LifePage';
import { LifeTimelinePanel } from './LifeTimeline';
import { activityKind, LifeDay } from './LifeDay';
import { AlertIcon, ArrowLeftIcon, RefreshIcon, SettingsIcon } from './Icons';
import { ProactiveDraft } from './ProactiveDraft';
import {
  classifyProactiveOutcome,
  ProactiveHistory,
  proactiveOutcomeText,
  proactiveSourceLabel,
} from './ProactiveHistory';
import { ContinuityControls } from './ContinuityControls';
import '../life-day.css';

const ACTIVITY_STATES: Record<string, string> = {
  proposed: '待开始', running: '在做', paused: '暂停',
  completed: '已结束', failed: '未完成', cancelled: '已取消',
};

function LifeRecords({
  activity,
  syncVersion,
  onSyncProactive,
}: {
  activity: ActivityResponse | null;
  syncVersion: number;
  onSyncProactive: () => Promise<void>;
}) {
  const [records, setRecords] = useState<{ goals: GoalsResponse; proactive: ProactiveResponse } | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [goals, proactive] = await Promise.all([api.goals(), api.proactive()]);
      setRecords({ goals, proactive });
    } catch (cause) {
      setError(explainError(cause));
    }
  }, []);

  useEffect(() => { void load(); }, [load, syncVersion]);

  const handleChanged = useCallback(async () => {
    await Promise.all([load(), onSyncProactive()]);
  }, [load, onSyncProactive]);

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
          <ContinuityControls onChanged={handleChanged} syncVersion={syncVersion} />
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
                      {proactive.recent?.some((record) => record.opportunity_id === item.opportunity_id && record.activity_id) ? (
                        <button type="button" className="button button--small" disabled={cancelling !== null}
                          onClick={async () => {
                            setCancelling(item.opportunity_id);
                            try {
                              await api.cancelProactiveShare(item.opportunity_id);
                              await handleChanged();
                            } catch (cause) {
                              setError(explainError(cause));
                            } finally {
                              setCancelling(null);
                            }
                          }}>撤销分享</button>
                      ) : null}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : <p className="page__hint">没有待发的主动机会。</p>}
          <ProactiveHistory records={proactive.recent ?? []} defaultOpen />

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
  const [proactiveSummary, setProactiveSummary] = useState<ProactiveResponse | null>(null);
  const [sharePrefs, setSharePrefs] = useState<RelationshipPreferences | null>(null);
  const [pauseBusy, setPauseBusy] = useState(false);
  const [pauseError, setPauseError] = useState<string | null>(null);
  const [syncVersion, setSyncVersion] = useState(0);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [timelineFailed, setTimelineFailed] = useState(false);
  const [showRecords, setShowRecords] = useState(false);

  const openTab = (next: 'now' | 'rhythm') => {
    navigate(lifeTabHref(next, href + window.location.hash));
  };

  const loadProactiveSummary = useCallback(async () => {
    const [proactiveResult, prefsResult] = await Promise.allSettled([
      api.proactive(),
      api.relationshipPreferences(),
    ]);
    setProactiveSummary(proactiveResult.status === 'fulfilled' ? proactiveResult.value : null);
    setSharePrefs(prefsResult.status === 'fulfilled' ? prefsResult.value : null);
  }, []);

  const load = useCallback(async () => {
    setError(null);
    // 时间线是首屏唯一的数据来源：活动名字来自 facts[].kind，
    // **不**拿 /v1/life/activity 的最近 10 条去补（那份按 updated_at 排序、
    // 与窗口无关，补出来的名字会和时间线的时间对不上）。
    const [activityResult, timelineResult] = await Promise.allSettled([
      api.activity(),
      api.lifeTimeline(),
      loadProactiveSummary(),
    ]);
    if (activityResult.status === 'fulfilled') setActivity(activityResult.value);
    setTimeline(timelineResult.status === 'fulfilled' ? timelineResult.value : null);
    setTimelineFailed(timelineResult.status === 'rejected');
    // 首屏只因为**时间线**失败而报错；活动/主动分享接口失败不阻断首屏。
    const cause = timelineResult.status === 'rejected' ? timelineResult.reason : null;
    setError(cause === null ? null : explainError(cause));
  }, [loadProactiveSummary]);

  useEffect(() => {
    if (tab === 'now') void load();
  }, [load, tab]);

  const toggleSharePause = async () => {
    if (!sharePrefs || pauseBusy) return;
    setPauseBusy(true);
    setPauseError(null);
    try {
      await api.setSharePause(!sharePrefs.share_pause.paused);
      await loadProactiveSummary();
      setSyncVersion((v) => v + 1);
    } catch (cause) {
      setPauseError(explainError(cause).reason);
    } finally {
      setPauseBusy(false);
    }
  };

  if (tab === 'rhythm') {
    return <LifePage onBack={() => openTab('now')} backLabel="返回生活" />;
  }

  const recentRecords = proactiveSummary?.recent ?? [];
  const pendingItems = proactiveSummary?.pending ?? [];
  const sharePause = sharePrefs?.share_pause ?? proactiveSummary?.share_pause;
  const showProactiveCard = Boolean(
    sharePause || pendingItems.length > 0 || recentRecords.length > 0,
  );
  const paused = sharePause?.paused;
  const shareTone = paused === undefined ? 'other' : paused ? 'uncertain' : 'delivered';
  const shareStatus = paused === undefined ? '分享状态暂不可用' : paused ? '主动分享已暂停' : '主动分享未暂停';

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

      {showProactiveCard ? (
        <section className="life-proactive-card" aria-label="主动分享状态" data-testid="life-proactive-summary">
          <div className="life-proactive-card__head">
            <div className="life-proactive-card__title-group">
              <h2>主动分享与动态</h2>
              <span
                className={`proactive-badge proactive-badge--${shareTone}`}
                data-testid="life-share-status"
              >
                {shareStatus}
              </span>
            </div>
            {sharePrefs ? (
              <button
                type="button"
                className={`button button--small${paused ? ' button--primary' : ''}`}
                data-testid="life-share-toggle"
                disabled={pauseBusy}
                onClick={() => void toggleSharePause()}
              >
                {pauseBusy ? '更新中…' : paused ? '恢复主动分享' : '暂停主动分享'}
              </button>
            ) : null}
          </div>
          <p className="life-proactive-card__rule">
            {paused && sharePause?.until
              ? `已暂停至 ${formatSessionTime(sharePause.until)}。`
              : ''}
            暂停仅停止主动分享，原有定时提醒仍按各自规则执行。
          </p>
          {pauseError ? <p className="life-controls__error" role="alert">{pauseError}</p> : null}
          {proactiveSummary && !proactiveSummary.gate && proactiveSummary.gate_reasons.length > 0 ? (
            <p className="life-proactive-card__gate">当前门控：{proactiveSummary.gate_reasons.join('；')}</p>
          ) : null}
          {pendingItems.length > 0 ? (
            <div className="life-proactive-card__block">
              <h3>待发送候选（{pendingItems.length}）</h3>
              <ul className="life-proactive-preview">
                {pendingItems.slice(0, 2).map((item) => (
                  <li key={item.opportunity_id} className="life-proactive-preview__item" data-outcome="pending">
                    <div className="life-proactive-preview__top">
                      <span className="proactive-badge proactive-badge--pending">待处理</span>
                      <span className="life-proactive-preview__reason">{item.reason}</span>
                      <time dateTime={item.expires_at}>有效期至 {formatSessionTime(item.expires_at)}</time>
                    </div>
                    <div className="life-proactive-preview__draft">
                      <ProactiveDraft item={item} />
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {recentRecords.length > 0 ? (
            <div className="life-proactive-card__block">
              <h3>最近投递结果</h3>
              <ul className="life-proactive-preview" data-testid="life-delivery-outcomes">
                {recentRecords.slice(0, 5).map((record) => {
                  const classified = classifyProactiveOutcome(record);
                  return (
                    <li
                      key={record.opportunity_id}
                      className={`life-proactive-preview__item life-proactive-preview__item--${classified.tone}`}
                      data-outcome={classified.tone}
                    >
                      <div className="life-proactive-preview__top">
                        <span className={`proactive-badge proactive-badge--${classified.tone}`}>
                          {classified.badge}
                        </span>
                        <strong>{proactiveOutcomeText(record)}</strong>
                        <time dateTime={record.created_at}>{formatSessionTime(record.created_at)}</time>
                      </div>
                      <p className="life-proactive-preview__meta">
                        <span>{proactiveSourceLabel(record)}</span>
                        <span> · {record.reason}</span>
                        {record.terminal_reason ? <span> · {record.terminal_reason}</span> : null}
                      </p>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : null}
          <div className="life-proactive-card__foot">
            <button
              type="button"
              className="ghost-button life-proactive-card__more"
              onClick={() => setShowRecords(true)}
            >
              管理话题边界与查看全部记录
            </button>
          </div>
        </section>
      ) : null}

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
        {showRecords ? (
          <LifeRecords
            activity={activity}
            syncVersion={syncVersion}
            onSyncProactive={loadProactiveSummary}
          />
        ) : null}
      </details>
    </div>
  );
}
