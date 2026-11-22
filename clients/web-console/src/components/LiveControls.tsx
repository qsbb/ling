import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import { liveStatus, stopLabel, type CurrentLive, type LiveSession } from '../live';
import { StopIcon } from './Icons';

export function useCurrentLive(owner: boolean, inLive: boolean) {
  const [data, setData] = useState<CurrentLive | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    if (!owner) return;
    const controller = new AbortController();
    let timer: number;
    let fast = inLive;

    async function poll() {
      try {
        const next = await api.currentLiveSession(controller.signal);
        if (controller.signal.aborted) return;
        setData(next);
        setError('');
        fast = inLive || (!!next.session && next.session.state !== 'ended') || !!next.planned_connector_id;
      } catch (cause) {
        if (!controller.signal.aborted) setError(explainError(cause).reason);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, fast ? 1_200 : 6_000);
      }
    }

    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [owner, inLive, revision]);

  return { data: owner ? data : null, error: owner ? error : '', refresh };
}

export function LiveHeader({ run, reachable, onGo, onStop }: {
  run: LiveSession;
  reachable: boolean;
  onGo: () => void;
  onStop: () => void;
}) {
  const presentation = liveStatus(run, reachable);
  return (
    <div className="live-header" data-tone={presentation.tone} data-testid="live-header-status">
      <button type="button" className="live-header__status" onClick={onGo}>
        <span className="live-dot" aria-hidden="true" />{presentation.label}
      </button>
      {run.state !== 'stopping' ? <button type="button" className="live-header__stop"
        data-testid="live-header-stop" aria-label={stopLabel(run)} title={stopLabel(run)} onClick={onStop}>
        <StopIcon /><span>{run.role === 'companion' && run.mode === 'broadcast' ? '退出' : '停止'}</span>
      </button> : null}
    </div>
  );
}

export function LiveStopDialog({ run, onClose, onStopped }: {
  run: LiveSession;
  onClose: () => void;
  onStopped: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const label = stopLabel(run);
  const detail = run.mode === 'preview'
    ? '结束本机预览和试音。'
    : run.role === 'companion'
      ? '结束她的语音、字幕和口型。主播的 OBS 推流和平台房间继续运行。'
      : '结束她的语音和本场拥有的 OBS 推流。平台房间状态会另行核实，必要时可在平台结束。';

  useEffect(() => { dialog.current?.showModal(); }, []);

  async function confirmStop() {
    setPending(true);
    setError('');
    try {
      await api.stopLiveSession(run.id);
      onStopped();
    } catch (cause) {
      setError(explainError(cause).reason);
      setPending(false);
    }
  }

  return <dialog className="live-dialog" ref={dialog} aria-labelledby="live-stop-title" onCancel={(event) => {
    event.preventDefault();
    if (!pending) onClose();
  }}>
    <h2 id="live-stop-title">{label}？</h2>
    <p>{detail}</p>
    {error ? <p role="alert">{error}</p> : null}
    <div className="live-actions">
      <button type="button" className="button button--small" autoFocus disabled={pending} onClick={onClose}>取消</button>
      <button type="button" className="button button--small button--danger" disabled={pending}
        data-testid="live-confirm-stop" onClick={() => void confirmStop()}>
        {pending ? '正在提交…' : '确认' + label}
      </button>
    </div>
  </dialog>;
}
