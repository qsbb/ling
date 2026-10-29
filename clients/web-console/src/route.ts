/**
 * 极简路由：聊天（/ 或 /s/{sessionId}）、生活（/life）、设置中心（/settings）、
 * 模型与生成（/settings/model）、记忆与学习（/settings/memory）、
 * 对话与主动性（/settings/dialogue）、生活与活动（/settings/life）、权限与隐私（/settings/privacy）、角色（/persona）、
 * 多用户与账号（/settings/identity）。
 *
 * 设置下挂子路由而不是平铺，是因为分类法有 9 类；平铺会让路由和导航各维护一份。
 *
 * 用 History API 而不是 hash：后端 SPA 兜底会把未知路径交回 index.html。
 * 路由中的 sessionId 只是界面指针，权威历史仍从服务端读取。
 */
import { useSyncExternalStore } from 'react';

export type Route =
  | { name: 'chat'; sessionId: string | null }
  | { name: 'now' }
  | { name: 'settings' }
  | { name: 'model' }
  | { name: 'memory' }
  | { name: 'dialogue' }
  | { name: 'life' }
  | { name: 'privacy' }
  | { name: 'persona' }
  | { name: 'identity' };

export function parseRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, '') || '/';
  // 子路由必须先判，否则 /settings/model 会被 /settings 吃掉
  if (path === '/life') return { name: 'now' };
  if (path === '/settings/model') return { name: 'model' };
  if (path === '/settings/memory') return { name: 'memory' };
  if (path === '/settings/dialogue') return { name: 'dialogue' };
  if (path === '/settings/life') return { name: 'life' };
  if (path === '/settings/privacy') return { name: 'privacy' };
  if (path === '/settings') return { name: 'settings' };
  if (path === '/persona') return { name: 'persona' };
  if (path === '/settings/identity') return { name: 'identity' };
  const match = /^\/s\/([^/]+)$/.exec(path);
  if (match && match[1]) {
    try {
      return { name: 'chat', sessionId: decodeURIComponent(match[1]) };
    } catch {
      return { name: 'chat', sessionId: match[1] };
    }
  }
  return { name: 'chat', sessionId: null };
}

export function routePath(route: Route): string {
  switch (route.name) {
    case 'now':
      return '/life';
    case 'settings':
      return '/settings';
    case 'model':
      return '/settings/model';
    case 'memory':
      return '/settings/memory';
    case 'dialogue':
      return '/settings/dialogue';
    case 'life':
      return '/settings/life';
    case 'privacy':
      return '/settings/privacy';
    case 'persona':
      return '/persona';
    case 'identity':
      return '/settings/identity';
    case 'chat':
      return route.sessionId ? `/s/${encodeURIComponent(route.sessionId)}` : '/';
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

export function navigate(path: string, options: { replace?: boolean } = {}): void {
  if (options.replace) {
    window.history.replaceState(null, '', path);
  } else {
    window.history.pushState(null, '', path);
  }
  window.dispatchEvent(new PopStateEvent('popstate'));
}
