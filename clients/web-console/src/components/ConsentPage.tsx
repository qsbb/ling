/**
 * 权限与隐私：⑦ 类的设置面。
 *
 * 这里的开关直接对应 `ConsentRule(scope, granted, valid_until)`——
 * **不是**环境变量。授权是业务表记录：可以随时授予、撤销、带有效期，
 * 而且每次变更都留 revision。改环境变量只能改"宿主是否提供能力"，
 * 改不了"这个 subject 有没有被授权"。
 */
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import type { ConsentRecord } from '../types';
import { AlertIcon, ArrowLeftIcon, RefreshIcon } from './Icons';

interface ConsentPageProps {
  onBack: () => void;
}

/** scope → 人话。没登记的后端 scope 会原样显示，不会被吞掉。 */
const SCOPE_LABELS: Record<string, { label: string; hint: string }> = {
  camera: { label: '摄像头', hint: '允许它在对话中调用摄像头。默认关闭。' },
  location: { label: '位置', hint: '允许它读取位置。默认关闭。' },
  public_publish: {
    label: '公开发布',
    hint: '允许把内容发布到外部平台。授权后每条内容仍需二次确认与真实回执。',
  },
  'memory.cross_device': {
    label: '跨设备记忆召回',
    hint: '允许同一个人的已绑定设备之间互相召回记忆。',
  },
  proactive_contact: { label: '主动联系', hint: '允许它在没有新消息时主动找你。' },
  media_upload: { label: '上传媒体', hint: '允许把本地媒体上传到外部服务。' },
  'media.inbound': {
    label: '描述你发来的图片/音频',
    hint: '允许把它交给你发来的图片或音频。默认关闭——"能看图"和"能把你发的图交给模型描述"是两件事。',
  },
  device: {
    label: '设备与真实世界动作',
    hint: '允许关灯、发指令到已连接设备等不可逆动作。默认关闭。',
  },
};

export function ConsentPage({ onBack }: ConsentPageProps) {
  const [records, setRecords] = useState<ConsentRecord[]>([]);
  const [scopes, setScopes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ExplainedError | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.consents();
      setRecords(data.consents);
      setScopes(data.known_scopes);
    } catch (err) {
      setError(explainError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (scope: string, granted: boolean) => {
    if (busy) return;
    setBusy(scope);
    setError(null);
    setMessage(null);
    try {
      await api.setConsent({ scope, granted });
      setMessage(
        granted
          ? `已授予「${SCOPE_LABELS[scope]?.label ?? scope}」`
          : `已撤销「${SCOPE_LABELS[scope]?.label ?? scope}」`,
      );
      await load();
    } catch (err) {
      setError(explainError(err));
    } finally {
      setBusy(null);
    }
  };

  const recordOf = (scope: string) => records.find((item) => item.scope === scope);

  return (
    <section className="page">
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeftIcon /> 返回设置
      </button>

      <h1 className="page__title">权限与隐私</h1>
      <p className="page__hint">
        授权记录可随时撤销，撤销后派生内容必须失效。默认全部关闭。
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

      {message ? <div className="banner banner--ok">{message}</div> : null}

      {!loading ? (
        <ul className="consent-list">
          {scopes.map((scope) => {
            const record = recordOf(scope);
            const granted = record?.granted ?? false;
            const meta = SCOPE_LABELS[scope];
            return (
              <li key={scope} className="consent-list__item">
                <div className="consent-list__body">
                  <div className="consent-list__title">
                    {meta?.label ?? scope}
                    <span className={`badge ${granted ? 'badge--published' : 'badge--draft'}`}>
                      {granted ? '已授权' : '未授权'}
                    </span>
                  </div>
                  <div className="consent-list__hint">{meta?.hint ?? '后端登记但界面未说明的授权范围'}</div>
                  <div className="consent-list__meta">
                    scope = <code>{scope}</code>
                    {record?.valid_until ? ` · 有效至 ${record.valid_until}` : ''}
                    {record ? ` · 修订 ${record.revision}` : ' · 未设置过'}
                  </div>
                </div>
                <button
                  type="button"
                  className={`button ${granted ? '' : 'button--primary'}`}
                  disabled={busy === scope}
                  onClick={() => void toggle(scope, !granted)}
                >
                  {busy === scope ? '处理中…' : granted ? '撤销' : '授予'}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
