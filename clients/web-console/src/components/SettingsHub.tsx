/**
 * 设置中心：**只放程序与运维**（用户拍板，DESIGN §2）。
 *
 * 判据是从 AstrBot 抄来的那条：**有领域对象、有列表-详情生命周期的算内容页；
 * 纯实例配置才算设置**。所以「改她要不要开口」不在这里，「改密钥/改凭据/
 * 查版本」才在这里。三组沿用 AstrBot 的命名：安全与隐私 / 模型与密钥 / 维护与关于。
 *
 * 三组各自渲染**固定的现有页面**，不新建空壳（Ling 没有语言/主题/CORS/限流
 * 这些配置项，新建就是白板页）。只有真有配置项时才加组。
 *
 * 第四组「接入与连接」是后加的，判据与上面一致：**她从哪条线收发消息**（OneBot /
 * QQ 官方机器人）是实例运维，不是她那一边的内容。这一页**只读**——渠道配置与密钥
 * 只存在于宿主部署目录的 `.env`，变成网页可编辑要先回答"存哪、怎么加密、改完怎么
 * 重启"，那是另一轮；现在缺的是"看得见"。
 *
 * 与后端 `GET /v1/settings/taxonomy` 的关系：它仍是那 9 类的唯一权威，
 * 这里只对**留在设置里的那两类**（模型与生成 ③、权限与隐私 ⑦）取用它的
 * `label`/`verb`，免得后端改名而这里还显示旧名。**taxonomy 挂了不影响导航**——
 * 卡片用的是本文件的文案，加载失败只是少了那条"后端文案"，不会让整页变白板
 * （旧实现正是这样：taxonomy 一失败，设置中心一个入口都不剩）。
 *
 * 已迁出的三类（记忆与学习 ④、对话与主动性 ⑤、生活与活动 ⑧）**不在设置里**，
 * 它们在各自的领域页（记忆页 / 人设页 / 生活页）。底部那条灰字只是指路，
 * 不是入口——把入口留在这里就等于没迁。
 *
 * 「多用户与账号」同理已经离开设置：它成了一级导航「关系」（`/relationships`）。
 * 它不在底部那行灰字里指路，因为左边导航本来就一直看得见，再指一次是多话。
 *
 * ## 例外：「你的本地时间」为什么在这里（而不是某个领域页）
 *
 * 时区**不是"她的行为调参"**，是**你**在哪儿——它没有领域对象、也没有
 * 列表-详情生命周期，放进记忆/人设/生活页的任何一页都是错的门牌。按
 * `docs/SETTINGS-TAXONOMY.md` 的分类轴，它同时改变 ⑤（何时开口：勿扰时段、
 * 每日限额）与 ⑧（"今天"的边界：日程）两个决策，没有哪一类独占它。
 *
 * 但它也不能进配置修订：修订跟着发布/回滚走，回滚一次旧配置不该把"我在东八区"
 * 一起回滚掉。所以后端把它存进 `user_preferences`（按 subject 一行），
 * 走 `GET/PUT /v1/settings/preferences`——**与草稿/发布无关**，保存即生效。
 * 这也是它在这一页上唯一一个"保存不需要点发布"的东西，界面上直接写清楚。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  TaxonomyResponse,
  TimezoneSource,
  UserPreferencesResponse,
  VideoMode,
} from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';
import '../settings.css';
// 时区小节的窄屏触控尺寸规则写在模型页那份 CSS 里（`.ms-timezone` 作用域），
// 显式 import 让依赖可见：否则哪天模型页被拆成懒加载，这里的 44px 会悄悄失效。
import '../model-settings.css';

interface SettingsHubProps {
  onBack: () => void;
  /** 直接给路径，App 不需要再维护一份 key→路径 的映射。 */
  onOpen: (path: string) => void;
}

