/**
 * 接入器的**实例**：左列表、右详情。
 *
 * ## 为什么单独一个文件
 *
 * `ConnectorsPage.tsx` 已经有 1400 多行，而这一块是**另一种**数据形状：
 * 它的状态键不是 `scope`，是 `(scope, instance_id)` 这个二元组。把实例选择器
 * 硬塞进 `forms[scope]` 里，就等于逼着后面每个人记住"这个 scope 的草案属于
 * 哪个实例"——那正是下面第 1 条约要防的 bug。
 *
 * ## 四条不能违反的规矩
 *
 * 1. **切换实例 = 换一份草案，不是复用同一份**。草案按
 *    `${scope}::${instance_id}` 分区（`instanceFormKey`）。如果只留一份草案，
 *    从 A 切到 B 时用户会看到 A 的值、然后点保存把它存到 B 上——界面一切正常，
 *    数据却写错了地方，这是这一块最容易出的 bug。见 `instancesOf` 与
 *    `ConnectorInstancePanel` 里的 `key={...}`。
 * 2. **保存必须带 `instance_id`**。`PUT /v1/settings/connectors/{scope}?instance_id=`
 *    是按实例落库的唯一途径；不带就是"保存到默认实例"（后端会落到
 *    `'default'`，甚至补建它）。所以保存按钮不接受"当前没选中实例"的状态——
 *    没有选中时按钮本来就是禁用的。
 * 3. **默认实例不可删除**。用后端给的 `deletable`，不用前端自己比
 *    `instance_id === 'default'`：判定只有一份。为 false 时按钮 `disabled`，
 *    旁边**写明原因**，而不是让用户点了收 409。
 * 4. **不把默认实例的值冒充成别的实例的值**。`GET /v1/settings/connectors`
 *    目前**没有实例维度**（`_connector_field_views` 不传 `instance_id`，
 *    `_activation_views` 直接按 `(scope, 'default')` 取 HEAD）。所以右侧的字段
 *    值只对默认实例为真；选中别的实例时必须把这件事画出来，不能让用户以为
 *    屏幕上那串已经保存的值属于 B。见 `ConnectorInstancePanel` 里的 `viewMatches`。
 *
 * ## 空列表不是错误
 *
 * 全新部署时 `connector_instances` 里可能一行都没有（默认实例只在"不带
 * instance_id 存配置"时才被补建）。这时画的是"新建第一个实例"的引导，
 * 不是"加载失败"，也不是一片空白。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type {
  ConnectorField,
  ConnectorInstance,
  ConnectorView,
} from '../types';
import {
  buildConnectorChange,
  draftFromFields,
  type FieldDraft,
  type SaveNotice,
} from './ConnectorsPage';

/** 实现在这里只用作类型标注，避免为了一个枚举值去拉整个组件树。 */
type Scope = string;

/** 默认实例的身份。与后端 `DEFAULT_INSTANCE_ID` 同值。 */
export const DEFAULT_INSTANCE_ID = 'default';

/* ------------------------------------------------------------------ 纯函数 */

/**
 * 一个实例的草案分区键。
 *
 * **必须同时含 scope 与 instance_id**：`instance_id` 只在 scope 内唯一
 * （主键是 `(scope, instance_id)`），只用 instance_id 会让 onebot 的
 * "default" 与 qqbot 的 "default" 共用一份草案。
 *
 * 分隔符用 `::`：后端派生出的 id 只含 `[a-z0-9._-]`，不会出现冒号。
 */
export function instanceFormKey(scope: Scope, instanceId: string): string {
  return `${scope}::${instanceId}`;
}

/** 页面上一个实例的编辑状态（草案 + 待清除的键 + 提交状态）。 */
export interface InstanceFormState {
  draft: Record<string, FieldDraft>;
  clearKeys: string[];
  saving: boolean;
  notice: SaveNotice | null;
  fieldError: string | null;
}

/**
 * 空状态。**每次进入一个新的实例键都要用新的对象**，不能共用一份常量：
 * 那份常量的 `draft` 会被后续编辑原地"继承"，第二个实例一开始就带着前一个
 * 的值——这与"切实例串草稿"是同一个 bug 的两种形态。
 */
export function emptyInstanceForm(fields: ConnectorField[]): InstanceFormState {
  return {
    draft: draftFromFields(fields),
    clearKeys: [],
    saving: false,
    notice: null,
    fieldError: null,
  };
}

/**
 * 从当前草案表里取**选中实例**的那一份；没有就按 `fields` 现造一份。
 *
 * 这是"切实例不串草稿"的落点：返回值只可能来自按键分区的那张表，或者
 * 刚由 `fields` 造出来的新对象。**不存在"上次选中的草案"这个变量**——
 * 只要它存在，就一定会有人在某个分支里忘了更新它。
 */
