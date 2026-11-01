/**
 * 左导航（桌面 56px rail ↔ 200px 展开）与窄屏抽屉（portal 到 body）。
 *
 * 视觉参考 `shots/ia-research/proto/`（A 方案已通过），但**没有**照抄那边的
 * 代码结构：原型是 `data-route` + `innerHTML` 拼字符串的单页演示，这里接的是
 * 真实路由（`routePath()`）与真实的登录态壳层，导航项由数据表驱动，
 * 当前项用 `aria-current="page"` 表达，不靠 class 名。
 *
 * 两条硬约束决定了这里的形状：
 * 1. **顺序是产品契约**（用户拍板 A 方案）：总览 → 对话 → 记忆 → 人设 → 生活，
 *    底部固定「设置」+ 版本号。顺序写在 `PRIMARY_NAV` 里，测试按顺序断言。
 * 2. 抽屉必须 **portal 到 body**：`.app-header` 有 `backdrop-filter: blur(10px)`，
 *    它会让 header 成为 `position: fixed` 的包含块——抽屉若留在 header 里，
 *    `inset: 0` 只盖住 header 自己那 56px，正文照样露在下面。
 */
import { useRef, type ReactElement } from 'react';
import { createPortal } from 'react-dom';
import { legacyRedirectTarget, type Route } from '../route';
import { useFocusTrap } from '../shell';
import {
  ChatIcon,
  ChevronIcon,
  CloseIcon,
  DashboardIcon,
  LifeIcon,
  LogoutIcon,
  MemoryIcon,
  PersonaIcon,
  SettingsIcon,
} from './Icons';

/** 一级导航项的身份（与路由名分开：`now` 路由对应的是「生活」这一项）。 */
export type NavSectionId = 'dashboard' | 'chat' | 'memory' | 'persona' | 'life' | 'settings';

export interface NavItemSpec {
  id: NavSectionId;
  /** E2E / 单测的公开选择器；改名必须同步改测试。 */
  testid: string;
  label: string;
  path: string;
}

/**
 * 一级导航的顺序与入口。**顺序即契约**（总览是登录后的落地页，排在第一位）。
 *
 * 手写而不是从 `Route` 派生：派生出来的期望值会跟着实现一起消失，
 * 删掉一项时测试仍然全绿。这里少一项、换一次顺序，`nav.test.ts` 都会红。
 */
export const PRIMARY_NAV: readonly NavItemSpec[] = [
  { id: 'dashboard', testid: 'nav-dashboard', label: '总览', path: '/dashboard' },
  { id: 'chat', testid: 'nav-chat', label: '对话', path: '/' },
  { id: 'memory', testid: 'nav-memory', label: '记忆', path: '/memory' },
  { id: 'persona', testid: 'nav-persona', label: '人设', path: '/persona' },
  { id: 'life', testid: 'nav-life', label: '生活', path: '/life' },
];

/** 底部固定项：设置（程序与运维）。版本号 badge 单独渲染。 */
export const SETTINGS_NAV: NavItemSpec = {
  id: 'settings',
  testid: 'nav-settings',
  label: '设置',
  path: '/settings',
};

/** 版本号 badge 的落点：`/settings/about`（「关于与维护」留在这里，用户已拍板）。 */
export const VERSION_NAV: NavItemSpec = {
  id: 'settings',
  testid: 'nav-version',
  label: '版本',
  path: '/settings/about',
};

const ICONS: Record<NavSectionId, ReactElement> = {
  dashboard: <DashboardIcon />,
  chat: <ChatIcon />,
  memory: <MemoryIcon />,
  persona: <PersonaIcon />,
  life: <LifeIcon />,
  settings: <SettingsIcon />,
};

/**
 * 路由 → 高亮哪一项。
 *
 * - `/` 与 `/s/:id` 都算「对话」（`/s/:id` 只是当前会话的指针，不是新的一页）；
 * - `/settings` 与它下面所有子页都高亮「设置」——这是 SHOTS_REPORT §5.7 里
 *   「打开设置子页时顶栏没有任何一项是当前页」的直接修复；
 * - `/life` 对应「生活」（路由名仍叫 `now`，重命名是后面步骤的事）。
 *
 * **旧路径（Step 4 迁出设置的那三条）按目标页的高亮算**，不按地址栏前缀：
 * `/settings/memory` 现在只是一个 `replace` 前的中间态，它真正会停在
 * `/memory?tab=settings`——高亮「设置」再闪成「记忆」等于把中间态画给用户看。
 * 目标页的高亮由 `legacyRedirectTarget` 给出，与 `replace` 之后完全一致。
 */
