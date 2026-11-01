import { useCallback, useEffect, useState, type ReactElement } from 'react';
import { api, setUnauthorizedHandler } from './api';
import { ChatPage } from './components/ChatPage';
import { AboutPage } from './components/AboutPage';
import { AccountPage } from './components/AccountPage';
import { ChangePasswordView } from './components/ChangePasswordView';
import { DashboardPage } from './components/DashboardPage';
import { LoginView } from './components/LoginView';
import { ConsentPage } from './components/ConsentPage';
import { LifeNowPage } from './components/LifeNowPage';
import { MemoryPage } from './components/MemoryPage';
import { PersonaPage } from './components/PersonaPage';
import { IdentityPage } from './components/IdentityPage';
import { SettingsHub } from './components/SettingsHub';
import { SettingsPage } from './components/SettingsPage';
import { TestModeBanner } from './components/TestModeBanner';
import { AlertIcon, LogoutIcon, MenuIcon, NavIcon } from './components/Icons';
import { NavDrawer, Sidenav, activeNavId } from './components/Navigation';
import { useNarrowLayout, useNavExpanded, useOverlay } from './shell';
// 总览页的布局补丁单独成文件：styles.css 由并行任务在改，不去挤那份文件。
import './dashboard.css';
import { legacyRedirectTarget, navigate, routePath, useRoute, type Route } from './route';
import type { AuthResponse, UserInfo } from './types';

type AuthState =
  | { status: 'loading' }
  | { status: 'anonymous' }
  | { status: 'authenticated'; user: UserInfo; testMode: boolean };

/**
 * 主区页面分发。
 *
 * 这里是**唯一**决定「哪条路由渲染哪个页面」的地方，并且刻意写成
 * `switch` + `default` 里的 `never` 赋值：`Route` 是封闭联合，新增一个
 * 路由名却忘了加 `case` 时 `pnpm exec tsc --noEmit` 会直接报错。
 *
 * 之前这里是 `<main>` 里一条三元链，尾巴兜底成 `<PersonaPage/>`：
 * `/dashboard` 漏了分支没人发现，于是总览被塞进 `<header>`、主区显示的是
 * 角色设定表单——三元链的兜底把「漏了一个分支」这个编译期错误变成了
 * 一个静默的运行时错误。
 */
export function mainPage(props: MainPageProps): ReactElement {
  const { route, user, sidebarOpen, onCloseSidebar, onTestMode, onUserChange, onLogout, onGo } = props;
  const goSettings = () => onGo(routePath({ name: 'settings' }));
  const goChat = () => onGo(routePath({ name: 'chat', sessionId: null }));
  const goPersona = () => onGo(routePath({ name: 'persona' }));

  switch (route.name) {
    case 'dashboard':
      return (
        <DashboardPage
          onOpenNow={() => onGo(routePath({ name: 'now' }))}
          onOpenChat={goChat}
          onOpenSettings={goSettings}
        />
      );
    case 'chat':
      return (
        <ChatPage
          sessionId={route.sessionId}
          user={user}
          sidebarOpen={sidebarOpen}
          onCloseSidebar={onCloseSidebar}
          onTestMode={onTestMode}
          onOpenSettings={goSettings}
          onOpenPersona={goPersona}
          onLogout={onLogout}
        />
      );
    case 'now':
      return <LifeNowPage onBack={goChat} />;
    case 'settings':
      return <SettingsHub onBack={goChat} onOpen={onGo} />;
    case 'model':
      return <SettingsPage onBack={goSettings} onTestMode={onTestMode} />;
    case 'memory':
      return <MemoryPage />;
    case 'privacy':
      return <ConsentPage onBack={goSettings} />;
    case 'account':
      return (
        <AccountPage
          username={user.username}
          onBack={goSettings}
          onRenamed={(name) => onUserChange({ ...user, username: name })}
          onLogout={onLogout}
        />
      );
    case 'identity':
      return <IdentityPage onBack={goSettings} />;
    case 'about':
      return <AboutPage onBack={goSettings} />;
    case 'persona':
      return <PersonaPage onBack={goChat} />;
    case 'legacyRedirect':
      // 旧地址（`/settings/memory` 等三条）的首帧：`replace` 在壳层的 effect 里，
      // 所以这一帧地址还是旧的。这里**直接渲染目标页的内容**，而不是先画一帧
      // 「无法识别的路由」——否则每次点旧书签都会闪一下 404。
      // 递归只有一层：`legacyRedirectTarget` 对"目标又是旧地址"返回 `notFound`。
      return mainPage({ ...props, route: legacyRedirectTarget(route.to) });
    case 'notFound':
      // `parseRoute` 对认不出来的 pathname 给的就是它（拼错的链接、过期书签）。
      // 这里渲染可见的「无法识别的路由」面板，并给一个回总览的入口——
      // 不是静默跳聊天，也不是死胡同。
      return <NotFoundPage onGo={onGo} />;
    default:
      // 这一行既是穷尽性检查（新增 Route 分支却忘了 case → tsc 报错），
      // 也是唯一还剩的类型：never。运行时真拿到没见过的路由名时，给一个
      // 明确的「无法识别的路由」，而不是把角色页当成兜底顶上去。
      return <UnknownRoutePage route={route} onGo={onGo} />;
  }
}

