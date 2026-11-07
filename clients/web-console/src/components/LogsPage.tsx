/**
 * 运行日志页（`/logs`）：直接看后端**本进程**最近留下的日志。
 *
 * 为什么要有这一页：这套系统跑在 Docker 里，出问题时要 SSH 上机器 `docker logs`
 * 才能看到一句话。日志接口（`GET /v1/logs`）读的是 API 进程内的环形缓冲，
 * 所以这一页能回答"她刚才为什么没回"，而不必先登进宿主机。
 *
 * ## 三条必须说清的事实（界面上都要写出来，不能只活在代码注释里）
 *
 * 1. **只有本进程**。多副本部署时每个副本各有一份缓冲，这里看到的是被分到
 *    的那一个进程。不是"全集群日志"。
 * 2. **只有最近 N 条**。缓冲满了就覆盖，没有分页、没有游标、没有持久化。
 *    更早的记录不在这个入口的范围内——不画假翻页。
 * 3. **服务端已脱敏**。消息里的 `api_key=…`、`access_token` 之类在写进缓冲前
 *    就被替换成 `[REDACTED]`。界面不做任何还原，也不提供"看原文"。
 *
 * ## 轮询与暂停
 *
 * 进页面先拉一次，之后每 2 秒一次（`LOG_POLL_MS`）。暂停是**真的停**：
 * 轮询函数每个 tick 都读 `isPaused()`，暂停时既不发起新请求，也不保留旧定时器
 * 语义——恢复后从下一个 tick 继续。测试用假时钟断言"暂停期间 fetch 一次都没调"，
 * 而不是只断言按钮上的字变了。
 *
 * 轮询循环被抽成不依赖 React 的 `startLogPolling`，就是为了能这样测：
 * 写在 `useEffect` 里的话，仓库没有 jsdom，只能断言渲染字符串，测不到"还发不发请求"。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { LogEntry, LogKind, LogLevel, LogsResponse } from '../types';
import { AlertIcon, RefreshIcon } from './Icons';
import '../logs.css';

/** 轮询间隔。2 秒：比人眼察觉新行的速度快，又不至于把接口打成压力源。 */
export const LOG_POLL_MS = 2000;

/** 搜索防抖。300ms：连续打字时只发最后一次请求。 */
export const LOG_SEARCH_DEBOUNCE_MS = 300;

/** 单次拉取的条数（后端上限 1000；200 够一屏翻看，也不会让每 2 秒的响应变胖）。 */
export const LOG_LIMIT = 200;

/** 距底部多少像素算"贴着底"。给一点余量：浏览器取整后 scrollTop 常常差 1px。 */
export const STICK_THRESHOLD_PX = 24;

/* ==================================================================== 纯逻辑 */

export type LevelTone = 'error' | 'warning' | 'info' | 'debug';

/**
 * 级别 → 颜色档。
 *
 * `critical` 归到 error 档（它比 error 更严重，绝不能显示成中性色）；
 * 认不出来的级别同样按 info 档显示，但**文字原样输出**——把未知级别改写成
 * "info" 会让人以为记录本身是 info。
 */
export function levelTone(level: string): LevelTone {
  switch (level.toLowerCase()) {
    case 'critical':
    case 'fatal':
    case 'error':
      return 'error';
    case 'warning':
    case 'warn':
      return 'warning';
    case 'debug':
    case 'trace':
      return 'debug';
    default:
      return 'info';
  }
}

