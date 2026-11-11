/**
 * 语音设置：「意愿档位」+「音色」。挂在 ⑤ 对话与主动性（`DialoguePage`）里，
 * 由 `PolicyForm` 的 `sections` 槽位渲染。
 *
 * ## 三家的差异是这一页的核心，不是细节
 *
 * MiniMax 要 `voice_id` + 语速/音量/音高/情绪；FishAudio **只有** reference_id；
 * MiMo 要一段**上传**的参考音频。**没有统一的 `voice_key`**（§17.2 撤销了
 * 跨厂商映射表）：同一个"温柔"在三家是三个不同的值，所以这里**不造映射表、
 * 不跨家预填**。选 `mimo` 时不会显示 speed/vol，也不会把 MiniMax 的 id 搬过去。
 *
 * ## 两个层面的"清残留"
 *
 * 1. **状态层**：切 kind 时 `switchVoiceKind()` 把旧家的字段真的清掉；
 * 2. **请求层**：`voicePayloadFrom()` 只发这一家拥有的字段。
 *
 * 为什么不能只做第 2 层：后端对"多填了别家的"**不报错**（它只判"缺该有的"），
 * 所以界面上留着 MiniMax 的语速、下面写着 mimo，用户会以为它也生效。
 * 只做第 1 层也不够：漏清一处就会把一个不生效的值送进配置库。
 *
 * ## 没配语音时不说她"可用"
 *
 * `kind` 为空 = 还没配。那时的文案是"她暂时只会发文字"，**不是**"已就绪"。
 * 后端原话：没音色时运行时会降级发文字（`voice_requirement_issue` 的注释）。
 */
import { useRef, useState, type ChangeEvent, type Dispatch, type SetStateAction } from 'react';
import { api } from '../api';
import type { VoiceDraft, VoiceField } from '../voiceSettings';
import {
  explainUploadFailure,
  formatBytes,
  MAX_VOICE_BYTES,
  switchVoiceKind,
  VOICE_CONTAINERS,
  VOICE_KIND_OPTIONS,
  VOICE_WILLINGNESS_OPTIONS,
  WILLINGNESS_COMMON_NOTE,
  WILLINGNESS_NOT_PERSISTED,
  WILLINGNESS_PERSISTS,
  voiceIdLabel,
} from '../voiceSettings';
import type { VoiceKind, VoiceReferenceUpload, VoiceWillingness } from '../types';

/* ------------------------------------------------------------ 上传小状态 */

interface UploadState {
  phase: 'idle' | 'uploading' | 'done' | 'failed';
  /** 最近一次成功的结果（用于展示格式/大小）。失败时**不清**它： */
  /** 已经传上去的那一份仍然有效，界面不该假装它没了。 */
  result: VoiceReferenceUpload | null;
  reason: string | null;
  hint: string | null;
  retriable: boolean;
  /** 失败时保留文件名，好让用户知道"是哪一段没传上"。 */
  failedName: string | null;
}

const IDLE_UPLOAD: UploadState = {
  phase: 'idle',
  result: null,
  reason: null,
  hint: null,
  retriable: false,
  failedName: null,
};

/** 这一家的字段里，有没有一处被标了字段级错误。 */
function issueFor(issues: ReadonlyMap<VoiceField | 'kind', string>, field: VoiceField | 'kind'): string | null {
  return issues.get(field) ?? null;
}

function FieldError({ message, testid }: { message: string | null; testid: string }) {
  if (!message) return null;
  return (
    <span className="voice-form__error" role="alert" data-testid={testid}>
      {message}
    </span>
  );
}

/* ------------------------------------------------------------------ 组件 */

export interface VoiceSettingsProps {
  willingness: VoiceWillingness;
  onWillingness: (next: VoiceWillingness) => void;
  draft: VoiceDraft;
  onDraft: Dispatch<SetStateAction<VoiceDraft>>;
  /** 字段级错误（来自后端 `error.field` / `validation_errors`），键已收敛成短名。 */
  issues: ReadonlyMap<VoiceField | 'kind', string>;
  /** 保存进行中：上传与保存都要禁用，避免"保存时音色还在变"。 */
  saving: boolean;
  /**
   * 上传中的回调。**上传中必须禁用保存**：否则用户可能在上传返回之前保存，
   * 存下去的是一份没有 `reference_asset_id` 的 mimo 配置（后端会拦，但那是 422），
   * 或者更糟——先保存了旧值，上传成功后页面显示"已上传"，库里却是空的。
   */
  onUploadingChange?: (uploading: boolean) => void;
}

