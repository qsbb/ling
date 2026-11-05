/**
 * 生活页（`/life`）：两个 tab。
 *
 * - **它现在在干嘛**：只读状态（presence / agenda / activity / goals / proactive）。
 *   这是本页原本的内容，只搬进了 tab 里。
 * - **生活节奏**（⑧）：原本在 `/settings/life`。Step 4 按用户拍板
 *   （设置只放程序与运维）迁回了这里——场景有效期、精力消耗、主动频率
 *   改的是「它怎么过日子」，不是运维。表单本体仍是 `LifePage`（PolicyForm）。
 *
 * 为什么要 tab 而不是堆在一屏：这一页原来就在最下面写着「想改它的生活节奏去
 * 设置」——那是把同一件事拆到两个页面的典型症状。tab 把「看」和「调」
 * 放在同一个地址下，两者都不必跳走。
 *
 * 全部只读，不改任何状态。tab 是 URL（`?tab=rhythm`），不是组件状态。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import { navigate, useLocationHref } from '../route';
import { lifeTabFromHref, lifeTabHref, type LifeTab } from '../tabs';
import type {
  ActivityResponse,
  GoalsResponse,
  ProactiveResponse,
} from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';
import { LifePage } from './LifePage';
import { PageTabPanel, PageTabs, type PageTabSpec } from './PageTabs';

interface LifeNowPageProps {
  /** 离开本页（回对话）。 */
  onBack: () => void;
}

interface Snapshot {
  activity: ActivityResponse;
  goals: GoalsResponse;
  proactive: ProactiveResponse;
}

const STATE_LABEL: Record<string, string> = {
  proposed: '想去',
  running: '在做',
  paused: '暂停',
  completed: '做完',
  failed: '没做成',
  cancelled: '取消',
};

const ORIGIN_LABEL: Record<string, string> = {
  simulated: '模拟',
  user_statement: '你说的',
  external_observation: '观察到的',
  verified_source: '查到的',
  tool_receipt: '有回执',
  model_inference: '它猜的',
};

export const LIFE_TABS: readonly PageTabSpec<LifeTab>[] = [
  { key: 'now', label: '它现在在干嘛', testid: 'life-tab-now' },
  { key: 'rhythm', label: '生活节奏', testid: 'life-tab-rhythm' },
];

export function LifeNowPage({ onBack }: LifeNowPageProps) {
  const href = useLocationHref();
  const tab = lifeTabFromHref(href);
  const openTab = (next: LifeTab) => navigate(lifeTabHref(next));

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回聊天
      </button>

      <h1 className="page__title">生活</h1>
      <p className="page__hint">
        这里是它自己的生活状态。模拟出来的活动会标「模拟」——不是现实里真发生的事。
      </p>

      <PageTabs label="生活页分区" tabs={LIFE_TABS} current={tab} onSelect={openTab} />

      <PageTabPanel tab="now" current={tab}>
        <LifeNowPanel onOpenRhythm={() => openTab('rhythm')} />
      </PageTabPanel>
      <PageTabPanel tab="rhythm" current={tab}>
        <LifePage onBack={() => openTab('now')} backLabel="返回生活状态" />
      </PageTabPanel>
    </section>
  );
}

/**
 * 「它现在在干嘛」的本体。原来是整个 `LifeNowPage`，现在只负责自己那一块：
 * 外壳（返回 / 标题 / tab 条）由上面的 `LifeNowPage` 提供，这里不再自带
 * `.page`，否则 tab 面板里会再缩进一次并多出一层内层滚动条。
 */
