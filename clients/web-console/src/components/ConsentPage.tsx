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
// 授权页与设置枢纽共用同一套页面骨架样式（settings.css 里的 .settings-page 等）。
// 之前这个页面一份 CSS 都没引，`consent-row*` 规则自然不生效，列表退化成
// 带圆点的裸 `<ul>`；显式引入后规则才落地。
import '../settings.css';
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
    label: '描述收到的图片/音频/视频',
    // 说清生效范围：授权是**按人**记的，而群里的发言人往往从没来过这个页面。
    // 只写"你发来的图片"会让人以为管不到别人发的图，于是永远不授权，
    // 而她永远看不懂群里的图。
    hint: '你自己授权一次，她就能描述你收到的媒体——包括群里别人发来的图片/音频/视频。撤销即停止，那时只认各自单独授权过的人；每个人的授权仍单独记录，可以分别授予或撤销。',
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
        <ul className="consent-rows">
          {scopes.map((scope) => {
            const record = recordOf(scope);
            const granted = record?.granted ?? false;
            const meta = SCOPE_LABELS[scope];
            return (
              /*
               * 一条权限 = 一行：名称 + 状态徽标 + 一句话 + 按钮。
               *
               * 原来每条占三行（名称 / 说明 / scope=… 修订…），八条就是二十四行，
               * 眼睛要来回跳着配对"这一行的按钮管的是哪一条"。现在名称与徽标
               * 同排、说明与按钮同行（窄屏堆叠），`scope` 与修订号降为 title
               * 悬停提示——它们是排查用的，不该占正文宽度。
               */
              <li
                key={scope}
                className="consent-row"
                data-state={granted ? 'granted' : 'denied'}
              >
                {/* 一行 = 主信息 + 按钮。`title` 那句（scope / 修订）挂在主信息上
                    而不是整行：整行带 title 会让悬停提示盖住按钮的可点区域。 */}
                <div className="consent-row__main">
                  <div className="consent-row__title">
                    <span className="consent-row__name">{meta?.label ?? scope}</span>
                    <span
                      className={`consent-row__badge consent-row__badge--${granted ? 'granted' : 'denied'}`}
                    >
                      {granted ? '已授权' : '未授权'}
                    </span>
                    <span
                      className="consent-row__code"
                      title={`scope=${scope}${record ? ` · 修订 ${record.revision}` : ' · 未设置过'}${
                        record?.valid_until ? ` · 有效至 ${record.valid_until}` : ''
                      }`}
                    >
                      {scope}
                    </span>
                  </div>
                  <p className="consent-row__hint">{meta?.hint ?? '后端登记但界面未说明的授权范围'}</p>
                </div>
                <div className="consent-row__action">
                <button
                  type="button"
                  className={`button button--small ${granted ? '' : 'button--primary'}`}
                  disabled={busy === scope}
                  onClick={() => void toggle(scope, !granted)}
                >
                  {busy === scope ? '处理中…' : granted ? '撤回' : '授予'}
                </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
