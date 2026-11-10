/**
 * 人设页（`/persona`）：两个 tab。
 *
 * - **角色设定**：多人设的**卡片列表**（本页本次改版的重点）。
 *   无人设 → 页面中间一句话 + 居中的「新建人设」；
 *   有人设 → 右上角「+ 创建人格」+ 卡片网格，点卡片进编辑表单。
 * - **对话与主动性**（⑤）：原本在 `/settings/dialogue`。Step 4 按用户拍板
 *   （设置只放程序与运维）把它迁回了这里——改「要不要开口」是行为调参，
 *   不是运维。表单本体仍是 `DialoguePage`（PolicyForm），只换了宿主。
 *
 * tab 是 URL 而不是组件状态（`?tab=dialogue`）：`/settings/dialogue` 的旧书签
 * 由壳层 `replace` 到这里，刷新/分享/后退都必须停在同一个 tab（DESIGN §1）。
 *
 * 两个 tab **各自加载自己的数据**：角色设定读 `/v1/personas` + 时间线，对话与主动性
 * 读 `/v1/settings/config`。把它们串成一串会让「角色设定读失败」连坐掉调参表单，
 * 而这两件事本来互不依赖。
 *
 * ## 多人设的两条铁律（改这个文件前先读）
 *
 * 1. **同一时间只有一套生效**，切换是**整套**换（不是逐字段合并）。所以"改哪一套"
 *    永远由卡片的 id 指明，写操作一律走 `/v1/personas/{id}`；`GET|PUT /v1/persona`
 *    只留给聊天页读称呼，不再承担编辑。
 * 2. **空就是空**。旧后端会把默认系统提示词回落进 `system_prompt`，前端一保存就
 *    把默认值冻结成用户"写过"的内容。现在后端不再回落，前端也**不许**用默认值
 *    预填输入框：空输入框只放 placeholder，`value` 一律照抄服务端给的原文。
 */

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { api, personaId } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { formatSessionTime } from '../format';
import { navigate, useLocationHref } from '../route';
import { personaTabFromHref, personaTabHref, type PersonaTab } from '../tabs';
import type { Persona, PersonaScenario, PersonaSummary, TimelineEvent } from '../types';
import '../persona.css';
import { DialoguePage } from './DialoguePage';
import { AlertIcon, ArrowLeftIcon, CheckIcon, PencilIcon, PlusIcon, RefreshIcon } from './Icons';
import { PageTabPanel, PageTabs, type PageTabSpec } from './PageTabs';

/** 与服务端 `domains/persona/scenarios.py` 的 MAX_SCENARIO_CHARS 对齐。 */
const MAX_SCENARIO_CHARS = 600;

/**
 * 卡片摘要取多少个字符。
 *
 * CSS 那边是 `-webkit-line-clamp: 2`（两行，宽度随网格走），这里再截一道是
 * 为了**别把 8000 字的系统提示词整个塞进 DOM**：每张卡片都带一份全文，十几套
 * 人格就是几百 KB 的隐藏文本。截断到 120 字后，两行怎么放都放不满，
 * 省略号由 CSS 在最末尾补。
 */
const SUMMARY_CHARS = 120;

/**
 * 把编辑中的草稿收成 PUT 的 payload。
 *
 * **空串不发送**：接口是整组替换语义，缺键就是"没有覆盖"，于是"清空"
 * 和"恢复默认"是同一条路径。发送空串反而是另一种意思（服务端也会把它
 * 丢掉），两种写法在日志里看起来一样、行为靠实现细节撑着，不值得。
 */
export function scenarioPayload(drafts: Record<string, string>): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const [key, value] of Object.entries(drafts)) {
    const text = value.trim();
    if (text) payload[key] = text;
  }
  return payload;
}

/** 服务端给的覆盖 → 编辑草稿。默认值不进草稿：它由占位提示展示。 */
export function scenarioDraftsFrom(persona: Persona): Record<string, string> {
  return { ...(persona.scenario_prompts ?? {}) };
}

/* ------------------------------------------------------------ 卡片显示的纯函数 */

/**
 * 这一套"有没有人设"。
 *
 * 判据优先级：**正文在本地**（详情已取回 / 来自 `GET /v1/persona`）就以正文为准，
 * 与注入侧 `persona_is_empty()` 是同一件事；正文不在本地时（列表项刻意不带正文）
 * 看后端的 `has_content`——它用的就是同一个函数。两者都没有 → 按"有"处理：
 * 宁可先显示"点开查看"，也不把一张可能有内容的卡片先说成空的。
 */
export function personaHasContent(persona: PersonaSummary): boolean {
  // 正文**在本地**：它就是答案，不用问后端（`''` 就是空，不是"缺失"）。
  if (typeof persona.system_prompt === 'string') {
    return persona.system_prompt.trim() !== '';
  }
  if (persona.has_content !== undefined) return persona.has_content === true;
  const chars = persona.system_prompt_chars;
  if (typeof chars === 'number') return chars > 0;
  return true;
}

