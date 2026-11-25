import {apiFetch, canAdministerInstance} from './auth.js';

const roles = {observer: '观察者', developer: '开发者', maintainer: '维护者', admin: '实例管理员'};
const decisions = {pending: '待放行', approved: '已放行', rejected: '已拒绝'};

export const projectAccessHref = (project, request) =>
  '#projects/' + encodeURIComponent(project) + '/access' + (request ? '/' + encodeURIComponent(request) : '');

export const executionAuditHref = (project, execution) =>
  '#projects/' + encodeURIComponent(project) + '/audit/' + encodeURIComponent(execution);

function el(tag, value) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  return node;
}

async function request(path, body) {
  const response = await apiFetch(path, body === undefined ? {cache: 'no-store'} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || '请求失败');
  return data;
}

function link(label, href) {
  const node = el('a', label);
  node.href = href;
  return node;
}

export async function mountExecutionAudit(root, project, execution, active) {
  const note = el('p', '正在读取审计记录…');
  note.setAttribute('role', 'status');
  root.replaceChildren(el('h1', '执行审计'), el('p', execution),
    link('返回项目审批', projectAccessHref(project)), note);
  try {
    const url = '/api/projects/' + encodeURIComponent(project) + '/executions/' + encodeURIComponent(execution) + '/audit';
    const {events} = await request(url);
    if (!active()) return;
    const labels = {request_submitted:'提交申请', request_approved:'放行', request_rejected:'拒绝',
      task_queued:'任务入队', chat_queued:'会话入队', task_claimed:'领取任务', chat_claimed:'领取会话轮次',
      task_authorization_passed:'任务授权复核通过', task_authorization_denied:'任务授权复核拒绝',
      chat_authorization_passed:'会话授权复核通过', chat_authorization_denied:'会话授权复核拒绝',
      task_finished:'任务终态已登记', chat_finished:'会话终态已登记',
      review_accepted:'接收交付', review_changes_requested:'要求修改'};
    const card = el('section');
    card.className = 'card';
    for (const event of events) {
      const item = el('article');
      item.className = 'event';
      item.append(el('h3', labels[event.type] || event.type),
        el('p', [new Date(event.at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}),
                  event.login || event.accountId, event.workerId && 'Worker ' + event.workerId,
                  event.reason].filter(Boolean).join(' · ')));
      if (event.artifactSha256) item.append(el('p', '补丁 SHA256 ' + event.artifactSha256));
      card.append(item);
    }
    note.textContent = '';
    root.append(card);
  } catch (error) {
    if (active()) note.textContent = error.message;
  }
}

