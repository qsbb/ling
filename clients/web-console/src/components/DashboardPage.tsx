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
import type {
  DashboardResponse,
  DashboardSection,
  FallbackNotice,
  IntentTeaserOut,
  OutlineRelation,
  TimelineState,
  TodayOutlineOut,
} from '../types';
import { AlertIcon, RefreshIcon } from './Icons';
import '../life-overview.css';

interface DashboardPageProps {
  /** 「详细」入口：生活页（`/life`）。 */
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

/**
 * 卡片在 12 列网格里占几列。
 *
 * 3 列只给「一个短值 + 一句解释」的小卡（可用性/情绪）：再放列表就会每行
 * 只剩两三个字。长列表一律 6 列起。窄屏怎么塌陷由 `dashboard.css` 统一处理，
 * 组件不按视口做判断。
 */
export type CardSpan = 3 | 6 | 12;

/** 卡片里最多先给几行，剩下的交给原页面的详情入口（§5：不超过 3 行）。 */
export const CARD_LIST_PREVIEW = 3;

export interface CardProps {
  title: string;
  section: DashboardSection;
  span?: CardSpan;
  /**
   * 锚点目标。
   *
   * 只给「渠道与模型」用：它在页尾，读不到时页顶那条告警要能跳过来，
   * 否则人得自己判断"她说不出话"是哪里出的问题。
   */
  id?: string;
  /**
   * 卡片右上角的语义标记（如「未安排」）。
   *
   * 只写**结论**：状态码、内部枚举不进这里，人话标签见各自的 `*Label` 映射。
   */
  badge?: string;
  action?: { label: string; onClick: () => void };
  /**
   * 这块读不到时的重试。
   *
   * 服务端只有一个聚合 `/v1/dashboard`，所以"重试这一块"实际是重新取整页；
   * 已经拿到的卡片会原样留着，不会退回加载态，只有仍是 unavailable 的那块
   * 继续显示告警——这样一次局部失败不会被放大成整页失败。
   */
  onRetry?: () => void;
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
export function Card({
  title,
  section,
  span = 6,
  id,
  badge,
  action,
  onRetry,
  renderWhenEmpty,
  children,
}: CardProps) {
  const status = section?.status ?? 'unavailable';
  return (
    <section
      className={`dash-card dash-card--span${span}`}
      data-testid={`dash-${title}`}
      {...(id ? { id } : {})}
    >
      <header className="dash-card__head">
        <h3 className="dash-card__title">{title}</h3>
        <div className="dash-card__meta">
          {badge ? <span className="dash-card__badge">{badge}</span> : null}
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
          <div className="dash-card__unavailable" role="status">
            <AlertIcon size={13} />
            <span className="dash-card__unavailable-text">暂时读不到这块数据。</span>
            {onRetry ? (
              <button type="button" className="dash-card__retry" onClick={onRetry}>
                重试
              </button>
            ) : null}
          </div>
        ) : status === 'empty' && !renderWhenEmpty ? (
          <p className="dash-card__muted">{emptyText(title)}</p>
        ) : (
          children((section.data as Record<string, unknown>) ?? {})
        )}
      </div>
    </section>
  );
}

/**
 * 区块标题（「现在 / 接下来 / 已留下的记录 / 连接状态」）。
 *
 * 12 列网格里的整行元素（`grid-column: 1 / -1`）。加它是因为原来 12 张卡
 * 平铺在同一个网格里，人只能逐张读卡片标题才知道哪张是"此刻"、哪张是
 * "未来"；区块标题把顺序写死在版面上，而不是留给读者自己归纳。
 */
export function DashboardZone({ id, title, hint }: { id: string; title: string; hint: string }) {
  return (
    <header className="dash-zone" data-zone={id}>
      <h2 className="dash-zone__title">{title}</h2>
      <p className="dash-zone__hint">{hint}</p>
    </header>
  );
}

/**
 * 上游是谁 → 界面上怎么称呼它。
 *
 * 后端 `domains/fallback.py::ResolvedInput.upstream` 给的是机器名
 * （`schedule` / `scene`），直接印出来就是"schedule 不可用"这种黑话。
 * **认不出来的一律不猜**：退回原样显示，而不是硬说成"日程"。
 */