export function activeNavId(route: Route): NavSectionId | null {
  switch (route.name) {
    case 'dashboard':
      return 'dashboard';
    case 'chat':
      return 'chat';
    case 'memory':
      return 'memory';
    case 'persona':
      return 'persona';
    case 'now':
      return 'life';
    case 'settings':
    case 'model':
    case 'privacy':
    case 'identity':
    case 'account':
    case 'about':
      return 'settings';
    case 'legacyRedirect':
      // 递归只有一层：`legacyRedirectTarget` 对"目标又是旧地址"返回 `notFound`。
      return activeNavId(legacyRedirectTarget(route.to));
    case 'notFound':
      // 404 页不属于任何一项：宁可不标，也不要把人指到一个不对的地方。
      return null;
    default: {
      const exhaustive: never = route;
      return exhaustive;
    }
  }
}

function NavItem({
  item,
  active,
  expanded,
  onGo,
}: {
  item: NavItemSpec;
  active: boolean;
  expanded: boolean;
  onGo: (path: string) => void;
}): ReactElement {
  return (
    <button
      type="button"
      data-testid={item.testid}
      className={`sidenav__item${active ? ' sidenav__item--active' : ''}`}
      aria-current={active ? 'page' : undefined}
      title={expanded ? undefined : item.label}
      onClick={() => onGo(item.path)}
    >
      <span className="sidenav__icon" aria-hidden="true">
        {ICONS[item.id]}
      </span>
      <span className="sidenav__label">{item.label}</span>
    </button>
  );
}

interface VersionBadgeProps {
  version: string | null;
  onGo: (path: string) => void;
}

/**
 * 版本号 badge（底部）。读不到版本时写 `v—` 而不是编一个数字：
 * 一个假装是版本号的占位符比空白更容易被当真。
 *
 * 更新可用 badge 是 Step 5 的事（DESIGN §6），这里只做版本号。
 */
export function VersionBadge({ version, onGo }: VersionBadgeProps): ReactElement {
  const text = version ? `v${version}` : 'v—';
  return (
    <button
      type="button"
      data-testid="nav-version"
      className="sidenav__version"
      title={version ? `当前版本 ${version}，打开「关于与维护」` : '版本号未读到，打开「关于与维护」'}
      aria-label={version ? `版本 ${version}` : '版本号未读到'}
      onClick={() => onGo(VERSION_NAV.path)}
    >
      <span className="sidenav__version-dot" aria-hidden="true" />
      <span className="sidenav__version-text">{text}</span>
    </button>
  );
}

export interface SidenavProps {
  active: NavSectionId | null;
  expanded: boolean;
  version: string | null;
  onToggle: () => void;
  onGo: (path: string) => void;
}

/** 桌面左导航：56px 图标 rail ↔ 200px 展开。 */
export function Sidenav({ active, expanded, version, onToggle, onGo }: SidenavProps): ReactElement {
  return (
    <aside
      className="sidenav"
      data-testid="sidenav"
      data-expanded={expanded ? 'true' : 'false'}
      aria-label="侧边导航"
    >
      <button
        type="button"
        className="sidenav__toggle"
        data-testid="nav-toggle"
        aria-expanded={expanded}
        aria-label={expanded ? '收起导航' : '展开导航'}
        title={expanded ? '收起导航' : '展开导航'}
        onClick={onToggle}
      >
        <ChevronIcon className={expanded ? 'sidenav__chevron' : 'sidenav__chevron sidenav__chevron--rail'} />
      </button>
      <nav className="sidenav__nav" aria-label="主导航">
        <ul className="sidenav__list">
          {PRIMARY_NAV.map((item) => (
            <li key={item.testid} className="sidenav__list-item">
              <NavItem item={item} active={active === item.id} expanded={expanded} onGo={onGo} />
            </li>
          ))}
        </ul>
        <div className="sidenav__foot">
          <NavItem
            item={SETTINGS_NAV}
            active={active === SETTINGS_NAV.id}
            expanded={expanded}
            onGo={onGo}
          />
          <VersionBadge version={version} onGo={onGo} />
        </div>
      </nav>
    </aside>
  );
}

