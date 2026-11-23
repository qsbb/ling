/**
 * 桌面页的轮询循环（与 React 无关，`desktop-polling.test.ts` 用假时钟直接测）。
 *
 * 三条硬要求来自验收口径，任何一条都不能靠"看起来对"：
 *
 * 1. **不重叠**：上一次响应回来之前不发下一次。
 *
 *    这一点由**结构**保证，而不是靠一个判重标志：下一拍只在上一拍的
 *    `finally` 之后才被排上（`setTimeout` 一次只挂一个），所以任意时刻最多
 *    一条在途请求——网络慢的时候，下一次请求会在**这次回来之后**才发出，
 *    不会"每 2 秒排队一次"。
 *
 *    曾经这里还有一个 `inFlight` 提前返回的守卫，看起来在防重叠，实际上
 *    **永远走不到**（同一个结构已经保证了），而它让人以为重叠被"检查"过。
 *    变异验证暴露了这件事：删掉守卫后没有任何测试变红，因为那一行从来不是
 *    不重叠的原因。现在删掉它，改由
 *    `desktop-polling.test.ts` 用"慢响应期间 tick 只发起一次"来证明结构本身。
 * 2. **离开页面就清干净**：`stop()` 清定时器**并中止在途请求**。只清定时器的话，
 *    停止那一刻已经在飞的那条响应仍会回来写状态，界面看起来"离开了还在动"。
 * 3. **失败了不能装作还在轮询**：网络断掉时 `fetchOnce` 抛错，这里把错误交给
 *    `onError`，并**继续排下一拍**——断线恢复后页面应该自己回到真实状态，
 *    而不是停在一个过期快照上等用户手动刷新。
 *
 * 为什么不复用 `startLogPolling`：那个函数自带"暂停时跳过"和日志专属的
 * 换挡语义（`isPaused`），而桌面页的"暂停接收"是**后端业务状态**，不是停止
 * 观察——暂停期间反而更要继续轮询（在途任务会被后端终止，界面得看到）。
 * 把两者合成一个带两个暂停概念的循环，是最容易出"看起来暂停了"的那类错。
 */

export interface DesktopPollOptions {
  /** 真实拉取一次。`signal` 在停止/超时被 abort。 */
  fetchOnce: (signal: AbortSignal) => Promise<void>;
  /** 拉取失败。由调用方决定显示什么，本函数不吞。 */
  onError: (error: unknown) => void;
  /** 一拍成功回调（用于清掉上一次的错误条）。 */
  onSuccess?: () => void;
  intervalMs: number;
  /**
   * 下一次间隔。页面可以用它做退避：连续失败时越排越慢，
   * 免得服务器挂掉时每 2 秒刷一次错误。
   */
  nextDelay?: (failures: number) => number;
}

const DEFAULT_BACKOFF_MAX_MS = 15_000;

export function backoffDelay(baseMs: number, failures: number): number {
  if (failures <= 0) return baseMs;
  return Math.min(baseMs * 2 ** Math.min(failures, 4), DEFAULT_BACKOFF_MAX_MS);
}

/**
 * 启动轮询，返回 `stop()`。
 *
 * `start` 会立刻跑一拍（进页面就有数据，不等 2 秒），之后按间隔继续。
 */
export function startDesktopPolling(options: DesktopPollOptions): () => void {
  const { fetchOnce, onError, onSuccess, intervalMs } = options;
  const nextDelay = options.nextDelay ?? ((failures: number) => backoffDelay(intervalMs, failures));
  let stopped = false;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;

  const schedule = (delay: number) => {
    if (stopped) return;
    timer = setTimeout(() => {
      void tick();
    }, delay);
  };

  const tick = async (): Promise<void> => {
    if (stopped) return;
    // 下一拍只在本拍的 `finally` 里排队（见上面"不重叠"的说明）：不需要判重标志。
    controller = new AbortController();
    let delay = intervalMs;
    try {
      await fetchOnce(controller.signal);
      failures = 0;
      onSuccess?.();
    } catch (error) {
      // AbortError 是 stop() 自己造成的，不是失败：不能报给界面。
      if (stopped || (error instanceof DOMException && error.name === 'AbortError')) return;
      failures += 1;
      delay = nextDelay(failures);
      onError(error);
    } finally {
      controller = null;
    }
    schedule(delay);
  };

  // 第一拍不等待：进页面立刻拉。
  void tick();

  return () => {
    stopped = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    controller?.abort();
    controller = null;
  };
}

/**
 * 单次提交编号：UUID v4。
 *
 * 取 `crypto.randomUUID()`（安全上下文/回环地址下都有）；不可用时退回
 * `getRandomValues` 手拼 v4——**不**退回 `Math.random()` 拼一个"像 UUID"的串，
 * 那会让"同一次重试沿用同一编号"的语义在少数浏览器上悄悄失效。
 * 编号由调用方持有，重试时复用；新提交才生成新的。
 */
export function newRequestId(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    bytes[6] = (bytes[6]! & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8]! & 0x3f) | 0x80; // variant 10
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  throw new Error('当前浏览器没有可用的安全随机源，无法生成提交编号');
}