export function VoiceSettings({
  willingness,
  onWillingness,
  draft,
  onDraft,
  issues,
  saving,
  onUploadingChange,
}: VoiceSettingsProps) {
  const [upload, setUpload] = useState<UploadState>(IDLE_UPLOAD);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const uploading = upload.phase === 'uploading';
  const disabled = saving || uploading;
  const kind = draft.kind;

  const setKind = (next: VoiceKind | null) => {
    // 这一步就是"清残留"：见文件头注释。`switchVoiceKind` 是纯函数，
    // 测试可以直接钉住"切到 mimo 后 speed/vol/pitch 都是空"。
    onDraft((current) => switchVoiceKind(current, next));
  };

  const patch = (field: VoiceField, value: string) => {
    onDraft((current) => ({ ...current, [field]: value }));
  };

  const runUpload = async (file: File | undefined) => {
    if (!file) return;
    setUpload((current) => ({
      ...current,
      phase: 'uploading',
      reason: null,
      hint: null,
      failedName: null,
    }));
    onUploadingChange?.(true);
    try {
      const result = await api.uploadVoiceReference(file);
      // 成功后把 asset_id 填进表单——**这是上传唯一的目的**。
      onDraft((current) => ({ ...current, reference_asset_id: result.asset_id }));
      setUpload({
        phase: 'done',
        result,
        reason: null,
        hint: null,
        retriable: false,
        failedName: null,
      });
    } catch (error) {
      const explained = explainUploadFailure(error);
      // 失败后**不卡死**：状态回到可重试的形态，输入框清空以便重选同一个文件
      // （不清空的话，<input> 的 change 事件对同一个文件不会再触发）。
      setUpload({
        phase: 'failed',
        result: null,
        reason: explained.reason,
        hint: explained.hint,
        retriable: explained.retriable,
        failedName: file.name,
      });
    } finally {
      if (fileInput.current) fileInput.current.value = '';
      onUploadingChange?.(false);
    }
  };

  const onFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    void runUpload(file);
  };

  return (
    <div className="voice-form" data-testid="voice-settings">
      {/* ------------------------------------------------------ 意愿档位 */}
      <section className="voice-form__block" data-testid="voice-willingness">
        <h2 className="voice-form__title">她愿不愿意开口说话</h2>
        <p className="voice-form__intro">{WILLINGNESS_COMMON_NOTE}</p>
        <div className="voice-form__choices" role="radiogroup" aria-label="语音意愿档位">
          {VOICE_WILLINGNESS_OPTIONS.map((option) => {
            const checked = option.value === willingness;
            return (
              <label
                key={option.value}
                className={`voice-form__choice${checked ? ' voice-form__choice--on' : ''}`}
                data-testid={`voice-willingness-${option.value}`}
              >
                <input
                  type="radio"
                  name="voice-willingness"
                  value={option.value}
                  checked={checked}
                  disabled={saving}
                  data-testid={`voice-willingness-input-${option.value}`}
                  onChange={() => onWillingness(option.value)}
                />
                <span className="voice-form__choice-body">
                  <span className="voice-form__choice-label">{option.label}</span>
                  <span className="voice-form__choice-detail">{option.detail}</span>
                </span>
              </label>
            );
          })}
        </div>
      </section>

      {/* ---------------------------------------------------------- 音色 */}
      <section className="voice-form__block" data-testid="voice-voice">
        <h2 className="voice-form__title">她用谁的声音</h2>
        <p className="voice-form__intro">
          三家音色参数各管各的：同一个名字在三家是不同的值，页面不会替你翻译，也不会跨家预填。
          不选就等于不使用音色。
        </p>
        {/* 这一档现在存不进去时**必须说出来**：控件能点、值却到不了后端，
            比没有这个控件更糟（用户会以为改好了）。 */}
        {WILLINGNESS_PERSISTS ? null : (
          <p className="voice-form__warn" role="status" data-testid="voice-willingness-not-persisted">
            {WILLINGNESS_NOT_PERSISTED}
          </p>
        )}
      </section>

      {/* ---------------------------------------------------------- 音色 */}
      <section className="voice-form__block" data-testid="voice-voice">
        <h2 className="voice-form__title">她用谁的声音</h2>
        <p className="voice-form__intro">
          三家音色参数各管各的：同一个名字在三家是不同的值，页面不会替你翻译，也不会跨家预填。
          不选就等于不使用音色。
        </p>

        <div className="voice-form__choices voice-form__choices--kinds" role="radiogroup" aria-label="音色类型">
          <label
            className={`voice-form__choice${kind === null ? ' voice-form__choice--on' : ''}`}
            data-testid="voice-kind-none"
          >
            <input
              type="radio"
              name="voice-kind"
              value=""
              checked={kind === null}
              disabled={saving}
              data-testid="voice-kind-input-none"
              onChange={() => setKind(null)}
            />
            <span className="voice-form__choice-body">
              <span className="voice-form__choice-label">不使用音色</span>
              <span className="voice-form__choice-detail">
                她会只发文字。这是默认状态，也是最不会出错的状态。
              </span>
            </span>
          </label>
          {VOICE_KIND_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={`voice-form__choice${kind === option.value ? ' voice-form__choice--on' : ''}`}
              data-testid={`voice-kind-${option.value}`}
            >
              <input
                type="radio"
                name="voice-kind"
                value={option.value}
                checked={kind === option.value}
                disabled={saving}
                data-testid={`voice-kind-input-${option.value}`}
                onChange={() => setKind(option.value)}
              />
              <span className="voice-form__choice-body">
                <span className="voice-form__choice-label">{option.label}</span>
                <span className="voice-form__choice-detail">{option.detail}</span>
              </span>
            </label>
          ))}
        </div>

        {/* 没选 kind：**一个音色字段都不显示**，并如实说"只会发文字"。 */}
        {kind === null ? (
          <p className="voice-form__note" role="status" data-testid="voice-not-configured">
            还没有配音色：她现在只会发文字。选了上面任意一家，下面的参数才会出现并生效。
          </p>
        ) : null}

        {/* ------------------------------------------------ MiniMax / FishAudio */}
        {kind === 'minimax_tts' || kind === 'fishaudio_tts' ? (
          <label className="memory-form__row" data-testid="voice-field-voice_id">
            <span className="memory-form__head">
              <span className="memory-form__label">{voiceIdLabel(kind)}</span>
              <input
                className="memory-form__input voice-form__input--wide"
                type="text"
                value={draft.voice_id}
                disabled={saving}
                placeholder={kind === 'fishaudio_tts' ? 'FishAudio 的 reference_id' : 'MiniMax 的 voice_id'}
                data-testid="voice-input-voice_id"
                onChange={(event) => patch('voice_id', event.target.value)}
              />
            </span>
            <span className="memory-form__hint">
              去 {kind === 'fishaudio_tts' ? 'FishAudio' : 'MiniMax'} 那边复制现成的 id 填进来。
              这个值只对这一家有意义，换一家要重新填。
            </span>
            <FieldError message={issueFor(issues, 'voice_id')} testid="voice-error-voice_id" />
          </label>
        ) : null}

        {/* ------------------------------------------------------ MiniMax 专属 */}
        {kind === 'minimax_tts' ? (
          <div className="voice-form__grid" data-testid="voice-field-minimax-params">
            <label className="memory-form__row">
              <span className="memory-form__head">
                <span className="memory-form__label">语速</span>
                <input
                  className="memory-form__input"
                  type="number"
                  step="0.1"
                  min={0.5}
                  max={2}
                  value={draft.speed}
                  disabled={saving}
                  data-testid="voice-input-speed"
                  onChange={(event) => patch('speed', event.target.value)}
                />
              </span>
              <span className="memory-form__hint">0.5–2.0，1 是原速。</span>
              <FieldError message={issueFor(issues, 'speed')} testid="voice-error-speed" />
            </label>
            <label className="memory-form__row">
              <span className="memory-form__head">
                <span className="memory-form__label">音量</span>
                <input
                  className="memory-form__input"
                  type="number"
                  step="0.1"
                  min={0}
                  max={10}
                  value={draft.vol}
                  disabled={saving}
                  data-testid="voice-input-vol"
                  onChange={(event) => patch('vol', event.target.value)}
                />
              </span>
              <span className="memory-form__hint">0–10，1 是原始音量。</span>
              <FieldError message={issueFor(issues, 'vol')} testid="voice-error-vol" />
            </label>
            <label className="memory-form__row">
              <span className="memory-form__head">
                <span className="memory-form__label">音高</span>
                <input
                  className="memory-form__input"
                  type="number"
                  step="1"
                  min={-12}
                  max={12}
                  value={draft.pitch}
                  disabled={saving}
                  data-testid="voice-input-pitch"
                  onChange={(event) => patch('pitch', event.target.value)}
                />
              </span>
              <span className="memory-form__hint">−12–12 个半音，0 是不变调。</span>
              <FieldError message={issueFor(issues, 'pitch')} testid="voice-error-pitch" />
            </label>
            <label className="memory-form__row">
              <span className="memory-form__head">
                <span className="memory-form__label">情绪</span>
                <input
                  className="memory-form__input voice-form__input--wide"
                  type="text"
                  value={draft.emotion}
                  disabled={saving}
                  placeholder="留空 = 不指定"
                  data-testid="voice-input-emotion"
                  onChange={(event) => patch('emotion', event.target.value)}
                />
              </span>
              <span className="memory-form__hint">
                留空就不发这一项。猜一个情绪会让语音听起来莫名其妙，所以这里不预填。
              </span>
              <FieldError message={issueFor(issues, 'emotion')} testid="voice-error-emotion" />
            </label>
          </div>
        ) : null}

        {/* ---------------------------------------------------------- MiMo */}
        {kind === 'mimo_tts' ? (
          <div className="memory-form__row" data-testid="voice-field-reference_asset_id">
            <span className="memory-form__head">
              <span className="memory-form__label">参考音频</span>
              <input
                ref={fileInput}
                className="voice-form__file"
                type="file"
                // 只作提示：后端**按字节头**判定，不看扩展名也不看 Content-Type。
                accept=".wav,.mp3,.flac,.ogg,.m4a,audio/wav,audio/mpeg,audio/flac,audio/ogg,audio/mp4"
                disabled={disabled}
                data-testid="voice-upload-input"
                onChange={onFile}
              />
            </span>
            <span className="memory-form__hint">
              MiMo 走声音克隆：给一段这个人说话的音频，不用填 id。选完会自动上传，
              上限 {MAX_VOICE_BYTES / (1024 * 1024)} MB，支持 {VOICE_CONTAINERS.join(' / ')}。
              服务端按文件内容判定格式，扩展名写错也会被拒。
            </span>

            {uploading ? (
              <p className="voice-form__note" role="status" data-testid="voice-upload-uploading">
                正在上传…上传完成之前不能保存（否则存下去的音色没有参考音频）。
              </p>
            ) : null}

            {upload.phase === 'failed' ? (
              <div className="voice-form__upload-error" role="alert" data-testid="voice-upload-error">
                <strong data-testid="voice-upload-error-reason">{upload.reason}</strong>
                {upload.hint ? <div className="banner__detail">{upload.hint}</div> : null}
                {upload.failedName ? (
                  <div className="banner__detail">没传上去的是：{upload.failedName}</div>
                ) : null}
                <button
                  type="button"
                  className="button button--small"
                  disabled={disabled}
                  data-testid="voice-upload-retry"
                  // 重新打开文件选择器：失败后必须能立刻重试，不能卡死。
                  onClick={() => fileInput.current?.click()}
                >
                  {upload.retriable ? '重新选择并重试' : '换一段音频'}
                </button>
              </div>
            ) : null}

            {upload.result ? (
              <p className="voice-form__ok" role="status" data-testid="voice-upload-result">
                已上传：{upload.result.format.toUpperCase()} · {formatBytes(upload.result.size_bytes)}
                {` · asset_id ${upload.result.asset_id.slice(0, 8)}…`}
                {upload.result.retention_days === 0 ? '（不会自动过期）' : ''}
              </p>
            ) : null}

            {draft.reference_asset_id ? (
              <p className="voice-form__note" data-testid="voice-reference-asset">
                当前参考素材：<code>{draft.reference_asset_id.slice(0, 8)}…</code>
                （完整的 64 位 id 会随保存一起提交）
              </p>
            ) : (
              <p className="voice-form__note" role="status" data-testid="voice-reference-missing">
                还没上传参考音频：选了 MiMo 但没有素材时不能保存。
              </p>
            )}
            <FieldError
              message={issueFor(issues, 'reference_asset_id')}
              testid="voice-error-reference_asset_id"
            />
          </div>
        ) : null}

        {/* kind 这一项本身错了（后端只会在"未知类型/填了参数却没选类型"时报）。 */}
        <FieldError message={issueFor(issues, 'kind')} testid="voice-error-kind" />
      </section>
    </div>
  );
}
