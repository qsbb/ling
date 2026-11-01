/**
 * 外壳（左导航 + 抽屉 + 浮层管理）的契约测试。
 *
 * 与 `dashboard.test.ts` 的分工：那边验证「路由 → 页面」和总览页本体，
 * 这边只验证**导航本身**——顺序、当前项、折叠持久化、浮层互斥、高亮归属。
 *
 * 不引入 DOM 测试库（仓库没有 jsdom 依赖，`about.test.ts` / `dashboard.test.ts`
 * 是同样的取舍）：用 `react-dom/server` 渲染字符串做结构断言；
 * 真正的 portal / 焦点陷阱 / 滚动锁 / 44×44 交给 `tests/e2e/nav_browser.mjs`。
 * `react-dom/server` **不支持 portal**，所以抽屉只渲染它的面板
 * （`NavDrawerPanel`）——真实抽屉就是「portal + scrim + 这个面板」。
 */
import { readFileSync } from 'node:fs';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AppShell } from './App';
import {
  NavDrawerPanel,
  PRIMARY_NAV,
  SETTINGS_NAV,
  Sidenav,
  VersionBadge,
  activeNavId,
  type NavSectionId,
} from './components/Navigation';
import { LEGACY_REDIRECTS, parseRoute, type Route } from './route';
import {
  NAV_EXPANDED_KEY,
  overlayReducer,
  readNavExpanded,
  writeNavExpanded,
  type OverlayId,
} from './shell';
import type { UserInfo } from './types';

const USER: UserInfo = {
  id: 'user_test',
  username: 'ling',
  role: 'owner',
  subject_id: 'subj_test',
  display_name: 'Ling',
  must_change_password: false,
};

const NOOP = () => undefined;

/** 用户拍板的顺序：总览（落地）→ 对话 → 记忆 → 人设 → 生活，底部设置 + 版本号。 */
const EXPECTED_ORDER = [
  'nav-dashboard',
  'nav-chat',
  'nav-memory',
  'nav-persona',
  'nav-life',
  'nav-settings',
  'nav-version',
];

const EXPECTED_LABELS = ['总览', '对话', '记忆', '人设', '生活', '设置'];

function renderShell(route: Route): string {
  return renderToStaticMarkup(
    h(AppShell, { route, user: USER, testMode: false, onLogout: NOOP, onTestMode: NOOP, onUserChange: NOOP }),
  );
}

function renderSidenav(expanded: boolean, active: NavSectionId | null = 'dashboard', version: string | null = '0.1.0'): string {
  return renderToStaticMarkup(
    h(Sidenav, { active, expanded, version, onToggle: NOOP, onGo: NOOP }),
  );
}

/** 抽屉面板的固定 props；账号区（`nav-account` / `nav-logout`）是窄屏的账号归宿。 */
const DRAWER_PROPS = {
  active: 'memory',
  version: '0.1.0',
  accountName: 'Ling',
  onClose: NOOP,
  onGo: NOOP,
  onLogout: NOOP,
} as const;

function renderDrawerPanel(): string {
  return renderToStaticMarkup(h(NavDrawerPanel, DRAWER_PROPS));
}

/** 取出 `<nav aria-label="主导航">…</nav>` 这一段（取不到直接失败）。 */
function navigationRegion(html: string): string {
  const landmark = html.indexOf('aria-label="主导航"');
  expect(landmark, '找不到 aria-label="主导航" 的导航区').toBeGreaterThanOrEqual(0);
  const start = html.lastIndexOf('<nav', landmark);
  const end = html.indexOf('</nav>', start);
  expect(start, '导航区没有起点').toBeGreaterThanOrEqual(0);
  expect(end, '导航区没有闭合').toBeGreaterThan(start);
  return html.slice(start, end + '</nav>'.length);
}

function testidsIn(region: string): string[] {
  return [...region.matchAll(/data-testid="([^"]+)"/g)].map((match) => match[1] as string);
}

function labelsIn(region: string): string[] {
  return [...region.matchAll(/<span class="sidenav__label">([^<]+)<\/span>/g)].map(
    (match) => match[1] as string,
  );
}

function activeTestId(html: string): string | null {
  const current = html.indexOf('aria-current="page"');
  if (current < 0) return null;
  const at = html.lastIndexOf('data-testid="', current);
  if (at < 0) return null;
  const end = html.indexOf('"', at + 'data-testid="'.length);
  return html.slice(at + 'data-testid="'.length, end);
}

