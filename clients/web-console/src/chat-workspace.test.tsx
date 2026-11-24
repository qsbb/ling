/**
 * 对话页重组的判据（服务端渲染层）。
 *
 * 这一组钉的是**信息位置**，不是像素：
 *
 * 1. 会话列表主行只读「名称 + 群/私聊 + 近期活动」，外部键默认不出现；
 *    重命名/置顶/排序/外部键都在「更多」菜单与「会话信息」里按需展开。
 * 2. 消息主行只留时间与需要动手的状态；模型与「她这次看到了什么」收进
 *    每条消息自己的详情折叠区。
 * 3. 正文里的 `[image]` / `[voice]` 渲染成可读的媒体块（不是假图片，
 *    也不是一串裸露的方括号）。
 * 4. 历史边界：有更早的一页时给「读取更早的消息」，到头时给「这里是开头」。
 * 5. 输入区默认只露工具按钮；限制文案只在展开后出现。
 *
 * 每个断言都配一条反例说明它失败于哪种错误实现，见各用例注释。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { Composer } from './components/Composer';
import { MessageList } from './components/MessageList';
import { buildSessionRows, SessionSidebar } from './components/SessionSidebar';
import type { SessionRecord } from './types';
import { mergeLatestPage, type UiMessage } from './useChat';

vi.mock('./personaName', () => ({ usePersonaName: () => '灵' }));

const CREATED = '2026-11-07T04:23:00Z';
const GROUP_REF = 'onebot-default:group:100200400';

function session(patch: Partial<SessionRecord> & { session_id: string }): SessionRecord {
  return {
    schema_version: 'v1',
    actor_id: 'actor_ling',
    subject_id: 'subject_owner',
    window_id: patch.session_id,
    title: '新的对话',
    created_at: '2026-11-06T01:00:00Z',
    updated_at: CREATED,
    message_count: 12,
    archived: false,
    ...patch,
  };
}

function message(patch: Partial<UiMessage> & { key: string }): UiMessage {
  const { key, ...rest } = patch;
  return {
    key,
    messageId: key,
    turnId: null,
    clientTurnId: null,
    role: 'user',
    system: false,
    text: '内容',
    state: 'completed',
    createdAt: CREATED,
    modelLabel: null,
    errorCode: null,
    errorMessage: null,
    errorDetail: null,
    errorTraceId: null,
    retriable: false,
    local: false,
    attempt: 1,
    silenceReason: null,
    senderName: null,
    senderId: null,
    recalledAt: null,
    recalledByActor: false,
    sequence: 1,
    ...rest,
  };
}

function renderMessages(props: {
  messages: UiMessage[];
  loadingOlder?: boolean;
  hasMoreHistory?: boolean;
  promptEnabled?: boolean;
  variant?: 'web' | 'group' | 'private';
}): string {
  return renderToStaticMarkup(
    <MessageList
      messages={props.messages}
      activeTurn={null}
      live={null}
      loading={false}
      loadingOlder={props.loadingOlder ?? false}
      hasMoreHistory={props.hasMoreHistory ?? false}
      onLoadOlder={() => undefined}
      onRetry={() => undefined}
      onResend={() => undefined}
      onStop={() => undefined}
      onOpenSettings={() => undefined}
      variant={props.variant ?? 'web'}
      promptEnabled={props.promptEnabled ?? false}
      onOpenPrompt={() => undefined}
    />,
  );
}

const countOf = (html: string, needle: string): number => html.split(needle).length - 1;

describe('会话行：主行只读名称/群聊/近期活动', () => {
  const sessions = [
    session({
      session_id: 'g1',
      title: GROUP_REF,
      display_name: '阿懒的家',
      channel: 'onebot-default',
      peer_id: '100200400',
      message_count: 42,
      pinned: true,
    }),
    session({ session_id: 'w1', title: '深夜聊天', channel: 'web', message_count: 7 }),
  ];

  it('主行没有外部键，信息按需展开后仍完整', () => {
    const rows = buildSessionRows(sessions, 'w1', 'owner');
    // 主行读"名称"：群名优先，没有名字才回退号码。
    expect(rows.map((row) => row.heading.title)).toEqual(['阿懒的家', '深夜聊天']);
    expect(rows.map((row) => row.heading.kind)).toEqual(['group', 'web']);
    // 选中状态由 currentId 决定，不靠颜色猜。
    expect(rows.map((row) => row.active)).toEqual([false, true]);
    // 外部键仍在（会话信息里给），但**不在主行渲染**——反例：把 subtitle
    // 直接塞回主行，下面这条会立刻红。
    expect(rows[0]?.heading.subtitle).toBe(GROUP_REF);
    // 会话列表主行：外部键（窗口键）不得出现在渲染结果里——反例：把
    // subtitle 塞回 `.session__subtitle`，这条会红。
    const sidebar = renderToStaticMarkup(
      <SessionSidebar
        sessions={sessions}
        currentId="w1"
        open={false}
        user={{ id: 'u', username: 'o', role: 'owner', subject_id: 's', display_name: '主人' }}
        onClose={() => undefined}
        onSelect={() => undefined}
        onCreate={() => undefined}
        onRename={async () => true}
        onPin={async () => undefined}
        onMove={async () => undefined}
        onNavigateSettings={() => undefined}
        onNavigatePersona={() => undefined}
        onLogout={() => undefined}
      />,
    );
    expect(sidebar).toContain('阿懒的家');
    expect(sidebar).not.toContain(GROUP_REF);
    expect(sidebar).toContain('42 条');
  });

  it('置顶只用一个标记，不额外占一行文字', () => {
    const rows = buildSessionRows(sessions, null, 'owner');
    // 置顶组在前；组内顺序保持后端给的顺序。
    expect(rows.map((row) => row.session.session_id)).toEqual(['g1', 'w1']);
    expect(rows[0]?.session.pinned).toBe(true);
  });

  it('排序项只在同组邻居存在时出现（跨置顶组不串）', () => {
    const rows = buildSessionRows(
      [
        session({ session_id: 'p1', title: '置顶一', pinned: true }),
        session({ session_id: 'p2', title: '置顶二', pinned: true }),
        session({ session_id: 'n1', title: '普通一' }),
      ],
      null,
      'admin',
    );
    // 置顶组内可以互换；跨组（p2 → n1）不给"下移"，否则会出现"移到另一组"
    // 这种后端不支持的语义。
    expect(rows[0]?.next?.session_id).toBe('p2');
    expect(rows[1]?.next).toBeNull();
    expect(rows[2]?.previous).toBeNull();
  });

  it('成员没有排序能力，但网页会话仍可改名', () => {
    const rows = buildSessionRows(sessions, null, 'member');
    expect(rows.every((row) => row.next === null && row.previous === null)).toBe(true);
    expect(rows.map((row) => row.renamable)).toEqual([false, true]);
  });
});

describe('不在的联系：整行置灰但还能点开', () => {
  const gone = [
    session({
      session_id: 'left',
      title: 'onebot-default:group:100200401',
      display_name: '退掉的群',
      channel: 'onebot-default',
      peer_id: '100200401',
      contact_unavailable: 'left',
    }),
    session({
      session_id: 'kicked',
      title: 'onebot-default:group:100200402',
      display_name: '被踢的群',
      channel: 'onebot-default',
      peer_id: '100200402',
      contact_unavailable: 'kicked',
    }),
    session({
      session_id: 'deleted',
      title: 'onebot-default:private:100200301',
      display_name: '阿岚',
      channel: 'onebot-default',
      peer_id: '100200301',
      contact_unavailable: 'friend_deleted',
    }),
    session({ session_id: 'ok', title: '还在的群', display_name: '还在的群', channel: 'web' }),
  ];

  it('徽标换成状态词，三种原因分开说', () => {
    const rows = buildSessionRows(gone, null, 'owner');
    // 徽标位本来是「群/私聊」，不在之后换成具体原因——用户的第一反应
    // 永远是「我怎么不在了」，这个位置就得回答它。
    expect(rows.map((row) => row.badge)).toEqual(['已退群', '被移出', '已删友', '网页']);
    expect(rows.map((row) => row.unavailable)).toEqual([true, true, true, false]);
  });

  it('渲染成灰行，但**条目仍可点**（不设 disabled、不隐藏）', () => {
    const html = renderToStaticMarkup(
      <SessionSidebar
        sessions={gone}
        currentId={null}
        open={false}
        user={{ id: 'u', username: 'o', role: 'owner', subject_id: 's', display_name: '主人' }}
        onClose={() => undefined}
        onSelect={() => undefined}
        onCreate={() => undefined}
        onRename={async () => true}
        onPin={async () => undefined}
        onMove={async () => undefined}
        onNavigateSettings={() => undefined}
        onNavigatePersona={() => undefined}
        onLogout={() => undefined}
      />,
    );
    // 只有不在的那三条带灰行类；还在的那条不带——反例：把类无条件加上，
    // 这条会红。
    expect(countOf(html, 'session--unavailable')).toBe(3);
    // 灰不等于消失：四条都渲染出来了，且都能点。
    expect(countOf(html, 'class="session__main"')).toBe(4);
    expect(html).toContain('退掉的群');
    // 正常的会话也照样在列表里，且不带灰行类（上一条已断言只有 3 条带）。
    expect(html).toContain('还在的群');
    expect(html).not.toContain('disabled');
    // 徽标样式跟着换成"不在"，别让「已退群」顶着群聊那块暖色底。
    expect(countOf(html, 'data-kind="unavailable"')).toBe(3);
  });

  it('未知原因也置灰（认得出不可用比认得出是哪种更重要）', () => {
    const rows = buildSessionRows(
      [session({ session_id: 'x', title: 'x', contact_unavailable: 'brand_new_reason' as never })],
      null,
      'owner',
    );
    expect(rows[0]?.unavailable).toBe(true);
    expect(rows[0]?.badge).toBe('不在');
  });
});

describe('消息行：主行只留状态与时间', () => {
  it('模型名与入口都在详情折叠区，不在主行', () => {
    const html = renderMessages({
      messages: [
        message({
          key: 'a1',
          role: 'actor',
          text: '在。',
          modelLabel: 'hy3gw2/hy3',
          messageId: 'msg-a1',
        }),
      ],
      promptEnabled: true,
    });

    // 主行（气泡下方那行 meta）不得出现模型名——反例：把 modelLabel 放回
    // meta，这条会红。
    const metaStart = html.indexOf('class="message__meta message__meta--own"');
    const metaEnd = html.indexOf('</div>', metaStart);
    const meta = html.slice(metaStart, metaEnd);
    expect(meta).not.toContain('hy3');
    // 详情里既给模型、也给完整标识（悬停可核对）。
    expect(html).toContain('data-testid="message-details"');
    expect(html).toContain('title="hy3gw2/hy3"');
    expect(html).toContain('>hy3<');
    // 入口在详情里，且详情默认收起（`<details>` 没有 open 属性）。
    expect(html).toContain('data-testid="prompt-entry"');
    expect(html).not.toMatch(/<details[^>]*\sopen/);
  });

  it('没有模型也没有入口的消息不渲染空详情（否则每条都多一行折叠）', () => {
    const html = renderMessages({
      messages: [message({ key: 'a1', role: 'actor', text: '嗯' })],
      promptEnabled: true,
    });
    // promptEnabled 时她有入口，所以详情在；换成非 owner 就没有任何详情。
    expect(countOf(html, 'data-testid="message-details"')).toBe(1);
    const memberView = renderMessages({
      messages: [message({ key: 'a1', role: 'actor', text: '嗯' })],
      promptEnabled: false,
    });
    expect(countOf(memberView, 'data-testid="message-details"')).toBe(0);
  });

  it('失败消息仍把错误与重试放在主区，不藏进详情', () => {
    const html = renderMessages({
      messages: [
        message({
          key: 'a1',
          role: 'actor',
          text: '',
          state: 'failed',
          turnId: 'turn-1',
          retriable: true,
          errorCode: 'provider_error',
          errorMessage: '模型服务不可用',
        }),
      ],
    });
    expect(html).toContain('模型服务不可用');
    expect(html).toContain('打开设置');
    // 重试按钮直接可见（不需要展开详情才能救场）。
    expect(html).toContain('message-error__actions');
  });
});

describe('正文里的媒体占位', () => {
  it('[image] / [voice] 渲染成可读媒体块，而不是裸露标记', () => {
    const html = renderMessages({
      messages: [
        message({
          key: 'm1',
          role: 'user',
          text: '看这个 [image] 还有 [voice]',
          senderName: '阿青',
          senderId: '100200302',
        }),
      ],
      variant: 'group',
    });
    expect(countOf(html, 'data-testid="message-media"')).toBe(2);
    expect(html).toContain('data-kind="image"');
    expect(html).toContain('data-kind="voice"');
    expect(html).toContain('图片');
    expect(html).toContain('语音');
    // 方括号原文不再裸露（反例：只做 CSS 不拆 token）。
    expect(html).not.toContain('[image]');
    expect(html).not.toContain('[voice]');
    // 同一句里其余文字原样保留，顺序不被打乱。
    expect(html.indexOf('看这个')).toBeLessThan(html.indexOf('图片'));
    expect(html.indexOf('图片')).toBeLessThan(html.indexOf('还有'));
    expect(html.indexOf('还有')).toBeLessThan(html.indexOf('语音'));
  });

  it('普通文本不产生媒体块（不误伤方括号）', () => {
    const html = renderMessages({
      messages: [message({ key: 'm1', text: '数组 a[i] 和 [草稿] 都不是媒体' })],
    });
    expect(countOf(html, 'data-testid="message-media"')).toBe(0);
  });
});

describe('历史边界', () => {
  it('还有更早的一页时给按钮；正在读时按钮禁用并换文案', () => {
    const idle = renderMessages({
      messages: [message({ key: 'm1' })],
      hasMoreHistory: true,
    });
    expect(idle).toContain('data-testid="history-edge"');
    expect(idle).toContain('读取更早的消息');
    expect(idle).not.toContain('data-testid="history-start"');

    const busy = renderMessages({
      messages: [message({ key: 'm1' })],
      hasMoreHistory: true,
      loadingOlder: true,
    });
    expect(busy).toContain('正在读取更早的消息…');
    expect(busy).toContain('disabled=""');
  });

  it('到头时说清"这里是开头"（反例：一直显示"读取更早"，点了永远没结果）', () => {
    const html = renderMessages({ messages: [message({ key: 'm1' })], hasMoreHistory: false });
    expect(html).toContain('data-testid="history-start"');
    expect(html).toContain('这里是这段对话的开头');
    expect(html).not.toContain('data-testid="history-edge"');
  });
});

describe('输入区：工具默认收起，限制按需表达', () => {
  function renderComposer(busy = false): string {
    return renderToStaticMarkup(
      <Composer
        busy={busy}
        stopping={false}
        disabled={false}
        sessionId="s1"
        onSend={() => undefined}
        onStop={() => undefined}
      />,
    );
  }

  it('默认只露附件与语音两个入口，不铺时长/大小限制', () => {
    const html = renderComposer();
    expect(html).toContain('data-testid="composer-attach-toggle"');
    expect(html).toContain('data-testid="composer-audio-toggle"');
    expect(html).toContain('aria-label="消息输入框"');
    expect(html).toContain('aria-label="发送消息"');
    // 限制文案只在展开后出现——反例：把这段写死在收起态，这条会红。
    expect(html).not.toContain('10 MiB');
    expect(html).not.toContain('PNG');
    expect(countOf(html, 'data-testid="composer-panel')).toBe(0);
  });

  it('生成中给停止键，发送键禁用', () => {
    const html = renderComposer(true);
    expect(html).toContain('停止');
    expect(html).toMatch(/<button[^>]*disabled[^>]*aria-label="发送消息"/);
  });

  it('换行提示与发送提示同处一行（保留中文输入法约定）', () => {
    const html = renderComposer();
    expect(html).toContain('Enter 发送 · Shift + Enter 换行');
  });
});

describe('轮询/刷新与历史分页：合并而不是替换', () => {
  /** 260 条的会话；返回 1..260 的 UiMessage。 */
  const series = (from: number, to: number) => Array.from(
    { length: to - from + 1 },
    (_, index) => {
      const sequence = from + index;
      return message({
        key: `m-${sequence}`,
        messageId: `m-${sequence}`,
        text: `第 ${sequence} 条`,
        sequence,
        role: sequence % 2 === 0 ? 'actor' : 'user',
      });
    },
  );

  it('最新一页不会删掉已经往上加载的更早历史', () => {
    // 视图里已经有全部 260 条（分页加载过两次）
    const view = series(1, 260);
    // 轮询返回最新 200 条 + 两条新消息
    const latest = [...series(61, 260), ...series(261, 262)];
    const merged = mergeLatestPage(view, latest);

    // 反例：整体替换会让这条红（oldest 变成 61）——正是验收日志里的现象。
    expect(merged[0]?.sequence).toBe(1);
    expect(merged).toHaveLength(262);
    expect(merged.map((item) => item.sequence)).toEqual(
      Array.from({ length: 262 }, (_, index) => index + 1),
    );
  });

  it('服务端返回空数组时保持原视图（不能把历史清空）', () => {
    const view = series(1, 20);
    expect(mergeLatestPage(view, [])).toHaveLength(20);
  });

  it('本地未落库的消息排在末尾，且不会被"更早"规则误删', () => {
    const view = [
      ...series(1, 10),
      message({ key: 'local-1', messageId: null, sequence: null, text: '刚发出的', local: true }),
    ];
    const merged = mergeLatestPage(view, series(5, 12));
    expect(merged.map((item) => item.key)).toEqual([
      'm-1', 'm-2', 'm-3', 'm-4', 'm-5', 'm-6', 'm-7', 'm-8', 'm-9', 'm-10',
      'm-11', 'm-12', 'local-1',
    ]);
  });

  it('同一条以服务端为准，但保留前端独有的 clientTurnId', () => {
    const view = [
      message({
        key: 'm-5', messageId: 'm-5', sequence: 5, state: 'pending',
        local: true, clientTurnId: 'client-abc',
      }),
    ];
    const merged = mergeLatestPage(view, [
      message({ key: 'm-5', messageId: 'm-5', sequence: 5, state: 'completed', text: '服务端版本' }),
    ]);
    expect(merged).toHaveLength(1);
    expect(merged[0]?.state).toBe('completed');
    expect(merged[0]?.text).toBe('服务端版本');
    expect(merged[0]?.clientTurnId).toBe('client-abc');
  });

  it('比最新一页更早的条目一律保留（宁可多留，不能删错）', () => {
    /*
     * 一个刻意的取舍，写下来免得后来人"优化"掉：
     *
     * 合并时唯一能拿到的信息是"这一批里最小的 sequence"。比它更早的条目，
     * 要么是分页加载的历史，要么是别的来源（撤回清理、跨会话残留）。
     * **两者在数据上无法区分**，而删错一条的后果是"历史凭空消失"——
     * 正是这次验收要修的那个 bug。所以一律保留。
     *
     * 反例：如果改成"服务端没给的都丢"，这条会红，而真实的第 1 条也会一起丢。
     */
    const view = [...series(1, 5), ...series(300, 302)];
    const merged = mergeLatestPage(view, series(300, 305));
    expect(merged.slice(0, 5).map((item) => item.sequence)).toEqual([1, 2, 3, 4, 5]);
    expect(merged.at(-1)?.sequence).toBe(305);
  });

  it('已拿到服务端 message_id 的本地行，不会与最新一页各亮一次', () => {
    /*
     * 验收复现的那条：发送后本地先画一行（key=local-user-…），
     * `turn.started` 给它补上服务端 messageId，但 key 不变；随后语音提交触发
     * `loadMessages`，最新一页带回同一条（key=messageId）。只按 key 去重会把
     * 同一条消息当成两条，界面显示 sent=2 / reply=2。
     */
    const confirmedUser = message({
      key: 'local-user-abc',
      messageId: 'msg-2',
      sequence: null,
      local: false,
      clientTurnId: 'abc',
      text: '验收：刚发送的文字',
    });
    const confirmedActor = message({
      key: 'local-actor-abc',
      messageId: 'msg-3',
      sequence: null,
      local: false,
      role: 'actor',
      text: '验收：收到的文字回复',
    });
    const view = [
      message({ key: 'msg-1', messageId: 'msg-1', sequence: 1, text: '已有历史' }),
      confirmedUser,
      confirmedActor,
    ];
    const latest = [
      message({ key: 'msg-1', messageId: 'msg-1', sequence: 1, text: '已有历史' }),
      message({ key: 'msg-2', messageId: 'msg-2', sequence: 2, text: '验收：刚发送的文字' }),
      message({ key: 'msg-3', messageId: 'msg-3', sequence: 3, role: 'actor', text: '验收：收到的文字回复' }),
      message({ key: 'msg-4', messageId: 'msg-4', sequence: 4, text: '验收：语音转写刷新标记' }),
    ];
    const merged = mergeLatestPage(view, latest);

    // 反例：只按 key 去重时这里会是 5 条（本地两条 + 服务端三条）。
    expect(merged).toHaveLength(4);
    expect(merged.map((item) => item.messageId)).toEqual(['msg-1', 'msg-2', 'msg-3', 'msg-4']);
    // 认领后 key 换成服务端的：下次刷新不会再走一遍同样的错配。
    expect(merged[1]?.key).toBe('msg-2');
    // 重发要用的 clientTurnId 不能丢。
    expect(merged[1]?.clientTurnId).toBe('abc');
  });

  it('还没确认的本地消息仍然保留（不能为了去重把在飞的发送吞掉）', () => {
    const view = [
      message({ key: 'msg-1', messageId: 'msg-1', sequence: 1, text: '已有历史' }),
      message({
        key: 'local-user-new', messageId: null, sequence: null,
        local: true, state: 'pending', text: '刚敲下的这句',
      }),
    ];
    const merged = mergeLatestPage(view, [
      message({ key: 'msg-1', messageId: 'msg-1', sequence: 1, text: '已有历史' }),
    ]);
    expect(merged.map((item) => item.key)).toEqual(['msg-1', 'local-user-new']);
    expect(merged[1]?.state).toBe('pending');
  });

  it('首屏（空视图）仍是整体替换，边界由这一页是否取满决定', () => {
    // 这条钉的是区分力：合并逻辑不能顺带把"首屏边界"也算错。
    const full = mergeLatestPage([], series(161, 260));
    expect(full).toHaveLength(100);
    expect(full[0]?.sequence).toBe(161);
  });
});
