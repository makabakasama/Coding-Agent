let session;
let serviceError = '';
let onSessionLost = () => {};

export function projectMode() {
  return session?.projectAccess === true;
}

export function canAdministerInstance() {
  return !projectMode() || session?.account?.admin === true;
}

async function errorMessage(response, fallback) {
  try {
    return (await response.json()).error?.message || fallback;
  } catch {
    return fallback;
  }
}

export async function initializeAuth(onLost) {
  onSessionLost = onLost;
  try {
    const response = await window.fetch('/api/auth/me', {cache: 'no-store'});
    if (response.status === 401) {
      session = null;
    } else if (!response.ok) {
      serviceError = await errorMessage(response, '会话服务暂不可用');
    } else {
      session = await response.json();
    }
  } catch {
    serviceError = '无法连接工作台，请稍后重试';
  }
}

export async function apiFetch(url, options = {}) {
  const currentSession = session;
  const headers = new Headers(options.headers);
  const method = (options.method || 'GET').toUpperCase();
  if (currentSession?.csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers.set('X-CSRF-Token', currentSession.csrfToken);
  }
  const response = await window.fetch(url, {...options, headers});
  if (response.status === 401 && currentSession?.authRequired && session === currentSession) {
    session = null;
    onSessionLost();
  }
  return response;
}

export function renderAuth(app) {
  const account = session?.account;
  const canUseWorkbench = session?.authRequired === false || account?.admin === true || projectMode();
  const accountLabel = document.querySelector('#account-label');
  const signOut = document.querySelector('#account-logout');
  const authAlert = document.querySelector('#account-alert');
  document.querySelector('nav').hidden = !canUseWorkbench;
  accountLabel.textContent = account?.login || (canUseWorkbench ? '本地单用户' : '账号登录');
  document.querySelector('#workbench-mode').textContent = canUseWorkbench && !account ? '本地单用户工作台' : '本地账号工作台';
  signOut.hidden = !account;
  authAlert.textContent = '';
  signOut.onclick = async () => {
    signOut.disabled = true;
    authAlert.textContent = '';
    try {
      const response = await apiFetch('/api/auth/logout', {method: 'POST'});
      if (response.ok) {
        location.reload();
      } else if (response.status !== 401) {
        authAlert.textContent = await errorMessage(response, '退出失败，请重试');
      }
    } catch {
      authAlert.textContent = '退出结果暂无法确认，请重试';
    } finally {
      signOut.disabled = false;
    }
  };
  if (canUseWorkbench) return true;

  document.querySelector('#breadcrumb').textContent = '工作台 / 账号';
  if (account) {
    app.innerHTML = '<section class="card account-card"><h1>已登录</h1><p>项目成员权限开放前，任务、仓库和历史记录由管理员操作。</p></section>';
    return false;
  }
  if (serviceError) {
    app.innerHTML = '<section class="card account-card"><h1>暂时无法访问工作台</h1><p role="alert"></p><button type="button">重试</button></section>';
    app.querySelector('[role="alert"]').textContent = serviceError;
    app.querySelector('button').onclick = () => location.reload();
    return false;
  }
  app.innerHTML = `<section class="card account-card">
    <h1>登录工作台</h1>
    <form>
      <label>登录名<input name="login" maxlength="64" autocomplete="username" required></label>
      <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
      <button type="submit">登录</button>
      <p role="alert"></p>
    </form>
  </section>`;
  const form = app.querySelector('form');
  const message = form.querySelector('[role="alert"]');
  const submit = form.querySelector('button');
  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    message.textContent = '';
    const values = Object.fromEntries(new FormData(form));
    try {
      const response = await window.fetch('/api/auth/login', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)
      });
      if (response.ok) location.reload();
      else message.textContent = await errorMessage(response, '登录失败，请重试');
    } catch {
      message.textContent = '无法确认登录结果，请刷新页面后重试';
    } finally {
      form.elements.password.value = '';
      submit.disabled = false;
    }
  };
  return false;
}
