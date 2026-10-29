import { useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError } from '../errors';
import type { AuthResponse } from '../types';
import { AlertIcon } from './Icons';

interface LoginViewProps {
  onSuccess: (auth: AuthResponse) => void;
}

export function LoginView({ onSuccess }: LoginViewProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof explainError> | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !username.trim() || !password) return;
    setPending(true);
    setError(null);
    try {
      const auth = await api.login(username.trim(), password);
      onSuccess(auth);
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
        <h1 className="login__title">登录后继续对话</h1>
        <p className="login__hint">会话与历史保存在服务端，浏览器只负责显示。</p>

        <label className="field">
          <span className="field__label">用户名</span>
          <input
            className="field__input"
            name="username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={pending}
            required
          />
        </label>
        <label className="field">
          <span className="field__label">密码</span>
          <input
            className="field__input"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={pending}
            required
          />
        </label>

        {error ? (
          <div className="login__error" role="alert">
            <AlertIcon />
            <div>
              <strong>{error.reason}</strong>
              {error.hint ? <span>{error.hint}</span> : null}
            </div>
          </div>
        ) : null}

        <button type="submit" className="button button--primary login__submit" disabled={pending}>
          {pending ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}
