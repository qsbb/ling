import { useEffect, useState } from 'react';
import { api } from '../api';
import { explainError } from '../errors';

type BackupStatus = Awaited<ReturnType<typeof api.backupRestoreStatus>>;
type BackupInspection = Awaited<ReturnType<typeof api.backupRestoreInspect>>;

export interface BackupRestorePageProps {
  onBack: () => void;
}

export function BackupRestorePage({ onBack }: BackupRestorePageProps) {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [selected, setSelected] = useState('');
  const [inspection, setInspection] = useState<BackupInspection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void api.backupRestoreStatus(controller.signal).then((result) => {
      setStatus(result);
      setSelected(result.packages[0]?.name ?? '');
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(explainError(cause).reason);
    });
    return () => controller.abort();
  }, []);

  const inspect = async () => {
    setError(null);
    setInspection(null);
    setChecking(true);
    try {
      setInspection(await api.backupRestoreInspect(selected));
    } catch (cause) {
      setError(explainError(cause).reason);
    } finally {
      setChecking(false);
    }
  };

  const row = status?.packages.find((entry) => entry.name === selected);

  return (
    <section className="page" data-testid="backup-restore-page">
      <header className="page__head">
        <button type="button" className="ghost-button" onClick={onBack}>返回设置</button>
        <h1>整包备份</h1>
      </header>
      <div className="page__body">
        {status && !status.operations_available ? (
          <p role="status">当前可校验已有备份包，宿主创建与恢复尚未接通。</p>
        ) : null}
        {status?.packages.length ? (
          <>
            <label htmlFor="backup-package">备份包</label>
            <select
              id="backup-package"
              value={selected}
              disabled={checking}
              onChange={(event) => {
                setSelected(event.target.value);
                setInspection(null);
                setError(null);
              }}
            >
              {status.packages.map((entry) => (
                <option key={entry.name} value={entry.name}>{entry.name}</option>
              ))}
            </select>
            {row ? <p>{row.version ?? '版本未知'} · {row.migration ?? '迁移未知'}</p> : null}
            <button type="button" disabled={checking || !selected} onClick={() => { void inspect(); }}>
              {checking ? '正在校验' : '校验备份文件'}
            </button>
          </>
        ) : status ? <p>暂无备份包。</p> : !error ? <p role="status">正在加载备份包。</p> : null}
        {inspection ? (
          <div data-testid="backup-preflight-result" role="status">
            <p>{inspection.ok ? '文件完整性校验通过' : '文件完整性校验失败'}</p>
            {[...inspection.errors, ...inspection.warnings].map((message, index) => (
              <p key={`${index}-${message}`}>{message}</p>
            ))}
          </div>
        ) : null}
        {error ? <p role="alert">{error}</p> : null}
      </div>
    </section>
  );
}
