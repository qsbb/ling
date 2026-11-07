/**
 * 极简路由：聊天（/ 或 /s/{sessionId}）、总览（/dashboard）、记忆（/memory）、
 * 角色（/persona）、关系（/relationships）、生活（/life）、日志（/logs）、
 * 设置中心（/settings）、模型与生成（/settings/model）、
 * 权限与隐私（/settings/privacy）、接入器（/settings/connectors）、
 * 登录名与密码（/settings/account）、关于与版本（/settings/about）。
 *
 * **设置只放程序与运维**（DESIGN §2）：行为调参回到它所属的领域页 ——
 * 记忆与学习在 `/memory?tab=settings`、对话与主动性在 `/persona?tab=dialogue`、
 * 生活节奏在 `/life?tab=rhythm`。三个旧地址（`/settings/memory`、
 * `/settings/dialogue`、`/settings/life`）见下面的 `LEGACY_REDIRECTS`。
 *
 * 「关系」同理**不留在设置里**：谁和谁是一伙的（`subjects` / 账号合并）是内容，
 * 不是实例配置。它曾经是 `/settings/identity`（「多用户与账号」），用户拍板
 * 挪进一级导航并改名；旧地址同样在 `LEGACY_REDIRECTS` 里精确命中。
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
  /** 「关系」（从设置里迁出的一级页，原 `/settings/identity`「多用户与账号」）。 */
  | { name: 'relationships' }
  | { name: 'account' }
  | { name: 'about' }
  /**
   * 接入器（`GET /v1/settings/connectors`）：她从哪条线收发消息。
   * 是设置子页——渠道配置属于实例运维，不是她那一边的内容。
   */
  | { name: 'connectors' }
  /** 运行日志（`GET /v1/logs`）。一级导航项，不是设置子页。 */
  | { name: 'logs' }
  /** 旧地址命中精确重定向表：由壳层用 `replace` 换掉，不是一页内容。 */
  | { name: 'legacyRedirect'; to: string }
  /** 认不出来的 pathname。它不是一页"内容"，而是"这个地址不存在"。 */
  | { name: 'notFound' };

/**
 * 旧路径重定向表（DESIGN §5，**精确表**）。
 *
 * 两类来源，四条地址：
 *
 * 1. **行为调参回领域页**（设置只放程序与运维）：④记忆与学习、⑤对话与主动性、
 *    ⑧生活与活动 的调参迁回了它们所属的领域页（用户拍板）。三条目标带 tab 深链。
 * 2. **页面搬家**：`/settings/identity`（「多用户与账号」）成了**一级导航**
 *    「关系」，地址是 `/relationships`。这一条是整页搬家，没有 tab 可带——
 *    所以目标不带 query。判定标准与上一条相同：谁和谁是一伙的是内容，
 *    不是实例配置，不该只有从设置里点两层才找得到。
 *
 * 为什么要**精确表**而不是 `/settings/*` 通配重定向到 `/settings`：
 * 通配会把「拼错的设置子路径」（`/settings/memroy`、`/settings/identty`）
 * 也悄悄导到一个能用的页面，于是没人再发现自己点错了地址——那正是 `notFound`
 * 要暴露的东西。表外路径一律 `notFound`，由 `dashboard.test.ts` 的
 * 「表外必须 notFound」锁住。
 *
 * 任何一条都不引 hash 路由：`/memory?tab=settings` 仍是 memory 路由。
 */
export const LEGACY_REDIRECTS: Readonly<Record<string, string>> = {
  '/settings/memory': '/memory?tab=settings',
  '/settings/dialogue': '/persona?tab=dialogue',
  '/settings/life': '/life?tab=rhythm',
  // 搬家，不是 tab 深链：目标是整页，「关系」页上没有 tab。
  '/settings/identity': '/relationships',
};

export function parseRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, '') || '/';
  // 重定向表先判：它是精确表，与下面任何真实路由都不重叠（这四条旧地址已经从
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
  if (path === '/settings/connectors') return { name: 'connectors' };
  if (path === '/settings') return { name: 'settings' };
  if (path === '/persona') return { name: 'persona' };
  // 「关系」是一级页：它和 `/persona`、`/life` 平级，不是 `/settings/*` 的子项。
  // 旧地址 `/settings/identity` 在上面的重定向表里，这里**不再**给 identity 留路由，
  // 否则旧地址会同时是一条能直接渲染的路由，重定向就成了摆设。
  if (path === '/relationships') return { name: 'relationships' };
  // `/logs` 是一级页，别写进 `/settings/*` 那一串：它是常驻的运维入口，
  // 不是设置里的子项（`activeNavId` 也跟着把它算成「日志」而不是「设置」）。
  if (path === '/logs') return { name: 'logs' };
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
 * 目标页的 tab 由 `tabs.ts` 的 `*TabFromHref` 推导，它们也认那三条带 tab 的旧路径，
 * 于是首帧开的 tab 与 `replace` 之后一致，不会先开错再跳一次。
 * `/settings/identity` → `/relationships` 是整页搬家，没有 tab 参与，走同一条路径。
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
    case 'relationships':
      return '/relationships';
    case 'account':
      return '/settings/account';
    case 'about':
      return '/settings/about';
    case 'connectors':
      return '/settings/connectors';
    case 'logs':
      return '/logs';
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