/* ------------------------------------------------------------------ 顺序 */

describe('一级导航的顺序（用户拍板的 A 方案）', () => {
  it('左导航按「总览 → 对话 → 记忆 → 人设 → 生活 → 设置 → 版本」排列', () => {
    // **断顺序**，不是「都在」：`toEqual` 对数组元素与位置同时生效，
    // 把「记忆」挪到「人设」后面、或把「设置」提到列表里，这条都会红。
    expect(testidsIn(navigationRegion(renderShell({ name: 'dashboard' })))).toEqual(EXPECTED_ORDER);
  });

  it('导航文字的顺序同样是契约（testid 对但文字错也算错）', () => {
    expect(labelsIn(navigationRegion(renderShell({ name: 'dashboard' })))).toEqual(EXPECTED_LABELS);
  });

  it('设置与版本号固定在底部（不在五项内容导航里面）', () => {
    const region = navigationRegion(renderShell({ name: 'dashboard' }));
    const listEnd = region.indexOf('sidenav__foot');
    expect(listEnd, '找不到底部区域').toBeGreaterThanOrEqual(0);
    // 五项内容项都在底部区域之前，设置与版本在之后
    for (const item of PRIMARY_NAV) {
      expect(region.indexOf(`data-testid="${item.testid}"`)).toBeLessThan(listEnd);
    }
    expect(region.indexOf(`data-testid="${SETTINGS_NAV.testid}"`)).toBeGreaterThan(listEnd);
    expect(region.indexOf('data-testid="nav-version"')).toBeGreaterThan(listEnd);
  });

  it('只有一份主导航 landmark，且顶栏不再有「快捷导航」', () => {
    const html = renderShell({ name: 'dashboard' });
    expect(html.match(/aria-label="主导航"/g) ?? []).toHaveLength(1);
    // Step 2：顶栏那排过渡入口（总览/设置/生活/角色/退出）已删除。
    // 它们与左导航逐项重复，而左导航是「当前页」的唯一权威来源。
    expect(html).not.toContain('aria-label="快捷导航"');
    expect(html).not.toContain('data-testid="quick-');
  });

  it('版本号 badge 指向「关于与维护」，读不到版本时不编数字', () => {
    const withVersion = renderToStaticMarkup(h(VersionBadge, { version: '0.1.0', onGo: NOOP }));
    expect(withVersion).toContain('v0.1.0');
    const withoutVersion = renderToStaticMarkup(h(VersionBadge, { version: null, onGo: NOOP }));
    expect(withoutVersion).toContain('v—');
    expect(withoutVersion).not.toContain('v0.');
  });
});

/* --------------------------------------------------------------- 当前项 */

/**
 * 每条地址的「当前项」期望**逐条手写**，不从实现推导。
 *
 * 这里把三个旧地址也列进来：它们的期望是**目标页**的高亮，不是「设置」。
 * 重定向是 `replace`，人最后停在目标页；中间那一帧若把「设置」点亮，
 * 用户会看到「设置」闪一下再跳到「记忆」——把中间态画出来就是错的。
 */
const ALL_ROUTES: Array<{ route: Route; label: string; highlight: string | null }> = [
  { route: { name: 'dashboard' }, label: '/dashboard', highlight: 'nav-dashboard' },
  { route: { name: 'chat', sessionId: null }, label: '/', highlight: 'nav-chat' },
  { route: { name: 'chat', sessionId: 'sess-1' }, label: '/s/sess-1', highlight: 'nav-chat' },
  { route: { name: 'memory' }, label: '/memory', highlight: 'nav-memory' },
  { route: { name: 'persona' }, label: '/persona', highlight: 'nav-persona' },
  { route: { name: 'now' }, label: '/life', highlight: 'nav-life' },
  { route: { name: 'settings' }, label: '/settings', highlight: 'nav-settings' },
  { route: { name: 'model' }, label: '/settings/model', highlight: 'nav-settings' },
  { route: { name: 'privacy' }, label: '/settings/privacy', highlight: 'nav-settings' },
  { route: { name: 'identity' }, label: '/settings/identity', highlight: 'nav-settings' },
  { route: { name: 'account' }, label: '/settings/account', highlight: 'nav-settings' },
  { route: { name: 'about' }, label: '/settings/about', highlight: 'nav-settings' },
  {
    route: { name: 'legacyRedirect', to: '/memory?tab=settings' },
    label: '/settings/memory（旧）',
    highlight: 'nav-memory',
  },
  {
    route: { name: 'legacyRedirect', to: '/persona?tab=dialogue' },
    label: '/settings/dialogue（旧）',
    highlight: 'nav-persona',
  },
  {
    route: { name: 'legacyRedirect', to: '/life?tab=rhythm' },
    label: '/settings/life（旧）',
    highlight: 'nav-life',
  },
  { route: { name: 'notFound' }, label: '/qa-no-such-route', highlight: null },
];