interface SettingsEntry {
  /** 稳定标识（也是 `data-testid` 的来源之一）。 */
  key: string;
  label: string;
  verb: string;
  path: string;
  /** 对应 taxonomy 的类 key；有值时才用后端的 label/verb 覆盖。 */
  taxonomyKey?: string;
  testid?: string;
}

/**
 * 设置中心的一组入口。
 *
 * 分组不只为好看：整页原来是一条长长的单列，六段之间没有任何层级，
 * 人只能一路往下读到底才知道"维护与关于"在哪。分组标题就是路上那几个
 * 路标——而且**顺序本身是信息**（常用的在上，低频的在最下）。
 */
export interface SettingsGroup {
  key: string;
  title: string;
  /** 一句话说清这一组装的是什么（≤ 30 字，别写成段落）。 */
  note: string;
  entries: readonly SettingsEntry[];
}

/**
 * 「关于她」这一组的两格**不是跳转入口**，而是就地可改的偏好。
 *
 * 把它们和跳转卡片放进同一个网格、长得也一样，是因为对用户来说它们是同一类
 * 东西——"设置里的一个开关/一项值"。但交互完全不同（就地改 vs 点进去），
 * 所以卡片上明确写「就地生效」，并且不给箭头。
 */
export type SettingsTile = 'timezone' | 'video';

export interface SettingsCardSpec {
  key: string;
  label: string;
  /** 卡片上那句话：说清"点进去能改什么"，不写长段落。 */
  verb: string;
  path: string;
  testid: string;
  /** 对应 taxonomy 的类 key（有值时用后端 label/verb 覆盖）。 */
  taxonomyKey?: string;
  /** 有值表示这张卡是就地可改的偏好块（不跳转）。 */
  tile?: SettingsTile;
  /** 右上角小标记，例：当前值 / 状态。 */
  badge?: string;
}

export interface SettingsSection {
  key: string;
  title: string;
  note: string;
  /** 组内卡片：全部等宽，2 列（≤760px 单列）。 */
  cards: readonly SettingsCardSpec[];
}

/**
 * 三组入口 + 一组就地偏好。顺序即首屏优先级：
 *
 * 1. **关于她**：时区与"怎么看视频"——改完不用发布，一天要用好几次；
 * 2. **安全**：权限与隐私、登录名与密码——偶尔要改，但改了立刻生效；
 * 3. **连接与运行**：模型与生成、接入器、维护与关于——装好之后很少动，
 *    出问题时才来，所以压在最下面。
 */
function SETTINGS_GROUPS_CARDS(): readonly SettingsSection[] {
  const group = (key: string) => SETTINGS_GROUPS.find((item) => item.key === key);
  const cardsOf = (key: string): SettingsCardSpec[] =>
    (group(key)?.entries ?? []).map((entry) => ({
      key: entry.key,
      label: entry.label,
      verb: entry.verb,
      path: entry.path,
      testid: entry.testid ?? `open-${entry.key}`,
      ...(entry.taxonomyKey ? { taxonomyKey: entry.taxonomyKey } : {}),
    }));

  const runCards = [
    ...cardsOf('model'),
    ...cardsOf('connectors'),
    ...cardsOf('maintenance'),
  ];

  return [
    {
      key: 'about-her',
      title: '关于她',
      note: '决定她怎么过日子、怎么看你发来的东西。改完就生效，不用发布。',
      cards: [
        {
          key: 'timezone',
          label: '你在哪个时区',
          verb: '「今天」的边界、免打扰时段、每日限额都按它算。',
          path: '',
          testid: 'settings-card-timezone',
          tile: 'timezone',
        },
        {
          key: 'video',
          label: '她怎么看视频',
          verb: '抽帧还是原生输入；只有她主动标记要看时才读。',
          path: '',
          testid: 'settings-card-video',
          tile: 'video',
        },
      ],
    },
    {
      key: 'security',
      title: '安全',
      note: '谁能看到什么，以及你自己的登录凭据。',
      cards: cardsOf('security'),
    },
    {
      key: 'runtime',
      title: '连接与运行',
      note: '实例能力与运维：模型、从哪条线收发消息、版本与更新。',
      cards: runCards,
    },
  ];
}

