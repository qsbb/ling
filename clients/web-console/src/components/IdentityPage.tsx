/**
 * 关系：谁是谁，以及「这几个号是不是同一个人」。
 *
 * 它是**一级导航页**（`/relationships`），不再是设置子页。用户原话是
 * 「设置里面的多用户与账号移到左边导航栏，叫『关系』」——判定标准与设置
 * 瘦身时那条一致：谁和谁是一伙的是内容，不是实例配置。因此这里没有
 * 「返回设置」，返回键回到聊天（与人设、生活两页同一套外壳）。
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
 *
 * ## 为什么会卡死，以及这一版怎么防复发
 *
 * `subjects` 是无界表：群里见到一个没见过的账号就建一行（`resolve_or_provision`），
 * 跑几个群就是几千行。原来的实现有两个独立的问题，叠在一起让「点开关系页就卡死、
 * 刷新也刷不动」：
 *
 * 1. **每张卡片都渲染一个含全部候选的 `<select>`** —— n 条关系就是 n² 个
 *    `<option>`（2000 条 = 4,023,210 个节点 / 289 MB，SSR 10.5 s，浏览器再把
 *    400 万个节点建成 DOM、布局，主线程占满）。刷新只是重放同一件事。
 *    现在**默认每张卡片都是只读摘要**，只有点开「合并到…」的那**一张**才渲染
 *    候选下拉，且同一时刻只允许一张展开（`mergeFor` 是单个 id，不是集合）。
 * 2. **接口不分页** —— 561 KB 一次吐完。现在服务端默认一页 50 条，
 *    这里用「加载更多」按 `offset` 续取；到底了就显示「没有更多了」。
 *
 * 搜索也放在**服务端**（`api.subjects({ q })`）：只在当前页里过滤会让"搜不到"
 * 变成假象——人可能在第 3 页。
 *
 * ## 候选从哪里来
 *
 * **只从当前已加载的页里取**，不再请求全量候选：目标要翻页就搜一下，
 * 那正是服务端 `q` 存在的理由。这样任何一次展开的 `<option>` 数都 ≤ 已加载条数。
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react';
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

/**
 * 每页条数。与后端默认值（50）一致：首屏就是一个"人眼能看完"的列表，
 * 而不是几千张卡片。
 */
export const SUBJECTS_PAGE_SIZE = 50;

/** 一个可以当合并目标的人（候选下拉里的一行）。 */
interface MergeCandidate {
  subject_id: string;
  display_name: string | null;
}

/**
 * 候选 = 当前**已加载**的、未被合并的其他 subject。
 *
 * 只在这一张卡片展开时算一次（旧实现在每张卡片上都 `filter` 一遍，是 n² 次比较）。
 * 排序按昵称，方便在几十条里找人；没有昵称的排在后面用自己的 id。
 */
export function mergeCandidates(
  subjects: SubjectRow[],
  selfId: string,
  query: string,
): MergeCandidate[] {
  const needle = query.trim().toLowerCase();
  const out: MergeCandidate[] = [];
  for (const row of subjects) {
    if (row.subject_id === selfId) continue;
    // 已被合并的人不能当目标：合并到"别人的小号"会让指针成环，拆开时说不清谁是谁。
    if (row.merged_into) continue;
    if (needle) {
      const hay = [row.display_name, row.subject_id, ...row.accounts.map((a) => a.external_account_id)]
        .join('\n')
        .toLowerCase();
      if (!hay.includes(needle)) continue;
    }
    out.push({ subject_id: row.subject_id, display_name: row.display_name });
  }
  out.sort((a, b) => {
    const left = a.display_name || a.subject_id;
    const right = b.display_name || b.subject_id;
    return left.localeCompare(right, 'zh-Hans-CN');
  });
  return out;
}

