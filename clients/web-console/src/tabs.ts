/**
 * 领域页的 tab 推导（DESIGN §1：tab 用 query，**不引 hash 路由**）。
 *
 * 为什么 tab 要能深链：设置瘦身把三个调参页迁回了它们所属的领域页
 * （`/settings/memory` → `/memory?tab=settings`、`/settings/dialogue` →
 * `/persona?tab=dialogue`、`/settings/life` → `/life?tab=rhythm`）。旧地址是
 * `replace` 掉的，但**新地址必须是可分享、可刷新、可后退的 URL**——
 * 否则「迁出设置」只是把入口藏起来，谁也没法直接把调参页发给别人。
 *
 * 三个 `*TabFromHref` 都**同时认旧路径**：重定向发生在壳层的 effect 里，
 * 首帧地址还是旧的；若这里只认 `?tab=`，首帧就会先开错 tab 再跳一次。
 * 记忆页的 `memoryTabFromHref`（在 `MemoryPage.tsx` 里）是同一个约定，
 * 这里只是把另外两页补齐（记忆页那个不能动：它被并行任务锁着）。
 *
 * 认不出来的 `tab` 值一律回到第一个 tab：**不猜**。拼错的 `?tab=dialgoue`
 * 如果悄悄落到调参页，人就再也不会发现自己的链接拼错了。
 */

export type PersonaTab = 'persona' | 'dialogue';
export type LifeTab = 'now' | 'rhythm';

/** 地址拆成 pathname + query（query 不解析成对象：只需读一个 `tab`）。 */
function splitHref(href: string): { path: string; params: URLSearchParams } {
  const [rawPath = '', rawQuery = ''] = href.split('?');
  const path = rawPath.replace(/\/+$/, '') || '/';
  return { path, params: new URLSearchParams(rawQuery) };
}

/**
 * 人设页的两个 tab：角色设定（①身份与人格）与对话与主动性（⑤）。
 *
 * ⑤ 原来在 `/settings/dialogue`。放进人设页而不是设置页的理由是**用户拍板**
 * 的那条判据：设置只放程序与运维，改「她要不要开口」是行为，不是运维。
 */
export function personaTabFromHref(href: string): PersonaTab {
  const { path, params } = splitHref(href);
  if (params.get('tab') === 'dialogue') return 'dialogue';
  if (path === '/settings/dialogue') return 'dialogue';
  return 'persona';
}

export function personaTabHref(tab: PersonaTab): string {
  return tab === 'dialogue' ? '/persona?tab=dialogue' : '/persona';
}

/**
 * 生活页的两个 tab：它现在在干嘛（只读状态）与生活节奏（⑧）。
 *
 * ⑧ 原来在 `/settings/life`。注意路由名仍叫 `now`（`/life`），
 * tab 名不能跟路由名撞：`now` 是内容 tab，`rhythm` 是调参 tab。
 */
export function lifeTabFromHref(href: string): LifeTab {
  const { path, params } = splitHref(href);
  if (params.get('tab') === 'rhythm') return 'rhythm';
  if (path === '/settings/life') return 'rhythm';
  return 'now';
}

export function lifeTabHref(tab: LifeTab): string {
  return tab === 'rhythm' ? '/life?tab=rhythm' : '/life';
}