describe('当前项：aria-current="page" 唯一且指对地方', () => {
  it.each(ALL_ROUTES)('$label 只有一个当前项，且是预期的那一个', ({ route, highlight }) => {
    const html = renderShell(route);
    const marks = html.match(/aria-current="page"/g) ?? [];
    if (highlight === null) {
      expect(marks, '不存在的地址不该有当前项').toHaveLength(0);
      return;
    }
    expect(marks, `${route.name} 的当前项必须恰好一个`).toHaveLength(1);
    expect(activeTestId(html)).toBe(highlight);
  });

  it('brand testid 唯一：brand-home 与左导航的 nav-chat 不再重名，也不冒充当前页', () => {
    const html = renderShell({ name: 'account' });
    // 页头 logo：唯一一个 brand-home
    expect(html.match(/data-testid="brand-home"/g) ?? []).toHaveLength(1);
    // nav-chat 只属于左导航「对话」这一项（QA-TASK102 L2：原先 brand 与它重名）
    expect(html.match(/data-testid="nav-chat"/g) ?? []).toHaveLength(1);
    expect(navigationRegion(html)).toContain('data-testid="nav-chat"');
    const headerStart = html.indexOf('<header');
    const header = html.slice(headerStart, html.indexOf('</header>', headerStart));
    expect(header).not.toContain('data-testid="nav-chat"');
    // brand 仍然没有 aria-current：当前页在左导航的「设置」上
    const brandAt = html.indexOf('data-testid="brand-home"');
    expect(brandAt, '顶栏 brand 丢了（E2E 依赖它回聊天）').toBeGreaterThanOrEqual(0);
    const brand = html.slice(brandAt, html.indexOf('</button>', brandAt));
    expect(brand).not.toContain('aria-current');
    expect(activeTestId(html)).toBe('nav-settings');
  });

  it('/s/:id 高亮「对话」，而不是把会话当成新的一页', () => {
    expect(activeNavId(parseRoute('/s/abc123'))).toBe('chat');
    expect(activeNavId(parseRoute('/'))).toBe('chat');
    expect(activeTestId(renderShell(parseRoute('/s/abc123')))).toBe('nav-chat');
  });

  it('/settings/* 全部高亮「设置」（子页不再是无归属页，SHOTS_REPORT §5.7）', () => {
    // Step 4 之后 /settings 下只剩这些**留在设置里**的子页：
    // 记忆/对话/生活三条旧地址已迁出，它们是重定向，不再属于这个前缀。
    const settingsPaths = [
      '/settings',
      '/settings/model',
      '/settings/privacy',
      '/settings/identity',
      '/settings/account',
      '/settings/about',
    ];
    for (const path of settingsPaths) {
      const route = parseRoute(path);
      expect(activeNavId(route), `${path} 的高亮项不对`).toBe('settings');
      expect(activeTestId(renderShell(route)), `${path} 的 aria-current 不对`).toBe('nav-settings');
    }
  });

  it('旧地址的高亮按**目标页**算，不按地址栏前缀（重定向不闪「设置」）', () => {
    // `/settings/memory` 的首帧还挂在 /settings 前缀下，但它会在同一个 tick
    // 里被 replace 到 /memory?tab=settings。高亮必须已经是「记忆」，
    // 否则用户会看到「设置」亮一下再跳到「记忆」。
    expect(activeNavId(parseRoute('/settings/memory'))).toBe('memory');
    expect(activeNavId(parseRoute('/settings/dialogue'))).toBe('persona');
    expect(activeNavId(parseRoute('/settings/life'))).toBe('life');
  });

  it('三个旧地址：重定向目标与高亮一一对应（表是唯一权威）', () => {
    for (const [from, to] of Object.entries(LEGACY_REDIRECTS)) {
      const route = parseRoute(from);
      expect(route, `${from} 没有命中重定向表`).toEqual({ name: 'legacyRedirect', to });
      const target = parseRoute(to.split('?')[0] ?? '/');
      // 高亮与「真的跳到目标页之后」完全一致
      expect(activeNavId(route), `${from} 的高亮与目标页不一致`).toBe(activeNavId(target));
      expect(activeNavId(target), `${to} 的目标页没有高亮项`).not.toBeNull();
    }
  });
});

