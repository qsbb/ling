/**
 * 夜间回顾：她某一晚整理后留下了哪些有来历的记忆。
 *
 * ## 为什么是和记忆库并列的独立分区
 *
 * 记忆库回答「她现在记得什么」，夜间回顾回答「某一晚她整理时留意到了什么」。
 * 两者取的是同一批记忆，但**时间口径不同**：记忆库是"当前有效"，回顾是
 * "那晚的窗口内、且此刻仍然成立"。塞进列表会让"为什么这条在这里不在那里"
 * 变成需要解释的问题，所以单独一栏，共享同一页头和 tab 条。
 *
 * ## 两条轴，不能合并成一个状态
 *
 * 后端的 `consolidation_state` 描述**合并归档**（ok/skipped/failed），
 * `review_status` 描述**回顾范围**（ready/empty/unavailable），两者独立。
 * 尤其 `skipped + ready` 是最常见的正常组合：那晚没有够老够低价值的记忆可并，
 * 但来源索引照样建立，于是有可读的回顾。把 `skipped` 画成失败，用户就会
 * 以为"昨晚出错了"，而实际上一切都按预期发生了。
 *
 * 所以这里：合并维度只说它做了什么（中性陈述），**只有 `failed` 带警示样式**；
 * 回顾维度单独说有没有东西可读。两者都写清，不互相顶替。
 *
 * ## 原话是证据，不是摘要
 *
 * `item.text` 是记忆本身（整理后的断言），`item.original` 是当时那几句话。
 * 界面上先给可读的那句，原话折叠在下面——用户要核对时再展开，避免一屏全是
 * 聊天记录。`truncated` 为真时明说不是全文，不假装完整。
 *
 * 服务端每次读取都重验来源，所以「原话不可再用」不是异常分支，而是正常结果：
 * 那一晚的条目可能在两次访问之间被撤回、纠正、过期或失去权限。这条路径必须
 * 自己说得清，不能让用户面对一个空面板猜发生了什么。
 *
 * ## 能读到原话，就要能走到它出现的地方
 *
 * 回顾是"她留下了什么"，核对是"这句话是在哪说的"。只给文本的话，用户没法确认
 * 这句话属于哪次对话——尤其两条记忆来自不同窗口时，光看文本分不出来。
 * 所以每条原话都带一个入口，指向 `item.window_id` 对应的会话。
 *
 * `window_id` 就是会话 id（服务端 `SessionRecord.session_id = window.id`），
 * 所以沿用既有的 `/s/{id}` 路由与授权，不新增端点、不自己拼鉴权。
 *
 * 为什么入口挂在**原话**上而不是整条记忆上：一条记忆的 `window_id` 是它自己的
 * 来源窗口，而原话才是用户真正要核对的那句话。契约里原话不单独带窗口，
 * 它与记忆同源，所以两条路径指向同一个会话——入口挂在原话上更贴近"我在核对
 * 这一句"的意图，同时展开区里也不再重复放第二个指向同一处的链接。
 */
import React from 'react';
import type {
  SleepReviewItem,
  SleepReviewNight,
  SleepReviewSource,
  SleepReviewsResponse,
} from '../types';
import type { ExplainedError } from '../errors';
import type { SectionState } from './MemoryPage';
import { navigate, routePath } from '../route';

/* ==================================================================== 文案 */

/**
 * 合并维度的措辞。
 *
 * 三档都用**陈述句**写"做了什么"，`failed` 例外——它是唯一需要人注意的一档。
 * `skipped` 的副本刻意带上"不需要合并"，因为用户最容易把它读成"没做"。
 */
export function consolidationCopy(
  state: SleepReviewNight['consolidation_state'],
  night: Pick<SleepReviewNight, 'groups_merged' | 'rows_archived'>,
): { label: string; detail: string; tone: 'neutral' | 'warn' } {
  if (state === 'failed') {
    return { label: '整理没做完', detail: '这一晚的整理中途出错，回顾可能不完整。', tone: 'warn' };
  }
  if (state === 'skipped') {
    return {
      label: '当晚不需要合并',
      detail: '没有够旧的同槽位记忆需要并到一起；这不影响回顾。',
      tone: 'neutral',
    };
  }
  const parts: string[] = [];
  if (night.groups_merged > 0) parts.push(`合并 ${night.groups_merged} 组`);
  if (night.rows_archived > 0) parts.push(`归档 ${night.rows_archived} 条`);
  return {
    label: '整理完成',
    detail: parts.length > 0 ? parts.join('、') : '这一晚整理完成。',
    tone: 'neutral',
  };
}

/**
 * 回顾维度的措辞。
 *
 * `empty` 与 `unavailable` 必须分开，因为**补救动作不同**：
 * `empty` 是"那晚有范围，但句子现在不能用了"（来源已撤回/纠正/过期/失权），
 * `unavailable` 是"根本没有范围可读"（旧版本记录，或这次没生成索引）。
 * 都写成"没有内容"会让第二种看起来像第一种，用户会以为自己的记忆丢了。
 */