function action(button, note, run) {
  const click = async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    note.textContent = '';
    try {
      await run();
    } catch (error) {
      note.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  if (button.form) button.form.onsubmit = click;
  else button.onclick = click;
}

export async function mountProjectAccess(root, project, requestId, active) {
  const projectPath = '/api/projects/' + encodeURIComponent(project);
  const heading = el('h1', '项目成员与执行审批');
  const returnLink = link('查看需求与执行进度', '#projects/' + encodeURIComponent(project) + '/tasks');
  const status = el('p', '正在读取项目…');
  status.setAttribute('role', 'status');
  root.replaceChildren(heading, returnLink, status);
  try {
    const {projects} = await request('/api/projects');
    if (!active()) return;
    const configuration = projects.find(item => item.projectId === project);
    if (!configuration) throw new Error('项目不存在');
    heading.textContent = configuration.name + ' · 项目成员与执行审批';
    status.textContent = '项目角色：' + (roles[configuration.role] || '未登记');
    const reload = () => active() && mountProjectAccess(root, project, requestId, active);
    if (!configuration.accessEnabled) {
      if (!canAdministerInstance() || !configuration.modes?.includes('ordinary')) return;
      const form = el('form');
      form.className = 'card';
      form.innerHTML = '<label>首位维护者的登录名<input name="maintainer" required maxlength="64"></label><button>启用项目成员授权</button><p role="status"></p>';
      root.append(form);
      action(form.querySelector('button'), form.querySelector('p'), async () => {
        await request(projectPath + '/enable', {maintainer: form.elements.maintainer.value});
        await reload();
      });
      return;
    }
    if (requestId) {
      const record = await request(projectPath + '/execution-requests/' + encodeURIComponent(requestId));
      if (active()) root.append(requestCard(record, projectPath, true, reload, active));
      return;
    }
    if (configuration.role === 'maintainer' || canAdministerInstance()) {
      const {members} = await request(projectPath + '/members');
      if (!active()) return;
      const form = el('form');
      form.className = 'card';
      const list = el('ul');
      for (const member of members) {
        list.append(el('li', member.login + ' · ' + roles[member.role] + (member.enabled ? '' : ' · 已禁用')));
      }
      form.append(el('h2', '成员'), list);
      form.insertAdjacentHTML('beforeend', '<label>登录名<input name="login" required maxlength="64"></label><label>设为<select name="role"><option value="observer">观察者</option><option value="developer">开发者</option><option value="maintainer">维护者</option><option value="">移除成员</option></select></label><button>保存成员变更</button><p role="status"></p>');
      root.append(form);
      action(form.querySelector('button'), form.querySelector('p'), async () => {
        await request(projectPath + '/members', {login: form.elements.login.value, role: form.elements.role.value || null});
        await reload();
      });
    }
    const panel = el('section');
    panel.className = 'card';
    panel.innerHTML = '<h2>执行申请</h2><label>申请状态<select><option value="pending">待放行</option><option value="approved">已放行</option><option value="rejected">已拒绝</option></select></label><button type="button" class="secondary">刷新</button><p role="status"></p><div></div>';
    root.append(panel);
    const select = panel.querySelector('select');
    const note = panel.querySelector('p');
    const list = panel.querySelector('div');
    const load = async () => {
      const selected = select.value;
      const payload = await request(projectPath + '/execution-requests?status=' + selected);
      if (!active() || !panel.isConnected || selected !== select.value) return;
      list.replaceChildren(...payload.requests.map(record => requestCard(record, projectPath, false, load, active)));
      note.textContent = payload.requests.length ? '' : '当前没有此状态的申请。';
    };
    action(panel.querySelector('button'), note, load);
    select.onchange = () => load().catch(error => { note.textContent = error.message; });
    await load();
  } catch (error) {
    if (active()) status.textContent = error.message;
  }
}

function requestCard(record, projectPath, detail, refresh, active) {
  const item = el('article');
  item.className = 'event';
  const snapshot = record.snapshot;
  const contract = snapshot.contract || snapshot.execution;
  item.append(el('h3', decisions[record.status] + ' · ' + (record.kind === 'chat' ? 'Chat 编码' : '普通任务')));
  item.append(el('p', contract.current || '执行申请'));
  const recordLink = record.kind === 'chat' ? '#chat/' + record.targetId : '#/tasks/' + record.targetId;
  item.append(link('查看任务或会话', recordLink));
  if (!detail) {
    item.append(el('span', ' · '), link('申请详情', projectAccessHref(record.projectId, record.requestId)));
    return item;
  }
  const summary = el('dl');
  summary.className = 'kv';
  const values = [
    ['申请人', record.requesterLogin], ['需求', snapshot.taskKey || snapshot.requirementKey || snapshot.reviewBase?.taskKey],
    ['期望结果', contract.expected], ['允许修改', contract.allowedPaths?.join(' / ')],
    ['工作空间', contract.workspaceMode], ['固定版本', contract.baseCommit || snapshot.frozenBaseCommit || contract.revision],
    ['检查', contract.allowTests ? contract.checkTargets?.join(' / ') || '按契约检查' : '未授权'],
    ['返工来源', snapshot.reviewBase?.parentTaskId],
    ['审批人', record.decidedByLogin], ['说明', record.status === 'pending' ? '等待实例管理员放行' : decisions[record.status]],
  ];
  for (const [name, value] of values) summary.append(el('dt', name), el('dd', value || '未提供'));
  item.append(summary);
  const note = el('p');
  note.setAttribute('role', 'status');
  if (record.status === 'pending' && canAdministerInstance()) {
    const actions = el('div');
    actions.className = 'actions';
    for (const [decision, label] of [['approve', '放行并入队'], ['reject', '拒绝']]) {
      const button = el('button', label);
      actions.append(button);
      action(button, note, async () => {
        await request(projectPath + '/execution-requests/' + record.requestId, {decision});
        await refresh();
      });
    }
    item.append(actions);
  }
  item.append(note);
  if (record.execution?.executionId) item.append(link('查看授权与执行审计',
    executionAuditHref(record.projectId, record.execution.executionId)));
  if (record.execution?.statusUrl) {
    request(record.execution.statusUrl).then(result => {
      if (active() && item.isConnected) note.textContent = '执行进度：' + ({
        queued: '等待 Worker', running: '执行中', finalizing: '整理中', completed: '已完成',
        terminated: '已终止', failed: '失败', unknown: '待核实',
      }[result.status] || result.status);
    }).catch(error => { if (active() && item.isConnected) note.textContent = error.message; });
  }
  return item;
}
