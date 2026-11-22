export interface LiveConnectorInput {
  display_name: string;
  room_id: string;
  enabled: boolean;
  source_ips: string;
  token?: string;
  signing_secret?: string;
  stage_token?: string;
  obs_control: boolean;
}

export type LiveRole = 'host' | 'companion';

export interface LivePlan {
  role: LiveRole;
  public_topic: string;
  host_name: string;
  companion_auto_reply: boolean;
  max_minutes: number;
  autonomous_window: { starts_at: string; ends_at: string } | null;
}

export interface LiveConnector extends Omit<LiveConnectorInput, 'token' | 'signing_secret' | 'stage_token'> {
  id: string;
  feature: 'bilibili' | null;
  event_path: string;
  account_prefix: string;
  token_set: boolean;
  signing_secret_set: boolean;
  stage_token_set: boolean;
  last_received_at: string | null;
  autonomous_state: 'deciding' | 'started' | 'wait' | 'unavailable' | null;
  live_plan: LivePlan;
  plan_pending: boolean;
  stage_observation: {
    scene: string | null;
    obs_active: boolean;
    obs_reconnecting: boolean;
    seen_at: string;
  } | null;
}

export interface LiveViewer {
  account_ref: string;
  display_name: string | null;
}

export interface LiveSession {
  id: string;
  connector_id: string;
  room_id: string;
  mode: 'preview' | 'broadcast';
  state: 'preview' | 'starting' | 'live' | 'stopping' | 'ended';
  role: LiveRole;
  topic: string;
  host_name: string;
  autonomous: boolean;
  deadline_at: string;
  confirmed_live_at: string | null;
  stopping_at: string | null;
  ended_at: string | null;
  obs_active: boolean | null;
  obs_reconnecting: boolean | null;
  platform_live: boolean | null;
  stage_ready: boolean;
  owns_output: boolean;
  stop_output_confirmed: boolean;
  stop_platform_confirmed: boolean;
  stage_seen_at: string | null;
  platform_seen_at: string | null;
  pending_comments: number;
  viewers: LiveViewer[];
  counts: Record<string, number>;
  played_count: number;
  replies: string[];
  detail: string | null;
}

export interface CurrentLive {
  session: LiveSession | null;
  planned_connector_id: string | null;
  navigation_visible: boolean;
}

export function liveStatus(run: LiveSession, reachable: boolean, now = Date.now()): {
  label: string; tone: 'live' | 'attention' | 'neutral';
} {
  if (run.state === 'ended') {
    return { label: run.mode === 'preview' ? '预览已结束' : run.role === 'companion' ? '陪伴已结束' : '本场已结束', tone: 'neutral' };
  }
  if (!reachable) return { label: '直播状态待核实', tone: 'attention' };
  if (run.state === 'stopping') {
    const timedOut = run.stopping_at && now - Date.parse(run.stopping_at) >= 15_000;
    const label = run.role === 'companion' && run.mode === 'broadcast' ? '正在退出陪伴' : '正在停止';
    return { label: timedOut ? '停止状态待核实' : label, tone: 'attention' };
  }
  if (run.obs_reconnecting) return { label: 'OBS 正在重连', tone: 'attention' };
  if (run.state === 'live' && run.obs_active === true && run.platform_live === true) {
    return { label: run.role === 'companion' ? '陪伴中' : '直播中', tone: 'live' };
  }
  if (run.obs_active === true && run.platform_live === false) return { label: '推流未开播', tone: 'attention' };
  if (run.obs_active === true && run.platform_live === null) return { label: '房间状态待核实', tone: 'attention' };
  if (run.obs_active === false && run.platform_live === true) return { label: '房间开着，OBS 未推流', tone: 'attention' };
  if (run.obs_active == null && run.stage_seen_at) return { label: '舞台状态待核实', tone: 'attention' };
  if (run.state === 'preview') {
    if (run.stage_ready && run.obs_active === false && run.platform_live === false) {
      return { label: '预览中 · 只有本机能看见', tone: 'neutral' };
    }
    return { label: run.stage_ready && run.obs_active === false ? 'OBS 本机预览' : '正在准备预览', tone: 'neutral' };
  }
  if (run.state === 'live') return { label: '直播状态待核实', tone: 'attention' };
  return { label: run.role === 'companion' ? '等待加入' : '准备开播', tone: 'neutral' };
}

export function stopLabel(run: LiveSession): string {
  if (run.mode === 'preview') return '结束预览';
  return run.role === 'companion' ? '退出陪伴' : '停止直播';
}

export function liveRoomUrl(roomId: string): string | null {
  return /^\d+$/.test(roomId) ? `https://live.bilibili.com/${roomId}` : null;
}

export const BILIBILI_LIVE_CENTER = 'https://link.bilibili.com/p/center/index#/my-room/start-live';