export function reviewStatusCopy(status: SleepReviewNight['review_status']): {
  note: string | null;
  tone: 'neutral' | 'warn';
} {
  if (status === 'ready') return { note: null, tone: 'neutral' };
  if (status === 'empty') {
    return {
      note: '这一晚整理出了回顾范围，但此刻没有可读的条目：来源可能已撤回、被纠正、过期，或已不在当前权限内。',
      tone: 'neutral',
    };
  }
  return {
    note: '这一晚没有留下可读的回顾：这是旧版本的记录，或整理时没有生成来源索引。',
    tone: 'neutral',
  };
}

/** 日期按**字符串**切开，不经过 `Date`，避免时区把 `sleep_date` 推错一天。 */
export function sleepDateLabel(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  const [, year, month, day] = match;
  return `${Number(year)} 年 ${Number(month)} 月 ${Number(day)} 日`;
}

/** 原话的说话人。服务端的 speaker 是稳定标识，这里只把已知值翻成人话。 */
export function speakerLabel(speaker: string): string {
  if (speaker === 'user') return '你';
  if (speaker === 'assistant') return '她';
  return speaker;
}

/* ==================================================================== 原话 */

/**
 * 来源会话的地址。
 *
 * `window_id` 就是会话 id，所以直接复用 `/s/{id}` 这条既有路由——不新增端点、
 * 不自己拼鉴权，授权由聊天页原本那一套负责。地址编码与 `routePath` 保持一致，
 * 避免窗口 id 里出现需要转义的字符时两条路径拼出不同结果。
 */
export function sourceSessionHref(windowId: string): string {
  return routePath({ name: 'chat', sessionId: windowId });
}

/**
 * 打开来源会话。
 *
 * 走 `navigate` 而不是 `<a href>`：这条路径与聊天页内部切会话是同一个动作
 * （`useChat` 也用 `navigate(routePath({ name: 'chat', sessionId }))`），
 * 用同一条路径才不会出现"点链接整页刷新、点会话列表是软跳转"两种行为。
 * 地址仍由 `sourceSessionHref` 生成，所以中键/右键复制出来的链接是可用的。
 */
export function openSourceSession(windowId: string): void {
  navigate(sourceSessionHref(windowId));
}

/**
 * 一条原话。截断时明确标注，不让人误以为看到了全文。
 *
 * 末尾的入口是「这句话是在哪说的」：只给文本的话，两条来自不同窗口的记忆
 * 在界面上长得一样，用户没法核对来源。
 */
export function OriginalLine({
  source,
  windowId,
  onOpen,
}: {
  source: SleepReviewSource;
  windowId: string;
  onOpen: (windowId: string) => void;
}): React.ReactElement {
  return (
    <li className="sleep-original">
      <span className="sleep-original__speaker">{speakerLabel(source.speaker)}</span>
      <p className="sleep-original__text">
        {source.text}
        {source.truncated ? <span className="sleep-original__clip">（原话较长，此处截断）</span> : null}
      </p>
      <button
        type="button"
        className="sleep-original__open"
        data-testid="sleep-open-source"
        data-window-id={windowId}
        onClick={() => onOpen(windowId)}
      >
        查看来源会话
      </button>
    </li>
  );
}

/**
 * 一条回顾条目：先是记忆本身，再是可以展开核对的原话。
 *
 * 用 `<details>` 而不是受控 state：展开与否是纯展示层的事，不需要跟着
 * 重新取数重置，也不需要为每个条目记一个布尔值。
 */
export function SleepReviewRow({
  item,
  index,
  onOpenSource = openSourceSession,
}: {
  item: SleepReviewItem;
  index: number;
  onOpenSource?: (windowId: string) => void;
}): React.ReactElement {
  return (
    <li className="sleep-row" data-testid={`sleep-item-${index}`} data-window-id={item.window_id}>
      <p className="sleep-row__text">{item.text}</p>
      {item.original.length > 0 ? (
        <details className="sleep-row__original">
          <summary className="sleep-row__summary">
            原话 {item.original.length} 条
          </summary>
          <ul className="sleep-originals">
            {item.original.map((source, position) => (
              <OriginalLine
                key={`${item.memory_id}-${position}`}
                source={source}
                windowId={item.window_id}
                onOpen={onOpenSource}
              />
            ))}
          </ul>
        </details>
      ) : (
        // 契约允许 `original` 为空数组；这时不该假装有原话可看。
        // 没有可读原话就没有可核对的那句话，所以这里也不给来源入口——
        // 只凭一条摘要跳进会话，用户到了那边也找不到对应的句子。
        <p className="sleep-row__no-original">这条没有可核对的原话。</p>
      )}
    </li>
  );
}

/* ==================================================================== 一晚 */

/**
 * `reason` 是服务端写给人看的一句话，但它的内容常常已经被状态行说过了
 * （`ok` 时它是"合并 N 组、归档 M 条"的同义重复，`skipped` 时是同一句
 * "没有达到合并条件的记忆"）。只有 `failed` 的 reason 带出错原因，是状态行
 * 给不出的信息。
 *
 * 所以这里不做"总是显示"，只在它说出状态行没说的东西时才显示。
 */