const UPSTREAM_LABEL: Record<string, string> = {
  schedule: '日程',
  scene: '她此刻的场景',
};

/**
 * 「这个值是兜底值」的提示条。
 *
 * 为什么必须有它：同一句话（「下次大约 14:20」）在两种情况下长得一模一样——
 *
 * 1. 日程按她的时间轴真的算出来的；
 * 2. 日程读不到 / 还没生成，后端回退到本域默认参数算出来的。
 *
 * 不标来源，等于**把兜底值伪装成她的真实状态**（「不编造」是本项目的底线）。
 * 所以这里只回答两件事：这次的值从哪来、为什么，以及**它不是她的状态**。
 *
 * 三条克制：
 * - 正常（`used=false`）时**什么都不渲染**——不是每块都挂一个"正常"标签；
 * - 不摆错误码：人话优先用后端给的 `reason`（`phone`/`presence` 那边本来就是
 *   中文人话），拿不到才按上游名说一句；
 * - 语气是"说明"不是"告警"：中性底 + 一条虚线左边框，不用警示色——上游为空
 *   （今天没排日程）是正常状态，不是故障。
 */
export function FallbackHint({
  notice,
  scope,
}: {
  /** `sections.*.data.fallback`，即后端 `FallbackNotice`。 */
  notice: FallbackNotice | null | undefined;
  /** 回退影响的是**哪件事**，如「看手机的节奏」；只在没有 reason 时用得上。 */
  scope: string;
}) {
  if (!notice?.used) return null;
  const upstream = UPSTREAM_LABEL[String(notice.upstream)] ?? String(notice.upstream ?? '');
  const reason = typeof notice.reason === 'string' ? notice.reason.trim() : '';
  return (
    <div className="dash-fallback" data-testid="dash-fallback" data-upstream={String(notice.upstream)}>
      <p className="dash-fallback__line">
        <span className="dash-fallback__tag">默认值</span>
        {reason || `${upstream || '上游'}暂时用不上，${scope}先按默认参数显示`}
      </p>
      {/*
        这句是这条提示存在的全部理由：让"她真的这么想"和"我们兜底算的"
        在界面上分开。没有它，上面那句原因只是技术说明，读者仍会把数字当成
        她的状态。
      */}
      <p className="dash-fallback__note">这是按默认参数算的，不是她此刻的真实状态。</p>
    </div>
  );
}

/**
 * 从 `sections.*.data` 里取出回退标记。
 *
 * 形状不认识（不是对象 / 没有布尔 `used`）时返回 `null`——那种情况下
 * **不能**当成"正常"去渲染，也编不出一句说明，所以由调用方决定怎么交代。
 */
export function fallbackNoticeOf(data: unknown): FallbackNotice | null {
  if (!data || typeof data !== 'object') return null;
  const raw = (data as Record<string, unknown>).fallback;
  if (!raw || typeof raw !== 'object') return null;
  const notice = raw as Record<string, unknown>;
  if (typeof notice.used !== 'boolean') return null;
  return {
    upstream: String(notice.upstream ?? ''),
    used: notice.used,
    state: (notice.state ?? 'unknown') as FallbackNotice['state'],
    source: (notice.source ?? 'upstream') as FallbackNotice['source'],
    reason: typeof notice.reason === 'string' ? notice.reason : null,
  };
}

/**
 * 「手机」卡片的正文。
 *
 * 抽成独立组件只为一个原因：它有三种可区分的样子（正在看 / 放下但有下次 /
 * 还没安排），而这三种的区别正是用户要看的东西——不抽出来就没法在单测里
 * 逐种钉住，只能靠整页渲染碰运气。
 *
 * 「下次什么时候看」是**参数**不是事实：它由上游日程决定，日程不可用时后端
 * 回退到本域默认节奏。这条来源不标出来，用户会把兜底值读成她的真实状态。
 * 从没看过手机（`empty`）的载荷里同样带着这个标记，所以这里不能只在
 * 「看得懂的那三种状态」里渲染它。
 */
/** 媒体活动的 kind → 人话。没列到的 kind 就原样显示，不猜。 */
const MEDIA_KIND_LABELS: Record<string, string> = {
  video_watch: '看视频',
  bilibili_feed: '刷视频',
  livestream: '看直播',
};

