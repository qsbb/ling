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

/**
 * 情绪的中文映射（后端 `EMOTIONS` 是闭集：calm/warm/tired/curious/uneasy/content）。
 *
 * 原来这里直接 `String(d.emotion)`，界面上就成了「calm 强度 0%」——
 * 同一页里状态、可用性、来源都有中文表，只有情绪漏了。
 */
const EMOTION_LABEL: Record<string, string> = {
  calm: '平静',
  warm: '温暖',
  tired: '疲惫',
  curious: '好奇',
  uneasy: '不安',
  content: '满足',
};

/** 认不出来的情绪值原样显示，不吞、不瞎翻译成"未知"。 */
export function emotionLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '未知';
  return EMOTION_LABEL[raw] ?? raw;
}

/** 计划的 state（后端 `PLAN_STATES`：planned/active/done/cancelled/skipped）。 */
const PLAN_STATE_LABEL: Record<string, string> = {
  planned: '计划中',
  active: '进行中',
  done: '已结束',
  cancelled: '已取消',
  skipped: '跳过了',
};

/**
 * 承诺的 state（后端 `COMMITMENT_STATES`：active/delivered/missed/cancelled/undeliverable）。
 *
 * 之前界面对 state 一个字都不渲染，于是 `cancelled`（已取消）和
 * `undeliverable`（送不出去）看起来与待履行的承诺一模一样。
 */
const COMMITMENT_STATE_LABEL: Record<string, string> = {
  active: '待履行',
  delivered: '已送到',
  missed: '没送到',
  cancelled: '已取消',
  undeliverable: '送不出去',
};

/** 承诺里这几种状态已经不是"待办"了：界面上不能让它冒充待履行。 */
const INACTIVE_COMMITMENT_STATES = new Set(['cancelled', 'undeliverable', 'missed', 'delivered']);

export function commitmentStateLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  return COMMITMENT_STATE_LABEL[raw] ?? raw;
}