export function IdentityPage({ onBack }: IdentityPageProps) {
  const [subjects, setSubjects] = useState<SubjectRow[]>([]);
  const [loading, setLoading] = useState(true);
  /** 「加载更多」单独一个 loading：不能把整页打回"加载中…"（那会让列表跳走）。 */
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** 服务端报的总行数（筛选后）与"还有没有下一页"。 */
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  /** 筛选词。输入时即时重查（服务端 `q`），不是在本地过滤当前页。 */
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  /**
   * 正在展开"合并到谁"的那**一张**卡片。`null` = 全部只读。
   *
   * 一个 id 而不是 `Record<id, target>`：既是"同时只允许一张展开"的直接表达，
   * 也让"切换时关掉上一张"变成赋值而不是清理一堆键。
   */
  const [mergeFor, setMergeFor] = useState<string | null>(null);
  /** 展开中的那张卡片选中的目标。切换卡片时清掉，避免"带着上一个人的选择"提交。 */
  const [mergeTarget, setMergeTarget] = useState('');

  /**
   * 取一页并替换列表。`offset=0` 是首屏 / 刷新 / 换筛选词；
   * 其它 offset 由「加载更多」用 `loadMore` 追加。
   */
  const load = useCallback(async (query: string) => {
    setLoading(true);
    setError(null);
    setMergeFor(null);
    setMergeTarget('');
    try {
      const data = await api.subjects({ limit: SUBJECTS_PAGE_SIZE, offset: 0, q: query });
      setSubjects(data.subjects);
      setTotal(data.total);
      setHasMore(data.has_more);
      setAppliedSearch(data.q ?? '');
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load('');
  }, [load]);

  const loadMore = async () => {
    setLoadingMore(true);
    setError(null);
    try {
      const data = await api.subjects({
        limit: SUBJECTS_PAGE_SIZE,
        offset: subjects.length,
        q: appliedSearch,
      });
      // 去重：翻页期间有人在群里说话会插入新行（新行按创建时间排在最前），
      // 纯 offset 可能让同一个人出现在两页里。宁可少一条也不要 React 因重复 key 崩。
      setSubjects((prev) => {
        const seen = new Set(prev.map((row) => row.subject_id));
        return [...prev, ...data.subjects.filter((row) => !seen.has(row.subject_id))];
      });
      setTotal(data.total);
      setHasMore(data.has_more);
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoadingMore(false);
    }
  };

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    void load(search.trim());
  };

  const openMergeFor = (subjectId: string) => {
    setMergeFor((current) => (current === subjectId ? null : subjectId));
    setMergeTarget('');
    setMessage(null);
  };

  const merge = async (sourceId: string) => {
    const targetId = mergeTarget;
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
      await load(appliedSearch);
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
      await load(appliedSearch);
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
    const isEditing = mergeFor === subject.subject_id;
    // **只在这一张卡片展开时**才算候选：未展开的卡片一个字都不多算。
    const others = isEditing ? mergeCandidates(subjects, subject.subject_id, '') : [];
    return (
      <div
        key={subject.subject_id}
        className={isEditing ? 'account-card account-card--editing' : 'account-card'}
        data-testid="subject-card"
        data-editing={isEditing ? 'true' : 'false'}
      >
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
        ) : isEditing ? (
          <div className="account-card__actions">
            {others.length === 0 ? (
              // 一个候选都没加载到时不渲染 `<select>`：空下拉看起来像界面坏了，
              // 对读屏软件也只剩一个空控件。直说下一步该做什么。
              <span className="page__hint" data-testid="merge-empty">
                已加载的人里没有可选的目标——用上面的搜索找人，或先「加载更多」。
              </span>
            ) : (
              <select
                className="input input--small"
                value={mergeTarget}
                onChange={(event) => setMergeTarget(event.target.value)}
                aria-label="合并到谁"
                data-testid="merge-target"
              >
                <option value="">认成另一个人…</option>
                {others.map((other) => (
                  <option key={other.subject_id} value={other.subject_id}>
                    {other.display_name || other.subject_id}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              className="button button--small"
              disabled={busy || !mergeTarget}
              onClick={() => void merge(subject.subject_id)}
              data-testid="merge-button"
            >
              合并
            </button>
            <button
              type="button"
              className="button button--small"
              disabled={busy}
              onClick={() => setMergeFor(null)}
              data-testid="merge-cancel"
            >
              取消
            </button>
          </div>
        ) : (
          <div className="account-card__actions">
            <button
              type="button"
              className="button button--small"
              disabled={busy || candidates.length <= 1}
              onClick={() => openMergeFor(subject.subject_id)}
              data-testid="merge-open"
            >
              合并到…
            </button>
          </div>
        )}
      </div>
    );
  };

  const loadedCount = subjects.length;
  const noneAtAll = !loading && loadedCount === 0;
  const nothingMatched = noneAtAll && appliedSearch !== '';

  return (
    <section className="page">
      {/* 一级页：返回键回聊天，不是回设置（它已经不在设置里了）。 */}
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回聊天
      </button>

      <h1 className="page__title">关系</h1>
      <p className="page__hint">
        同一个人在多个平台、或者一个平台上有多个号时，可以把它们认成同一个人。
        <strong>合并只写一个指针，不搬任何数据</strong>——合错了点「拆开」就还原，
        拆开时还会把合并期间写在别人名下的记忆送回原主。
      </p>
      <p className="page__hint">
        判定的依据永远是<strong>账号</strong>，不是昵称：两个都叫「小明」的人不会被当成一个人。
      </p>

      {/* 搜索在**服务端**做（`q`）：只在当前页里过滤会把"人在第 3 页"说成"没这个人"。 */}
      <form className="identity-search" onSubmit={submitSearch} role="search">
        <input
          className="input input--small"
          type="search"
          value={search}
          placeholder="搜昵称、ID 或账号"
          aria-label="搜昵称、ID 或账号"
          onChange={(event) => setSearch(event.target.value)}
          data-testid="subjects-search"
        />
        <button type="submit" className="button button--small" disabled={loading}>
          搜索
        </button>
        {appliedSearch ? (
          <button
            type="button"
            className="button button--small"
            onClick={() => {
              setSearch('');
              void load('');
            }}
            data-testid="search-clear"
          >
            清除
          </button>
        ) : null}
      </form>

      {loading ? <p className="page__hint">加载中…</p> : null}

      {error ? (
        <div className="banner banner--error">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void load(appliedSearch)}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      {message ? (
        <div className="banner banner--ok" data-testid="identity-message">
          {message}
        </div>
      ) : null}

      {noneAtAll ? (
        <p className="page__hint" data-testid="subjects-empty">
          {nothingMatched ? `没有匹配「${appliedSearch}」的人。` : '还没有认识任何人。'}
        </p>
      ) : null}

      {merged.length > 0 ? (
        <>
          <h2 className="page__subtitle">已合并</h2>
          {merged.map(renderSubject)}
        </>
      ) : null}

      {loadedCount > 0 ? (
        <>
          <h2 className="page__subtitle">独立的人</h2>
          {/* 提示"这是第几条到第几条 / 一共多少"：分页之后不说清楚，用户会以为
              "只剩这几个人了"。 */}
          <p className="page__hint" data-testid="subjects-count">
            已显示 {loadedCount} 人{total > 0 ? `，共 ${total} 人` : ''}
          </p>
          {candidates.map(renderSubject)}
        </>
      ) : null}

      {loadedCount > 0 ? (
        <div className="identity-more">
          {hasMore ? (
            <button
              type="button"
              className="button button--small"
              disabled={loadingMore || busy}
              onClick={() => void loadMore()}
              data-testid="load-more"
            >
              {loadingMore ? '加载中…' : '加载更多'}
            </button>
          ) : (
            <span className="page__hint" data-testid="no-more">
              没有更多了
            </span>
          )}
        </div>
      ) : null}
    </section>
  );
}