/** 级别排序（"不低于"判定用）；未知级别排最后，不会被 `error` 筛选漏掉。 */
export function levelRank(level: string): number {
  switch (level.toLowerCase()) {
    case 'debug':
    case 'trace':
      return 10;
    case 'info':
      return 20;
    case 'warning':
    case 'warn':
      return 30;
    case 'critical':
    case 'fatal':
    case 'error':
      return 40;
    default:
      return 20;
  }
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

/**
 * 时间戳 → `HH:mm:ss.SSS`（本机时区）。
 *
 * 带毫秒：日志排查里同一秒常常有十几条，只到秒就分不清先后。
 * 日期不显示——缓冲只覆盖最近 N 条（默认 500），跨天的可能性远小于同秒冲突；
 * 需要日期时悬停的 `title` 里有完整的 ISO 串。解析失败原样返回，不编时间。
 */
export function formatLogTime(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(
    date.getMilliseconds(),
    3,
  )}`;
}

/** 悬停用的绝对时间（本地时区，含日期与毫秒）。 */
export function formatLogTimestampFull(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

/**
 * 列表是否需要自动跟到底部。
 *
 * 用户手动往上滚之后**不要**强制拉回底部：正在读一段报错时被新日志顶走，
 * 比看不到新日志更烦。`distance <= threshold` 才算贴底，滚轮往上一点点
 * （常见的中途停顿）就已经不贴底了。
 */
export function isNearBottom(
  metrics: { scrollTop: number; scrollHeight: number; clientHeight: number },
  threshold = STICK_THRESHOLD_PX,
): boolean {
  const distance = metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight;
  // 内容比容器短（没有滚动条）时 scrollHeight=clientHeight、distance=0，算贴底
  return distance <= threshold;
}

/** 条数与缓冲占用的说明。不把"最近 200 条"说成"全部日志"。 */
export function describeCounts(shown: number, buffered: number, capacity: number): string {
  const head = `已显示 ${shown} 条`;
  if (buffered <= 0) return `${head} · 缓冲为空`;
  // 缓冲写满之后旧条目已经被覆盖，这时"最近 N 条"与"全部"才等价
  const tail =
    buffered >= capacity
      ? `缓冲已满（${buffered}/${capacity}，更早的已被覆盖）`
      : `缓冲 ${buffered}/${capacity}`;
  return `${head} · ${tail}`;
}

/**
 * 被白名单拒掉的入站条数。
 *
 * 这个数字与「是否逐条写拒绝日志」**无关**：关掉逐条 WARNING 之后，它是
 * 界面里唯一还能回答"最近有没有人在扫我的机器人"的证据。所以即使为 0 也
 * 要显示——0 也是答案（"没人被拒"），而不是隐藏整个字段让人以为没这功能。
 */
export function describeDenials(deniedTotal: number): string {
  if (deniedTotal <= 0) return '本进程还没有拒绝过入站消息';
  return `本进程已拒绝 ${deniedTotal} 条入站消息（白名单/出入许可）`;
}

/** 一条日志的主行（时间 级别 logger 消息）——四种信息缺一不可定位。 */
export function logLineParts(entry: LogEntry): {
  time: string;
  level: string;
  logger: string;
  message: string;
} {
  return {
    time: formatLogTime(entry.timestamp),
    level: entry.level.toUpperCase(),
    logger: entry.logger || '(未命名 logger)',
    message: entry.message,
  };
}

/* ==================================================================== 轮询 */

export interface LogPollOptions {
  /** 每次真正发起请求时调；`signal` 在停止/换挡时被 abort。 */
  fetchOnce: (signal: AbortSignal) => Promise<LogsResponse>;
  onResult: (response: LogsResponse) => void;
  /** 失败**必须**交给界面显示（本页不吞异常）。 */
  onError: (error: unknown) => void;
  /** 每个 tick 读一次；为 true 时**不发起请求**。 */
  isPaused: () => boolean;
  intervalMs?: number;
}

/**
 * 启动轮询，返回停止函数。
 *
 * 停止函数会 clearTimeout **并** abort 在途请求：只清定时器的话，暂停那一刻
 * 已经在飞的那条响应仍会回来写状态，界面看起来"暂停了还在动"。
 */
export function startLogPolling(options: LogPollOptions): () => void {
  const interval = options.intervalMs ?? LOG_POLL_MS;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;

  const schedule = () => {
    if (stopped) return;
    timer = setTimeout(() => {
      void tick();
    }, interval);
  };

  const tick = async () => {
    if (stopped) return;
    if (options.isPaused()) {
      // 暂停：不请求，但保留下一个 tick，恢复时不必等重新挂载
      schedule();
      return;
    }
    controller = new AbortController();
    try {
      const response = await options.fetchOnce(controller.signal);
      if (stopped) return;
      options.onResult(response);
    } catch (error) {
      if (stopped) return;
      // 主动 abort（暂停/换挡）不是故障，不能弹一条"网络错误"
      if (error instanceof DOMException && error.name === 'AbortError') {
        schedule();
        return;
      }
      options.onError(error);
    }
    schedule();
  };

  void tick();

  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    controller?.abort();
  };
}

/* ==================================================================== 状态 */

export type LogsView =
  | { status: 'loading' }
  | { status: 'ready'; data: LogsResponse }
  | { status: 'error'; error: ExplainedError };

export type LogLevelFilter = LogLevel | 'all';

/** 「全部」之外的四个分类与后端 `LOG_KINDS` 同源（见 types.ts 的 LogKind）。 */
export type LogKindFilter = LogKind | 'all';

/**
 * 权限不足的文案单独写：`explainError` 里的 `forbidden` 是给会话/记忆写的
 * （"没有权限访问该会话"），放在这一页会指错方向。其余错误码仍走通用解释，
 * 不另抄一份文案表。
 */
export function logsErrorCopy(error: unknown): ExplainedError {
  const explained = explainError(error);
  if (explained.code === 'forbidden') {
    return {
      ...explained,
      reason: '只有所有者或管理员可以查看运行日志',
      hint: '这一页会暴露内部路径与账号标识，所以只对运维身份开放。',
    };
  }
  return explained;
}

export interface LogsHeaderProps {
  /** 条数说明由页面状态算出（暂停/加载/失败各有不同文案），页头只负责摆放。 */
  counts: string;
}

/**
 * 页头：标题、渲染规则说明、条数说明。
 *
 * 条数说明放在这里而不是工具栏或列表上方：它是"这一页能看到多少"的说明，
 * 不是一项筛选控件。放在列表正上方会横成一整条窄带（工具栏换行后尤其明显），
 * 把列表往下挤，所以它归页头。
 */
export function LogsHeader({ counts }: LogsHeaderProps) {
  return (
    <div className="page__head" data-testid="logs-head">
      <div>
        <h1 className="page__title">日志</h1>
        <p className="page__subtitle">
          每次最多 {LOG_LIMIT} 条，按时间正序排列，最新的在最下面，每 {LOG_POLL_MS / 1000} 秒自动刷新。
        </p>
      </div>
      <p className="logs__count" data-testid="logs-count">
        {counts}
      </p>
    </div>
  );
}

export interface LogsToolbarProps {
  kind: LogKindFilter;
  level: LogLevelFilter;
  search: string;
  paused: boolean;
  /** logger 名默认不显示（列很宽，会挤掉消息），需要按来源排查时才打开。 */
  showLogger: boolean;
  onKind: (kind: LogKindFilter) => void;
  onLevel: (level: LogLevelFilter) => void;
  onSearch: (value: string) => void;
  onTogglePause: () => void;
  onRefresh: () => void;
  onToggleLogger: () => void;
}

/** 顶部一行控件：分类 / 级别 / 搜索 / 暂停 / 刷新 / 显示方法名。单独导出以便 SSR 断言。 */
export function LogsToolbar({
  kind,
  level,
  search,
  paused,
  showLogger,
  onKind,
  onLevel,
  onSearch,
  onTogglePause,
  onRefresh,
  onToggleLogger,
}: LogsToolbarProps) {
  return (
    <div className="logs__toolbar" data-testid="logs-toolbar">
      <label className="logs__control">
        <span className="logs__control-label">分类</span>
        <select
          className="logs__select"
          data-testid="logs-kind"
          value={kind}
          onChange={(event) => onKind(event.target.value as LogKindFilter)}
        >
          {/* 默认「全部」：不预设筛选，避免有人以为"日志丢了"。
              要只看她的处理过程时选「处理」。 */}
          <option value="all">全部</option>
          <option value="runtime">运行</option>
          <option value="behavior">处理</option>
          <option value="audit">审计</option>
          <option value="security">安全</option>
        </select>
      </label>

      <label className="logs__control">
        <span className="logs__control-label">级别</span>
        <select
          className="logs__select"
          data-testid="logs-level"
          value={level}
          onChange={(event) => onLevel(event.target.value as LogLevelFilter)}
        >
          {/* 「全部」是默认：第一次打开这一页的人想看的是最近发生了什么，
              而不是"先选一个筛选条件" */}
          <option value="all">全部</option>
          <option value="debug">debug</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
      </label>

      <label className="logs__control logs__control--grow">
        <span className="logs__control-label">搜索</span>
        <input
          className="logs__input"
          data-testid="logs-search"
          type="search"
          value={search}
          placeholder="消息里的词（不区分大小写）"
          onChange={(event) => onSearch(event.target.value)}
        />
      </label>

      <button
        type="button"
        className={`button button--small${paused ? '' : ' button--ghost'}`}
        data-testid="logs-pause"
        aria-pressed={paused}
        onClick={onTogglePause}
      >
        {paused ? '继续' : '暂停'}
      </button>

      <button
        type="button"
        className="button button--small button--ghost"
        data-testid="logs-refresh"
        onClick={onRefresh}
      >
        <RefreshIcon /> 刷新
      </button>

      {/* 默认关闭：logger 名又长又每行重复（ling.connectors.onebot.ws_server），
          打开后消息列在窄屏上只剩几个字。要按来源排查时再打开。 */}
      <label className="logs__toggle" title="每行左侧的 logger 名，例如 ling.behavior">
        <input
          type="checkbox"
          data-testid="logs-show-logger"
          checked={showLogger}
          onChange={onToggleLogger}
        />
        <span>显示方法名</span>
      </label>
    </div>
  );
}

export interface LogsListProps {
  items: LogEntry[];
  level: LogLevelFilter;
  /**
   * 是否显示 logger 名。为 false 时**不渲染那一列**（而不是渲染一个空占位格）：
   * 留空列会把消息列挤窄，窄屏上消息就只剩几个字。
   */
  showLogger: boolean;
}

/**
 * 日志行。等宽字体、时间/级别/消息三段，logger 名可选。
 *
 * 级别**同时用颜色和文字**表达：只靠颜色的话，色觉差异或截图转灰之后就只剩
 * 一串无法区分的行。
 *
 * 刻意**不加** `aria-live`：列表每 2 秒整批重渲染，live region 会让读屏在
 * 每一拍把整屏日志重念一遍，实际效果是这一页对读屏用户完全不可用。
 */
export function LogsList({ items, level, showLogger }: LogsListProps) {
  return (
    <ol
      // 没有 logger 列时由修饰类换一套列宽（见 logs.css），时间/级别仍定宽对齐
      className={`logs__list${showLogger ? '' : ' logs__list--no-logger'}`}
      data-testid="logs-list"
    >
      {items.map((entry, index) => {
        const parts = logLineParts(entry);
        const tone = levelTone(entry.level);
        // 行 key 用「时间戳+logger+序号」：同一毫秒可能有多条完全相同的行
        // （例如同样的告警刷两次），只靠内容做 key 会让 React 复用错行。
        return (
          <li
            className={`logs__row logs__row--${tone}`}
            data-testid="logs-row"
            data-level={entry.level}
            key={`${entry.timestamp}|${entry.logger}|${index}`}
            title={entry.trace_id ? `trace_id ${entry.trace_id}` : undefined}
          >
            <time className="logs__time" dateTime={entry.timestamp} title={formatLogTimestampFull(entry.timestamp)}>
              {parts.time}
            </time>
            <span className={`logs__level logs__level--${tone}`}>{parts.level}</span>
            {showLogger ? <span className="logs__logger">{parts.logger}</span> : null}
            <span className="logs__message">{parts.message}</span>
          </li>
        );
      })}
      {items.length === 0 ? (
        <li className="logs__empty-row">
          {/* 空状态要说清**为什么**空，而不是只写"暂无数据" */}
          <p className="logs__empty" data-testid="logs-empty">
            后端暂无日志。
          </p>
          <p className="logs__empty-hint">
            {level === 'all'
              ? '这一页读的是本进程的内存缓冲：服务刚启动、或还没有任何请求时它确实是空的。'
              : '当前有级别筛选；把级别切回「全部」再确认一次。'}
          </p>
        </li>
      ) : null}
    </ol>
  );
}

/* ==================================================================== 页面 */

export function LogsPage() {
  const [kind, setKind] = useState<LogKindFilter>('all');
  const [level, setLevel] = useState<LogLevelFilter>('all');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [paused, setPaused] = useState(false);
  // 默认关：先让人读得清消息。不持久化——这一页是排障时的临时视图，
  // 记住上次的显示选择反而会让某次排查后"名字突然不见了"成了新谜题。
  const [showLogger, setShowLogger] = useState(false);
  const [view, setView] = useState<LogsView>({ status: 'loading' });
  const [refreshNonce, setRefreshNonce] = useState(0);

  /*
   * 暂停有**两道闸**，缺一不可：
   *
   * 1. `paused` 在轮询 effect 的依赖里 → 一按暂停就执行 cleanup（clearTimeout
   *    + abort 在途请求）。只靠第 2 道的话，暂停那一刻已经在飞的那条响应
   *    还会回来写状态，界面看起来"暂停了还在动"。
   * 2. 循环内部每个 tick 再看一次 `isPaused()` → 即便某一拍恰好落在状态
   *    更新的间隙里，也不会发请求。
   *
   * 用 ref 读当前值：循环是长生命周期的闭包，直接闭包捕获 `paused` 会一直
   * 读到挂载那一刻的 false。
   */
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const viewportRef = useRef<HTMLDivElement | null>(null);
  // 是否贴底。初值 true：刚进页面就该在最新一条上，而不是停在缓冲区顶部。
  const stickRef = useRef(true);

  // 搜索防抖：300ms 内的连续输入只发最后一次请求
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), LOG_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [search]);

  const fetchOnce = useCallback(
    (signal: AbortSignal) =>
      api.getLogs(
        {
          limit: LOG_LIMIT,
          // 分类原样交给后端筛选：前端再滤一遍会让「buffered」与实际显示
          // 对不上，而人没法分辨差在哪。
          kind: kind === 'all' ? null : kind,
          level: level === 'all' ? null : level,
          // 搜索词原样交给后端：前端不再过滤一遍，否则"前端滤掉的行"与接口
          // 说的 `buffered` 会对不上，而人没法分辨差在哪。
          q: debouncedSearch.trim() || null,
        },
        signal,
      ),
    [kind, level, debouncedSearch],
  );

  useEffect(() => {
    if (paused) return;
    const stop = startLogPolling({
      fetchOnce,
      isPaused: () => pausedRef.current,
      onResult: (data) => setView({ status: 'ready', data }),
      onError: (error) => setView({ status: 'error', error: logsErrorCopy(error) }),
    });
    return stop;
  }, [fetchOnce, paused, refreshNonce]);

  // 换筛选条件或点刷新时回到「正在读取」：否则旧级别的内容会留在屏幕上，
  // 让人以为那就是新的筛选结果。
  //
  // 暂停时**不**清空：那时不会发请求，清空只会留下一个永远转不完的加载态。
  // 暂停中改筛选的效果由条数那行明说（见下面的 counts）。
  useEffect(() => {
    if (pausedRef.current) return;
    setView({ status: 'loading' });
  }, [level, debouncedSearch, refreshNonce]);

  // 暂停由 effect 依赖直接承担：`paused` 一变就执行上面的 cleanup（clearTimeout
  // + abort 在途请求），所以暂停是**立刻**停手，不是等下一个 tick 才不发请求。

  // 新数据到达后：只在贴底时才自动滚到底部
  const items = view.status === 'ready' ? view.data.items : [];
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !stickRef.current) return;
    viewport.scrollTop = viewport.scrollHeight;
  }, [items]);

  const onScroll = () => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    stickRef.current = isNearBottom(viewport);
  };

  const counts = paused
    ? // 暂停期间不请求，所以缓存里的数据可能已经不符合当前筛选条件。
      // 明说这一点，而不是让"暂停中改了级别却还是旧内容"看起来像 bug。
      '已暂停 · 继续后按当前条件重新加载'
    : view.status === 'ready'
      ? describeCounts(view.data.items.length, view.data.buffered, view.data.capacity)
      : view.status === 'loading'
        ? '正在读取…'
        : '读取失败';

  return (
    <div className="page logs-page" data-testid="logs-page">
      <LogsHeader counts={counts} />

      <p className="logs__boundary" data-testid="logs-boundary">
        只有本进程：多副本部署时这里看到的是被分到的那一个副本。缓冲写满后最旧的会被覆盖，
        更早的记录不在这个入口里（要长历史请看容器日志采集）。
        凭据类内容已在服务端脱敏成 [REDACTED]，界面不做还原。
      </p>

      {/* 关掉逐条拒绝日志之后，这个计数是界面里唯一还能回答
          "最近有没有人在扫我的机器人"的证据（见 `describeDenials`）。 */}
      {view.status === 'ready' ? (
        <p className="logs__boundary" data-testid="logs-denied-total">
          {describeDenials(view.data.denied_total)}。逐条拒绝日志可以在设置里关掉，
          这个计数不受那个开关影响。
        </p>
      ) : null}

      <LogsToolbar
        level={level}
        search={search}
        paused={paused}
        showLogger={showLogger}
        kind={kind}
        onKind={setKind}
        onLevel={setLevel}
        onSearch={setSearch}
        onTogglePause={() => setPaused((value) => !value)}
        onRefresh={() => setRefreshNonce((value) => value + 1)}
        onToggleLogger={() => setShowLogger((value) => !value)}
      />

      {view.status === 'error' ? (
        <div className="banner banner--error" role="alert" data-testid="logs-error">
          <AlertIcon />
          <div>
            <strong>{view.error.reason}</strong>
            {view.error.hint ? <div className="banner__detail">{view.error.hint}</div> : null}
            {view.error.detail ? <div className="banner__detail">{view.error.detail}</div> : null}
          </div>
          <button
            type="button"
            className="button button--small"
            onClick={() => setRefreshNonce((value) => value + 1)}
          >
            重试
          </button>
        </div>
      ) : null}

      <div className="logs__viewport" data-testid="logs-viewport" ref={viewportRef} onScroll={onScroll}>
        {view.status === 'loading' && items.length === 0 ? (
          <div className="logs__loading">
            <span className="spinner" aria-hidden="true" /> 正在读取日志…
          </div>
        ) : (
          <LogsList items={items} level={level} showLogger={showLogger} />
        )}
      </div>
    </div>
  );
}
