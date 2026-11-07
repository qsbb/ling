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
  DEFAULT_LOG_KIND,
  DEFAULT_LOG_SETTINGS,
  KIND_LABELS,
  LOGS_SETTINGS_PANEL_ID,
  LOG_LIMIT,
  LOG_POLL_MS,
  LOG_SEARCH_DEBOUNCE_MS,
  STICK_THRESHOLD_PX,
  LogsHeader,
  LogsList,
  LogsPage,
  LogsSettings,
  LogsToolbar,
  describeCounts,
  describeDenials,
  describeKindFilter,
  formatLogTime,
  formatLogTimestampFull,
  isDefaultLogSettings,
  isNearBottom,
  levelRank,
  levelTone,
  logLineParts,
  logsErrorCopy,
  logsRequestParams,
  settingsDismissAction,
  shouldAutoScroll,
  startLogPolling,
  toggleLogSetting,
  type LogSettings,
} from './components/LogsPage';
import { ApiError } from './api';
import { LEGACY_REDIRECTS, parseRoute, routePath } from './route';
import { activeNavId } from './components/Navigation';
import type { LogKindFilter, LogLevelFilter } from './components/LogsPage';
import type { LogEntry, LogsResponse } from './types';

const NOOP = () => undefined;

/** 工具栏的渲染助手：只关心被断言的那几项，其余用默认值。 */
function renderToolbar(
  overrides: {
    kind?: LogKindFilter;
    level?: LogLevelFilter;
    search?: string;
    paused?: boolean;
  } = {},
): string {
  return renderToStaticMarkup(
    h(LogsToolbar, {
      kind: overrides.kind ?? 'all',
      level: overrides.level ?? 'all',
      search: overrides.search ?? '',
      paused: overrides.paused ?? false,
      onKind: NOOP,
      onLevel: NOOP,
      onSearch: NOOP,
      onTogglePause: NOOP,
      onRefresh: NOOP,
    }),
  );
}

/** 设置面板的渲染助手（开/关、设置值由用例决定）。 */
function renderSettings(
  overrides: Partial<{
    open: boolean;
    settings: LogSettings;
  }> = {},
): string {
  return renderToStaticMarkup(
    h(LogsSettings, {
      // 默认渲染成"打开"——面板里的东西只有打开才看得见
      open: overrides.open ?? true,
      settings: overrides.settings ?? DEFAULT_LOG_SETTINGS,
      onOpenChange: NOOP,
      onToggle: NOOP,
      onReset: NOOP,
    }),
  );
}

/**
 * 取某个 `data-testid` 对应的 `<input …>` 开标签。
 *
 * 只搜 `'checked'` 会假绿（属性顺序一变就废），所以取整个标签再判断。
 */
function inputTag(html: string, testId: string): string {
  const match = html.match(new RegExp(`<input[^>]*data-testid="${testId}"[^>]*>`));
  return match?.[0] ?? '';
}

/**
 * 取某个下拉框的整段 `<select …>…</select>`。
 *
 * 断言"选中项"时必须限定在同一个下拉里：级别下拉也有 `value="all"`，
 * 直接在整页 HTML 上搜会搜到别的控件的 selected。
 */