/**
 * 未知路由的可见兜底。
 *
 * 不静默：既写进界面（人可以看见并回总览），也带 `data-testid`（测试可断言）。
 * 为什么不是「跳回聊天」：静默跳转会让人以为链接没错，而 404 式的页面
 * 才能暴露「路由表与页面表脱节」。
 */
export function NotFoundPage({
  detail,
  onGo,
}: {
  detail?: string;
  /** 走壳层的跳转入口（和页头导航同一条路径，不绕过侧栏收起的逻辑）。 */
  onGo: (path: string) => void;
}): ReactElement {
  return (
    <div className="page">
      <div className="dash-error" role="alert" data-testid="unknown-route">
        <AlertIcon />
        <div>
          <strong>无法识别的路由</strong>
          <p>
            {detail ? `这个地址（${detail}）没有对应的界面。` : '这个地址没有对应的界面。'}
          </p>
        </div>
        <div className="dash-error__actions">
          <button
            type="button"
            className="button"
            data-testid="unknown-route-home"
            onClick={() => onGo(routePath({ name: 'dashboard' }))}
          >
            回到总览
          </button>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => onGo(routePath({ name: 'chat', sessionId: null }))}
          >
            回到聊天
          </button>
        </div>
      </div>
    </div>
  );
}

export function UnknownRoutePage({
  route,
  onGo,
}: {
  route: never;
  onGo: (path: string) => void;
}): ReactElement {
  const unknown = route as unknown as { name?: unknown };
  return <NotFoundPage detail={String(unknown?.name ?? '未知')} onGo={onGo} />;
}

export interface MainPageProps {
  route: Route;
  user: UserInfo;
  /** 聊天页的会话列表开关；由壳层持有，顶栏菜单按钮与页面共用。 */
  sidebarOpen: boolean;
  onCloseSidebar: () => void;
  onTestMode: (value: boolean) => void;
  onUserChange: (user: UserInfo) => void;
  onLogout: () => void;
  /** 关闭侧栏后再跳转；所有导航都走这里，避免某处忘了收侧栏。 */
  onGo: (path: string) => void;
}

export interface AppShellProps {
  route: Route;
  user: UserInfo;
  testMode: boolean;
  onLogout: () => void;
  onTestMode: (value: boolean) => void;
  onUserChange: (user: UserInfo) => void;
}

/**
 * 版本号（底部 badge 用）。
 *
 * 读 `/v1/version` 一次；失败就退回 `null`（界面显示 `v—`）。
 * 不编一个数字：假装是版本号的占位符比空白更容易被当真。
 * 更新可用 badge 是 DESIGN §6 Step 5 的事，这里只做版本号。
 */
