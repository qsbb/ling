/**
 * 「导入旧记忆」：把 AstrBot Memory Companion 的迁移文件带进记忆库。
 *
 * 用户要完成的事只有一件：**确认这次会带来什么、记到谁名下，然后导完去得能看到**。
 * 所以页面的信息顺序是固定的，不按服务端字段顺序排：
 *
 *   1. 目标自然人 + QQ 私聊来源 + 可导入条数 —— 三个"会不会导错"的判断；
 *   2. 真实样本 —— 让人看见自己认得的内容，而不是一个总数；
 *   3. 重复与跳过原因 —— 次要，折叠在下面；零条时才升上来当主信息；
 *   4. 确认后的真实结果 + 「查看 AstrBot 记忆」—— 去得看，才叫导完。
 *
 * 三条不会妥协的实现约束：
 *
 * - **请求体是文件原始字节**。服务端按字节算摘要并签确认令牌，所以这里绝不
 *   把 File 读成文本再拼回去；`body: file` 就是原文。
 * - **结果以服务端为准**。确认后显示 `imported`，不是预览的 `accepted`——
 *   两者不同（确认时又有到期的、并发已写入的），拿 accepted 顶替就是说谎。
 * - **预览失效时保留已选文件**。用户不需要重新找文件：一步「重新预览」。
 *   文件错了才需要换文件，那是另一条路径。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  MemoryImportCommit,
  MemoryImportDestination,
  MemoryImportPreview,
  MemoryImportSample,
  MemoryImportSkipReason,
} from '../types';
import { AlertIcon, CheckIcon, CloseIcon } from './Icons';
import '../memory-import.css';

/* ==================================================================== 文案 */

/** 跳过原因的页面文案（docs/ASTRBOT-MEMORY-IMPORT.md 的对照表，逐字对齐）。 */
export const SKIP_REASON_TEXT: Record<MemoryImportSkipReason, string> = {
  invalid_record: '记录格式不完整',
  outside_private_pair: '不属于所选 QQ 私聊',
  unsupported_memory: '此类旧记忆暂不支持',
  inactive: '原记忆已失效',
  not_reviewed: '原记忆待审核',
  restricted: '原记忆受来源权限限制',
  not_yet_valid: '尚未到生效时间',
  expired: '已超过原有效期',
  retention_expired: '已超过 Ling 的保留期',
};

/** 固定顺序：从"这份文件本身有问题"到"时间与保留期"，读起来是一条因果链。 */
export const SKIP_REASON_ORDER: MemoryImportSkipReason[] = [
  'invalid_record',
  'outside_private_pair',
  'unsupported_memory',
  'inactive',
  'not_reviewed',
  'restricted',
  'not_yet_valid',
  'expired',
  'retention_expired',
];

/** 只列非零项，且保持固定顺序。 */
export function skipEntries(
  reasons: Partial<Record<MemoryImportSkipReason, number>> | undefined,
): Array<{ reason: MemoryImportSkipReason; label: string; count: number }> {
  if (!reasons) return [];
  return SKIP_REASON_ORDER.flatMap((reason) => {
    const count = reasons[reason] ?? 0;
    return count > 0 ? [{ reason, label: SKIP_REASON_TEXT[reason], count }] : [];
  });
}

/**
 * 零可导入时说"为什么会这样"。
 *
 * 只描述**最多的那个原因**：一条一条列出来是给"为什么少了几条"用的；
 * 这里一条都导不进来，用户要的是"我该去改什么"。返回 null 表示没有跳过
 * 原因（例如文件里本来就没有记录），由调用方给通用说法。
 */