function selectBlock(html: string, testId: string): string {
  const at = html.indexOf(`data-testid="${testId}"`);
  if (at < 0) return '';
  const open = html.lastIndexOf('<select', at);
  const end = html.indexOf('</select>', at);
  return open < 0 || end < 0 ? '' : html.slice(open, end + '</select>'.length);
}



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
  it('工具栏有分类/级别下拉、搜索框、暂停与刷新；方法名开关**不在**工具栏', () => {
    const html = renderToolbar();
    expect(html).toContain('data-testid="logs-level"');
    expect(html).toContain('data-testid="logs-search"');
    expect(html).toContain('data-testid="logs-pause"');
    expect(html).toContain('data-testid="logs-refresh"');
    // 显示方法名已经搬进右上角的设置面板：工具栏里再留一个入口就会有两份状态
    expect(html).not.toContain('data-testid="logs-show-logger"');
    expect(html).not.toContain('显示方法名');
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
    const html = renderToolbar({ search: 'x', paused: true });
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
    const html = renderToStaticMarkup(
      h(LogsHeader, { counts, kindHint: describeKindFilter('behavior') }),
    );
    expect(html).toContain('data-testid="logs-head"');
    expect(html).toContain('page__head');
    expect(html).toContain('日志');
    // 语义不在渲染层加工：describeCounts 说什么就显示什么
    expect(html).toContain('已显示 200 条');
    expect(html).toContain('缓冲已满');
    expect(html).toContain('更早的已被覆盖');
    // 当前分类也写在页头里：分类默认「处理」，不说明会被当成"日志少了"
    expect(html).toContain('data-testid="logs-kind-hint"');
    expect(html).toContain('处理');
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

  it('设置按钮在页头右侧（排在工具栏之前），不是工具栏里的一项', () => {
    const html = renderToStaticMarkup(h(LogsPage));
    const headAt = html.indexOf('data-testid="logs-head"');
    const actionsAt = html.indexOf('data-testid="logs-head-actions"');
    const toggleAt = html.indexOf('data-testid="logs-settings-toggle"');
    const toolbarAt = html.indexOf('data-testid="logs-toolbar"');
    expect(actionsAt).toBeGreaterThan(headAt);
    expect(toggleAt).toBeGreaterThan(actionsAt);
    expect(toggleAt).toBeLessThan(toolbarAt);
    expect(html).toContain('logs__head-actions');
    // 工具栏里没有第二个设置入口
    expect(html.slice(toolbarAt)).not.toContain('logs-settings-toggle');
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

  it('开关在设置面板里，且默认关（页面默认不渲染 logger 列）', () => {
    // 页面默认面板是关着的，所以页面上看不到这个开关——但更要紧的是它不能
    // 又出现在工具栏里（两处入口 = 两份状态，迟早不同步）
    const page = renderToStaticMarkup(h(LogsPage));
    expect(page).not.toContain('data-testid="logs-show-logger"');
    expect(renderToolbar()).not.toContain('logs-show-logger');
    // 默认值本身是"关"：列表按默认设置渲染时不带 logger 列
    expect(DEFAULT_LOG_SETTINGS.showLogger).toBe(false);
    expect(
      renderToStaticMarkup(
        h(LogsList, { level: 'all', showLogger: DEFAULT_LOG_SETTINGS.showLogger, items: rows }),
      ),
    ).toContain('logs__list--no-logger');

    // 打开设置面板：开关在里面，且默认未勾选（默认值来自 DEFAULT_LOG_SETTINGS，
    // 不是测试传进去的）
    const panel = renderSettings();
    const input = inputTag(panel, 'logs-show-logger');
    expect(input).not.toBe('');
    expect(input).not.toContain('checked');
    expect(panel).toContain('显示方法名');
  });

  it('面板里勾上时是 checked（状态真的从设置对象来）', () => {
    const panel = renderSettings({ settings: { ...DEFAULT_LOG_SETTINGS, showLogger: true } });
    expect(inputTag(panel, 'logs-show-logger')).toContain('checked');
    // 列表侧同步：同一份 settings 打开后就渲染 logger 列
    const html = renderToStaticMarkup(
      h(LogsList, { level: 'all', showLogger: true, showMillis: true, items: rows }),
    );
    expect(html).toContain('ling.kernel.bridge');
    expect(html).not.toContain('logs__list--no-logger');
  });
});

/* -------------------------------------------------------------- 默认分类 */

describe('分类初值是「处理」', () => {
  it('DEFAULT_LOG_KIND 是 behavior，不是 all', () => {
    expect(DEFAULT_LOG_KIND).toBe('behavior');
    expect(KIND_LABELS.behavior).toBe('处理');
  });

  it('页面一进来分类下拉就选中「处理」（不是「全部」）', () => {
    const kindSelect = selectBlock(renderToStaticMarkup(h(LogsPage)), 'logs-kind');
    // React SSR 把选中项标成 selected；「全部」不能被选中
    expect(kindSelect).toContain('value="behavior" selected');
    expect(kindSelect).not.toContain('value="all" selected');
    // 级别下拉仍是「全部」——分类的默认值不该顺带改掉别的筛选
    expect(selectBlock(renderToStaticMarkup(h(LogsPage)), 'logs-level')).toContain(
      'value="all" selected',
    );
  });

  it('页头写明当前只看处理过程（否则条数变少会被当成日志丢了）', () => {
    const html = renderToStaticMarkup(h(LogsPage));
    const hint = describeKindFilter(DEFAULT_LOG_KIND);
    expect(html).toContain('data-testid="logs-kind-hint"');
    expect(html).toContain(hint);
    expect(hint).toContain('处理过程');
    // 切到「全部」时不再说"只看处理"，而是明说当前是全部
    expect(describeKindFilter('all')).toContain('全部');
    expect(describeKindFilter('all')).not.toContain('只看');
    expect(describeKindFilter('runtime')).toContain('运行');
  });

  it('切换分类后请求参数跟着变（behavior → runtime → 全部是 null）', () => {
    expect(logsRequestParams('behavior', 'all', '')).toEqual({
      limit: LOG_LIMIT,
      kind: 'behavior',
      level: null,
      q: null,
    });
    expect(logsRequestParams('runtime', 'all', '').kind).toBe('runtime');
    // 「全部」必须翻译成 null，不能是空串：空串是"筛一个空分类"，结果恒为空
    expect(logsRequestParams('all', 'all', '').kind).toBeNull();
    // 级别与搜索词同样如此（顺带确认搜索词 trim）
    expect(logsRequestParams('all', 'error', '  超时  ')).toEqual({
      limit: LOG_LIMIT,
      kind: null,
      level: 'error',
      q: '超时',
    });
  });

  it('切换后的下拉回显（工具栏不自己假设默认值）', () => {
    expect(selectBlock(renderToolbar({ kind: 'behavior' }), 'logs-kind')).toContain(
      'value="behavior" selected',
    );
    expect(selectBlock(renderToolbar({ kind: 'runtime' }), 'logs-kind')).toContain(
      'value="runtime" selected',
    );
    expect(selectBlock(renderToolbar({ kind: 'all' }), 'logs-kind')).toContain(
      'value="all" selected',
    );
  });

  it('空列表先说是分类筛掉的，而不是"后端没日志"', () => {
    const html = renderToStaticMarkup(
      h(LogsList, { level: 'all', kind: 'behavior', showLogger: false, items: [] }),
    );
    expect(html).toContain('分类筛选');
    expect(html).toContain('处理');
    // 没有分类筛选时才说"服务刚启动"
    const plain = renderToStaticMarkup(h(LogsList, { level: 'all', showLogger: false, items: [] }));
    expect(plain).toContain('服务刚启动');
  });
});

/* -------------------------------------------------------------- 设置面板 */

describe('设置面板（右上角）', () => {
  it('默认值：方法名关、自动跟到最新开、毫秒开', () => {
    expect(DEFAULT_LOG_SETTINGS).toEqual({
      showLogger: false,
      followLatest: true,
      showMillis: true,
    });
    expect(isDefaultLogSettings(DEFAULT_LOG_SETTINGS)).toBe(true);
  });

  it('关闭时不渲染面板，但按钮报 aria-expanded="false"', () => {
    const html = renderSettings({ open: false });
    expect(html).toContain('data-testid="logs-settings-toggle"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('data-testid="logs-settings-panel"');
    // 面板里的开关一个都不在（关掉就是关掉，不是藏起来）
    expect(html).not.toContain('logs-show-logger');
    expect(html).toContain('设置');
  });

  it('打开时按钮报 aria-expanded="true"，面板带 role=dialog 与 aria-controls 指向的 id', () => {
    const html = renderSettings({ open: true });
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain(`aria-controls="${LOGS_SETTINGS_PANEL_ID}"`);
    expect(html).toContain('aria-haspopup="dialog"');
    expect(html).toContain('data-testid="logs-settings-panel"');
    expect(html).toContain(`id="${LOGS_SETTINGS_PANEL_ID}"`);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-label="日志设置"');
  });

  it('面板里有三项设置 + 恢复默认，顺序固定', () => {
    const html = renderSettings();
    const loggerAt = html.indexOf('data-testid="logs-show-logger"');
    const followAt = html.indexOf('data-testid="logs-follow-latest"');
    const millisAt = html.indexOf('data-testid="logs-show-millis"');
    const resetAt = html.indexOf('data-testid="logs-settings-reset"');
    expect(loggerAt).toBeGreaterThan(-1);
    expect(followAt).toBeGreaterThan(loggerAt);
    expect(millisAt).toBeGreaterThan(followAt);
    expect(resetAt).toBeGreaterThan(millisAt);
    expect(html).toContain('显示方法名');
    expect(html).toContain('自动跟到最新');
    expect(html).toContain('时间显示毫秒');
    expect(html).toContain('恢复默认');
    // 默认状态下两个"开"的开关是勾上的
    expect(inputTag(html, 'logs-follow-latest')).toContain('checked');
    expect(inputTag(html, 'logs-show-millis')).toContain('checked');
  });

  it('「恢复默认」在已是默认值时置灰（点了没反应比置灰更糟）', () => {
    expect(renderSettings()).toMatch(/data-testid="logs-settings-reset"[^>]*disabled/);
    const changed = renderSettings({
      settings: { showLogger: true, followLatest: false, showMillis: false },
    });
    expect(changed).not.toMatch(/data-testid="logs-settings-reset"[^>]*disabled/);
  });

  it('toggleLogSetting 只翻一项，其余不动', () => {
    const once = toggleLogSetting(DEFAULT_LOG_SETTINGS, 'showLogger');
    expect(once).toEqual({ showLogger: true, followLatest: true, showMillis: true });
    expect(isDefaultLogSettings(once)).toBe(false);
    // 翻两次回到默认
    expect(toggleLogSetting(once, 'showLogger')).toEqual(DEFAULT_LOG_SETTINGS);
    // 原对象不被改（不可变更新）
    expect(DEFAULT_LOG_SETTINGS.showLogger).toBe(false);
    expect(toggleLogSetting(DEFAULT_LOG_SETTINGS, 'followLatest').followLatest).toBe(false);
    expect(toggleLogSetting(DEFAULT_LOG_SETTINGS, 'showMillis').showMillis).toBe(false);
  });

  it('Esc 无条件关面板；点面板外才关（点里面、别的键都不关）', () => {
    expect(settingsDismissAction({ type: 'keydown', key: 'Escape', inside: false })).toBe(true);
    // 焦点还在按钮上（inside=false）或已进面板（inside=true）都要关
    expect(settingsDismissAction({ type: 'keydown', key: 'Escape', inside: true })).toBe(true);
    // 别的键不关：Tab / 方向键在面板里还要用
    for (const key of ['Tab', 'Enter', 'ArrowDown', 'a']) {
      expect(settingsDismissAction({ type: 'keydown', key, inside: true })).toBe(false);
    }
    // 点面板外关，点里面不关
    expect(settingsDismissAction({ type: 'mousedown', inside: false })).toBe(true);
    expect(settingsDismissAction({ type: 'mousedown', inside: true })).toBe(false);
    // 触摸设备走 touchstart，语义与 mousedown 一致
    expect(settingsDismissAction({ type: 'touchstart', inside: false })).toBe(true);
    expect(settingsDismissAction({ type: 'touchstart', inside: true })).toBe(false);
    // 不相干的事件不理
    expect(settingsDismissAction({ type: 'click', inside: false })).toBe(false);
  });
});

/* ------------------------------------------------------------ 自动跟到最新 */

describe('自动跟到最新 / 时间毫秒：开关真的改变行为', () => {
  it('默认开着；只有"开着且本来就贴底"才自动滚', () => {
    expect(DEFAULT_LOG_SETTINGS.followLatest).toBe(true);
    expect(shouldAutoScroll(true, true)).toBe(true);
    // 用户往上翻着读报错：不能被新日志拽回底部
    expect(shouldAutoScroll(true, false)).toBe(false);
    // 关掉开关：即使本来就贴底也不再自动滚
    expect(shouldAutoScroll(false, true)).toBe(false);
    expect(shouldAutoScroll(false, false)).toBe(false);
  });

  it('毫秒关掉时时间只到秒，打开时带三位毫秒', () => {
    const timestamp = '2026-11-05T01:02:03.456+00:00';
    expect(formatLogTime(timestamp)).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    expect(formatLogTime(timestamp, true).endsWith('.456')).toBe(true);
    const secondOnly = formatLogTime(timestamp, false);
    expect(secondOnly).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(secondOnly).not.toContain('.');
    // 解析不了的仍然原样返回（关毫秒不是"编一个 00"）
    expect(formatLogTime('不是时间', false)).toBe('不是时间');
  });

  it('列表按 showMillis 渲染时间，悬停的完整时间始终带毫秒', () => {
    const withMillis = renderToStaticMarkup(
      h(LogsList, { level: 'all', showLogger: false, showMillis: true, items: [entry()] }),
    );
    expect(withMillis).toMatch(/>\d{2}:\d{2}:\d{2}\.\d{3}</);
    const noMillis = renderToStaticMarkup(
      h(LogsList, { level: 'all', showLogger: false, showMillis: false, items: [entry()] }),
    );
    expect(noMillis).toMatch(/>\d{2}:\d{2}:\d{2}</);
    expect(noMillis).not.toMatch(/>\d{2}:\d{2}:\d{2}\.\d{3}</);
    // title 上是完整时间，关掉毫秒不影响它（还能查同秒先后）
    expect(noMillis).toMatch(/title="2026-11-0[45] \d{2}:\d{2}:\d{2}\.\d{3}"/);
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

describe('日志分类的选项与行内容', () => {
  it('工具栏的分类下拉里五个选项齐全（少一个就有一个分类用不了）', () => {
    const html = renderToolbar();
    expect(html).toContain('data-testid="logs-kind"');
    for (const [value, label] of Object.entries(KIND_LABELS)) {
      expect(html).toContain(`value="${value}"`);
      expect(html).toContain(`>${label}<`);
    }
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
