/**
 * 极简路由：聊天（/ 或 /s/{sessionId}）、总览（/dashboard）、记忆（/memory）、
 * 角色（/persona）、生活（/life）、设置中心（/settings）、
 * 模型与生成（/settings/model）、权限与隐私（/settings/privacy）、
 * 多用户与账号（/settings/identity）、登录名与密码（/settings/account）、
 * 关于与版本（/settings/about）。
 *
 * **设置只放程序与运维**（DESIGN §2）：行为调参回到它所属的领域页 ——
 * 记忆与学习在 `/memory?tab=settings`、对话与主动性在 `/persona?tab=dialogue`、
 * 生活节奏在 `/life?tab=rhythm`。三个旧地址（`/settings/memory`、
 * `/settings/dialogue`、`/settings/life`）见下面的 `LEGACY_REDIRECTS`。
 *
 * 认不出来的路径给 `notFound`，**不再静默落到聊天页**：拼错的链接（/dashbord）
 * 如果能安静地显示聊天页，就没人会发现自己点错了地址，路由表与页面表脱节
 * 也会被藏起来。`notFound` 由 `App.tsx` 渲染成可见的「无法识别的路由」面板。
 *
 * 用 History API 而不是 hash：后端 SPA 兜底会把未知路径交回 index.html。
 * 路由中的 sessionId 只是界面指针，权威历史仍从服务端读取。
 */
import { useSyncExternalStore } from 'react';

export type Route =
  | { name: 'dashboard' }
  | { name: 'chat'; sessionId: string | null }
  | { name: 'now' }
  | { name: 'settings' }
  | { name: 'model' }
  | { name: 'memory' }
  | { name: 'privacy' }
  | { name: 'persona' }
  | { name: 'identity' }
  | { name: 'account' }
  | { name: 'about' }
  /** 旧地址命中精确重定向表：由壳层用 `replace` 换掉，不是一页内容。 */
  | { name: 'legacyRedirect'; to: string }
  /** 认不出来的 pathname。它不是一页"内容"，而是"这个地址不存在"。 */
  | { name: 'notFound' };

/**
 * 旧路径重定向表（DESIGN §5，**精确表**）。
 *
 * 只有三个地址：设置瘦身把 ④记忆与学习、⑤对话与主动性、⑧生活与活动 的调参
 * 迁回了它们所属的领域页（用户拍板：设置只放程序与运维）。这三条地址曾经是
 * 真实入口，书签、聊天里的旧链接、别人的文档都还指着它们。
 *
 * 为什么是**精确表**而不是 `/settings/*` 通配重定向到 `/settings`：
 * 通配会把「拼错的设置子路径」（`/settings/memroy`）也悄悄导到一个能用的页面，
 * 于是没人再发现自己点错了地址——那正是 `notFound` 要暴露的东西。
 * 表外路径一律 `notFound`，由 `dashboard.test.ts` 的「表外必须 notFound」锁住。
 *
 * 目标带 query（tab 深链），不引 hash 路由：`/memory?tab=settings` 仍是 memory 路由。
 */
export const LEGACY_REDIRECTS: Readonly<Record<string, string>> = {
  '/settings/memory': '/memory?tab=settings',
  '/settings/dialogue': '/persona?tab=dialogue',
  '/settings/life': '/life?tab=rhythm',
};

