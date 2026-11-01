/**
 * 总览（/dashboard）的回归测试。
 *
 * 这组测试补的是一个「没有任何测试能发现」的缺陷：`DashboardPage` 曾被挂在
 * `<header className="app-header">` 里，而 `<main>` 的三元链没有 `dashboard`
 * 分支，于是 `/dashboard` 落到兜底的 `<PersonaPage/>`——总览卡片被压进页头
 * （1440 视口下 12 列每列 8.05px），主区显示的是「角色设定 / 系统提示（人格设定）」。
 * 因为没有任何测试断言过「哪条路由渲染哪个页面」「页面渲染在页头还是主区」，
 * 这个缺陷一路躺到了线上首屏。
 *
 * 所以这里断言的是**结构**，不是像素：
 * - `mainPage(route)`：路由 → 页面的一对一映射，且除 `persona` 外不许出现角色页；
 * - `AppShell`：整页渲染成 HTML 后，页头里不许有总览、主区里必须有总览；
 * - 未知路由不再是静默的 `PersonaPage` 兜底。
 *
 * 不引入 DOM 测试库（仓库没有 jsdom 依赖，`about.test.ts` 也是同样的取舍）：
 * 用 `react-dom/server` 把组件渲染成字符串，再对结构和文案做断言；
 * 布局/视口相关的断言交给 `tests/e2e/dashboard_browser.mjs`（真 Chromium）。
 */
import { readFileSync } from 'node:fs';
import { createElement as h, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AppShell, mainPage, type MainPageProps } from './App';
import {
  AgendaBody,
  Card,
  commitmentStateLabel,
  emotionLabel,
  formatDayTime,
  isToday,
  proactiveDeliveryLine,
  type CardProps,
} from './components/DashboardPage';
import { LEGACY_REDIRECTS, parseRoute, routePath, type Route } from './route';
import type { DashboardSection, UserInfo } from './types';

const USER: UserInfo = {
  id: 'user_test',
  username: 'ling',
  role: 'owner',
  subject_id: 'subj_test',
  display_name: 'Ling',
  must_change_password: false,
};

/** PersonaPage 的指纹：加载态文案 + 加载完成后的字段标题。 */
const PERSONA_MARKERS = ['正在读取角色设定', '系统提示（人格设定）', '叙事边界', '保存设定'];
/** DashboardPage 的指纹：它自己的加载态文案（SSR 下页面一定停在加载态）。 */
const DASHBOARD_MARKER = '正在读取她的状态';
/** 聊天页的指纹：输入框/布局。未知路由绝不能渲染出这些。 */
const CHAT_MARKERS = ['chat-layout', 'composer-attach', '说点什么吧'];

const NOOP = () => undefined;

function mainProps(route: Route): MainPageProps {
  return {
    route,
    user: USER,
    sidebarOpen: false,
    onCloseSidebar: NOOP,
    onTestMode: NOOP,
    onUserChange: NOOP,
    onLogout: NOOP,
    onGo: NOOP,
  };
}

function renderRoute(route: Route): string {
  return renderToStaticMarkup(mainPage(mainProps(route)));
}

/** `Card` 的 children 是渲染函数；`h(Card, props, child)` 在这种必填 children 的
 *  组件上过不了 TS 的重载，所以统一把 children 放进 props 里。 */
function renderCard(
  props: Omit<CardProps, 'children'>,
  body: (data: Record<string, unknown>) => ReactNode = () => null,
): string {
  return renderToStaticMarkup(h(Card, { ...props, children: body }));
}

function renderShell(route: Route): string {
  return renderToStaticMarkup(
    h(AppShell, { route, user: USER, testMode: false, onLogout: NOOP, onTestMode: NOOP, onUserChange: NOOP }),
  );
}

/** 取出 `<header …>…</header>` / `<main …>…</main>` 这一段，取不到直接失败。 */
function slice(html: string, open: string, close: string): string {
  const start = html.indexOf(open);
  const end = start < 0 ? -1 : html.indexOf(close, start);
  expect(start, `找不到 ${open}`).toBeGreaterThanOrEqual(0);
  expect(end, `找不到 ${close}`).toBeGreaterThan(start);
  return html.slice(start, end + close.length);
}

/**
 * 取出被 `aria-current="page"` 标记的那一个导航项（从它的 `data-testid` 到
 * 自己的 `</button>`）。
 *
 * 刻意不再用视觉高亮类名：那是样式，左导航改造时一定会改；
 * 「当前页」是产品契约，用语义属性表达。
 */
function activeNavItem(header: string): string {
  const marks = header.match(/aria-current="page"/g) ?? [];
  expect(marks.length, '当前页高亮必须恰好一项').toBe(1);
  const current = header.indexOf('aria-current="page"');
  const testidAt = header.lastIndexOf('data-testid="', current);
  expect(testidAt, '高亮项没有 data-testid，测试无法定位它').toBeGreaterThanOrEqual(0);
  const end = header.indexOf('</button>', current);
  return header.slice(testidAt, end < 0 ? header.length : end);
}

