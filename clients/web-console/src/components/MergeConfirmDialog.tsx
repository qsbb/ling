/**
 * 「这两个号是不是同一个人」——合并前的确认。
 *
 * ## 为什么必须有一张确认面板
 *
 * 合并只写一个指针，但**它的后果是"以后分不清谁是谁"**：A 的记忆会被算到 B
 * 头上，拆开时要靠 `source_account` 一条条归位。原实现是列表里一个 `<select>`
 * 加一个「合并」按钮，选完直接提交——用户看不到自己选中的人是谁、双方各挂着
 * 哪些号，只能凭下拉里一行昵称判断。而**同名正是最常见的场景**（群昵称撞车），
 * 那行昵称恰好不携带任何区分信息。
 *
 * 所以确认面板把三件事摊开：
 *
 * 1. **源**：将被并入别人的那个人（会变成「已合并」，不能再改名）。
 * 2. **目标**：保留下来的自然人（合并后由它承载这段关系，可以改名）。
 * 3. **双方的平台账号**：唯一的区分依据。
 *
 * ## 候选为什么走服务端
 *
 * 候选原来只从「当前已加载的一页」里取。要合并的人在第 3 页时，下拉里根本
 * 没有他——用户会得出「系统里没这个人」的结论。服务端 `GET /v1/accounts/subjects`
 * 的 `q` 本来就按昵称 / subject_id / 账号 id 筛，所以这里把它当搜索用：
 * **跨分页**，按需加载，不再预取全量。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { accountTail, platformLabel, summarizePlatforms } from '../relationshipSummary';
import type { AccountRow, SubjectRow } from '../types';
import { AlertIcon } from './Icons';

/** 候选搜索的每页条数。够看、不至于让弹层里的列表长到需要滚很久。 */
export const MERGE_CANDIDATE_LIMIT = 20;
/** 输入防抖：连续敲字只发最后一次请求。 */
const SEARCH_DEBOUNCE_MS = 250;

/** 弹层里要展示的「一方」：姓名 + 平台账号。 */
export interface MergeParty {
  subjectId: string;
  displayName: string;
  accounts: AccountRow[];
  /** 是否已绑定账号；没有账号的一方要说清，而不是显示一片空白。 */
  trustLevel?: string;
}

function partyOf(subject: SubjectRow): MergeParty {
  return {
    subjectId: subject.subject_id,
    displayName: (subject.display_name ?? '').trim(),
    accounts: subject.accounts ?? [],
    trustLevel: subject.trust_level,
  };
}

/**
 * 一方的账号清单。
 *
 * 完整外部 ID 在这里**照实显示**：确认面板正是核对的地方，"…0001" 这种列表上
 * 的缩写不足以让人确认"这就是那个号"。所以这里不做截断。
 */
function PartyAccounts({ party }: { party: MergeParty }) {
  const platforms = summarizePlatforms(party.accounts);
  if (party.accounts.length === 0) {
    return <p className="merge-dialog__empty">没有绑定账号</p>;
  }
  return (
    <ul className="merge-dialog__accounts">
      {party.accounts.map((account) => (
        <li key={account.account_id} className="merge-dialog__account">
          <span className="merge-dialog__platform">{platformLabel(account.connector_instance)}</span>
          <code className="merge-dialog__id">{account.external_account_id}</code>
          <span className={account.proof === 'proven' || account.proof === 'admin'
            ? 'badge badge--ok'
            : 'badge badge--muted'}
          >
            {account.proof === 'proven' || account.proof === 'admin' ? '已验证' : '未验证'}
          </span>
          {account.revoked ? <span className="badge badge--muted">已解绑</span> : null}
        </li>
      ))}
      {platforms.length > 1 ? (
        <li className="merge-dialog__platforms">
          {platforms.map((row) => `${row.platform} ${row.accounts}`).join(' · ')}
        </li>
      ) : null}
    </ul>
  );
}

interface MergeConfirmDialogProps {
  /** 将被合并掉的一方（源）。 */
  source: MergeParty;
  /** 合并后保留的一方（目标）。`null` = 还没选，此时不能提交。 */
  target: MergeParty | null;
  busy: boolean;
  error: ExplainedError | null;
  /** 候选选择区（由调用方提供，通常是 {@link CandidateSearch}）。 */
  children: ReactNode;
  onCancel: () => void;
  onConfirm: () => void;
}

export function MergeConfirmDialog({
  source,
  target,
  busy,
  error,
  children,
  onCancel,
  onConfirm,
}: MergeConfirmDialogProps) {
  return (
    <dialog
      className="merge-dialog"
      open
      data-testid="merge-confirm"
      aria-labelledby="merge-confirm-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <h3 className="merge-dialog__title" id="merge-confirm-title">
        {target
          ? `把「${source.displayName || '未命名'}」认成「${target.displayName || '未命名'}」？`
          : '确认这两个账号属于同一个人'}
      </h3>

      <div className="merge-dialog__parties">
        <section className="merge-dialog__party" data-testid="merge-confirm-source">
          <header>
            <span className="merge-dialog__role">并入的一方</span>
            <strong>{source.displayName || '未命名'}</strong>
          </header>
          <PartyAccounts party={source} />
        </section>
        <section className="merge-dialog__party merge-dialog__party--target" data-testid="merge-confirm-target">
          <header>
            <span className="merge-dialog__role">保留的一方</span>
            <strong>{target ? target.displayName || '未命名' : '还没选'}</strong>
          </header>
          {target ? <PartyAccounts party={target} /> : <p className="merge-dialog__empty">在下面选一个目标</p>}
        </section>
      </div>

      {children}

      <p className="merge-dialog__note">
        合并后，并入一方的账号与记忆仍留在原处，只是解析身份时跟随到保留的一方。随时可以拆开。
      </p>

      {error ? (
        <div className="banner banner--error" role="alert" data-testid="merge-confirm-error">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
        </div>
      ) : null}

      <div className="merge-dialog__actions">
        <button type="button" className="button button--small" onClick={onCancel} disabled={busy}>
          取消
        </button>
        <button
          type="button"
          className="button button--small"
          onClick={onConfirm}
          disabled={busy || !target}
          data-testid="merge-confirm-ok"
        >
          {busy ? '合并中…' : '确认合并'}
        </button>
      </div>
    </dialog>
  );
}