function LifeNowPanel({ onOpenRhythm }: { onOpenRhythm: () => void }) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [activity, goals, proactive] = await Promise.all([
        api.activity(),
        api.goals(),
        api.proactive(),
      ]);
      setData({ activity, goals, proactive });
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const current = data?.activity.current ?? null;
  const history = data?.activity.history ?? [];
  const goals = data?.goals.goals ?? [];
  const pending = data?.proactive.pending ?? [];

  return (
    <div className="domain-body">
      {loading ? <p className="page__hint">加载中…</p> : null}

      {error ? (
        <div className="banner banner--error">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      {data ? (
        <>
          <div className="life-now">
            {current ? (
              <>
                <div className="life-now__title">
                  {STATE_LABEL[current.state] ?? current.state}·{current.kind}
                  {/* 字段缺失时宁可不显示，也不要渲染一个没有文字的空胶囊。 */}
                  {current.origin ? (
                    <span className="badge">{ORIGIN_LABEL[current.origin] ?? current.origin}</span>
                  ) : null}
                </div>
                <div className="life-now__meta">
                  {current.started_at ? `自 ${formatSessionTime(current.started_at)}` : '还没开始'}
                </div>
              </>
            ) : (
              <div className="life-now__title">此刻空闲</div>
            )}
          </div>

          <h2 className="life-section">主动联系</h2>
          <p className="page__hint">
            {data.proactive.gate
              ? '当前允许主动找你。'
              : '当前不会主动找你，原因：'}
            {data.proactive.gate ? null : (
              <span className="life-gate">{data.proactive.gate_reasons.join('；')}</span>
            )}
          </p>
          {pending.length > 0 ? (
            <p className="page__hint">
              {/* gate 放行**不等于**会真的发出：主动发送目前没有实现。
                  原来这里写"条件满足就会发"，那是一句假话。 */}
              {/* 三种状态要分开说，不能合成一句：
                  ① 发送功能没开（默认）——只会生成，不会发
                  ② 开了但门控没过——排队中
                  ③ 开了且门控过了——真的会发 */}
              {data.proactive.delivery_enabled === false
                ? data.proactive.note ||
                  '主动发送默认关闭：下面能看到她会说什么，但不会真的发出去。'
                : data.proactive.gate
                  ? '下面这些机会正在排队，条件满足就会发。'
                  : '下面这些机会已经产生，但按上面的原因还没发出去——不是已经联系过你。'}
            </p>
          ) : null}
          {pending.length > 0 ? (
            <ul className="life-list">
              {pending.map((item) => (
                <li key={item.opportunity_id} className="life-list__row">
                  {/* 她会说什么 —— 这是生成好的开场白，不是内部推理 */}
                  <span className="life-list__main">
                    {item.draft_text ? (
                      <span data-testid="proactive-draft">「{item.draft_text}」</span>
                    ) : (
                      <span className="life-list__note">（开场白还没生成）</span>
                    )}
                  </span>
                  <span className="life-list__meta">
                    {/* reason 是内部推理，标出来避免被当成台词 */}
                    因为她想：{item.reason}
                    <br />
                    {item.kind} · 到 {formatSessionTime(item.expires_at)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="page__hint">没有待发的主动机会。</p>
          )}

          <h2 className="life-section">最近做过的事</h2>
          {history.length > 0 ? (
            <ul className="life-list">
              {history.map((item) => (
                <li key={item.activity_id} className="life-list__row">
                  <span className="life-list__main">
                    {item.kind}
                    {item.note ? <span className="life-list__note"> · {item.note}</span> : null}
                  </span>
                  <span className="life-list__meta">
                    {STATE_LABEL[item.state] ?? item.state}
                    {item.completion_evidence ? ' · 有证据' : ''}
                    {item.started_at ? ` · ${formatSessionTime(item.started_at)}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="page__hint">还没有记录。</p>
          )}

          <h2 className="life-section">目标</h2>
          {goals.length > 0 ? (
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
          ) : (
            <p className="page__hint">还没有目标。</p>
          )}

          <p className="page__hint life-foot">
            想改它的生活节奏（场景有效期、精力消耗、主动频率）去
            <button type="button" className="link-button" onClick={onOpenRhythm}>
              生活节奏
            </button>
            。
          </p>
        </>
      ) : null}
    </div>
  );
}
