/**
 * 整包备份与导入恢复：设置中心里的功能组件。
 *
 * 页面只展示后端真实状态和请求结果；它不接触密钥字节，也不执行宿主命令。
 * `api.ts` 的调用集中在这里，公共导航只保留设置中心里的一个入口。
 */
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import { explainError } from '../errors';

interface PackageRow {
  name: string;
  version?: string | null;
  commit?: string | null;
  migration?: string | null;
  artifact?: string | null;
}

interface StatusResponse {
  package_dir: string;
  packages: PackageRow[];
  requests: Array<Record<string, unknown>>;
  target_arch: string;
}

interface InspectResponse {
  ok: boolean;
  errors: string[];
  warnings: string[];
  manifest?: Record<string, unknown>;
  deploy_env?: string;
  required_bytes?: number;
}

type Message = { kind: 'ok' | 'error' | 'info'; text: string; detail?: string } | null;


function errorMessage(error: unknown): Message {
  const explanation = explainError(error);
  const detail = explanation.hint || explanation.detail || (error instanceof ApiError ? error.message : undefined);
  return { kind: 'error', text: explanation.reason, detail: detail ?? undefined };
}

export interface BackupRestorePageProps {
  onBack: () => void;
}

export function BackupRestorePage({ onBack }: BackupRestorePageProps) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [selected, setSelected] = useState('');
  const [inspect, setInspect] = useState<InspectResponse | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.backupRestoreStatus());
    } catch (error) {
      setMessage(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selected && status?.packages.length) setSelected(status.packages[0]!.name);
  }, [status, selected]);

  const loadInspect = useCallback(async (name: string) => {
    if (!name) return;
    setBusy(true);
    try {
      setInspect(await api.backupRestoreInspect(name));
      setMessage(null);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const createBackup = async () => {
    setBusy(true);
    try {
      const result = await api.backupRestoreCreate({ note: 'Web 管理入口创建' });
      setMessage({ kind: 'ok', text: `已提交备份请求 ${result.request_id}` });
      await refresh();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const preflight = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.backupRestorePreflight({ package: selected });
      await loadInspect(selected);
      setMessage({ kind: 'info', text: '预检请求已提交，结果见本页清单核对。' });
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!selected || !inspect?.ok) return;
    setBusy(true);
    try {
      const result = await api.backupRestoreConfirm({ package: selected, confirm: confirming });
      setMessage({ kind: 'ok', text: `已提交恢复请求 ${result.request_id}` });
      setConfirming(false);
      await refresh();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="page" data-testid="backup-restore-page">
      <header className="page__head">
        <button type="button" className="ghost-button" onClick={onBack}>返回设置</button>
        <h1>整包备份与恢复</h1>
      </header>

      <div className="page__body">
        <section data-testid="backup-create">
          <h2>创建备份</h2>
          <p className="page__hint">完整包包含数据库、媒体、配置、插件状态和可启动的版本产物。</p>
          <button type="button" disabled={busy} onClick={createBackup}>提交备份请求</button>
        </section>

        <section data-testid="backup-packages">
          <h2>备份成品</h2>
          {status?.packages.length ? (
            <ul>
              {status.packages.map((row) => (
                <li key={row.name}>
                  <button
                    type="button"
                    className={selected === row.name ? 'selected' : ''}
                    onClick={() => { setSelected(row.name); void loadInspect(row.name); }}
                  >
                    {row.name}
                  </button>
                  <span>{row.version ?? '版本未知'} · {row.migration ?? '迁移未知'}</span>
                </li>
              ))}
            </ul>
          ) : <p className="page__hint">还没有完整备份包。</p>}
        </section>

        <section data-testid="backup-inspect">
          <h2>导入预检与版本核对</h2>
          <p className="page__hint">目标架构：{status?.target_arch ?? '未知'}</p>
          <button type="button" disabled={!selected || busy} onClick={preflight}>提交预检</button>
          {inspect ? (
            <div>
              <p data-testid="backup-preflight-result">
                {inspect.ok ? '预检通过' : `预检未通过：${inspect.errors.join('；')}`}
              </p>
              {inspect.manifest ? (
                <dl>
                  <dt>应用版本</dt><dd>{String((inspect.manifest.version as Record<string, unknown> | undefined)?.app ?? '未知')}</dd>
                  <dt>迁移版本</dt><dd>{String((inspect.manifest.version as Record<string, unknown> | undefined)?.migration ?? '未知')}</dd>
                  <dt>凭据来源</dt><dd>{String((inspect.manifest.secrets as Record<string, unknown> | undefined)?.root_key_source ?? '未知')}</dd>
                </dl>
              ) : null}
              {inspect.deploy_env ? <pre data-testid="backup-env-preview">{inspect.deploy_env}</pre> : null}
            </div>
          ) : null}
        </section>

        <section data-testid="backup-confirm">
          <h2>确认恢复</h2>
          <label>
            <input
              type="checkbox"
              checked={confirming}
              onChange={(event) => setConfirming(event.target.checked)}
            />
            我已核对版本、数据覆盖范围和回退点
          </label>
          <button type="button" disabled={!inspect?.ok || !confirming || busy} onClick={confirm}>
            提交恢复请求
          </button>
        </section>

        <section data-testid="backup-results">
          <h2>请求与结果</h2>
          {status?.requests.length ? (
            <ol>
              {status.requests.map((row, index) => (
                <li key={String(row.id ?? index)}>{String(row.action ?? 'unknown')} · {String(row.status ?? row.id ?? '')}</li>
              ))}
            </ol>
          ) : <p className="page__hint">暂无请求。</p>}
          {message ? <p className={message.kind === 'error' ? 'error' : ''}>{message.text}{message.detail ? `：${message.detail}` : ''}</p> : null}
        </section>
      </div>
    </section>
  );
}
