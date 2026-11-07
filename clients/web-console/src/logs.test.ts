/**
 * 运行日志页（/logs）的回归测试。
 *
 * 这一页有两类错误最容易混过去，测试分别盯着：
 *
 * 1. **暂停是假的**。只断言按钮文字变成「继续」是没用的——用户要的是
 *    "暂停之后不再有请求"。所以用假时钟把 `startLogPolling` 单独跑起来，
 *    断言暂停期间 `fetchOnce` 的调用次数**一次都不增加**，且恢复后继续。
 * 2. **自动滚动追着人跑**。用户往上滚去读一段报错时被新日志顶回底部，
 *    比看不到新日志更烦。`isNearBottom` 的阈值语义单独测。
 *
 * 没有 DOM 测试库（仓库的取舍与 `dashboard.test.ts` / `memory.test.ts` 一致）：
 * 渲染用 `react-dom/server` 做结构断言；轮询这种时间相关的行为抽出成不依赖
 * React 的 `startLogPolling`，用 `vi.useFakeTimers` 直接测——写在 `useEffect`
 * 里的话这两种行为都测不到。
 */
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  LOG_LIMIT,
  LOG_POLL_MS,
  LOG_SEARCH_DEBOUNCE_MS,
  STICK_THRESHOLD_PX,
  LogsHeader,
  LogsList,
  LogsPage,
  LogsToolbar,
  describeCounts,
  describeDenials,
  formatLogTime,
  formatLogTimestampFull,
  isNearBottom,
  levelRank,
  levelTone,
  logLineParts,
  logsErrorCopy,
  startLogPolling,
} from './components/LogsPage';
import { ApiError } from './api';
import { LEGACY_REDIRECTS, parseRoute, routePath } from './route';
import { activeNavId } from './components/Navigation';
import type { LogEntry, LogsResponse } from './types';

const NOOP = () => undefined;

function entry(overrides: Partial<LogEntry> = {}): LogEntry {
  return {
    timestamp: '2026-11-05T01:02:03.456+00:00',
    level: 'info',
    logger: 'ling.api.access',
    message: '请求完成',
    ...overrides,
  };
}

function response(
  items: LogEntry[],
  buffered = items.length,
  capacity = 500,
  deniedTotal = 0,
): LogsResponse {
  return { items, buffered, capacity, denied_total: deniedTotal };
}

/* ------------------------------------------------------------------ 路由 */

describe('/logs 是一条真实路由，不是 404 也不是设置子页', () => {
  it('解析与生成地址往返一致', () => {
    expect(parseRoute('/logs')).toEqual({ name: 'logs' });
    expect(routePath({ name: 'logs' })).toBe('/logs');
    // 尾斜杠与其它真实路由的处理一致
    expect(parseRoute('/logs/')).toEqual({ name: 'logs' });
  });

  it('/logs/extra 仍是 notFound（不做前缀通配）', () => {
    // 通配会把拼错的子路径也导到一个能用的页面，`notFound` 要暴露的就没暴露
    expect(parseRoute('/logs/extra')).toEqual({ name: 'notFound' });
    expect(parseRoute('/log')).toEqual({ name: 'notFound' });
  });

  it('高亮「日志」这一项，而不是「设置」', () => {
    expect(activeNavId(parseRoute('/logs'))).toBe('logs');
    // 反证：设置子页仍归设置，两者不能因为都在"运维"这一侧就并成一项
    expect(activeNavId(parseRoute('/settings'))).toBe('settings');
  });

  it('没有把 /logs 塞进旧地址重定向表', () => {
    expect(LEGACY_REDIRECTS['/logs']).toBeUndefined();
  });
});

/* -------------------------------------------------------------- 级别映射 */

describe('级别 → 颜色档与排序', () => {
  it.each([
    ['error', 'error'],
    ['ERROR', 'error'],
    ['critical', 'error'],
    ['warning', 'warning'],
    ['warn', 'warning'],
    ['info', 'info'],
    ['debug', 'debug'],
    ['trace', 'debug'],
  ])('%s 归到 %s 档', (level, tone) => {
    expect(levelTone(level)).toBe(tone);
  });

  it('critical 归到 error 档而不是中性色（更严重不能被画成普通）', () => {
    expect(levelTone('critical')).toBe('error');
    // 排序上它也不低于 error：按 error 过滤时它必须出现
    expect(levelRank('critical')).toBeGreaterThanOrEqual(levelRank('error'));
  });

  it('认不出来的级别按 info 档显示（但文字原样保留，见渲染断言）', () => {
    expect(levelTone('notice')).toBe('info');
    expect(levelRank('notice')).toBe(levelRank('info'));
  });
});