let cache: readonly SettingsSection[] | null = null;

/**
 * 渲染时用的分组。
 *
 * 惰性求值不是洁癖：`SETTINGS_GROUPS` 在本文件**下面**才声明，模块顶层直接
 * 调 `SETTINGS_GROUPS_CARDS()` 会撞上 TDZ（`Cannot access before
 * initialization`），整页白板。缓存也只算一次——分组是常量，不随渲染变。
 */
export function settingsSections(): readonly SettingsSection[] {
  if (cache === null) cache = SETTINGS_GROUPS_CARDS();
  return cache;
}

/**
 * 四组 / 五个页面（DESIGN §2 的表，后加「接入与连接」）。
 *
 * 这里是**数据的唯一来源**，`settingsSections()` 只重排它、不改写它：
 * 两份入口表迟早走散，而"设置页少了一个入口"是那种没人报但会让人点不开的错。
 *
 * 条目数比原先少一个：「多用户与账号」已搬成一级导航「关系」（`/relationships`），
 * 设置里不再留入口。留一条就是"迁出"没做干净——旧路径会经由
 * `LEGACY_REDIRECTS` 精确跳到新地址，这里再放一个按钮只会让人以为它还在设置里。
 */
export const SETTINGS_GROUPS: readonly SettingsGroup[] = [
  {
    key: 'security',
    title: '安全与隐私',
    note: '主体权利与凭据。同意可以撤回、密码可以改——都在一跳之内。',
    entries: [
      {
        key: 'privacy',
        taxonomyKey: 'privacy',
        label: '权限与隐私',
        verb: '谁能看到什么：同意、撤回、有效期。撤回后新数据不再采集。',
        path: '/settings/privacy',
        testid: 'open-privacy',
      },
      {
        key: 'account',
        label: '登录名与密码',
        verb: '改你自己的登录凭据；改完其它设备上的登录会失效。不改她的人设。',
        path: '/settings/account',
        testid: 'open-account',
      },
    ],
  },
  {
    key: 'model',
    title: '模型与密钥',
    note: '实例能力与凭据：provider、密钥、职责绑定、发布与回滚。',
    entries: [
      {
        key: 'model',
        taxonomyKey: 'model',
        label: '模型与生成',
        verb: '配置 provider 与密钥，绑定对话/记忆/嵌入各自的职责；改动走草稿再发布，可回滚。',
        path: '/settings/model',
        testid: 'open-model',
      },
    ],
  },
  {
    key: 'connectors',
    title: '接入与连接',
    note: '她从哪条线收发消息：OneBot（NapCat 等）与 QQ 官方机器人。配置来自宿主的 .env，本轮只读。',
    entries: [
      {
        key: 'connectors',
        label: '接入器',
        verb: '看每个渠道的凭据齐不齐、现在连没连上、缺的那一项该在 .env 里改哪个变量。',
        path: '/settings/connectors',
        testid: 'open-connectors',
      },
    ],
  },
  {
    key: 'maintenance',
    title: '维护与关于',
    note: '运维动作：看版本、查更新、装更新，以及只读的运行状态。',
    entries: [
      {
        key: 'about',
        label: '关于与版本',
        verb: '查看当前版本与构建提交；手动检查 GitHub 上有没有新 Release，并按门禁安装。',
        path: '/settings/about',
        testid: 'open-about',
      },
    ],
  },
];

/** 迁出设置的三类：只指路，不做成入口。 */
const MOVED_HINT = [
  { label: '记忆与学习', path: '/memory?tab=settings' },
  { label: '对话与主动性', path: '/persona?tab=dialogue' },
  { label: '生活节奏', path: '/life?tab=rhythm' },
];