/* ------------------------------------------------------ 旧地址重定向表（逐条） */

/**
 * Step 4 的 `LEGACY_REDIRECTS`：**逐条**各一个用例。
 *
 * 上面那条「三个旧地址：重定向目标与高亮一一对应」是**遍历表**写的，
 * 表里删掉一条它只会少跑一圈、仍然全绿。这里逐条手写：删掉表里任何一条，
 * 对应用例必须变红（变异测试见 `shots/ia-research/TASK104_REPORT.md`）。
 *
 * 表外路径同理：`/settings/unknown` 必须 `notFound`。若把精确表换成
 * `/settings/*` 通配重定向，这条会立刻红——通配会把拼错的设置子路径
 * 也悄悄导到一个能用的页面，`notFound` 要暴露的东西就再也暴露不出来。
 */
describe('旧地址重定向表：三条精确命中，表外一律 notFound', () => {
  it('/settings/memory → /memory?tab=settings（记忆与学习回到记忆页）', () => {
    expect(parseRoute('/settings/memory')).toEqual({
      name: 'legacyRedirect',
      to: '/memory?tab=settings',
    });
  });

  it('/settings/dialogue → /persona?tab=dialogue（对话与主动性回到人设页）', () => {
    expect(parseRoute('/settings/dialogue')).toEqual({
      name: 'legacyRedirect',
      to: '/persona?tab=dialogue',
    });
  });

  it('/settings/life → /life?tab=rhythm（生活节奏回到生活页）', () => {
    expect(parseRoute('/settings/life')).toEqual({
      name: 'legacyRedirect',
      to: '/life?tab=rhythm',
    });
  });

  it('表外路径一律 notFound（重定向表不许变成通配）', () => {
    // 拼错的旧地址：看起来像迁移过的，但表里没有它
    expect(parseRoute('/settings/unknown')).toEqual({ name: 'notFound' });
    // 旧地址后面多一段：不是表里的精确键
    expect(parseRoute('/settings/memory/extra')).toEqual({ name: 'notFound' });
    // 表外但没有通配兜底的兄弟路径
    expect(parseRoute('/settings/policy')).toEqual({ name: 'notFound' });
    expect(parseRoute('/settings/memroy')).toEqual({ name: 'notFound' });
    // 反证：通配「/settings/* → /settings」的写法会让上面几条全都变成 settings
    expect(parseRoute('/settings/unknown')).not.toEqual({ name: 'settings' });
  });
});

/* ------------------------------------------------------------ 折叠持久化 */

describe('桌面侧栏的折叠状态（56px ↔ 200px）', () => {
  function memoryStorage(initial: Record<string, string> = {}) {
    const map = new Map(Object.entries(initial));
    return {
      getItem: (key: string) => map.get(key) ?? null,
      setItem: (key: string, value: string) => {
        map.set(key, value);
      },
      read: (key: string) => map.get(key) ?? null,
    };
  }

  it('默认展开（第一次来的人看到的是带文字的导航）', () => {
    expect(readNavExpanded(memoryStorage())).toBe(true);
    expect(readNavExpanded(null)).toBe(true);
    expect(renderShell({ name: 'dashboard' })).toContain('data-expanded="true"');
  });

  it('折叠后写回 localStorage，重新读取仍是折叠', () => {
    const storage = memoryStorage();
    writeNavExpanded(storage, false);
    expect(storage.read(NAV_EXPANDED_KEY)).toBe('0');
    expect(readNavExpanded(storage)).toBe(false);
    writeNavExpanded(storage, true);
    expect(readNavExpanded(storage)).toBe(true);
  });

  it('存过的值就是唯一事实：不是「每次启动都重置成展开」', () => {
    expect(readNavExpanded(memoryStorage({ [NAV_EXPANDED_KEY]: '0' }))).toBe(false);
    expect(readNavExpanded(memoryStorage({ [NAV_EXPANDED_KEY]: '1' }))).toBe(true);
    // 脏数据（被人手改坏/旧版本格式）按默认展开，不要因为一个坏值把导航藏起来
    expect(readNavExpanded(memoryStorage({ [NAV_EXPANDED_KEY]: 'yes' }))).toBe(true);
  });

  it('存储不可用时不抛异常（隐私模式下页面照样能起）', () => {
    const throwing = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
    };
    expect(readNavExpanded(throwing)).toBe(true);
    expect(() => writeNavExpanded(throwing, false)).not.toThrow();
  });

  it('折叠态只藏视觉、不藏可访问名字（读屏仍读得到「记忆」）', () => {
    const rail = renderSidenav(false);
    expect(rail).toContain('data-expanded="false"');
    // 文字节点还在 DOM 里（CSS 做 visual hidden，而不是 display:none）
    expect(rail).toContain('<span class="sidenav__label">记忆</span>');
    const expanded = renderSidenav(true);
    expect(expanded).toContain('data-expanded="true"');
  });

  it('折叠按钮自己会报出状态（aria-expanded）', () => {
    expect(renderSidenav(true)).toContain('aria-expanded="true"');
    expect(renderSidenav(false)).toContain('aria-expanded="false"');
    // 展开态点它是「收起」，折叠态点它是「展开」
    expect(renderSidenav(true)).toContain('aria-label="收起导航"');
    expect(renderSidenav(false)).toContain('aria-label="展开导航"');
  });
});