/**
 * 「正在忙什么」：她此刻是不是在看/刷媒体。
 *
 * ## 为什么不并进「手机」那张卡
 *
 * 用户原话：「他在忙，可以给他变成他的真实活动状态，放到"正在看手机"那里，
 * 显示当前的状态」。
 *
 * 「正在看手机」讲的是**手机**（解锁、刷 app、放下）；「正在看视频」讲的是
 * **媒体注意力**。两者可以同时成立，也常常不同时成立——她可能在看 B 站而
 * 根本没在「看手机」。并成一句会把具体事实换成更含糊的说法。
 *
 * ## 别人的会话只能知道"忙"
 *
 * 后端按受众过滤过：`in_my_session === false` 时只说明"她正忙着媒体动作"，
 * 说不出她在看什么、给谁看。这里**如实照做**，不假装知道更多。
 */
export function MediaBody({ data }: { data: Record<string, unknown> }) {
  const kind = String(data.kind ?? '');
  const label = MEDIA_KIND_LABELS[kind] ?? kind;
  const mine = data.in_my_session === true;
  const since = data.started_at ? formatClock(String(data.started_at)) : '';
  return (
    <>
      <p className="dash-now__line">
        <strong>{mine ? `正在${label || '忙媒体'}` : '正忙着看东西'}</strong>
        {mine && kind ? <span className="dash-now__kind">{label}</span> : null}
      </p>
      {since ? <p className="dash-card__muted">从 {since} 起</p> : null}
      {mine ? null : (
        <p className="dash-card__muted">
          发生在别的会话里，这里只知道她正忙着——细节不在这一侧。
        </p>
      )}
    </>
  );
}

export function PhoneBody({ data }: { data: Record<string, unknown> }) {
  const watching = String(data.state ?? '') === 'watching';
  const app = data.watching_app ? String(data.watching_app) : '';
  const fallback = fallbackNoticeOf(data);
  /*
   * `empty` = 从没看过手机。载荷里没有事实字段（后端刻意不给 state /
   * last_looked_at，那是编事实），**只有**「下次该隔多久」这个参数和它的来源；
   * 所以这里不能写"手机放下了"——她没拿起过，谈不上放下。
   */
  const neverLooked = !('state' in data) && !watching;
  const intervalMinutes = Number(data.next_look_interval_minutes ?? NaN);
  return (
    <>
      {neverLooked ? (
        <p className="dash-card__muted">她还没拿起过手机。</p>
      ) : (
        <p className="dash-now__line">
          <strong>{watching ? '正在看手机' : '手机放下了'}</strong>
          {watching && app ? <span className="dash-now__kind">{app}</span> : null}
        </p>
      )}
      {watching && data.watching_since ? (
        <p className="dash-card__muted">从 {formatClock(String(data.watching_since))} 起</p>
      ) : null}
      {!watching && data.last_looked_at ? (
        <p className="dash-card__muted">上次看是 {formatClock(String(data.last_looked_at))}</p>
      ) : null}
      {neverLooked ? (
        Number.isFinite(intervalMinutes) ? (
          <p className="dash-card__muted">按现在的节奏，大约每 {intervalMinutes} 分钟看一次</p>
        ) : null
      ) : (
        /*
          正在看的时候后端不排下一次（状态机规则 2），所以这里写「等她收起手机」，
          而不是显示一个会跳的数字——那个数字在"正在看"期间根本不存在。
        */
        <p className="dash-card__muted">
          {watching
            ? '等她收起手机'
            : data.next_look_at
              ? `下次大约 ${formatClock(String(data.next_look_at))}`
              : '还没安排下一次'}
        </p>
      )}
      {/*
        **正在看时不显示**：这段时间卡上唯一的"下次"是「等她收起手机」，
        它是状态机规则、不是回退参数。此刻挂一句「不是她此刻的真实状态」，
        读者会以为我们在说"她正在看手机"这件事也是猜的——那是新的误导。
        等她放下手机、真的排了 `next_look_at`，提示才跟着那个值一起出现。
      */}
      <FallbackHint notice={watching ? null : fallback} scope="看手机的节奏" />
    </>
  );
}

