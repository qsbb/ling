import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import type { LiveConnector, LiveConnectorInput, LiveSession, LiveViewer } from '../live';
import { RefreshIcon } from './Icons';
import '../live-connectors.css';

const labels: Record<LiveSession['state'], string> = {
  preview: '预览', starting: '准备开播', live: '正在直播', stopping: '正在停止', ended: '已结束',
};

function localDateTime(text: string | undefined): string {
  if (!text) return '';
  const time = new Date(text);
  return new Date(time.getTime() - time.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function ConnectorForm({
  connector, busy, submit,
}: {
  connector: LiveConnector | undefined;
  busy: boolean;
  submit: (id: string, input: LiveConnectorInput, feature: 'bilibili' | null) => void;
}) {
  const [automatic, setAutomatic] = useState(!!connector?.autonomous_window);

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const field = (key: string) => String(form.get(key) || '').trim();
    const secret = (key: string) => form.has(`clear_${key}`) ? '' : field(key) || undefined;
    submit(connector?.id ?? field('id'), {
      display_name: field('display_name'),
      room_id: connector?.room_id ?? field('room_id'),
      enabled: form.has('enabled'),
      public_topic: field('public_topic'),
      max_minutes: Number(form.get('max_minutes')),
      obs_control: form.has('obs_control'),
      source_ips: field('source_ips'),
      token: secret('token'),
      stage_token: secret('stage_token'),
      signing_secret: secret('signing_secret'),
      autonomous_window: automatic ? {
        starts_at: new Date(field('starts_at')).toISOString(),
        ends_at: new Date(field('ends_at')).toISOString(),
      } : null,
    }, connector?.feature ?? (form.get('feature') === 'bilibili' ? 'bilibili' : null));
  }

  return <form className="live-connector__form" onSubmit={save}>
    <label>类型<select className="field__input" name="feature" defaultValue={connector?.feature ?? 'custom'} disabled={!!connector}>
      <option value="custom">自定义 HTTP</option><option value="bilibili">B 站直播</option>
    </select></label>
    <label>接入器 ID<input className="field__input" name="id" required defaultValue={connector?.id} readOnly={!!connector}
      maxLength={16} pattern={'[a-z0-9][a-z0-9_\\-]{0,15}'} /></label>
    <label>显示名<input className="field__input" name="display_name" required maxLength={64} defaultValue={connector?.display_name} /></label>
    <label>房间 ID<input className="field__input" name="room_id" required maxLength={128} defaultValue={connector?.room_id} readOnly={!!connector} /></label>
    <label>公开主题<input className="field__input" name="public_topic" maxLength={400} defaultValue={connector?.public_topic} /></label>
    <label>单场上限（分钟）<input className="field__input" name="max_minutes" type="number" min={1} max={240} required defaultValue={connector?.max_minutes ?? 30} /></label>
    <label>来源 IP（可选）<input className="field__input" name="source_ips" defaultValue={connector?.source_ips} maxLength={4096} /></label>
    {([
      ['token', '上报令牌', connector?.token_set],
      ['signing_secret', '签名密钥', connector?.signing_secret_set],
      ['stage_token', '舞台令牌', connector?.stage_token_set],
    ] as const).map(([key, label, set]) => <div key={key}>
      <label>{label}<input className="field__input" name={key} type="password" placeholder={set ? '已配置' : undefined}
        autoComplete="new-password" maxLength={256} /></label>
      {set ? <label className="live-connector__check"><input type="checkbox" name={`clear_${key}`} />清除{label}</label> : null}
    </div>)}
    <div className="live-connector__actions">
      <label className="live-connector__check"><input name="enabled" type="checkbox" defaultChecked={connector?.enabled ?? true} />启用接入器</label>
      <label className="live-connector__check"><input name="obs_control" type="checkbox" defaultChecked={connector?.obs_control ?? false}
        onChange={(event) => { if (!event.target.checked) setAutomatic(false); }} />允许启停 OBS 推流</label>
      <label className="live-connector__check"><input type="checkbox" checked={automatic}
        onChange={(event) => setAutomatic(event.target.checked)} />授权一次自主开播</label>
    </div>
    {automatic ? <>
      <label>授权开始<input className="field__input" name="starts_at" type="datetime-local" required defaultValue={localDateTime(connector?.autonomous_window?.starts_at)} /></label>
      <label>授权结束<input className="field__input" name="ends_at" type="datetime-local" required defaultValue={localDateTime(connector?.autonomous_window?.ends_at)} /></label>
    </> : null}
    <div><button className="button button--small" disabled={busy}>保存接入器</button></div>
  </form>;
}

export function LiveConnectors() {
  const [open, setOpen] = useState(false);
  const [connectors, setConnectors] = useState<LiveConnector[]>([]);
  const [session, setSession] = useState<LiveSession | null>(null);
  const [selected, setSelected] = useState('');
  const [saved, setSaved] = useState<Array<LiveViewer & { note: string }>>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [formRevision, setFormRevision] = useState(0);
  const connector = connectors.find((item) => item.id === selected);
  const active = session && session.state !== 'ended';
  const details = session?.connector_id === selected ? session : null;

  const refresh = useCallback(async (id: string) => {
    const [instances, current, viewers] = await Promise.all([
      api.customConnectors(), api.currentLiveSession(),
      id ? api.liveViewers(id) : Promise.resolve({ viewers: [] }),
    ]);
    setConnectors(instances.connectors);
    setSession(current.session);
    setSaved(viewers.viewers);
  }, []);

  useEffect(() => {
    if (!open) return;
    void refresh(selected).catch((cause) => setError(explainError(cause).reason));
    let closed = false;
    const timer = window.setInterval(() => {
      void api.currentLiveSession().then((out) => {
        if (!closed) setSession(out.session);
      }).catch((cause) => {
        if (!closed) setError(explainError(cause).reason);
      });
    }, 2500);
    return () => {
      closed = true;
      window.clearInterval(timer);
    };
  }, [open, selected, refresh]);

  async function act(action: () => Promise<unknown>, id = selected) {
    setBusy(true);
    setError('');
    try {
      await action();
      await refresh(id);
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  }

  return <details className="live-connector" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>自定义接入与直播</summary>
    {open ? <div className="live-connector__body">
      <div className="live-connector__actions">
        <label>接入器<select className="field__input" value={selected} onChange={(event) => setSelected(event.target.value)} disabled={busy}>
          <option value="">添加接入器</option>
          {connectors.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
        </select></label>
        <button type="button" className="button button--small" onClick={() => void act(async () => {})}
          disabled={busy}><RefreshIcon /> 刷新</button>
      </div>
      {error ? <p role="alert" className="panel__note">{error}</p> : null}
      {connector ? <div className="live-connector__status">
        <code>{connector.event_path}</code>
        <span>{connector.enabled ? '已启用' : '已停用'}</span>
        <span>{connector.last_received_at ? `收到事件于 ${new Date(connector.last_received_at).toLocaleTimeString()}` : '等待事件'}</span>
        {connector.autonomous_state ? <span>自主开播 {{
          deciding: '正在决定', started: '已决定开场', wait: '选择等待', unavailable: '决策失败',
        }[connector.autonomous_state]}</span> : null}
      </div> : null}
      <ConnectorForm key={`${connector?.id || 'new'}:${formRevision}`} connector={connector} busy={busy}
        submit={(id, body, feature) => void act(async () => {
          await api.saveCustomConnector(id, body, feature);
          setSelected(id);
          setFormRevision((value) => value + 1);
        }, id)} />
      {connector ? <section aria-label="直播控制" className="live-connector__session">
        <div className="live-connector__actions">
          <strong>{details ? labels[details.state] : '未开场'}</strong>
          {!active && connector.enabled ? <>
            <button className="button button--small" disabled={busy || !connector.stage_token_set}
              onClick={() => void act(() => api.startLiveSession(connector.id, 'preview'))}>预览</button>
            {connector.obs_control ? <button className="button button--small" disabled={busy || !connector.stage_token_set}
              onClick={() => {
                if (window.confirm(`确认启动 ${connector.display_name} 的 OBS 推流？`)) {
                  void act(() => api.startLiveSession(connector.id, 'broadcast'));
                }
              }}>开播</button> : null}
          </> : null}
          {active && details ? <button className="button button--small button--danger" disabled={busy || details.state === 'stopping'}
            onClick={() => void act(() => api.stopLiveSession(details.id))}>停止</button> : null}
        </div>
        {details ? <>
          <div className="live-connector__status">
            <span>OBS {details.obs_reconnecting ? '重连中' : details.obs_active == null ? '等待舞台' : details.obs_active ? '推流中' : '已停止'}</span>
            <span>房间 {details.platform_live == null ? '等待确认' : details.platform_live ? '在播' : '未开播'}</span>
            <span>待回弹幕 {details.pending_comments}</span>
          </div>
          {details.detail ? <p className="panel__note">{details.detail}</p> : null}
          {details.viewers.filter((viewer) => !saved.some((item) => item.account_ref === viewer.account_ref)).map((viewer) => (
            <form className="live-connector__viewer" key={viewer.account_ref} onSubmit={(event) => {
              event.preventDefault();
              const note = String(new FormData(event.currentTarget).get('note') || '');
              void act(() => api.rememberLiveViewer(details.id, viewer.account_ref, note));
            }}>
              <strong>{viewer.display_name || '观众'}</strong>
              <code title={viewer.account_ref}>…{viewer.account_ref.slice(-12)}</code>
              <input className="field__input" aria-label="房间公开备注" name="note" placeholder="房间公开备注" maxLength={300} />
              <button className="button button--small" disabled={busy}>记住</button>
            </form>
          ))}
        </> : null}
        {saved.length ? <>
          <h3>已记住的观众</h3>
          {saved.map((viewer) => <div className="live-connector__viewer" key={viewer.account_ref}>
            <strong>{viewer.display_name || '观众'}</strong>
            <code>…{viewer.account_ref.slice(-12)}</code>
            <span>{viewer.note}</span>
            <button className="button button--small" disabled={busy}
              onClick={() => void act(() => api.forgetLiveViewer(connector.id, viewer.account_ref))}>清除备注</button>
          </div>)}
          <a href="/relationships">管理账号关联</a>
        </> : null}
      </section> : null}
    </div> : null}
  </details>;
}