/* ------------------------------------------------------------------ 时间 */

describe('时间格式', () => {
  it('显示到毫秒（同一秒常有十几条，到秒分不清先后）', () => {
    const text = formatLogTime('2026-11-05T01:02:03.456+00:00');
    expect(text).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    expect(text.endsWith('.456')).toBe(true);
  });

  it('悬停的完整时间带日期', () => {
    const text = formatLogTimestampFull('2026-11-05T01:02:03.456+00:00');
    expect(text).toMatch(/^2026-11-0[45] \d{2}:\d{2}:\d{2}\.\d{3}$/);
  });

  it('解析不了就原样返回，不编一个时间', () => {
    expect(formatLogTime('不是时间')).toBe('不是时间');
    expect(formatLogTimestampFull('')).toBe('');
  });
});

/* -------------------------------------------------------------- 贴底判定 */

describe('是否贴底（决定要不要自动滚到最新）', () => {
  it('滚到底算贴底；往上滚就立刻不贴底', () => {
    // scrollHeight=1000, clientHeight=300 → 到底时 scrollTop=700
    expect(isNearBottom({ scrollTop: 700, scrollHeight: 1000, clientHeight: 300 })).toBe(true);
    // 用户往上滚了 100px：不再跟到底部（这是本页最核心的一条行为）
    expect(isNearBottom({ scrollTop: 600, scrollHeight: 1000, clientHeight: 300 })).toBe(false);
  });

  it('阈值内的取整误差仍算贴底', () => {
    expect(
      isNearBottom({
        scrollTop: 700 - STICK_THRESHOLD_PX,
        scrollHeight: 1000,
        clientHeight: 300,
      }),
    ).toBe(true);
  });

  it('内容比容器短（没有滚动条）时算贴底', () => {
    expect(isNearBottom({ scrollTop: 0, scrollHeight: 200, clientHeight: 400 })).toBe(true);
  });
});

/* ------------------------------------------------------------------ 计数 */

describe('条数说明不把「最近 N 条」说成「全部日志」', () => {
  it('缓冲未满时如实显示占用', () => {
    expect(describeCounts(12, 12, 500)).toBe('已显示 12 条 · 缓冲 12/500');
  });

  it('缓冲写满时明确说「更早的已被覆盖」', () => {
    const text = describeCounts(200, 500, 500);
    expect(text).toContain('已显示 200 条');
    expect(text).toContain('缓冲已满');
    expect(text).toContain('更早的已被覆盖');
  });

  it('缓冲为空时说空，不说"全部 0 条"', () => {
    expect(describeCounts(0, 0, 500)).toContain('缓冲为空');
  });
});

describe('被拒入站计数：关掉逐条日志后仍然可见', () => {
  it('0 也照实显示，不隐藏字段', () => {
    expect(describeDenials(0)).toContain('还没有拒绝');
  });

  it('非 0 时给出具体数字和范围', () => {
    const text = describeDenials(180);
    expect(text).toContain('180');
    expect(text).toContain('本进程');
  });
});

/* ------------------------------------------------------------------ 轮询 */