function useAppVersion(): string | null {
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .version()
      .then((info) => {
        if (!cancelled) setVersion(info.current?.trim() || null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return version;
}

/**
 * 登录后的壳层：页头（brand / 账号区）+ 左导航 + 主区。
 *
 * 单独抽成组件是为了能在 `dashboard.test.ts` 里把它渲染成 HTML，
 * 断言「总览在主区里、页头里没有」——这个缺陷此前没有任何测试能发现，
 * 因为没人能断言页头与主区各装了什么。
 *
 * 三件事收在这里，页面组件一概不碰：
 * 1. **浮层只有一片**（`useOverlay`）：导航抽屉与聊天页会话列表共用一个槽位，
 *    打开一片必然挤掉另一片，不会出现两层 scrim 叠罗汉；
 * 2. **窄屏只渲染抽屉**：≤900px 不渲染桌面 rail，避免隐藏的重复 testid；
 * 3. **离开当前页就收浮层**：路由一变（含浏览器后退）立刻关掉，不从新页面
 *    带着一层遮罩开局。
 *
 * 页头只剩 brand 与账号区（用户名 + 退出）：Step 1 的「总览 / 设置 / 生活 /
 * 角色」这四个过渡入口已在 Step 2 删除，因为它们与左导航逐项重复，而左导航是
 * 「当前页」的唯一权威来源。**一个入口都没丢**：总览/设置/人设/生活在 rail 与
 * 抽屉里，退出在桌面页头、窄屏抽屉底部（`nav-account` / `nav-logout`）。
 *
 * 账号区按同一个断点只渲染一份：桌面在页头，窄屏在抽屉。两边同时渲染会让
 * `nav-logout` 在 DOM 里出现两次（其中一个还 `display:none`），E2E 点到的可能
 * 是藏起来的那个——和 Step 1 不渲染隐藏导航项是同一个理由。
 */
export function AppShell({
  route,
  user,
  testMode,
  onLogout,
  onTestMode,
  onUserChange,
}: AppShellProps) {
  const narrow = useNarrowLayout();
  const overlay = useOverlay();
  const nav = useNavExpanded();
  const version = useAppVersion();
  const active = activeNavId(route);

  const onGo = useCallback(
    (path: string) => {
      overlay.close();
      navigate(path);
    },
    [overlay],
  );

  // 路由变化自动关闭浮层。带上 sessionId：`/s/a → /s/b` 也算换页。
  const routeKey = route.name === 'chat' ? `chat:${route.sessionId ?? ''}` : route.name;
  useEffect(() => {
    overlay.close();
    // 只在路由真的变了时收；overlay.close 是稳定引用。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeKey]);

  /*
   * 旧地址重定向：`parseRoute` 只负责报告「这条旧地址该去哪」（返回
   * `{name:'legacyRedirect', to}`），**改地址栏是壳层的事**——路由表不碰
   * `history`，这样 `route.ts` 仍然是纯函数，单测可以直接断言它。
   *
   * 用 `replace` 不是 `push`：旧书签不该在后退栈里留一层，否则人点一次后退
   * 又被重定向回来，看起来像「后退键坏了」。
   *
   * 依赖是一个字符串而不是 `route` 对象：`parseRoute` 每次都新建对象，
   * 以对象做依赖会每渲染都跑一遍 effect。
   */
  const legacyTo = route.name === 'legacyRedirect' ? route.to : null;
  useEffect(() => {
    if (legacyTo) navigate(legacyTo, { replace: true });
  }, [legacyTo]);

  // 视口从窄屏拉宽（或反过来）时，抽屉不该继续占着屏幕：收掉并解锁滚动。
  useEffect(() => {
    if (!narrow && overlay.active === 'nav') overlay.close();
  }, [narrow, overlay]);

  return (
    <div className="app">
      <header className="app-header">
        {narrow ? (
          <button
            type="button"
            className="icon-button app-header__nav-toggle"
            data-testid="nav-open"
            aria-label="打开导航"
            aria-haspopup="dialog"
            aria-expanded={overlay.active === 'nav'}
            onClick={() => overlay.open('nav')}
          >
            <NavIcon />
          </button>
        ) : null}
        {/*
          聊天页的会话列表入口：aria-label 不许改（w0_browser.mjs 依赖），
          并且只在 ≤900 是真正的入口——>900 由 nav.css 隐藏（桌面上会话列表
          是常驻的一列，这个按钮点了没有可开可关的东西，见 nav.css 的说明）。
        */}
        {route.name === 'chat' ? (
          <button
            type="button"
            className="icon-button app-header__menu"
            aria-label="打开会话列表"
            aria-expanded={overlay.active === 'session'}
            onClick={() => overlay.open('session')}
          >
            <MenuIcon />
          </button>
        ) : null}
        {/*
          brand 的 testid 从 `nav-chat` 改成 `brand-home`：原先它与左导航的
          「对话」项同名，DOM 里两个 `nav-chat` 会让任何忘了限定作用域的
          E2E 取到 brand（它是 logo，不是导航项）。改名后 `nav-chat` 只剩
          左导航/抽屉里那一处，`w0_browser` / `dashboard_browser` 同步改用
          `brand-home`（它们要的就是「点 logo 回聊天」）。
        */}
        <button
          type="button"
          data-testid="brand-home"
          className="brand"
          onClick={() => onGo(routePath({ name: 'chat', sessionId: null }))}
          aria-label="ling 首页"
        >
          <span className="brand__dot" aria-hidden="true" />
          <span className="brand__text">ling</span>
        </button>
        <div className="app-header__spacer" />
        {/*
          桌面账号区：用户名 + 退出。窄屏不渲染——那里的归宿是抽屉底部的
          `nav-account`（同一个断点只出现一份 `nav-logout`）。
        */}
        {narrow ? null : (
          <div className="app-header__account">
            <span className="app-header__user">{user.display_name || user.username}</span>
            <button
              type="button"
              data-testid="nav-logout"
              className="header-link"
              onClick={onLogout}
            >
              <LogoutIcon size={15} /> 退出
            </button>
          </div>
        )}
      </header>

      {testMode ? <TestModeBanner /> : null}

      <div className="app__body">
        {narrow ? null : (
          <Sidenav
            active={active}
            expanded={nav.expanded}
            version={version}
            onToggle={nav.toggle}
            onGo={onGo}
          />
        )}
        <div className="app__column">
          <main className="app-main">
            {mainPage({
              route,
              user,
              sidebarOpen: overlay.active === 'session',
              onCloseSidebar: overlay.close,
              onTestMode,
              onUserChange,
              onLogout,
              onGo,
            })}
          </main>
        </div>
      </div>

      {narrow ? (
        <NavDrawer
          open={overlay.active === 'nav'}
          active={active}
          version={version}
          accountName={user.display_name || user.username}
          onClose={overlay.close}
          onGo={onGo}
          onLogout={onLogout}
        />
      ) : null}
    </div>
  );
}

export default function App() {
  const route = useRoute();
  const [auth, setAuth] = useState<AuthState>({ status: 'loading' });

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuth({ status: 'anonymous' });
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setAuth({ status: 'authenticated', user: me.user, testMode: me.test_mode });
      })
      .catch(() => {
        if (!cancelled) setAuth({ status: 'anonymous' });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogin = (response: AuthResponse) => {
    setAuth({ status: 'authenticated', user: response.user, testMode: response.test_mode });
    // 登录后先给总览，而不是直接把聊天框推到人脸上
    navigate('/dashboard');
  };

  const handleLogout = useCallback(() => {
    void api
      .logout()
      .catch(() => undefined)
      .finally(() => {
        setAuth({ status: 'anonymous' });
        navigate('/dashboard');
      });
  }, []);

  const handleTestMode = useCallback((value: boolean) => {
    setAuth((current) =>
      current.status === 'authenticated' && current.testMode !== value
        ? { ...current, testMode: value }
        : current,
    );
  }, []);

  const handleUserChange = useCallback((user: UserInfo) => {
    setAuth((current) => (current.status === 'authenticated' ? { ...current, user } : current));
  }, []);

  if (auth.status === 'loading') {
    return (
      <div className="splash">
        <span className="spinner" aria-hidden="true" />
        <span>ling 正在醒来…</span>
      </div>
    );
  }

  if (auth.status === 'anonymous') {
    return <LoginView onSuccess={handleLogin} />;
  }

  // 首次安装的随机口令还没改：整页接管，不给任何绕过入口。
  // 后端也会在改密前对所有 /v1/auth/* 之外的请求返回 403，这里是同一件事的两侧。
  if (auth.user.must_change_password) {
    return (
      <ChangePasswordView
        username={auth.user.username}
        onSuccess={() => {
          setAuth({
            status: 'authenticated',
            user: { ...auth.user, must_change_password: false },
            testMode: auth.testMode,
          });
        }}
        onLogout={() => {
          void api.logout().catch(() => undefined);
          setAuth({ status: 'anonymous' });
        }}
      />
    );
  }

  return (
    <AppShell
      route={route}
      user={auth.user}
      testMode={auth.testMode}
      onLogout={handleLogout}
      onTestMode={handleTestMode}
      onUserChange={handleUserChange}
    />
  );
}