/* ------------------------------------------------------- 抽屉与浮层互斥 */

describe('抽屉开关与浮层互斥（导航 vs 会话列表）', () => {
  it('打开一片会顶掉另一片：同一个槽位里只会有一个', () => {
    let state: OverlayId | null = null;
    state = overlayReducer(state, { type: 'open', overlay: 'nav' });
    expect(state).toBe('nav');
    // 会话列表抢走槽位 → 导航必须已经关了（否则用户看到两层遮罩）
    state = overlayReducer(state, { type: 'open', overlay: 'session' });
    expect(state).toBe('session');
    state = overlayReducer(state, { type: 'open', overlay: 'nav' });
    expect(state).toBe('nav');
    state = overlayReducer(state, { type: 'close' });
    expect(state).toBeNull();
  });

  it('toggle 只在点到自己时才关，点另一片是切换过去', () => {
    expect(overlayReducer(null, { type: 'toggle', overlay: 'nav' })).toBe('nav');
    expect(overlayReducer('nav', { type: 'toggle', overlay: 'nav' })).toBeNull();
    expect(overlayReducer('session', { type: 'toggle', overlay: 'nav' })).toBe('nav');
    // 关一个已经关掉的槽位不会凭空打开什么
    expect(overlayReducer(null, { type: 'close' })).toBeNull();
  });

  it('抽屉面板是一个带名字的模态对话框，有关闭入口与同一套导航', () => {
    const panel = renderDrawerPanel();
    expect(panel).toContain('role="dialog"');
    expect(panel).toContain('aria-modal="true"');
    expect(panel).toContain('aria-label="导航"');
    // 关掉它的三个入口：关闭按钮（Escape / scrim 在 E2E 里测）
    expect(panel).toContain('data-testid="nav-close"');
    expect(panel).toContain('aria-label="关闭导航"');
    // 抽屉里的顺序和桌面 rail 完全一致（不是另一套导航）
    expect(testidsIn(navigationRegion(panel))).toEqual(EXPECTED_ORDER);
    // 当前页带过来，不会因为在抽屉里就丢掉高亮
    expect(activeTestId(panel)).toBe('nav-memory');
  });

  it('抽屉底部有账号区：用户名 + 退出登录（nav-account / nav-logout）', () => {
    const panel = renderDrawerPanel();
    const ids = testidsIn(panel);
    expect(ids).toContain('nav-account');
    expect(ids).toContain('nav-logout');
    const accountAt = panel.indexOf('data-testid="nav-account"');
    const account = panel.slice(accountAt, panel.indexOf('</aside>', accountAt));
    expect(account).toContain('Ling');
    expect(account).toContain('退出登录');
    // 账号区在「设置 + 版本号」之后：抽屉底部（不是塞进五项内容导航里）
    expect(panel.indexOf('data-testid="nav-version"')).toBeLessThan(accountAt);
    // 它是动作与身份，不是导航目的地：放在主导航 landmark 之外。
    expect(navigationRegion(panel)).not.toContain('nav-account');
    expect(panel).toContain('role="group"');
    expect(panel).toContain('aria-label="账号"');
  });

  it('nav-logout 是可聚焦的普通按钮（不是 tabindex=-1 / disabled 的假入口）', () => {
    const panel = renderDrawerPanel();
    const at = panel.indexOf('data-testid="nav-logout"');
    expect(at, '抽屉里没有 nav-logout').toBeGreaterThanOrEqual(0);
    const start = panel.lastIndexOf('<button', at);
    const button = panel.slice(start, panel.indexOf('</button>', at));
    expect(button).toContain('type="button"');
    // 焦点陷阱的 FOCUSABLE 选择器只收 button:not([disabled])，
    // 且会过滤掉 tabindex="-1"（scrim 就是那么排除的）。
    expect(button).not.toContain('disabled');
    expect(button).not.toContain('tabindex="-1"');
    expect(button).toContain('退出登录');
    // 顶栏那份在窄屏不渲染，所以 DOM 里不会有两个 nav-logout
    expect(panel.match(/data-testid="nav-logout"/g) ?? []).toHaveLength(1);
  });
});

