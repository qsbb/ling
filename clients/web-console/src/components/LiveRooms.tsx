/**
 * 直播间的**连接参数**：表单、平台卡片、以及直播页里那个设置弹窗。
 *
 * ## 为什么单独一个文件
 *
 * 同一张表单要在两个地方出现：平台接入页（「平台」列表里的「直播间」，与
 * OneBot / QQ 官方机器人同级）和直播工作台（页头「设置」点开的弹窗）。
 * 两处若各写一份，就会出现"工作台改了显示名、接入页还是旧值"这类分叉——
 * 所以字段、默认值、提交语义只有这里一份，两处都渲染它。
 *
 * ## 几条不能违反的规矩
 *
 * 1. **令牌是"留空=不改"**。已配置的令牌只回显"已配置"，输入框空着表示不动它；
 *    要清空必须勾「清除」。把空字符串当成"清空"会让每次保存都悄悄抹掉令牌。
 * 2. **房间 ID 建好之后只读**。后端在房间变更时直接 409（房间已绑定），
 *    前端不该给出一个按了必然失败的输入框。
 * 3. **保存即建档**。`PUT /v1/settings/custom-connectors/{id}` 对新 id 是创建，
 *    所以从直播工作台填完参数保存，直播间就出现在「平台」列表里——不需要
 *    先去别处建一个空壳再回来填。
 * 4. **状态只说看得到的事**：接入器启用/停用、有没有配舞台令牌、最近一次收到
 *    事件的时间。没有实时探活，就不画一个像探活一样的绿点。
 */
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import type { LiveConnector, LiveConnectorInput } from '../live';
import { RefreshIcon } from './Icons';
import '../live-room.css';

/**
 * 读直播间列表。
 *
 * 放在这里导出，是因为**两个页面**都要它：平台接入页（列表给左栏状态点用）
 * 和直播工作台（设置弹窗里选房间）。各自写一份 fetch 就会有两套错误文案。
 */
export function useLiveRooms(): {
  connectors: LiveConnector[] | null;
  error: string;
  reload: () => void;
} {
  const [connectors, setConnectors] = useState<LiveConnector[] | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    void (async () => {
      try {
        const data = await api.customConnectors(controller.signal);
        if (disposed) return;
        setConnectors(data.connectors);
        setError('');
      } catch (cause) {
        if (disposed || controller.signal.aborted) return;
        setConnectors([]);
        setError(explainError(cause).reason);
      }
    })();
    return () => { disposed = true; controller.abort(); };
  }, [revision]);

  const reload = useCallback(() => setRevision((value) => value + 1), []);
  return { connectors, error, reload };
}

const SECRETS = [
  ['token', '上报令牌'],
  ['signing_secret', '签名密钥'],
  ['stage_token', '舞台令牌'],
] as const;

export interface LiveRoomFormProps {
  /** 省略 = 新建：多出「接入器 ID」与「类型」两个只在创建时可填的字段。 */
  connector?: LiveConnector;
  busy: boolean;
  submit: (id: string, input: LiveConnectorInput, feature: 'bilibili' | null) => void;
  submitLabel?: string;
  /** 表单末尾的附加控件（弹窗里的"取消"）。 */
  children?: ReactNode;
}

export function LiveRoomForm({
  connector, busy, submit, submitLabel = '保存直播间', children,
}: LiveRoomFormProps) {
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const field = (key: string) => String(form.get(key) ?? '').trim();
    const secret = (key: string) => (form.has(`clear_${key}`) ? '' : field(key) || undefined);
    submit(connector?.id ?? field('id'), {
      display_name: field('display_name'),
      room_id: connector?.room_id ?? field('room_id'),
      enabled: form.has('enabled'),
      obs_control: form.has('obs_control'),
      source_ips: field('source_ips'),
      token: secret('token'),
      signing_secret: secret('signing_secret'),
      stage_token: secret('stage_token'),
    }, connector?.feature ?? (form.get('feature') === 'bilibili' ? 'bilibili' : null));
  }

  return (
    <form className="live-room__form" onSubmit={save} data-testid="live-room-form">
      <label>类型<select className="field__input" name="feature" defaultValue={connector?.feature ?? 'custom'}
        disabled={!!connector}>
        <option value="custom">自定义 HTTP</option><option value="bilibili">B 站直播</option>
      </select></label>
      <label>接入器 ID<input className="field__input" name="id" required defaultValue={connector?.id}
        readOnly={!!connector} maxLength={16} pattern={'[a-z0-9][a-z0-9_\\-]{0,15}'}
        placeholder="room1" autoComplete="off" /></label>
      <label>显示名<input className="field__input" name="display_name" required maxLength={64}
        defaultValue={connector?.display_name} /></label>
      <label>房间 ID<input className="field__input" name="room_id" required maxLength={128}
        defaultValue={connector?.room_id} readOnly={!!connector} /></label>
      <label>来源 IP（可选）<input className="field__input" name="source_ips" maxLength={4096}
        defaultValue={connector?.source_ips} /></label>
      {SECRETS.map(([key, label]) => {
        const set = connector ? connector[`${key}_set` as const] : false;
        return (
          <div key={key}>
            <label>{label}<input className="field__input" name={key} type="password"
              placeholder={set ? '已配置' : undefined} autoComplete="new-password" maxLength={256} /></label>
            {set ? <label className="live-room__check"><input type="checkbox" name={`clear_${key}`} />清除{label}</label> : null}
          </div>
        );
      })}
      <div className="live-room__checks">
        <label className="live-room__check"><input type="checkbox" name="enabled"
          defaultChecked={connector?.enabled ?? true} />启用直播间</label>
        <label className="live-room__check"><input type="checkbox" name="obs_control"
          defaultChecked={connector?.obs_control ?? false} />允许主播模式启停 OBS 推流</label>
      </div>
      <div className="live-room__actions">
        <button className="button button--small" disabled={busy}>{submitLabel}</button>
        {children}
      </div>
    </form>
  );
}

