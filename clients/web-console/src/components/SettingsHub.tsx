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
 * 与后端 `GET /v1/settings/taxonomy` 的关系：它仍是那 9 类的唯一权威，
 * 这里只对**留在设置里的那两类**（模型与生成 ③、权限与隐私 ⑦）取用它的
 * `label`/`verb`，免得后端改名而这里还显示旧名。**taxonomy 挂了不影响导航**——
 * 卡片用的是本文件的文案，加载失败只是少了那条"后端文案"，不会让整页变白板
 * （旧实现正是这样：taxonomy 一失败，设置中心一个入口都不剩）。
 *
 * 已迁出的三类（记忆与学习 ④、对话与主动性 ⑤、生活与活动 ⑧）**不在设置里**，
 * 它们在各自的领域页（记忆页 / 人设页 / 生活页）。底部那条灰字只是指路，
 * 不是入口——把入口留在这里就等于没迁。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { TaxonomyResponse } from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';
import '../settings.css';

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

interface SettingsGroup {
  key: string;
  title: string;
  /** 这一组为什么算"程序/运维"。 */
  note: string;
  entries: readonly SettingsEntry[];
}

/**
 * 三组 / 六个页面（DESIGN §2 的表）。顺序即渲染顺序：
 * 安全与隐私（一跳可达的权利与凭据）→ 模型与密钥（实例能力）→ 维护与关于（运维动作）。
 */
export const SETTINGS_GROUPS: readonly SettingsGroup[] = [
  {
    key: 'security',
    title: '安全与隐私',
    note: '主体权利与凭据。同意可以撤回、账号可以合并、密码可以改——都在一跳之内。',
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
        key: 'identity',
        label: '多用户与账号',
        verb: '把同一个人的多个号认成一个人；合并只写指针，可随时拆开。',
        path: '/settings/identity',
        testid: 'open-identity',
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
  const decorate = (entry: SettingsEntry): SettingsEntry => {
    if (!entry.taxonomyKey) return entry;
    const category = taxonomy?.categories.find((item) => item.key === entry.taxonomyKey);
    if (!category) return entry;
    return { ...entry, label: category.label, verb: category.verb };
  };

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回聊天
      </button>

      <h1 className="page__title">设置</h1>
      <p className="page__hint">
        这里只有程序与运维：凭据、密钥、版本。改她的行为（记什么、说不说话、
        怎么过日子）跟着各自的页面走，不在这里。
      </p>

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

      <div className="settings-groups">
        {SETTINGS_GROUPS.map((group) => (
          <section key={group.key} className="settings-group" data-settings-group={group.key}>
            <div className="settings-group__head">
              <h2 className="settings-group__title">{group.title}</h2>
              <p className="settings-group__note">{group.note}</p>
            </div>
            <div className="settings-hub">
              {group.entries.map((raw) => {
                const entry = decorate(raw);
                return (
                  <button
                    key={entry.key}
                    type="button"
                    className="settings-hub__card"
                    data-taxonomy-key={entry.taxonomyKey ?? undefined}
                    data-testid={entry.testid}
                    onClick={() => onOpen(entry.path)}
                  >
                    <span className="settings-hub__index">{entry.key === 'about' ? 'i' : '≡'}</span>
                    <span className="settings-hub__body">
                      <span className="settings-hub__label">{entry.label}</span>
                      <span className="settings-hub__verb">{entry.verb}</span>
                    </span>
                  </button>
                );
              })}
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