/* ------------------------------------------------------------- 结构不变式 */

describe('外壳结构不变式', () => {
  it('左导航在 <main> 之外，且不与主区共用容器', () => {
    const html = renderShell({ name: 'dashboard' });
    const mainStart = html.indexOf('<main');
    const mainEnd = html.indexOf('</main>', mainStart);
    const main = html.slice(mainStart, mainEnd);
    expect(main).toContain('class="app-main"');
    // 侧栏不混进主区
    expect(main).not.toContain('class="sidenav"');
    expect(main).not.toContain('aria-label="主导航"');
    // 侧栏在主区之前（两列布局：左栏先出现）
    expect(html.indexOf('class="sidenav"')).toBeLessThan(mainStart);
  });

  it('窄屏才渲染「打开导航」与抽屉：桌面不出现重复的导航源', () => {
    const html = renderShell({ name: 'dashboard' });
    // 服务端渲染没有 window，按桌面形态渲染
    expect(html).not.toContain('data-testid="nav-open"');
    expect(html).not.toContain('nav-drawer');
  });

  it('顶栏只剩 brand + 账号区：没有 quick-*，旧入口在左导航里一个不少', () => {
    const html = renderShell({ name: 'dashboard' });
    const headerStart = html.indexOf('<header');
    const header = html.slice(headerStart, html.indexOf('</header>', headerStart));
    // 桌面账号区：用户名 + 退出
    expect(header).toContain('data-testid="brand-home"');
    expect(header).toContain('app-header__user');
    expect(header).toContain('data-testid="nav-logout"');
    expect(header).toContain('Ling');
    // Step 2 删除的四个过渡入口：不能以任何形式回到页头
    expect(header).not.toContain('data-testid="quick-');
    expect(header).not.toContain('aria-label="快捷导航"');
    // 「没有任何入口丢失」：原顶栏入口的目的地都由左导航覆盖
    const nav = navigationRegion(html);
    for (const testid of ['nav-dashboard', 'nav-settings', 'nav-life', 'nav-persona']) {
      expect(nav, `左导航没有覆盖 ${testid}`).toContain(`data-testid="${testid}"`);
    }
    // 窄屏账号区在抽屉里；桌面不渲染抽屉，所以不会出现第二个 nav-logout
    expect(html).not.toContain('data-testid="nav-account"');
    expect(html.match(/data-testid="nav-logout"/g) ?? []).toHaveLength(1);
  });

  it('>900px 真正隐藏聊天页的 ≡（L1 的 CSS 契约，浏览器行为由 E2E 复验）', () => {
    const css = readFileSync(new URL('./nav.css', import.meta.url), 'utf8');
    const block = /@media\s*\(min-width:\s*901px\)\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? '';
    expect(block, 'nav.css 缺少 >900px 的媒体查询').not.toBe('');
    // 必须带 `.app-header` 这一级：`.icon-button { display:inline-flex }`
    // 与裸 `.app-header__menu` 同优先级（0,1,0）且源码在后，会覆盖掉隐藏规则。
    expect(block).toMatch(/\.app-header\s+\.app-header__menu\s*\{[^}]*display:\s*none/);
    // 反证：只写裸类名的「修复」在这个媒体查询里不算数
    expect(block).not.toMatch(/(^|\n)\s*\.app-header__menu\s*\{/);
  });
});
