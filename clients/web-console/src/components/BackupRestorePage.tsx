import { useEffect, useRef, useState } from 'react';
import { api, backupDownloadPath, type BackupArchive, type BackupStatus } from '../api';
import { explainError } from '../errors';
import './backup-restore.css';

const jobLabels = {
  queued: '等待执行',
  running: '正在执行',
  succeeded: '完成',
  failed: '失败',
};

function archiveTime(archive: BackupArchive): string {
  return new Date(archive.created_at).toLocaleString('zh-CN', { hour12: false });
}

export function BackupRestorePage({ onBack }: { onBack: () => void }) {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [selected, setSelected] = useState<BackupArchive | null>(null);
  const [busy, setBusy] = useState(false);
  const [networkError, setNetworkError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    let next: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const state = await api.backupStatus(controller.signal);
        if (!controller.signal.aborted) {
          setStatus(state);
          setNetworkError(null);
        }
      } catch (cause) {
        if (!controller.signal.aborted) setNetworkError(explainError(cause).reason);
      } finally {
        if (!controller.signal.aborted) next = setTimeout(() => { void refresh(); }, 2000);
      }
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(next); };
  }, []);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await action();
      setStatus(await api.backupStatus());
    } catch (cause) {
      setActionError(explainError(cause).reason);
    } finally {
      setBusy(false);
    }
  };
  const last = status?.jobs[0];
  const working = status?.jobs.some((job) => job.status === 'queued' || job.status === 'running');
  const disabled = busy || working || !status?.available;

  return (
    <section className="page backup-page" data-testid="backup-restore-page">
      <header className="page__head">
        <button type="button" className="ghost-button" onClick={onBack}>返回设置</button>
        <h1>备份与恢复</h1>
      </header>
      <div className="page__body backup-page__body">
        <div className="backup-page__actions">
          <button type="button" className="button button--primary" disabled={disabled}
            onClick={() => { void run(() => api.queueBackup('create')); }}>
            创建完整备份
          </button>
          <button type="button" className="button" disabled={disabled}
            onClick={() => fileInput.current?.click()}>
            导入备份
          </button>
          <input ref={fileInput} hidden type="file" accept=".ling-backup"
            aria-label="选择备份文件"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) void run(async () => { setSelected(await api.importBackup(file)); });
            }} />
        </div>
        {status && !status.available && !working ? <p role="status">宿主备份程序未运行</p> : null}
        {last ? (
          <p role="status">{last.action === 'create' ? '备份' : '恢复'} · {jobLabels[last.status]}
            {last.error ? <> · {last.error}</> : null}</p>
        ) : null}
        {networkError ? <p role="status">{working ? '等待 Ling 重新连接' : networkError}</p> : null}
        {actionError ? <p role="alert">{actionError}</p> : null}
        {selected ? (
          <section className="backup-page__confirm" aria-label="确认恢复">
            <h2>恢复到 {selected.version}</h2>
            <p>{archiveTime(selected)} 的数据会替换当前数据。恢复期间 Ling 会短暂停机。</p>
            <div className="backup-page__actions">
              <button className="button button--primary" type="button" disabled={disabled}
                onClick={() => {
                  void run(async () => {
                    await api.queueBackup('restore', selected.name);
                    setSelected(null);
                  });
                }}>
                确认恢复
              </button>
              <button className="button" type="button" onClick={() => setSelected(null)}>取消</button>
            </div>
          </section>
        ) : null}
        <ul className="backup-page__archives" aria-label="备份文件">
          {status?.archives.map((archive) => (
            <li key={archive.name} className="backup-page__archive">
              <div>
                <strong>{archive.version} · {archiveTime(archive)}</strong>
                <small>{(archive.bytes / (1024 * 1024)).toFixed(1)} MB</small>
              </div>
              <div className="backup-page__actions">
                <a className="button" href={backupDownloadPath(archive.name)} download>下载</a>
                <button type="button" className="button" disabled={disabled}
                  onClick={() => setSelected(archive)}>恢复</button>
              </div>
            </li>
          ))}
        </ul>
        {status?.archives.length === 0 ? <p>暂无备份文件</p> : null}
      </div>
    </section>
  );
}
