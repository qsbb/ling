/**
 * 总览：登录后第一眼看到的页面。
 *
 * 这一页不是服务器监控大盘，而是「她此刻怎样」——在做什么、情绪如何、
 * 有没有想找我、最近记住了什么、渠道通不通。
 *
 * 三条设计上的硬要求：
 *
 * 1. **不把快照说成实时**。每块数据都带 `as_of`，界面上标"更新于"，
 *    而不是画个绿点写"实时"。
 * 2. **空数据和查询失败必须长得不一样**。前者是事实（她此刻没在做什么），
 *    后者是故障（读不到）。混在一起会让人对系统状态产生错误判断。
 * 3. **候选不等于已发送**。主动联系那块要同时给出"有几条候选""能不能发
 *    "和"为什么不能发"，不能只显示一句"她想找你"。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { DashboardResponse, DashboardSection } from '../types';
import { AlertIcon, RefreshIcon } from './Icons';

interface DashboardPageProps {
  onOpenNow: () => void;
  onOpenChat: () => void;
  onOpenSettings: () => void;
}

const STATE_LABEL: Record<string, string> = {
  proposed: '想去',
  running: '在做',
  paused: '暂停',
  completed: '做完',
  failed: '没做成',
  cancelled: '取消',
};

const AVAILABILITY_LABEL: Record<string, string> = {
  available: '可以聊',
  low: '有点乏',
  resting: '在休息',
};

const PLAN_LEVEL_LABEL: Record<string, string> = {
  tentative: '还没定',
  confirmed: '已约定',
  soft: '随口提的',
};

const ORIGIN_LABEL: Record<string, string> = {
  simulated: '模拟',
  user_statement: '你说的',
  external_observation: '观察到的',
  verified_source: '查到的',
  tool_receipt: '有回执',
  model_inference: '推测',
};

function formatClock(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

interface CardProps {
  title: string;
  section: DashboardSection;
  span?: 'wide' | 'half' | 'third';
  action?: { label: string; onClick: () => void };
  children: (data: Record<string, unknown>) => React.ReactNode;
}

/**
 * 统一的卡片外壳。
 *
 * 三态在这里一次处理完：加载中/空/失败各有各的样子，领域卡片只管
 * "拿到数据之后怎么画"。这样任何一块的失败都不会传染给整页。
 */
function Card({ title, section, span = 'half', action, children }: CardProps) {
  const status = section?.status ?? 'unavailable';
  return (
    <section className={`dash-card dash-card--${span}`} data-testid={`dash-${title}`}>
      <header className="dash-card__head">
        <h2 className="dash-card__title">{title}</h2>
        <div className="dash-card__meta">
          {status === 'ok' ? <span>更新于 {formatClock(section.as_of)}</span> : null}
          {action ? (
            <button type="button" className="dash-card__action" onClick={action.onClick}>
              {action.label}
            </button>
          ) : null}
        </div>
      </header>
      <div className="dash-card__body">
        {status === 'unavailable' ? (
          <p className="dash-card__muted">暂时读不到这块数据。</p>
        ) : status === 'empty' ? (
          <p className="dash-card__muted">{emptyText(title)}</p>
        ) : (
          children((section.data as Record<string, unknown>) ?? {})
        )}
      </div>
    </section>
  );
}

function emptyText(title: string): string {
  switch (title) {
    case '此刻':
      return '当前没有登记中的活动。';
    case '情绪':
      return '暂时没有状态记录。';
    case '主动联系':
      return '当前没有待处理的候选。';
    case '最近记住的事':
      return '还没有可展示的长期记忆。';
    case '关系':
      return '还没有足够的互动记录。';
    default:
      return '暂无内容。';
  }
}