export function instanceFormOf(
  forms: Record<string, InstanceFormState>,
  scope: Scope,
  instanceId: string,
  fields: ConnectorField[],
): InstanceFormState {
  return forms[instanceFormKey(scope, instanceId)] ?? emptyInstanceForm(fields);
}

/** 实例的启停徽标文案（与"连接状态"不是一回事：停用 ≠ 没连上）。 */
export function instanceStatusLabel(instance: ConnectorInstance): string {
  if (instance.deleted_at !== null) return '已删除';
  return instance.enabled ? '已启用' : '已停用';
}

/** 删除按钮为什么不能点。返回 null 表示可以删。 */
export function deleteBlockReason(instance: ConnectorInstance | null): string | null {
  if (!instance) return '还没有选中实例';
  if (!instance.deletable) {
    return '默认实例是存量配置的属主，不能删除（删掉会让历史修订链失去属主）';
  }
  if (instance.deleted_at !== null) return '这个实例已经删除了';
  return null;
}

/** 选中实例的标题：显示名 + 身份。两者看起来很像，混用会存错地方。 */
export function instanceTitle(instance: ConnectorInstance): string {
  return `${instance.display_name}（${instance.instance_id}）`;
}

/* ------------------------------------------------------------------ 数据 */

type ListState =
  | { status: 'loading' }
  | { status: 'ready'; items: ConnectorInstance[] }
  | { status: 'error'; error: ExplainedError };

/**
 * 读一个 scope 的实例列表，并**在选中态失效时**给出建议的选中值。
 *
 * 失效有两种：当前选中的被删了（或刷新后不再返回），以及第一次加载。
 * 都不自动写回 `state`——由调用方决定（组件里用 `useEffect` 落地），
 * 这样这个函数仍然可以单独测。
 */
export function nextSelectedId(
  items: ConnectorInstance[],
  current: string | null,
): string | null {
  if (current !== null && items.some((item) => item.instance_id === current)) {
    return current;
  }
  // 后端已把默认实例排最前；列表非空时选第一个 = 选默认实例。
  return items.length > 0 ? (items[0]?.instance_id ?? null) : null;
}

/* ------------------------------------------------------------------ 列表 */

/**
 * 从 `GET /v1/settings/connectors?instance_id=` 的响应里取出这个 scope 的连接器视图。
 *
 * 响应形状是 ``{schema_version, as_of, can_edit, instance_id, connectors[], access}``
 * ——**字段挂在 `connectors[i].fields` 上，顶层没有 `fields`**。
 *
 * 为什么单独抽出来：从前这里是 ``body as unknown as ConnectorView``，把整个
 * 响应体当成连接器视图。类型断言让 tsc 闭嘴，但 ``view.fields`` 恒为
 * ``undefined``，于是 ``Array.isArray(view.fields)`` 恒假——按实例读回来的视图
 * **一次都没被采用过**，用户永远看到"这些值不是这个实例的"。
 *
 * 返回 ``null`` 表示"这份响应里没有可用的连接器视图"，调用方据此退回"读不到"，
 * **不要**拿默认实例的值顶上。
 */
export function connectorViewOf(
  body: unknown,
  scope: string,
): ConnectorView | null {
  if (body === null || typeof body !== 'object') return null;
  const connectors = (body as { connectors?: unknown }).connectors;
  if (!Array.isArray(connectors)) return null;
  const found = connectors.find(
    (item): item is ConnectorView =>
      item !== null &&
      typeof item === 'object' &&
      (item as { id?: unknown }).id === scope &&
      Array.isArray((item as { fields?: unknown }).fields),
  );
  return found ?? null;
}

export interface InstanceListProps {
  items: ConnectorInstance[];
  selectedId: string | null;
  onSelect: (instanceId: string) => void;
  /** 列表上是否还有未保存的草案（切走会丢，需要提示）。 */
  dirtyIds?: ReadonlySet<string>;
  busy: boolean;
}

/**
 * 左侧实例列表。
 *
 * 每一项是 `<button>` 而不是 `<li onClick>`：键盘能 Tab 到、能用 Enter 选中，
 * 读屏软件会念"按钮"而不是"列表项"。选中态用 `aria-current`（与
 * `ProvidersPanel` 同一套约定），不靠 class 名。
 */
