export interface LiveConnectorInput {
  display_name: string;
  room_id: string;
  enabled: boolean;
  source_ips: string;
  token?: string;
  signing_secret?: string;
  stage_token?: string;
  public_topic: string;
  max_minutes: number;
  obs_control: boolean;
  autonomous_window: { starts_at: string; ends_at: string } | null;
}

export interface LiveConnector extends Omit<LiveConnectorInput, 'token' | 'signing_secret' | 'stage_token'> {
  id: string;
  feature: 'bilibili' | null;
  event_path: string;
  token_set: boolean;
  signing_secret_set: boolean;
  stage_token_set: boolean;
  last_received_at: string | null;
  autonomous_state: 'deciding' | 'started' | 'wait' | 'unavailable' | null;
}

export interface LiveViewer {
  account_ref: string;
  display_name: string | null;
}

export interface LiveSession {
  id: string;
  connector_id: string;
  mode: 'preview' | 'broadcast';
  state: 'preview' | 'starting' | 'live' | 'stopping' | 'ended';
  obs_active: boolean | null;
  obs_reconnecting: boolean | null;
  platform_live: boolean | null;
  pending_comments: number;
  viewers: LiveViewer[];
  detail: string | null;
}
