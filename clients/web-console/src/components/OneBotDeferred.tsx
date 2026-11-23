import { useEffect, useRef, useState } from 'react';
import { api, ApiError, type OneBotDeferredSetting } from '../api';

/**
 * 一个会话自己的「允许晚点回」开关。
 *
 * **会话切换靠卸载而不是靠比对**：`ChatPage` 在 `currentId` 变化时把整个
 * 会话设置面板收起，面板按 `session_id` 作 key 重新挂载。所以这里只需要
 * 保证「卸载后回来的响应不再写 state」，不必再自己记一份"我是哪个会话"。
 * 那层比对在没有卸载的调用方里才有意义，而这个组件目前只有这一处调用方。
 */

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : '无法连接服务端，请稍后重试';
}

/**
 * 409 不是普通失败：它说的是「这个会话还没有可信的接入实例」。
 * 服务器消息已经说清了原因，这里只补一句**下一步做什么**——面板里
 * 紧挨着就有实例确认入口，文案指向它，而不是让用户自己找。
 */
function conflictText(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  if (error.code === 'conflict' && error.message.includes('接入实例')) {
    return `${error.message}。可在下方「平台来源」里确认后再开启。`;
  }
  return error.message;
}

export function OneBotDeferred({ sessionId }: { sessionId: string }) {
  const [setting, setSetting] = useState<OneBotDeferredSetting | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  /**
   * 上一次确认过的服务端值。保存失败时把复选框拨回它——**界面显示的状态
   * 必须等于服务端的状态**，不能让一次失败的点击看起来像生效了。
   */
  const confirmed = useRef<boolean | null>(null);

  useEffect(() => {
    let active = true;
    setSetting(null);
    setError('');
    confirmed.current = null;
    void api.onebotDeferred(sessionId).then((result) => {
      if (!active) return;
      confirmed.current = result.enabled;
      setSetting(result);
    }).catch((reason: unknown) => {
      if (!active) return;
      // 群聊、网页与官方 QQ 机器人会话在这里拿到 422：这个功能对它们不存在。
      // 界面本来就没挂载这个组件（见 `supportsOneBotDeferred`），这里是兜底：
      // 不显示、也不把 422 当成"读失败"念给用户听。
      if (reason instanceof ApiError && reason.status === 422) {
        setSetting({ enabled: false });
        return;
      }
      if (reason instanceof ApiError && (reason.status === 404 || reason.status === 403)) return;
      setError(errorText(reason));
    });
    return () => { active = false; };
  }, [sessionId]);

  if (setting === null) return null;

  const enabled = setting.enabled;

  async function save(next: boolean) {
    const previous = confirmed.current ?? enabled;
    setBusy(true);
    setError('');
    // 先乐观显示，让点击立刻有反馈；失败时再拨回去。
    setSetting({ enabled: next });
    try {
      const result = await api.setOnebotDeferred(sessionId, next);
      confirmed.current = result.enabled;
      setSetting(result);
    } catch (reason) {
      setSetting({ enabled: previous });
      setError(conflictText(reason) ?? errorText(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="window-policy__access window-policy__deferred">
      <label>
        <input
          type="checkbox"
          aria-label="允许晚点回"
          checked={enabled}
          disabled={busy}
          onChange={(event) => void save(event.target.checked)}
        />
        允许晚点回
      </label>
      <small>
        {enabled
          ? '她可以在这次私聊里决定过一会儿再回，等多久也由她判断。能立刻回的时候照常聊。'
          : '只能在私聊里开启。开启后，等不等、等多久都由她在实际对话中决定。'}
      </small>
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}