/** 卡片标题：显示名优先，空串就退回机器名，再空就说明"没有名字"。 */
export function personaTitle(persona: Pick<Persona, 'display_name' | 'name'>): string {
  const display = (persona.display_name ?? '').trim();
  if (display) return display;
  const name = (persona.name ?? '').trim();
  return name || '未命名人格';
}

/**
 * 卡片摘要。`system_prompt` 为空时返回空串——**由调用方渲染成灰色提示**，
 * 不在这里编一句话，否则"没写"会看起来像"写了点什么"。
 *
 * 空白（换行、空格）先压成一行：系统提示词通常是多段，卡片上留两行
 * 正文比两行空行有用。
 */
export function personaSummary(systemPrompt: string | null | undefined): string {
  const text = (systemPrompt ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text.length > SUMMARY_CHARS ? `${text.slice(0, SUMMARY_CHARS)}…` : text;
}

/**
 * 创建时间：`YYYY/MM/DD HH:mm:ss`（参考图的写法，固定宽度、不受 locale 影响）。
 *
 * 拿不到或解析不了时返回空串，卡片整行不出现——**不画 `1970/01/01`**，
 * 那会把"后端没给这个字段"说成"这套人格是 1970 年建的"。
 */
export function formatPersonaTime(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/**
 * 两行能力徽章。
 *
 * **只显示后端真的给了的**：`tool_count` / `skill_count` 缺失就不出徽章。
 * 写死「使用所有可用工具」会把一个我们并不知道的状态说死——徽章在参考图里
 * 是事实陈述，不是装饰。全局那条（技能）即使为 0 也只说"没有技能"，
 * 不隐藏：0 是一个有意义的结论。
 */
export function personaBadges(persona: PersonaSummary): string[] {
  const badges: string[] = [];
  const tools = persona.tool_count;
  if (typeof tools === 'number' && Number.isFinite(tools) && tools >= 0) {
    badges.push(tools === 0 ? '不开放工具' : `使用 ${tools} 个工具`);
  }
  const skills = persona.skill_count;
  if (typeof skills === 'number' && Number.isFinite(skills) && skills >= 0) {
    badges.push(skills === 0 ? '没有技能' : `${skills} 个技能`);
  }
  return badges;
}

/**
 * 哪一套在生效。
 *
 * 三处信息按可信度排：列表项自己的 `is_active` > 列表响应里的 `active_id` >
 * `GET /v1/persona` 的 id。**都不是**就返回 null——页面上就一个「当前使用」
 * 徽章都没有，而不是默认把第一张标成生效（那会让人以为切换已经成功）。
 */
export function activePersonaId(
  personas: readonly PersonaSummary[],
  current: Persona | null,
  listActiveId: string | null = null,
): string | null {
  for (const persona of personas) {
    if (persona.is_active === true) {
      const id = personaId(persona);
      if (id) return id;
    }
  }
  if (listActiveId) {
    return personas.some((persona) => personaId(persona) === listActiveId) ? listActiveId : null;
  }
  if (!current) return null;
  const currentId = personaId(current);
  if (!currentId) return null;
  const known = personas.some((persona) => personaId(persona) === currentId);
  // 列表里没有它（刚被别处删掉）就不算"某张卡片在生效"：徽章挂不到卡片上。
  return known ? currentId : null;
}

/**
 * 把错误翻成一句能直接显示的话。
 *
 * `explainError` 对已知错误码给的是**概括**（409 → 「状态冲突」），而后端原话
 * 往往才是唯一有信息量的部分（「正在使用的人格不能删除，请先切换到其他人格」）。
 * 两者都要：先给概括，原话不同时再补一句。完全相同就只留一份。
 */
export function personaErrorMessage(error: unknown): string {
  const explained = explainError(error);
  const message = error instanceof Error ? error.message.trim() : '';
  if (message && message !== explained.reason) {
    return `${explained.reason}：${message}`;
  }
  return explained.reason;
}

/* ------------------------------------------------------------ 场景编辑区 */

interface ScenarioEditorProps {
  scenarios: PersonaScenario[];
  drafts: Record<string, string>;
  onChange: (key: string, value: string) => void;
}

/**
 * 「场景处理」区：同一份人设，在不同场合该怎么处理。
 *
 * 空着的输入框不是"没写"，而是"用默认"——所以占位提示直接显示默认文本，
 * 而不是放一句"请输入"。想看清默认到底写了什么，不应该需要先删掉自己的话。
 */
export function ScenarioEditor({ scenarios, drafts, onChange }: ScenarioEditorProps) {
  const customCount = scenarios.filter(
    (s) => (drafts[s.key] ?? '').trim().length > 0,
  ).length;

  return (
    <div className="scenario-block">
      <div className="scenario-block__head">
        <h3>场景处理</h3>
        <span className="panel__count">
          {customCount > 0 ? `${customCount} 条自己写的` : '全部用默认'}
        </span>
      </div>
      <p className="panel__note">
        不同场合她该怎么做，会在这个场合真的出现时注入提示词。空着就用默认（灰色字），
        写了自己的话按你写的来。
      </p>
      <div className="scenarios">
        {scenarios.map((scenario) => {
          const draft = drafts[scenario.key] ?? '';
          const custom = draft.trim().length > 0;
          return (
            <div className="scenario" key={scenario.key}>
              <div className="scenario__head">
                <span className="scenario__label">{scenario.label}</span>
                <span
                  className={`scenario__state${custom ? ' scenario__state--custom' : ''}`}
                >
                  {custom ? '自己写的' : '默认'}
                </span>
                {custom ? (
                  <button
                    type="button"
                    className="button button--small scenario__reset"
                    onClick={() => onChange(scenario.key, '')}
                    data-testid={`scenario-reset-${scenario.key}`}
                  >
                    恢复默认
                  </button>
                ) : null}
              </div>
              <p className="scenario__when">{scenario.when}</p>
              <textarea
                className="field__input field__textarea scenario__input"
                rows={3}
                maxLength={MAX_SCENARIO_CHARS}
                aria-label={`${scenario.label}的处理方式`}
                placeholder={scenario.default_text}
                value={draft}
                onChange={(event) => onChange(scenario.key, event.target.value)}
                data-testid={`scenario-input-${scenario.key}`}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ 页面外壳 */

interface PersonaPageProps {
  /** 离开本页（回对话）。 */
  onBack: () => void;
}

export const PERSONA_TABS: readonly PageTabSpec<PersonaTab>[] = [
  { key: 'persona', label: '角色设定', testid: 'persona-tab-profile' },
  { key: 'dialogue', label: '对话与主动性', testid: 'persona-tab-dialogue' },
];

export function PersonaPage({ onBack }: PersonaPageProps) {
  const href = useLocationHref();
  const tab = personaTabFromHref(href);
  const openTab = (next: PersonaTab) => navigate(personaTabHref(next));

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回对话
      </button>

      <h1 className="page__title">人设</h1>
      <p className="page__hint">
        她是谁、怎么说话、什么时候开口。记忆内容在
        <button type="button" className="link-button" onClick={() => navigate('/memory')}>
          记忆页
        </button>
        。
      </p>

      <PageTabs label="人设页分区" tabs={PERSONA_TABS} current={tab} onSelect={openTab} />

      <PageTabPanel tab="persona" current={tab}>
        <PersonaSettings />
      </PageTabPanel>
      <PageTabPanel tab="dialogue" current={tab}>
        <DialoguePage onBack={() => openTab('persona')} backLabel="返回角色设定" />
      </PageTabPanel>
    </section>
  );
}

/* ------------------------------------------------------------ 卡片与列表 */

/** 「⋮」按钮的三个点。不做成 SVG：三个圆点在 CSS 里就够，且不会被描边糊掉。 */
function KebabDots() {
  return (
    <span className="persona-kebab__dots" aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  );
}

interface PersonaCardProps {
  persona: PersonaSummary;
  active: boolean;
  busy: boolean;
  menuOpen: boolean;
  onOpen: () => void;
  onToggleMenu: () => void;
  onActivate: () => void;
  onDelete: () => void;
}

/**
 * 一张人格卡片。
 *
 * 整张卡片可点（进编辑），但**卡片本身不是按钮**：里面还有「⋮」和它的菜单项。
 * 用 `role="button"` 套嵌套按钮是无效 HTML，也会让读屏把菜单项读成卡片的一部分。
 * 于是标题才是那个"进编辑"的按钮，卡片上再挂 `onClick` 兜住空白处的点击。
 */
export function PersonaCard({
  persona,
  active,
  busy,
  menuOpen,
  onOpen,
  onToggleMenu,
  onActivate,
  onDelete,
}: PersonaCardProps) {
  const id = personaId(persona);
  const title = personaTitle(persona);
  const summary = personaSummary(persona.system_prompt);
  // 列表接口不带正文：摘要要么是本地已取到的正文，要么是"写了 N 字，点开看"。
  // **不能**因为"本地没有正文"就画成"未设置人设"——那是把没加载说成没有。
  const pendingSummary =
    summary === '' && personaHasContent(persona)
      ? typeof persona.system_prompt_chars === 'number' && persona.system_prompt_chars > 0
        ? `已写 ${persona.system_prompt_chars} 字，点开查看`
        : '点开查看人设内容'
      : '';
  const created = formatPersonaTime(persona.created_at);
  const badges = personaBadges(persona);
  const className = [
    'persona-card',
    active ? 'persona-card--active' : '',
    menuOpen ? 'persona-card--menu-open' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <article
      className={className}
      data-testid={`persona-card-${id}`}
      data-active={active ? 'true' : 'false'}
      aria-busy={busy ? 'true' : undefined}
      onClick={(event) => {
        // 点自己（而不是菜单里的东西）才进编辑；菜单开着时先收起，别跳走。
        if (menuOpen) return;
        if ((event.target as HTMLElement).closest('.persona-card__menu')) return;
        onOpen();
      }}
    >
      <div className="persona-card__head">
        <button
          type="button"
          className="persona-card__title"
          onClick={(event) => {
            event.stopPropagation();
            if (menuOpen) return;
            onOpen();
          }}
          data-testid={`persona-edit-${id}`}
        >
          {title}
        </button>

        {active ? (
          <span className="persona-card__active" data-testid={`persona-active-${id}`}>
            <CheckIcon size={12} /> 当前使用
          </span>
        ) : null}

        <div className={`persona-card__menu${menuOpen ? ' persona-card__menu--open' : ''}`}>
          <button
            type="button"
            className="persona-card__kebab"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label={`${title} 的更多操作`}
            disabled={busy}
            onClick={(event) => {
              event.stopPropagation();
              onToggleMenu();
            }}
            data-testid={`persona-menu-${id}`}
          >
            <KebabDots />
          </button>
          {menuOpen ? (
            <div className="persona-card__dropdown" role="menu">
              <button
                type="button"
                role="menuitem"
                className="persona-card__menu-item"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpen();
                }}
              >
                <PencilIcon size={14} /> 编辑
              </button>
              <button
                type="button"
                role="menuitem"
                className="persona-card__menu-item"
                disabled={active}
                title={active ? '这套人格正在生效' : undefined}
                onClick={(event) => {
                  event.stopPropagation();
                  onActivate();
                }}
                data-testid={`persona-activate-${id}`}
              >
                <CheckIcon size={14} /> {active ? '正在使用' : '设为当前'}
              </button>
              <button
                type="button"
                role="menuitem"
                className="persona-card__menu-item persona-card__menu-item--danger"
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete();
                }}
                data-testid={`persona-delete-${id}`}
              >
                删除
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {summary || pendingSummary ? (
        <p
          className={`persona-card__summary${summary ? '' : ' persona-card__summary--pending'}`}
          data-testid={`persona-summary-${id}`}
        >
          {summary || pendingSummary}
        </p>
      ) : (
        // 空系统提示词：灰色一句"没写"。它**不是**摘要的占位符——
        // 用户要能一眼看出这套人格现在不会注入任何角色设定。
        <p
          className="persona-card__summary persona-card__summary--empty"
          data-testid={`persona-summary-${id}`}
        >
          （未设置人设：不会注入任何角色设定）
        </p>
      )}

      {badges.length > 0 ? (
        <ul className="persona-card__badges">
          {badges.map((badge) => (
            <li className="persona-badge" key={badge}>
              {badge}
            </li>
          ))}
        </ul>
      ) : null}

      {created || persona.published === false ? (
        <div className="persona-card__foot">
          <span className="persona-card__time">
            {created ? `创建时间：${created}` : ''}
          </span>
          {persona.published === false ? (
            <span className="persona-badge persona-badge--muted">未发布</span>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

interface PersonaEmptyStateProps {
  onCreate: () => void;
  disabled?: boolean;
}

/**
 * 无人设时的空状态。
 *
 * 按钮**在页面中间**（用户原话），不是右上角：此刻页面上唯一值得做的事就是
 * 建第一套人格，"另起一行放个次要按钮"会让人以为还有别的东西可点。
 *
 * 那句话（"没有人设时……"）不是免责声明，是行为说明：不注入角色设定 ≠ 模型
 * 什么都不收到（功能提示词还在）。用户会据此判断"空着会不会变笨"。
 */
export function PersonaEmptyState({ onCreate, disabled = false }: PersonaEmptyStateProps) {
  return (
    <div className="persona-empty" data-testid="persona-empty">
      <h3 className="persona-empty__title">暂无人设</h3>
      <p className="persona-empty__text">
        还没有创建人格。没有人设时，模型只会收到功能提示词，不会注入任何角色设定。
      </p>
      <button
        type="button"
        className="button button--primary persona-empty__action"
        onClick={onCreate}
        disabled={disabled}
        data-testid="persona-empty-create"
      >
        <PlusIcon size={16} /> 新建人设
      </button>
    </div>
  );
}

/* ------------------------------------------------------------ 角色设定本体 */

/** 列表/编辑两段式：`list` 是卡片网格，`detail` 是同一套人格的编辑表单。 */
type PersonaView = { kind: 'list' } | { kind: 'detail'; personaId: string };

/**
 * ① 身份与人格的本体。
 *
 * 外壳（返回 / 标题 / tab 条）由 `PersonaPage` 提供，这里只负责 tab 面板内部。
 *
 * 两段式而不是两张并排：编辑表单有 5 个字段 + 场景覆盖，跟卡片网格挤在一屏里
 * 两边都会变小；"点进去改、改完回来"也符合卡片列表的直觉。切回列表时**重读**
 * 一次列表（保存会改摘要与修订号，切换会改激活标记）。
 */
function PersonaSettings() {
  const [personas, setPersonas] = useState<PersonaSummary[]>([]);
  /**
   * 后端 `total`。空状态以它为准：`active_persona_id` 在删光之后仍然有值
   * （它就是一个 id），按它判空会得到"有一个人格但其实一张卡都没有"。
   */
  const [total, setTotal] = useState<number | null>(null);
  /** 列表接口给的 `active_persona_id`（"哪套生效"的第二判据）。 */
  const [turnActiveId, setTurnActiveId] = useState<string | null>(null);
  const [current, setCurrent] = useState<Persona | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [view, setView] = useState<PersonaView>({ kind: 'list' });
  const [creating, setCreating] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<PersonaSummary | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<ExplainedError | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      // 列表是主角，`/v1/persona` 只是"哪套在生效"的**回退来源**、时间线是旁白：
      // 两者谁挂了都不该让卡片列表整页消失，所以列表单独 try，其余 allSettled。
      const list = await api.listPersonas();
      const [currentResult, timelineResult] = await Promise.allSettled([
        api.persona(),
        api.timeline(50),
      ]);
      if (!alive.current) return;
      const fallbackActive =
        currentResult.status === 'fulfilled' ? currentResult.value : null;
      setTurnActiveId(list.activePersonaId);
      setTotal(list.total);
      setCurrent(fallbackActive);
      setEvents(timelineResult.status === 'fulfilled' ? timelineResult.value.events : []);

      // 列表项**没有正文**（后端刻意不带），卡片要摘要就得按 id 拉详情。
      // 失败的保持原样（渲染成"点开查看"），不影响整页——多取一份摘要
      // 失败不等于人设不存在。
      // **每次都重取**，不拿上一轮的正文当缓存：上一轮的正文可能正是用户刚
      // 在表单里清空的那一份（清空后重新读，卡片却还显示旧摘要——这是错的）。
      // 代价是每次进列表多 N 个请求（N = 卡片数，量级个位数）。
      const details = await Promise.allSettled(
        list.items.map((item) => {
          const id = personaId(item);
          return id
            ? api.getPersona(id)
            : Promise.reject(new Error(`人格没有 id：${personaTitle(item)}`));
        }),
      );
      if (!alive.current) return;
      setPersonas(
        list.items.map((item, index) => {
          const detail = details[index];
          if (!detail || detail.status !== 'fulfilled') return item;
          // 详情里的 `scenarios` / 正文是全的；列表项上的 `is_active` 留着。
          // 顺序要紧：**列表项在前**（它的 `is_active`/`has_content`/`published`
          // 是列表接口当次算的），详情只补正文与场景（`Persona` 的必填字段）。
          // 反过来写会把列表项缺的键用 `undefined` 盖上去。
          return { ...item, ...detail.value };
        }),
      );
    } catch (error) {
      if (!alive.current) return;
      setLoadError(explainError(error));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const activeId = activePersonaId(personas, current, turnActiveId);
  /**
   * "有没有人格"**以 `total` 为准**：删光之后 `active_persona_id` 仍然有值
   * （它就是一个 id），按它判会得到"有人格、但一张卡都没有"。
   * `total` 缺失（旧后端）时退回列表长度。
   */
  const hasAny = (total ?? personas.length) > 0;

  const openCreate = () => {
    setMenuFor(null);
    setMessage(null);
    setFormError(null);
    setCreating(true);
  };

  const backToList = async () => {
    setView({ kind: 'list' });
    setMessage(null);
    setFormError(null);
    await load();
  };

  const activate = async (persona: PersonaSummary) => {
    const id = personaId(persona);
    if (!id || busyId) return;
    setMenuFor(null);
    setMessage(null);
    setFormError(null);
    setBusyId(id);
    try {
      await api.activatePersona(id);
      if (!alive.current) return;
      // 回执只有一个 id：谁是"生效"的以重读后的列表为准，不在本地改标记。
      await load();
      if (!alive.current) return;
      setMessage(`已切换到「${personaTitle(persona)}」，后续轮次整套按这套人格走`);
    } catch (error) {
      if (alive.current) setFormError(personaErrorMessage(error));
    } finally {
      if (alive.current) setBusyId(null);
    }
  };

  const requestDelete = (persona: PersonaSummary) => {
    setMenuFor(null);
    setMessage(null);
    setFormError(null);
    setPendingDelete(persona);
  };

  const confirmDelete = async () => {
    const target = pendingDelete;
    if (!target || busyId) return;
    const id = personaId(target);
    if (!id) return;
    setBusyId(id);
    try {
      await api.deletePersona(id);
      if (!alive.current) return;
      setPendingDelete(null);
      // 删的正好是正在编辑的那套 → 回列表（详情页已经没有数据可显示了）。
      setView((prev) => (prev.kind === 'detail' && prev.personaId === id ? { kind: 'list' } : prev));
      await load();
      if (alive.current) setMessage(`已删除「${personaTitle(target)}」`);
    } catch (error) {
      // **不关对话框**：后端拒绝（常见于"正在生效的人格不能删"）时这里显示原因，
      // 用户可以在同一个框里决定是先切换还是算了，不用重新打开一遍。
      if (alive.current) setFormError(personaErrorMessage(error));
    } finally {
      if (alive.current) setBusyId(null);
    }
  };

  // 首帧：`total` 还没拿到（null）= 还在读；拿到 0 = 真的没有任何人格。
  if ((loading || total === null) && personas.length === 0 && !loadError) {
    return (
      <div className="page__loading">
        <span className="spinner" aria-hidden="true" /> 正在读取角色设定…
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="load-error" role="alert">
        <div className="load-error__head">
          <AlertIcon />
          <strong>{loadError.reason}</strong>
        </div>
        {loadError.hint ? <p>{loadError.hint}</p> : null}
        <button type="button" className="button button--small" onClick={() => void load()}>
          重新读取
        </button>
      </div>
    );
  }

  if (creating) {
    return (
      <PersonaForm
        persona={null}
        current={current}
        events={events}
        busy={saving}
        error={formError}
        onBusyChange={setSaving}
        onError={setFormError}
        onCancel={() => {
          setCreating(false);
          setFormError(null);
        }}
        onSaved={async (saved) => {
          setCreating(false);
          // 先重读列表再报成功：卡片上的摘要/修订号/激活标记都来自列表，
          // 只弹一句"已创建"而列表还是旧的，下一眼就是矛盾的。
          await load();
          if (alive.current) {
            setMessage(
              `已创建「${personaTitle(saved)}」（修订 #${saved.revision}），后续轮次立即生效`,
            );
          }
        }}
      />
    );
  }

  if (view.kind === 'detail') {
    const target =
      personas.find((persona) => personaId(persona) === view.personaId) ??
      (personaId(current ?? {}) === view.personaId ? current : null);
    if (!target) {
      // 正在编辑的那套被别处删掉了／列表还没刷新回来：说清楚，给一条回去的路。
      return (
        <div className="domain-body">
          <div className="load-error" role="alert">
            <div className="load-error__head">
              <AlertIcon />
              <strong>这套人格已经不存在了</strong>
            </div>
            <p>它可能已被删除。回到列表可以重新选择。</p>
            <button type="button" className="button button--small" onClick={() => void backToList()}>
              返回人格列表
            </button>
          </div>
        </div>
      );
    }
    return (
      <PersonaForm
        persona={target}
        current={current}
        events={events}
        busy={saving}
        error={formError}
        onBusyChange={setSaving}
        onError={setFormError}
        onCancel={() => void backToList()}
        onSaved={async (saved) => {
          setView({ kind: 'list' });
          await load();
          if (alive.current) {
            setMessage(
              `已保存「${personaTitle(saved)}」（修订 #${saved.revision}），后续轮次立即生效`,
            );
          }
        }}
      />
    );
  }

  return (
    <div className="domain-body" data-testid="persona-list">
      <div className="page__head">
        <div>
          <h2 className="page__title">人格设定</h2>
          <p className="page__subtitle">
            管理人格角色设定。多套人格可以并存，同一时间只有一套生效；切换时整套替换。
          </p>
        </div>
        <div className="persona-list__actions">
          <button
            type="button"
            className="button button--small"
            onClick={() => void load()}
            disabled={loading}
          >
            <RefreshIcon /> 刷新
          </button>
          {hasAny ? (
            <button
              type="button"
              className="button button--primary button--small"
              onClick={openCreate}
              data-testid="persona-create"
            >
              <PlusIcon size={16} /> 创建人格
            </button>
          ) : null}
        </div>
      </div>

      {message ? (
        <div className="banner banner--ok" role="status">
          {message}
        </div>
      ) : null}

      {formError && !pendingDelete ? (
        <div className="banner banner--error" role="alert">
          {formError}
        </div>
      ) : null}

      {!hasAny || personas.length === 0 ? (
        <PersonaEmptyState onCreate={openCreate} />
      ) : (
        <div className="persona-grid" data-testid="persona-grid">
          {personas.map((persona) => {
            const id = personaId(persona);
            return (
              <PersonaCard
                key={id || personaTitle(persona)}
                persona={persona}
                active={id !== '' && id === activeId}
                busy={busyId === id}
                menuOpen={menuFor === id}
                onOpen={() => {
                  setMenuFor(null);
                  setMessage(null);
                  setFormError(null);
                  setView({ kind: 'detail', personaId: id });
                }}
                onToggleMenu={() => setMenuFor((prev) => (prev === id ? null : id))}
                onActivate={() => void activate(persona)}
                onDelete={() => requestDelete(persona)}
              />
            );
          })}
        </div>
      )}

      {pendingDelete ? (
        <DeleteConfirm
          persona={pendingDelete}
          isActive={personaId(pendingDelete) === activeId}
          busy={busyId !== null}
          error={formError}
          onCancel={() => {
            setPendingDelete(null);
            setFormError(null);
          }}
          onConfirm={() => void confirmDelete()}
        />
      ) : null}

      {events.length > 0 ? <PersonaTimeline events={events} /> : null}
    </div>
  );
}

/* ------------------------------------------------------------ 删除确认 */

interface DeleteConfirmProps {
  persona: PersonaSummary;
  isActive: boolean;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}

/**
 * 删除的二次确认。
 *
 * 用 `<dialog open>` 而不是自绘遮罩：焦点、Esc、"模态"语义都由浏览器给。
 * 删的是**正在生效**的那套时，这里提前把后果说清（后端也可能直接拒绝，
 * 那条错会原样显示在下面 `error` 里，不在这里替后端做判断）。
 */
export function DeleteConfirm({
  persona,
  isActive,
  busy,
  error,
  onCancel,
  onConfirm,
}: DeleteConfirmProps) {
  const title = personaTitle(persona);
  return (
    <dialog
      className="persona-dialog"
      open
      data-testid="persona-delete-confirm"
      // 原生 Esc：`onCancel` 不阻止默认行为，对话框会自己关掉，状态得跟着清，
      // 否则下次打开时还挂着上一次的错误。
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <h3 className="persona-dialog__title">删除「{title}」？</h3>
      <p className="persona-dialog__text">
        删除后这套人格的设定无法恢复。
        {isActive
          ? '它正在生效：如果服务端要求先切换到别的人格，这次删除会被拒绝并给出原因。'
          : '当前生效的不是这一套，删除不影响她现在的设定。'}
      </p>
      {error ? (
        <div className="banner banner--error" role="alert" data-testid="persona-delete-error">
          {error}
        </div>
      ) : null}
      <div className="persona-dialog__actions">
        <button type="button" className="button button--small" onClick={onCancel} disabled={busy}>
          取消
        </button>
        <button
          type="button"
          className="button button--small button--danger"
          onClick={onConfirm}
          disabled={busy}
          data-testid="persona-delete-confirm-ok"
        >
          {busy ? '删除中…' : '确认删除'}
        </button>
      </div>
    </dialog>
  );
}

/* ------------------------------------------------------------ 编辑表单 */

interface PersonaFormProps {
  /** `null` = 新建。 */
  persona: PersonaSummary | null;
  current: Persona | null;
  events: TimelineEvent[];
  /** 保存中：按钮文案与禁用都由它决定（状态归列表层，切回去时不会闪）。 */
  busy: boolean;
  onBusyChange: (busy: boolean) => void;
  error: string | null;
  onError: (message: string | null) => void;
  onCancel: () => void;
  onSaved: (saved: PersonaSummary, verb: string) => void | Promise<void>;
}

type TraitsState =
  | { kind: 'empty'; text: string; touched: boolean }
  | { kind: 'object'; text: string };

/**
 * 编辑 / 新建同一份表单。
 *
 * `persona === null` 时是新建：字段全部为空，placeholder 说明"不填会怎样"。
 * 两处刻意的"空"：
 *
 * 1. **系统提示词不预填任何东西**。`value` 只照抄服务端给的原文，空就是空；
 *    "不填则不注入人设"写在 placeholder 里。
 * 2. **`traits` 的空对象不算用户写过**。JSON 编辑器需要合法 JSON，所以框里
 *    显示 `{}`，但用 `traitsTouched` 记住"这是不是用户敲出来的"：没敲过就
 *    显示 placeholder，而且**保存时不发送 `traits`**（缺键 = 没有特征）。
 *    否则每次编辑都会把 `{}` 写成"用户明确写了空对象"，二者在库里无从区分。
 */
function PersonaForm({
  persona,
  current,
  events,
  busy,
  onBusyChange,
  error,
  onError,
  onCancel,
  onSaved,
}: PersonaFormProps) {
  const editing = persona !== null;
  const initialTraits = persona?.traits ?? {};
  const [name, setName] = useState(persona?.name ?? '');
  const [displayName, setDisplayName] = useState(persona?.display_name ?? '');
  const [systemPrompt, setSystemPrompt] = useState(persona?.system_prompt ?? '');
  const [traits, setTraits] = useState<TraitsState>(() =>
    Object.keys(initialTraits).length === 0
      ? { kind: 'empty', text: '{}', touched: false }
      : { kind: 'object', text: JSON.stringify(initialTraits, null, 2) },
  );
  const [boundariesText, setBoundariesText] = useState((persona?.boundaries ?? []).join('\n'));
  // 场景覆盖的编辑草稿：只有"自己写过"的键会出现在这里，默认值靠占位提示展示。
  const [scenarioDrafts, setScenarioDrafts] = useState<Record<string, string>>(() =>
    persona ? scenarioDraftsFrom(persona) : {},
  );
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    setLocalError(null);
  }, [persona]);

  const shownError = localError ?? error;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setLocalError(null);
    onError(null);

    const trimmedName = name.trim();
    const trimmedDisplay = displayName.trim();
    if (!editing && !trimmedName) {
      setLocalError('先给这套人格起个名字（只用于区分，不影响她怎么说话）');
      return;
    }

    // traits：只有用户真的写进框里的内容才会被解析与发送。
    const traitsText = traits.text.trim();
    let traitsValue: Record<string, unknown> | undefined;
    if (traits.kind === 'object' || traits.touched) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(traitsText || '{}');
      } catch {
        setLocalError('性格特征不是合法 JSON');
        return;
      }
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setLocalError('性格特征必须是 JSON 对象，例如 {"tone": "温和"}');
        return;
      }
      // 用户写了 `{}` = 明确"没有特征"，与从没写过等价 → 不发这个键。
      if (Object.keys(parsed as Record<string, unknown>).length > 0) {
        traitsValue = parsed as Record<string, unknown>;
      }
    }

    const boundaries = boundariesText
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);

    onBusyChange(true);
    try {
      const saved = editing
        ? await api.updatePersonaById(personaId(persona), {
            ...(trimmedName && trimmedName !== persona.name ? { name: trimmedName } : {}),
            display_name: trimmedDisplay,
            system_prompt: systemPrompt,
            traits: traitsValue ?? {},
            boundaries,
            scenario_prompts: scenarioPayload(scenarioDrafts),
          })
        : await api.createPersona({
            // 后端 `PersonaCreate` **没有 `name`**：库里那一列由服务端从
            // display_name 生成，id 也由服务端生成。表单校验过的名字就是它。
            display_name: trimmedDisplay || trimmedName,
            ...(systemPrompt.trim() ? { system_prompt: systemPrompt } : {}),
            ...(traitsValue ? { traits: traitsValue } : {}),
            ...(boundaries.length > 0 ? { boundaries } : {}),
            ...(Object.keys(scenarioPayload(scenarioDrafts)).length > 0
              ? { scenario_prompts: scenarioPayload(scenarioDrafts) }
              : {}),
          });
      await onSaved(saved, editing ? '已保存' : '已创建');
    } catch (caught) {
      setLocalError(personaErrorMessage(caught));
    } finally {
      onBusyChange(false);
    }
  };

  const scenarios: PersonaScenario[] =
    persona?.scenarios ?? current?.scenarios ?? [];

  return (
    <div className="domain-body" data-testid="persona-form">
      <div className="page__head">
        <div>
          <button type="button" className="back-link" onClick={onCancel}>
            <ArrowLeftIcon /> 返回人格列表
          </button>
          <h2 className="page__title">{editing ? '编辑人格' : '新建人设'}</h2>
          <p className="page__subtitle">
            {editing
              ? `${personaTitle(persona)} · persona_id ${personaId(persona)} · 当前修订 #${persona.revision}。保存会新建一条修订，后续轮次立即生效。`
              : '先起个名字；其余字段都可以留空，留空就是不注入。'}
          </p>
        </div>
      </div>

      <form className="panel" onSubmit={(event) => void submit(event)}>
        <div className="panel__head">
          <h2>设定</h2>
          <span className="panel__count">服务端权威</span>
        </div>

        <label className="field">
          <span className="field__label">名称（用于区分）</span>
          <input
            className="field__input"
            value={name}
            maxLength={64}
            placeholder="例如 hexia"
            onChange={(event) => setName(event.target.value)}
            data-testid="persona-field-name"
          />
        </label>

        <label className="field">
          <span className="field__label">显示名</span>
          <input
            className="field__input"
            value={displayName}
            maxLength={64}
            placeholder="她自称的名字；留空则用名称"
            onChange={(event) => setDisplayName(event.target.value)}
            data-testid="persona-field-display-name"
          />
        </label>

        <label className="field">
          <span className="field__label">系统提示（人格设定）</span>
          <textarea
            className="field__input field__textarea"
            rows={8}
            maxLength={8000}
            value={systemPrompt}
            placeholder="不填则不注入人设：模型只会收到功能提示词，不会扮演任何角色。"
            onChange={(event) => setSystemPrompt(event.target.value)}
            data-testid="persona-field-system-prompt"
          />
        </label>

        <label className="field">
          <span className="field__label">性格特征（JSON 对象）</span>
          <textarea
            className="field__input field__textarea field__mono"
            rows={5}
            placeholder={'不填则没有性格特征，例如 {"tone": "温和", "pace": "慢"}'}
            value={traits.text}
            onChange={(event) =>
              setTraits({ kind: 'object', text: event.target.value })
            }
            data-testid="persona-field-traits"
          />
        </label>

        <label className="field">
          <span className="field__label">叙事边界（每行一条）</span>
          <textarea
            className="field__input field__textarea"
            rows={4}
            value={boundariesText}
            placeholder="不填则不额外限制，例如：不替用户做决定"
            onChange={(event) => setBoundariesText(event.target.value)}
            data-testid="persona-field-boundaries"
          />
        </label>

        {scenarios.length > 0 ? (
          <ScenarioEditor
            scenarios={scenarios}
            drafts={scenarioDrafts}
            onChange={(key, value) =>
              setScenarioDrafts((prev) => ({ ...prev, [key]: value }))
            }
          />
        ) : null}

        {shownError ? (
          <div className="banner banner--error" role="alert">
            {shownError}
          </div>
        ) : null}

        <div className="panel__actions">
          <button type="submit" className="button button--primary" disabled={busy}>
            {busy ? '保存中…' : editing ? '保存设定' : '创建'}
          </button>
          <button type="button" className="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <span className="panel__hint">留空的字段不会写入，也不会用默认值补上</span>
        </div>
      </form>

      {editing && events.length > 0 ? <PersonaTimeline events={events} /> : null}
    </div>
  );
}

/* ------------------------------------------------------------ 角色时间线 */

function PersonaTimeline({ events }: { events: TimelineEvent[] }) {
  return (
    <section className="panel">
      <div className="panel__head">
        <h2>角色时间线</h2>
        <span className="panel__count">最近 {events.length} 条</span>
      </div>
      <p className="panel__note">
        时间线是服务端权威事件序列（含来源与可见范围），不是浏览器拼出来的记录。
      </p>
      <ul className="timeline">
        {events.map((event) => (
          <li className="timeline__item" key={event.id}>
            <div className="timeline__main">
              <span className="timeline__type">{event.type}</span>
              <span className="timeline__summary">{event.summary}</span>
            </div>
            <div className="timeline__meta">
              <span>rev {event.revision}</span>
              <span>{event.origin}</span>
              <span>{event.visibility}</span>
              <span>{formatSessionTime(event.occurred_at)}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