/** 取出带指定 `data-testid` 的那个 `<button>`；找不到返回 null。 */
function buttonWithTestId(html: string, testid: string): string | null {
  const at = html.indexOf(`data-testid="${testid}"`);
  if (at < 0) return null;
  const start = html.lastIndexOf('<button', at);
  const end = html.indexOf('</button>', at);
  if (start < 0 || end < 0) return null;
  return html.slice(start, end + '</button>'.length);
}

/**
 * 取出外壳里的主导航区（`<nav aria-label="主导航">`）。
 *
 * 刻意不写死 `<header>`：Step 1 已把导航搬到左导航，Step 2/4 会删掉顶栏的
 * 快捷入口。测试锚定**主导航这个 landmark**（按 aria-label 找），而不是
 * 「第一个 <nav>」或页头——否则取到的会是别的东西（页头已没有导航）。
 */
function navRegion(html: string): string {
  const landmark = html.indexOf('aria-label="主导航"');
  expect(landmark, '找不到 aria-label="主导航" 的导航区').toBeGreaterThanOrEqual(0);
  const start = html.lastIndexOf('<nav', landmark);
  const end = html.indexOf('</nav>', start);
  expect(start, '找不到主导航 <nav>').toBeGreaterThanOrEqual(0);
  expect(end, '主导航没有闭合').toBeGreaterThan(start);
  return html.slice(start, end + '</nav>'.length);
}

/**
 * 外壳里除页面主区以外的部分：页头、左导航都算，页面正文不算。
 *
 * 用「外壳（去掉 main）」当作用域，防止导航项只出现在页面正文里。
 * `nav-chat` 现在只在左导航/抽屉的「对话」项上；页头 logo 用独立 testid
 * `brand-home`（原先两者同名，见 QA-TASK102 L2）。
 */
function shellChrome(html: string): string {
  const mainStart = html.indexOf('<main');
  const mainEnd = html.indexOf('</main>', mainStart);
  expect(mainStart, '找不到 <main>').toBeGreaterThanOrEqual(0);
  expect(mainEnd, '<main> 没有闭合').toBeGreaterThan(mainStart);
  return html.slice(0, mainStart) + html.slice(mainEnd + '</main>'.length);
}

/**
 * 一级导航的**产品契约**。
 *
 * 刻意手写，不从 `App.tsx` 的渲染列表推导：从实现推导的期望值会跟着实现
 * 一起消失，删掉一个导航项时测试仍然全绿——那正是 `ROUTE_NAMES` 漏掉
 * `notFound` 的同款问题。这里删掉任何一项，测试必须红。
 *
 * `nav-chat` 只属于左导航（与抽屉）里的「对话」项；页头品牌标识的 testid
 * 是 `brand-home`（同样回聊天，但不是导航项）。`nav-now` 已按用户拍板
 * 改名为 `nav-life`（路由仍是 `/life`，改名的是导航项）。
 */
const PRIMARY_NAV = [
  { testid: 'nav-chat', label: '对话' },
  { testid: 'nav-dashboard', label: '总览' },
  { testid: 'nav-memory', label: '记忆' },
  { testid: 'nav-life', label: '生活' },
  { testid: 'nav-persona', label: '人设' },
  { testid: 'nav-settings', label: '设置' },
] as const;

/**
 * 路由名清单**从 `Route` 类型派生**，不手写。
 *
 * 之前这里是手写数组：新增 `notFound` 时数组没跟着加，穷尽性测试直接漏掉了
 * 这条路由，测试仍然全绿。现在 `satisfies Record<Route['name'], true>` 要求
 * 每个路由名都必须有一个键——漏一个就是编译错误（`tsc --noEmit`），
 * 新增路由名却忘了更新测试会当场红，而不是悄悄少测一条。
 *
 * 多写一个 `Route` 里没有的键同样会报错（对象字面量的多余属性检查）。
 */
const ROUTE_NAME_SET = {
  dashboard: true,
  chat: true,
  now: true,
  settings: true,
  model: true,
  memory: true,
  privacy: true,
  persona: true,
  identity: true,
  account: true,
  about: true,
  legacyRedirect: true,
  notFound: true,
} satisfies Record<Route['name'], true>;

const ROUTE_NAMES = Object.keys(ROUTE_NAME_SET) as Array<Route['name']>;

/**
 * 造一条该路由的**真实地址**再解析回来，而不是硬编一个对象。
 *
 * 两个理由：①`legacyRedirect` 带 `to`，光有 `name` 造不出合法对象；
 * ②这样 `routePath` 与 `parseRoute` 之间任何一处脱节都会在这里暴露。
 */
function routeFor(name: Route['name']): Route {
  if (name === 'chat') return { name: 'chat', sessionId: null };
  if (name === 'legacyRedirect') return { name: 'legacyRedirect', to: '/memory?tab=settings' };
  if (name === 'notFound') return { name: 'notFound' };
  return parseRoute(routePath({ name } as Route));
}

