/**
 * 桌面页轮询：不重叠、离开即清、失败不装死。
 *
 * 全是**可观测的行为**，不是"看起来对"：
 * - 不重叠 → 在慢响应期间前进假时钟，断言 `fetchOnce` 的调用次数不增长；
 * - 离开即清 → `stop()` 之后前进时钟，断言不再发请求，且**在途请求被 abort**；
 * - 失败不装死 → 一次失败之后前进时钟，仍会再发（断线恢复应自己回到真实状态）；
 * - 退避 → 连续失败时间隔变大，成功之后回到基础间隔。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backoffDelay, newRequestId, startDesktopPolling } from './desktop-polling';

const BASE = 2000;

/** 让微任务队列跑干净（await 链需要几轮）。 */
async function flush(times = 6): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}

describe('startDesktopPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('启动立刻拉一拍，不等间隔', async () => {
    const fetchOnce = vi.fn(async () => undefined);
    const stop = startDesktopPolling({ fetchOnce, onError: () => undefined, intervalMs: BASE });
    await flush();
    expect(fetchOnce).toHaveBeenCalledTimes(1);
    stop();
  });

  it('不重叠：慢响应期间 tick 只被发起一次（结构保证，不靠判重标志）', async () => {
    // 这条**必须用真实时钟**：`vi.advanceTimersByTimeAsync` 只推进它自己的
    // 微任务链，被 `await` 悬住的续体不会跟着前进——用假时钟时，"重叠"
    // 在任何实现下都不会发生，断言就成了永远为真。
    //
    // 断言的是"慢响应期间 tick 的**发起次数**"（1），而不是"最终调用次数"：
    // 前者才对应"任意时刻最多一条在途请求"这个不变量。变异验证用过两种破坏：
    // 删掉判重标志（本不该红：它不是原因），以及把 `schedule` 挪到 `try` 之前
    // （应该红：那才是真正的重叠）。
    vi.useRealTimers();
    let resolveGate!: () => void;
    const gate = new Promise<void>((resolve) => {
      resolveGate = resolve;
    });
    const starts: number[] = [];
    const fetchOnce = vi.fn(async () => {
      starts.push(Date.now());
      if (starts.length === 1) await gate;
    });
    const stop = startDesktopPolling({ fetchOnce, onError: () => undefined, intervalMs: 20 });

    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(starts, '上一拍未完成却发起了新的请求（重叠）').toHaveLength(1);

    resolveGate();
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(starts.length).toBeGreaterThanOrEqual(2);
    stop();
    vi.useFakeTimers();
  });

  it('stop() 清定时器并中止在途请求；之后不再发请求', async () => {
    const captured: { signal: AbortSignal | null } = { signal: null };
    const fetchOnce = vi.fn((s: AbortSignal) => {
      captured.signal = s;
      // 与真实 `fetch` 一致：abort 时以 AbortError 拒绝。
      return new Promise<void>((_resolve, reject) => {
        s.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      });
    });
    const onError = vi.fn();
    const stop = startDesktopPolling({ fetchOnce, onError, intervalMs: BASE });
    await flush();
    expect(fetchOnce).toHaveBeenCalledTimes(1);

    stop();
    expect(captured.signal?.aborted).toBe(true);
    expect(onError, 'AbortError 不是失败，不能报给界面').not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(BASE * 5);
    expect(fetchOnce).toHaveBeenCalledTimes(1);
  });

  it('失败后继续轮询（断线恢复应自己回到真实状态），并回报错误', async () => {
    const failure = new Error('network down');
    const fetchOnce = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(failure)
      .mockResolvedValue(undefined);
    const onError = vi.fn();
    const onSuccess = vi.fn();
    const stop = startDesktopPolling({
      fetchOnce,
      onError,
      onSuccess,
      intervalMs: BASE,
      // 关掉退避，这条只测"还会不会继续"
      nextDelay: () => BASE,
    });

    await flush();
    expect(onError).toHaveBeenCalledWith(failure);
    expect(onSuccess).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(BASE);
    await flush();
    expect(fetchOnce).toHaveBeenCalledTimes(2);
    expect(onSuccess).toHaveBeenCalledTimes(1);
    stop();
  });

  it('连续失败时按退避拉长间隔，成功后回到基础间隔', async () => {
    const fetchOnce = vi
      .fn<() => Promise<void>>()
      .mockRejectedValue(new Error('down'));
    const delays: number[] = [];
    const stop = startDesktopPolling({
      fetchOnce,
      onError: () => undefined,
      intervalMs: BASE,
      nextDelay: (failures) => {
        const delay = backoffDelay(BASE, failures);
        delays.push(delay);
        return delay;
      },
    });

    await flush();
    // 第一拍失败 → 下一拍等 2×base
    expect(delays).toEqual([BASE * 2]);
    await vi.advanceTimersByTimeAsync(BASE * 2);
    await flush();
    expect(delays).toEqual([BASE * 2, BASE * 4]);
    stop();
  });

  it('backoffDelay 封顶，不无限翻倍', () => {
    expect(backoffDelay(BASE, 0)).toBe(BASE);
    expect(backoffDelay(BASE, 1)).toBe(BASE * 2);
    expect(backoffDelay(BASE, 20)).toBe(15_000);
  });
});

describe('newRequestId', () => {
  it('生成 UUID v4（版本位 4、变体位 10xx）', () => {
    for (let i = 0; i < 20; i += 1) {
      const id = newRequestId();
      expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    }
  });

  it('两次调用不同（这就是"新提交换新编号"的基础）', () => {
    expect(newRequestId()).not.toBe(newRequestId());
  });
});