export function reasonDetail(night: SleepReviewNight): string | null {
  if (night.consolidation_state === 'failed') return night.reason || null;
  return null;
}

export function SleepReviewNightCard({
  night,
  onOpenSource = openSourceSession,
}: {
  night: SleepReviewNight;
  onOpenSource?: (windowId: string) => void;
}): React.ReactElement {
  const consolidation = consolidationCopy(night.consolidation_state, night);
  const review = reviewStatusCopy(night.review_status);
  const detail = reasonDetail(night);
  const dateLabel = sleepDateLabel(night.sleep_date);

  return (
    <article className="panel sleep-night" data-testid="sleep-night">
      <header className="sleep-night__head">
        <h2 className="sleep-night__date">{dateLabel}</h2>
        <p
          className={`sleep-night__state sleep-night__state--${consolidation.tone}`}
          data-testid="sleep-consolidation"
          data-state={night.consolidation_state}
        >
          <span className="sleep-night__state-label">{consolidation.label}</span>
          <span className="sleep-night__state-detail">{consolidation.detail}</span>
        </p>
      </header>

      {detail ? <p className="sleep-night__reason">{detail}</p> : null}

      {review.note ? (
        <p className={`sleep-night__note sleep-night__note--${review.tone}`} data-testid="sleep-review-note">
          {review.note}
        </p>
      ) : null}

      {night.items.length > 0 ? (
        <ol className="sleep-rows" data-testid="sleep-items">
          {night.items.map((item, index) => (
            <SleepReviewRow
              key={item.memory_id}
              item={item}
              index={index}
              onOpenSource={onOpenSource}
            />
          ))}
        </ol>
      ) : null}
      {night.life?.length ? (
        <ol className="sleep-rows" aria-label="生活回顾">
          {night.life.map((item) => (
            <li className="sleep-row" key={item.source_id}>
              <small>{({
                activity: item.status === 'simulated_outcome' ? '模拟活动已完成' : '有来源的活动',
                dream: '梦境', plan: '取消的安排', impression: 'Ling 当时的感受',
                clarification: '用户后来的解释', intention: '还想做的事',
              })[item.kind]}</small>
              <p className="sleep-row__text">{item.text}</p>
              {item.window_id ? (
                <a className="sleep-original__open" href={sourceSessionHref(item.window_id)}>查看来源会话</a>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
    </article>
  );
}

/* ==================================================================== 分区 */

export interface SleepReviewProps {
  state: SectionState<SleepReviewsResponse>;
  onReload: () => void;
  /** 打开来源会话。测试注入，产品里走 `openSourceSession`。 */
  onOpenSource?: (windowId: string) => void;
}

/**
 * 整个夜间回顾分区。
 *
 * 四种真实分支各说各的话：
 * - 读取中：一行占位，不提前说"没有回顾"；
 * - 读取失败：说明失败本身并能重试，**不**降级成空状态——那会把"没读到"
 *   说成"没有"，用户会以为记忆丢了；
 * - 读到但 `nights` 为空：一条直接的空状态；
 * - 读到有内容：按服务端给的日期倒序渲染，不再前端排序。
 */
export function SleepReviewSection({
  state,
  onReload,
  onOpenSource = openSourceSession,
}: SleepReviewProps): React.ReactElement {
  if (state.status === 'loading') {
    return (
      <div className="sleep" data-testid="sleep-review">
        <p className="sleep__loading" role="status">正在读取夜间回顾…</p>
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className="sleep" data-testid="sleep-review">
        <div className="panel sleep__error" role="alert">
          <p className="sleep__error-title">夜间回顾没读出来</p>
          <p className="sleep__error-reason">{errorReason(state.error)}</p>
          <p className="sleep__error-hint">
            这不是"没有回顾"：现在的情况是没取到数据。稍后重试；如果一直失败，说明服务端没答上来。
          </p>
          <button type="button" className="ghost-button" onClick={onReload} data-testid="sleep-retry">
            重试
          </button>
        </div>
      </div>
    );
  }

  const nights = state.data?.nights ?? [];
  if (nights.length === 0) {
    return (
      <div className="sleep" data-testid="sleep-review">
        <div className="panel sleep__empty" data-testid="sleep-empty">
          <p>还没有可看的夜间回顾。</p>
          <p className="sleep__empty-hint">
            她睡着后会整理一次记忆；整理过的那一晚才会出现在这里。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="sleep" data-testid="sleep-review">
      {nights.map((night) => (
        <SleepReviewNightCard key={night.sleep_date} night={night} onOpenSource={onOpenSource} />
      ))}
    </div>
  );
}

/** 失败原因只取已知错误码的短句，不回显服务端内部细节。 */
export function errorReason(error: ExplainedError | null): string {
  if (!error) return '请求失败。';
  if (error.code === 'forbidden') return '当前账号没有读取这份回顾的权限。';
  if (error.code === 'network_error') return '连不上服务端。';
  if (error.code === 'unauthorized') return '登录状态已失效，重新登录后再试。';
  return error.reason || '请求失败。';
}