describe('路由表与页面表一一对应', () => {
  it('/dashboard 解析成 dashboard 路由，而不是被当成未知路径', () => {
    expect(parseRoute('/dashboard')).toEqual({ name: 'dashboard' });
    expect(routePath({ name: 'dashboard' })).toBe('/dashboard');
  });

  it('未知路径落到 notFound，不再静默回聊天（M1）', () => {
    expect(parseRoute('/qa-no-such-route')).toEqual({ name: 'notFound' });
    expect(parseRoute('/no-such')).toEqual({ name: 'notFound' });
    expect(parseRoute('/settings/nope')).toEqual({ name: 'notFound' });
    // 拼错的已知路径同样不能冒充聊天页
    expect(parseRoute('/dashbord')).toEqual({ name: 'notFound' });
    expect(parseRoute('/s/')).toEqual({ name: 'notFound' });
  });

  it('已知路由零回归：10 条真实地址 + /s/{id} 深链', () => {
    expect(parseRoute('/')).toEqual({ name: 'chat', sessionId: null });
    expect(parseRoute('/s/sess-1')).toEqual({ name: 'chat', sessionId: 'sess-1' });
    expect(parseRoute('/s/a%2Fb')).toEqual({ name: 'chat', sessionId: 'a/b' });
    expect(parseRoute('/dashboard')).toEqual({ name: 'dashboard' });
    expect(parseRoute('/dashboard/')).toEqual({ name: 'dashboard' });
    expect(parseRoute('/life')).toEqual({ name: 'now' });
    expect(parseRoute('/settings')).toEqual({ name: 'settings' });
    expect(parseRoute('/settings/model')).toEqual({ name: 'model' });
    expect(parseRoute('/settings/privacy')).toEqual({ name: 'privacy' });
    expect(parseRoute('/settings/identity')).toEqual({ name: 'identity' });
    expect(parseRoute('/settings/account')).toEqual({ name: 'account' });
    expect(parseRoute('/settings/about')).toEqual({ name: 'about' });
    expect(parseRoute('/persona')).toEqual({ name: 'persona' });
    expect(parseRoute('/memory')).toEqual({ name: 'memory' });
  });

  it('旧地址命中精确重定向表：/settings/memory、/settings/dialogue、/settings/life', () => {
    // Step 4 把这三类的调参迁回各自的领域页（设置只放程序与运维）。
    // 旧地址必须**指出去**，而不是继续当一页内容渲染——否则「迁出」只是加了个别名。
    expect(parseRoute('/settings/memory')).toEqual({
      name: 'legacyRedirect',
      to: '/memory?tab=settings',
    });
    expect(parseRoute('/settings/dialogue')).toEqual({
      name: 'legacyRedirect',
      to: '/persona?tab=dialogue',
    });
    expect(parseRoute('/settings/life')).toEqual({
      name: 'legacyRedirect',
      to: '/life?tab=rhythm',
    });
    // 尾斜杠同样命中（和真实路由的尾斜杠处理一致）
    expect(parseRoute('/settings/memory/')).toEqual({
      name: 'legacyRedirect',
      to: '/memory?tab=settings',
    });
  });

  it('重定向目标必须是**真实路由**，且带 tab 深链', () => {
    for (const [from, to] of Object.entries(LEGACY_REDIRECTS)) {
      const target = parseRoute(to.split('?')[0] ?? '/');
      expect(target.name, `${from} 的目标 ${to} 不是真实路由`).not.toBe('notFound');
      expect(target.name, `${from} 的目标又指向旧地址（会循环）`).not.toBe('legacyRedirect');
      expect(to, `${from} 的目标 ${to} 没有 tab 深链`).toContain('?tab=');
      expect(to, `${from} 的目标用了 hash 路由`).not.toContain('#');
    }
  });

  it('表外路径一律 notFound（重定向不许掩盖真 404）', () => {
    // 若把精确表换成 `/settings/*` 通配重定向，这些断言会立刻红。
    for (const path of [
      '/settings/memroy',
      '/settings/memory/extra',
      '/settings/dialogue/extra',
      '/settings/life/extra',
      '/settings/policy',
      '/settings/whatever',
      '/settings/nope',
    ]) {
      expect(parseRoute(path), `${path} 被错误地重定向了`).toEqual({ name: 'notFound' });
    }
  });

  it('重定向首帧渲染的是**目标页**，不是 404（旧书签不闪一下「无法识别的路由」）', () => {
    const memory = renderRoute(parseRoute('/settings/memory'));
    // 记忆页在 SSR 下停在加载态：它不是 404 面板，也不是聊天页
    expect(memory).not.toContain('data-testid="unknown-route"');
    expect(memory).not.toContain('data-testid="chat-layout"');

    const persona = renderRoute(parseRoute('/settings/dialogue'));
    expect(persona).not.toContain('data-testid="unknown-route"');
    // 人设页的对话 tab：首帧就该开在调参表单上（tab 推导也认旧路径）
    expect(persona).toContain('对话与主动性');

    const life = renderRoute(parseRoute('/settings/life'));
    expect(life).not.toContain('data-testid="unknown-route"');
    expect(life).toContain('生活节奏');
  });

  it('routePath 拒绝为 notFound 生成可导航 URL', () => {
    // 返回一个 URL（哪怕是聊天页）都会让"未知地址"重新变成静默跳转。
    expect(() => routePath({ name: 'notFound' })).toThrow(/notFound/);
  });

  it('每个路由名都能渲染出页面（穷尽性：新增 Route 分支却漏了 case 时 tsc 会报错）', () => {
    for (const name of ROUTE_NAMES) {
      const markup = renderRoute(routeFor(name));
      expect(markup.length, `${name} 渲染为空`).toBeGreaterThan(0);
    }
  });

  it('只有 persona 路由出现角色页，其余路由都不出现', () => {
    for (const name of ROUTE_NAMES) {
      const markup = renderRoute(routeFor(name));
      const hits = PERSONA_MARKERS.filter((marker) => markup.includes(marker));
      if (name === 'persona') {
        expect(hits.length, 'persona 路由必须渲染角色页').toBeGreaterThan(0);
      } else {
        expect(hits, `${name} 路由不该出现角色页`).toEqual([]);
      }
    }
  });
});