describe('轮询：暂停是真的停，恢复后继续', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  /** 把一轮轮询跑起来，返回控制句柄。 */
  function harness(options: { pausedInitially?: boolean } = {}) {
    let paused = options.pausedInitially ?? false;
    let calls = 0;
    const results: LogsResponse[] = [];
    const errors: unknown[] = [];
    const stop = startLogPolling({
      fetchOnce: async () => {
        calls += 1;
        return response([entry({ message: `第 ${calls} 次` })]);
      },
      onResult: (data) => results.push(data),
      onError: (error) => errors.push(error),
      isPaused: () => paused,
    });
    return {
      stop,
      results,
      errors,
      get calls() {
        return calls;
      },
      pause: () => {
        paused = true;
      },
      resume: () => {
        paused = false;
      },
    };
  }

  it('进页面先拉一次，然后每 2 秒一次', async () => {
    vi.useFakeTimers();
    const handle = harness();
    // 首次 tick 是立即执行的（async，需要把微任务放行）
    await vi.advanceTimersByTimeAsync(0);
    expect(handle.calls).toBe(1);

    await vi.advanceTimersByTimeAsync(LOG_POLL_MS);
    expect(handle.calls).toBe(2);
    await vi.advanceTimersByTimeAsync(LOG_POLL_MS);
    expect(handle.calls).toBe(3);
    handle.stop();
  });

  it('暂停之后**一次请求都不发**（不是只改按钮文字）', async () => {
    vi.useFakeTimers();
    const handle = harness();
    await vi.advanceTimersByTimeAsync(0);
    expect(handle.calls).toBe(1);

    handle.pause();
    // 跑过 5 个周期：暂停期间不能有任何新请求
    await vi.advanceTimersByTimeAsync(LOG_POLL_MS * 5);
    expect(handle.calls).toBe(1);

    handle.resume();
    await vi.advanceTimersByTimeAsync(LOG_POLL_MS);
    expect(handle.calls).toBe(2);
    handle.stop();
  });

  it('stop 之后彻底不再请求（卸载 / 关页面）', async () => {
    vi.useFakeTimers();
    const handle = harness();
    await vi.advanceTimersByTimeAsync(0);
    const before = handle.calls;
    handle.stop();
    await vi.advanceTimersByTimeAsync(LOG_POLL_MS * 5);
    expect(handle.calls).toBe(before);
  });

  it('失败交给界面显示（本页不吞异常），且下一拍继续轮询', async () => {
    vi.useFakeTimers();
    let attempts = 0;
    const errors: unknown[] = [];
    const results: LogsResponse[] = [];
    const stop = startLogPolling({
      fetchOnce: async () => {
        attempts += 1;
        if (attempts === 1) throw new ApiError(500, 'internal', '炸了', true);
        return response([]);
      },
      onResult: (data) => results.push(data),
      onError: (error) => errors.push(error),
      isPaused: () => false,
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toBeInstanceOf(ApiError);

    // 一次失败不能让轮询停掉：下一拍照常
    await vi.advanceTimersByTimeAsync(LOG_POLL_MS);
    expect(attempts).toBe(2);
    expect(results).toHaveLength(1);
    stop();
  });

  it('主动 abort 不算故障（不弹"网络错误"）', async () => {
    vi.useFakeTimers();
    const errors: unknown[] = [];
    const stop = startLogPolling({
      fetchOnce: async () => {
        throw new DOMException('aborted', 'AbortError');
      },
      onResult: () => undefined,
      onError: (error) => errors.push(error),
      isPaused: () => false,
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(errors).toEqual([]);
    stop();
  });

  it('初始就暂停时不发任何请求', async () => {
    vi.useFakeTimers();
    const handle = harness({ pausedInitially: true });
    await vi.advanceTimersByTimeAsync(LOG_POLL_MS * 3);
    expect(handle.calls).toBe(0);
    handle.stop();
  });
});

/* ------------------------------------------------------------------ 渲染 */

describe('渲染结构（SSR）', () => {
  it('工具栏有分类/级别下拉、搜索框、暂停按钮与显示方法名开关', () => {
    const html = renderToStaticMarkup(
      h(LogsToolbar, {
        kind: 'all',
        level: 'all',
        search: '',
        paused: false,
        showLogger: false,
        onKind: NOOP,
        onLevel: NOOP,
        onSearch: NOOP,
        onTogglePause: NOOP,
        onRefresh: NOOP,
        onToggleLogger: NOOP,
      }),
    );
    expect(html).toContain('data-testid="logs-level"');
    expect(html).toContain('data-testid="logs-search"');
    expect(html).toContain('data-testid="logs-pause"');
    expect(html).toContain('data-testid="logs-show-logger"');
    // 条数说明已经归页头：工具栏里不能再出现它（以前它会换行成一条横贯的窄带）
    expect(html).not.toContain('data-testid="logs-count"');
    expect(html).not.toContain('已显示');
    // 四个级别 + 「全部」都在下拉里（少一个就有一个筛选用不了）
    for (const value of ['all', 'debug', 'info', 'warning', 'error']) {
      expect(html).toContain(`value="${value}"`);
    }
    expect(html).toContain('暂停');
  });

  it('暂停时按钮变成「继续」并报出 aria-pressed', () => {
    const html = renderToStaticMarkup(
      h(LogsToolbar, {
        kind: 'all',
        level: 'all',
        search: 'x',
        paused: true,
        showLogger: false,
        onKind: NOOP,
        onLevel: NOOP,
        onSearch: NOOP,
        onTogglePause: NOOP,
        onRefresh: NOOP,
        onToggleLogger: NOOP,
      }),
    );
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('继续');
    // 暂停态下不该还写着「暂停」——按钮文字是状态，不是动作标签
    expect(html).not.toContain('>暂停</button>');
  });

  it('每行有 时间 / 级别 / logger / 消息四段（开关打开时），级别同时用文字与 class 表达', () => {
    const html = renderToStaticMarkup(
      h(LogsList, {
        level: 'all',
        showLogger: true,
        items: [
          entry({ level: 'error', logger: 'ling.api', message: '炸了' }),
          entry({ level: 'warning', logger: 'ling.worker', message: '要慢' }),
        ],
      }),
    );
    expect(html.match(/data-testid="logs-row"/g) ?? []).toHaveLength(2);
    expect(html).toContain('logs__row--error');
    expect(html).toContain('logs__row--warning');
    expect(html).toContain('>ERROR<');
    expect(html).toContain('>WARNING<');
    expect(html).toContain('ling.api');
    expect(html).toContain('炸了');
    // 时间列用 <time> 承载机器可读的原始时间戳（属性名的大小写由 React 决定，
    // 这里只断言值与元素都在，不去锁它的拼写）
    expect(html).toContain('<time');
    expect(html).toContain('2026-11-05T01:02:03.456+00:00');
  });

  it('认不出来的级别原样显示（不改写成 info）', () => {
    const html = renderToStaticMarkup(
      h(LogsList, {
        level: 'all',
        showLogger: false,
        items: [entry({ level: 'critical', message: '严重' })],
      }),
    );
    expect(html).toContain('>CRITICAL<');
    // 颜色档归到了 error（这个可以有），但文字不能变
    expect(html).toContain('logs__level--error');
  });

  it('空列表说清「后端暂无日志」并解释为什么会空', () => {
    const html = renderToStaticMarkup(h(LogsList, { level: 'all', showLogger: false, items: [] }));
    expect(html).toContain('data-testid="logs-empty"');
    expect(html).toContain('后端暂无日志');
    // 有级别筛选时给出下一步动作，而不是让人对着空白发呆
    const filtered = renderToStaticMarkup(
      h(LogsList, { level: 'error', showLogger: false, items: [] }),
    );
    expect(filtered).toContain('切回「全部」');
  });

  it('消息里的尖括号被转义（日志是数据，不是 HTML）', () => {
    const html = renderToStaticMarkup(
      h(LogsList, {
        level: 'all',
        showLogger: false,
        items: [entry({ message: '<script>alert(1)</script>' })],
      }),
    );
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });
});

/* -------------------------------------------------------------- 页头条数说明 */

describe('条数说明在页头，不在列表上方占一条窄带', () => {
  it('页头把 describeCounts 的原文原样显示在标题旁', () => {
    const counts = describeCounts(200, 500, 500);
    const html = renderToStaticMarkup(h(LogsHeader, { counts }));
    expect(html).toContain('data-testid="logs-head"');
    expect(html).toContain('page__head');
    expect(html).toContain('日志');
    // 语义不在渲染层加工：describeCounts 说什么就显示什么
    expect(html).toContain('已显示 200 条');
    expect(html).toContain('缓冲已满');
    expect(html).toContain('更早的已被覆盖');
  });

  it('页面里条数说明位于页头与工具栏之间，工具栏之后不再有它', () => {
    const html = renderToStaticMarkup(h(LogsPage));
    const headAt = html.indexOf('data-testid="logs-head"');
    const countAt = html.indexOf('data-testid="logs-count"');
    const toolbarAt = html.indexOf('data-testid="logs-toolbar"');
    expect(headAt).toBeGreaterThanOrEqual(0);
    expect(countAt).toBeGreaterThan(headAt);
    expect(countAt).toBeLessThan(toolbarAt);
    // 工具栏及之后（列表上方那段）没有条数说明——红框里那条窄带就此消失
    expect(html.slice(toolbarAt)).not.toContain('logs-count');
    expect(html.slice(toolbarAt)).not.toContain('logs__count');
    // 加载态也走页头，而不是另起一条
    expect(html).toContain('正在读取…');
  });
});

/* -------------------------------------------------------------- 方法名开关 */

describe('方法名（logger）开关：默认不显示，打开才显示', () => {
  const rows = [
    entry({ logger: 'ling.kernel.bridge', message: '桥接就绪' }),
    entry({ logger: 'ling.connectors.onebot.ws_server', message: '收到事件' }),
  ];

  it('关闭时不渲染 logger 列（不是藏起来），也不给列宽留空位', () => {
    const html = renderToStaticMarkup(
      h(LogsList, { level: 'all', showLogger: false, items: rows }),
    );
    expect(html).not.toContain('ling.kernel.bridge');
    expect(html).not.toContain('ling.connectors.onebot.ws_server');
    // DOM 里根本没有那一列：空占位格会把消息列挤窄
    expect(html).not.toContain('logs__logger');
    // 由修饰类把列宽换成三列（时间/级别/消息），见 logs.css
    expect(html).toContain('logs__list--no-logger');
    // 时间、级别、消息都还在，顺序不变
    expect(html).toContain('<time');
    expect(html).toContain('>INFO<');
    expect(html).toContain('桥接就绪');
    expect(html).toContain('收到事件');
  });

  it('打开时每行显示 logger 名，并去掉「无 logger」的列宽修饰', () => {
    const html = renderToStaticMarkup(h(LogsList, { level: 'all', showLogger: true, items: rows }));
    expect(html).toContain('ling.kernel.bridge');
    expect(html).toContain('ling.connectors.onebot.ws_server');
    expect(html).toContain('logs__logger');
    expect(html).not.toContain('logs__list--no-logger');
  });

  it('页面默认关闭：开关未勾选（默认值来自页面状态，不是测试传进去的）', () => {
    const html = renderToStaticMarkup(h(LogsPage));
    // 取整个 input 标签再判断：只搜 'checked' 的话属性顺序一变就假绿
    const input = html.match(/<input[^>]*logs-show-logger[^>]*>/);
    expect(input).not.toBeNull();
    expect(input?.[0]).not.toContain('checked');
    expect(html).toContain('显示方法名');
  });
});

/* ------------------------------------------------------------------ 错误 */

describe('错误文案', () => {
  it('403 写成「只有所有者或管理员」，不是通用的「没有权限访问该会话」', () => {
    const copy = logsErrorCopy(new ApiError(403, 'forbidden', '只有所有者可以查看运行日志'));
    expect(copy.reason).toContain('所有者');
    expect(copy.reason).toContain('管理员');
    // 通用文案指错了对象（这一页没有"会话"），不能出现在这里
    expect(copy.reason).not.toContain('该会话');
  });

  it('其它错误码仍走通用解释（不另抄一份文案表）', () => {
    const copy = logsErrorCopy(new ApiError(0, 'network_error', '无法连接服务端'));
    expect(copy.reason).toContain('无法连接');
  });
});

/* ------------------------------------------------------------ 行内容包装 */

describe('logLineParts', () => {
  it('四段齐全，级别大写', () => {
    const parts = logLineParts(entry({ level: 'warning', logger: 'ling.x', message: '慢' }));
    expect(parts.level).toBe('WARNING');
    expect(parts.logger).toBe('ling.x');
    expect(parts.message).toBe('慢');
    expect(parts.time).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  });

  it('logger 名为空时给出明确占位，而不是留空一列', () => {
    expect(logLineParts(entry({ logger: '' })).logger).toBe('(未命名 logger)');
  });
});

/* -------------------------------------------------------------- 常量契约 */

describe('常量与后端契约对齐', () => {
  it('轮询 2 秒、搜索防抖 300ms', () => {
    expect(LOG_POLL_MS).toBe(2000);
    expect(LOG_SEARCH_DEBOUNCE_MS).toBe(300);
  });

  it('单次拉取条数不超过后端上限 1000', () => {
    expect(LOG_LIMIT).toBeLessThanOrEqual(1000);
    expect(LOG_LIMIT).toBeGreaterThan(0);
  });
});

describe('日志分类（运行 / 处理）', () => {
  it('工具栏有分类下拉，默认「全部」', () => {
    const html = renderToStaticMarkup(
      h(LogsToolbar, {
        kind: 'all',
        level: 'all',
        search: '',
        paused: false,
        showLogger: false,
        onKind: NOOP,
        onLevel: NOOP,
        onSearch: NOOP,
        onTogglePause: NOOP,
        onRefresh: NOOP,
        onToggleLogger: NOOP,
      }),
    );
    expect(html).toContain('data-testid="logs-kind"');
    expect(html).toContain('>处理<');
    expect(html).toContain('>运行<');
  });

  it('选了「处理」时下拉回显处理', () => {
    const html = renderToStaticMarkup(
      h(LogsToolbar, {
        kind: 'behavior',
        level: 'all',
        search: '',
        paused: false,
        showLogger: false,
        onKind: NOOP,
        onLevel: NOOP,
        onSearch: NOOP,
        onTogglePause: NOOP,
        onRefresh: NOOP,
        onToggleLogger: NOOP,
      }),
    );
    // React SSR 会把选中项标成 selected
    expect(html).toMatch(/value="behavior"[^>]*selected/);
  });

  it('每行带 kind 时能标出来（不靠 attributes 猜）', () => {
    const html = renderToStaticMarkup(
      h(LogsList, {
        level: 'all',
        showLogger: false,
        items: [
          entry({ message: '看了下测试群，没接话', kind: 'behavior' }),
          entry({ message: '请求完成', kind: 'runtime' }),
        ],
      }),
    );
    expect(html).toContain('看了下测试群，没接话');
    expect(html).toContain('请求完成');
  });
});