export function zeroAcceptedHint(
  preview: Pick<MemoryImportPreview, 'skip_reasons' | 'total' | 'duplicates'>,
): { headline: string; next: string } {
  const entries = skipEntries(preview.skip_reasons).sort((a, b) => b.count - a.count);
  const dominant = entries[0];
  if (!dominant) {
    if (preview.duplicates > 0) {
      return {
        headline: '这些记忆都已经导入过了',
        next: '直接去记忆库看即可，不需要再导一次。',
      };
    }
    return {
      headline: '这份文件里没有可导入的记忆',
      next: '确认导出脚本用的是本人的 QQ 和旧 Bot 的 QQ，再重新导出一份迁移文件。',
    };
  }
  if (dominant.reason === 'outside_private_pair') {
    return {
      headline: '文件里的记录不属于你与这个旧 Bot 的私聊',
      next: '核对自然人设置里的本人 QQ 绑定，再用本人的 QQ 重新导出迁移文件。',
    };
  }
  if (dominant.reason === 'retention_expired' || dominant.reason === 'expired') {
    return {
      headline: '这些记忆都超过了保留期或原有有效期',
      next: '这类旧记忆按 Ling 的保留策略不再引入；如果只想留下其中几条，可以手动在核心记忆里重写。',
    };
  }
  if (dominant.reason === 'not_reviewed') {
    return {
      headline: '这些记忆在原插件里还没有通过审核',
      next: '回到 AstrBot 的 Memory Companion 里确认或审核这些记忆，再重新导出。',
    };
  }
  if (dominant.reason === 'restricted') {
    return {
      headline: '这些记忆在原插件里受来源权限限制',
      next: '受限制的记忆不会迁移；如需保留，请在记忆库手动补记。',
    };
  }
  return {
    headline: '这批记录都不满足导入条件',
    next: '重新导出迁移文件，或换一份文件再试。',
  };
}

/** 导入相关错误的用户可执行说法；认不出来的交回 explainError。 */
export interface ImportErrorCopy {
  reason: string;
  hint: string;
  /** 保留已选文件、一步重新预览。 */
  needsRepreview: boolean;
  /** 文件本身不可用，需要换文件。 */
  needsAnotherFile: boolean;
}

export function importErrorCopy(error: ExplainedError): ImportErrorCopy {
  switch (error.detail) {
    case 'preview_invalid':
      return {
        reason: '预览已失效',
        hint: '文件还在，重新预览一次就能确认。',
        needsRepreview: true,
        needsAnotherFile: false,
      };
    case 'qq_binding_required':
      return {
        reason: '还没有确认的本人 QQ 绑定',
        hint: '到「关系」里核对自然人，把本人 QQ 绑定到这个账号后再预览。',
        needsRepreview: true,
        needsAnotherFile: false,
      };
    case 'memory_purge_in_progress':
      return { reason: '记忆正在清理', hint: '清理结束后重新预览即可。', needsRepreview: true, needsAnotherFile: false };
    case 'destination_unavailable':
      return { reason: '目标记忆库暂不可用', hint: '稍后重新预览。', needsRepreview: true, needsAnotherFile: false };
    case 'file_too_large':
      return { reason: '文件超过 8 MiB', hint: '用较小的 --limit 分段导出，再逐份导入。', needsRepreview: false, needsAnotherFile: true };
    case 'too_many_records':
      return { reason: '文件超过 5000 条记录', hint: '用 --limit 与输出提示里的 --offset 分段导出。', needsRepreview: false, needsAnotherFile: true };
    case 'invalid_encoding':
      return { reason: '文件不是 UTF-8 编码', hint: '用导出脚本重新生成，不要手工另存。', needsRepreview: false, needsAnotherFile: true };
    case 'unsupported_archive':
      return { reason: '不是导出脚本生成的迁移文件', hint: '请选择转换脚本产出的 .jsonl。', needsRepreview: false, needsAnotherFile: true };
    case 'invalid_source_pair':
      return { reason: '旧 Bot 和你的 QQ 相同', hint: '导出时两个 QQ 号必须不同。', needsRepreview: false, needsAnotherFile: true };
    default:
      return { reason: error.reason, hint: error.hint, needsRepreview: false, needsAnotherFile: false };
  }
}

/* ==================================================================== 时间 */

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