describe('一级导航存在性契约', () => {
  it('每个路由下，所有一级导航项都出现在外壳导航区里', () => {
    // 导航不随页面消失：在每一条路由上都查一遍；而且必须真的在导航区，
    // 不是页面正文里某个同名的 testid。
    for (const name of ROUTE_NAMES) {
      const html = renderShell(routeFor(name));
      const chrome = shellChrome(html);
      // 导航区必须存在，且不能混进页面主区。
      expect(navRegion(html)).toContain('aria-label="主导航"');
      for (const item of PRIMARY_NAV) {
        const button = buttonWithTestId(chrome, item.testid);
        expect(button, `${name} 路由缺少一级导航项 ${item.testid}`).not.toBeNull();
        expect(button, `${name} 路由的 ${item.testid} 不是按钮`).toContain('<button');
      }
    }
  });

  it('每个一级导航项的文字与入口对得上（不是空壳 testid）', () => {
    const chrome = shellChrome(renderShell({ name: 'dashboard' }));
    for (const item of PRIMARY_NAV) {
      const button = buttonWithTestId(chrome, item.testid);
      expect(button, `缺少一级导航项 ${item.testid}`).not.toBeNull();
      expect(button, `${item.testid} 的文字不对`).toContain(item.label);
      expect(button, `${item.testid} 不是普通按钮`).toContain('type="button"');
    }
  });

  it('同一时刻只有一个导航项是「当前页」', () => {
    const chrome = shellChrome(renderShell({ name: 'dashboard' }));
    expect(chrome.match(/aria-current="page"/g) ?? []).toHaveLength(1);
    expect(buttonWithTestId(chrome, 'nav-dashboard')).toContain('aria-current="page"');
    // 其它导航项不许冒充当前页
    for (const item of PRIMARY_NAV) {
      if (item.testid === 'nav-dashboard') continue;
      const button = buttonWithTestId(chrome, item.testid);
      expect(button, `缺少一级导航项 ${item.testid}`).not.toBeNull();
      expect(button, `${item.testid} 不该标记为当前页`).not.toContain('aria-current="page"');
    }
  });

  it('导航项带 testid，E2E 不再靠文字或页头类名定位', () => {
    const chrome = shellChrome(renderShell({ name: 'dashboard' }));
    // 回归锁：这些 testid 是 E2E 的公开选择器，改名必须同步改 E2E。
    expect(chrome).toContain('data-testid="nav-chat"');
    expect(chrome).toContain('data-testid="nav-dashboard"');
    expect(chrome).toContain('data-testid="nav-settings"');
    expect(chrome).toContain('data-testid="nav-life"');
    expect(chrome).toContain('data-testid="nav-persona"');
    // 退出不是一级导航目的地，但它是外壳里的可点入口（桌面页头 / 窄屏抽屉）
    expect(chrome).toContain('data-testid="nav-logout"');
    // 页头 logo 的公开选择器；`w0_browser` / `dashboard_browser` 点它回聊天
    expect(chrome).toContain('data-testid="brand-home"');
  });
});

