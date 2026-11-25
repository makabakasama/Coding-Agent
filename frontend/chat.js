import {apiFetch as fetch, canAdministerInstance, projectMode} from './auth.js';
import {projectAccessHref} from './project-access.js';

const localDrafts = new Map();
const identifier = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
const states = {queued: '排队中', running: '正在处理', completed: '回复已保存', failed: '本轮失败', unknown: '运行状态待核实'};
const reasons = {
  CONTEXT_LIMIT: '会话内容超过本轮输入上限，可新建会话继续。',
  TURN_BUDGET: '本轮累计资源预算已用尽，已保留已取得的观察记录。',
  WORKER_HEARTBEAT_LOST: '暂时无法确认 Worker 状态，正在继续查询。',
  WORKER_INTERRUPTED: '本轮执行中断，已保留记录。',
  RECOVERY_EVIDENCE_INVALID: '中断后的材料校验未通过。',
  TASK_EXECUTION_UNKNOWN: '编码任务状态待核实，请查看任务详情。',
  TASK_RESULT_UNAVAILABLE: '编码任务已结束，但结果材料暂不可用。',
  TASK_UNAVAILABLE: '关联编码任务已不可用，请核对历史材料。',
  RESPONSE_SAVED: '回复与轮次记录已保存。',
};
const artifactNames = {context: '上下文', request: '模型请求', response: '模型响应', reply: '回复材料', trace: '调用过程', result: '轮次结果'};
const defaultExecution = () => ({kind: 'fix_bug', scope: '', workspaceMode: 'direct',
  allowTests: false, checkTargets: '', maxSteps: 30});
for (let index = 2; index <= 8; index += 1) {
  artifactNames[`request${index}`] = `第 ${index} 次模型请求`;
  artifactNames[`response${index}`] = `第 ${index} 次模型响应`;
}
const eventNames = {
  turn_queued: '已入队', turn_started: '开始处理', context_ready: '上下文已准备',
  model_input: '模型输入已保存', model_request_attempt: '模型请求',
  model_response: '模型响应已保存', model_usage: '模型用量', turn_finished: '本轮结束',
  agent_step: 'Agent 开始下一步', tool_decision: '调用读取工具',
  tool_observation: '读取结果已保存', agent_reply: 'Agent 已生成回复',
  task_linked: '已关联编码任务', task_started: '编码任务已入队',
  execution_unknown: '编码任务状态待核实',
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未记录' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function validConversation(value) {
  return value?.schemaVersion === 1 && identifier(value.conversationId) && typeof value.title === 'string'
    && integer(value.revision) && (value.activeTurnId === null || identifier(value.activeTurnId))
    && (value.workspaceRef === null || typeof value.workspaceRef === 'string');
}

function validTurn(value, conversationId) {
  return value?.schemaVersion === 1 && identifier(value.turnId) && value.conversationId === conversationId
    && Object.hasOwn(states, value.status) && typeof value.clientRequestId === 'string';
}

function loadDraft(conversationId) {
  if (localDrafts.has(conversationId)) return localDrafts.get(conversationId);
  let value = {draft: '', pending: null, mode: 'discuss', execution: defaultExecution()};
  try {
    const saved = JSON.parse(sessionStorage.getItem(`coding-agent-chat:${conversationId}`));
    if (saved && typeof saved.draft === 'string') {
      value.draft = saved.draft;
      if (saved.mode === 'execute') value.mode = 'execute';
      if (saved.execution && typeof saved.execution === 'object') {
        value.execution = {...defaultExecution(), ...saved.execution};
      }
      const pending = saved.pending;
      if (pending?.schemaVersion === 1 && ['discuss', 'execute'].includes(pending.mode)
          && typeof pending.clientRequestId === 'string'
          && /^[A-Za-z0-9_-]{1,128}$/.test(pending.clientRequestId)
          && integer(pending.expectedRevision) && typeof pending.content === 'string'
          && (pending.mode === 'discuss' || pending.execution && typeof pending.execution === 'object')) value.pending = pending;
    }
  } catch { /* Session storage can be unavailable. Keep an in-memory draft. */ }
  localDrafts.set(conversationId, value);
  return value;
}

function renderText(container, text) {
  // Build DOM nodes from text, including fenced code, without interpreting model HTML.
  let code = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('```')) {
      if (code) code = null;
      else {
        const pre = element('pre', 'chat-code');
        code = element('code');
        pre.append(code);
        container.append(pre);
      }
    } else if (code) code.append(document.createTextNode(line + '\n'));
    else container.append(element('div', 'chat-text-line', line || '\u00a0'));
  }
}

