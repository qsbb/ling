/**
 * 一个人的详情：账号（含完整外部 ID 与验证状态）、关系事件、可见记忆。
 *
 * ## 为什么完整 ID 只在这里出现
 *
 * 列表要回答「这个人是谁、挂了几个号」，详情要回答「具体是哪几个号」。
 * 前一个问题需要短（尾号 + 平台 + 数量），后一个问题需要精确（完整 ID +
 * 证明方式）。把两者压在同一行，是上一版姓名被数字淹没的原因。
 *
 * ## 三段各自独立加载
 *
 * 三段用不同的可见范围（账号按整组、事件只给本人、记忆按可见性），其中任何
 * 一段失败都不该拖垮另两段——所以每段自带加载/失败/重试，不共用一个 loading。
 */

import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { accountProofLabel } from '../relationshipDetail';
import type { SubjectDetailItems, SubjectDetailPage, SubjectDetailSection } from '../relationshipDetail';
import { platformLabel } from '../relationshipSummary';

function useDetailSection<K extends SubjectDetailSection>(subjectId: string, section: K) {
  const [page, setPage] = useState<SubjectDetailPage<SubjectDetailItems[K]> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const loadingMore = useRef(false);

  useEffect(() => {
    const active = new AbortController();
    controller.current = active;
    setLoading(true);
    setError(null);
    setPage(null);
    void api.subjectDetails(subjectId, section, null, active.signal)
      .then(setPage)
      .catch((err: unknown) => {
        if (!active.signal.aborted) setError(explainError(err));
      })
      .finally(() => {
        if (!active.signal.aborted) setLoading(false);
      });
    return () => active.abort();
  }, [subjectId, section, attempt]);

  const loadMore = async () => {
    if (!page?.next_cursor || loading || loadingMore.current) return;
    loadingMore.current = true;
    const signal = controller.current?.signal;
    setLoading(true);
    setError(null);
    try {
      const next = await api.subjectDetails(subjectId, section, page.next_cursor, signal);
      setPage((current) => (
        current && current.access === next.access
          ? { ...next, items: [...current.items, ...next.items] }
          : next
      ));
    } catch (err) {
      if (!signal?.aborted) setError(explainError(err));
    } finally {
      loadingMore.current = false;
      if (!signal?.aborted) setLoading(false);
    }
  };

  const retry = () => {
    if (page?.next_cursor) {
      void loadMore();
    } else {
      setAttempt((current) => current + 1);
    }
  };

  return { page, loading, error, loadMore, retry };
}

const INTERACTION_LABELS: Record<string, string> = {
  warmth: '温暖的互动',
  shared_experience: '共同经历',
  conflict: '一次分歧',
  repair: '修复关系',
  boundary: '表达边界',
  disclosure: '分享心事',
};

const ORIGIN_LABELS: Record<string, string> = {
  user_statement: '用户陈述',
  external_observation: '外部观察',
  verified_source: '已验证来源',
  tool_receipt: '工具回执',
  model_inference: '模型推断',
  simulated: '模拟',
};

function dateLabel(value: string | null): string {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false });
}

interface SectionStatusProps {
  label: string;
  loading: boolean;
  error: ExplainedError | null;
  hasMore: boolean;
  retry: () => void;
  loadMore: () => Promise<void>;
}

function SectionStatus({ label, loading, error, hasMore, retry, loadMore }: SectionStatusProps) {
  return (
    <>
      {error ? (
        <p className="identity-detail__error" role="alert">
          {error.reason} <button type="button" className="button button--small" onClick={retry}>重试</button>
        </p>
      ) : null}
      {loading ? <p className="page__hint">加载中…</p> : null}
      {hasMore && !error ? (
        <button
          type="button"
          className="button button--small"
          disabled={loading}
          onClick={() => void loadMore()}
          aria-label={`加载更多${label}`}
        >
          加载更多
        </button>
      ) : null}
    </>
  );
}

/**
 * 证明状态的**视觉分级**：已验证是绿、未验证是警告色、未知是灰。
 *
 * 三档必须可分——「待验证」和「状态未知」不是同一件事，前者是等一个人确认，
 * 后者是我们没有这个信息。用同一个灰色徽章表达两者，等于把「不知道」说成
 * 「还在等」。
 */
