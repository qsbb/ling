import { useEffect, useState } from 'react';
import '../window-policy.css';
import { api, ApiError, type WindowDialoguePolicy, type WindowPolicyChange, type WindowPolicyPreview } from '../api';

type Selection = 'inherit' | 'allow' | 'require';

function selectionFor(override: boolean | null): Selection {
  return override === null ? 'inherit' : override ? 'allow' : 'require';
}

function changeFor(selection: Selection): WindowPolicyChange {
  return selection === 'inherit'
    ? { set: {}, clear: ['private_may_stay_silent'] }
    : { set: { private_may_stay_silent: selection === 'allow' }, clear: [] };
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : '无法连接服务端，请稍后重试';
}

export function WindowPolicy({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [policy, setPolicy] = useState<WindowDialoguePolicy | null>(null);
  const [selection, setSelection] = useState<Selection>('inherit');
  const [preview, setPreview] = useState<WindowPolicyPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    api.windowDialoguePolicy(sessionId).then((result) => {
      if (!active) return;
      setPolicy(result);
      setSelection(selectionFor(result.override));
    }).catch((reason: unknown) => {
      if (active) setError(errorText(reason));
    });
    return () => { active = false; };
  }, [sessionId]);

  async function previewChange() {
    setBusy(true);
    setError('');
    try {
      setPreview(await api.previewWindowPolicy(sessionId, changeFor(selection)));
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!preview) return;
    setBusy(true);
    setError('');
    try {
      const saved = await api.saveWindowPolicy(sessionId, changeFor(selection), preview.preview_token);
      setPolicy(saved);
      setSelection(selectionFor(saved.override));
      setPreview(null);
    } catch (reason) {
      setPreview(null);
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="window-policy" aria-label="会话设置">
      <div className="window-policy__heading">
        <strong>会话设置</strong>
        <button type="button" className="ghost-button" onClick={onClose}>关闭</button>
      </div>
      {policy ? (
        <>
          <label htmlFor="window-policy-silence">私聊回应</label>
          <select id="window-policy-silence" value={selection} disabled={busy}
            onChange={(event) => { setSelection(event.target.value as Selection); setPreview(null); }}>
            <option value="inherit">跟随全局设置</option>
            <option value="allow">允许选择沉默</option>
            <option value="require">优先回应</option>
          </select>
          <p>当前{policy.effective ? '允许选择沉默' : '优先回应'} · {policy.source === 'global' ? '来自全局设置' : '本会话设置'}</p>
          {preview ? (
            <div className="window-policy__preview">
              <p>保存后，本会话{preview.proposed.effective ? '允许选择沉默' : '优先回应'}，{preview.proposed.source === 'global' ? '跟随全局设置' : '使用本会话设置'}。</p>
              <button type="button" className="button button--primary button--small" disabled={busy} onClick={() => void save()}>确认保存</button>
              <button type="button" className="button button--small" disabled={busy} onClick={() => setPreview(null)}>返回修改</button>
            </div>
          ) : (
            <button type="button" className="button button--small" disabled={busy || selection === selectionFor(policy.override)} onClick={() => void previewChange()}>预览影响</button>
          )}
        </>
      ) : !error ? <p>正在读取会话设置…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
