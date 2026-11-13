/**
 * 语音消息播放控件的回归测试（SP03）。
 *
 * ## 这个文件在守什么
 *
 * 参与者看到语音标签、时长和播放器，不看到 canonical transcript。
 * 管理员正文入口由独立的 operator view 显式触发。
 *
 * 第二重要的是**播放与送达分开**。后端给了两个独立字段，界面上也必须是
 * **两句不同的话**：「已发出 / 没发到渠道」与「你听过 / 还没听」。把它们合并成
 * 一个"已读"角标，正是这个任务要避免的失败。
 *
 * 第三是**播放失败不许静默**：三种失败（过期 / 浏览器不支持该编码 / 其它）
 * 各有各的下一步，文案必须不同，且都必须出现 `role="alert"`。
 *
 * 与仓库其它测试一致：不引入 DOM 测试库，用 `react-dom/server` 渲染成
 * 字符串断言结构；纯逻辑直接调函数。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  VoicePlayer,
  classifyMediaError,
  deliveryAndPlaybackText,
  voiceErrorText,
  type VoiceMessageView,
} from './components/VoicePlayer';
import { OperatorSpeechText } from './components/MessageList';
import {
  fetchVoiceMessages,
  fetchOperatorSpeechText,
  indexVoicesByMessage,
  refetchVoiceUrl,
  reportPlayback,
} from './voiceMessages';

function voice(patch: Partial<VoiceMessageView> = {}): VoiceMessageView {
  return {
    schema_version: 'v1',
    message_id: 'msg_voice_1',
    window_id: 'win_1',
    kind: 'audio',
    url: 'http://testserver/v1/media/generated/abc?exp=1&sig=x',
    mime_type: 'audio/mpeg',
    format: 'mp3',
    size_bytes: 2048,
    duration_ms: 2400,
    sample_rate: 24000,
    text: '晚安，早点睡。',
    created_at: '2026-11-11T00:00:00Z',
    expires_at: null,
    delivered: true,
    played: false,
    play_count: 0,
    last_played_at: null,
    ...patch,
  };
}

describe('参与者语音投影', () => {
  it('渲染语音标签、时长与播放器，不渲染正文', () => {
    const html = renderToStaticMarkup(<VoicePlayer voice={voice()} />);

    expect(html).toContain('data-testid="voice-label"');
    expect(html).toContain('语音');
    // 时长 2400ms → 0:02
    expect(html).toContain('0:02');
    expect(html).not.toContain('voice-text');
    expect(html).not.toContain('晚安，早点睡。');
    expect(html).toContain('data-testid="voice-audio"');
    expect(html).toContain('data-testid="voice-toggle"');
  });

  it('管理员需要显式触发后台正文读取', () => {
    const html = renderToStaticMarkup(<OperatorSpeechText messageId="msg_voice_1" />);
    expect(html).toContain('后台查看正文');
    expect(html).not.toContain('晚安，早点睡。');
  });

  it('空正文时标签仍在：这条依然是语音', () => {
    const html = renderToStaticMarkup(<VoicePlayer voice={voice({ text: '' })} />);
    expect(html).toContain('data-testid="voice-label"');
  });
});

describe('播放与送达分开（关键设计）', () => {
  it('已发出 + 没听 → 两句话分开出现', () => {
    const html = renderToStaticMarkup(<VoicePlayer voice={voice()} />);
    expect(html).toContain('data-testid="voice-delivery"');
    expect(html).toContain('已发出');
    expect(html).toContain('data-testid="voice-played"');
    expect(html).toContain('还没听');
    // 不能出现合并后的"已读/已听"这种把两件事混起来的说法
    expect(html).not.toContain('已读');
  });

  it('没发出 + 听过 → 两个事实各自如实表达', () => {
    const html = renderToStaticMarkup(
      <VoicePlayer voice={voice({ delivered: false, played: true, play_count: 1 })} />,
    );
    expect(html).toContain('没发到渠道');
    expect(html).toContain('你听过');
    expect(html).toContain('data-played="true"');
  });

  it('听过多次显示次数（不是每次都变成一行）', () => {
    const text = deliveryAndPlaybackText({ delivered: true, played: true, play_count: 3 });
    expect(text.playback).toBe('你听过 3 次');
    expect(text.delivery).toBe('已发出');
  });

  it('播放状态来自播放字段，与送达字段互不影响', () => {
    const notDelivered = deliveryAndPlaybackText({
      delivered: false,
      played: true,
      play_count: 1,
    });
    const deliveredNotPlayed = deliveryAndPlaybackText({
      delivered: true,
      played: false,
      play_count: 0,
    });
    expect(notDelivered).toEqual({ delivery: '没发到渠道', playback: '你听过' });
    expect(deliveredNotPlayed).toEqual({ delivery: '已发出', playback: '还没听' });
  });
});

describe('播放失败必须说清楚（三种文案不合并）', () => {
  it('三类错误各有各的下一步', () => {
    const expired = voiceErrorText('expired');
    const unsupported = voiceErrorText('unsupported');
    const other = voiceErrorText('error');

    expect(new Set([expired, unsupported, other]).size).toBe(3);
    expect(expired).toContain('过期');
    expect(unsupported).toContain('不支持');
    expect(unsupported).not.toContain('正文');
    expect(other).toContain('重试');
  });

  it('MediaError 码翻译：解码/源不支持 → unsupported，其余 → error', () => {
    expect(classifyMediaError(3)).toBe('unsupported'); // DECODE
    expect(classifyMediaError(4)).toBe('unsupported'); // SRC_NOT_SUPPORTED
    expect(classifyMediaError(2)).toBe('error'); // NETWORK
    expect(classifyMediaError(undefined)).toBe('error');
  });
});

describe('取数封装：失败必须与"没有语音"区分', () => {
  it('后台正文只从显式 operator 视图读取', async () => {
    const calls: string[] = [];
    const fake = (async (url: string) => {
      calls.push(url);
      return new Response(JSON.stringify({ message: { text: '后台正文' } }), { status: 200 });
    }) as unknown as typeof fetch;
    expect(await fetchOperatorSpeechText('msg/1', fake)).toBe('后台正文');
    expect(calls).toEqual(['/v1/speech/messages/msg%2F1?view=operator']);
  });

  it('200 且空列表 → 没有语音（不算失败）', async () => {
    const fake = (async () =>
      new Response(JSON.stringify({ voice_messages: [] }), { status: 200 })) as typeof fetch;
    const result = await fetchVoiceMessages('win_1', fake);
    expect(result.failed).toBe(false);
    expect(result.voices).toEqual([]);
  });

  it('401 → failed，并给一句人话', async () => {
    const fake = (async () => new Response('{}', { status: 401 })) as typeof fetch;
    const result = await fetchVoiceMessages('win_1', fake);
    expect(result.failed).toBe(true);
    expect(result.reason).toContain('登录');
  });

  it('网络异常 → failed 而不是抛出', async () => {
    const fake = (async () => {
      throw new Error('offline');
    }) as typeof fetch;
    const result = await fetchVoiceMessages('win_1', fake);
    expect(result.failed).toBe(true);
    expect(result.reason).toContain('offline');
  });

  it('成功时原样带回列表（含文字与两个记录字段）', async () => {
    const payload = { voice_messages: [voice()] };
    const fake = (async () =>
      new Response(JSON.stringify(payload), { status: 200 })) as typeof fetch;
    const result = await fetchVoiceMessages('win_1', fake);
    expect(result.voices).toHaveLength(1);
    const first = result.voices[0]!;
    expect(first.text).toBe('晚安，早点睡。');
    expect(first.delivered).toBe(true);
    expect(first.played).toBe(false);
  });

  it('重取地址：成功返回新 url，失败返回 null（不抛）', async () => {
    const ok = (async () =>
      new Response(JSON.stringify({ url: 'http://x/new' }), { status: 200 })) as typeof fetch;
    expect(await refetchVoiceUrl('msg_1', ok)).toBe('http://x/new');

    const gone = (async () => new Response('{}', { status: 404 })) as typeof fetch;
    expect(await refetchVoiceUrl('msg_1', gone)).toBeNull();
  });

  it('上报播放：只发播放字段，不带"送达"', async () => {
    const calls: { url: string; body: unknown }[] = [];
    const fake = (async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? '{}')) });
      return new Response(JSON.stringify({ play_count: 1 }), { status: 200 });
    }) as unknown as typeof fetch;

    const ok = await reportPlayback('msg_1', { completed: true }, fake);
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    const call = calls[0]!;
    expect(call.url).toBe('/v1/messages/msg_1/playback');
    expect(call.body).toEqual({ position_ms: null, completed: true });
    // 播放上报里**不能**出现送达字段：两件事不能互相冒充
    expect(JSON.stringify(call.body)).not.toContain('delivered');
  });

  it('上报失败静默（不抛）', async () => {
    const fake = (async () => new Response('', { status: 500 })) as typeof fetch;
    expect(await reportPlayback('msg_1', {}, fake)).toBe(false);
  });

  it('按 message_id 建索引', () => {
    const map = indexVoicesByMessage([voice(), voice({ message_id: 'msg_2' })]);
    expect(map.size).toBe(2);
    expect(map.get('msg_2')?.message_id).toBe('msg_2');
  });
});