describe('dashboard 路由渲染总览页', () => {
  it('主区渲染的是 DashboardPage，不是 PersonaPage', () => {
    const markup = renderRoute({ name: 'dashboard' });
    expect(markup).toContain(DASHBOARD_MARKER);
    for (const marker of PERSONA_MARKERS) {
      expect(markup, `总览页不该出现「${marker}」`).not.toContain(marker);
    }
  });

  it('总览在 <main> 里，页头里一点都没有', () => {
    const html = renderShell({ name: 'dashboard' });
    const header = slice(html, '<header', '</header>');
    const main = slice(html, '<main', '</main>');

    // 主区：总览页本体 + 它的容器类名
    expect(main).toContain('class="app-main"');
    expect(main).toContain(DASHBOARD_MARKER);

    // 页头：只有 brand / 导航 / 用户名，没有任何总览内容
    expect(header).toContain('class="app-header"');
    expect(header).toContain('brand__text');
    expect(header).toContain('Ling');
    // 注意不能笼统地查 "dash"：导航项自己的 data-testid 就叫 nav-dashboard。
    // 这里查的是总览页本体的容器与卡片，而不是导航标识。
    expect(header).not.toContain('class="page dash"');
    expect(header).not.toContain('dash-card');
    expect(header).not.toContain(DASHBOARD_MARKER);
    // `main` 标签在整页里只该有一个，且位于页头之后
    expect(html.indexOf('<header')).toBeLessThan(html.indexOf('<main'));
    expect(html.split('<main').length - 1).toBe(1);
  });

  it('导航仍能把人送到总览（当前页语义标记也还在）', () => {
    const html = renderShell({ name: 'dashboard' });
    const chrome = shellChrome(html);
    const active = activeNavItem(chrome);
    expect(active).toContain('data-testid="nav-dashboard"');
    expect(active).toContain('总览');
    // 其它页面时标记的是那一页自己的导航项，不是总览
    const nowItem = activeNavItem(shellChrome(renderShell({ name: 'now' })));
    expect(nowItem).toContain('data-testid="nav-life"');
    expect(nowItem).toContain('生活');
    expect(nowItem).not.toContain('总览');
    // 聊天页的当前项是左导航「对话」，页头 logo 不冒充当前页
    const chatItem = activeNavItem(
      shellChrome(renderShell({ name: 'chat', sessionId: null })),
    );
    expect(chatItem).toContain('data-testid="nav-chat"');
  });

  it('渲染层面：页面本体只在主区，导航不混进页面', () => {
    // 之前这里读 App.tsx 源码、断言页头里没有 `DashboardPage` 字面量。
    // 源码字符串不是产品契约：改个组件名或换个外壳就假红/假绿。
    // 现在直接渲染整页：页面本体只在主区，导航在导航区（位置随外壳变，testid 不变）。
    for (const name of ROUTE_NAMES) {
      const html = renderShell(routeFor(name));
      const header = slice(html, '<header', '</header>');
      const main = slice(html, '<main', '</main>');
      expect(header, `${name}：页头混进了主区容器`).not.toContain('class="app-main"');
      expect(header, `${name}：页头混进了页面容器`).not.toContain('class="page"');
      expect(header, `${name}：页头出现了页面加载态`).not.toContain('正在读取');
      expect(header, `${name}：页头里出现了 <main>`).not.toContain('<main');
      // 导航必须在 shell 里，且不能混进页面主区；具体在页头还是左导航不锁。
      expect(navRegion(html), `${name}：外壳里没有主导航`).toContain('aria-label="主导航"');
      expect(main, `${name}：主导航混进了页面主区`).not.toContain('aria-label="主导航"');
    }

    const dashboardHtml = renderShell({ name: 'dashboard' });
    const dashboardMain = slice(dashboardHtml, '<main', '</main>');
    expect(dashboardMain).toContain(DASHBOARD_MARKER);
    expect(dashboardMain).not.toContain('data-testid="unknown-route"');
    for (const marker of PERSONA_MARKERS) {
      expect(dashboardMain, `总览主区不该出现角色页「${marker}」`).not.toContain(marker);
    }
    for (const marker of CHAT_MARKERS) {
      expect(dashboardMain, `总览主区不该出现聊天页「${marker}」`).not.toContain(marker);
    }
  });

  it('每条已知路由都有自己的页面，不会掉进未知路由兜底', () => {
    // 取代源码正则 `case 'dashboard': … <DashboardPage`：直接对每条路由
    // 断言渲染结果不是 unknown-route。漏写 case 时 tsc 已经会报错，
    // 这里再从行为上锁一次（防止有人把 case 写成返回兜底组件）。
    for (const name of ROUTE_NAMES) {
      if (name === 'notFound') continue;
      const markup = renderRoute(routeFor(name));
      expect(markup, `${name} 掉进了 unknown-route 兜底`).not.toContain(
        'data-testid="unknown-route"',
      );
    }
  });

  it('未知路由名走 default 分支：渲染可见兜底，并带上不认识的名字', () => {
    // 取代源码正则 `default: … <UnknownRoutePage`。
    const bogus = { name: 'qa-default-branch' } as unknown as Route;
    const markup = renderRoute(bogus);
    expect(markup).toContain('data-testid="unknown-route"');
    expect(markup).toContain('无法识别的路由');
    expect(markup).toContain('qa-default-branch');
    for (const marker of PERSONA_MARKERS) expect(markup).not.toContain(marker);
    for (const marker of CHAT_MARKERS) expect(markup).not.toContain(marker);
  });
});

