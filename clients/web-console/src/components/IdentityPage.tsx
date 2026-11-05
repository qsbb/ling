/**
 * 多用户与账号：谁是谁，以及「这几个号是不是同一个人」。
 *
 * ## 这一页最重要的性质：合并不搬数据
 *
 * 合并只写 `subjects.merged_into` 一个字段。账号和记忆**都留在原处**，
 * 解析时跟随指针、召回时展开集合。所以：
 *
 * - 合并错了 → 点一下拆开就还原
 * - 拆开时还会按 `source_account` 把合并期间写在别人名下的记忆送回原主
 *
 * 界面因此把「合并」和「拆开」做成同样轻的动作，而不是"一次不可逆的资料迁移"。
 *
 * ## 名字只是标签
 *
 * 页面上显示昵称，但**合并/拆开的依据是账号**。两个都叫「小明」的人不会被
 * 合并——那正是要避免的误判。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { SubjectRow } from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';

interface IdentityPageProps {
  onBack: () => void;
}

const TRUST_LABELS: Record<string, string> = {
  owner: '主人',
  known: '认识（未验证身份）',
  stranger: '第一次见',
};

export function IdentityPage({ onBack }: IdentityPageProps) {
  const [subjects, setSubjects] = useState<SubjectRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** 选中要合并到谁名下：键是被合并的 subject_id。 */
  const [targets, setTargets] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.subjects();
      setSubjects(data.subjects);
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const merge = async (sourceId: string) => {
    const targetId = targets[sourceId];
    if (!targetId) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await api.mergeSubjects({ source_id: sourceId, target_id: targetId });
      setMessage(
        result.moved === 0
          ? '已合并。数据没有搬动，随时可以拆开。'
          : `已合并，但有 ${result.moved} 条数据被搬动了——这不该发生，请检查实现。`,
      );
      await load();
    } catch (err) {
      setError(explainError(err));
    } finally {
      setBusy(false);
    }
  };

  const unmerge = async (sourceId: string) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await api.unmergeSubject(sourceId);
      setMessage(
        result.restored_memories > 0
          ? `已拆开，并把合并期间写的 ${result.restored_memories} 条记忆归还原主。`
          : '已拆开。',
      );
      await load();
    } catch (err) {
      setError(explainError(err));
    } finally {
      setBusy(false);
    }
  };

  const candidates = subjects.filter((s) => !s.merged_into);
  const merged = subjects.filter((s) => s.merged_into);

  const renderSubject = (subject: SubjectRow) => {
    const isMerged = Boolean(subject.merged_into);
    const others = candidates.filter((s) => s.subject_id !== subject.subject_id);
    return (
      <div key={subject.subject_id} className="account-card" data-testid="subject-card">
        <div className="account-card__head">
          <strong>{subject.display_name || '（无昵称）'}</strong>
          <span className="badge">{TRUST_LABELS[subject.trust_level] ?? subject.trust_level}</span>
          {isMerged ? (
            <span className="badge badge--muted" data-testid="merged-badge">
              已合并
            </span>
          ) : null}
        </div>

        <div className="account-card__accounts">
          {subject.accounts.length === 0 ? (
            <span className="page__hint">没有绑定账号</span>
          ) : (
            subject.accounts.map((account) => (
              <div key={account.account_id} className="account-row">
                <code>
                  {account.connector_instance}:{account.external_account_id}
                </code>
                <span className="badge">{account.proof === 'proven' ? '已证明' : '待验证'}</span>
                {account.revoked ? <span className="badge badge--muted">已解绑</span> : null}
              </div>
            ))
          )}
        </div>

        {isMerged ? (
          <div className="account-card__actions">
            <span className="page__hint">这个 subject 已被当作另一个人的号。</span>
            <button
              type="button"
              className="button button--small"
              disabled={busy}
              onClick={() => void unmerge(subject.subject_id)}
              data-testid="unmerge-button"
            >
              拆开
            </button>
          </div>
        ) : others.length > 0 ? (
          <div className="account-card__actions">
            <select
              className="input input--small"
              value={targets[subject.subject_id] ?? ''}
              onChange={(event) =>
                setTargets((prev) => ({ ...prev, [subject.subject_id]: event.target.value }))
              }
              data-testid="merge-target"
            >
              <option value="">认成另一个人…</option>
              {others.map((other) => (
                <option key={other.subject_id} value={other.subject_id}>
                  {other.display_name || other.subject_id}
                  {other.accounts[0] ? `（${other.accounts[0].external_account_id}）` : ''}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="button button--small"
              disabled={busy || !targets[subject.subject_id]}
              onClick={() => void merge(subject.subject_id)}
              data-testid="merge-button"
            >
              合并
            </button>
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">多用户与账号</h1>
      <p className="page__hint">
        同一个人在多个平台、或者一个平台上有多个号时，可以把它们认成同一个人。
        <strong>合并只写一个指针，不搬任何数据</strong>——合错了点「拆开」就还原，
        拆开时还会把合并期间写在别人名下的记忆送回原主。
      </p>
      <p className="page__hint">
        判定的依据永远是<strong>账号</strong>，不是昵称：两个都叫「小明」的人不会被当成一个人。
      </p>

      {loading ? <p className="page__hint">加载中…</p> : null}

      {error ? (
        <div className="banner banner--error">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      {message ? (
        <div className="banner banner--ok" data-testid="identity-message">
          {message}
        </div>
      ) : null}

      {merged.length > 0 ? (
        <>
          <h2 className="page__subtitle">已合并</h2>
          {merged.map(renderSubject)}
        </>
      ) : null}

      <h2 className="page__subtitle">独立的人</h2>
      {candidates.map(renderSubject)}
    </section>
  );
}
