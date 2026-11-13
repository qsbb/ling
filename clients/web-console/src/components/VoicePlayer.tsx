/**
 * 语音消息的播放控件。参与者默认看不到原始文字。
 *
 * 后台正文通过独立的 operator 视图显式读取，不依赖音频资产是否存在。
 *
 * ## 为什么单独一个端点，而不是给消息加字段
 *
 * `types.ts::ChatMessage` 由 SP00 独占，且"这条消息有没有音频"是一次
 * `media_assets` join。挂在 `GET /v1/sessions/{id}/voice-messages` 上，
 * 会话页可以在消息渲染之后补一层播放器，不必改消息契约。
 *
 * ## 播放 ≠ 送达（界面上也要分开）
 *
 * 后端给两个独立字段：
 *
 * - `delivered`：渠道回了回执（她真的发出去了）；
 * - `played` / `play_count`：主人在 Web 上点开听过。
 *
 * 界面上是**两句不同的话**，不是一个"已读"角标：
 * 「已发出，还没听」和「没发出去（你听过了）」是两种完全不同的处境。
 *
 * ## 播放失败必须说清楚
 *
 * 三种失败各有各的下一步，所以文案不能合并：
 *
 * - `expired`（签名 URL 404，超过 2 分钟）：**自动重取一次**地址再播；
 *   重取还失败才提示"这条语音已经过期（保留 7 天）"。
 * - `unsupported`（浏览器不支持该编码，如 silk/amr）：直说"这个浏览器播不了
 *   这种格式"，并给出文字（文字一直都在，所以不算内容丢失）。
 * - `error`（其它）："没播出来，点一下重试"。
 *
 * 静默失败是这里最坏的结果——用户会以为是"她没发语音"。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/** 后端 `GET /v1/sessions/{id}/voice-messages` 的一条。 */
export interface VoiceMessageView {
  schema_version: string;
  message_id: string;
  window_id: string;
  kind: 'audio';
  /** 签名 URL（短时效，无 cookie 也能取）。`null` = 后端没给（理论上不出现）。 */
  url: string | null;
  mime_type: string;
  format: string;
  size_bytes: number;
  duration_ms: number | null;
  sample_rate: number | null;
  /** 普通参与者视图必须为空；后台正文单独读取。 */
  text: string;
  created_at: string;
  expires_at: string | null;
  /** 渠道送达（与播放**无关**）。 */
  delivered: boolean;
  /** 主人在 Web 上听过没有（与送达**无关**）。 */
  played: boolean;
  play_count: number;
  last_played_at: string | null;
}

/** 播放器三态之外再加一个"正在重取地址"。 */
export type VoicePlayerState = 'idle' | 'playing' | 'paused' | 'error';

export interface VoicePlayerProps {
  voice: VoiceMessageView;
  /**
   * 重取一次签名 URL（地址过期时用）。返回新地址；拿不到返回 `null`。
   * 不传时表示"地址不会过期"，401/403/404 直接报错。
   */
  refreshUrl?: (messageId: string) => Promise<string | null>;
  /** 上报一次播放（后端按 `(message_id, subject)` 累加，不会一次请求一行）。 */
  onPlayed?: (messageId: string, info: { positionMs?: number; completed?: boolean }) => void;
  /** 时长格式化。默认 `m:ss`。 */
  formatDuration?: (ms: number | null) => string;
}

