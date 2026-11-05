/**
 * 外壳运行时：浮层（overlay）单一管理者、窄屏判定、侧栏折叠状态的持久化。
 *
 * 为什么单独一个文件：`App.tsx` 的壳层要同时管「导航抽屉」和「聊天页会话列表」
 * 两片浮层。两片各自开各自的，就会出现「抽屉没关、会话列表又盖上来了」这种
 * 双浮层叠罗汉——真实手机上表现为点 scrim 只关得掉一片，另一片留在下面。
 * 所以**只有一个**「当前打开的是谁」的槽位：打开一片必然挤掉另一片。
 *
 * 这里只放与 React 无关的纯逻辑 + 几个很小的 hook；DOM 相关的副作用
 * （Escape、焦点陷阱、滚动锁）都集中在 hook 里，组件只负责渲染。
 *
 * 不引入 DOM 测试库（仓库没有 jsdom）：纯函数（`overlayReducer`、
 * `readNavExpanded`/`writeNavExpanded`）用 vitest 直接测，
 * 真实浏览器行为（portal、焦点、滚动锁、44×44）交给 `tests/e2e/nav_browser.mjs`。
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type RefObject } from 'react';

/* ------------------------------------------------------------ 浮层互斥 */

/** 同一时刻最多打开一片浮层。 */
export type OverlayId = 'nav' | 'session';

export type OverlayAction =
  | { type: 'open'; overlay: OverlayId }
  | { type: 'toggle'; overlay: OverlayId }
  | { type: 'close' };

/**
 * 浮层状态机：`open` 直接顶掉当前那片（互斥），`toggle` 只在点到自己时才关。
 *
 * 之所以让 `open` 覆盖而不是排队：导航抽屉与会话列表在视觉上是同一层
 * （都盖住正文），同时存在只会让人不知道点哪里能关。
 */
export function overlayReducer(current: OverlayId | null, action: OverlayAction): OverlayId | null {
  switch (action.type) {
    case 'open':
      return action.overlay;
    case 'toggle':
      return current === action.overlay ? null : action.overlay;
    case 'close':
      return null;
  }
}

export interface OverlayController {
  active: OverlayId | null;
  open: (overlay: OverlayId) => void;
  close: () => void;
}

/**
 * 浮层控制器的 hook 外壳：状态 + Escape + 滚动锁 + 关闭后焦点还原。
 *
 * 焦点还原用的是「打开浮层时正在聚焦的那个元素」（`openerRef`）。它必须由
 * `open()` 当场记录：等 effect 跑起来时焦点已经在抽屉里了，那时再读
 * `document.activeElement` 只会读到抽屉自己的第一个按钮。
 */
export function useOverlay(): OverlayController {
  const [active, setActive] = useState<OverlayId | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const open = useCallback((overlay: OverlayId) => {
    if (typeof document !== 'undefined') {
      const focused = document.activeElement;
      openerRef.current = focused instanceof HTMLElement ? focused : null;
    }
    setActive((current) => overlayReducer(current, { type: 'open', overlay }));
  }, []);

  const close = useCallback(() => {
    const opener = openerRef.current;
    openerRef.current = null;
    setActive((current) => (current === null ? current : overlayReducer(current, { type: 'close' })));
    if (opener && opener.isConnected) {
      // 等这一帧的卸载做完再还焦点：抽屉卸载时若自身还在处理焦点，
      // 同步 focus 会被随后的清理覆盖掉（表现为焦点掉回 <body>）。
      window.setTimeout(() => {
        if (opener.isConnected) opener.focus();
      }, 0);
    }
  }, []);

  // Escape 关闭。用捕获阶段：抽屉内部若也有 Escape 语义（改名输入框），
  // 浮层优先关，避免「按了 Escape 但什么都没发生」。
  useEffect(() => {
    if (active === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      close();
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [active, close]);

  // body 滚动锁：抽屉盖住正文时，底下的页面不许跟着滚。
  useEffect(() => {
    if (active === null) return;
    const body = document.body;
    const previous = body.style.overflow;
    body.style.overflow = 'hidden';
    return () => {
      body.style.overflow = previous;
    };
  }, [active]);

  return { active, open, close };
}

/* ------------------------------------------------------------ 焦点陷阱 */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 把 Tab 关在 `ref` 里，并在打开时把焦点放到第一个可聚焦元素上。
 *
 * 只做「首尾环绕」：抽屉会挂在 `document.body` 末尾（portal），如果不拦，
 * Tab 会把焦点送到抽屉背后整个页面上，读屏用户会以为自己还在正文里。
 * `tabindex="-1"` 的元素（scrim）算背景，不算 Tab 停靠点。
 */
export function useFocusTrap(
  ref: RefObject<HTMLElement | null>,
  active: boolean,
  onClose: () => void,
): void {
  useEffect(() => {
    if (!active) return;
    const root = ref.current;
    if (!root) return;

    const focusables = (): HTMLElement[] =>
      Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => element.getAttribute('tabindex') !== '-1' && !element.hasAttribute('hidden'),
      );

    focusables()[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const items = focusables();
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const head = items[0];
      const tail = items[items.length - 1];
      if (!head || !tail) {
        event.preventDefault();
        return;
      }
      const current = document.activeElement;
      const inside = current instanceof Node && root.contains(current);
      if (event.shiftKey && (current === head || !inside)) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && (current === tail || !inside)) {
        event.preventDefault();
        head.focus();
      }
    };
    root.addEventListener('keydown', onKeyDown);
    return () => root.removeEventListener('keydown', onKeyDown);
  }, [ref, active, onClose]);
}