function emptyText(title: string): string {
  switch (title) {
    case '此刻':
      return '当前没有登记中的活动。';
    case '可用性':
      return '暂时没有可用性记录。';
    case '情绪':
      return '暂时没有状态记录。';
    case '手机':
      return '她还没拿起过手机。';
    case '近期生活':
      return '最近三天还没有记录下什么。';
    case '最近记住的事':
      return '还没有可展示的长期记忆。';
    case '关系':
      return '还没有足够的互动记录。';
    case '渠道与模型':
      return '还没有配置任何渠道，也还没绑定模型职责。';
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
  /*
   * 总览卡片只给前几条：这一块是"扫一眼知道今天有什么"，不是日程表本身。
   * 剩下的条数必须明说，否则人会以为日程只有前面这几条（尤其在 375px 下，
   * 看不到的条目没有任何暗示）。完整列表在「生活」页。
   */
  const plansShown = plans.slice(0, CARD_LIST_PREVIEW);
  const commitmentsShown = commitments.slice(0, CARD_LIST_PREVIEW);
  return (
    <>
      <p className="dash-card__muted">打算做（可能变）</p>
      {plansShown.length === 0 ? (
        <p className="dash-card__muted">今天没有登记的日程。</p>
      ) : (
        <ul className="dash-list">
          {plansShown.map((v) => (
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
      {plans.length > plansShown.length ? (
        <p className="dash-card__muted">还有 {plans.length - plansShown.length} 条日程，去生活页看全部。</p>
      ) : null}
      <p className="dash-card__muted">答应对方的事</p>
      {commitmentsShown.length === 0 ? (
        <p className="dash-card__muted">没有待履行的承诺。</p>
      ) : (
        <ul className="dash-list">
          {commitmentsShown.map((c) => {
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
      {commitments.length > commitmentsShown.length ? (
        <p className="dash-card__muted">
          还有 {commitments.length - commitmentsShown.length} 条承诺，去生活页看全部。
        </p>
      ) : null}
      {goals.length > 0 ? (
        <p className="dash-card__muted">
          进行中的目标：{goals.map((g) => String(g.title ?? '')).join('、')}
        </p>
      ) : null}
    </>
  );
}

/**
 * `today_outline.items[].relation` 的中文（契约 §11.9 / 后端 `OutlineRelation`）。
 *
 * 三个值**不能合并**：`now` 是"此刻可能在做"，`next` 是"接下来（可能）"，
 * `anchor` 才是"已约定"。把 next 写成"在做"、把 anchor 写成"打算"，
 * 都会让人把计划当承诺 / 把承诺当计划。
 */
export const RELATION_LABEL: Record<OutlineRelation, string> = {
  now: '此刻可能在做',
  next: '接下来',
  anchor: '已约定',
};

export function relationLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  return RELATION_LABEL[raw as OutlineRelation] ?? raw;
}

/**
 * `state` 的短文案（总览用；完整解释在生活页时间轴顶部）。
 *
 * 六种都要有：把 `empty` 和 `unavailable` 说成同一句，就是"查不到"与
 * "确实没有"最典型的混淆。
 */
export const OUTLINE_STATE_LABEL: Record<TimelineState, string> = {
  ready: '已生成',
  degraded: '规则兜底',
  failed: '生成失败',
  pending: '还没生成',
  empty: '这一天还没有安排',
  unavailable: '读不到',
};

export function outlineStateLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  return OUTLINE_STATE_LABEL[raw as TimelineState] ?? raw;
}

/** 契约 §11.9 的 `intent_teaser.items[].state`（与时间轴的 `intents[]` 同一套）。 */
export function intentTeaserStateLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  switch (raw) {
    case 'proposed':
      return '候选';
    case 'planned':
      return '想/考虑';
    case 'confirmed':
      // 确认的是目标/日期，不是某个时段已经约好。
      return '已确认想在这个日期段做（时段仍待定）';
    default:
      return raw;
  }
}

const INTENT_SOURCE_LABEL: Record<string, string> = {
  model: '模型提的',
  user: '你提的',
  actor: '她提的',
  recurrence: '按周期',
};

export function intentTeaserSourceLabel(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  return INTENT_SOURCE_LABEL[raw] ?? raw;
}

/** 卡片拿不到区块时按"读不到"处理，不按"没有"处理。 */
const MISSING_SECTION: DashboardSection = { status: 'unavailable', as_of: '' };

/**
 * 「今天她可能做什么」的正文。
 *
 * 与旧 `agenda` 卡片并列、互不改义：这块只画时间轴上的 2–3 条摘要。
 * 条数是后端选的，**没有就不补**——"下午还没定"不该显示成"安排满了"。
 * 时间用区块自带的 `display_timezone` 格式化（不读浏览器时区）。
 */
export function TodayOutlineBody({ data }: { data: Record<string, unknown> }): React.ReactElement {
  const outline = data as unknown as Partial<TodayOutlineOut>;
  const state = String(outline.state ?? '');
  const items = outline.items ?? [];
  const timeZone = String(outline.display_timezone ?? 'UTC');
  const displayDate = String(outline.display_date ?? '');
  const planDay = String(outline.plan_day ?? '');
  // 两个日期本来就是背靠背的：不抹成同一个"今天"（契约 §11.2）。
  const zonesDiffer = Boolean(displayDate && planDay && displayDate !== planDay);
  const fmt = (iso: string | null | undefined) => {
    if (!iso) return '';
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return '';
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        timeZone,
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      }).format(at);
    } catch {
      return String(iso).slice(11, 16);
    }
  };
  return (
    <>
      {/* 「今天是哪天」压在页头（`dash__subtitle`）里，卡片里不再重复一遍。 */}
      {zonesDiffer ? (
        <p className="dash-card__muted" data-testid="dash-outline-tz-note">
          两边日期不同：你的今天 {displayDate}（{outline.display_timezone ?? '未知'}）与她的计划日{' '}
          {planDay}（{outline.actor_timezone ?? '未知'}），时间轴按各自的时区裁剪。
        </p>
      ) : null}
      {items.length === 0 ? (
        <p className="dash-card__muted" data-testid="dash-outline-empty">
          {/* 空态也分状态：没安排 / 还没生成 / 读不到，三件事。 */}
          {outlineStateLabel(state) || '今天暂时没有可展示的摘要。'}
        </p>
      ) : (
        <ul className="dash-list" data-testid="dash-outline-list">
          {items.map((item, index) => (
            <li
              key={`${item.segment_id ?? item.title}-${index}`}
              className="dash-list__row dash-list__row--life"
              data-relation={item.relation}
            >
              <span className={`dash-list__kind dash-relation dash-relation--${item.relation}`}>
                {relationLabel(item.relation)}
              </span>
              <span className="dash-list__text">{item.title}</span>
              <span className="dash-list__meta">
                {fmt(item.start_at)}–{fmt(item.end_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {/*
        状态一行只在**需要提醒**时才出现：已生成是常态，占一行只会把有用的
        摘要挤下去；规则兜底 / 生成失败 才是人需要知道的。
      */}
      {state === 'ready' ? null : (
        <p className="dash-card__muted" data-testid="dash-outline-state">
          状态：{outlineStateLabel(state) || '未知'}
          {state === 'degraded' ? '（内容是规则兜底，不是模型排的）' : ''}
          {state === 'failed' ? '（最近一次生成失败，详情在生活页时间轴上）' : ''}
        </p>
      )}
      {outline.note ? <p className="dash-card__muted">{outline.note}</p> : null}
    </>
  );
}

/**
 * 「未来意向」的正文。
 *
 * `horizon_unplanned=true` 是**契约的一部分**：未来日没有生成大纲，这些条目
 * 只是意向。所以这句话必须和列表一起出现——单独一张"未来 7 天"列表，
 * 会被读成日程表。`proposed` 候选不会因为上了首屏就升级。
 */
export function IntentTeaserBody({ data }: { data: Record<string, unknown> }): React.ReactElement {
  const teaser = data as unknown as Partial<IntentTeaserOut>;
  const items = teaser.items ?? [];
  const horizon =
    typeof teaser.horizon_unplanned === 'boolean' ? teaser.horizon_unplanned : true;
  return (
    <>
      {horizon ? (
        <p className="dash-card__muted" data-testid="dash-intent-horizon">
          未来日还未生成大纲——下面这些只是意向，不是那一天的安排。
        </p>
      ) : null}
      {items.length === 0 ? (
        <p className="dash-card__muted" data-testid="dash-intent-empty">
          未来 7 天还没有有来源的意向。
        </p>
      ) : (
        <ul className="dash-list" data-testid="dash-intent-list">
          {items.map((item) => (
            <li key={item.intention_id} className="dash-list__row dash-list__row--life">
              <span className="dash-list__kind">
                {item.target_start_day === item.target_end_day
                  ? item.target_start_day
                  : `${item.target_start_day} → ${item.target_end_day}`}
              </span>
              <span className="dash-list__text">{item.title}</span>
              <span className="dash-list__meta">
                <span className="dash-list__state">{intentTeaserStateLabel(item.state)}</span>
                {intentTeaserSourceLabel(item.source_kind) ? (
                  <span className="dash-list__kind">
                    {intentTeaserSourceLabel(item.source_kind)}
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      )}
      {teaser.note ? <p className="dash-card__muted">{teaser.note}</p> : null}
    </>
  );
}

/**
 * 12 列网格的正文（4 个区块 / 12 张卡）。
 *
 * 从 `DashboardPage` 里拆出来是为了能在 SSR 下断言区块顺序、每块独立降级和
 * 首屏密度——`DashboardPage` 自己第一次渲染永远停在"加载中"。它只拿一个
 * 已经解析好的 `DashboardResponse`，不负责取数、轮询或整页错误。
 */
export function DashboardBody({
  data,
  onOpenNow,
  onOpenChat,
  onOpenSettings,
  onRetry,
}: {
  data: DashboardResponse;
  onOpenNow: () => void;
  onOpenChat: () => void;
  onOpenSettings: () => void;
  onRetry: () => void;
}) {
  const s = data.sections;
  const outline: DashboardSection = s.today_outline ?? MISSING_SECTION;
  const intent: DashboardSection = s.intent_teaser ?? MISSING_SECTION;
  const horizonUnplanned = (intent.data as Partial<IntentTeaserOut> | null | undefined)
    ?.horizon_unplanned;
  return (
    <div className="dash__grid">
      {/* ---------------------------------------------------------- A 现在 */}
      <DashboardZone id="now" title="现在" hint="她此刻在做什么、能不能聊" />
      <Card
        title="此刻"
        span={6}
        section={s.activity}
        onRetry={onRetry}
        action={{ label: '查看生活', onClick: onOpenNow }}
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

      <Card
        title="可用性"
        span={3}
        section={s.presence}
        onRetry={onRetry}
        action={{ label: '去聊天', onClick: onOpenChat }}
      >
        {(d) => {
          /*
           * 这一块**有两个**上游，别把它们的回退混成一件：
           *
           * - `availability` / `energy` / `social_load`：presence 自己的状态，
           *   与回退无关，照常显示；
           * - `fallback`：讲的是**场景**（`presence/service.py::resolve_scene`，
           *   场景过期 / 从没提交过 → 领域默认 `unknown`）。
           *
           * ## 为什么现在分成两行说话
           *
           * 这里原来只渲染后端的一个 `describe`，而它本意是**可用性**说明
           * （"状态正常，可以随时说话"）。它被摆在这张卡唯一的描述行上、
           * 紧挨着讲场景的 `FallbackHint`——于是「状态正常，可以随时说话」
           * 会被读成「她的场景正常」，而场景其实是 `unknown`。
           *
           * 后端现在把两件事拆开给了：`scene.note` 说**她在做什么**，
           * `describe_note` 说**能不能说话**。两句话各有各的位置，谁也不
           * 冒充谁。`describe` 仍是给表达层的那一句（正常时是空串），
           * 首屏**不用**它——用它就又会把空串补成一句像结论的话。
           */
          const fallback = fallbackNoticeOf(d);
          const scene = (d.scene ?? {}) as Record<string, unknown>;
          const sceneNote = String(scene.note ?? '');
          // 兼容两种后端形状：新后端分开给 `describe_note`（可用性）与
          // `scene.note`（她在做什么）；旧后端只有 `describe`。
          // 回退到 `describe` 是**为了不让旧响应渲染成空白**，不是为了
          // 复活那个一词两义——新后端下 `describe` 正常时是空串，
          // 这条回退自然不生效。
          const describeNote = String(d.describe_note ?? d.describe ?? '');
          return (
            <>
              <p className="dash-now__line">
                <strong>{AVAILABILITY_LABEL[String(d.availability)] ?? String(d.availability ?? '')}</strong>
                <span className="dash-now__kind">
                  精力 {String(d.energy ?? '?')} · 社交负担 {String(d.social_load ?? '?')}
                </span>
              </p>
              {describeNote ? <p className="dash-card__muted">{describeNote}</p> : null}
              {sceneNote ? <p className="dash-card__muted dash-now__scene">{sceneNote}</p> : null}
              <FallbackHint notice={fallback} scope="她此刻在做什么" />
            </>
          );
        }}
      </Card>

      <Card title="情绪" span={3} section={s.affect} onRetry={onRetry}>
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

      {/*
        「手机」单独一块：她低头看手机那段时间不回消息，不是装死。
        真人一天解锁几十上百次，这段"在忙别的"里她照样在，只是没在看对话。
        把这层画出来，「没秒回」才不会被读成「不理人」。

        `renderWhenEmpty`：`empty`（从没看过手机）的载荷里**没有**事实字段，
        但有「下次该隔多久」这个**参数**和它的来源标记（见后端
        `dashboard.py::phone()` 里那段注释）。用一句"她还没拿起过手机"盖掉它，
        等于在最常见的空态里把回退说明藏起来——而这正是提醒用户"这是默认值"
        最该出现的时候。
      */}
      <Card title="手机" span={6} section={s.phone} renderWhenEmpty onRetry={onRetry}>
        {(d) => <PhoneBody data={d} />}
      </Card>

      {/*
        「正在忙什么」：与「手机」并列而不是合并。看视频≠看手机，
        合起来说会把"她在看手机"这个具体事实换成更含糊的一句。
        `empty`（此刻没有媒体活动）就不渲染——没这件事，不用摆个空壳。
      */}
      {s.media && s.media.status === 'ok' ? (
        <Card title="正在忙什么" span={6} section={s.media} onRetry={onRetry}>
          {(d) => <MediaBody data={d} />}
        </Card>
      ) : null}

      {/*
        没有候选时也照常渲染：`empty` 分支的载荷里带着门控（能不能发、为什么
        不能发），用一句"没有待处理的候选"盖掉它，就等于把「渠道不支持主动
        联系」这件事藏起来了。
      */}
      <Card title="主动联系" span={6} section={s.proactive} renderWhenEmpty onRetry={onRetry}>
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

      {/* ------------------------------------------------------ B 接下来 */}
      <DashboardZone
        id="next"
        title="接下来"
        hint="她可能做什么 / 已约定什么 —— 计划与承诺，不是已经发生的事"
      />

      <Card
        title="今天的节奏"
        span={6}
        section={outline}
        renderWhenEmpty
        onRetry={onRetry}
        action={{ label: '查看生活', onClick: onOpenNow }}
      >
        {(d) => <TodayOutlineBody data={d} />}
      </Card>

      <Card
        title="今天的安排"
        span={6}
        section={s.agenda}
        renderWhenEmpty
        onRetry={onRetry}
        action={{ label: '查看生活', onClick: onOpenNow }}
      >
        {(d) => <AgendaBody data={d} />}
      </Card>

      <Card
        title="未来意向"
        span={12}
        section={intent}
        renderWhenEmpty
        onRetry={onRetry}
        // 「未安排」常驻可见：意向不等于日程，这条不能只在有内容时才说。
        badge={horizonUnplanned === false ? undefined : '未安排'}
        action={{ label: '查看生活', onClick: onOpenNow }}
      >
        {(d) => <IntentTeaserBody data={d} />}
      </Card>

      {/* ------------------------------------------- C 已留下的记录 */}
      <DashboardZone
        id="records"
        title="已留下的记录"
        hint="已经发生的经历、已经记住的事 —— 与「接下来」里的计划分开读"
      />

      <Card title="近期生活" span={6} section={s.timeline} onRetry={onRetry}>
        {(d) => {
          const items = (d.items as Array<Record<string, unknown>>) ?? [];
          return (
            <ul className="dash-list">
              {items.slice(0, CARD_LIST_PREVIEW).map((e) => (
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

      <Card title="最近记住的事" span={6} section={s.memories} onRetry={onRetry}>
        {(d) => {
          const items = (d.items as Array<Record<string, unknown>>) ?? [];
          return (
            <ul className="dash-list">
              {items.slice(0, CARD_LIST_PREVIEW).map((m) => (
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

      <Card
        title="关系"
        span={12}
        section={s.relationship}
        onRetry={onRetry}
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

      <DashboardZone
        id="connectivity"
        title="连接状态"
        hint="渠道与模型能不能用 —— 她说不出话时，问题通常在这里"
      />

      <Card
        title="渠道与模型"
        span={12}
        id="dash-connectivity"
        section={s.connectivity}
        onRetry={onRetry}
        action={{ label: '去设置', onClick: onOpenSettings }}
      >
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
  );
}

export function DashboardPage({ onOpenNow, onOpenChat, onOpenSettings }: DashboardPageProps) {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);
  /**
   * 后台刷新失败的时刻。
   *
   * `error` 只留给"第一次就没拿到"的整页失败；定时刷新/手动刷新失败时页面
   * 上已经有旧卡片，**不能**把它们换成错误面板——那样人正在读的状态会突然
   * 消失。但也不能把旧数据当实时：页头写明哪次刷新没成功，卡片上的
   * 「更新于」保持旧时刻不变。
   */
  const [refreshFailedAt, setRefreshFailedAt] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.dashboard();
      setData(next);
      setError(null);
      setRefreshFailedAt(null);
    } catch (cause) {
      const explained = explainError(cause);
      setError((current) => current ?? explained);
      setRefreshFailedAt((current) => current ?? new Date().toISOString());
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

  // 只有**一次都没成功**时才是整页失败：没数据可降级，只能整体重试。
  if (!data) {
    return (
      <div className="page">
        <div className="dash-error" role="alert">
          <AlertIcon />
          <div>
            <strong>{error?.reason ?? '读取失败'}</strong>
            <p>{error?.hint ?? '这是网络或服务端的问题；稍后再试一次。'}</p>
          </div>
          <button type="button" className="button" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      </div>
    );
  }

  const s = data.sections;
  const reload = () => void load();
  const outline: DashboardSection = s.today_outline ?? MISSING_SECTION;
  const outlineData = (outline.data ?? {}) as Partial<TodayOutlineOut>;
  // 页头的一句话摘要：只说已经拿到的事实，读不到的块不在这里下结论。
  const displayDate = String(outlineData.display_date ?? '');

  return (
    <div className="page dash">
      <header className="dash__head">
        <div className="dash__heading">
          <h1 className="dash__title">总览</h1>
          <p className="dash__subtitle">
            {displayDate ? `今天是 ${displayDate} · ` : ''}
            先看她此刻怎样，再看接下来要做什么
          </p>
        </div>
        <div className="dash__meta">
          <span>更新于 {formatClock(data.generated_at)}</span>
          {refreshFailedAt ? (
            <span className="dash__stale" role="status" data-testid="dash-refresh-failed">
              刚刚这次没有更新成功（{formatClock(refreshFailedAt)}），下面是之前读到的时间
            </span>
          ) : null}
          <button type="button" className="dash__refresh" onClick={reload}>
            <RefreshIcon /> 刷新
          </button>
        </div>
      </header>

      {data.sections.connectivity?.status === 'unavailable' ? (
        // 页尾那块坏了，人在首屏根本滚不下去，而它正是"她没反应"最可能的原因。
        <a className="dash-jump" href="#dash-connectivity" data-testid="dash-connectivity-alert">
          渠道与模型这块暂时读不到，点这里看
        </a>
      ) : null}

      <DashboardBody
        data={data}
        onOpenNow={onOpenNow}
        onOpenChat={onOpenChat}
        onOpenSettings={onOpenSettings}
        onRetry={reload}
      />

      <p className="dash__foot">
        <button type="button" className="dash__refresh" onClick={onOpenChat}>
          直接去聊天
        </button>
      </p>
    </div>
  );
}