describe('未知路由的兜底是可见的', () => {
  it('渲染「无法识别的路由」而不是角色页', () => {
    const bogus = { name: 'no-such-route' } as unknown as Route;
    const markup = renderToStaticMarkup(mainPage(mainProps(bogus)));
    expect(markup).toContain('无法识别的路由');
    expect(markup).toContain('data-testid="unknown-route"');
    // 面板类名必须真有样式：`page__error` 当初就没有，失败态只剩裸文字
    expect(markup).toContain('class="dash-error"');
    for (const marker of PERSONA_MARKERS) {
      expect(markup).not.toContain(marker);
    }
  });

  it('URL 层的未知路径渲染 unknown-route，不含聊天输入框指纹（M1 阻断项）', () => {
    const route = parseRoute('/qa-no-such-route');
    const markup = renderToStaticMarkup(mainPage(mainProps(route)));
    expect(markup).toContain('data-testid="unknown-route"');
    expect(markup).toContain('无法识别的路由');
    // 修复前这里渲染的是整页聊天：输入框、会话侧栏都在
    for (const marker of CHAT_MARKERS) {
      expect(markup, `未知路径不该出现聊天页指纹「${marker}」`).not.toContain(marker);
    }
    for (const marker of PERSONA_MARKERS) {
      expect(markup).not.toContain(marker);
    }
  });

  it('未知页有可点的返回入口（回 /dashboard），不是死胡同', () => {
    const markup = renderToStaticMarkup(mainPage(mainProps(parseRoute('/qa-no-such-route'))));
    expect(markup).toContain('data-testid="unknown-route-home"');
    expect(markup).toContain('回到总览');
    // 入口按钮必须是真按钮，且不靠 routePath(notFound) 生成地址
    expect(markup).toMatch(
      /<button[^>]*data-testid="unknown-route-home"[^>]*>回到总览<\/button>/,
    );
  });

  it('路由兜底是 notFound：所有未知地址都渲染可见兜底，且 notFound 没有可导航地址', () => {
    // 取代原来读 route.ts 源码、断言 `return { name: 'notFound' }` 的写法：
    // 源码字符串会随着实现重写而假红，行为不会。
    for (const path of ['/qa-no-such-route', '/no-such', '/settings/nope', '/dashbord', '/s/']) {
      const route = parseRoute(path);
      expect(route, path).toEqual({ name: 'notFound' });
      const markup = renderToStaticMarkup(mainPage(mainProps(route)));
      expect(markup, path).toContain('data-testid="unknown-route"');
      for (const marker of CHAT_MARKERS) {
        expect(markup, `${path} 不该渲染聊天页「${marker}」`).not.toContain(marker);
      }
    }
    expect(() => routePath({ name: 'notFound' })).toThrow(/notFound/);
  });
});

describe('卡片三态：空数据与查询失败必须长得不一样', () => {
  const asOf = '2026-10-30T05:48:02.650639+00:00';

  it('ok：渲染数据并存「更新于」', () => {
    const markup = renderCard(
      { title: '情绪', section: { status: 'ok', as_of: asOf } },
      () => h('p', null, 'calm'),
    );
    expect(markup).toContain('calm');
    expect(markup).toContain('更新于');
  });

  it('empty：仍标「更新于」（空是结论，也要能看出是什么时候的结论）', () => {
    const markup = renderCard({ title: '情绪', section: { status: 'empty', as_of: asOf } });
    expect(markup).toContain('暂时没有状态记录。');
    expect(markup).toContain('更新于');
  });

  it('unavailable：明确说读不到，且不冒充有采集时间', () => {
    const markup = renderCard({ title: '情绪', section: { status: 'unavailable', as_of: asOf } });
    expect(markup).toContain('暂时读不到这块数据。');
    expect(markup).not.toContain('更新于');
    expect(markup).not.toContain('暂时没有状态记录。');
  });

  it('unavailable 与 empty 的渲染指纹不同（L1：读不到 ≠ 确实没有）', () => {
    const unavailable = renderCard({ title: '情绪', section: { status: 'unavailable', as_of: asOf } });
    const empty = renderCard({ title: '情绪', section: { status: 'empty', as_of: asOf } });
    // 失败态：卡内告警条（图标 + 警示底）+ role=status
    expect(unavailable).toContain('dash-card__unavailable');
    expect(unavailable).toContain('role="status"');
    expect(unavailable).not.toContain('dash-card__muted');
    // 空态：裸灰字，没有告警条
    expect(empty).toContain('dash-card__muted');
    expect(empty).not.toContain('dash-card__unavailable');
  });
});