/* ------------------------------------------------------------ 窄屏判定 */

/** 与 CSS 里 `@media (max-width: 900px)` 同一个断点，只写一处。 */
export const NARROW_QUERY = '(max-width: 900px)';

function subscribeNarrow(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return () => undefined;
  }
  const media = window.matchMedia(NARROW_QUERY);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}

function readNarrowSnapshot(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia(NARROW_QUERY).matches;
}

/**
 * 窄屏（≤900px）返回 true。
 *
 * 用「是否渲染 rail」而不是「rail 用 CSS 藏起来」：藏起来的导航项仍然在 DOM 里，
 * 会给出重复的 `data-testid`，E2E 点到的可能是 `display:none` 的那一个；
 * 而 portal 抽屉与 rail 本来就是两套交互，不该同时存在。
 *
 * 服务端渲染（单测用 `renderToStaticMarkup`）没有 `window`，退回桌面形态。
 */
export function useNarrowLayout(): boolean {
  return useSyncExternalStore(subscribeNarrow, readNarrowSnapshot, () => false);
}

/* ------------------------------------------------------ 侧栏折叠持久化 */

/** localStorage 键名：改了它等于把所有人的折叠偏好重置回默认（默认展开）。 */
export const NAV_EXPANDED_KEY = 'ling.nav.expanded';

type ReadableStorage = { getItem: (key: string) => string | null };
type WritableStorage = { setItem: (key: string, value: string) => void };

/**
 * 读折叠状态：**默认展开**，只有明确存过 `'0'` 才折叠。
 *
 * 隐私模式/被禁用的 localStorage 会抛异常；抛了就按默认值走——
 * 一个存不下的偏好不该让整页起不来。
 */
export function readNavExpanded(storage: ReadableStorage | null): boolean {
  if (!storage) return true;
  try {
    return storage.getItem(NAV_EXPANDED_KEY) !== '0';
  } catch {
    return true;
  }
}

/** 写折叠状态（`'1'` 展开 / `'0'` 折叠）；写失败静默，不影响交互。 */
export function writeNavExpanded(storage: WritableStorage | null, expanded: boolean): void {
  if (!storage) return;
  try {
    storage.setItem(NAV_EXPANDED_KEY, expanded ? '1' : '0');
  } catch {
    /* 存不下就算了 */
  }
}

function currentStorage(): WritableStorage & ReadableStorage {
  return window.localStorage;
}

/** 桌面侧栏的展开状态：初始值来自 localStorage，切换即写回。 */
export function useNavExpanded(): { expanded: boolean; toggle: () => void } {
  const [expanded, setExpanded] = useState<boolean>(() =>
    readNavExpanded(typeof window !== 'undefined' ? window.localStorage : null),
  );

  const toggle = useCallback(() => {
    setExpanded((current) => {
      const next = !current;
      writeNavExpanded(typeof window !== 'undefined' ? currentStorage() : null, next);
      return next;
    });
  }, []);

  return { expanded, toggle };
}