interface CandidateSearchProps {
  /** 当前要合并掉的人；不出现在候选里。 */
  sourceId: string;
  selected: MergeParty | null;
  onSelect: (party: MergeParty | null) => void;
  /** 首屏已加载的人，用来在搜索词为空时先给一批候选。 */
  initialCandidates: readonly SubjectRow[];
  disabled?: boolean;
}

/**
 * 候选搜索：**跨分页**，走服务端 `q`。
 *
 * 三个刻意的取舍：
 *
 * 1. 搜索词为空时用「已加载的人」当初始候选——用户常常就在当前页里找；
 *    这不需要额外请求，也不假装那是全量。
 * 2. 一旦输入搜索词，就换成服务端结果，并明确写出「共 N 人匹配」——
 *    否则用户会以为搜索只在已加载的人里做。
 * 3. 已被合并的人**不能当目标**：合并到"别人的小号"会让指针成环，拆开时
 *    说不清谁是谁。服务端不会拦这一步，所以这里过滤掉。
 */
export function CandidateSearch({
  sourceId,
  selected,
  onSelect,
  initialCandidates,
  disabled,
}: CandidateSearchProps) {
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [rows, setRows] = useState<SubjectRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [searched, setSearched] = useState(false);
  const requestId = useRef(0);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (!debounced) {
      setRows([]);
      setTotal(0);
      setSearched(false);
      setError(null);
      return;
    }
    const ticket = ++requestId.current;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void api.subjects({ limit: MERGE_CANDIDATE_LIMIT, offset: 0, q: debounced })
      .then((data) => {
        if (ticket !== requestId.current) return;
        setRows(data.subjects);
        setTotal(data.total);
        setSearched(true);
      })
      .catch((err: unknown) => {
        if (ticket !== requestId.current) return;
        setError(explainError(err));
      })
      .finally(() => {
        if (ticket !== requestId.current) return;
        setLoading(false);
      });
    return () => controller.abort();
  }, [debounced]);

  const candidates = useMemo(() => {
    const source = debounced ? rows : initialCandidates;
    return source.filter((row) => row.subject_id !== sourceId && !row.merged_into);
  }, [debounced, rows, initialCandidates, sourceId]);

  const pick = useCallback((row: SubjectRow) => {
    if (selected?.subjectId === row.subject_id) {
      onSelect(null);
      return;
    }
    onSelect(partyOf(row));
  }, [onSelect, selected]);

  return (
    <div className="merge-candidates" data-testid="merge-candidates">
      <label className="merge-candidates__label" htmlFor="merge-candidate-search">
        合并到谁
      </label>
      <input
        id="merge-candidate-search"
        className="input input--small"
        type="search"
        value={query}
        placeholder="搜姓名、ID 或账号（跨全部人）"
        disabled={disabled}
        onChange={(event) => setQuery(event.target.value)}
        data-testid="merge-candidate-search"
      />

      {loading ? <p className="page__hint" data-testid="merge-candidates-loading">正在搜索…</p> : null}

      {error ? (
        <div className="banner banner--error" role="alert">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
        </div>
      ) : null}

      {!loading && !error && searched ? (
        <p className="page__hint" data-testid="merge-candidates-count">
          {total === 0
            ? `没有匹配「${debounced}」的人。`
            : total > candidates.length
              ? `共 ${total} 人匹配，先显示 ${candidates.length} 个——再输入几个字缩小范围。`
              : `共 ${total} 人匹配。`}
        </p>
      ) : null}

      {!loading && !error && candidates.length === 0 && !searched ? (
        <p className="page__hint" data-testid="merge-candidates-empty">
          已加载的人里没有可选的目标——用上面的搜索找人（会搜全部人，不只是这一页）。
        </p>
      ) : null}

      {candidates.length > 0 ? (
        <ul className="merge-candidates__list" role="listbox" aria-label="可合并的目标">
          {candidates.map((row) => {
            const summary = summarizePlatforms(row.accounts ?? []);
            const isSelected = selected?.subjectId === row.subject_id;
            return (
              <li key={row.subject_id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  className={isSelected
                    ? 'merge-candidates__option merge-candidates__option--selected'
                    : 'merge-candidates__option'}
                  disabled={disabled}
                  onClick={() => pick(row)}
                  data-testid="merge-candidate"
                  /* 同名的人很多：候选项必须能被**按身份**定位，不能只靠名字。 */
                  data-subject-id={row.subject_id}
                >
                  <span className="merge-candidates__name">{(row.display_name ?? '').trim() || '未命名'}</span>
                  {/*
                    * 右侧只给"一共几个号"：平台与尾号已经在下面一行逐个写出，
                    * 再按平台统计一遍是同一信息的第二次陈述。
                    */}
                  <span className="merge-candidates__meta">
                    {summary.length === 0 ? '没有绑定账号' : `${(row.accounts ?? []).length} 个号`}
                  </span>
                  <span className="merge-candidates__ids">
                    {(row.accounts ?? [])
                      .map((account) => `${platformLabel(account.connector_instance)} ${accountTail(account.external_account_id)}`)
                      .join(' / ')}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
