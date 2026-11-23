import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { MediaUploadResponse } from '../types';
import { AttachIcon, MicIcon, PlusIcon, SendIcon, StopIcon } from './Icons';
import { AudioInputComposer } from './AudioInputComposer';
import './composer.css';

interface ComposerProps {
  busy: boolean;
  stopping: boolean;
  disabled: boolean;
  sessionId: string | null;
  onSend: (text: string) => void;
  onStop: () => void;
  /** 附件或语音改变了服务端数据：让页面刷新消息与会话列表。 */
  onAttached?: () => void;
}

interface Attachment {
  evidenceId: string;
  name: string;
  mime: string;
}

const MAX_HEIGHT = 168;

/**
 * 收件箱式输入区：**一条横线里排完**文字、附件、语音、发送。
 *
 * 图七的问题是"语音工具散在输入区上方"——音频那一组（预听、发送语音、
 * 转成文字、时长上限说明）永远占一行高度，把输入框往下压，而且和文字输入
 * 没有任何视觉关系。这里改成：
 *
 * - 默认只露 `+`（附件，含语音上传）、`🎤`（录音）、输入框、发送；
 * - 点 `+` 展开附件区，点 `🎤` 展开语音区，两者互斥（同一时刻只开一个）；
 * - 限制（10 MiB / 120 秒 / 需要识别服务）只在**展开后**说；
 * - 有附件或录音待处理时，收起的浮层自动重新展开，不会让人以为东西没了。
 *
 * 中文输入法：`compositionstart/end` + `nativeEvent.isComposing` 双判据保留
 * （用 IME 选词时按 Enter 不该发送）。Enter 发送、Shift+Enter 换行、生成中
 * 可停止，这三条原样保留。
 */