export function planStateLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  return PLAN_STATE_LABEL[raw] ?? raw;
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatClock(iso: string | null | undefined): string {
  const d = parseDate(iso);
  if (!d) return '';
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

/**
 * 「月-日 时:分」（本地时区）。
 *
 * 原来的「今天的安排」只渲染 `HH:mm`，于是 10-30 的两条日程在 10-31 的
 * 首屏上看上去就是今天的 15:00 / 20:00。日期必须一起显示，标题写着
 * "今天"的卡片才不会被昨天的数据冒充。
 */
export function formatDayTime(iso: string | null | undefined): string {
  const d = parseDate(iso);
  if (!d) return '';
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** 这个时间戳是不是"今天"（按浏览器本地时区判断，和用户看到的一致）。 */
export function isToday(iso: string | null | undefined, now: Date = new Date()): boolean {
  const d = parseDate(iso);
  if (!d) return false;
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

export interface CardProps {
  title: string;
  section: DashboardSection;
  span?: 'wide' | 'half' | 'third';
  action?: { label: string; onClick: () => void };
  /**
   * 空态也渲染内容。
   *
   * 只给"空本身分几个子类"的卡片用——例如今天的安排：日程空、承诺空、
   * 目标空是三件事，用一句"没有数据"盖掉，就看不出是哪种空。
   */
  renderWhenEmpty?: boolean;
  children: (data: Record<string, unknown>) => React.ReactNode;
}

/**
 * 统一的卡片外壳。
 *
 * 三态在这里一次处理完：加载中/空/失败各有各的样子，领域卡片只管
 * "拿到数据之后怎么画"。这样任何一块的失败都不会传染给整页。
 */
export function Card({ title, section, span = 'half', action, renderWhenEmpty, children }: CardProps) {
  const status = section?.status ?? 'unavailable';
  return (
    <section className={`dash-card dash-card--${span}`} data-testid={`dash-${title}`}>
      <header className="dash-card__head">
        <h2 className="dash-card__title">{title}</h2>
        <div className="dash-card__meta">
          {/*
            `empty` 也是「查到了，只是内容为空」，它同样带着后端采集时刻的
            `as_of`；不给时间人就没法判断这句"没有内容"是什么时候的结论。
            只有 `unavailable`（查询失败）不标时间——那本来就没有可信时刻。
          */}
          {status === 'ok' || status === 'empty' ? <span>更新于 {formatClock(section.as_of)}</span> : null}
          {action ? (
            <button type="button" className="dash-card__action" onClick={action.onClick}>
              {action.label}
            </button>
          ) : null}
        </div>
      </header>
      <div className="dash-card__body">
        {status === 'unavailable' ? (
          /*
           * 分段级"读不到"必须和"确实没有"（`empty`）长得不一样：
           * 前者是故障、后者是事实。整页失败用 `.dash-error` 告警面板，
           * 这里用卡内的小号告警条——两者的分量与范围也不同。
           */
          <p className="dash-card__unavailable" role="status">
            <AlertIcon size={13} /> 暂时读不到这块数据。
          </p>
        ) : status === 'empty' && !renderWhenEmpty ? (
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
    case '最近记住的事':
      return '还没有可展示的长期记忆。';
    case '关系':
      return '还没有足够的互动记录。';
    default:
      return '暂无内容。';
  }
}

/**
 * 「能不能发 / 为什么不能发」这一行。
 *
 * 后端在 `empty` 分支里同样会带 `delivery_enabled` 与 `gate_reason`（真实响应
 * 就是这样：0 条候选 + "OneBot 没有声明支持主动推送"）。所以没有候选时也必须
 * 把门控读出来——否则人只看到"没有待处理的候选"，会以为一旦有候选就会自动发出去，
 * 而实际上这个渠道根本不支持主动联系。设计上要求这块同时给出「几条候选 / 能不能发 /
 * 为什么不能发」三件事，缺一件就会误导。
 */
export function proactiveDeliveryLine(data: Record<string, unknown>): string {
  if (typeof data.delivery_enabled !== 'boolean') return '还不能确定能不能发出';
  if (data.delivery_enabled) return '可以发出';
  const reason = String(data.gate_reason ?? '').trim();
  return `不会发出：${reason || '渠道没有给出原因'}`;
}

/**
 * 「今天的安排」正文。
 *
 * 抽成独立组件是为了能直接渲染并断言两个已证实的缺陷：
 * 1. 日程只给 `HH:mm`，而后端 `AgendaService.upcoming()` 并不按"今天"过滤，
 *    于是 10-30 的日程在 10-31 的首屏上冒充今天的 15:00 / 20:00；
 * 2. 承诺完全不渲染 `state`，`cancelled` / `undeliverable` 与待履行长得一模一样。
 * 修法是前端补日期、补状态标记；**后端要不要按天过滤另说**（不在此任务范围）。
 */
export function AgendaBody({ data }: { data: Record<string, unknown> }): React.ReactElement {
  const plans = (data.plans as Array<Record<string, unknown>>) ?? [];
  const commitments = (data.commitments as Array<Record<string, unknown>>) ?? [];
  const goals = (data.goals as Array<Record<string, unknown>>) ?? [];
  return (
    <>
      <p className="dash-card__muted">打算做（可能变）</p>
      {plans.length === 0 ? (
        <p className="dash-card__muted">今天没有登记的日程。</p>
      ) : (
        <ul className="dash-list">
          {plans.map((v) => (
            <li key={String(v.plan_id)} className="dash-list__row">
              {/* 必须带日期：后端不按"今天"过滤，昨天的日程也会出现在这张卡里。 */}
              <span className="dash-list__kind">{formatDayTime(String(v.start_at ?? ''))}</span>
              <span className="dash-list__text">{String(v.title ?? '')}</span>
              <span className="dash-list__meta">
                {isToday(String(v.start_at ?? '')) ? null : (
                  <span className="dash-list__flag">不是今天</span>
                )}
                {String(v.state ?? '') === 'planned' ? null : (
                  <span className="dash-list__state">{planStateLabel(v.state)}</span>
                )}
                {PLAN_LEVEL_LABEL[String(v.commitment_level)] ? (
                  <span className="dash-list__state">
                    {PLAN_LEVEL_LABEL[String(v.commitment_level)]}
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="dash-card__muted">答应对方的事</p>
      {commitments.length === 0 ? (
        <p className="dash-card__muted">没有待履行的承诺。</p>
      ) : (
        <ul className="dash-list">
          {commitments.map((c) => {
            const state = String(c.state ?? '');
            const inactive = INACTIVE_COMMITMENT_STATES.has(state);
            return (
              <li key={String(c.commitment_id)} className="dash-list__row">
                <span className="dash-list__kind">{formatDayTime(String(c.due_at ?? ''))}</span>
                <span className="dash-list__text">{String(c.text ?? '')}</span>
                {/* 已取消 / 送不出去不能看起来和待履行一样。 */}
                <span
                  className={`dash-list__meta${inactive ? ' dash-list__meta--inactive' : ''}`}
                >
                  <span className="dash-list__state">{commitmentStateLabel(c.state)}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {goals.length > 0 ? (
        <p className="dash-card__muted">
          进行中的目标：{goals.map((g) => String(g.title ?? '')).join('、')}
        </p>
      ) : null}
    </>
  );
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
        <div className="dash-error" role="alert">
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
                <strong>{emotionLabel(d.emotion)}</strong>
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

        {/*
          没有候选时也照常渲染：`empty` 分支的载荷里带着门控（能不能发、为什么
          不能发），用一句"没有待处理的候选"盖掉它，就等于把「渠道不支持主动
          联系」这件事藏起来了。
        */}
        <Card title="主动联系" span="half" section={s.proactive} renderWhenEmpty>
          {(d) => {
            const pending = Number(d.pending_count ?? 0);
            return (
              <>
                <p className="dash-now__line">
                  <strong>{pending}</strong>
                  <span className="dash-now__kind">条候选</span>
                </p>
                <p className="dash-card__muted">{proactiveDeliveryLine(d)}</p>
                {pending === 0 ? (
                  <p className="dash-card__muted">当前没有待处理的候选。</p>
                ) : null}
                {d.top ? (
                  <p className="dash-quote">
                    “{String((d.top as Record<string, unknown>).draft_text ?? '（还没写开场白）')}”
                  </p>
                ) : null}
              </>
            );
          }}
        </Card>

        <Card title="今天的安排" span="half" section={s.agenda} renderWhenEmpty>
          {(d) => <AgendaBody data={d} />}
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
