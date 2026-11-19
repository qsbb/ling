import { useCallback, useState } from 'react';

type ConfiguredToolStatus = {
  tools_enabled: boolean;
  search_configured: boolean;
  profiles: Record<string, string>;
  manifest_valid: boolean;
  plugins: Array<{ id: string; name: string; enabled: boolean }>;
};

const PROFILES: Record<string, string> = {
  'dialogue.fast': '快速对话', 'dialogue.deep': '深入对话', 'dialogue.group': '群聊对话',
};
const STATES: Record<string, string> = {
  ready: '已声明支持', unbound: '未绑定模型', unsupported_protocol: '此协议未接入工具',
  instance_not_enabled: '未启用原生工具',
};

export function ToolStatus() {
  const [status, setStatus] = useState<ConfiguredToolStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const response = await fetch('/v1/settings/tool-status', { credentials: 'same-origin' });
      if (!response.ok) throw new Error('tool status unavailable');
      setStatus(await response.json() as ConfiguredToolStatus);
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <details onToggle={(event) => { if (event.currentTarget.open) void load(); }}>
      <summary>工具状态与自定义插件</summary>
      <div className="page__hint">
        {loading ? <p role="status">正在读取生效配置…</p> : null}
        {failed ? <p role="status">暂时读不到工具状态。</p> : null}
        {!loading && !failed && status ? (
          <>
            <p>已发布的对话工具开关：{status.tools_enabled ? '开启' : '关闭'}</p>
            <ul>
              {Object.entries(status.profiles).map(([profile, state]) => (
                <li key={profile}>{PROFILES[profile] ?? profile}：{STATES[state] ?? '未知'}</li>
              ))}
            </ul>
            <p>公开网页搜索：{status.search_configured ? '已配置密钥' : '未配置 Brave Search 密钥'}</p>
            {status.manifest_valid ? (
              status.plugins.length ? (
                <ul>
                  {status.plugins.map((plugin) => (
                    <li key={plugin.id}>
                      {plugin.name}：{plugin.enabled ? <code>/plugin {plugin.id} 输入</code> : '已停用'}
                    </li>
                  ))}
                </ul>
              ) : <p>还没有安装自定义插件（由部署管理员配置）。</p>
            ) : <p role="status">插件清单格式错误或无法读取。</p>}
          </>
        ) : null}
        <button type="button" className="button button--small" disabled={loading} onClick={() => void load()}>
          刷新工具状态
        </button>
      </div>
    </details>
  );
}
