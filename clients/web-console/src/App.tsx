import { useCallback, useEffect, useState } from 'react';
import { api, setUnauthorizedHandler } from './api';
import { ChatPage } from './components/ChatPage';
import { ChangePasswordView } from './components/ChangePasswordView';
import { LoginView } from './components/LoginView';
import { ConsentPage } from './components/ConsentPage';
import { DialoguePage } from './components/DialoguePage';
import { LifeNowPage } from './components/LifeNowPage';
import { LifePage } from './components/LifePage';
import { MemoryPage } from './components/MemoryPage';
import { PersonaPage } from './components/PersonaPage';
import { IdentityPage } from './components/IdentityPage';
import { SettingsHub } from './components/SettingsHub';
import { SettingsPage } from './components/SettingsPage';
import { TestModeBanner } from './components/TestModeBanner';
import { LifeIcon, LogoutIcon, MenuIcon, PersonaIcon, SettingsIcon } from './components/Icons';
import { navigate, routePath, useRoute } from './route';
import type { AuthResponse, UserInfo } from './types';

type AuthState =
  | { status: 'loading' }
  | { status: 'anonymous' }
  | { status: 'authenticated'; user: UserInfo; testMode: boolean };

export default function App() {
  const route = useRoute();
  const [auth, setAuth] = useState<AuthState>({ status: 'loading' });
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuth({ status: 'anonymous' });
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setAuth({ status: 'authenticated', user: me.user, testMode: me.test_mode });
      })
      .catch(() => {
        if (!cancelled) setAuth({ status: 'anonymous' });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogin = (response: AuthResponse) => {
    setAuth({ status: 'authenticated', user: response.user, testMode: response.test_mode });
  };

  const handleLogout = useCallback(() => {
    void api
      .logout()
      .catch(() => undefined)
      .finally(() => {
        setAuth({ status: 'anonymous' });
        navigate(routePath({ name: 'chat', sessionId: null }));
      });
  }, []);

  const handleTestMode = useCallback((value: boolean) => {
    setAuth((current) =>
      current.status === 'authenticated' && current.testMode !== value
        ? { ...current, testMode: value }
        : current,
    );
  }, []);

  if (auth.status === 'loading') {
    return (
      <div className="splash">
        <span className="spinner" aria-hidden="true" />
        <span>ling 正在醒来…</span>
      </div>
    );
  }

  if (auth.status === 'anonymous') {
    return <LoginView onSuccess={handleLogin} />;
  }

  const goSettings = () => {
    setSidebarOpen(false);
    navigate('/settings');
  };
  const goLife = () => {
    setSidebarOpen(false);
    navigate('/life');
  };
  const goPersona = () => {
    setSidebarOpen(false);
    navigate('/persona');
  };
  const goChat = () => {
    setSidebarOpen(false);
    navigate(routePath({ name: 'chat', sessionId: null }));
  };

  // 首次安装的随机口令还没改：整页接管，不给任何绕过入口。
  // 后端也会在改密前对所有 /v1/auth/* 之外的请求返回 403，这里是同一件事的两侧。
  if (auth.status === 'authenticated' && auth.user.must_change_password) {
    return (
      <ChangePasswordView
        username={auth.user.username}
        onSuccess={() => {
          setAuth({
            status: 'authenticated',
            user: { ...auth.user, must_change_password: false },
            testMode: auth.testMode,
          });
        }}
        onLogout={() => {
          void api.logout().catch(() => undefined);
          setAuth({ status: 'anonymous' });
        }}
      />
    );
  }

  return (
    <div className="app">
      <header className="app-header">
        {route.name === 'chat' ? (
          <button
            type="button"
            className="icon-button app-header__menu"
            aria-label="打开会话列表"
            onClick={() => setSidebarOpen(true)}
          >
            <MenuIcon />
          </button>
        ) : null}
        <button type="button" className="brand" onClick={goChat} aria-label="ling 首页">
          <span className="brand__dot" aria-hidden="true" />
          <span className="brand__text">ling</span>
        </button>
        <div className="app-header__spacer" />
        <nav className="app-header__nav" aria-label="主导航">
          <button
            type="button"
            className={`header-link ${route.name === 'settings' ? 'header-link--active' : ''}`}
            onClick={goSettings}
          >
            <SettingsIcon size={15} /> 设置
          </button>
          <button
            type="button"
            className={`header-link ${route.name === 'now' ? 'header-link--active' : ''}`}
            onClick={goLife}
          >
            <LifeIcon size={15} /> 生活
          </button>
          <button
            type="button"
            className={`header-link ${route.name === 'persona' ? 'header-link--active' : ''}`}
            onClick={goPersona}
          >
            <PersonaIcon size={15} /> 角色
          </button>
          <button type="button" className="header-link" onClick={handleLogout}>
            <LogoutIcon size={15} /> 退出
          </button>
        </nav>
        <span className="app-header__user">{auth.user.display_name || auth.user.username}</span>
      </header>

      {auth.testMode ? <TestModeBanner /> : null}

      <main className="app-main">
        {route.name === 'chat' ? (
          <ChatPage
            sessionId={route.sessionId}
            user={auth.user}
            sidebarOpen={sidebarOpen}
            onCloseSidebar={() => setSidebarOpen(false)}
            onTestMode={handleTestMode}
            onOpenSettings={goSettings}
            onOpenPersona={goPersona}
            onLogout={handleLogout}
          />
        ) : route.name === 'now' ? (
          <LifeNowPage onBack={goChat} onOpenSettings={goSettings} />
        ) : route.name === 'settings' ? (
          <SettingsHub onBack={goChat} onOpen={navigate} />
        ) : route.name === 'model' ? (
          <SettingsPage onBack={goSettings} onTestMode={handleTestMode} />
        ) : route.name === 'memory' ? (
          <MemoryPage onBack={goSettings} />
        ) : route.name === 'dialogue' ? (
          <DialoguePage onBack={goSettings} />
        ) : route.name === 'life' ? (
          <LifePage onBack={goSettings} />
        ) : route.name === 'privacy' ? (
          <ConsentPage onBack={goSettings} />
        ) : route.name === 'identity' ? (
          <IdentityPage onBack={goSettings} />
        ) : (
          <PersonaPage onBack={goSettings} />
        )}
      </main>
    </div>
  );
}
