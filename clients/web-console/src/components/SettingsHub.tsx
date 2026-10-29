/**
 * 设置中心：**只渲染已经有设置面的类**。
 *
 * 类名与顺序全部来自 GET /v1/settings/taxonomy，前端不自己抄一份——
 * 抄一份就会漂：后端加了类、前端不知道；后端改了名、前端还显示旧名。
 *
 * available=false 的类**不建入口**，只在底部用一行灰字说明"尚未开放"。
 * 建了空页面用户点进去只会看到白板，比没有入口更糟。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { TaxonomyResponse } from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';

interface SettingsHubProps {
  onBack: () => void;
  /** 直接给路径，App 不需要再维护一份 key→路径 的映射。 */
  onOpen: (path: string) => void;
}

/** 分类 key → 界面跳转目标。没列在这里的 key 说明 UI 还没接，卡片不渲染。 */
const ROUTES: Record<string, string> = {
  identity: '/persona',
  model: '/settings/model',
  memory: '/settings/memory',
  dialogue: '/settings/dialogue',
  life: '/settings/life',
  privacy: '/settings/privacy',
};

export function SettingsHub({ onBack, onOpen }: SettingsHubProps) {
  const [data, setData] = useState<TaxonomyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ExplainedError | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.taxonomy());
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const available = (data?.categories ?? []).filter(
    (category) => category.available && ROUTES[category.key],
  );
  const pending = (data?.categories ?? []).filter(
    (category) => !category.available || !ROUTES[category.key],
  );

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回聊天
      </button>

      <h1 className="page__title">设置</h1>
      <p className="page__hint">
        每一类对应一种你能直接改的决策。只列出已经有设置面的类别，其余等实现后再开放。
      </p>

      {loading ? <p className="page__hint">加载中…</p> : null}

      {error ? (
        <div className="banner banner--error">
          <AlertIcon />
          <div>
            <strong>{error.reason}</strong>
            {error.hint ? <div className="banner__detail">{error.hint}</div> : null}
          </div>
          <button type="button" className="button button--small" onClick={() => void load()}>
            <RefreshIcon /> 重试
          </button>
        </div>
      ) : null}

      {available.length > 0 ? (
        <div className="settings-hub">
          {available.map((category) => (
            <button
              key={category.key}
              type="button"
              className="settings-hub__card"
              data-taxonomy-key={category.key}
              onClick={() => onOpen(ROUTES[category.key] ?? '/settings')}
            >
              <span className="settings-hub__index">{category.index}</span>
              <span className="settings-hub__body">
                <span className="settings-hub__label">{category.label}</span>
                <span className="settings-hub__verb">{category.verb}</span>
                <span className="settings-hub__groups">
                  {category.groups.map((group) => (
                    <span key={group.key} className="badge">
                      {group.label}
                    </span>
                  ))}
                </span>
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {!loading && !error && available.length === 0 ? (
        <p className="page__hint">后端没有报告任何可编辑的类别。</p>
      ) : null}

      {pending.length > 0 ? (
        <p className="page__hint settings-hub__pending">
          另有 {pending.length} 类尚未开放：
          {pending.map((category) => category.label).join('、')}。
        </p>
      ) : null}

      {/* 不挂在 taxonomy 上：这不是"一类设置"，而是"谁是谁"的现状表。
          合并是人的决定，系统只提供操作与现状。 */}
      <h2 className="page__subtitle">账号</h2>
      <div className="settings-hub">
        <button
          type="button"
          className="settings-hub__card"
          onClick={() => onOpen('/settings/account')}
          data-testid="open-account"
        >
          <span className="settings-hub__index">≡</span>
          <span className="settings-hub__body">
            <span className="settings-hub__label">登录名与密码</span>
            <span className="settings-hub__verb">
              改你自己的登录凭据；改完其它设备上的登录会失效
            </span>
          </span>
        </button>
        <button
          type="button"
          className="settings-hub__card"
          onClick={() => onOpen('/settings/identity')}
          data-testid="open-identity"
        >
          <span className="settings-hub__index">＋</span>
          <span className="settings-hub__body">
            <span className="settings-hub__label">多用户与账号</span>
            <span className="settings-hub__verb">
              把同一个人的多个号认成一个人；合并只写指针，可随时拆开
            </span>
          </span>
        </button>
      </div>
    </section>
  );
}
