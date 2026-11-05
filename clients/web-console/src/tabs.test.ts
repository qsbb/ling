/**
 * 领域页 tab 深链的契约测试（`tabs.ts`）。
 *
 * 为什么单独一个文件：`tabs.ts` 是**纯函数**，没有任何 React 依赖，是这次
 * 「设置瘦身」里最容易被悄悄改坏的接缝——它的输出决定了用户点旧书签后
 * 打开的是调参表单还是内容页。把它锁在这里，就不必每次跑 E2E 才知道错了。
 *
 * 与 `memory.test.ts` 的分工：记忆页的 `memoryTabFromHref` 在
 * `components/MemoryPage.tsx` 里（被并行任务锁着，本任务不改），它的用例
 * 已在 `memory.test.ts`。本文件只覆盖 `tabs.ts` 里的另外两页。
 *
 * 断言三条不变式（对每页都成立）：
 * 1. `?tab=<调参>` → 调参 tab；
 * 2. 无参数 → 默认 tab（内容页）；
 * 3. **未知值 → 默认 tab，且不抛异常**（拼错的 `?tab=dialgoue` 不能让人
 *    落到调参页，也不能让整页白掉）；
 * 4. `*TabHref` 与 `*TabFromHref` **往返一致**（链接可分享、可刷新、可后退）。
 *
 * 旧路径（`/settings/dialogue`、`/settings/life`）也要认：重定向发生在壳层的
 * effect 里，首帧地址还是旧的，只认 `?tab=` 会先开错 tab 再跳一次。
 */
import { describe, expect, it } from 'vitest';
import {
  lifeTabFromHref,
  lifeTabHref,
  personaTabFromHref,
  personaTabHref,
  type LifeTab,
  type PersonaTab,
} from './tabs';

/* ------------------------------------------------------------------ 人设页 */

describe('人设页 tab：?tab=dialogue ↔ 对话与主动性', () => {
  it('?tab=dialogue 深链开在调参 tab', () => {
    expect(personaTabFromHref('/persona?tab=dialogue')).toBe('dialogue');
    // query 顺序不该影响结果（有人手工调换过参数顺序的链接仍要能用）
    expect(personaTabFromHref('/persona?foo=1&tab=dialogue')).toBe('dialogue');
  });

  it('没有参数时停在默认 tab（角色设定），不是调参页', () => {
    expect(personaTabFromHref('/persona')).toBe('persona');
    expect(personaTabFromHref('/')).toBe('persona');
    expect(personaTabFromHref('')).toBe('persona');
  });

  it('未知的 tab 值回到默认 tab，且不抛异常', () => {
    // 拼错的调参名不能悄悄落到调参页：那正是要暴露的错误
    expect(personaTabFromHref('/persona?tab=dialgoue')).toBe('persona');
    expect(personaTabFromHref('/persona?tab=')).toBe('persona');
    expect(personaTabFromHref('/persona?tab=PERSONA')).toBe('persona');
    expect(() => personaTabFromHref('/persona?tab=%E0%A4%A')).not.toThrow();
    expect(() => personaTabFromHref(':::not-a-url')).not.toThrow();
  });

  it('旧路径 /settings/dialogue 也开在调参 tab（重定向首帧不先开错）', () => {
    expect(personaTabFromHref('/settings/dialogue')).toBe('dialogue');
    // 尾斜杠与真实路由的尾斜杠处理一致
    expect(personaTabFromHref('/settings/dialogue/')).toBe('dialogue');
  });

  it('personaTabHref 与 personaTabFromHref 往返一致', () => {
    const tabs: PersonaTab[] = ['persona', 'dialogue'];
    for (const tab of tabs) {
      expect(personaTabFromHref(personaTabHref(tab)), `${tab} 往返不一致`).toBe(tab);
    }
    // 具体地址本身也是契约（旧文档与书签指着它们）
    expect(personaTabHref('dialogue')).toBe('/persona?tab=dialogue');
    expect(personaTabHref('persona')).toBe('/persona');
  });
});

/* ------------------------------------------------------------------ 生活页 */

describe('生活页 tab：?tab=rhythm ↔ 生活节奏', () => {
  it('?tab=rhythm 深链开在调参 tab', () => {
    expect(lifeTabFromHref('/life?tab=rhythm')).toBe('rhythm');
    expect(lifeTabFromHref('/life?foo=1&tab=rhythm')).toBe('rhythm');
  });

  it('没有参数时停在默认 tab（它现在在干嘛），不是调参页', () => {
    expect(lifeTabFromHref('/life')).toBe('now');
    expect(lifeTabFromHref('/')).toBe('now');
    expect(lifeTabFromHref('')).toBe('now');
  });

  it('未知的 tab 值回到默认 tab，且不抛异常', () => {
    expect(lifeTabFromHref('/life?tab=rhyth')).toBe('now');
    expect(lifeTabFromHref('/life?tab=')).toBe('now');
    // 路由名是 now，但它不是这个页面的 tab 值：只有 rhythm 才是调参
    expect(lifeTabFromHref('/life?tab=now')).toBe('now');
    expect(() => lifeTabFromHref('/life?tab=%E0%A4%A')).not.toThrow();
    expect(() => lifeTabFromHref(':::not-a-url')).not.toThrow();
  });

  it('旧路径 /settings/life 也开在调参 tab', () => {
    expect(lifeTabFromHref('/settings/life')).toBe('rhythm');
    expect(lifeTabFromHref('/settings/life/')).toBe('rhythm');
  });

  it('lifeTabHref 与 lifeTabFromHref 往返一致', () => {
    const tabs: LifeTab[] = ['now', 'rhythm'];
    for (const tab of tabs) {
      expect(lifeTabFromHref(lifeTabHref(tab)), `${tab} 往返不一致`).toBe(tab);
    }
    expect(lifeTabHref('rhythm')).toBe('/life?tab=rhythm');
    expect(lifeTabHref('now')).toBe('/life');
  });
});

/* -------------------------------------------------------------- 交叉不变式 */

describe('两页的 tab 推导互不串味', () => {
  it('一页的调参值不会被另一页认领（/life?tab=dialogue 仍是默认 tab）', () => {
    expect(lifeTabFromHref('/life?tab=dialogue')).toBe('now');
    expect(personaTabFromHref('/persona?tab=rhythm')).toBe('persona');
  });

  it('两条深链都只用 query、不用 hash 路由（DESIGN §1）', () => {
    expect(personaTabHref('dialogue')).not.toContain('#');
    expect(lifeTabHref('rhythm')).not.toContain('#');
    expect(personaTabFromHref('/persona#dialogue')).toBe('persona');
    expect(lifeTabFromHref('/life#rhythm')).toBe('now');
  });
});
