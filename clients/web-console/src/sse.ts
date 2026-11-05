/**
 * SSE 解析（fetch + ReadableStream）。
 *
 * 为什么不用 EventSource：发送消息需要 POST + JSON body，EventSource 只支持 GET。
 * 处理要点：
 * - 事件以空行（`\n\n`）分隔，一个事件可能跨多个网络 chunk。
 * - 每个事件可能有多行 `data:`，按行拼接。
 * - 空闲超时：服务端长时间无新数据时主动中断，让界面给出明确原因与重试。
 */
import type { SseEvent } from './types';

export class StreamTimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'StreamTimeoutError';
  }
}

export class StreamAbortError extends Error {
  constructor() {
    super('已取消读取');
    this.name = 'StreamAbortError';
  }
}

export interface ConsumeOptions {
  /** 外部取消（切换会话、卸载组件）。 */
  signal?: AbortSignal;
  /** 无新数据的超时毫秒数；0 表示不设超时。 */
  idleMs?: number;
}

interface IdleTimer {
  promise: Promise<never>;
  cancel: () => void;
}

function idleTimer(ms: number): IdleTimer {
  let handle: ReturnType<typeof setTimeout> | undefined;
  let cancelled = false;
  const promise = new Promise<never>((_, reject) => {
    handle = setTimeout(() => {
      if (!cancelled) {
        reject(new StreamTimeoutError(`服务端 ${Math.round(ms / 1000)} 秒没有返回新数据`));
      }
    }, ms);
  });
  return {
    promise,
    cancel: () => {
      cancelled = true;
      if (handle !== undefined) clearTimeout(handle);
    },
  };
}

function parseBlock(raw: string): SseEvent | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') {
      event = value;
    } else if (field === 'data') {
      dataLines.push(value);
    }
  }
  if (dataLines.length === 0) return null;
  const rawData = dataLines.join('\n');
  let data: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(rawData);
    data =
      parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : { value: parsed };
  } catch {
    data = { raw: rawData };
  }
  return { event, data };
}

/**
 * 读取 SSE 响应并逐事件回调。返回时表示服务端已正常结束流。
 */
export async function consumeSse(
  response: Response,
  onEvent: (event: SseEvent) => void,
  options: ConsumeOptions = {},
): Promise<void> {
  const body = response.body;
  if (!body) {
    throw new StreamAbortError();
  }
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  const idleMs = options.idleMs ?? 0;
  let buffer = '';
  let aborted = false;

  const onAbort = () => {
    aborted = true;
    void reader.cancel().catch(() => undefined);
  };
  options.signal?.addEventListener('abort', onAbort, { once: true });

  try {
    for (;;) {
      if (options.signal?.aborted) throw new StreamAbortError();
      const timer = idleMs > 0 ? idleTimer(idleMs) : null;
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = timer ? await Promise.race([reader.read(), timer.promise]) : await reader.read();
      } catch (error) {
        if (error instanceof StreamTimeoutError) {
          void reader.cancel().catch(() => undefined);
          throw error;
        }
        throw error;
      } finally {
        timer?.cancel();
      }
      if (result.done) break;

      buffer = (buffer + decoder.decode(result.value, { stream: true })).replace(/\r\n/g, '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseBlock(block);
        if (event) onEvent(event);
        boundary = buffer.indexOf('\n\n');
      }
    }

    if (options.signal?.aborted) throw new StreamAbortError();
    buffer = (buffer + decoder.decode()).replace(/\r\n/g, '\n').trim();
    if (buffer) {
      const event = parseBlock(buffer);
      if (event) onEvent(event);
    }
  } catch (error) {
    if (aborted) throw new StreamAbortError();
    if (error instanceof DOMException && error.name === 'AbortError') throw new StreamAbortError();
    throw error;
  } finally {
    options.signal?.removeEventListener('abort', onAbort);
  }
}
