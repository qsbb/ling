import { useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import {
  BILIBILI_LIVE_CENTER, liveRoomUrl, liveStatus, stopLabel,
  type CurrentLive, type LiveConnector, type LivePlan, type LiveRole, type LiveSession, type LiveViewer,
} from '../live';
import { CheckIcon, RefreshIcon, StopIcon } from './Icons';

interface LivePageProps {
  current: CurrentLive | null;
  statusError: string;
  refresh: () => void;
  onGo: (path: string) => void;
  onStop: () => void;
}

function localDateTime(text?: string): string {
  if (!text) return '';
  const date = new Date(text);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function clock(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function duration(from: string, to?: string | null): string {
  const seconds = Math.max(0, Math.floor(((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes + ':' + String(seconds % 60).padStart(2, '0');
}

function PlanForm({ connector, connected, busy, onSave, onClear }: {
  connector: LiveConnector;
  connected: boolean;
  busy: boolean;
  onSave: (plan: LivePlan, action: 'save' | 'preview' | 'broadcast') => void;
  onClear: () => void;
}) {
  const saved = connector.live_plan;
  const [role, setRole] = useState<LiveRole>(saved.role);
  const [automatic, setAutomatic] = useState(!!saved.autonomous_window && connector.autonomous_state !== 'started');
  const isHost = role === 'host';
  const stage = connector.stage_observation;
  const ready = connected && connector.enabled && connector.stage_token_set && !stage?.obs_reconnecting;
  const obsActive = stage?.obs_active === true;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const read = (field: string) => String(fields.get(field) ?? '').trim();
    const action = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value');
    if (action === 'broadcast' && !window.confirm(isHost
      ? '启动「' + connector.display_name + '」的 OBS 推流？'
      : '加入「' + connector.display_name + '」的直播？')) return;
    onSave({
      role,
      public_topic: read('public_topic'),
      host_name: isHost ? '' : read('host_name'),
      companion_auto_reply: !isHost && fields.has('companion_auto_reply'),
      max_minutes: Number(read('max_minutes')),
      autonomous_window: isHost && automatic && connector.obs_control ? {
        starts_at: new Date(read('starts_at')).toISOString(),
        ends_at: new Date(read('ends_at')).toISOString(),
      } : null,
    }, action === 'preview' || action === 'broadcast' ? action : 'save');
  }

  return (
    <form className="live-plan" onSubmit={submit} data-testid="live-plan">
      <fieldset className="live-role">
        <legend>本场角色</legend>
        <label><input type="radio" name="role" value="host" checked={isHost} onChange={() => setRole('host')} />主播</label>
        <label><input type="radio" name="role" value="companion" checked={!isHost} onChange={() => setRole('companion')} />陪伴助手</label>
      </fieldset>
      <label>公开主题<input name="public_topic" maxLength={400} className="field__input" defaultValue={saved.public_topic} /></label>
      {!isHost ? <label>主播称呼<input name="host_name" maxLength={64} className="field__input" defaultValue={saved.host_name} /></label> : null}
      <label>本场上限（分钟）<input name="max_minutes" type="number" min={1} max={240} required
        className="field__input" defaultValue={saved.max_minutes} /></label>
      {isHost ? (
        <label>开播决定<select className="field__input" value={automatic ? 'automatic' : 'manual'}
          onChange={(event) => setAutomatic(event.target.value === 'automatic')}>
          <option value="manual">我来开播</option>
          <option value="automatic" disabled={!connector.obs_control}>她在授权时段内决定</option>
        </select></label>
      ) : (
        <label className="live-check"><input type="checkbox" name="companion_auto_reply"
          defaultChecked={saved.companion_auto_reply} />允许她挑选弹幕回应</label>
      )}
      {isHost && automatic && connector.obs_control ? <>
        <label>授权开始<input className="field__input" name="starts_at" type="datetime-local" required
          defaultValue={localDateTime(saved.autonomous_window?.starts_at)} /></label>
        <label>授权结束<input className="field__input" name="ends_at" type="datetime-local" required
          defaultValue={localDateTime(saved.autonomous_window?.ends_at)} /></label>
      </> : null}
      <div className="live-actions live-plan__actions">
        <button type="submit" value="save" className="button button--small"
          disabled={busy || !connector.enabled}>保存计划</button>
        <button type="submit" value="preview" className="button button--small"
          data-testid="live-preview" disabled={busy || !ready || obsActive}>预览</button>
        <button type="submit" value="broadcast" className="button button--primary button--small"
          data-testid="live-start" disabled={busy || !ready || (isHost ? !connector.obs_control || obsActive || automatic : !obsActive)}>
          {isHost ? '开始直播' : '加入直播'}
        </button>
        {connector.plan_pending ? <button type="button" className="button button--small" disabled={busy}
          onClick={onClear}>{saved.autonomous_window ? '撤销开播授权' : '取消计划'}</button> : null}
      </div>
      {connector.plan_pending && connector.autonomous_state ? <p className="live-note live-plan__actions" role="status">{{
        deciding: '她正在决定是否开场',
        started: '本次授权已使用',
        wait: '她选择等待',
        unavailable: '这次未能完成开播决定',
      }[connector.autonomous_state]}</p> : null}
    </form>
  );
}

function RunStatus({ run, connector, reachable, refresh }: {
  run: LiveSession;
  connector?: LiveConnector;
  reachable: boolean;
  refresh: () => void;
}) {
  const status = liveStatus(run, reachable);
  const settled = run.state === 'ended';
  const stopping = run.state === 'stopping';
  const roomLabel = connector?.feature === 'bilibili' ? 'B 站房间' : '平台房间';
  const obs = !reachable || run.obs_active == null ? '待核实'
    : run.obs_reconnecting ? '正在重连' : run.obs_active ? '推流中' : '未推流';
  const platform = !reachable || run.platform_live == null ? '待核实'
    : run.platform_live ? '已开播' : '未开播';
  return (
    <section className="live-section" aria-label="运行状态">
      <div className="live-state" data-tone={status.tone} data-testid="live-run-status">
        <span className="live-dot" aria-hidden="true" />
        <strong>{status.label}</strong>
        {status.tone === 'live' && run.confirmed_live_at ? <time>{duration(run.confirmed_live_at)}</time> : null}
        {run.mode === 'preview' && !settled && !stopping ? <span className="live-state__tag">预览</span> : null}
      </div>
      {!settled || (run.role === 'host' && run.mode === 'broadcast') ? (
        <dl className="live-facts">
          <div data-testid="live-obs-state"><dt>OBS 推流</dt><dd>{obs}{run.stage_seen_at ? <small>{clock(run.stage_seen_at)}</small> : null}</dd></div>
          <div data-testid="live-platform-state"><dt>{roomLabel}</dt><dd>{platform}{run.platform_seen_at ? <small>{clock(run.platform_seen_at)}</small> : null}</dd></div>
        </dl>
      ) : null}
      {stopping && run.role === 'host' && run.mode === 'broadcast' ? <div className="live-actions live-receipts">
        <span>{run.stop_output_confirmed ? <><CheckIcon />已停推流</> : '等待停推回执'}</span>
        <span>{run.stop_platform_confirmed ? <><CheckIcon />已确认下播</> : '等待下播回执'}</span>
      </div> : null}
      {run.detail && run.detail !== status.label ? <p className="live-note">{run.detail}</p> : null}
      {!settled && (stopping || status.tone === 'attention') ? <div className="live-actions">
        <button type="button" className="button button--small" onClick={refresh}><RefreshIcon />重新核实</button>
        {connector?.feature === 'bilibili' && run.role === 'host' && (stopping || run.obs_active === true) ? (
          <a className="button button--small" href={BILIBILI_LIVE_CENTER} target="_blank" rel="noreferrer">
            {stopping ? '去 B 站直播中心' : '去 B 站开播'}
          </a>
        ) : null}
      </div> : null}
    </section>
  );
}

function ShowRecords({ run, onGo }: { run: LiveSession; onGo: LivePageProps['onGo'] }) {
  const [saved, setSaved] = useState<Array<LiveViewer & { note: string }>>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let disposed = false;
    api.liveViewers(run.connector_id).then((data) => { if (!disposed) setSaved(data.viewers); }).catch((cause) => {
      if (!disposed) setError(explainError(cause).reason);
    });
    return () => { disposed = true; };
  }, [run.connector_id]);

  async function remember(viewer: LiveViewer, event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const note = String(new FormData(event.currentTarget).get('note') ?? '').trim();
    setBusy(true);
    setError('');
    try {
      const item = await api.rememberLiveViewer(run.id, viewer.account_ref, note);
      setSaved((previous) => [...previous, { ...item, note }]);
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  }

  async function forget(accountRef: string) {
    setBusy(true);
    setError('');
    try {
      await api.forgetLiveViewer(run.connector_id, accountRef);
      setSaved((previous) => previous.filter((item) => item.account_ref !== accountRef));
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  }

  return <section className="live-section live-records" aria-label="本场记录">
    <h2>本场记录</h2>
    <p className="live-note">
      {run.confirmed_live_at && run.ended_at ? '已播 ' + duration(run.confirmed_live_at, run.ended_at) + ' · ' : ''}
      播出 {run.played_count} 段 · 收到 {run.counts.comment ?? 0} 条弹幕
    </p>
    {error ? <p className="live-error" role="alert">{error}</p> : null}
    {run.viewers.length || saved.length ? <h3>观众</h3> : null}
    {run.viewers.filter((viewer) => !saved.some((item) => item.account_ref === viewer.account_ref)).map((viewer) => (
      <form className="live-viewer" key={viewer.account_ref} onSubmit={(event) => void remember(viewer, event)}>
        <span><strong>{viewer.display_name || '观众'}</strong> <code title={viewer.account_ref}>…{viewer.account_ref.slice(-10)}</code></span>
        <input className="field__input" name="note" placeholder="房间公开备注" aria-label="房间公开备注" maxLength={300} />
        <button className="button button--small" disabled={busy}>记住</button>
      </form>
    ))}
    {saved.map((viewer) => <div className="live-viewer" key={viewer.account_ref}>
      <span><strong>{viewer.display_name || '观众'}</strong> <code title={viewer.account_ref}>…{viewer.account_ref.slice(-10)}</code></span>
      <span>{viewer.note}</span>
      <button type="button" className="button button--small" disabled={busy}
        onClick={() => void forget(viewer.account_ref)}>清除房间备注</button>
    </div>)}
    {saved.length ? <button type="button" className="ghost-button" onClick={() => onGo('/relationships')}>管理账号关联</button> : null}
    {run.replies.length ? <details className="live-replies">
      <summary>最近播出的字幕</summary>
      <ol>{run.replies.map((reply, index) => <li key={index}>{reply}</li>)}</ol>
    </details> : null}
  </section>;
}

export function LivePage({ current, statusError, refresh, onGo, onStop }: LivePageProps) {
  const [connectors, setConnectors] = useState<LiveConnector[] | null>(null);
  const [selected, setSelected] = useState('');
  const [revision, setRevision] = useState(0);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [busy, setBusy] = useState(false);
  const run = current?.session ?? null;
  const active = !!run && run.state !== 'ended';
  const connectorId = active ? run.connector_id
    : selected || current?.planned_connector_id || run?.connector_id
      || connectors?.find((item) => item.enabled)?.id || connectors?.[0]?.id;
  const connector = connectors?.find((item) => item.id === connectorId);
  const show = run?.connector_id === connectorId ? run : null;
  const connected = !!connector?.stage_observation && !loadError
    && Date.now() - Date.parse(connector.stage_observation.seen_at) <= 10_000;
  const room = connector?.feature === 'bilibili' ? liveRoomUrl(connector.room_id) : null;
  const roleName = show?.role === 'companion' ? '陪伴助手' : '主播';

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const data = await api.customConnectors(controller.signal);
        if (controller.signal.aborted) return;
        setConnectors(data.connectors);
        setLoadError('');
      } catch (cause) {
        if (!controller.signal.aborted) setLoadError(explainError(cause).reason);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(load, 4_000);
      }
    }
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [revision]);

  function refreshAll() {
    setRevision((value) => value + 1);
    refresh();
  }

  async function perform(action: () => Promise<unknown>, notice = '') {
    setBusy(true);
    setActionError('');
    setFeedback('');
    try {
      await action();
      setFeedback(notice);
    } catch (cause) {
      setActionError(explainError(cause).reason);
    } finally {
      setBusy(false);
      refreshAll();
    }
  }

  function cue(event: FormEvent<HTMLFormElement>, sessionId: string) {
    event.preventDefault();
    const form = event.currentTarget;
    const text = String(new FormData(form).get('cue') ?? '').trim();
    void perform(async () => {
      await api.cueLiveSession(sessionId, text);
      form.reset();
    }, '已递话');
  }

  return (
    <article className="live-page" data-testid="live-page">
      <header className="live-page__head">
        <div>
          <h1>直播</h1>
          {active && show ? <p>{connector?.display_name ?? show.room_id} · {roleName}</p> : null}
        </div>
        <div className="live-actions">
          {room ? <a className="button button--small" href={room} target="_blank" rel="noreferrer">查看房间</a> : null}
          <button type="button" className="button button--small" onClick={() => onGo('/settings/connectors')}>接入设置</button>
        </div>
      </header>
      {statusError || loadError || actionError ? <p className="live-error" role="alert">{actionError || statusError || loadError}</p> : null}
      {feedback ? <p className="live-note" role="status">{feedback}</p> : null}
      {active && show ? <>
        {show.topic ? <h2 className="live-topic">{show.topic}</h2> : null}
        {show.host_name ? <p className="live-note">主播 {show.host_name}</p> : null}
        <RunStatus run={show} connector={connector} reachable={!statusError} refresh={refreshAll} />
        <div className="live-actions live-active-actions">
          {show.mode === 'preview' && show.state === 'preview' && show.role === 'host'
            && connector?.obs_control && !connector.live_plan.autonomous_window && show.stage_ready
            && show.obs_active === false && show.platform_live !== true && !statusError
            ? <button type="button" className="button button--small button--primary" data-testid="live-start-after-preview"
              disabled={busy} onClick={() => {
                if (window.confirm('启动「' + connector.display_name + '」的 OBS 推流？')) {
                  void perform(() => api.startLiveSession(connector.id, 'broadcast'));
                }
              }}>开始直播</button> : null}
          {show.state !== 'stopping' ? <button type="button" className="button button--danger button--small"
            data-testid="live-stop" onClick={onStop}><StopIcon />{stopLabel(show)}</button> : null}
        </div>
        {(show.state === 'preview' || show.state === 'live') && show.stage_ready && !statusError
          && !show.obs_reconnecting && (show.state === 'preview'
            ? show.obs_active === false && show.platform_live !== true
            : show.obs_active === true && show.platform_live === true) ? (
          <form className="live-cue" onSubmit={(event) => cue(event, show.id)}>
            <label>{show.state === 'preview' ? '试音话题' : '递话'}<input className="field__input"
              required name="cue" maxLength={400} /></label>
            <button className="button button--small" disabled={busy}>{show.state === 'preview' ? '试听' : '请她接话'}</button>
          </form>
        ) : null}
        {show.mode === 'preview' && show.stage_ready ? <p className="live-note">画面、试音和字幕在直播电脑的 OBS 中预览。</p> : null}
      </> : <>
        <section className="live-section" aria-label="本场计划">
          <h2>本场计划</h2>
          {connectors?.length ? (
            <label className="live-room-select">直播间<select className="field__input" value={connector?.id ?? ''}
              onChange={(event) => { setSelected(event.target.value); setActionError(''); setFeedback(''); }}>
              {connectors.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
            </select></label>
          ) : (
            <p className="live-note">{connectors ? '请先在接入设置添加直播间。' : loadError ? '无法读取直播间。' : '正在读取直播间…'}</p>
          )}
          {connector ? <>
            <dl className="live-facts">
              <div><dt>舞台端</dt><dd>{!connector.enabled ? '接入器已停用'
                : !connector.stage_token_set ? '待配置舞台令牌' : connected ? '已连接' : '等待连接'}</dd></div>
              {connected && connector.stage_observation?.scene ? <div><dt>OBS 场景</dt><dd>{connector.stage_observation.scene}</dd></div> : null}
              <div><dt>事件接入</dt><dd>{connector.last_received_at ? '最近收到于 ' + clock(connector.last_received_at) : '等待事件'}</dd></div>
            </dl>
            <PlanForm key={connector.id + JSON.stringify(connector.live_plan)} connector={connector}
              connected={connected && !statusError && current !== null} busy={busy}
              onSave={(plan, action) => void perform(async () => {
                await api.saveLivePlan(connector.id, plan);
                if (action !== 'save') await api.startLiveSession(connector.id, action);
              }, action === 'save' ? '计划已保存' : '')}
              onClear={() => void perform(() => api.clearLivePlan(connector.id), '计划已撤销')} />
          </> : null}
        </section>
        {show ? <RunStatus run={show} connector={connector} reachable={!statusError} refresh={refreshAll} /> : null}
      </>}
      {show && (show.state === 'ended' || show.played_count > 0) ? <ShowRecords key={show.id} run={show} onGo={onGo} /> : null}
    </article>
  );
}