describe('主动联系：候选数、能不能发、为什么不能发', () => {
  it('没有候选时也把门控读出来', () => {
    const section: DashboardSection = {
      status: 'empty',
      as_of: '2026-10-30T05:48:02Z',
      // 真实响应就是「0 条候选 + 门控原因」，照抄形状
      data: { delivery_enabled: false, gate_reason: 'OneBot 没有声明支持主动推送' },
    };
    const markup = renderCard({ title: '主动联系', section, renderWhenEmpty: true }, (data) =>
      h(
        'div',
        null,
        h('span', null, `${Number(data.pending_count ?? 0)} 条候选`),
        h('p', null, proactiveDeliveryLine(data)),
        h('p', null, '当前没有待处理的候选。'),
      ),
    );
    expect(markup).toContain('0 条候选');
    expect(markup).toContain('不会发出');
    expect(markup).toContain('当前没有待处理的候选。');
  });

  it('proactiveDeliveryLine 覆盖三种情况', () => {
    expect(proactiveDeliveryLine({ delivery_enabled: true, gate_reason: '' })).toBe('可以发出');
    expect(
      proactiveDeliveryLine({ delivery_enabled: false, gate_reason: 'OneBot 没有声明支持主动推送' }),
    ).toBe('不会发出：OneBot 没有声明支持主动推送');
    expect(proactiveDeliveryLine({ delivery_enabled: false, gate_reason: '   ' })).toBe(
      '不会发出：渠道没有给出原因',
    );
    // 字段缺失（旧服务端 / 别的实现）不许编一个结论出来
    expect(proactiveDeliveryLine({})).toBe('还不能确定能不能发出');
  });

  it('总览相关组件渲染出的类名都真有样式（失败态曾用一个不存在的类）', () => {
    // 取代读 `DashboardPage.tsx` 再正则抓 `className="..."` 的源码断言：
    // 现在先把组件真正渲染成 HTML，只检查**实际渲染出来**的类名。
    // 这样模板字符串（`dash-card--${span}`）也会以具体值被覆盖，
    // 而失败态/空态/列表态都由渲染结果证明真的能出现。
    const styles =
      readFileSync(new URL('./styles.css', import.meta.url), 'utf8') +
      readFileSync(new URL('./dashboard.css', import.meta.url), 'utf8');
    const asOf = '2026-10-30T05:48:02+00:00';
    const markup = [
      renderCard({ title: '情绪', section: { status: 'ok', as_of: asOf } }, () => h('p', null, '平静')),
      renderCard({ title: '情绪', section: { status: 'empty', as_of: asOf } }),
      renderCard({ title: '情绪', section: { status: 'unavailable', as_of: asOf } }),
      renderCard(
        {
          title: '今天的安排',
          section: { status: 'empty', as_of: asOf, data: { plans: [], commitments: [], goals: [] } },
          renderWhenEmpty: true,
        },
        (data) => h(AgendaBody, { data }),
      ),
      renderRoute({ name: 'dashboard' }),
    ].join('');
    const classNames = [...markup.matchAll(/class="([^"]+)"/g)].flatMap((match) =>
      (match[1] ?? '').split(/\s+/),
    );
    expect(classNames.length).toBeGreaterThan(10);
    const unstyled = [...new Set(classNames)].filter(
      (name) => name.length > 0 && !new RegExp(`\\.${name}(?![\\w-])`).test(styles),
    );
    expect(unstyled, '这些类名没有对应样式').toEqual([]);
  });

  it('renderWhenEmpty：empty 时渲染卡片正文，而不是一句笼统的空态', () => {
    // 主动联系卡在 0 条候选时仍要显示「几条候选 / 能不能发 / 为什么不能发」：
    // 后端 empty 载荷里带着门控，用「暂无内容」盖掉等于把渠道限制藏起来。
    const section: DashboardSection = {
      status: 'empty',
      as_of: '2026-10-30T05:48:02Z',
      data: { delivery_enabled: false, gate_reason: 'OneBot 没有声明支持主动推送' },
    };
    const withBody = renderCard({ title: '主动联系', section, renderWhenEmpty: true }, (data) =>
      h(
        'div',
        null,
        h('p', null, `${Number(data.pending_count ?? 0)} 条候选`),
        h('p', null, proactiveDeliveryLine(data)),
      ),
    );
    expect(withBody).toContain('0 条候选');
    expect(withBody).toContain('不会发出');
    expect(withBody).not.toContain('暂无内容。');
    // 不传 renderWhenEmpty 才是笼统空态：两者必须长得不一样。
    const withoutBody = renderCard({ title: '主动联系', section });
    expect(withoutBody).toContain('暂无内容。');
    expect(withoutBody).not.toContain('条候选');
  });
});

describe('情绪显示中文（F5）', () => {
  it('后端闭集里的六种情绪都有中文', () => {
    expect(emotionLabel('calm')).toBe('平静');
    expect(emotionLabel('warm')).toBe('温暖');
    expect(emotionLabel('tired')).toBe('疲惫');
    expect(emotionLabel('curious')).toBe('好奇');
    expect(emotionLabel('uneasy')).toBe('不安');
    expect(emotionLabel('content')).toBe('满足');
  });

  it('未知值兜底显示原文，不瞎翻译成「未知」', () => {
    expect(emotionLabel('melancholy')).toBe('melancholy');
    expect(emotionLabel('  好奇 ')).toBe('好奇');
    expect(emotionLabel(undefined)).toBe('未知');
    expect(emotionLabel('')).toBe('未知');
  });
});