export function parseRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, '') || '/';
  // 重定向表先判：它是精确表，与下面任何真实路由都不重叠（三条旧地址已经从
  // 路由表里删掉，否则「迁出」只是加了一个别名，设置里仍然能直接进去）。
  const redirected = LEGACY_REDIRECTS[path];
  if (redirected) return { name: 'legacyRedirect', to: redirected };
  // 子路由必须先判，否则 /settings/model 会被 /settings 吃掉
  if (path === '/') return { name: 'chat', sessionId: null };
  if (path === '/dashboard') return { name: 'dashboard' };
  if (path === '/life') return { name: 'now' };
  if (path === '/memory') return { name: 'memory' };
  if (path === '/settings/model') return { name: 'model' };
  if (path === '/settings/privacy') return { name: 'privacy' };
  if (path === '/settings/about') return { name: 'about' };
  if (path === '/settings') return { name: 'settings' };
  if (path === '/persona') return { name: 'persona' };
  if (path === '/settings/identity') return { name: 'identity' };
  if (path === '/settings/account') return { name: 'account' };
  const match = /^\/s\/([^/]+)$/.exec(path);
  if (match && match[1]) {
    try {
      return { name: 'chat', sessionId: decodeURIComponent(match[1]) };
    } catch {
      return { name: 'chat', sessionId: match[1] };
    }
  }
  return { name: 'notFound' };
}

/**
 * 重定向目标 → 目标页的路由。
 *
 * 只用于**首帧**渲染：地址在这一帧还是旧的（壳层在 effect 里 `replace`），
 * 若先渲染 404 再跳，用户会看见一下「无法识别的路由」。所以直接渲染目标页。
 * 目标页的 tab 由 `tabs.ts` 的 `*TabFromHref` 推导，它们也认那三条旧路径，
 * 于是首帧开的 tab 与 `replace` 之后一致，不会先开错再跳一次。
 *
 * 表里若有人把目标写成另一个旧地址（不该发生，`routePath` 也不给
 * `legacyRedirect` 生成地址），这里返回 `notFound` 而不是递归。
 */
export function legacyRedirectTarget(to: string): Route {
  const path = to.split('?')[0] ?? '/';
  const route = parseRoute(path);
  return route.name === 'legacyRedirect' ? { name: 'notFound' } : route;
}


export function routePath(route: Route): string {
  switch (route.name) {
    case 'dashboard':
      return '/dashboard';
    case 'now':
      return '/life';
    case 'settings':
      return '/settings';
    case 'model':
      return '/settings/model';
    case 'memory':
      return '/memory';
    case 'privacy':
      return '/settings/privacy';
    case 'persona':
      return '/persona';
    case 'identity':
      return '/settings/identity';
    case 'account':
      return '/settings/account';
    case 'about':
      return '/settings/about';
    case 'chat':
      return route.sessionId ? `/s/${encodeURIComponent(route.sessionId)}` : '/';
    case 'legacyRedirect':
      // 旧地址本身就是可导航的：壳层要 `replace` 到它。返回目标而不是旧地址，
      // 是为了让「重定向后应该停在哪」只有一个答案（测试直接用它断言）。
      return route.to;
    case 'notFound':
      // `notFound` 是「地址本身不存在」，不能反过来生成一个地址：
      // 返回一个可导航 URL 会让调用方（或未来的代码）把错误悄悄藏起来，
      // 例如错误地跳回聊天页——那正是这次要修掉的行为。
      throw new Error('notFound 路由没有可导航的 URL');
  }
}

function subscribe(callback: () => void): () => void {
  window.addEventListener('popstate', callback);
  return () => window.removeEventListener('popstate', callback);
}

function getSnapshot(): string {
  return `${window.location.pathname}${window.location.search}`;
}

export function useRoute(): Route {
  const location = useSyncExternalStore(subscribe, getSnapshot, () => '/');
  return parseRoute(location.split('?')[0] ?? '/');
}

/**
 * 当前地址（pathname + search）。
 *
 * `useRoute` 故意丢掉 query：路由名不该由 tab 之类的东西决定（`/memory?tab=settings`
 * 仍是 memory 路由）。但 tab 深链（DESIGN §1：tab 用 query，不引 hash 路由）需要
 * 读到 query，所以单独给一个不解析的 hook。
 */
export function useLocationHref(): string {
  return useSyncExternalStore(subscribe, getSnapshot, () => '/');
}

export function navigate(path: string, options: { replace?: boolean } = {}): void {
  if (options.replace) {
    window.history.replaceState(null, '', path);
  } else {
    window.history.pushState(null, '', path);
  }
  window.dispatchEvent(new PopStateEvent('popstate'));
}