const DEFAULT_FORMAT_DURATION = (ms: number | null): string => {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return '--:--';
  const total = Math.round(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
};

/** 播放失败的三类原因。文案分开，因为下一步不同。 */
export type VoiceErrorKind = 'expired' | 'unsupported' | 'error';

export function voiceErrorText(kind: VoiceErrorKind): string {
  switch (kind) {
    case 'expired':
      return '这条语音已经过期了（音频保留 7 天）。';
    case 'unsupported':
      return '这个浏览器暂不支持这种语音格式。';
    default:
      return '没播出来，点一下重试。';
  }
}

/**
 * 把 `<audio>` 的 `error` 事件翻译成三态之一。
 *
 * `MediaError.code` 的取值：1 ABORTED / 2 NETWORK / 3 DECODE / 4 SRC_NOT_SUPPORTED。
 * `DECODE` 与 `SRC_NOT_SUPPORTED` 都表示"这个浏览器/这份数据播不了"——
 * silk、amr 编码在大多数浏览器上就是走这条。网络错误则可能是签名过期，
 * 交给调用方去重取地址。
 */
export function classifyMediaError(code: number | undefined): VoiceErrorKind {
  if (code === 3 || code === 4) return 'unsupported';
  return 'error';
}

/** 送达/播放两句话必须分行说。 */
export function deliveryAndPlaybackText(voice: {
  delivered: boolean;
  played: boolean;
  play_count: number;
}): { delivery: string; playback: string } {
  return {
    delivery: voice.delivered ? '已发出' : '没发到渠道',
    playback: voice.played
      ? voice.play_count > 1
        ? `你听过 ${voice.play_count} 次`
        : '你听过'
      : '还没听',
  };
}

export function VoicePlayer({
  voice,
  refreshUrl,
  onPlayed,
  formatDuration = DEFAULT_FORMAT_DURATION,
}: VoicePlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<VoicePlayerState>('idle');
  const [errorKind, setErrorKind] = useState<VoiceErrorKind | null>(null);
  const [src, setSrc] = useState<string | null>(voice.url);
  const [refreshed, setRefreshed] = useState(false);

  // 换了消息（列表重排/刷新）就重置，避免把上一条的播放状态画到这一条上。
  useEffect(() => {
    setSrc(voice.url);
    setRefreshed(false);
    setState('idle');
    setErrorKind(null);
  }, [voice.message_id, voice.url]);

  const notes = useMemo(() => deliveryAndPlaybackText(voice), [voice]);

  const handlePlay = useCallback(() => {
    setState('playing');
    setErrorKind(null);
    onPlayed?.(voice.message_id, {});
  }, [onPlayed, voice.message_id]);

  const handleEnded = useCallback(() => {
    setState('idle');
    const durationMs = voice.duration_ms ?? 0;
    // **只有 ended 才算听完**：把"播到 99%"当听完，会让「他根本没听完」
    // 这个信号消失。
    onPlayed?.(voice.message_id, { positionMs: durationMs, completed: true });
  }, [onPlayed, voice.duration_ms, voice.message_id]);

  const recover = useCallback(
    async (kind: VoiceErrorKind) => {
      // 只在"可能是签名过期"且还没重取过时自动重取一次。地址 TTL 是 2 分钟，
      // 而会话页可能开着很久；重取一次能救回绝大多数"刚才还好好的"。
      if (kind === 'error' && refreshUrl && !refreshed) {
        setRefreshed(true);
        const next = await refreshUrl(voice.message_id);
        if (next) {
          setSrc(next);
          setErrorKind(null);
          setState('idle');
          return;
        }
      }
      setErrorKind(kind);
      setState('error');
    },
    [refreshUrl, refreshed, voice.message_id],
  );

  return (
    <div className="voice-message" data-testid={`voice-message-${voice.message_id}`}>
      <div className="voice-message__head">
        {/* 「语音标签」：必须一眼看出这条是语音，而不是截图/文字。 */}
        <span className="voice-message__tag" data-testid="voice-label">
          语音
        </span>
        <span className="voice-message__duration">{formatDuration(voice.duration_ms)}</span>
      </div>

      <div className="voice-message__actions">
        <audio
          ref={audioRef}
          data-testid="voice-audio"
          className="voice-message__audio"
          src={src ?? undefined}
          preload="metadata"
          onPlay={handlePlay}
          onPause={() => setState((prev) => (prev === 'playing' ? 'paused' : prev))}
          onEnded={handleEnded}
          onError={(event) => {
            void recover(classifyMediaError(event.currentTarget.error?.code));
          }}
        >
          {/* 轨道文本：读屏用户听到的是"语音消息"，不是一个无标签的播放器。 */}
          <track kind="captions" />
        </audio>
        <button
          type="button"
          className="voice-message__toggle"
          data-testid="voice-toggle"
          aria-label={state === 'playing' ? '暂停这条语音' : '播放这条语音'}
          onClick={() => {
            const audio = audioRef.current;
            if (!audio) return;
            if (state === 'playing') {
              audio.pause();
              return;
            }
            void audio.play().catch(() => {
              void recover('error');
            });
          }}
        >
          {state === 'playing' ? '暂停' : '播放'}
        </button>
        {/* 两个事实分两行，不合并成一个角标：送达是渠道的事实，播放是人的事实。 */}
        <span className="voice-message__delivery" data-testid="voice-delivery">
          {notes.delivery}
        </span>
        <span
          className="voice-message__played"
          data-testid="voice-played"
          data-played={voice.played ? 'true' : 'false'}
        >
          {notes.playback}
        </span>
      </div>

      {errorKind ? (
        <p className="voice-message__error" role="alert" data-testid="voice-error">
          {voiceErrorText(errorKind)}
        </p>
      ) : null}
    </div>
  );
}

export default VoicePlayer;