describe('今天的安排：日期与状态（F4）', () => {
  it('日程时间带日期，不再只有 HH:mm', () => {
    expect(formatDayTime('2026-10-30T07:00:00')).toBe('10-30 07:00');
    expect(formatDayTime('2030-01-01T12:00:00')).toBe('01-01 12:00');
    expect(formatDayTime('')).toBe('');
    expect(formatDayTime('not-a-date')).toBe('');
  });

  it('isToday 按本地日期判断', () => {
    const now = new Date(2026, 9, 31, 12, 0, 0);
    expect(isToday('2026-10-31T08:00:00', now)).toBe(true);
    expect(isToday('2026-10-30T08:00:00', now)).toBe(false);
    expect(isToday('', now)).toBe(false);
    expect(isToday('not-a-date', now)).toBe(false);
  });

  it('承诺状态有中文映射，未知值兜底原文', () => {
    expect(commitmentStateLabel('active')).toBe('待履行');
    expect(commitmentStateLabel('cancelled')).toBe('已取消');
    expect(commitmentStateLabel('undeliverable')).toBe('送不出去');
    expect(commitmentStateLabel('delivered')).toBe('已送到');
    expect(commitmentStateLabel('future_state')).toBe('future_state');
    expect(commitmentStateLabel('')).toBe('');
  });

  it('过期日程标「不是今天」，已取消/送不出去的承诺不再冒充待履行', () => {
    const markup = renderToStaticMarkup(
      h(AgendaBody, {
        data: {
          plans: [
            {
              plan_id: 'p1',
              title: '给无人机小兔子做保养',
              start_at: '2020-01-02T15:00:00',
              commitment_level: 'tentative',
              state: 'planned',
            },
          ],
          commitments: [
            {
              commitment_id: 'c1',
              text: '提醒我周五交那份材料',
              due_at: '2020-01-03T15:22:09',
              state: 'undeliverable',
            },
            {
              commitment_id: 'c2',
              text: '周末提醒你买书',
              due_at: '2030-01-01T12:00:00',
              state: 'cancelled',
            },
            {
              commitment_id: 'c3',
              text: '还没到点的事',
              due_at: '2030-02-01T12:00:00',
              state: 'active',
            },
          ],
          goals: [],
        },
      }),
    );
    expect(markup).toContain('01-02 15:00');
    expect(markup).toContain('不是今天');
    expect(markup).toContain('送不出去');
    expect(markup).toContain('已取消');
    expect(markup).toContain('待履行');
    // 已结束的承诺带 inactive 标记；待履行的不带
    expect(markup).toContain('dash-list__meta--inactive');
    expect(markup.match(/dash-list__meta--inactive/g) ?? []).toHaveLength(2);
  });

  it('「今天」由前端按本地日期判断，日程永远显示日期（不依赖后端过滤）', () => {
    // 取代读 `DashboardPage.tsx`、断言 `formatDayTime(String(v.start_at` 的源码写法：
    // 直接渲染两条日程——今天的一条、很久以前的一条——验证输出。
    const now = new Date();
    const pad = (value: number) => String(value).padStart(2, '0');
    const todayIso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T09:05:00`;
    const oldIso = '2020-01-02T15:00:00';
    const markup = renderToStaticMarkup(
      h(AgendaBody, {
        data: {
          plans: [
            {
              plan_id: 'p-today',
              title: '今天要开的会',
              start_at: todayIso,
              commitment_level: 'tentative',
              state: 'planned',
            },
            {
              plan_id: 'p-old',
              title: '很久以前的事',
              start_at: oldIso,
              commitment_level: 'tentative',
              state: 'planned',
            },
          ],
          commitments: [],
          goals: [],
        },
      }),
    );
    // 日期由前端格式化后显示，原始 ISO 串不许直接漏到界面上
    expect(markup).toContain(formatDayTime(todayIso));
    expect(markup).toContain('01-02 15:00');
    expect(markup).not.toContain(todayIso);
    expect(markup).not.toContain(oldIso);
    // 今天的不标「不是今天」，过期的那条必须标
    const rows = markup.split('<li').filter((row) => row.includes('要开的会') || row.includes('很久以前'));
    const rowFor = (title: string) => rows.find((row) => row.includes(title)) ?? '';
    expect(rowFor('今天要开的会'), '今天的日程被误标成「不是今天」').not.toContain('不是今天');
    expect(rowFor('很久以前的事'), '过期日程没有标「不是今天」').toContain('不是今天');
  });
});

describe('账号页「新的登录名」（F11）', () => {
  const markup = renderRoute({ name: 'account' });

  it('输入框从空白开始，不再预填当前登录名', () => {
    expect(markup).not.toContain('value="ling"');
    expect(markup).toContain('value=""');
    expect(markup).toContain('placeholder="输入新的登录名（不想改就留空）"');
  });

  it('不再常驻「登录名没有变化。」', () => {
    expect(markup).not.toContain('登录名没有变化。');
  });

  it('两个提交按钮初始都禁用（空值不能提交）；输入后的启用/禁用由 E2E 覆盖', () => {
    // 取代读 `AccountPage.tsx` 断言 `trimmedName.length > 0 && ...` 的源码写法。
    // SSR 只能验证初始状态：两个提交按钮都必须是禁用态。真正的「填了不同的
    // 名字 / 当前密码才启用」在浏览器里由
    // `tests/e2e/dashboard_browser.mjs` 的账号页场景逐条验证（缺密码仍禁用、
    // 名字没变仍禁用、名字不同才启用），那里能真的输入。
    const buttons = [...markup.matchAll(/<button[^>]*type="submit"[^>]*>([^<]*)<\/button>/g)];
    expect(buttons).toHaveLength(2);
    expect(buttons.map((match) => match[1])).toEqual(['保存登录名', '保存密码']);
    for (const [tag] of buttons) {
      expect(tag, '空输入时提交按钮必须禁用').toContain('disabled=""');
    }
  });
});
