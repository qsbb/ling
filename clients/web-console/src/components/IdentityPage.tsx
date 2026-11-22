/**
 * 关系：谁是谁，以及「这几个号是不是同一个人」。
 *
 * 它是**一级导航页**（`/relationships`），不再是设置子页。用户原话是
 * 「设置里面的多用户与账号移到左边导航栏，叫『关系』」——判定标准与设置
 * 瘦身时那条一致：谁和谁是一伙的是内容，不是实例配置。
 *
 * ## 这一页要回答的问题
 *
 * 「按自然人找关系，清楚看到平台账号和操作目标。」三件事按重要性排：
 *
 * 1. **姓名**是列表的首要信息——用户是带着一个名字来的（「小明」），不是带着
 *    一个账号 id 来的。
 * 2. **平台标识与账号数**用于扫读：一眼看出"这个人在 QQ 和 OneBot 各有一个号"。
 * 3. **完整外部 ID、验证状态、关联历史**在详情里核对。列表上只留尾号做区分，
 *    因为核对要精确、扫读要短——两件事挤在同一层级时，两件都做不好。
 *
 * 上一版把 `napcat-2:30000000` 用 `<code>` 直接铺在卡片正文里，一个人两个号
 * 就是两行等宽长串：姓名被数字淹没，同名的人又只能靠比对数字区分。
 *
 * ## 合并不搬数据
 *
 * 合并只写 `subjects.merged_into` 一个字段。账号和记忆**都留在原处**，
 * 解析时跟随指针、召回时展开集合。所以合并错了 → 拆开就还原，拆开时还会按
 * `source_account` 把合并期间写在别人名下的记忆送回原主。
 *
 * 界面因此把「合并」做成**一次确认 + 随时可拆**，而不是"一次不可逆的资料迁移"。
 *
 * ## 名字只是标签
 *
 * 页面显示姓名，但**合并/拆开的依据是账号**。两个都叫「小明」的人不会被
 * 合并——那正是要避免的误判。所以合并前有一张确认面板把双方的平台账号摊开。
 *
 * ## 为什么会卡死，以及这一版怎么防复发
 *
 * `subjects` 是无界表：群里见到一个没见过的账号就建一行，跑几个群就是几千行。
 * 原来有两个独立问题叠在一起：**每张卡片渲染一个含全部候选的 `<select>`**
 * （n 条 = n² 个 `<option>`）和**接口不分页**。
 *
 * 现在：服务端一页 50 条、按 `offset` 续取；候选只在**打开确认面板时**才取，
 * 且走服务端 `q` 搜索——既不预取全量，也不受"当前已加载的一页"限制。
 */

import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { distinctHints, summarizePlatforms, summarizeSubject } from '../relationshipSummary';
import type { SubjectRow } from '../types';
import { AlertIcon, ArrowLeftIcon, PencilIcon, RefreshIcon } from './Icons';
import { CandidateSearch, MergeConfirmDialog, type MergeParty } from './MergeConfirmDialog';
import { SubjectDetail } from './SubjectDetail';
import '../identity.css';

interface IdentityPageProps {
  onBack: () => void;
}

const TRUST_LABELS: Record<string, string> = {
  owner: '主人',
  known: '认识',
  stranger: '第一次见',
};

/** 每页条数。与后端默认值（50）一致：首屏就是一个"人眼能看完"的列表。 */
export const SUBJECTS_PAGE_SIZE = 50;

/**
 * 候选 = 当前**已加载**的、未被合并的其他 subject。
 *
 * 只用于"打开确认面板时先给一批候选"，不承担跨分页找人——那是搜索的事。
 * 保留这个导出是因为它是纯函数、可单测，且旧调用方（测试）还在用它。
 */