export function mountChat(root, conversationId = null, reviewTask = null) {
  let closed = false;
  let timer;
  let syncing = false;
  let sending = false;
  let conversation = null;
  let refreshRequired = true;
  let selectedTurnId = null;
  let manualSelection = false;
  let selectedTurn = null;
  let selectionEpoch = 0;
  let turnRequest = 0;
  let messageCursor = 0;
  let messagesBusy = false;
  let listBusy = false;
  let listCursor = null;
  let listHasMore = false;
  let eventBusy = false;
  let taskEventBusy = false;
  let artifactBusy = false;
  let artifactEpoch = 0;
  let artifactOptions = '';
  let streamSource = null;
  let streamTurnId = null;
  let streamPreview = null;
  let streamLabel = null;
  let streamContent = null;
  const savedAssistantTurns = new Set();
  const conversations = new Map();
  const eventPages = new Map();
  const taskEventPages = new Map();
  const artifactPages = new Map();
  const controller = new AbortController();
  const local = conversationId ? loadDraft(conversationId)
    : {draft: '', pending: null, mode: 'discuss', execution: defaultExecution()};

  root.classList.add('chat-page');
  root.innerHTML = `
    <div class="chat-shell">
      <section class="chat-history-panel" aria-label="会话列表">
        <div class="chat-list-heading"><h1>会话</h1><button type="button" id="chat-new">新建会话</button></div>
        <button type="button" class="chat-link-button" id="chat-refresh-list">刷新列表</button>
        <p id="chat-list-error" class="chat-error" role="status"></p>
        <div id="chat-conversations" class="chat-conversation-list"><p class="muted">正在读取会话…</p></div>
        <button type="button" class="secondary" id="chat-list-more" hidden>更多会话</button>
      </section>
      <section class="chat-workspace" aria-label="对话">
        <div class="chat-top"><div><h2 id="chat-title">开始一段对话</h2><p id="chat-workspace" class="muted">可选择项目后讨论需求。</p></div><a id="chat-project-tasks" hidden>项目需求与交付</a></div>
        <p id="chat-connection" class="chat-error" role="status"></p>
        <div id="chat-messages" class="chat-messages" role="log" aria-label="对话消息"><p class="chat-empty">新建会话后发送问题。</p></div>
        <button type="button" class="chat-link-button" id="chat-messages-more" hidden>继续加载消息</button>
        <details id="chat-run" class="chat-run" hidden>
          <summary><span id="chat-run-summary">轮次记录</span></summary>
          <div class="chat-run-body">
            <p id="chat-turn-status" role="status"></p><p id="chat-turn-reason"></p>
            <p id="chat-turn-id" class="hint"></p><a id="chat-task-link" hidden>查看编码任务、Diff 与检查结果 →</a><p id="chat-metrics" class="hint"></p>
            <details><summary>请求与处理过程</summary><ol id="chat-events" class="chat-events"></ol><button type="button" class="chat-link-button" id="chat-events-more" hidden>继续加载事件</button><p id="chat-events-error" class="chat-error"></p></details>
            <details id="chat-task-progress" hidden><summary>编码任务过程</summary><ol id="chat-task-events" class="chat-events"></ol><button type="button" class="chat-link-button" id="chat-task-events-more" hidden>继续加载任务事件</button><p id="chat-task-events-error" class="chat-error"></p></details>
            <details><summary>上下文与运行材料</summary><div class="chat-artifact-actions"><label>材料<select id="chat-artifact"></select></label><button type="button" class="secondary" id="chat-artifact-load">读取材料</button></div><p id="chat-artifact-note" class="hint"></p><pre id="chat-artifact-text" class="chat-artifact-text" hidden></pre></details>
          </div>
        </details>
        <form id="chat-composer" class="chat-composer" hidden>
          <label class="visually-hidden" for="chat-input">消息</label>
          <textarea id="chat-input" rows="3" placeholder="输入问题或讨论需求，支持多轮追问"></textarea>
          <label class="chat-execute-choice"><input type="checkbox" id="chat-execute">本轮授权修改项目</label>
          <div id="chat-execution-fields" class="chat-execution-fields" hidden>
            <label id="chat-review-label" hidden>本轮归属<select id="chat-review-base"><option value="">新需求</option></select></label>
            <label>修改类型<select id="chat-execution-kind"><option value="fix_bug">修复问题</option><option value="add_feature">新增功能</option></select></label>
            <label>允许修改的相对路径<input id="chat-execution-scope" placeholder="例如 src/parser.py；多个路径用逗号分隔"></label>
            <label>工作方式<select id="chat-execution-workspace"><option value="direct">直接修改当前项目</option><option value="worktree">在独立工作副本修改</option></select></label>
            <label>最多决策次数<input id="chat-execution-steps" type="number" min="1" max="50" value="30"></label>
            <label><input id="chat-execution-tests" type="checkbox">允许 Agent 运行指定测试</label>
            <label><input id="chat-execution-temporary-checks" type="checkbox">允许在本机运行 Agent 编写的临时 Python 自测</label>
            <label id="chat-execution-targets-label" hidden>允许的测试目标<input id="chat-execution-targets" placeholder="例如 tests/test_parser.py；多个用逗号分隔"></label>
            <p class="hint">发送即按本轮设置授权。修改范围和检查权限只对这条消息有效；执行结果会回到当前会话。</p>
          </div>
          <div class="chat-compose-actions"><span id="chat-count" class="hint"></span><button type="submit" id="chat-send" disabled>发送</button></div>
          <p id="chat-send-note" class="hint" role="status"></p><p id="chat-send-error" class="chat-error" role="alert"></p><p id="chat-storage-error" class="chat-error" role="status"></p>
        </form>
      </section>
    </div>
    <dialog id="chat-create-dialog" aria-label="新建会话"><form id="chat-create-form"><h2>新建会话</h2><label>会话名称<input name="title" maxlength="120" placeholder="新会话"></label><label>关联项目<select name="workspace"><option value="">不关联项目</option></select></label><p class="hint">关联项目后可按需读取文件；在对话中单独授权后可修改。</p><p id="chat-projects-error" class="chat-error"></p><p id="chat-create-error" class="chat-error" role="alert"></p><div class="actions"><button type="submit" id="chat-create-submit">创建会话</button><button type="button" class="secondary" id="chat-create-cancel">取消</button></div></form></dialog>`;

  const $ = selector => root.querySelector(selector);
  const input = $('#chat-input');
  const base = `/api/conversations/${conversationId}`;
  input.value = local.pending?.content ?? local.draft;
  const executeBox = $('#chat-execute');
  const execution = local.pending?.mode === 'execute' ? {
    kind: local.pending.execution.kind,
    scope: (local.pending.execution.allowedPaths || []).join(', '),
    workspaceMode: local.pending.execution.workspaceMode,
    maxSteps: local.pending.execution.maxSteps,
    allowTests: local.pending.execution.allowTests,
    allowTemporaryChecks: local.pending.execution.allowTemporaryChecks,
    checkTargets: (local.pending.execution.checkTargets || []).join(', '),
  } : local.execution;
  $('#chat-execution-kind').value = execution.kind;
  $('#chat-execution-scope').value = execution.scope;
  $('#chat-execution-workspace').value = execution.workspaceMode;
  $('#chat-execution-steps').value = execution.maxSteps;
  $('#chat-execution-tests').checked = execution.allowTests;
  $('#chat-execution-temporary-checks').checked = Boolean(execution.allowTemporaryChecks);
  $('#chat-execution-targets').value = execution.checkTargets;
  const splitPaths = value => value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);

  const reviewSelect = $('#chat-review-base');
  let loadedRequirements = false;
  async function loadRequirements(project) {
    if (loadedRequirements || !project || conversation?.requirement) return;
    loadedRequirements = true;
    try {
      const data = await request('/api/projects/' + encodeURIComponent(project) + '/tasks');
      if (closed) return;
      for (const item of data.tasks.filter(item => item.mode === 'ordinary'))
        reviewSelect.add(new Option('继续需求 · ' + item.title.slice(0,80), 'req:' + item.taskKey));
      const savedKey = local.pending?.execution?.requirementKey || local.execution.requirementKey;
      if (!parent && savedKey && [...reviewSelect.options].some(o => o.value === 'req:' + savedKey))
        reviewSelect.value = 'req:' + savedKey;
      $('#chat-review-label').hidden = false;
    } catch (error) {
      loadedRequirements = false;
      if (!closed) $('#chat-send-error').textContent = '需求列表读取失败：' + error.message;
    }
  }
  const parent = local.pending ? local.pending.execution?.parentTaskId : reviewTask;
  let reviewMode = null;
  if (parent) {
    reviewSelect.add(new Option('返工 · ' + parent, parent));
    reviewSelect.value = parent;
    $('#chat-review-label').hidden = false;
    request(`/api/tasks/${parent}`).then(task => {
      if (closed) return;
      [...reviewSelect.options].find(o => o.value === parent).textContent = '基于交付 · ' + task.contract.current.slice(0,80);
      reviewMode = task.contract.workspaceMode || 'worktree';
      if (reviewSelect.value !== parent) return;
      $('#chat-execution-workspace').value = reviewMode;
      if (!local.pending && !$('#chat-execution-scope').value)
        $('#chat-execution-scope').value = task.contract.allowedPaths.join(', ');
      updateComposer();
    }).catch(error => {if (!closed) $('#chat-send-error').textContent = error.message;});
  }
  if (reviewTask) input.placeholder = '写下对所选交付的意见；需要返工时勾选本轮授权修改项目';
  reviewSelect.onchange = () => {
    if (reviewSelect.value === parent && reviewMode) $('#chat-execution-workspace').value = reviewMode;
    updateComposer();
  };
  function executionOptions() {
    const selection = reviewSelect.value;
    const association = selection.startsWith('req:') ? {requirementKey:selection.slice(4)}
      : selection ? {parentTaskId:selection} : {};
    return {...association, kind: $('#chat-execution-kind').value,
      allowedPaths: splitPaths($('#chat-execution-scope').value),
      workspaceMode: $('#chat-execution-workspace').value,
      revision: 'HEAD', maxSteps: Number($('#chat-execution-steps').value),
      allowTests: $('#chat-execution-tests').checked,
      ...($('#chat-execution-tests').checked && $('#chat-execution-temporary-checks').checked
        ? {allowTemporaryChecks: true} : {}),
      checkTargets: $('#chat-execution-tests').checked ? splitPaths($('#chat-execution-targets').value) : []};
  }

  function saveExecutionSettings() {
    Object.assign(local.execution, {requirementKey:reviewSelect.value.startsWith('req:') ? reviewSelect.value.slice(4) : null,
      kind: $('#chat-execution-kind').value,
      scope: $('#chat-execution-scope').value, workspaceMode: $('#chat-execution-workspace').value,
      maxSteps: Number($('#chat-execution-steps').value),
      allowTests: $('#chat-execution-tests').checked,
      allowTemporaryChecks: $('#chat-execution-tests').checked && $('#chat-execution-temporary-checks').checked,
      checkTargets: $('#chat-execution-targets').value});
    persistDraft();
    updateComposer();
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {cache: 'no-store', signal: controller.signal, ...options});
    let data;
    try { data = await response.json(); }
    catch {
      const error = new Error('接口返回了无法识别的响应。');
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(data?.error?.message || '请求失败，请重新读取。');
      error.status = response.status;
      error.code = data?.error?.code;
      throw error;
    }
    return data;
  }

  function persistDraft() {
    try {
      sessionStorage.setItem(`coding-agent-chat:${conversationId}`, JSON.stringify(local));
      if (!closed) $('#chat-storage-error').textContent = '';
    } catch {
      if (!closed) $('#chat-storage-error').textContent = '浏览器暂时无法保存草稿和请求编号，刷新可能丢失这些内容。';
    }
  }

  function acknowledge(requestId) {
    if (local.pending?.clientRequestId !== requestId) return;
    if (local.draft === local.pending.content) local.draft = '';
    if (local.pending.mode === 'execute') local.mode = 'discuss';
    if (local.pending?.execution?.parentTaskId) {
      reviewSelect.value = '';
      if (reviewTask) history.replaceState(null, '', '#chat/' + conversationId);
    }
    local.pending = null;
    persistDraft();
    if (!closed) {
      input.value = local.draft;
      $('#chat-send-error').textContent = '';
    }
  }

  function updateComposer() {
    if (closed || !conversationId) return;
    $('#chat-composer').hidden = false;
    const savedText = local.pending?.content ?? local.draft;
    if (input.value !== savedText) input.value = savedText;
    const count = Array.from(input.value).length;
    const activeTurn = Boolean(conversation?.activeTurnId);
    const readOnly = conversation?.canPost === false;
    const mode = local.pending?.mode || local.mode;
    const changing = mode === 'execute';
    const settings = changing ? executionOptions() : null;
    executeBox.checked = changing;
    executeBox.disabled = readOnly || sending || Boolean(local.pending) || !conversation?.workspaceRef;
    $('#chat-execution-fields').hidden = !changing;
    $('#chat-execution-targets-label').hidden = !$('#chat-execution-tests').checked;
    for (const field of $('#chat-execution-fields').querySelectorAll('input,select')) {
      field.disabled = sending || Boolean(local.pending);
    }
    $('#chat-execution-temporary-checks').disabled ||= !$('#chat-execution-tests').checked;
    $('#chat-execution-workspace').disabled ||= Boolean(reviewSelect.value && !reviewSelect.value.startsWith('req:'));
    input.readOnly = readOnly || sending || Boolean(local.pending);
    $('#chat-count').textContent = `${count} / ${changing ? 4000 : 8000}`;
    $('#chat-send').textContent = local.pending && !sending ? '重试原请求' : sending ? '正在提交…'
      : changing ? (projectMode() ? '提交编码申请' : '授权并执行') : '发送';
    $('#chat-send').disabled = readOnly || sending || !conversation || refreshRequired
      || (!local.pending && (count === 0 || count > (changing ? 4000 : 8000) || !input.value.trim()
        || changing && (!conversation.workspaceRef || !settings.allowedPaths.length
          || Boolean(settings.parentTaskId && !reviewMode)
          || !Number.isInteger(settings.maxSteps) || settings.maxSteps < 1 || settings.maxSteps > 50
          || settings.allowTests && !settings.allowTemporaryChecks && !settings.checkTargets.length)));
    $('#chat-send-note').textContent = readOnly ? '你可以阅读该项目会话。' : local.pending
      ? '该请求的提交结果待确认，重试会沿用原请求编号。'
      : activeTurn ? '本轮正在处理；新消息会排队，待前面的轮次结束后依次处理。'
        : changing ? (projectMode() ? '实例管理员放行后才会排队执行。' : '本轮会按上面的修改范围与检查权限执行。')
          : '回复按整轮显示；关联项目后可按需读取文件。';
  }

  function renderList() {
    const nodes = [...conversations.values()]
      .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
      .map(record => {
        const link = element('a', 'chat-conversation');
        link.href = `#chat/${record.conversationId}`;
        if (record.conversationId === conversationId) link.setAttribute('aria-current', 'page');
        link.append(element('strong', '', record.title), element('small', '', formatTime(record.updatedAt)));
        return link;
      });
    $('#chat-conversations').replaceChildren(...(nodes.length ? nodes : [element('p', 'muted', '还没有会话。')]));
    $('#chat-list-more').hidden = !listHasMore;
  }

  async function loadList(reset = false) {
    if (listBusy || closed) return;
    listBusy = true;
    $('#chat-list-more').disabled = true;
    try {
      const cursor = reset ? null : listCursor;
      const query = new URLSearchParams({limit: '30'});
      if (cursor) query.set('cursor', cursor);
      const data = await request('/api/conversations?' + query);
      if (closed) return;
      if (!Array.isArray(data.conversations) || !data.conversations.every(validConversation)
          || typeof data.hasMore !== 'boolean'
          || data.hasMore && (!data.conversations.length || typeof data.nextCursor !== 'string'
            || !data.nextCursor || data.nextCursor === cursor)) throw new Error('会话列表格式异常。');
      if (reset) conversations.clear();
      for (const item of data.conversations) conversations.set(item.conversationId, item);
      if (conversation) conversations.set(conversationId, conversation);
      listCursor = data.nextCursor;
      listHasMore = data.hasMore;
      renderList();
      $('#chat-list-error').textContent = '';
    } catch (error) {
      if (!closed) $('#chat-list-error').textContent = error.message;
    } finally {
      listBusy = false;
      if (!closed) $('#chat-list-more').disabled = false;
    }
  }

  function appendMessage(message) {
    if (message.role === 'assistant') {
      savedAssistantTurns.add(message.turnId);
      if (message.turnId === streamTurnId) {
        streamPreview?.remove();
        streamPreview = null;
      }
    }
    const article = element('article', `chat-message chat-message-${message.role}`);
    const meta = element('div', 'chat-message-meta');
    const inspect = element('button', 'chat-link-button', '查看本轮');
    inspect.type = 'button';
    inspect.onclick = () => {
      manualSelection = true;
      selectTurn(message.turnId);
      $('#chat-run').open = true;
    };
    meta.append(element('strong', '', message.role === 'user' ? '你' : 'Agent'),
      element('time', '', formatTime(message.createdAt)), inspect);
    const content = element('div', 'chat-message-content');
    renderText(content, message.content);
    article.append(meta, content);
    $('#chat-messages').append(article);
  }

  function followStream(turnId) {
    if (!turnId || streamTurnId === turnId || closed) return;
    streamSource?.close();
    streamPreview?.remove();
    streamPreview = null;
    streamTurnId = turnId;
    const source = new EventSource(`${base}/turns/${turnId}/events?stream=1`);
    streamSource = source;
    let cursor = 0;
    source.onmessage = event => {
      if (closed || streamSource !== source) return;
      try {
        const value = JSON.parse(event.data);
        if (value.turnId !== turnId || !integer(value.seq) || value.seq <= cursor) return;
        cursor = value.seq;
        if (value.type !== 'model_text' || typeof value.text !== 'string'
            || savedAssistantTurns.has(turnId)) return;
        const area = $('#chat-messages');
        const follow = area.scrollHeight - area.scrollTop - area.clientHeight < 100;
        if (!streamPreview || !streamPreview.isConnected) {
          area.querySelector('.chat-empty')?.remove();
          streamPreview = element('article', 'chat-message chat-message-assistant');
          streamLabel = element('div', 'chat-message-meta', 'Agent · 正在生成（尚未保存）');
          streamContent = element('div', 'chat-message-content chat-text-line');
          streamPreview.append(streamLabel, streamContent);
          area.append(streamPreview);
        }
        // Text is provisional; render as plain text until the saved answer arrives.
        streamContent.textContent = value.text;
        streamLabel.textContent = 'Agent · 正在生成（尚未保存）';
        if (follow) area.scrollTop = area.scrollHeight;
      } catch {
        if (streamLabel) streamLabel.textContent = 'Agent · 接收异常，正在核对状态';
      }
    };
    source.addEventListener('stream_done', () => {
      if (streamSource !== source) return;
      source.close();
      streamSource = null;
      if (streamLabel && streamPreview?.isConnected)
        streamLabel.textContent = 'Agent · 生成已结束，正在核对保存结果';
      sync();
    });
    source.onerror = () => {
      if (closed || streamSource !== source) return;
      if (streamLabel && streamPreview?.isConnected)
        streamLabel.textContent = 'Agent · 连接恢复中，已有文本尚未确认完成';
      if (source.readyState === EventSource.CLOSED) {
        streamSource = null;
        streamTurnId = null;
        sync();
      }
      // EventSource reconnects with Last-Event-ID; no turn is resubmitted.
    };
  }

  async function loadMessages() {
    if (messagesBusy || closed) return;
    messagesBusy = true;
    $('#chat-messages-more').disabled = true;
    try {
      const after = messageCursor;
      const data = await request(`${base}/messages?afterSeq=${after}&limit=50`);
      if (closed) return;
      let cursor = after;
      if (!Array.isArray(data.messages) || typeof data.hasMore !== 'boolean') throw new Error('消息分页格式异常。');
      for (const message of data.messages) {
        if (!identifier(message.messageId) || !identifier(message.turnId) || message.conversationId !== conversationId
            || !['user', 'assistant'].includes(message.role) || typeof message.content !== 'string'
            || !integer(message.seq) || message.seq <= cursor) throw new Error('消息顺序或归属异常。');
        cursor = message.seq;
      }
      if (data.nextAfterSeq !== cursor || data.hasMore && cursor === after) throw new Error('消息游标异常。');
      const area = $('#chat-messages');
      const follow = area.scrollHeight - area.scrollTop - area.clientHeight < 100;
      if (!cursor) area.replaceChildren(element('p', 'chat-empty', '发送第一条消息，开始讨论。'));
      if (data.messages.length && messageCursor === 0) area.replaceChildren();
      for (const message of data.messages) appendMessage(message);
      messageCursor = cursor;
      $('#chat-messages-more').hidden = !data.hasMore;
      if (follow) area.scrollTop = area.scrollHeight;
    } finally {
      messagesBusy = false;
      if (!closed) $('#chat-messages-more').disabled = false;
    }
  }

  function selectTurn(turnId) {
    if (turnId !== selectedTurnId) {
      selectedTurnId = turnId;
      selectedTurn = null;
      selectionEpoch++;
      artifactEpoch++;
      artifactOptions = '';
      $('#chat-run').hidden = false;
      $('#chat-run-summary').textContent = '正在读取轮次…';
      $('#chat-turn-status').textContent = '';
      $('#chat-turn-reason').textContent = '';
      $('#chat-turn-id').textContent = '';
      $('#chat-task-link').hidden = true;
      $('#chat-metrics').textContent = '';
      $('#chat-artifact-load').disabled = true;
      $('#chat-events').replaceChildren();
      $('#chat-task-progress').hidden = true;
      $('#chat-task-events').replaceChildren();
      $('#chat-task-events-error').textContent = '';
      $('#chat-artifact-text').textContent = '';
      $('#chat-artifact-text').hidden = true;
      $('#chat-artifact-note').textContent = '';
    }
    return loadTurn();
  }

  function renderTurn(turn) {
    $('#chat-run').hidden = false;
    $('#chat-run-summary').textContent = `轮次记录 · ${states[turn.status]}`;
    $('#chat-turn-status').textContent = states[turn.status];
    $('#chat-turn-id').textContent = `轮次 ${turn.turnId}`;
    const taskLink = $('#chat-task-link');
    taskLink.hidden = !identifier(turn.taskId);
    if (!taskLink.hidden) taskLink.href = `#/tasks/${turn.taskId}`;
    $('#chat-task-progress').hidden = taskLink.hidden;
    $('#chat-turn-reason').textContent = turn.error?.message || reasons[turn.stopReason] || turn.stopReason || '';
    const metrics = turn.metrics;
    $('#chat-metrics').textContent = metrics
      ? `模型决策 ${metrics.modelDecisions ?? '未记录'} · HTTP 尝试 ${metrics.httpAttempts ?? '未记录'} · 输入 token ${metrics.usage?.inputTokens ?? '未提供'} · 输出 token ${metrics.usage?.outputTokens ?? '未提供'}${metrics.usageComplete ? '' : ' · 用量记录不完整'}` : '';
    const available = Object.keys(artifactNames).filter(name => turn.artifacts?.[name]
      && (canAdministerInstance() || name === 'reply'));
    const signature = turn.turnId + ':' + available.join(',');
    if (signature !== artifactOptions) {
      const previous = $('#chat-artifact').value;
      $('#chat-artifact').replaceChildren(...available.map(name => {
        const option = element('option', '', artifactNames[name]);
        option.value = name;
        return option;
      }));
      if (available.includes(previous)) $('#chat-artifact').value = previous;
      artifactOptions = signature;
    }
    $('#chat-artifact-load').disabled = !available.length || artifactBusy;
    if (!available.length) $('#chat-artifact-note').textContent = '本轮尚无可读取的登记材料。';
  }

  async function loadTurn() {
    if (!selectedTurnId || closed) return;
    const turnId = selectedTurnId;
    const epoch = selectionEpoch;
    const requestId = ++turnRequest;
    try {
      const turn = await request(`${base}/turns/${turnId}`);
      if (closed || epoch !== selectionEpoch || requestId !== turnRequest) return;
      if (!validTurn(turn, conversationId) || turn.turnId !== turnId) throw new Error('轮次状态格式或归属异常。');
      selectedTurn = turn;
      acknowledge(turn.clientRequestId);
      renderTurn(turn);
      if (identifier(turn.taskId)) await loadTaskEvents(turn.taskId);
      await loadEvents();
    } catch (error) {
      if (!closed && epoch === selectionEpoch && requestId === turnRequest) $('#chat-connection').textContent = error.message;
    }
  }

  async function loadEvents() {
    if (eventBusy || !selectedTurnId || closed) return;
    eventBusy = true;
    const turnId = selectedTurnId;
    const epoch = selectionEpoch;
    const page = eventPages.get(turnId) || {after: 0, events: []};
    try {
      const data = await request(`${base}/turns/${turnId}/events?after=${page.after}&limit=50`);
      if (closed || epoch !== selectionEpoch) return;
      let cursor = page.after;
      if (!Array.isArray(data.events) || typeof data.hasMore !== 'boolean') throw new Error('事件分页格式异常。');
      for (const event of data.events) {
        if (event.turnId !== turnId || !integer(event.seq) || event.seq <= cursor || typeof event.type !== 'string') throw new Error('事件归属或顺序异常。');
        cursor = event.seq;
      }
      if (data.nextAfter !== cursor || data.hasMore && cursor === page.after) throw new Error('事件游标异常。');
      page.events.push(...data.events);
      page.after = cursor;
      eventPages.set(turnId, page);
      $('#chat-events').replaceChildren(...page.events.filter(event => event.type !== 'model_text').map(event => {
        const item = element('li');
        const outcome = {started: '开始', response_received: '收到响应', failed: '失败'}[event.outcome] || event.outcome;
        const detail = event.type === 'model_request_attempt' ? ` · 第 ${event.attempt} 次 · ${outcome}`
          : event.type === 'tool_decision' ? ` · ${event.action}`
            : event.type === 'tool_observation' ? ` · ${event.action} · ${event.result?.ok ? '成功' : '失败'}` : '';
        item.append(element('span', '', (eventNames[event.type] || event.type) + detail), element('time', '', formatTime(event.at)));
        return item;
      }));
      $('#chat-events-more').hidden = !data.hasMore;
      $('#chat-events-error').textContent = '';
    } catch (error) {
      if (!closed && epoch === selectionEpoch) $('#chat-events-error').textContent = error.message;
    } finally { eventBusy = false; }
  }

  async function loadTaskEvents(taskId) {
    if (taskEventBusy || closed || !identifier(taskId)) return;
    taskEventBusy = true;
    const epoch = selectionEpoch;
    const page = taskEventPages.get(taskId) || {after: 0, events: []};
    try {
      const data = await request(`/api/tasks/${taskId}/execution/events?after=${page.after}&limit=50`);
      if (closed || epoch !== selectionEpoch || selectedTurn?.taskId !== taskId) return;
      let cursor = page.after;
      if (data.taskId !== taskId || !Array.isArray(data.events) || typeof data.hasMore !== 'boolean')
        throw new Error('任务事件格式异常。');
      for (const item of data.events) {
        if (!integer(item.seq) || item.seq <= cursor || typeof item.type !== 'string')
          throw new Error('任务事件顺序异常。');
        cursor = item.seq;
      }
      if (data.nextCursor !== cursor) throw new Error('任务事件游标异常。');
      page.after = cursor;
      page.events.push(...data.events);
      taskEventPages.set(taskId, page);
      $('#chat-task-events').replaceChildren(...page.events.map(item => {
        const detail = [item.action, item.feedback?.message].filter(Boolean).join(' · ');
        const entry = element('li');
        entry.append(element('span', '', `${item.type}${detail ? ` · ${detail}` : ''}`),
          element('time', '', formatTime(item.at)));
        return entry;
      }));
      $('#chat-task-events-more').hidden = !data.hasMore;
      $('#chat-task-events-error').textContent = '';
    } catch (error) {
      if (!closed && epoch === selectionEpoch) $('#chat-task-events-error').textContent = error.message;
    } finally {
      taskEventBusy = false;
    }
  }

  async function loadArtifact() {
    if (artifactBusy || !selectedTurn || closed) return;
    const turnId = selectedTurnId;
    const artifactId = $('#chat-artifact').value;
    if (!Object.hasOwn(artifactNames, artifactId)) return;
    const epoch = ++artifactEpoch;
    const key = `${turnId}/${artifactId}`;
    const page = artifactPages.get(key) || {offset: 0, text: '', sha256: null};
    artifactBusy = true;
    $('#chat-artifact-load').disabled = true;
    $('#chat-artifact-note').textContent = '正在读取材料…';
    try {
      const data = await request(`${base}/turns/${turnId}/artifacts/${artifactId}?offset=${page.offset}&limit=16000`);
      if (closed || epoch !== artifactEpoch) return;
      if (data.conversationId !== conversationId || data.turnId !== turnId || data.artifactId !== artifactId
          || typeof data.content !== 'string' || data.offset !== page.offset || !integer(data.nextOffset)
          || data.nextOffset !== page.offset + Array.from(data.content).length || !integer(data.totalChars)
          || data.nextOffset > data.totalChars || typeof data.hasMore !== 'boolean'
          || data.hasMore !== (data.nextOffset < data.totalChars) || data.hasMore && !data.content
          || data.integrityChecked !== true || !/^[0-9a-f]{64}$/.test(data.sha256)
          || page.sha256 && page.sha256 !== data.sha256) throw new Error('材料分页或哈希响应异常。');
      page.text += data.content;
      page.offset = data.nextOffset;
      page.sha256 = data.sha256;
      artifactPages.set(key, page);
      $('#chat-artifact-text').textContent = page.text;
      $('#chat-artifact-text').hidden = false;
      $('#chat-artifact-note').textContent = `${page.offset} / ${data.totalChars} 字符 · 已核对登记哈希 · 已应用凭据脱敏${data.hasMore ? ' · 可继续读取' : ' · 已读完'}`;
      $('#chat-artifact-load').textContent = data.hasMore ? '继续读取' : '重新读取';
      if (!data.hasMore) artifactPages.delete(key);
    } catch (error) {
      if (!closed && epoch === artifactEpoch) $('#chat-artifact-note').textContent = error.message;
    } finally {
      artifactBusy = false;
      if (!closed) $('#chat-artifact-load').disabled = !$('#chat-artifact').options.length;
    }
  }

  async function sync() {
    if (closed || syncing || !conversationId) return;
    syncing = true;
    clearTimeout(timer);
    try {
      const record = await request(base);
      if (closed) return;
      if (!validConversation(record) || record.conversationId !== conversationId
          || record.latestTurnId !== null && !identifier(record.latestTurnId)) throw new Error('会话状态格式异常。');
      conversation = record;
      refreshRequired = false;
      $('#chat-title').textContent = record.title;
      if (record.requirement) {
        const key = record.requirement.taskKey;
        $('#chat-project-tasks').hidden = false;
        $('#chat-project-tasks').href = '#projects/' + encodeURIComponent(record.workspaceRef)
          + '/tasks/' + encodeURIComponent(key);
        $('#chat-project-tasks').textContent = '返回需求与交付';
        reviewSelect.options[0].textContent = '继续当前需求';
        $('#chat-review-label').hidden = false;
      } else if (record.workspaceRef) {
        if (local.mode === "execute") loadRequirements(record.workspaceRef);
        $('#chat-project-tasks').hidden = false;
        $('#chat-project-tasks').href = '#projects/' + encodeURIComponent(record.workspaceRef) + '/tasks';
      }
      $('#chat-workspace').textContent = record.workspaceRef
        ? `关联项目 ${record.workspaceRef} · 讨论可读取，修改需本轮授权` : '未关联项目';
      $('#chat-connection').textContent = '';
      if (JSON.stringify(conversations.get(conversationId)) !== JSON.stringify(record)) {
        conversations.set(conversationId, record);
        renderList();
      }
      updateComposer();
      await loadMessages();
      if (record.activeTurnId) followStream(record.activeTurnId);
      else if (streamPreview?.isConnected && streamLabel) {
        streamLabel.textContent = 'Agent · 未保存为正式回复，请查看本轮状态';
      }
      const nextTurn = manualSelection ? selectedTurnId : record.activeTurnId || record.latestTurnId;
      if (nextTurn) await selectTurn(nextTurn);
    } catch (error) {
      if (!closed) {
        refreshRequired = true;
        $('#chat-connection').textContent = `${error.message} 正在重新查询。`;
      }
    } finally {
      syncing = false;
      if (!closed) {
        updateComposer();
        timer = setTimeout(sync, conversation?.activeTurnId ? 2000 : 5000);
      }
    }
  }

  input.oninput = () => {
    if (!local.pending) local.draft = input.value;
    persistDraft();
    updateComposer();
  };
  executeBox.onchange = () => {
    if (local.pending) return;
    if (executeBox.checked) loadRequirements(conversation?.workspaceRef);
    local.mode = executeBox.checked ? 'execute' : 'discuss';
    persistDraft();
    updateComposer();
  };
  for (const field of $('#chat-execution-fields').querySelectorAll('input,select')) {
    field.addEventListener('input', saveExecutionSettings);
    field.addEventListener('change', saveExecutionSettings);
  }
  $('#chat-composer').onsubmit = async event => {
    event.preventDefault();
    updateComposer();
    if (!conversationId || !conversation || refreshRequired || $('#chat-send').disabled || sending) return;
    sending = true;
    if (!local.pending) {
      local.draft = input.value;
      local.pending = {schemaVersion: 1, clientRequestId: crypto.randomUUID(),
        expectedRevision: conversation.revision, mode: local.mode, content: input.value};
      if (local.mode === 'execute') local.pending.execution = executionOptions();
      persistDraft();
    }
    const payload = local.pending;
    $('#chat-send-error').textContent = '';
    updateComposer();
    try {
      const data = await request(`${base}/turns`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const application = data.executionRequest;
      if (payload.mode === 'execute' && application?.kind === 'chat' && application.targetId === conversationId
          && identifier(application.requestId) && application.snapshot?.clientRequestId === payload.clientRequestId) {
        acknowledge(payload.clientRequestId);
        if (!closed) {
          const note = $('#chat-send-error');
          const link = element('a', '', '查看执行申请');
          link.href = projectAccessHref(application.projectId, application.requestId);
          note.replaceChildren('编码申请已提交。', link);
        }
        return;
      }
      if (!validTurn(data, conversationId) || data.clientRequestId !== payload.clientRequestId || !integer(data.conversationRevision)) throw new Error('提交响应未能核实，请重试原请求。');
      acknowledge(payload.clientRequestId);
      if (!closed) {
        manualSelection = false;
        refreshRequired = true;
        await selectTurn(data.turnId);
      }
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        if (local.pending?.clientRequestId === payload.clientRequestId) local.pending = null;
        persistDraft();
      }
      if (!closed) {
        refreshRequired = true;
        $('#chat-send-error').textContent = error.code === 'REVISION_CONFLICT' || error.code === 'TURN_ACTIVE'
          ? `${error.message} 草稿已保留，请核对会话后再发送。` : `${error.message}${local.pending ? ' 可重试原请求以确认结果。' : ''}`;
      }
    } finally {
      sending = false;
      if (!closed) { updateComposer(); await sync(); }
    }
  };
  $('#chat-messages-more').onclick = () => loadMessages().catch(error => {
    if (!closed) $('#chat-connection').textContent = error.message;
  });
  $('#chat-events-more').onclick = loadEvents;
  $('#chat-task-events-more').onclick = () => {
    if (selectedTurn?.taskId) loadTaskEvents(selectedTurn.taskId);
  };
  $('#chat-artifact-load').onclick = loadArtifact;
  $('#chat-artifact').onchange = () => {
    artifactEpoch++;
    $('#chat-artifact-text').hidden = true;
    $('#chat-artifact-text').textContent = '';
    $('#chat-artifact-note').textContent = '';
    $('#chat-artifact-load').textContent = '读取材料';
  };
  $('#chat-list-more').onclick = () => loadList();
  $('#chat-refresh-list').onclick = () => { loadList(true); sync(); };
  $('#chat-new').onclick = () => $('#chat-create-dialog').showModal();
  $('#chat-create-cancel').onclick = () => $('#chat-create-dialog').close();
  $('#chat-create-form').onsubmit = async event => {
    event.preventDefault();
    const button = $('#chat-create-submit');
    if (button.disabled) return;
    button.disabled = true;
    const form = event.currentTarget;
    try {
      const data = await request('/api/conversations', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: form.elements.title.value.trim() || '新会话', workspaceRef: form.elements.workspace.value || null})});
      if (!validConversation(data)) throw new Error('创建结果未能核实，请刷新列表核对。');
      if (!closed) location.hash = `#chat/${data.conversationId}`;
    } catch (error) {
      if (!closed) { $('#chat-create-error').textContent = `${error.message} 可刷新会话列表核对。`; loadList(true); }
    } finally { if (!closed) button.disabled = false; }
  };

  async function loadProjects() {
    try {
      const data = await request('/api/projects');
      if (closed) return;
      if (!Array.isArray(data.projects)) throw new Error('项目列表格式异常。');
      for (const project of data.projects) {
        if (project.canCreateTask === false || project.accessEnabled === false) continue;
        if (project.canCreateTask === false || typeof project.projectId !== 'string' || typeof project.name !== 'string') continue;
        const option = element('option', '', project.name);
        option.value = project.projectId;
        $('#chat-create-form').elements.workspace.append(option);
      }
    } catch (error) { if (!closed) $('#chat-projects-error').textContent = `${error.message} 仍可创建无项目会话。`; }
  }

  loadList(true);
  loadProjects();
  if (conversationId) {
    $('#chat-messages').replaceChildren(element('p', 'chat-empty', '正在读取消息…'));
    updateComposer();
    sync();
  }
  return () => {
    closed = true;
    streamSource?.close();
    clearTimeout(timer);
    controller.abort();
    root.classList.remove('chat-page');
  };
}
