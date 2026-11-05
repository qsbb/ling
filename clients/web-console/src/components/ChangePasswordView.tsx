import { useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import { AlertIcon } from './Icons';

interface ChangePasswordViewProps {
  username: string;
  onSuccess: () => void;
  onLogout: () => void;
}

/**
 * 首次登录的强制改密页。
 *
 * 为什么要有这一页：安装脚本生成的是随机口令，只打印在启动日志里。
 * 如果允许带着它继续用，那串随机密码就永久留在日志和 .env 里——
 * 等于给这个系统装了个不用维护的后门。所以后端在改密之前只放行
 * `/v1/auth/*`，前端必须先把人送到这里。
 *
 * 这一页刻意**不能跳过**：没有"稍后再说"，也不显示其它导航。
 */
export function ChangePasswordView({ username, onSuccess, onLogout }: ChangePasswordViewProps) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof explainError> | null>(null);

  const tooShort = next.length > 0 && next.length < 8;
  const mismatch = confirm.length > 0 && next !== confirm;
  const canSubmit = !pending && current.length > 0 && next.length >= 8 && next === confirm;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setPending(true);
    setError(null);
    try {
      await api.changePassword(current, next);
      onSuccess();
    } catch (cause) {
      setError(explainError(cause));
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login">
      <form className="login__card" onSubmit={(event) => void submit(event)}>
        <div className="login__brand">
          <span className="login__brand-dot" aria-hidden="true" />
          <span>ling</span>
        </div>
        <h1 className="login__title">先设置一个新密码</h1>
        <p className="login__hint">
          {username} 当前用的是一次性随机口令（在启动日志里）。改成你自己记得的密码后
          才能继续使用。
        </p>

        <label className="login__field">
          <span>当前密码</span>
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
            data-testid="current-password"
          />
        </label>
        <label className="login__field">
          <span>新密码（至少 8 位）</span>
          <input
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(event) => setNext(event.target.value)}
            data-testid="new-password"
          />
        </label>
        <label className="login__field">
          <span>再输一次</span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            data-testid="confirm-password"
          />
        </label>

        {tooShort && <p className="login__error">新密码至少 8 位</p>}
        {mismatch && <p className="login__error">两次输入不一致</p>}
        {error && (
          <p className="login__error" role="alert">
            <AlertIcon />
            <strong>{error.reason}</strong>
            {error.hint ? <span>{error.hint}</span> : null}
          </p>
        )}

        <button className="login__submit" type="submit" disabled={!canSubmit} data-testid="change-password">
          {pending ? '保存中…' : '保存并继续'}
        </button>
        <button className="login__link" type="button" onClick={onLogout}>
          换个账号登录
        </button>
      </form>
    </div>
  );
}