export function mergeCandidates(
  subjects: SubjectRow[],
  selfId: string,
  query: string,
): Array<{ subject_id: string; display_name: string | null }> {
  const needle = query.trim().toLowerCase();
  const out: Array<{ subject_id: string; display_name: string | null }> = [];
  for (const row of subjects) {
    if (row.subject_id === selfId) continue;
    if (row.merged_into) continue;
    if (needle) {
      const hay = [row.display_name, row.subject_id, ...(row.accounts ?? []).map((a) => a.external_account_id)]
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

/**
 * 列表一行。
 *
 * 三种行：
 *
 * 1. **独立的人**（`merged_into === null`）：姓名 → 平台徽标 → 操作。
 *    若有人并入它，加一句「已并入 N 个号」（数量来自服务端，不依赖成员是否在本页）。
 * 2. **已并入的人**（`merged_into` 指向本页的人）：显示「已并入 <目标姓名>」，
 *    并给拆开。它**不**挤进目标那一行——成员记录可能在别的页，折进去会让同一份
 *    数据在"整页浏览"和"搜索命中"两种情况下显示成两样。
 * 3. 目标不在本页的并入记录：由 `orphans` 块统一处理，这里不重复。
 */
function PersonRow({
  subject,
  mergedMemberCount,
  mergedIntoName,
  sameNameRows,
  busy,
  onRename,
  onMerge,
  onUnmerge,
  onOpenDetail,
  detailOpen,
}: {
  subject: SubjectRow;
  /** 服务端给的"并入我名下的记录数"；已并入别人的行为 0。 */
  mergedMemberCount: number;
  /** 已并入的目标姓名（本页能解析到才有）；独立的人为 `null`。 */
  mergedIntoName: string | null;
  sameNameRows: readonly SubjectRow[];
  busy: boolean;
  onRename: (person: SubjectRow) => void;
  onMerge: (person: SubjectRow) => void;
  onUnmerge: (source: SubjectRow) => void;
  onOpenDetail: (subjectId: string) => void;
  detailOpen: boolean;
}) {
  const accounts = subject.accounts ?? [];
  const summary = summarizeSubject(subject);
  const platforms = summarizePlatforms(accounts);
  const hints = distinctHints(subject, sameNameRows);
  const isMerged = Boolean(subject.merged_into);

  return (
    <div
      className={[
        'person-row',
        isMerged ? 'person-row--merged' : '',
        detailOpen ? 'person-row--detail-open' : '',
      ].filter(Boolean).join(' ')}
      data-testid="subject-card"
      data-subject-id={subject.subject_id}
      data-has-name={summary.hasName ? 'true' : 'false'}
      data-merged={isMerged ? 'true' : 'false'}
    >
      <div className="person-row__main">
        <span className="person-avatar" aria-hidden="true">
          {Array.from(summary.displayName)[0]}
        </span>

        <div className="person-row__identity">
          <div className="person-row__name-line">
            <strong
              className={summary.hasName ? 'person-row__name' : 'person-row__name person-row__name--unset'}
              data-testid="person-name"
            >
              {summary.displayName}
            </strong>
            {isMerged ? (
              <span className="badge badge--muted" data-testid="merged-badge">
                {mergedIntoName ? `已并入${mergedIntoName}` : '已并入另一个人'}
              </span>
            ) : mergedMemberCount > 0 ? (
              <span className="badge badge--muted" data-testid="merged-members-badge">
                已并入 {mergedMemberCount} 个号
              </span>
            ) : null}
            {summary.accountCount > 0 && summary.hasUnverified ? (
              <span className="badge badge--warn" data-testid="unverified-badge">有未验证</span>
            ) : null}
            {subject.trust_level === 'owner' ? (
              <span className="badge">{TRUST_LABELS.owner}</span>
            ) : null}
          </div>

          <div className="person-row__meta">
            {summary.accountCount === 0 ? (
              <span className="person-row__platforms person-row__platforms--empty" data-testid="no-accounts">
                没有绑定账号
              </span>
            ) : (
              <span className="person-row__platforms" data-testid="person-platforms">
                {platforms.map((entry) => (
                  <span className="platform-chip" key={entry.platform}>
                    <span className="platform-chip__name">{entry.platform}</span>
                    <span className="platform-chip__count" aria-label={`${entry.accounts} 个账号`}>
                      {entry.accounts}
                    </span>
                  </span>
                ))}
              </span>
            )}
            {hints.length > 0 ? (
              <span className="person-row__hint" data-testid="same-name-hint">
                同名区分：{hints.join(' / ')}
              </span>
            ) : null}
          </div>
        </div>

        <div className="person-row__actions">
          {/*
           * 改名只对**保留的一方**开放：已合并的记录改名会被服务端拒绝（409），
           * 界面不该给一个必然会失败的按钮。所以这里不是"禁用"，而是不渲染。
           */}
          {subject.can_rename !== false ? (
            <button
              type="button"
              className="button button--small"
              disabled={busy}
              onClick={() => onRename(subject)}
              data-testid="rename-open"
            >
              <PencilIcon /> 改名
            </button>
          ) : null}
          {isMerged ? (
            <button
              type="button"
              className="button button--small"
              disabled={busy}
              onClick={() => onUnmerge(subject)}
              data-testid="unmerge-button"
            >
              拆开
            </button>
          ) : (
            <button
              type="button"
              className="button button--small"
              disabled={busy}
              onClick={() => onMerge(subject)}
              data-testid="merge-open"
            >
              合并…
            </button>
          )}
          <button
            type="button"
            className="button button--small"
            aria-expanded={detailOpen}
            aria-label={`${detailOpen ? '收起' : '查看'}${summary.displayName}的详情`}
            onClick={() => onOpenDetail(subject.subject_id)}
            data-testid="detail-open"
          >
            {detailOpen ? '收起' : '详情'}
          </button>
        </div>
      </div>

      {detailOpen ? <SubjectDetail key={subject.subject_id} subjectId={subject.subject_id} /> : null}
    </div>
  );
}

export function IdentityPage({ onBack }: IdentityPageProps) {
  const [subjects, setSubjects] = useState<SubjectRow[]>([]);
  const [loading, setLoading] = useState(true);
  /** 「加载更多」单独一个 loading：不能把整页打回"加载中…"（那会让列表跳走）。 */
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  /** 操作反馈：{语气, 文本}。保存/合并/拆开都要有**真实结果**，不是"操作成功"四个字。 */
  const [notice, setNotice] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  /** 正在改名的自然人；`null` = 没有在改名。 */
  const [rename, setRename] = useState<{ subjectId: string; name: string } | null>(null);
  const [renameBusy, setRenameBusy] = useState(false);
  /** 改名失败的原因：显示在弹层里，弹层不关。 */
  const [renameError, setRenameError] = useState<ExplainedError | null>(null);
  /** 正在确认合并的源。`null` = 没有合并面板。 */
  const [mergeSource, setMergeSource] = useState<MergeParty | null>(null);
  const [mergeTarget, setMergeTarget] = useState<MergeParty | null>(null);
  const [mergeError, setMergeError] = useState<ExplainedError | null>(null);
  const [detailFor, setDetailFor] = useState<string | null>(null);
  /**
   * 关闭弹层后要回到的那条记录。
   *
   * 弹层关闭时列表可能已经重排（合并会改变分组），所以记的是**主体 id**，
   * 不是滚动位置或行号——id 在重排后仍然指向同一个人。
   */
  const [returnTo, setReturnTo] = useState<string | null>(null);

  const load = useCallback(async (query: string) => {
    setLoading(true);
    setError(null);
    setMergeSource(null);
    setMergeTarget(null);
    setMergeError(null);
    setDetailFor(null);
    setRename(null);
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

  /**
   * 关闭弹层/详情后把视线带回原记录。
   *
   * 用 `data-subject-id` 定位而不是"第几行"：合并会把被并入的记录折进目标，
   * 行号会变，id 不会。找不到（比如改名后被搜索条件筛掉了）就什么都不做——
   * 硬滚到某一行比不滚更让人困惑。
   *
   * ## 焦点为什么给按钮，而不是给行
   *
   * 行是一个 `<div>`，`div.focus()` 是**空操作**——它没有 `tabindex`，也不在
   * tab 序列里。早先这里写的就是 `element.focus?.()`，看起来有"回到原记录"
   * 的意图，实测焦点仍留在弹层关闭前的那个按钮上（探针读到 `activeElement`
   * 是 `detail-open`，且 `data-subject-id` 为 null）。
   *
   * 两个选择：
   * - 给行加 `tabindex="-1"`：可聚焦，但对读屏用户多出一个语义为空的停靠点；
   * - 把焦点交给**该行自己的身份入口**（`detail-open` 按钮）：它就是"这一行
   *   是谁"的可聚焦代表，键盘用户按回车能立刻回到详情，语义不丢。
   *
   * 选后者。滚动仍落在行上（`block: 'nearest'`），所以"视线回到原记录"与
   * "键盘回到原记录"指的是同一个地方。
   */
  useEffect(() => {
    if (!returnTo) return;
    const selector = `[data-subject-id="${CSS.escape(returnTo)}"]`;
    const row = document.querySelector<HTMLElement>(selector);
    if (row) {
      row.scrollIntoView({ block: 'nearest' });
      const focusTarget = row.querySelector<HTMLElement>('[data-testid="detail-open"]');
      focusTarget?.focus();
    }
    setReturnTo(null);
  }, [returnTo, subjects]);

  const loadMore = async () => {
    setLoadingMore(true);
    setError(null);
    try {
      const data = await api.subjects({
        limit: SUBJECTS_PAGE_SIZE,
        offset: subjects.length,
        q: appliedSearch,
      });
      // 去重：翻页期间有人在群里说话会插入新行，纯 offset 可能让同一个人
      // 出现在两页里。宁可少一条也不要 React 因重复 key 崩。
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

  const saveName = async (event: FormEvent) => {
    event.preventDefault();
    if (!rename || !rename.name.trim() || renameBusy) return;
    setRenameBusy(true);
    setNotice(null);
    const subjectId = rename.subjectId;
    try {
      await api.renameSubject(subjectId, rename.name.trim());
      setNotice({ tone: 'ok', text: `姓名已保存为「${rename.name.trim()}」。` });
      setRenameError(null);
      setRename(null);
      await load(appliedSearch);
      setReturnTo(subjectId);
    } catch (err) {
      // 失败留在弹层里（`rename` 不清空）：关掉再报错，用户会以为已经存了。
      setRenameError(explainError(err));
    } finally {
      setRenameBusy(false);
    }
  };

  const merge = async () => {
    if (!mergeSource || !mergeTarget) return;
    setBusy(true);
    setMergeError(null);
    try {
      const result = await api.mergeSubjects({
        source_id: mergeSource.subjectId,
        target_id: mergeTarget.subjectId,
      });
      setNotice({
        tone: 'ok',
        text: result.moved === 0
          ? `已把「${mergeSource.displayName || '未命名'}」认成「${mergeTarget.displayName || '未命名'}」。数据没有搬动，随时可以拆开。`
          : `已合并，但有 ${result.moved} 条数据被搬动了——这不该发生，请检查实现。`,
      });
      const targetId = mergeTarget.subjectId;
      setMergeSource(null);
      setMergeTarget(null);
      await load(appliedSearch);
      setReturnTo(targetId);
    } catch (err) {
      // 失败**留在面板里**：关掉面板再报错，用户会以为已经合并了。
      setMergeError(explainError(err));
    } finally {
      setBusy(false);
    }
  };

  const unmerge = async (source: SubjectRow) => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.unmergeSubject(source.subject_id);
      setNotice({
        tone: 'ok',
        text: result.restored_memories > 0
          ? `已拆开「${(source.display_name ?? '').trim() || '未命名记录'}」，并把合并期间写的 ${result.restored_memories} 条记忆归还原主。`
          : `已拆开「${(source.display_name ?? '').trim() || '未命名记录'}」。`,
      });
      await load(appliedSearch);
      setReturnTo(source.subject_id);
    } catch (err) {
      setNotice({ tone: 'error', text: explainError(err).reason });
    } finally {
      setBusy(false);
    }
  };

  /*
   * 一页的 subject 分成三类：
   *
   * - `standalone`：独立的人（`merged_into === null`）；
   * - `mergedHere`：已并入本页某个人的记录（能在本页解析到目标姓名）；
   * - `orphans`：已并入本页之外的人（目标不在这一页，连名字都拿不到）。
   *
   * 第三类单独成块，是因为它**不能**被悄悄混进列表：用户需要看见它、也能拆开它。
   */
  const { standalone, mergedHere, orphans } = useMemo(() => {
    const byId = new Map(subjects.map((row) => [row.subject_id, row]));
    const standaloneRows: SubjectRow[] = [];
    const mergedRows: Array<{ row: SubjectRow; targetName: string }> = [];
    const orphanRows: SubjectRow[] = [];
    for (const row of subjects) {
      if (!row.merged_into) {
        standaloneRows.push(row);
        continue;
      }
      const target = byId.get(row.merged_into);
      if (target) {
        mergedRows.push({ row, targetName: (target.display_name ?? '').trim() || '未命名' });
      } else {
        orphanRows.push(row);
      }
    }
    return { standalone: standaloneRows, mergedHere: mergedRows, orphans: orphanRows };
  }, [subjects]);

  const noneAtAll = !loading && !error && subjects.length === 0;
  const nothingMatched = noneAtAll && appliedSearch !== '';

  const openMerge = (person: SubjectRow) => {
    setRename(null);
    setDetailFor(null);
    setMergeError(null);
    setMergeTarget(null);
    setMergeSource({
      subjectId: person.subject_id,
      displayName: (person.display_name ?? '').trim(),
      accounts: person.accounts ?? [],
      trustLevel: person.trust_level,
    });
  };

  return (
    <section className="page identity-page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回聊天
      </button>

      <header className="identity-heading">
        <div>
          <h1 className="page__title">关系</h1>
        </div>
        {total > 0 ? (
          <p className="identity-heading__count" data-testid="subjects-count">
            {appliedSearch ? `匹配 ${total} 人` : `${total} 人`}
            {subjects.length < total ? ` · 已加载 ${subjects.length}` : ''}
          </p>
        ) : null}
      </header>

      {/* 搜索在**服务端**做（`q`）：只在当前页里过滤会把"人在第 3 页"说成"没这个人"。 */}
      <form className="identity-search" onSubmit={submitSearch} role="search">
        <input
          className="input input--small"
          type="search"
          value={search}
          placeholder="搜姓名、ID 或账号"
          aria-label="搜姓名、ID 或账号"
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

      {loading ? <p className="page__hint" data-testid="subjects-loading">加载中…</p> : null}

      {error ? (
        <div className="banner banner--error" role="alert">
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

      {notice ? (
        <div
          className={notice.tone === 'ok' ? 'banner banner--ok' : 'banner banner--error'}
          role={notice.tone === 'ok' ? 'status' : 'alert'}
          data-testid="identity-message"
        >
          {notice.text}
        </div>
      ) : null}

      {noneAtAll ? (
        <p className="page__hint" data-testid="subjects-empty">
          {nothingMatched
            ? `没有匹配「${appliedSearch}」的人。`
            : '还没有认识任何人。群里见到新账号时会自动建档。'}
        </p>
      ) : null}

      {standalone.length > 0 ? (
        <div className="identity-list" data-testid="person-list">
          {standalone.map((subject) => (
            <PersonRow
              key={subject.subject_id}
              subject={subject}
              mergedMemberCount={subject.merged_member_count ?? 0}
              mergedIntoName={null}
              sameNameRows={subjects}
              busy={busy || renameBusy}
              onRename={(person) => {
                setMergeSource(null);
                setDetailFor(null);
                setNotice(null);
                setRenameError(null);
                setRename({ subjectId: person.subject_id, name: person.display_name ?? '' });
              }}
              onMerge={openMerge}
              onUnmerge={(source) => void unmerge(source)}
              onOpenDetail={(subjectId) => {
                setMergeSource(null);
                setRename(null);
                if (detailFor === subjectId) {
                  setDetailFor(null);
                  setReturnTo(subjectId);
                } else {
                  setDetailFor(subjectId);
                }
              }}
              detailOpen={detailFor === subject.subject_id}
            />
          ))}
        </div>
      ) : null}

      {mergedHere.length > 0 ? (
        <section className="identity-merged" aria-label="已并入的身份">
          <h2 className="identity-section-title">已并入 <span>{mergedHere.length}</span></h2>
          <div className="identity-list">
            {mergedHere.map(({ row, targetName }) => (
              <PersonRow
                key={row.subject_id}
                subject={row}
                mergedMemberCount={0}
                mergedIntoName={targetName}
                sameNameRows={subjects}
                busy={busy || renameBusy}
                onRename={() => undefined}
                onMerge={() => undefined}
                onUnmerge={(source) => void unmerge(source)}
                onOpenDetail={(subjectId) => {
                  setMergeSource(null);
                  setRename(null);
                  setDetailFor(detailFor === subjectId ? null : subjectId);
                  if (detailFor === subjectId) setReturnTo(subjectId);
                }}
                detailOpen={detailFor === row.subject_id}
              />
            ))}
          </div>
        </section>
      ) : null}

      {/*
        * 「目标不在本次结果里」的记录。
        *
        * 早期文案写的是"找不到目标"——**那是假的**：目标存在，只是没落在这次
        * 结果集里（比如按这个人的账号搜索时，只命中了他自己）。把"不在这一页"
        * 说成"找不到"，会让人以为数据坏了，进而去点"拆开"做无谓的修复。
        *
        * 所以这里只说事实：并入了谁（id 照实给），以及为什么没在列表里合到
        * 那个目标下面。拆开仍然保留——那是独立于"找不找得到目标"的操作。
        */}
      {orphans.length > 0 ? (
        <section className="identity-orphans" aria-label="目标不在本次结果中的并入记录">
          <h2 className="identity-section-title">已并入 <span>{orphans.length}</span></h2>
          <p className="page__hint">
            目标这次没有出现在结果里。按目标姓名或账号搜索就能看到它，或在这里直接拆开。
          </p>
          <ul className="identity-orphans__list">
            {orphans.map((row) => {
              const summary = summarizeSubject(row);
              return (
                <li
                  key={row.subject_id}
                  className="identity-orphans__row"
                  data-testid="subject-card"
                  data-subject-id={row.subject_id}
                  data-merged="true"
                >
                  <span className="identity-orphans__name" data-testid="person-name">
                    {summary.displayName}
                  </span>
                  <span className="page__hint" data-testid="merged-badge">
                    并入 {row.merged_into}
                  </span>
                  <button
                    type="button"
                    className="button button--small"
                    disabled={busy}
                    onClick={() => void unmerge(row)}
                    data-testid="unmerge-button"
                  >
                    拆开
                  </button>
                  <button
                    type="button"
                    className="button button--small"
                    aria-label={`查看${summary.displayName}的详情`}
                    onClick={() => setDetailFor(detailFor === row.subject_id ? null : row.subject_id)}
                    data-testid="detail-open"
                  >
                    详情
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {subjects.length > 0 ? (
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
            <span className="page__hint" data-testid="no-more">没有更多了</span>
          )}
        </div>
      ) : null}

      {/* 改名：行内展开会挤掉姓名本身，所以放在弹层里——但关闭后回到原记录。 */}
      {rename ? (
        <dialog
          className="rename-dialog"
          open
          data-testid="rename-dialog"
          aria-labelledby="rename-dialog-title"
          onCancel={(event) => {
            event.preventDefault();
            if (renameBusy) return;
            setReturnTo(rename.subjectId);
            setRename(null);
          }}
        >
          <form onSubmit={(event) => void saveName(event)}>
            <h3 className="rename-dialog__title" id="rename-dialog-title">改姓名</h3>
            <label className="rename-dialog__label" htmlFor="rename-input">自然人姓名</label>
            <input
              id="rename-input"
              className="input"
              aria-label="自然人姓名"
              value={rename.name}
              maxLength={128}
              autoFocus
              disabled={renameBusy}
              onChange={(event) => setRename({ subjectId: rename.subjectId, name: event.target.value })}
              data-testid="rename-input"
            />
            <p className="page__hint">
              姓名只是标签；合并与拆开依据的是账号，不是名字。
            </p>
            {renameError ? (
              <div className="banner banner--error" role="alert" data-testid="rename-error">
                <AlertIcon />
                <div>
                  <strong>{renameError.reason}</strong>
                  {renameError.hint ? <div className="banner__detail">{renameError.hint}</div> : null}
                </div>
              </div>
            ) : null}
            <div className="rename-dialog__actions">
              <button
                type="button"
                className="button button--small"
                disabled={renameBusy}
                onClick={() => {
                  setReturnTo(rename.subjectId);
                  setRename(null);
                }}
              >
                取消
              </button>
              <button
                type="submit"
                className="button button--small"
                disabled={renameBusy || !rename.name.trim()}
                data-testid="rename-save"
              >
                {renameBusy ? '保存中…' : '保存'}
              </button>
            </div>
          </form>
        </dialog>
      ) : null}

      {mergeSource ? (
        <MergeConfirmDialog
          source={mergeSource}
          target={mergeTarget}
          busy={busy}
          error={mergeError}
          onCancel={() => {
            setReturnTo(mergeSource.subjectId);
            setMergeSource(null);
            setMergeTarget(null);
            setMergeError(null);
          }}
          onConfirm={() => void merge()}
        >
          <CandidateSearch
            sourceId={mergeSource.subjectId}
            selected={mergeTarget}
            onSelect={setMergeTarget}
            initialCandidates={standalone}
            disabled={busy}
          />
        </MergeConfirmDialog>
      ) : null}
    </section>
  );
}