export function DashboardPage({ onOpenNow, onOpenChat, onOpenSettings }: DashboardPageProps) {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await api.dashboard());
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // 只在页面可见时刷新：后台标签页没必要一直打请求
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  if (loading) {
    return (
      <div className="page">
        <div className="page__loading">
          <span className="spinner" aria-hidden="true" /> 正在读取她的状态…
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="page">
        <div className="page__error" role="alert">
          <AlertIcon />
          <div>
            <strong>{error?.reason ?? '读取失败'}</strong>
            {error?.hint ? <p>{error.hint}</p> : null}
          </div>
          <button type="button" className="button" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      </div>
    );
  }

  const s = data.sections;

  return (
    <div className="page dash">
      <header className="dash__head">
        <h1 className="dash__title">总览</h1>
        <div className="dash__meta">
          <span>更新于 {formatClock(data.generated_at)}</span>
          <button type="button" className="dash__refresh" onClick={() => void load()}>
            <RefreshIcon /> 刷新
          </button>
        </div>
      </header>

      <div className="dash__grid">
        <Card
          title="此刻"
          span="wide"
          section={s.activity}
          action={{ label: '详细', onClick: onOpenNow }}
        >
          {(d) => (
            <>
              <p className="dash-now__line">
                <strong>
                  {STATE_LABEL[String(d.state)] ?? String(d.state ?? '')}
                </strong>
                {d.kind ? <span className="dash-now__kind">{String(d.kind)}</span> : null}
              </p>
              {d.place_id ? <p className="dash-card__muted">地点：{String(d.place_id)}</p> : null}
              <p className="dash-now__origin">
                来源：{ORIGIN_LABEL[String(d.origin)] ?? String(d.origin ?? '未知')}
              </p>
            </>
          )}
        </Card>

        <Card title="情绪" span="half" section={s.affect}>
          {(d) => (
            <>
              <p className="dash-now__line">
                <strong>{String(d.emotion ?? '')}</strong>
                <span className="dash-now__kind">
                  强度 {Math.round(Number(d.intensity ?? 0) * 100)}%
                </span>
              </p>
              {d.cause ? <p className="dash-card__muted">因为：{String(d.cause)}</p> : null}
            </>
          )}
        </Card>

        <Card title="可用性" span="half" section={s.presence}>
          {(d) => (
            <>
              <p className="dash-now__line">
                <strong>{AVAILABILITY_LABEL[String(d.availability)] ?? String(d.availability ?? '')}</strong>
                <span className="dash-now__kind">
                  精力 {String(d.energy ?? '?')} · 社交负担 {String(d.social_load ?? '?')}
                </span>
              </p>
              <p className="dash-card__muted">{String(d.describe ?? '')}</p>
            </>
          )}
        </Card>

        <Card title="主动联系" span="half" section={s.proactive}>
          {(d) => (
            <>
              <p className="dash-now__line">
                <strong>{String(d.pending_count ?? 0)}</strong>
                <span className="dash-now__kind">条候选</span>
              </p>
              <p className="dash-card__muted">
                {d.delivery_enabled ? '可以发出' : `不会发出：${String(d.gate_reason ?? '')}`}
              </p>
              {d.top ? (
                <p className="dash-quote">
                  “{String((d.top as Record<string, unknown>).draft_text ?? '（还没写开场白）')}”
                </p>
              ) : null}
            </>
          )}
        </Card>

        <Card title="今天的安排" span="half" section={s.agenda}>
          {(d) => {
            const plans = (d.plans as Array<Record<string, unknown>>) ?? [];
            const commitments = (d.commitments as Array<Record<string, unknown>>) ?? [];
            const goals = (d.goals as Array<Record<string, unknown>>) ?? [];
            return (
              <>
                <p className="dash-card__muted">打算做（可能变）</p>
                {plans.length === 0 ? (
                  <p className="dash-card__muted">今天没有登记的日程。</p>
                ) : (
                  <ul className="dash-list">
                    {plans.map((v) => (
                      <li key={String(v.plan_id)} className="dash-list__row">
                        <span className="dash-list__kind">{formatClock(String(v.start_at ?? ''))}</span>
                        <span className="dash-list__text">{String(v.title ?? '')}</span>
                        <span className="dash-list__meta">{PLAN_LEVEL_LABEL[String(v.commitment_level)] ?? ''}</span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="dash-card__muted">答应对方的事</p>
                {commitments.length === 0 ? (
                  <p className="dash-card__muted">没有待履行的承诺。</p>
                ) : (
                  <ul className="dash-list">
                    {commitments.map((c) => (
                      <li key={String(c.commitment_id)} className="dash-list__row">
                        <span className="dash-list__kind">{String(c.due_at ?? '').slice(5, 16).replace('T', ' ')}</span>
                        <span className="dash-list__text">{String(c.text ?? '')}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {goals.length > 0 ? (
                  <p className="dash-card__muted">
                    进行中的目标：{goals.map((g) => String(g.title ?? '')).join('、')}
                  </p>
                ) : null}
              </>
            );
          }}
        </Card>

        <Card title="最近记住的事" span="wide" section={s.memories}>
          {(d) => {
            const items = (d.items as Array<Record<string, unknown>>) ?? [];
            return (
              <ul className="dash-list">
                {items.map((m) => (
                  <li key={String(m.memory_id)} className="dash-list__row">
                    <span className="dash-list__kind">{String(m.kind ?? '')}</span>
                    <span className="dash-list__text">{String(m.text ?? '')}</span>
                    <span className="dash-list__meta">
                      {ORIGIN_LABEL[String(m.origin)] ?? String(m.origin ?? '')}
                    </span>
                  </li>
                ))}
              </ul>
            );
          }}
        </Card>

        <Card title="近期生活" span="wide" section={s.timeline}>
          {(d) => {
            const items = (d.items as Array<Record<string, unknown>>) ?? [];
            return (
              <ul className="dash-list">
                {items.map((e) => (
                  <li key={String(e.experience_id)} className="dash-list__row">
                    <span className="dash-list__kind">{String(e.occurred_at ?? '').slice(5, 16).replace('T', ' ')}</span>
                    <span className="dash-list__text">{String(e.summary ?? '')}</span>
                    <span className="dash-list__meta">
                      {ORIGIN_LABEL[String(e.origin)] ?? String(e.origin ?? '')}
                    </span>
                  </li>
                ))}
              </ul>
            );
          }}
        </Card>

        <Card
          title="关系"
          span="half"
          section={s.relationship}
          action={{ label: '设置', onClick: onOpenSettings }}
        >
          {(d) => (
            <>
              <p className="dash-now__line">
                <strong>熟悉度 {Number(d.familiarity ?? 0).toFixed(2)}</strong>
                <span className="dash-now__kind">信任 {Number(d.trust ?? 0).toFixed(2)}</span>
              </p>
              <p className="dash-card__muted">
                最近互动：{d.last_interaction_at ? String(d.last_interaction_at).slice(0, 16).replace('T', ' ') : '还没有'}
              </p>
            </>
          )}
        </Card>

        <Card title="渠道与模型" span="half" section={s.connectivity}>
          {(d) => {
            const channels = (d.channels as Array<Record<string, unknown>>) ?? [];
            const bound = (d.bound_model_roles as string[]) ?? [];
            return (
              <>
                <ul className="dash-list">
                  {channels.map((ch) => (
                    <li key={String(ch.channel)} className="dash-list__row">
                      <span className="dash-list__kind">{String(ch.channel)}</span>
                      <span className="dash-list__text">
                        {ch.configured ? '已配置' : `未配置${ch.reason ? `（${String(ch.reason)}）` : ''}`}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="dash-card__muted">
                  {bound.length > 0
                    ? `已绑定模型职责：${bound.join('、')}`
                    : '还没有绑定任何模型职责——她现在说不出话。'}
                </p>
                {bound.length === 0 ? (
                  <button type="button" className="button" onClick={onOpenSettings}>
                    去配置模型
                  </button>
                ) : null}
              </>
            );
          }}
        </Card>
      </div>

      <p className="dash__foot">
        <button type="button" className="dash__refresh" onClick={onOpenChat}>
          直接去聊天
        </button>
      </p>
    </div>
  );
}