export function Composer({
  busy,
  stopping,
  disabled,
  sessionId,
  onSend,
  onStop,
  onAttached,
}: ComposerProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const composingRef = useRef(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [attached, setAttached] = useState<Attachment | null>(null);
  const [attaching, setAttaching] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  /** 展开的是哪一块：`attach`（图片）或 `audio`（录音/上传语音）。 */
  const [panel, setPanel] = useState<'attach' | 'audio' | null>(null);

  /*
   * 刷新后先看有没有"上次没走完的语音任务"。
   *
   * 语音区收起时 `AudioInputComposer` 不挂载，它内部的 `localStorage`
   * 恢复逻辑也就不会跑——用户以为任务还在后台，其实界面已经不认识它了。
   * 所以挂载时读一次那个键，有残留就**直接展开语音区**，把恢复交还给它。
   * 这里只读 `assetId/requestId/mode` 的形状，不解释业务字段。
   */
  useEffect(() => {
    if (!sessionId) return;
    const panelKey = `ling-composer-panel:${sessionId}`;
    try {
      // 用户上次把哪一块留在展开状态，刷新后照旧——尤其是"刚取消完、正在
      // 看结果"这一屏，收回去会让人以为操作没生效。
      const remembered = window.sessionStorage.getItem(panelKey);
      if (remembered === 'audio' || remembered === 'attach') setPanel(remembered);
    } catch {
      /* 存储不可用时按默认收起走。 */
    }
    try {
      const raw = window.localStorage.getItem(`ling-asr:${sessionId}`);
      if (!raw) return;
      const saved = JSON.parse(raw) as { assetId?: unknown; requestId?: unknown; mode?: unknown };
      if (
        typeof saved.assetId === 'string'
        && typeof saved.requestId === 'string'
        && (saved.mode === 'send' || saved.mode === 'draft')
      ) {
        // 有未完成的任务：必须展开，否则恢复逻辑没有宿主。
        setPanel((current) => current ?? 'audio');
      }
    } catch {
      /* 存储不可用 / 内容坏了：当作没有残留，不要让页面崩掉。 */
    }
  }, [sessionId]);

  // 记住展开选择（按会话），让刷新与切回来都保持用户刚在用的那一块。
  useEffect(() => {
    if (!sessionId) return;
    try {
      if (panel === null) window.sessionStorage.removeItem(`ling-composer-panel:${sessionId}`);
      else window.sessionStorage.setItem(`ling-composer-panel:${sessionId}`, panel);
    } catch {
      /* 存储不可用：只是少了记忆，不影响当前交互。 */
    }
  }, [panel, sessionId]);

  const pickFile = () => {
    if (busy || disabled || !sessionId) return;
    setAttachError(null);
    fileRef.current?.click();
  };

  const upload = async (file: File) => {
    if (!sessionId) return;
    setAttaching(true);
    setAttachError(null);
    try {
      const res: MediaUploadResponse = await api.uploadMedia(sessionId, file);
      setAttached({ evidenceId: res.evidence_id, name: file.name, mime: res.mime });
  } catch (err) {
      // 服务端对缺授权给了可执行指引，直接显示它，不要自己编一句"上传失败"。
      setAttachError(err instanceof Error ? err.message : '上传失败');
    } finally {
      setAttaching(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${Math.min(node.scrollHeight, MAX_HEIGHT)}px`;
  }, [text]);

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || busy || disabled) return;
    onSend(trimmed);
    setText('');
    const node = textareaRef.current;
    if (node) {
      node.style.height = 'auto';
      const coarse = window.matchMedia('(pointer: coarse)').matches;
      if (coarse) node.blur();
      else node.focus();
    }
  };

  const afterAttach = useCallback(() => {
    onAttached?.();
  }, [onAttached]);

  return (
    <div className="composer">
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        className="composer__file"
        aria-label="选择图片"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
        }}
      />

      {panel === 'attach' ? (
        <div className="composer__panel" data-testid="composer-attach-panel">
          <div className="composer__panel-head">
            <span className="composer__panel-title">附件</span>
            <button
              type="button"
              className="composer__panel-close"
              aria-label="收起附件"
              onClick={() => setPanel(null)}
            >
              ×
            </button>
          </div>
          {attached ? (
            <div className="composer__attach" data-testid="composer-attach">
              <span className="composer__attach-chip">
                <span className="composer__attach-name" title={attached.name}>
                  {attached.name}
                </span>
                <button
                  type="button"
                  className="composer__attach-remove"
                  aria-label="移除图片"
                  onClick={() => setAttached(null)}
                >
                  ×
                </button>
              </span>
            </div>
          ) : null}
          {attaching ? <span className="composer__panel-note">上传中…</span> : null}
          {attachError ? (
            <span className="composer__attach-error" role="alert">
              {attachError}
            </span>
          ) : null}
          {/* 限制只在用到时说：格式与"她什么时候能看到"。 */}
          <p className="composer__panel-note">
            PNG / JPEG / WebP / GIF。图片由视觉模型在**下一轮**描述后她才能看到，需要在设置里绑好视觉模型。
          </p>
          <button
            type="button"
            className="button button--small"
            onClick={pickFile}
            disabled={busy || disabled || attaching || !sessionId}
          >
            选择图片
          </button>
        </div>
      ) : null}

      {panel === 'audio' && sessionId ? (
        <div className="composer__panel" data-testid="composer-audio-panel">
          <div className="composer__panel-head">
            <span className="composer__panel-title">语音</span>
            <button
              type="button"
              className="composer__panel-close"
              aria-label="收起语音"
              onClick={() => setPanel(null)}
            >
              ×
            </button>
          </div>
          <AudioInputComposer
            sessionId={sessionId}
            disabled={busy || disabled}
            onUpdated={afterAttach}
          />
        </div>
      ) : null}

      <div className="composer__box">
        <div className="composer__tools">
          <button
            type="button"
            className={`composer__tool ${panel === 'attach' ? 'is-open' : ''}`}
            onClick={() => setPanel((current) => (current === 'attach' ? null : 'attach'))}
            disabled={busy || disabled || !sessionId}
            aria-label="附件"
            aria-expanded={panel === 'attach'}
            title="附件"
            data-testid="composer-attach-toggle"
          >
            <PlusIcon />
          </button>
          <button
            type="button"
            className={`composer__tool ${panel === 'audio' ? 'is-open' : ''}`}
            onClick={() => setPanel((current) => (current === 'audio' ? null : 'audio'))}
            disabled={busy || disabled || !sessionId}
            aria-label="语音"
            aria-expanded={panel === 'audio'}
            title="语音"
            data-testid="composer-audio-toggle"
          >
            <MicIcon />
          </button>
        </div>
        <textarea
          ref={textareaRef}
          className="composer__input"
          value={text}
          rows={1}
          placeholder={busy ? '正在回应…可随时停止' : '说点什么吧'}
          aria-label="消息输入框"
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
          onKeyDown={(event) => {
            if (event.key !== 'Enter' || event.shiftKey) return;
            if (composingRef.current || event.nativeEvent.isComposing) return;
            event.preventDefault();
            submit();
          }}
        />
        <div className="composer__actions">
          {busy ? (
            <button
              type="button"
              className="composer__stop"
              onClick={onStop}
              disabled={stopping}
              title="停止当前生成"
            >
              <StopIcon />
              <span>{stopping ? '停止中…' : '停止'}</span>
            </button>
          ) : null}
          <button
            type="button"
            className="composer__send"
            onClick={submit}
            disabled={busy || disabled || text.trim().length === 0}
            aria-label="发送消息"
            title="发送（Enter）"
          >
            <SendIcon />
          </button>
        </div>
      </div>
      <div className="composer__hint">
        <span className="composer__hint-keys">Enter 发送 · Shift + Enter 换行</span>
        {attached ? (
          <span className="composer__hint-pending" data-testid="composer-attached-hint">
            <AttachIcon size={12} /> 已附 1 张图片
          </span>
        ) : null}
        {panel === null ? (
          <span className="composer__hint-mode" aria-hidden="true">
            <AttachIcon size={12} /> 附件 · 语音 · 文字
          </span>
        ) : null}
      </div>
    </div>
  );
}
