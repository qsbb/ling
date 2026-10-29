import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { MediaUploadResponse } from '../types';
import { AttachIcon, SendIcon, StopIcon } from './Icons';

interface ComposerProps {
  busy: boolean;
  stopping: boolean;
  disabled: boolean;
  sessionId: string | null;
  onSend: (text: string) => void;
  onStop: () => void;
}

interface Attachment {
  evidenceId: string;
  name: string;
  mime: string;
}

const MAX_HEIGHT = 168;

export function Composer({
  busy,
  stopping,
  disabled,
  sessionId,
  onSend,
  onStop,
}: ComposerProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const composingRef = useRef(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [attached, setAttached] = useState<Attachment | null>(null);
  const [attaching, setAttaching] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);

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
      {attached || attaching || attachError ? (
        <div className="composer__attach" data-testid="composer-attach">
          {attaching ? <span className="composer__attach-note">上传中…</span> : null}
          {attached ? (
            <>
              <span className="composer__attach-chip">
                🖼 {attached.name}
                <button
                  type="button"
                  className="composer__attach-remove"
                  aria-label="移除图片"
                  onClick={() => setAttached(null)}
                >
                  ×
                </button>
              </span>
              {/* 说清"她什么时候能看到"：描述是异步生成的，
                  所以是"下一轮"而不是"这一条"。 */}
              <span className="composer__attach-note">
                已上传，她下一轮就能看到（需要先配好视觉模型）
              </span>
            </>
          ) : null}
          {attachError ? (
            <span className="composer__attach-error" role="alert">
              {attachError}
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="composer__box">
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
          <button
            type="button"
            className="composer__attach-btn"
            onClick={pickFile}
            disabled={busy || disabled || attaching || !sessionId}
            aria-label="发送图片"
            title="发送图片"
          >
            <AttachIcon />
          </button>
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
        <span className="composer__hint-mode">消息会写入服务端权威时间线</span>
      </div>
    </div>
  );
}