function proofBadge(proof: string): { className: string; label: string } {
  const label = accountProofLabel(proof);
  if (label === '已证明') return { className: 'badge badge--ok', label };
  if (label === '待验证') return { className: 'badge badge--warn', label };
  return { className: 'badge badge--muted', label };
}

export function SubjectDetail({ subjectId }: { subjectId: string }) {
  const accounts = useDetailSection(subjectId, 'accounts');
  const interactions = useDetailSection(subjectId, 'interactions');
  const memories = useDetailSection(subjectId, 'memories');

  const accountRows = accounts.page?.items ?? [];
  const unverified = accountRows.filter(
    (account) => proofBadge(account.proof).label !== '已证明',
  ).length;

  return (
    <div className="identity-detail" data-testid="subject-detail">
      <section className="identity-detail__section" aria-label="关联账号">
        <h3>
          关联账号
          {accountRows.length > 0 ? <span className="identity-detail__count">{accountRows.length}</span> : null}
        </h3>
        {accounts.page?.items.length === 0 ? <p className="page__hint">没有绑定账号</p> : null}
        {unverified > 0 && !accounts.loading ? (
          <p className="page__hint" data-testid="detail-unverified">
            {unverified} 个账号还没有验证：它们能记录关系，但不能用来确定"这句话是谁说的"。
          </p>
        ) : null}
        {accountRows.map((account) => {
          const badge = proofBadge(account.proof);
          return (
            <p className="identity-detail__row" key={account.account_id}>
              <span className="merge-dialog__platform">{platformLabel(account.connector_instance)}</span>
              <code>{account.external_account_id}</code>
              <span className={badge.className}>{badge.label}</span>
              {account.revoked ? <span className="badge badge--muted">已解绑</span> : null}
            </p>
          );
        })}
        <SectionStatus label="账号" {...accounts} hasMore={Boolean(accounts.page?.next_cursor)} />
      </section>

      <section className="identity-detail__section" aria-label="近期关系事件">
        <h3>近期关系事件</h3>
        {interactions.page?.access === 'full' ? <p className="page__hint">显示可追溯到本人未撤回私聊的关系事件。</p> : null}
        {interactions.page?.access === 'restricted' ? (
          <p className="page__hint">仅本人可查看关系事件。</p>
        ) : interactions.page?.items.length === 0 ? (
          <p className="page__hint">暂无可显示的关系事件。</p>
        ) : null}
        {interactions.page?.items.map((event) => (
          <div className="identity-detail__row" key={event.event_id}>
            <span>{INTERACTION_LABELS[event.kind] ?? '互动记录'}</span>
            <span className="page__hint">{ORIGIN_LABELS[event.origin] ?? '来源未知'} · {dateLabel(event.occurred_at)}</span>
          </div>
        ))}
        <SectionStatus label="关系事件" {...interactions} hasMore={Boolean(interactions.page?.next_cursor)} />
      </section>

      <section className="identity-detail__section" aria-label="可见记忆">
        <h3>可见记忆</h3>
        {memories.page?.access === 'public' ? <p className="page__hint">仅显示公开记忆。</p> : null}
        {memories.page?.access === 'purging' ? (
          <p className="page__hint">记忆正在清理。</p>
        ) : memories.page?.items.length === 0 ? (
          <p className="page__hint">暂无可见记忆。</p>
        ) : null}
        {memories.page?.items.map((memory) => (
          <div className="identity-detail__memory" key={memory.memory_id}>
            <p>{memory.text}</p>
            <span className="page__hint">
              {memory.visibility === 'public' ? '公开' : '私聊'} · {dateLabel(memory.created_at)}
              {memory.valid_until ? ` · 有效期至 ${dateLabel(memory.valid_until)}` : ''}
            </span>
          </div>
        ))}
        <SectionStatus label="记忆" {...memories} hasMore={Boolean(memories.page?.next_cursor)} />
      </section>
    </div>
  );
}