/* ---------------------------------------------------------- 你的本地时间 */

export interface TimezoneFormProps {
  /** 输入框里的草稿值。空串 = "没设过"（跟着环境变量/默认值走）。 */
  draft: string;
  /** 生效值（服务端解析后的结果）。 */
  effective: string;
  source: TimezoneSource;
  /** `LING_USER_TIMEZONE` 的值。null = 没配。 */
  env: string | null;
  /** 服务端给的常用时区建议。**不是白名单**：任何 IANA 名都能填。 */
  common: string[];
  saving: boolean;
  dirty: boolean;
  message: { kind: 'ok' | 'error'; text: string; detail?: string } | null;
  onChange: (value: string) => void;
  onSave: () => void;
  onReset: () => void;
}

/**
 * 时区表单。**纯展示**：状态在 `TimezoneSection` 里。
 *
 * 为什么单独导出：静态渲染测试要断言"默认值 / 环境变量 / 你自己设的"这三句话
 * 互不串——混成一句"当前 Asia/Shanghai"，用户会以为自己设过。
 *
 * 为什么用 `list` + `datalist` 而不是下拉 + "自定义"：
 * 常用项只是**建议**，"我在 America/Argentina/Ushuaia"这种情况必须能填。
 * 下拉 + 手填两件控件改同一个值，会出现"下拉显示 A、输入框写着 B"。
 */