export interface NavDrawerProps {
  open: boolean;
  active: NavSectionId | null;
  version: string | null;
  /** 抽屉底部账号区显示的名字（`display_name || username`）。 */
  accountName: string;
  onClose: () => void;
  onGo: (path: string) => void;
  onLogout: () => void;
}

/**
 * 抽屉的**内容**（不含 portal 外壳）。
 *
 * 单独拆出来是为了能在 `nav.test.ts` 里用 `renderToStaticMarkup` 断言它的
 * 结构与无障碍属性——`react-dom/server` 不支持 portal，整棵渲染会直接抛错。
 * 真实抽屉只是「portal + scrim + 这个面板」，E2E 量的仍然是同一份 DOM。
 */
export function NavDrawerPanel({
  active,
  version,
  accountName,
  onClose,
  onGo,
  onLogout,
}: Omit<NavDrawerProps, 'open'>): ReactElement {
  return (
    <aside className="nav-drawer__panel" role="dialog" aria-modal="true" aria-label="导航">
      <div className="nav-drawer__head">
        <span className="nav-drawer__brand">
          <span className="brand__dot" aria-hidden="true" />
          <span className="brand__text">ling</span>
        </span>
        <button
          type="button"
          className="icon-button nav-drawer__close"
          data-testid="nav-close"
          aria-label="关闭导航"
          onClick={onClose}
        >
          <CloseIcon />
        </button>
      </div>

      <nav className="sidenav__nav" aria-label="主导航">
        <ul className="sidenav__list">
          {PRIMARY_NAV.map((item) => (
            <li key={item.testid} className="sidenav__list-item">
              <NavItem item={item} active={active === item.id} expanded onGo={onGo} />
            </li>
          ))}
        </ul>
        <div className="sidenav__foot">
          <NavItem item={SETTINGS_NAV} active={active === SETTINGS_NAV.id} expanded onGo={onGo} />
          <VersionBadge version={version} onGo={onGo} />
        </div>
      </nav>

      {/*
        账号区放在 `aria-label="主导航"` 这个 landmark **之外**：
        它是「退出」这个动作 + 当前是谁，不是一个导航目的地。放在里面会让
        「主导航里有哪些项」的断言把账号/退出也算进去（桌面 rail 没有它们，
        两边就不再是同一套导航），也不符合 nav landmark 的语义。
        数据里用户名与退出按钮都在这一区（`nav-account` / `nav-logout`）。
      */}
      <div className="nav-drawer__account" data-testid="nav-account" role="group" aria-label="账号">
        <span className="nav-drawer__user" title={accountName}>
          {accountName}
        </span>
        <button
          type="button"
          className="sidenav__item nav-drawer__logout"
          data-testid="nav-logout"
          onClick={onLogout}
        >
          <span className="sidenav__icon" aria-hidden="true">
            <LogoutIcon />
          </span>
          <span className="sidenav__label">退出登录</span>
        </button>
      </div>
    </aside>
  );
}

/**
 * 窄屏导航抽屉（portal 到 `document.body`）。
 *
 * 关闭时**整棵卸载**：留在 DOM 里的话，`[data-testid^="nav-"]` 会同时命中
 * 隐藏的抽屉项和顶栏 brand，E2E 点到的可能是不可见的那个。
 */
export function NavDrawer({
  open,
  active,
  version,
  accountName,
  onClose,
  onGo,
  onLogout,
}: NavDrawerProps): ReactElement | null {
  const rootRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(rootRef, open, onClose);

  if (!open) return null;
  if (typeof document === 'undefined') return null;

  return createPortal(
    <div className="nav-drawer" data-testid="nav-drawer" ref={rootRef}>
      <button
        type="button"
        tabIndex={-1}
        className="nav-drawer__scrim"
        data-testid="nav-scrim"
        aria-label="关闭导航"
        onClick={onClose}
      />
      <NavDrawerPanel
        active={active}
        version={version}
        accountName={accountName}
        onClose={onClose}
        onGo={onGo}
        onLogout={onLogout}
      />
    </div>,
    document.body,
  );
}