/** 一个直播间的"看得见的状态"。没有探活，所以只说配置与最近事件。 */
function RoomStatus({ connector }: { connector: LiveConnector }) {
  const stage = connector.stage_observation;
  const stageText = !connector.enabled ? '已停用'
    : !connector.stage_token_set ? '待配置舞台令牌'
      : stage && Date.now() - Date.parse(stage.seen_at) <= 10_000 ? '舞台已连接' : '等待舞台连接';
  return (
    <div className="live-room__status">
      <code title="上报地址">{connector.event_path}</code>
      <code title="账号前缀">{connector.account_prefix}</code>
      <span>{stageText}</span>
      <span>{connector.last_received_at
        ? `收到事件于 ${new Date(connector.last_received_at).toLocaleTimeString()}`
        : '等待事件'}</span>
    </div>
  );
}

/**
 * 平台接入页里的「直播间」卡片：选一个直播间改参数，或者新建一个。
 *
 * 列表为空是**合法状态**（还没建过），所以这里画的是"新建"表单，而不是
 * 一句"请先去别处添加"。
 *
 * 列表由**页面**持有并传进来：左栏那个状态点要跟着列表变，两处各读一次会
 * 出现"左栏说没配、卡片里却有一个"的错位。
 */
export function LiveRoomsPanel({ connectors, onReload }: {
  connectors: LiveConnector[] | null;
  onReload: () => void;
}) {
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [formRevision, setFormRevision] = useState(0);
  const connector = connectors?.find((item) => item.id === selected);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      onReload();
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="live-room" data-testid="live-rooms-panel">
      <div className="live-room__head">
        <label>直播间<select className="field__input" value={selected} disabled={busy}
          onChange={(event) => { setSelected(event.target.value); setError(''); }}>
          <option value="">新建直播间</option>
          {(connectors ?? []).map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
        </select></label>
        <button type="button" className="button button--small" disabled={busy}
          onClick={() => void act(async () => {})}><RefreshIcon /> 刷新</button>
      </div>
      {error ? <p className="live-room__error" role="alert">{error}</p> : null}
      {connector ? <RoomStatus connector={connector} /> : null}
      {connectors === null ? <p className="live-room__note">正在读取直播间…</p> : (
        <LiveRoomForm key={`${connector?.id || 'new'}:${formRevision}`} connector={connector} busy={busy}
          submit={(id, body, feature) => void act(async () => {
            await api.saveCustomConnector(id, body, feature);
            setSelected(id);
            setFormRevision((value) => value + 1);
          })} />
      )}
    </div>
  );
}

export interface LiveRoomSettingsDialogProps {
  connectors: LiveConnector[];
  /** 当前选中的直播间 id；空字符串 = 新建。 */
  selectedId: string;
  onSelect: (id: string) => void;
  onClose: () => void;
  onSaved: (connector: LiveConnector) => void;
}

/**
 * 直播工作台的「设置」弹窗。
 *
 * 这里改的是**连接参数**（房间、令牌、OBS 控制），不是本场计划——计划留在
 * 页面上直接改，因为它是"这一场"的东西，而连接参数是"这个房间"的东西。
 */
export function LiveRoomSettingsDialog({
  connectors, selectedId, onSelect, onClose, onSaved,
}: LiveRoomSettingsDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const [formRevision, setFormRevision] = useState(0);
  const connector = connectors.find((item) => item.id === selectedId);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function save(id: string, body: LiveConnectorInput, feature: 'bilibili' | null) {
    setBusy(true);
    setError('');
    setSaved('');
    try {
      const connector = await api.saveCustomConnector(id, body, feature);
      onSaved(connector);
      setFormRevision((value) => value + 1);
      setSaved('已保存');
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog className="live-dialog live-room-dialog" ref={dialog} aria-labelledby="live-room-title"
      data-testid="live-room-settings"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <h2 id="live-room-title">直播间设置</h2>
      <div className="live-room">
        <label>直播间<select className="field__input" value={selectedId} disabled={busy}
          onChange={(event) => { setError(''); setSaved(''); onSelect(event.target.value); }}>
          <option value="">新建直播间</option>
          {connectors.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
        </select></label>
        {connector ? <RoomStatus connector={connector} /> : null}
        {error ? <p className="live-room__error" role="alert">{error}</p> : null}
        {saved ? <p className="live-room__note" role="status">{saved}</p> : null}
        <LiveRoomForm key={`${connector?.id || 'new'}:${formRevision}`} connector={connector} busy={busy}
          submit={(id, body, feature) => void save(id, body, feature)}>
          <button type="button" className="button button--small" disabled={busy} onClick={onClose}>关闭</button>
        </LiveRoomForm>
      </div>
    </dialog>
  );
}
