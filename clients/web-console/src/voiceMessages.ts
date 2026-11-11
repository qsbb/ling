/**
 * 语音消息的取数与上报（不碰 `api.ts`：那由 SP00 独占）。
 *
 * ## 为什么单独一个模块
 *
 * 会话页需要三件事，而它们与 `api.ts` 的既有封装无关：
 *
 * 1. 拉一个会话里**可播放的语音消息**（`GET /v1/sessions/{id}/voice-messages`）
 * 2. 单条重取地址（签名 URL 有效期 2 分钟，页面开久了一定会过期）
 * 3. 上报一次播放（`POST /v1/messages/{id}/playback`）
 *
 * 把这三个函数放这里，`VoicePlayer` 只依赖"给 id 拿 url / 上报播放"两个
 * 回调，测试可以注入假实现；而 `api.ts` 一行不用改。
 *
 * ## 失败不要静默
 *
 * `fetchVoiceMessages` 失败时返回 `{voices: [], failed: true, reason}` 而不是
 * 抛异常或空数组：会话页要能区分「这个会话没有语音」与「语音列表没取到」。
 * 后者必须提示，否则用户会以为"她根本没发语音"——那是最糟的误导。
 */

import type { VoiceMessageView } from './components/VoicePlayer';

export interface VoiceMessagesResult {
  voices: VoiceMessageView[];
  /** 请求本身失败（网络/5xx/鉴权）。**与"没有语音"必须区分**。 */
  failed: boolean;
  /** 失败时给人看的一句话。 */
  reason: string | null;
}

/** 会话里可播放的语音消息。 */
export async function fetchVoiceMessages(
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<VoiceMessagesResult> {
  try {
    const response = await fetchImpl(`/v1/sessions/${encodeURIComponent(sessionId)}/voice-messages`, {
      credentials: 'same-origin',
      headers: { accept: 'application/json' },
    });
    if (!response.ok) {
      return {
        voices: [],
        failed: true,
        reason:
          response.status === 401
            ? '登录已过期，语音列表没取到。刷新页面重新登录。'
            : `语音列表没取到（HTTP ${response.status}）。这一页可能没有显示全部语音。`,
      };
    }
    const body = (await response.json()) as { voice_messages?: VoiceMessageView[] };
    return { voices: body.voice_messages ?? [], failed: false, reason: null };
  } catch (error) {
    return {
      voices: [],
      failed: true,
      reason: `语音列表没取到（${error instanceof Error ? error.message : '网络错误'}）。`,
    };
  }
}

/** 单条语音的最新签名地址（过期后重取）。拿不到返回 `null`。 */
export async function refetchVoiceUrl(
  messageId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  try {
    const response = await fetchImpl(`/v1/messages/${encodeURIComponent(messageId)}/voice`, {
      credentials: 'same-origin',
      headers: { accept: 'application/json' },
    });
    if (!response.ok) return null;
    const body = (await response.json()) as { url?: string | null };
    return body.url ?? null;
  } catch {
    return null;
  }
}

/**
 * 上报一次播放。
 *
 * **失败静默**：播放记录是统计量，不是凭据。用户已经听到声音了，一个
 * 上报失败不该弹出错误打断他。真正的可见失败留给"播不出来"那条路径。
 */
export async function reportPlayback(
  messageId: string,
  info: { positionMs?: number; completed?: boolean } = {},
  fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  try {
    const response = await fetchImpl(`/v1/messages/${encodeURIComponent(messageId)}/playback`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({
        position_ms: info.positionMs ?? null,
        completed: info.completed ?? false,
      }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** 按 `message_id` 建索引，供消息列表逐条查找。 */
export function indexVoicesByMessage(
  voices: VoiceMessageView[],
): Map<string, VoiceMessageView> {
  return new Map(voices.map((voice) => [voice.message_id, voice]));
}