export function InstanceList({
  items,
  selectedId,
  onSelect,
  dirtyIds,
  busy,
}: InstanceListProps) {
  return (
    <ul className="connector-instance-list" role="list" data-testid="connector-instance-list">
      {items.map((instance) => {
        const active = instance.instance_id === selectedId;
        const dirty = dirtyIds?.has(instance.instance_id) === true;
        return (
          <li key={instance.instance_id}>
            <button
              type="button"
              className={`connector-instance-item${active ? ' connector-instance-item--active' : ''}`}
              data-testid={`connector-instance-${instance.instance_id}`}
              data-selected={active ? 'true' : 'false'}
              aria-current={active ? 'true' : undefined}
              aria-pressed={active}
              disabled={busy}
              onClick={() => onSelect(instance.instance_id)}
            >
              <span className="connector-instance-item__main">
                <span className="connector-instance-item__name">{instance.display_name}</span>
                <span
                  className="connector-instance-item__id"
                  data-testid={`connector-instance-${instance.instance_id}-id`}
                >
                  {instance.instance_id}
                </span>
              </span>
              <span className="connector-instance-item__tags">
                {!instance.deletable ? (
                  <span
                    className="connector-instance-tag connector-instance-tag--default"
                    data-testid={`connector-instance-${instance.instance_id}-default`}
                  >
                    默认
                  </span>
                ) : null}
                <span
                  className={`connector-instance-tag ${
                    instance.enabled
                      ? 'connector-instance-tag--on'
                      : 'connector-instance-tag--off'
                  }`}
                  data-testid={`connector-instance-${instance.instance_id}-enabled`}
                >
                  {instanceStatusLabel(instance)}
                </span>
                {dirty ? (
                  <span
                    className="connector-instance-tag connector-instance-tag--dirty"
                    data-testid={`connector-instance-${instance.instance_id}-dirty`}
                  >
                    未保存
                  </span>
                ) : null}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/* ------------------------------------------------------------------ 新建 */

export interface CreateInstanceFormProps {
  scope: Scope;
  busy: boolean;
  onCreated: (instance: ConnectorInstance) => void;
}

/**
 * 「新建实例」：只收一个显示名。
 *
 * **不需要**：`instance_id` 的输入框（由后端派生，建后不可变——让人手填一个
 * 之后改不了的身份只会制造事故）、`enabled` 开关（建完就能在右侧关掉）。
 * 重名会拿 409 `duplicate_display_name`，就地把那句话显示出来。
 */
export function CreateInstanceForm({ scope, busy, onCreated }: CreateInstanceFormProps) {
  const [name, setName] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = name.trim();
    if (trimmed === '' || pending || busy) return;
    setPending(true);
    setError(null);
    try {
      const result = await api.createConnectorInstance(scope, { display_name: trimmed });
      setName('');
      onCreated(result.item);
    } catch (err) {
      const explained = explainError(err);
      setError(explained.detail ?? explained.hint ?? explained.reason);
    } finally {
      setPending(false);
    }
  };

  return (
    <form
      className="connector-instance-create"
      data-testid="connector-instance-create"
      onSubmit={(event) => {
        // 不 preventDefault 会让整个设置页刷新，用户的其它未保存输入全丢。
        event.preventDefault();
        void submit();
      }}
    >
      <label className="connector-instance-create__label" htmlFor={`instance-name-${scope}`}>
        新实例的显示名
      </label>
      <input
        id={`instance-name-${scope}`}
        className="connector-input"
        data-testid="connector-instance-name"
        value={name}
        maxLength={64}
        placeholder="例如：主号 / 小号二号"
        disabled={pending || busy}
        onChange={(event) => setName(event.target.value)}
      />
      <button
        type="submit"
        className="button button--primary button--small"
        data-testid="connector-instance-create-submit"
        disabled={name.trim() === '' || pending || busy}
      >
        {pending ? '正在创建…' : '新建实例'}
      </button>
      {error ? (
        <p className="connector-instance-create__error" role="alert" data-testid="connector-instance-create-error">
          {error}
        </p>
      ) : null}
    </form>
  );
}

/* ------------------------------------------------------------------ 面板 */

export interface ConnectorInstancePanelProps {
  scope: Scope;
  /** 页面读到的**默认实例**诊断 + 字段视图（`GET /v1/settings/connectors`，不带实例）。 */
  connector: ConnectorView;
  /** `GET /v1/settings/connectors` 是 owner 才给编辑入口。 */
  canEdit: boolean;
  /**
   * 页面上别处在忙着（保存/恢复默认）。与实例自身的 saving 共同禁用按钮，
   * 避免两处写同时进行。
   */
  pageBusy?: boolean;
  /**
   * 选中实例变化时上报给页面。
   *
   * **为什么页面需要知道**：字段表单和它的草案状态在页面上（`ConnectorsPage`
   * 的 `forms`）。面板只报"现在选的是谁"，不替页面分区、也不传表单——草案的
   * 分区键必须由页面用 `instanceFormKey(scope, instanceId)` 拼，保证两处用的
   * 是**同一个**键函数。
   *
   * 面板**不靠它驱动**自己的选中态（内部 `selectedId` 仍是唯一来源）：这是
   * 一条单向的上报，不是受控属性。传空值也不会让面板跟着变空。
   */
  onSelectInstance?: (instanceId: string) => void;
  /**
   * 按实例读到的字段视图上报给页面。`fields === null` 表示"这一份不成立"
   * （读失败，或响应没回显实例、无法证明属于它），页面据此**别拿它当基准**。
   *
   * 只对这个**选中的**实例报：页面拿它给卡片当字段视图、并据此建草案。
   */
  onInstanceView?: (instanceId: string, fields: ConnectorField[] | null) => void;
  /**
   * 这个 scope 下**有未保存改动**的实例 id（由页面算，因为草案由页面持有）。
   *
   * 为什么必须由外部给：字段输入框的值来自**页面**的 `forms[instanceFormKey]`，
   * 面板自己那份 `forms` 只服务实例级动作（改名/启停/删除）。面板拿自己的
   * `forms` 去算"有没有未保存改动"，结果是——用户明明改了字段、卡片上也写着
   * "有未保存的改动"，列表里那个实例却**没有脏标**。
   *
   * 不传时退回面板自算（保留单独使用面板的场合）。
   */
  dirtyInstanceIds?: ReadonlySet<string>;
}

/**
 * 一个接入器 scope 的实例面板。
 *
 * 自己负责列表（GET）、选中、新建/改名/启停/删除，以及**按实例分区的草案**。
 * 右侧直接复用 `ConnectorCard`（四态诊断 + 字段表单），只是把表单状态换成
 * 当前选中实例的那一份。
 *
 * ## 右侧的字段视图是**选中实例**自己的
 *
 * 选中变化时按 `?instance_id=` 重读一次 `GET /v1/settings/connectors`。这是
 * 唯一能拿到"这个实例已保存的值"的途径——把页面级的默认实例视图直接画给
 * 实例 B，会让人以为 B 已经配好了（B 可能是全新的空实例）。
 *
 * 但重读有三种结果，**只有一种能证明视图属于 B**：
 *
 * - 成功、且响应里回显的实例就是 B → 用这份视图；
 * - 成功、但响应**没有实例回显** → 无法证明，于是仍用页面级的默认视图并
 *   明确标注"这些值不是这个实例的"（`viewMatches === false`）；
 * - 失败（例如 409 已软删、404 实例不存在）→ 同样标注警告，不冒充。
 *
 * 为什么要把"没有回显"单独当作一种结果：后端的 docstring 声称会回显
 * `instance_id`，而 `ConnectorsOut` 是 `StrictModel` 且**没有**这个字段——
 * "名字比实现强"。前端在这里必须靠**实际收到的响应**判断，而不是靠后端文件
 * 里写了什么。后端补上字段之前，这个警告会一直在，这是如实、不是 bug。
 *
 * 为什么右侧整块包一层 `key={selectedId}`：表单是按需初始化的受控组件，
 * 单靠"传进去的 draft 变了"也能刷新，但 `key` 让 React **重建**这一整棵子树，
 * 从而保证"切实例后旧的草案不可能以任何形式残留"（包括将来有人往子组件里加
 * 一个 `useState` 缓冲）。这是防串草稿的第二道闸，而不是唯一一道——第一道是
 * `instanceFormKey` 分区。
 */
export function ConnectorInstancePanel({
  scope,
  connector,
  canEdit,
  pageBusy = false,
  onSelectInstance,
  onInstanceView,
  dirtyInstanceIds,
}: ConnectorInstancePanelProps) {
  const [list, setList] = useState<ListState>({ status: 'loading' });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [forms, setForms] = useState<Record<string, InstanceFormState>>({});
  const [mutation, setMutation] = useState<SaveNotice | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  /**
   * 按实例读到的视图。`view` 只在**能证明**属于 `instanceId` 时才非空——
   * 存不住证明就不存，宁可显示"这些值不是这个实例的"。
   */
  const [scoped, setScoped] = useState<
    { instanceId: string; view: ConnectorView } | null
  >(null);

  const load = useCallback(async () => {
    setList({ status: 'loading' });
    try {
      const result = await api.connectorInstances(scope);
      setList({ status: 'ready', items: result.items });
      // 选中的实例没了（被别处删掉/改名后不再返回）→ 退回后端排序的第一个；
      // **不沿用旧 id**：那个实例已经不在列表里了，留着它只会让右侧显示
      // 一个"不存在"的实例，保存必然 404。
      setSelectedId((current) => nextSelectedId(result.items, current));
    } catch (err) {
      setList({ status: 'error', error: explainError(err) });
    }
  }, [scope]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * 选中变化 → 按实例重读一次视图。
   *
   * 没选中、或选中的是默认实例时**不必重读**：页面级那份就是默认实例的。
   * 其余情况发 `?instance_id=`；失败（404/409，例如刚被别人删掉）就**清空**
   * `scoped`，让 `viewMatches` 落回 false 并显示警告，而不是继续画上一份。
   */
  useEffect(() => {
    if (selectedId === null || selectedId === DEFAULT_INSTANCE_ID) {
      setScoped(null);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const body = await api.connectors(selectedId);
        if (!alive) return;
        // 两件事都要成立才敢用这份视图：
        //
        // 1. **响应自己说了**读到哪个实例、且正好是我要的那个；
        // 2. 这份响应里**真的有这个 scope 的连接器**，且它的 `fields` 是数组。
        //
        // 从前这里写的是 `body as unknown as ConnectorView`——把**整个响应体**
        // 当成连接器视图。响应形状是 `{schema_version, instance_id, connectors,
        // access}`，顶层根本没有 `fields`，于是 `Array.isArray(view.fields)`
        // 永远为假：按实例读回来的视图**一次都没被采用过**，页面永远显示"这些
        // 值不是这个实例的"。`as unknown as` 骗过了 tsc，这是它最贵的一种骗法。
        const echoed = (body as { instance_id?: unknown }).instance_id;
        const view = connectorViewOf(body, scope);
        setScoped(
          typeof echoed === 'string' && echoed === selectedId && view !== null
            ? { instanceId: selectedId, view }
            : null,
        );
      } catch {
        if (alive) setScoped(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [selectedId]);

  /**
   * 把"按实例读到的字段视图"上报给页面。
   *
   * 只报**能证明属于选中实例**的那一份（`scoped` 非空且 id 对得上）；否则报
   * `null`，让页面知道"这个实例的现状拿不到"——页面宁可让输入框空着，也不
   * 拿默认实例的值冒充。不报的话页面就没有这个实例的字段可分，卡片会画成空白
   * 而看不出原因。
   *
   * 与选中上报同样用"值没变就不再报"断开重渲染环：字段数组只在一次成功读取后
   * 才换新引用，所以按引用比较即可。
   */
  const reportedView = useRef<{ instanceId: string; fields: ConnectorField[] | null } | null>(null);
  useEffect(() => {
    if (selectedId === null || selectedId === DEFAULT_INSTANCE_ID) return;
    const fields =
      scoped !== null &&
      scoped.instanceId === selectedId &&
      Array.isArray(scoped.view?.fields)
        ? scoped.view.fields
        : null;
    const previous = reportedView.current;
    if (previous !== null && previous.instanceId === selectedId && previous.fields === fields) {
      return;
    }
    reportedView.current = { instanceId: selectedId, fields };
    onInstanceView?.(selectedId, fields);
  }, [selectedId, scoped, onInstanceView]);

  /** 取字段视图；选中实例不在列表里时为 null。 */
  const items = list.status === 'ready' ? list.items : [];
  const selected = useMemo(
    () => items.find((item) => item.instance_id === selectedId) ?? null,
    [items, selectedId],
  );

  /**
   * 把"当前选中"上报给页面 —— **只在它真的变了的时候**。
   *
   * 选中态有三个改写点（点列表、新建成功后选中新实例、列表刷新后旧 id 失效
   * 退回第一个），都在这里汇合：用一个 ref 记住上次报过的值，值没变就不再报。
   * 这么做不只是省事——`onSelectInstance` 是调用方每次渲染新建的函数，放进
   * 依赖里会让这个 effect 每帧都跑；而上报会改页面状态、进而重渲染。用
   * "值变了才报"断开这个环，同时保证**三个改写点一个都不漏**
   * （只接 `onSelect` 的话，后两个改写点页面永远收不到）。
   */
  const reportedId = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId === null || reportedId.current === selectedId) return;
    reportedId.current = selectedId;
    onSelectInstance?.(selectedId);
  }, [selectedId, onSelectInstance]);

  /**
   * 一份读回来的视图里有没有**字段数组**。
   *
   * 不能只看"有没有这个属性"：`vue`/后端老版本可能给 `fields: null`，而
   * `draftFromFields()` 会对它做 `for...of` 直接抛 `fields is not iterable`
   * ——整棵组件树崩掉，而且这里正是**切实例那一刻**才会踩到。
   */
  const usableView = (value: ConnectorView | undefined | null): value is ConnectorView =>
    value !== null && value !== undefined && Array.isArray(value.fields);

  /**
   * 右侧卡片用的字段来源——**只有能证明属于选中实例时才用它**。
   *
   * 两种情形能证明：
   *
   * 1. 选中的就是默认实例：页面级视图（不带 `instance_id` 的那次 GET）读的
   *    本来就是它；
   * 2. 按实例读成功，**且响应回显了同一个 id**（`scoped.instanceId === selectedId`）。
   *
   * 其余一律 ``null``——包括"我刚发过 `?instance_id=` 但请求失败/没回显"。
   *
   * ## 为什么不能回落到 ``connector``
   *
   * 之前这里写的是 ``: connector``，即读不到就用默认实例的视图顶上。后果不是
   * "显示旧数据"，是**数据错位**：``instanceFormOf(forms, scope, 'alpha',
   * connector.fields)`` 会把 alpha 的表单基线填成默认实例的字段值，用户在
   * alpha 上看到的、编辑的、保存的全是默认实例的值——而 ``?instance_id=alpha``
   * 会把它真的存到 alpha 上。
   *
   * ``null`` 时右侧不画表单，改画"读不到这个实例的配置"，让"读不到"保持
   * 可见，而不是被一份看起来合理的值填满。
   */
  const view: ConnectorView | null =
    selectedId === null
      ? null
      : selectedId === DEFAULT_INSTANCE_ID
        ? connector
        : scoped !== null && scoped.instanceId === selectedId && usableView(scoped.view)
          ? scoped.view
          : null;

  /**
   * 表单基线：只有 ``view`` 可信时才建；否则为 ``null``，右侧不画表单。
   *
   * 注意这里**不能**在 ``view === null`` 时退回 ``connector.fields``。
   */
  const form =
    selectedId === null || view === null
      ? null
      : instanceFormOf(forms, scope, selectedId, view.fields);

  /* ------------------------------------------------ 实例目录的写操作 */

  const runMutation = useCallback(
    async (action: () => Promise<unknown>, ok: SaveNotice) => {
      setMutation(null);
      try {
        await action();
        setMutation(ok);
        await load();
      } catch (err) {
        const explained = explainError(err);
        setMutation({
          tone: 'error',
          text: '这次操作没有成功。',
          detail: explained.detail ?? explained.hint ?? explained.reason,
        });
      }
    },
    [load],
  );

  const toggleEnabled = (instance: ConnectorInstance) =>
    void runMutation(
      () => api.setConnectorInstanceEnabled(scope, instance.instance_id, !instance.enabled),
      {
        tone: 'ok',
        text: instance.enabled
          ? `已停用「${instance.display_name}」：已保存的配置和修订链都还在，只是不再参与运行。`
          : `已启用「${instance.display_name}」。`,
      },
    );

  const remove = (instance: ConnectorInstance) => {
    // 删掉的实例不再出现在列表里，它那份草案也就没有落点了：留着只会在
    // 有人用一个**同名新实例**（身份不同）时被"捡回来"当成它的草稿。
    setForms((previous) => {
      const next = { ...previous };
      delete next[instanceFormKey(scope, instance.instance_id)];
      return next;
    });
    setMutation(null);
    void (async () => {
      try {
        await api.deleteConnectorInstance(scope, instance.instance_id);
        setMutation({
          tone: 'warn',
          text: `已删除「${instance.display_name}」。`,
          // 软删：用户最担心的"我的配置是不是没了"必须在这一句里答掉。
          detail:
            '这是软删除：历史配置与修订链都还留在服务端可以查，只是名字不再出现在这个列表里。',
        });
        await load();
      } catch (err) {
        const explained = explainError(err);
        setMutation({
          tone: 'error',
          text: '删除没有成功。',
          detail: explained.detail ?? explained.hint ?? explained.reason,
        });
      }
    })();
  };

  const rename = (instance: ConnectorInstance, name: string) =>
    void runMutation(
      () => api.renameConnectorInstance(scope, instance.instance_id, name),
      { tone: 'ok', text: '名字已改。身份（instance_id）没有变。' },
    );

  /* ------------------------------------------------ 渲染 */

  const busy = pageBusy || form?.saving === true;
  const editable = canEdit;
  const selfDirtyIds = useMemo(() => {
    const out = new Set<string>();
    // `view === null`（读不到选中实例的视图）时**算不出**基线，此时不能判"没改"。
    // 保守起见把建过草案的都标脏——让"有未保存改动"不会因为读失败被抹掉。
    const baselineFields = view?.fields ?? null;
    for (const [key, value] of Object.entries(forms)) {
      if (!key.startsWith(`${scope}::`)) continue;
      const id = key.slice(scope.length + 2);
      if (baselineFields === null) {
        if (value.clearKeys.length > 0 || Object.keys(value.draft).length > 0) out.add(id);
        continue;
      }
      const baseline = buildConnectorChange(baselineFields, value.draft, value.clearKeys);
      if (baseline.dirty || value.clearKeys.length > 0) out.add(id);
    }
    return out;
  }, [forms, scope, view]);

  /**
   * 列表上真正画的脏集合。
   *
   * **外部给了就以外部为准**：草案由页面持有（字段输入框读写的是页面的
   * `forms[instanceFormKey]`），面板那份 `selfDirtyIds` 只反映实例级动作，
   * 拿它画脏标会让"用户改了字段但列表没标脏"。
   *
   * 两者取并集而不是取外部一份：面板自己发起的改名/启停也可能留下未提交态，
   * 丢弃会让它变得更不准。
   */
  const effectiveDirtyIds = useMemo(() => {
    if (dirtyInstanceIds === undefined) return selfDirtyIds;
    const merged = new Set<string>(dirtyInstanceIds);
    for (const id of selfDirtyIds) merged.add(id);
    return merged;
  }, [dirtyInstanceIds, selfDirtyIds]);

  /** 有没有选中一个可操作的实例。实例级动作只在有选中时才画。 */
  const hasSelection = selectedId !== null && form !== null;

  const blockReason = deleteBlockReason(selected);
  /**
   * 右侧这份字段视图**能不能说成是选中实例的**。
   *
   * 只有两种情形能说"是"：选中的就是默认实例（页面级视图本来就是它的），
   * 或者按实例重读成功**且**响应回显了同一个 id。其余一律 false——包括
   * "我刚发过 `?instance_id=` 但响应没告诉我它读了谁"。发送参数不等于拿到
   * 那一份，这条判断必须落在**收到的响应**上。
   */
  const viewMatches =
    selectedId !== null &&
    (selectedId === DEFAULT_INSTANCE_ID ||
      (scoped !== null && scoped.instanceId === selectedId && Array.isArray(scoped.view?.fields)));
  const hasLoadedList = list.status === 'ready';

  return (
    <div
      className="connector-instances"
      data-testid={`connector-instance-panel-${scope}`}
      data-scope={scope}
    >
      <div className="connector-instances__layout">
        <aside className="connector-instances__rail" aria-label={`${connector.label} 的实例`}>
          <div className="connector-instances__rail-head">
            <span>实例</span>
            <span className="connector-instances__count" data-testid={`connector-instance-panel-${scope}-count`}>
              {hasLoadedList ? items.length : '—'}
            </span>
          </div>

          {list.status === 'loading' ? (
            <p className="panel__note" data-testid={`connector-instance-panel-${scope}-loading`}>
              正在读取实例…
            </p>
          ) : null}

          {list.status === 'error' ? (
            <div className="connector-instances__error" role="alert" data-testid={`connector-instance-panel-${scope}-error`}>
              <strong>{list.error.reason}</strong>
              {list.error.hint ? <span>{list.error.hint}</span> : null}
              <button type="button" className="button button--small" onClick={() => void load()}>
                重试
              </button>
            </div>
          ) : null}

          {hasLoadedList && items.length === 0 ? (
            <div className="connector-instances__empty" data-testid={`connector-instance-panel-${scope}-empty`}>
              <strong>还没有任何实例</strong>
              <span>
                这是全新部署时的正常状态（默认实例只在第一次保存配置时才由服务端补建）。
                在下面建第一个——建好之后，右边的配置就属于它。
              </span>
            </div>
          ) : null}

          {hasLoadedList && items.length > 0 ? (
            <InstanceList
              items={items}
              selectedId={selectedId}
              onSelect={(instanceId) => {
                setSelectedId(instanceId);
                // 换实例时清掉"删除确认/改名"这类附着在旧实例上的临时态。
                setConfirmDelete(null);
                setRenaming(null);
              }}
              dirtyIds={effectiveDirtyIds}
              busy={pageBusy}
            />
          ) : null}

          {editable ? (
            <CreateInstanceForm
              scope={scope}
              busy={pageBusy}
              onCreated={(instance) => {
                setMutation({
                  tone: 'ok',
                  text: `已新建「${instance.display_name}」（${instance.instance_id}）。`,
                  detail: '它现在有自己的一份配置，保存时不会再影响其它实例。',
                });
                setSelectedId(instance.instance_id);
                void load();
              }}
            />
          ) : null}
        </aside>

        <div className="connector-instances__detail" data-testid={`connector-instance-panel-${scope}-detail`}>
          {selected && selected.deleted_at !== null ? (
            <div className="banner banner--error" role="status" data-testid={`connector-instance-panel-${scope}-deleted`}>
              这个实例已被删除，不能再保存配置。请在左侧选另一个实例，或新建一个。
            </div>
          ) : null}

          {!hasSelection ? (
            <p className="panel__note" data-testid={`connector-instance-panel-${scope}-nothing-selected`}>
              {hasLoadedList && items.length === 0
                ? '左侧建好第一个实例后，这里会显示它的配置。'
                : '在左侧选一个实例，这里显示它的配置。'}
            </p>
          ) : (
            <>
              {/*
               * **绝不在这里再放一个 `ConnectorCard`。**
               *
               * 卡片（`connector-${scope}`）自己就带着一个实例面板——挂在
               * 「配置项」之前——那正是"先选实例、再改字段"的入口。如果这里再
               * 渲染一张卡片，面板就会递归地套进自己：每一层都再挂一个面板，
               * 列表与表单成倍增长。这个 bug 不会报错、`tsc` 也看不见（参数
               * 类型完全合法），只有真的在浏览器里数元素才会发现。
               *
               * 所以右侧详情**只放实例级的动作**（改名/启停/删除/通知），
               * 字段表单留在卡片自己的位置上。
               */}
              <div
                className="connector-instances__toolbar"
                role="group"
                aria-label={`「${selected?.display_name ?? ''}」的实例操作`}
              >
                {renaming && renaming.id === selectedId ? (
                  <form
                    className="connector-instances__rename"
                    onSubmit={(event) => {
                      event.preventDefault();
                      if (selected) rename(selected, renaming.name);
                      setRenaming(null);
                    }}
                  >
                    <input
                      className="connector-input"
                      aria-label="新的显示名"
                      data-testid="connector-instance-rename-input"
                      value={renaming.name}
                      maxLength={64}
                      disabled={busy}
                      onChange={(event) => setRenaming({ id: renaming.id, name: event.target.value })}
                    />
                    <button
                      type="submit"
                      className="button button--small button--primary"
                      data-testid="connector-instance-rename-submit"
                      disabled={busy || renaming.name.trim() === ''}
                    >
                      保存名字
                    </button>
                    <button
                      type="button"
                      className="button button--small"
                      onClick={() => setRenaming(null)}
                      disabled={busy}
                    >
                      取消
                    </button>
                  </form>
                ) : (
                  <button
                    type="button"
                    className="button button--small"
                    data-testid="connector-instance-rename"
                    disabled={!editable || busy || !selected}
                    onClick={() =>
                      selected && setRenaming({ id: selected.instance_id, name: selected.display_name })
                    }
                  >
                    改名
                  </button>
                )}
                <button
                  type="button"
                  className="button button--small"
                  data-testid="connector-instance-toggle-enabled"
                  disabled={!editable || busy || !selected}
                  onClick={() => selected && toggleEnabled(selected)}
                >
                  {selected?.enabled ? '停用这个实例' : '启用这个实例'}
                </button>
                <button
                  type="button"
                  className="button button--small button--danger"
                  data-testid="connector-instance-delete"
                  // **置灰而不是点了才报错**：原因是后端给的 `deletable`。
                  disabled={!editable || busy || blockReason !== null}
                  title={blockReason ?? '删除这个实例（软删）'}
                  aria-describedby={`connector-instance-delete-reason-${scope}`}
                  onClick={() => selected && setConfirmDelete(selected.instance_id)}
                >
                  删除这个实例
                </button>
                <span
                  className="connector-form__note"
                  id={`connector-instance-delete-reason-${scope}`}
                  data-testid="connector-instance-delete-reason"
                >
                  {blockReason ?? ''}
                </span>
              </div>

              {confirmDelete !== null && selected && confirmDelete === selected.instance_id ? (
                <div
                  className="connector-confirm"
                  role="alertdialog"
                  aria-labelledby={`connector-instance-delete-title-${scope}`}
                >
                  <strong id={`connector-instance-delete-title-${scope}`}>
                    删除「{selected.display_name}」？
                  </strong>
                  <p>
                    这是<strong>软删除</strong>：这个实例的名字会从列表里消失，也不再参与运行；
                    但历史配置与修订链留在服务端可以查，不会被销毁。已软删的实例不能再保存配置，
                    同名的新实例是一个全新的身份。
                  </p>
                  <div className="connector-confirm__actions">
                    <button
                      type="button"
                      className="button button--small button--danger"
                      data-testid="connector-instance-delete-confirm"
                      disabled={busy}
                      onClick={() => {
                        remove(selected);
                        setConfirmDelete(null);
                      }}
                    >
                      确认删除
                    </button>
                    <button
                      type="button"
                      className="button button--small"
                      data-testid="connector-instance-delete-cancel"
                      disabled={busy}
                      onClick={() => setConfirmDelete(null)}
                    >
                      再想想
                    </button>
                  </div>
                </div>
              ) : null}

              {!viewMatches ? (
                <div
                  className="connector-instances__view-warning"
                  role="status"
                  data-testid="connector-instance-view-warning"
                >
                  <strong>这里的"当前值"不是这个实例的。</strong>
                  按实例读取没有成功（实例刚被删除、服务端版本较旧，或者响应没有告诉我们它读的是
                  哪个实例），所以下面显示的是默认实例（<code>{DEFAULT_INSTANCE_ID}</code>）已保存的
                  值，<strong>只作参考，不代表这个实例现在的配置</strong>。
                  <strong>保存会写进你选中的「{selected?.display_name}」（{selectedId}）</strong>，
                  不影响默认实例。
                </div>
              ) : null}

              {mutation ? (
                <div
                  className={`connector-notice connector-notice--${mutation.tone}`}
                  role={mutation.tone === 'error' ? 'alert' : 'status'}
                  data-testid="connector-instance-mutation-notice"
                >
                  <div className="connector-notice__body">
                    <strong>{mutation.text}</strong>
                    {mutation.detail ? (
                      <p className="connector-notice__detail">{mutation.detail}</p>
                    ) : null}
                  </div>
                </div>
              ) : null}

            </>
          )}
        </div>
      </div>
    </div>
  );
}