/** 绝对时间；拿不到就返回 null，**不猜**。 */
export function sampleTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(
    date.getHours(),
  )}:${pad2(date.getMinutes())}`;
}

/* ==================================================================== 状态机 */

export type ImportStep =
  | { name: 'pick' }
  | { name: 'previewing'; file: File }
  | { name: 'preview'; file: File; preview: MemoryImportPreview }
  | { name: 'committing'; file: File; preview: MemoryImportPreview }
  | { name: 'done'; file: File; preview: MemoryImportPreview; result: MemoryImportCommit };

export interface MemoryImportPanelProps {
  /** 确认成功后的回调：让记忆库刷新，避免列表停在导入前的批次。 */
  onCommitted: () => void;
  /** 导入按钮点「查看 AstrBot 记忆」时，把记忆库切到该来源并刷新。 */
  onViewImported: () => void;
  /** 关闭面板（不改变已导入的数据）。 */
  onClose: () => void;
}

/**
 * 导入面板本体。
 *
 * 打开就显示"选文件"，因为**入口本身就是这一步**——先给一个空白页让人再点
 * 一次"开始"，只是多一层。选完立刻预览，中间不设"下一步"。
 */
export function MemoryImportPanel({ onCommitted, onViewImported, onClose }: MemoryImportPanelProps): React.ReactElement {
  const [step, setStep] = useState<ImportStep>({ name: 'pick' });
  const [error, setError] = useState<ExplainedError | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const lastFile = useRef<File | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const runPreview = useCallback(async (file: File) => {
    lastFile.current = file;
    setError(null);
    setStep({ name: 'previewing', file });
    try {
      const preview = await api.previewMemoryImport(file);
      if (!alive.current) return;
      setStep({ name: 'preview', file, preview });
    } catch (cause) {
      if (!alive.current) return;
      // 失败时**保留文件**：回 pick 但记住 lastFile，让「重新预览」一步可用。
      setStep({ name: 'pick' });
      setError(explainError(cause));
    }
  }, []);

  const runCommit = useCallback(async (file: File, preview: MemoryImportPreview) => {
    if (!preview.confirmation_token) return;
    setError(null);
    setStep({ name: 'committing', file, preview });
    try {
      const result = await api.commitMemoryImport(file, preview.confirmation_token);
      if (!alive.current) return;
      setStep({ name: 'done', file, preview, result });
      titleRef.current?.focus();
      onCommitted();
    } catch (cause) {
      if (!alive.current) return;
      setStep({ name: 'preview', file, preview });
      setError(explainError(cause));
    }
  }, [onCommitted]);

  const pickFile = (file: File | null | undefined) => {
    if (!file) return;
    void runPreview(file);
  };

  const reset = () => {
    lastFile.current = null;
    setError(null);
    setStep({ name: 'pick' });
    if (inputRef.current) inputRef.current.value = '';
  };

  const copy = error ? importErrorCopy(error) : null;
  const busy = step.name === 'previewing' || step.name === 'committing';

  return (
    <section className="import-panel" aria-labelledby="import-panel-title" data-testid="import-panel">
      <header className="import-panel__head">
        <h2 id="import-panel-title" ref={titleRef} tabIndex={-1}>
          导入旧记忆
        </h2>
        <button type="button" className="import-panel__close" aria-label="关闭导入" onClick={onClose}>
          <CloseIcon size={17} />
        </button>
      </header>

      {step.name === 'pick' || step.name === 'previewing' ? (
        <PickStage
          dragging={dragging}
          busy={step.name === 'previewing'}
          inputRef={inputRef}
          onDragState={setDragging}
          onPick={pickFile}
          error={error}
          copy={copy}
          retainedFile={lastFile.current}
          onRepreview={() => (lastFile.current ? void runPreview(lastFile.current) : reset())}
          onReset={reset}
        />
      ) : null}

      {step.name === 'preview' || step.name === 'committing' ? (
        <PreviewStage
          preview={step.preview}
          busy={step.name === 'committing'}
          error={error}
          copy={copy}
          onConfirm={() => void runCommit(step.file, step.preview)}
          onRepreview={() => void runPreview(step.file)}
          onChooseAnother={reset}
        />
      ) : null}

      {step.name === 'done' ? (
        <ResultStage
          result={step.result}
          preview={step.preview}
          onViewImported={onViewImported}
          onClose={onClose}
        />
      ) : null}

      {busy ? (
        <p className="import-panel__busy" role="status">
          <span className="spinner" aria-hidden="true" />
          {step.name === 'previewing' ? '正在读取文件…' : '正在写入记忆…'}
        </p>
      ) : null}
    </section>
  );
}

/* ==================================================================== 第一步：选文件 */

function PickStage({
  dragging,
  busy,
  inputRef,
  onDragState,
  onPick,
  error,
  copy,
  retainedFile,
  onRepreview,
  onReset,
}: {
  dragging: boolean;
  busy: boolean;
  inputRef: React.RefObject<HTMLInputElement | null>;
  onDragState: (value: boolean) => void;
  onPick: (file: File | null | undefined) => void;
  error: ExplainedError | null;
  copy: ImportErrorCopy | null;
  retainedFile: File | null;
  onRepreview: () => void;
  onReset: () => void;
}): React.ReactElement {
  return (
    <>
      <label
        className={`import-drop${dragging ? ' import-drop--over' : ''}`}
        data-testid="import-drop"
        onDragOver={(event) => {
          event.preventDefault();
          onDragState(true);
        }}
        onDragLeave={() => onDragState(false)}
        onDrop={(event) => {
          event.preventDefault();
          onDragState(false);
          onPick(event.dataTransfer.files?.[0]);
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".jsonl,application/x-ndjson,application/jsonl,text/plain"
          aria-label="选择迁移文件"
          data-testid="import-file-input"
          disabled={busy}
          onChange={(event) => onPick(event.target.files?.[0])}
        />
        <span className="import-drop__title">选择迁移文件</span>
        <span className="import-drop__meta">.jsonl，由导出脚本生成</span>
      </label>

      {error && copy ? (
        <div className="import-error" role="alert" data-testid="import-error" data-error-detail={error.detail ?? error.code}>
          <AlertIcon size={14} />
          <div>
            <strong>{copy.reason}</strong>
            {copy.hint ? <p>{copy.hint}</p> : null}
            {error.traceId ? <p className="import-error__trace">trace：{error.traceId}</p> : null}
          </div>
        </div>
      ) : null}

      {retainedFile ? (
        <div className="import-retained" data-testid="import-retained">
          <div>
            <strong>{retainedFile.name}</strong>
            <span>已选文件还在</span>
          </div>
          <div className="import-retained__actions">
            <button type="button" className="button button--small" onClick={onRepreview}>
              {copy?.needsRepreview ? '重新预览' : '再试一次'}
            </button>
            {copy?.needsAnotherFile || copy?.needsRepreview ? (
              <button type="button" className="ghost-button" onClick={onReset}>
                换个文件
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}

/* ==================================================================== 第二步：预览 */

function DestinationBlock({ destination }: { destination: MemoryImportDestination }): React.ReactElement {
  return (
    <dl className="import-facts" data-testid="import-destination">
      <div className="import-facts__wide">
        <dt>记到谁名下</dt>
        <dd data-testid="import-destination-name">
          {destination.display_name}
          <code>{destination.subject_id}</code>
        </dd>
      </div>
      <div className="import-facts__wide">
        <dt>QQ 私聊来源</dt>
        <dd className="import-pair" data-testid="import-source-pair">
          <span>{destination.qq}</span>
          <span className="import-pair__arrow" aria-hidden="true">→</span>
          <span>{destination.bot_qq}</span>
        </dd>
      </div>
    </dl>
  );
}

function SampleList({ samples }: { samples: MemoryImportSample[] }): React.ReactElement | null {
  if (samples.length === 0) return null;
  return (
    <section className="import-samples" aria-labelledby="import-samples-title" data-testid="import-samples">
      <h3 id="import-samples-title">
        样本<span className="import-count">{samples.length}</span>
      </h3>
      <ul>
        {samples.map((sample) => {
          const when = sampleTime(sample.occurred_at) ?? sampleTime(sample.created_at);
          return (
            <li key={sample.source_id} data-testid="import-sample">
              <span className="import-sample__kind">
                {sample.kind === 'episode' ? '经历' : '断言'}
              </span>
              <p className="import-sample__text">{sample.text}</p>
              {when ? <time dateTime={sample.occurred_at ?? sample.created_at ?? undefined}>{when}</time> : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function SkipBlock({
  preview,
  open,
}: {
  preview: MemoryImportPreview;
  open: boolean;
}): React.ReactElement | null {
  const entries = skipEntries(preview.skip_reasons);
  if (preview.duplicates === 0 && entries.length === 0) return null;
  return (
    <details className="import-secondary" data-testid="import-secondary" open={open || undefined}>
      <summary>
        <span>重复与跳过</span>
        <span className="import-secondary__sum">
          {preview.duplicates > 0 ? `重复 ${preview.duplicates}` : null}
          {preview.duplicates > 0 && entries.length > 0 ? ' · ' : null}
          {entries.length > 0 ? `跳过 ${preview.skipped}` : null}
        </span>
      </summary>
      <dl className="import-skip" data-testid="import-skip-reasons">
        {preview.duplicates > 0 ? (
          <div>
            <dt>已经导入过</dt>
            <dd>{preview.duplicates}</dd>
          </div>
        ) : null}
        {entries.map((entry) => (
          <div key={entry.reason}>
            <dt>{entry.label}</dt>
            <dd>{entry.count}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function PreviewStage({
  preview,
  busy,
  error,
  copy,
  onConfirm,
  onRepreview,
  onChooseAnother,
}: {
  preview: MemoryImportPreview;
  busy: boolean;
  error: ExplainedError | null;
  copy: ImportErrorCopy | null;
  onConfirm: () => void;
  onRepreview: () => void;
  onChooseAnother: () => void;
}): React.ReactElement {
  const empty = preview.accepted === 0;
  const zeroHint = empty ? zeroAcceptedHint(preview) : null;
  return (
    <>
      <DestinationBlock destination={preview.destination} />

      <div className={`import-count-block${empty ? ' import-count-block--empty' : ''}`} data-testid="import-accepted">
        <span className="import-count-block__value" data-testid="import-accepted-value">
          {preview.accepted}
        </span>
        <span className="import-count-block__unit">条可导入</span>
        <span className="import-count-block__total">文件共 {preview.total} 条</span>
      </div>

      {empty && zeroHint ? (
        <div className="import-zero" data-testid="import-zero">
          <strong>{zeroHint.headline}</strong>
          <p>{zeroHint.next}</p>
        </div>
      ) : null}

      {empty ? null : <SampleList samples={preview.sample} />}

      <SkipBlock preview={preview} open={empty} />

      {error && copy ? (
        <div className="import-error" role="alert" data-testid="import-error" data-error-detail={error.detail ?? error.code}>
          <AlertIcon size={14} />
          <div>
            <strong>{copy.reason}</strong>
            {copy.hint ? <p>{copy.hint}</p> : null}
            {error.traceId ? <p className="import-error__trace">trace：{error.traceId}</p> : null}
          </div>
        </div>
      ) : null}

      <div className="import-actions">
        {copy?.needsRepreview ? (
          <>
            <button type="button" className="button" onClick={onRepreview}>重新预览</button>
            <button type="button" className="ghost-button" onClick={onChooseAnother}>换个文件</button>
          </>
        ) : empty ? (
          <>
            <button type="button" className="button" onClick={onChooseAnother}>
              换个文件
            </button>
            <button type="button" className="ghost-button" disabled={busy} onClick={onRepreview}>
              重新预览
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="button"
              data-testid="import-confirm"
              disabled={busy || !preview.confirmation_token}
              onClick={onConfirm}
            >
              {busy ? '正在导入…' : `导入这 ${preview.accepted} 条`}
            </button>
            <button type="button" className="ghost-button" disabled={busy} onClick={onRepreview}>
              重新预览
            </button>
          </>
        )}
      </div>

      {preview.expires_at && !copy?.needsRepreview ? (
        <p className="import-expiry" data-testid="import-expiry">
          预览在 {sampleTime(preview.expires_at)} 前有效
        </p>
      ) : null}
    </>
  );
}

/* ==================================================================== 第三步：结果 */

function ResultStage({
  result,
  preview,
  onViewImported,
  onClose,
}: {
  result: MemoryImportCommit;
  preview: MemoryImportPreview;
  onViewImported: () => void;
  onClose: () => void;
}): React.ReactElement {
  const entries = skipEntries(result.skip_reasons);
  return (
    <>
      <div className="import-result" data-testid="import-result">
        <div className="import-result__main" data-testid="import-result-imported">
          <CheckIcon size={16} />
          <span className="import-count-block__value">{result.imported}</span>
          <span>条新增</span>
        </div>
        <dl className="import-result__rest">
          <div>
            <dt>重复</dt>
            <dd data-testid="import-result-duplicates">{result.duplicates}</dd>
          </div>
          <div>
            <dt>跳过</dt>
            <dd data-testid="import-result-skipped">{result.skipped}</dd>
          </div>
        </dl>
      </div>

      <DestinationBlock destination={result.destination} />

      {entries.length > 0 ? (
        <details className="import-secondary" data-testid="import-secondary">
          <summary>
            <span>跳过原因</span>
            <span className="import-secondary__sum">{result.skipped}</span>
          </summary>
          <dl className="import-skip">
            {entries.map((entry) => (
              <div key={entry.reason}>
                <dt>{entry.label}</dt>
                <dd>{entry.count}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}

      {result.imported === 0 ? (
        <p className="import-zero" data-testid="import-zero-result">
          这次没有新增条目。{result.skipped > 0 ? '可展开查看跳过原因。' : result.duplicates > 0 ? '这些记录已经在库里了。' : null}
        </p>
      ) : null}

      <div className="import-actions">
        <button type="button" className="button" data-testid="import-view-imported" onClick={onViewImported}>
          查看 AstrBot 记忆
        </button>
        <button type="button" className="ghost-button" onClick={onClose}>
          关闭
        </button>
      </div>

      {preview.accepted !== result.imported ? (
        <p className="import-expiry" data-testid="import-accepted-diff">
          预览时可导入 {preview.accepted} 条，实际新增 {result.imported} 条。
        </p>
      ) : null}
    </>
  );
}