export function TimezoneForm({
  draft,
  effective,
  source,
  env,
  common,
  saving,
  dirty,
  message,
  onChange,
  onSave,
  onReset,
}: TimezoneFormProps) {
  // 文案尽量短：窄屏一行放不下时会折行，长句容易在末行剩 1–3 个字
  // （验收 O3：默认值那句末行只剩「为准。」）。短句 + `text-wrap: pretty`
  // （见 model-settings.css）一起用。
  const sourceText =
    source === 'preference'
      ? `你自己设的：${effective}。存数据库里，重启和回滚都不变。`
      : source === 'env'
        ? `来自环境变量 LING_USER_TIMEZONE=${env ?? ''}。填一个就以你为准。`
        : `没有设置过，用默认值 ${effective}。填一次就以你为准。`;

  return (
    <section className="settings-tile settings-tile--timezone" data-settings-group="local-time">
      {/*
        这里**不再重复**卡片标题与那句说明：外层卡片的标题就是「你在哪个时区」，
        说明也已经在卡片上。块内只留"当前生效的是哪个值、从哪来"——
        一格里写两遍同一件事，正是这一轮要收掉的噪音。
      */}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSave();
        }}
      >
        {/* form-grid 是自适应网格（auto-fit minmax(170px,1fr)）：375px 下自动
            变一列，不会把输入框挤出屏幕。 */}
        <div className="form-grid">
          <label className="field">
            <span className="field__label">时区（IANA 名，例：Asia/Shanghai）</span>
            <input
              className="field__input"
              value={draft}
              list="settings-timezone-options"
              placeholder={effective}
              autoComplete="off"
              spellCheck={false}
              data-testid="settings-timezone-input"
              onChange={(event) => onChange(event.target.value)}
            />
            {/* 常用项作建议：原生 datalist 在窄屏是系统自己的选择器，
                不会把页面撑宽，也不需要额外的 CSS。 */}
            <datalist id="settings-timezone-options">
              {common.map((tz) => (
                <option key={tz} value={tz} />
              ))}
            </datalist>
            <span className="field__label">{sourceText}</span>
          </label>
        </div>

        <div className="inline-form" role="status">
          <button
            type="submit"
            className="button button--primary button--small"
            disabled={saving}
            data-testid="settings-timezone-save"
          >
            {saving ? '保存中…' : '保存时区'}
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={saving}
            data-testid="settings-timezone-reset"
            onClick={onReset}
          >
            清空（回退默认）
          </button>
          <span className="settings-tile__note">
            {dirty ? '有未保存的改动' : '保存即生效，不走草稿/发布'}
          </span>
        </div>
      </form>

      {message ? (
        <div className={`banner banner--${message.kind === 'ok' ? 'ok' : 'error'}`} role="status">
          <div>
            <strong>{message.text}</strong>
            {message.detail ? <div className="banner__detail">{message.detail}</div> : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}

/**
 * 读取 / 保存用户时区。
 *
 * 取不到就只显示一条错误，**不挡住下面的入口**：时区读失败不该让整个设置页
 * 变成白板（与 taxonomy 同一条理由）。
 */
export function TimezoneSection() {
  const [prefs, setPrefs] = useState<UserPreferencesResponse | null>(null);
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [message, setMessage] = useState<TimezoneFormProps['message']>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.userPreferences();
      setPrefs(next);
      // 输入框显示**库里存的原值**（null → 空串）。把生效值塞进去会让
      // "默认值"看起来像"你自己设的"。
      setDraft(next.stored ?? '');
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async (value: string) => {
    setSaving(true);
    setMessage(null);
    setError(null);
    try {
      const next = await api.setTimezone(value);
      setPrefs(next);
      setDraft(next.stored ?? '');
      setMessage({
        kind: 'ok',
        text:
          next.source === 'preference'
            ? `已保存：现在按 ${next.timezone} 计算「今天」和免打扰时段`
            : `已清空：回退到 ${next.source === 'env' ? '环境变量' : '默认值'} ${next.timezone}`,
      });
    } catch (err) {
      const explained = explainError(err);
      setMessage({
        kind: 'error',
        text: `保存失败：${explained.reason}`,
        ...(explained.detail ?? explained.hint
          ? { detail: explained.detail ?? explained.hint ?? undefined }
          : {}),
      });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="settings-tile settings-tile--timezone" data-settings-group="local-time">
        <div className="page__loading">
          <span className="spinner" aria-hidden="true" /> 正在读取时区…
        </div>
      </div>
    );
  }

  if (error || !prefs) {
    return (
      <section className="settings-tile settings-tile--timezone" data-settings-group="local-time">
        <div className="banner banner--error" role="status">
          <AlertIcon />
          <div>
            <strong>没能读取你的时区设置</strong>
            <div className="banner__detail">
              {error?.reason ?? '接口没有返回内容'}。下面的入口仍然可用。
            </div>
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      </section>
    );
  }

  return (
    <TimezoneForm
      draft={draft}
      effective={prefs.timezone}
      source={prefs.source}
      env={prefs.env}
      common={prefs.common}
      saving={saving}
      dirty={draft.trim() !== (prefs.stored ?? '')}
      message={message ?? null}
      onChange={(value) => {
        setDraft(value);
        setMessage(null);
      }}
      onSave={() => void submit(draft.trim())}
      onReset={() => void submit('')}
    />
  );
}

/* ------------------------------------------------------------ 媒体与视频 */

/**
 * 「怎么看视频」：抽帧 / 原生视频输入。
 *
 * ## 为什么放这里，不放模型页
 *
 * 模型页管的是"用哪个模型"。这一项管的是**怎么把视频交给模型**：抽帧是
 * 任何供应商都能用的通用做法，原生视频输入只有部分供应商支持。它跟着
 * 用户偏好走（`user_preferences`），换模型不该把它重置掉。
 *
 * ## 为什么要把"只在你想看时才读"写在界面上
 *
 * 视频的 token 成本远高于图片，用户最担心的是"它会不会偷偷把群里的视频
 * 全看一遍"。这句话是承诺，也是事实（后台描述队列**跳过**视频）。
 */
export function VideoModeSection() {
  const [prefs, setPrefs] = useState<UserPreferencesResponse | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPrefs(await api.userPreferences());
      setError(null);
    } catch (err) {
      setError(explainError(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const choose = async (mode: VideoMode) => {
    if (!prefs || prefs.video_mode === mode) return;
    setSaving(true);
    setMessage(null);
    try {
      const next = await api.setVideoMode(mode);
      setPrefs(next);
      setMessage(mode === 'native' ? '已保存：直接把视频交给模型（不支持时会自动退回抽帧）' : '已保存：按固定间隔抽帧');
    } catch (err) {
      setError(explainError(err));
    } finally {
      setSaving(false);
    }
  };

  if (error && !prefs) {
    return (
      <section className="settings-tile settings-tile--video" data-settings-group="video-mode">
        <div className="banner banner--error" role="status">
          <AlertIcon />
          <div>
            <strong>没能读取视频读取方式</strong>
            <div className="banner__detail">{error.reason}</div>
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      </section>
    );
  }

  const current = prefs?.video_mode ?? 'frames';
  return (
    <section className="settings-tile settings-tile--video" data-settings-group="video-mode">
      {/*
        块内**不再重复**卡片标题（「她怎么看视频」就是卡片标题）：原来这里还有
        一个 `<legend>` 写着同一句话，一格之内说两遍。说明也只留一句——
        "不写标记就不读"由卡片那句"只有她主动标记要看时才读"承担。
      */}
      <fieldset className="settings-tile__body" disabled={saving}>
        <legend className="settings-tile__legend">交给模型的方式</legend>
        <label>
          <input
            type="radio"
            name="video-mode"
            value="frames"
            checked={current === 'frames'}
            onChange={() => void choose('frames')}
            data-testid="settings-video-mode-frames"
          />
          抽帧（默认）：任何供应商都能用；按固定间隔抽几帧交给视频模型看。
        </label>
        <label>
          <input
            type="radio"
            name="video-mode"
            value="native"
            checked={current === 'native'}
            onChange={() => void choose('native')}
            data-testid="settings-video-mode-native"
          />
          原生视频输入：直接把视频交给模型；只有部分供应商支持
          （Gemini / Qwen-VL / 豆包 / 智谱 / Kimi），不支持时会自动退回抽帧并记录原因。
        </label>
        <p className="settings-tile__note">
          标记形如 <code>&lt;&lt;看视频 m…&gt;&gt;</code>；不写标记就不读。
        </p>
        {message ? <p className="settings-tile__note" role="status">{message}</p> : null}
      </fieldset>
    </section>
  );
}

/**
 * 一张设置卡片。
 *
 * 三件东西，一个不多：**标题 + 一句话 + （跳转卡片）箭头**。
 * 原来每张卡是一个符号 + 标题 + 两行长说明，五张卡高度不一、还互相错位；
 * 现在说明只留一句，长解释留给点进去之后的页面。
 */
export function SettingsCard({
  spec,
  onOpen,
  decorate,
  tile,
}: {
  spec: SettingsCardSpec;
  onOpen: (path: string) => void;
  decorate: (spec: SettingsCardSpec) => SettingsCardSpec;
  /** 就地可改的偏好块（时区/视频）：整块嵌进卡片，不再单开一节。 */
  tile?: React.ReactNode;
}) {
  const card = decorate(spec);
  const isTile = Boolean(spec.tile);
  const inner = (
    <>
      <span className="settings-card__head">
        <span className="settings-card__label">{card.label}</span>
        {card.badge ? <span className="settings-card__badge">{card.badge}</span> : null}
        {isTile ? null : (
          // 箭头是"这张卡会跳走"的唯一提示；就地可改的块没有它。
          <span className="settings-card__arrow" aria-hidden="true">
            →
          </span>
        )}
      </span>
      <span className="settings-card__verb">{card.verb}</span>
      {tile ? <span className="settings-card__tile">{tile}</span> : null}
    </>
  );

  if (isTile) {
    // 就地可改的块不能做成 `<button>`：里面还有输入框与提交按钮，
    // 按钮套按钮既不是合法 HTML，也会让键盘焦点打架。
    return (
      <section
        className="settings-card settings-card--tile"
        data-testid={card.testid}
        data-settings-card={card.key}
        aria-label={card.label}
      >
        {inner}
      </section>
    );
  }

  return (
    <button
      type="button"
      className="settings-card"
      data-testid={card.testid}
      data-settings-card={card.key}
      data-taxonomy-key={card.taxonomyKey ?? undefined}
      onClick={() => onOpen(card.path)}
    >
      {inner}
    </button>
  );
}

export function SettingsHub({ onBack, onOpen }: SettingsHubProps) {
  const [taxonomy, setTaxonomy] = useState<TaxonomyResponse | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setTaxonomy(await api.taxonomy());
    } catch (err) {
      // 只记下来、不挡路：入口用的是本文件的文案，taxonomy 只是"后端怎么说"。
      setError(explainError(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** 有 taxonomy 就用它的 label/verb；没有就用本文件的。 */
  const decorate = (spec: SettingsCardSpec): SettingsCardSpec => {
    if (!spec.taxonomyKey) return spec;
    const category = taxonomy?.categories.find((item) => item.key === spec.taxonomyKey);
    if (!category) return spec;
    return { ...spec, label: category.label, verb: category.verb };
  };

  const sections = settingsSections();

  return (
    <section className="page settings-page">
      <button type="button" className="back-link settings-hub__back" onClick={onBack}>
        <ArrowLeftIcon /> 返回聊天
      </button>

      <header className="settings-page__head">
        <h1 className="page__title">设置</h1>
        <p className="page__hint">
          这里分三组：<strong>你</strong>的时区与偏好、你的权限与凭据、以及实例的模型与版本。
          改她的行为（记什么、说不说话、怎么过日子）跟着各自的页面走，不在这里。
        </p>
      </header>

      {error ? (
        <div className="banner banner--error" role="status">
          <AlertIcon />
          <div>
            <strong>没能读取后端对设置类别的说明</strong>
            <div className="banner__detail">
              {error.reason}
              。下面的入口仍然可用，只是少了后端维护的那句说明。
            </div>
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      <div className="settings-sections">
        {sections.map((section) => (
          <section key={section.key} className="settings-group" data-settings-group={section.key}>
            <div className="settings-group__head">
              <h2 className="settings-group__title">{section.title}</h2>
              <p className="settings-group__note">{section.note}</p>
            </div>
            {/*
              等宽 2 列网格：卡片高度由 `min-height` 对齐、内容顶对齐，
              一张卡文案长一点不会把它那一行整体撑高（原来单列时没有这个问题，
              但一改成多列就会出现"参差"）。≤760px 单列，见 settings.css。
            */}
            <div className="settings-grid">
              {section.cards.map((spec) => (
                <SettingsCard
                  key={spec.key}
                  spec={spec}
                  onOpen={onOpen}
                  decorate={decorate}
                  tile={
                    spec.tile === 'timezone' ? (
                      // `ms-timezone` 只用于窄屏触控尺寸（model-settings.css），
                      // 不引入模型页的颜色/排版变量。
                      <span className="ms-timezone">
                        <TimezoneSection />
                      </span>
                    ) : spec.tile === 'video' ? (
                      <VideoModeSection />
                    ) : undefined
                  }
                />
              ))}
            </div>
          </section>
        ))}
      </div>

      {/* 迁出去的三类：只留一行指路。做成入口就等于没迁（用户的原话是"不要再放到设置里"）。 */}
      <p className="page__hint">
        记忆与学习、对话与主动性、生活节奏不在设置里，它们是行为调参，跟着各自的页面走：
        {MOVED_HINT.map((item, index) => (
          <span key={item.path}>
            {index > 0 ? '、' : ''}
            <button type="button" className="link-button" onClick={() => onOpen(item.path)}>
              {item.label}
            </button>
          </span>
        ))}
        。
      </p>
    </section>
  );
}
