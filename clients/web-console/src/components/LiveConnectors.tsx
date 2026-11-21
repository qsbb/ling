import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import type { LiveConnector, LiveConnectorInput } from '../live';
import { navigate } from '../route';
import { RefreshIcon } from './Icons';
import '../live-connectors.css';

function ConnectorForm({
  connector, busy, submit,
}: {
  connector: LiveConnector | undefined;
  busy: boolean;
  submit: (id: string, input: LiveConnectorInput, feature: 'bilibili' | null) => void;
}) {
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const field = (key: string) => String(form.get(key) || '').trim();
    const secret = (key: string) => form.has(`clear_${key}`) ? '' : field(key) || undefined;
    submit(connector?.id ?? field('id'), {
      display_name: field('display_name'),
      room_id: connector?.room_id ?? field('room_id'),
      enabled: form.has('enabled'),
      obs_control: form.has('obs_control'),
      source_ips: field('source_ips'),
      token: secret('token'),
      stage_token: secret('stage_token'),
      signing_secret: secret('signing_secret'),
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
      <label className="live-connector__check"><input name="obs_control" type="checkbox" defaultChecked={connector?.obs_control ?? false} />允许主播模式启停 OBS 推流</label>
    </div>
    <div><button className="button button--small" disabled={busy}>保存接入器</button></div>
  </form>;
}

export function LiveConnectors() {
  const [open, setOpen] = useState(false);
  const [connectors, setConnectors] = useState<LiveConnector[]>([]);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [formRevision, setFormRevision] = useState(0);
  const connector = connectors.find((item) => item.id === selected);
  const refresh = useCallback(async () => {
    const instances = await api.customConnectors();
    setConnectors(instances.connectors);
  }, []);

  useEffect(() => {
    if (!open) return;
    void refresh().catch((cause) => setError(explainError(cause).reason));
  }, [open, refresh]);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      await refresh();
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  }

  return <details className="live-connector" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>自定义接入器</summary>
    {open ? <div className="live-connector__body">
      <div className="live-connector__actions">
        <label>接入器<select className="field__input" value={selected} onChange={(event) => setSelected(event.target.value)} disabled={busy}>
          <option value="">添加接入器</option>
          {connectors.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
        </select></label>
        <button type="button" className="button button--small" onClick={() => void act(async () => {})}
          disabled={busy}><RefreshIcon /> 刷新</button>
        <button type="button" className="button button--small" onClick={() => navigate('/live')}>管理直播</button>
      </div>
      {error ? <p role="alert" className="panel__note">{error}</p> : null}
      {connector ? <div className="live-connector__status">
        <code>{connector.event_path}</code>
        <code>{connector.account_prefix}</code>
        <span>{connector.enabled ? '已启用' : '已停用'}</span>
        <span>{connector.last_received_at ? `收到事件于 ${new Date(connector.last_received_at).toLocaleTimeString()}` : '等待事件'}</span>
      </div> : null}
      <ConnectorForm key={`${connector?.id || 'new'}:${formRevision}`} connector={connector} busy={busy}
        submit={(id, body, feature) => void act(async () => {
          await api.saveCustomConnector(id, body, feature);
          setSelected(id);
          setFormRevision((value) => value + 1);
        })} />
    </div> : null}
  </details>;
}
