/**
 * 账号页：改登录名、改密码、退出登录。
 *
 * 为什么单独一页而不是塞进"设置中心"的某个分类：设置中心的分类来自
 * `/v1/settings/taxonomy`（人格、模型、记忆…都是**她**的配置），
 * 而这里是**你**的登录凭据，不该被当成她的某个设置项。
 *
 * 两条硬要求：
 * 1. **改任何一项都要当前密码**。cookie 被盗时改名/改密是接管账号的第一步。
 * 2. **改完别把人踢下线**。后端保留当前会话、吊销其它会话；这里改完
 *    直接把新用户名显示出来，不需要重新登录。
 */
import { useState, type FormEvent } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import { AlertIcon } from './Icons';

interface AccountPageProps {
  username: string;
  onBack: () => void;
  onRenamed: (username: string) => void;
  onLogout: () => void;
}

export function AccountPage({ username, onBack, onRenamed, onLogout }: AccountPageProps) {
  const [currentForName, setCurrentForName] = useState('');
  // 「新的登录名」从空白开始：预填当前值会让人以为"新的"就是旧值，
  // 并且让下面的"登录名没有变化。"变成一句常驻废话。
  const [newName, setNewName] = useState('');
  const [namePending, setNamePending] = useState(false);
  const [nameError, setNameError] = useState<ExplainedError | null>(null);
  const [nameDone, setNameDone] = useState('');

  const [currentForPass, setCurrentForPass] = useState('');
  const [nextPass, setNextPass] = useState('');
  const [confirmPass, setConfirmPass] = useState('');
  const [passPending, setPassPending] = useState(false);
  const [passError, setPassError] = useState<ExplainedError | null>(null);
  const [passDone, setPassDone] = useState(false);

  const trimmedName = newName.trim();
  const nameChanged = trimmedName.length > 0 && trimmedName !== username;
  const nameTooShort = trimmedName.length > 0 && trimmedName.length < 3;
  const passTooShort = nextPass.length > 0 && nextPass.length < 8;
  const passMismatch = confirmPass.length > 0 && nextPass !== confirmPass;

  const submitName = async (event: FormEvent) => {
    event.preventDefault();
    if (!nameChanged || nameTooShort) return;
    setNamePending(true);
    setNameError(null);
    setNameDone('');
    try {
      const result = await api.changeAccount(currentForName, newName.trim());
      onRenamed(result.username);
      // 改完之后输入框回到空白：这里要的是"新的登录名"，不是当前值的副本。
      setNewName('');
      setCurrentForName('');
      setNameDone(`已改为「${result.username}」，其它设备上的登录已失效`);
    } catch (err) {
      setNameError(explainError(err));
    } finally {
      setNamePending(false);
    }
  };

  const submitPass = async (event: FormEvent) => {
    event.preventDefault();
    if (passTooShort || passMismatch) return;
    setPassPending(true);
    setPassError(null);
    setPassDone(false);
    try {
      await api.changePassword(currentForPass, nextPass);
      setCurrentForPass('');
      setNextPass('');
      setConfirmPass('');
      setPassDone(true);
    } catch (err) {
      setPassError(explainError(err));
    } finally {
      setPassPending(false);
    }
  };

  return (
    <div className="page">
      <div className="page__head">
        <button type="button" className="button button--ghost" onClick={onBack}>
          返回
        </button>
        <h1 className="page__title">账号</h1>
        <p className="page__hint">这里的改动只影响你的登录，不改她的人设。</p>
      </div>

      <section className="card">
        <h2 className="card__title">登录名</h2>
        <p className="card__hint">当前：{username}</p>
        <form className="form" onSubmit={submitName}>
          <label className="field">
            <span className="field__label">新的登录名</span>
            <input
              className="input"
              value={newName}
              placeholder="输入新的登录名（不想改就留空）"
              onChange={(e) => setNewName(e.target.value)}
              autoComplete="username"
            />
          </label>
          {nameTooShort ? <p className="field__error">至少 3 个字符</p> : null}
          <label className="field">
            <span className="field__label">当前密码（确认是你在改）</span>
            <input
              className="input"
              type="password"
              value={currentForName}
              onChange={(e) => setCurrentForName(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          {nameError ? (
            <p className="field__error">
              <AlertIcon /> {nameError.reason}
            </p>
          ) : null}
          {nameDone ? <p className="field__ok">{nameDone}</p> : null}
          <button
            type="submit"
            className="button"
            disabled={namePending || !nameChanged || !currentForName || nameTooShort}
          >
            {namePending ? '保存中…' : '保存登录名'}
          </button>
        </form>
      </section>

      <section className="card">
        <h2 className="card__title">密码</h2>
        <p className="card__hint">改完会吊销其它设备上的登录，当前这次保留。</p>
        <form className="form" onSubmit={submitPass}>
          <label className="field">
            <span className="field__label">当前密码</span>
            <input
              className="input"
              type="password"
              value={currentForPass}
              onChange={(e) => setCurrentForPass(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          <label className="field">
            <span className="field__label">新密码</span>
            <input
              className="input"
              type="password"
              value={nextPass}
              onChange={(e) => setNextPass(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          {passTooShort ? <p className="field__error">至少 8 个字符</p> : null}
          <label className="field">
            <span className="field__label">再输一次</span>
            <input
              className="input"
              type="password"
              value={confirmPass}
              onChange={(e) => setConfirmPass(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          {passMismatch ? <p className="field__error">两次输入不一致</p> : null}
          {passError ? (
            <p className="field__error">
              <AlertIcon /> {passError.reason}
            </p>
          ) : null}
          {passDone ? <p className="field__ok">密码已更新</p> : null}
          <button
            type="submit"
            className="button"
            disabled={
              passPending || !currentForPass || !nextPass || passTooShort || passMismatch
            }
          >
            {passPending ? '保存中…' : '保存密码'}
          </button>
        </form>
      </section>

      <section className="card">
        <h2 className="card__title">退出登录</h2>
        <p className="card__hint">退出后需要重新输入账号密码。</p>
        <button
          type="button"
          className="button button--ghost account-logout"
          data-testid="account-logout"
          onClick={onLogout}
        >
          退出登录
        </button>
      </section>
    </div>
  );
}
