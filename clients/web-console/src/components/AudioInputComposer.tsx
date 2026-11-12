import { useEffect, useRef, useState } from 'react';
import { api, ApiError, type TranscriptionJob } from '../api';

interface Props {
  sessionId: string;
  disabled: boolean;
  onUpdated: () => void;
}

interface SavedRequest {
  assetId: string;
  requestId: string;
  mode: 'send' | 'draft';
  jobId?: string;
}

const MAX_AUDIO_BYTES = 10 * 1024 * 1024;
const MAX_RECORDING_MS = 120_000;

function readSaved(key: string): SavedRequest | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const saved = JSON.parse(raw) as SavedRequest;
    if (saved && typeof saved.assetId === 'string' && typeof saved.requestId === 'string'
      && (saved.mode === 'send' || saved.mode === 'draft')) return saved;
  } catch { /* 存储不可用时仍允许当前页面发送。 */ }
  return null;
}

function saveRequest(key: string, request: SavedRequest | null): void {
  try {
    if (request) localStorage.setItem(key, JSON.stringify(request));
    else localStorage.removeItem(key);
  } catch { /* 浏览器可能禁用本地存储。 */ }
}

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return '语音处理失败，请重试或输入文字';
}

export function AudioInputComposer({ sessionId, disabled, onUpdated }: Props) {
  const [clip, setClip] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<TranscriptionJob | null>(null);
  const [saved, setSaved] = useState<SavedRequest | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const updated = useRef(onUpdated);
  updated.current = onUpdated;
  const storageKey = `ling-asr:${sessionId}`;

  function remember(request: SavedRequest | null) {
    saveRequest(storageKey, request);
    setSaved(request);
  }

  async function perform(action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setError('');
    setBusy(true);
    try {
      await action();
    } catch (cause) {
      if (mounted.current) setError(messageOf(cause));
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  async function restore(request: SavedRequest) {
    const next = request.jobId
      ? await api.transcription(sessionId, request.jobId)
      : await api.createTranscription(sessionId, request.assetId, request.requestId, request.mode);
    if (!mounted.current) return;
    remember({ ...request, jobId: next.job_id });
    setJob(next);
    setClip(null);
    updated.current();
  }

  useEffect(() => {
    mounted.current = true;
    const request = readSaved(storageKey);
    if (request) {
      setSaved(request);
      void perform(() => restore(request));
    }
    return () => {
      mounted.current = false;
      const rec = recorder.current;
      if (rec) {
        rec.onstop = null;
        rec.ondataavailable = null;
        rec.onerror = null;
        if (rec.state !== 'inactive') rec.stop();
      }
      stream.current?.getTracks().forEach((track) => track.stop());
      if (timer.current) clearTimeout(timer.current);
    };
  }, [sessionId, storageKey]);

  useEffect(() => {
    if (!clip) {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(clip);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [clip]);

  useEffect(() => {
    if (job?.state === 'succeeded' && job.mode === 'draft') setDraft(job.text);
  }, [job?.job_id, job?.revision, job?.state, job?.mode]);

  useEffect(() => {
    if (!job) return;
    if (job.submission === 'committed' || job.submission === 'suppressed' || job.state === 'cancelled') {
      remember(null);
      updated.current();
      return;
    }
    const pending = ['queued', 'running'].includes(job.state)
      || (job.state === 'succeeded' && job.mode === 'send');
    if (!pending) return;
    const controller = new AbortController();
    let timerId: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await api.transcription(sessionId, job!.job_id, controller.signal);
        if (!controller.signal.aborted) {
          setError('');
          setJob(next);
          if (next.state !== job!.state || next.submission !== job!.submission) updated.current();
        }
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(messageOf(cause));
        timerId = setTimeout(() => void poll(), 3000);
      }
    }
    timerId = setTimeout(() => void poll(), 1000);
    return () => {
      controller.abort();
      clearTimeout(timerId);
    };
  }, [job, sessionId, storageKey]);

  function selectClip(file: File | null) {
    setError('');
    if (file && file.size > MAX_AUDIO_BYTES) {
      setError('语音文件超过 10 MiB，请选择更小的文件');
      return;
    }
    if (file && file.size === 0) {
      setError('语音文件为空，请重新录音或选择文件');
      return;
    }
    setClip(file);
    setJob(null);
  }

  async function record() {
    await perform(async () => {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
        throw new ApiError(0, 'unsupported', '当前浏览器无法录音，请上传音频文件');
      }
      let media: MediaStream;
      try {
        media = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        throw new ApiError(0, 'microphone_denied', '无法使用麦克风，请检查录音权限或上传音频');
      }
      if (!mounted.current) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = media;
      try {
        const mime = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']
          .find((type) => MediaRecorder.isTypeSupported(type));
        const rec = new MediaRecorder(media, mime ? { mimeType: mime } : undefined);
        recorder.current = rec;
        const chunks: Blob[] = [];
        let failed = false;
        rec.ondataavailable = (event) => {
          if (event.data.size) chunks.push(event.data);
        };
        rec.onstop = () => {
          media.getTracks().forEach((track) => track.stop());
          stream.current = null;
          if (timer.current) clearTimeout(timer.current);
          if (!mounted.current) return;
          setRecording(false);
          if (!failed) selectClip(new File(chunks, 'recording.audio', { type: rec.mimeType }));
        };
        rec.onerror = () => {
          failed = true;
          setError('录音失败，请检查麦克风或上传音频');
          if (rec.state !== 'inactive') rec.stop();
          media.getTracks().forEach((track) => track.stop());
          setRecording(false);
        };
        rec.start();
        setRecording(true);
        setClip(null);
        setJob(null);
        timer.current = setTimeout(() => {
          if (rec.state !== 'inactive') rec.stop();
        }, MAX_RECORDING_MS);
      } catch {
        media.getTracks().forEach((track) => track.stop());
        stream.current = null;
        throw new ApiError(0, 'recording_failed', '录音无法启动，请重试或上传音频');
      }
    });
  }

  async function send(mode: 'send' | 'draft') {
    await perform(async () => {
      if (!clip) return;
      const uploaded = await api.uploadAudio(sessionId, clip);
      const request: SavedRequest = { assetId: uploaded.asset_id, requestId: crypto.randomUUID(), mode };
      if (!mounted.current) return;
      remember(request);
      await restore(request);
    });
  }

  const pending = !!job && (['queued', 'running'].includes(job.state)
    || (job.mode === 'send' && job.submission !== 'committed' && job.submission !== 'suppressed' && job.state === 'succeeded'));
  const editingDraft = job?.state === 'succeeded' && job.mode === 'draft' && job.submission === 'pending';
  const blocked = disabled || busy || pending || !!saved;

  return (
    <div className="composer__audio" role="group" aria-label="语音输入">
      <span className="muted">语音最长 120 秒、最大 10 MiB，将交给设置中绑定的识别服务处理。</span>
      <input ref={input} type="file" accept="audio/*" hidden aria-label="选择语音文件"
        onChange={(event) => { selectClip(event.target.files?.[0] ?? null); event.target.value = ''; }} />
      <button type="button" disabled={!recording && blocked}
        onClick={() => recording ? recorder.current?.stop() : void record()}>
        {recording ? '停止录音' : '录音'}
      </button>
      <button type="button" disabled={blocked || recording} onClick={() => input.current?.click()}>上传语音</button>
      {preview && !saved && <>
        <audio controls src={preview} aria-label="语音预听" />
        <button type="button" disabled={busy || disabled} onClick={() => void send('send')}>发送语音</button>
        <button type="button" disabled={busy || disabled} onClick={() => void send('draft')}>转成文字</button>
        <button type="button" disabled={busy} onClick={() => selectClip(null)}>移除录音</button>
      </>}
      {saved && !job && <>
        <span role="status">正在确认语音提交状态…</span>
        <button type="button" disabled={busy} onClick={() => void perform(() => restore(saved))}>重试提交</button>
      </>}
      {pending && <>
        <span role="status">{job?.state === 'queued' ? '等待识别…' : job?.state === 'running' ? '正在识别…' : '正在处理回复…'}</span>
        <button type="button" disabled={busy || job?.submission === 'dispatching'} onClick={() => void perform(async () => {
          if (job) setJob(await api.cancelTranscription(sessionId, job.job_id));
        })}>取消识别</button>
      </>}
      {editingDraft && <>
        <textarea aria-label="编辑转写文字" value={draft} maxLength={8000} onChange={(event) => setDraft(event.target.value)} />
        <button type="button" disabled={busy || disabled || !draft.trim()} onClick={() => void perform(async () => {
          setJob(await api.commitTranscription(sessionId, job, draft));
        })}>发送转写文字</button>
        <button type="button" disabled={busy} onClick={() => void perform(async () => {
          setJob(await api.cancelTranscription(sessionId, job.job_id));
        })}>丢弃草稿</button>
      </>}
      {job?.submission === 'committed' && <span role="status">语音已处理</span>}
      {job?.submission === 'suppressed' && <span role="status">语音处理已停止</span>}
      {job && ['failed', 'unknown', 'expired', 'unintelligible', 'no_speech'].includes(job.state) && <>
        <span role="status">{job.error?.message ?? '这段语音没有识别出可用文字，可以重录或输入文字'}</span>
        {job.submission === 'pending' && job.state !== 'no_speech' && <button type="button" disabled={busy || disabled} onClick={() => void perform(async () => {
          setJob(await api.retryTranscription(sessionId, job.job_id));
        })}>重试识别</button>}
        {job.submission === 'pending' && <button type="button" disabled={busy} onClick={() => void perform(async () => {
          setJob(await api.cancelTranscription(sessionId, job.job_id));
        })}>移除语音任务</button>}
      </>}
      {error && <span role="alert">{error}</span>}
    </div>
  );
}
