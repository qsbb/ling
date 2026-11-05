/**
 * SSE 解析器单测：跨 chunk 切分、粘包、多行 data、CRLF、UTF-8 多字节、超时与取消。
 *
 * 用真实 ReadableStream 构造响应，不 mock 解析逻辑。
 * 运行：pnpm test
 */
import { describe, expect, it } from 'vitest';
import { consumeSse, StreamAbortError, StreamTimeoutError } from './sse';
import type { SseEvent } from './types';

const encoder = new TextEncoder();

interface StreamOptions {
  delayMs?: number;
  /** 发完所有 chunk 后不关闭流，用于等待空闲超时。 */
  neverEnd?: boolean;
}

function responseFromChunks(chunks: Array<string | Uint8Array>, options: StreamOptions = {}): Response {
  let index = 0;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (index >= chunks.length) {
        if (options.neverEnd) {
          return new Promise<void>(() => undefined);
        }
        controller.close();
        return;
      }
      const chunk = chunks[index];
      index += 1;
      if (options.delayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayMs));
      }
      controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : (chunk as Uint8Array));
    },
  });
  return new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
}

async function collect(
  chunks: Array<string | Uint8Array>,
  options: { stream?: StreamOptions; idleMs?: number } = {},
): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  await consumeSse(responseFromChunks(chunks, options.stream), (event) => events.push(event), {
    idleMs: options.idleMs,
  });
  return events;
}

describe('consumeSse', () => {
  it('解析单个事件与单行 data', async () => {
    const events = await collect([
      'event: turn.started\ndata: {"turn_id":"turn_1","state":"streaming"}\n\n',
    ]);
    expect(events).toEqual([
      { event: 'turn.started', data: { turn_id: 'turn_1', state: 'streaming' } },
    ]);
  });

  it('同一 chunk 里的多个事件（粘包）全部解析', async () => {
    const events = await collect([
      'event: delta\ndata: {"text":"嗯"}\n\nevent: delta\ndata: {"text":"好"}\n\n' +
        'event: turn.completed\ndata: {"turn_id":"turn_1","state":"completed"}\n\n',
    ]);
    expect(events.map((event) => event.event)).toEqual(['delta', 'delta', 'turn.completed']);
    expect(events[0]?.data.text).toBe('嗯');
    expect(events[1]?.data.text).toBe('好');
  });

  it('事件被切分到多个 chunk（逐字符）仍能拼回', async () => {
    const payload =
      'event: delta\ndata: {"text":"逐字切分"}\n\nevent: turn.completed\n' +
      'data: {"turn_id":"turn_1","state":"completed"}\n\n';
    const events = await collect(Array.from(payload));
    expect(events).toHaveLength(2);
    expect(events[0]?.data.text).toBe('逐字切分');
    expect(events[1]?.data.turn_id).toBe('turn_1');
  });

  it('UTF-8 多字节字符被按字节切开也不乱码', async () => {
    const payload = 'event: delta\ndata: {"text":"中文与 emoji 😀"}\n\n';
    const bytes = encoder.encode(payload);
    const chunks = Array.from(bytes, (byte) => Uint8Array.of(byte));
    const events = await collect(chunks);
    expect(events).toHaveLength(1);
    expect(events[0]?.data.text).toBe('中文与 emoji 😀');
  });

  it('多行 data 用换行拼接后解析', async () => {
    const events = await collect(['event: delta\ndata: {"text":\ndata: "多行数据"}\n\n']);
    expect(events).toHaveLength(1);
    expect(events[0]?.data.text).toBe('多行数据');
  });

  it('兼容 CRLF 行结束符', async () => {
    const events = await collect([
      'event: delta\r\ndata: {"text":"回车换行"}\r\n\r\nevent: turn.stopped\r\ndata: {"state":"stopped"}\r\n\r\n',
    ]);
    expect(events.map((event) => event.event)).toEqual(['delta', 'turn.stopped']);
    expect(events[0]?.data.text).toBe('回车换行');
  });

  it('流结束时没有结尾空行也会派发最后一个事件', async () => {
    const events = await collect([
      'event: turn.failed\ndata: {"turn_id":"turn_1","error":{"code":"provider_error"}}',
    ]);
    expect(events).toHaveLength(1);
    expect((events[0]?.data.error as { code: string }).code).toBe('provider_error');
  });

  it('非法 JSON 保留原始文本而不是抛错', async () => {
    const events = await collect(['event: delta\ndata: not-json\n\n']);
    expect(events).toEqual([{ event: 'delta', data: { raw: 'not-json' } }]);
  });

  it('只有注释的 chunk 不产生事件', async () => {
    const events = await collect([': heartbeat\n\n']);
    expect(events).toEqual([]);
  });

  it('长时间没有新数据时以 StreamTimeoutError 结束并取消读取', async () => {
    const stream = responseFromChunks(['event: delta\ndata: {"text":"开始"}\n\n'], {
      neverEnd: true,
    });
    await expect(
      consumeSse(stream, () => undefined, { idleMs: 80 }),
    ).rejects.toBeInstanceOf(StreamTimeoutError);
  });

  it('外部 AbortSignal 中止时抛出 StreamAbortError', async () => {
    const controller = new AbortController();
    const stream = responseFromChunks(
      ['event: delta\ndata: {"text":"一"}\n\n', 'event: delta\ndata: {"text":"二"}\n\n'],
      { delayMs: 40 },
    );
    const pending = consumeSse(stream, () => undefined, { signal: controller.signal });
    setTimeout(() => controller.abort(), 20);
    await expect(pending).rejects.toBeInstanceOf(StreamAbortError);
  });
});
