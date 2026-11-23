// Actual headless Chromium, loopback API, independent Worker and scripted model.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function eventually(check, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await pause(50);
  }
  throw new Error('Acceptance condition timed out');
}

async function connectDebugger(url) {
  const socket = new WebSocket(url);
  const waiting = new Map();
  const exceptions = [];
  let sequence = 0;
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data);
    if (data.method === 'Runtime.exceptionThrown') exceptions.push(data.params.exceptionDetails);
    if (!data.id) return;
    const pending = waiting.get(data.id);
    if (!pending) return;
    waiting.delete(data.id);
    clearTimeout(pending.timer);
    if (data.error) pending.reject(new Error(data.error.message));
    else pending.resolve(data.result);
  });
  socket.addEventListener('close', () => {
    for (const pending of waiting.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error('Browser debugger closed'));
    }
    waiting.clear();
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    if (socket.readyState !== WebSocket.OPEN) {
      reject(new Error('Browser debugger is not connected'));
      return;
    }
    const id = ++sequence;
    const timer = setTimeout(() => {
      waiting.delete(id);
      reject(new Error(`Browser debugger timeout: ${method}`));
    }, method === 'Page.navigate' ? 30000 : 15000);
    waiting.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
  await call('Runtime.enable');
  await call('Page.enable');
  return {
    call, exceptions, close: () => socket.close(),
    evaluate: async expression => {
      const result = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + (result.result.description || ''));
      return result.result.value;
    },
  };
}

test('Chat browser acceptance', {skip: !process.env.CODING_AGENT_TEST_DATABASE_URL || !process.env.CHAT_TEST_CHROME, timeout: 150000}, async t => {
  const root = path.join(process.env.CHAT_ACCEPTANCE_CASES_ROOT, 'ui');
  const artifacts = process.env.CHAT_ACCEPTANCE_ARTIFACTS || root;
  fs.mkdirSync(root, {recursive: true});
  fs.mkdirSync(artifacts, {recursive: true});
  const python = process.env.CHAT_TEST_PYTHON || path.resolve('.venv/Scripts/python.exe');
  const children = [];
  const descriptors = [];
  const clients = [];

  function launch(executable, args, label) {
    const log = fs.openSync(path.join(root, label + '.log'), 'w');
    descriptors.push(log);
    const child = spawn(executable, args, {windowsHide: true, stdio: ['ignore', log, log]});
    children.push(child);
    return child;
  }

  t.after(async () => {
    if (clients.length) await Promise.race([clients[0].call('Browser.close').catch(() => {}), pause(3000)]);
    for (const client of clients) client.close();
    fs.writeFileSync(path.join(root, 'stop'), '');
    await Promise.race([
      Promise.all(children.slice(0, 2).map(child => new Promise(resolve => {
        if (child.exitCode !== null) resolve();
        else child.once('exit', resolve);
      }))),
      pause(5000),
    ]);
    for (const child of children) if (child.exitCode === null) child.kill();
    for (const descriptor of descriptors) fs.closeSync(descriptor);
  });

  launch(python, ['-m', 'tests.chat_fixture', 'server', '--root', root], 'server');
  const server = await eventually(() => {
    const target = path.join(root, 'server.json');
    if (!fs.existsSync(target)) return false;
    try { return JSON.parse(fs.readFileSync(target, 'utf8')); } catch { return false; }
  });
  launch(python, ['-m', 'tests.chat_fixture', 'worker', '--root', root], 'worker');
  const origin = `http://127.0.0.1:${server.port}`;
  const profile = path.join(root, 'browser-profile');
  launch(process.env.CHAT_TEST_CHROME, ['--headless', '--disable-gpu', '--in-process-gpu', '--disable-software-rasterizer',
    '--no-sandbox', '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], 'browser');
  const debugPort = await eventually(() => {
    const target = path.join(profile, 'DevToolsActivePort');
    if (!fs.existsSync(target)) return false;
    return Number(fs.readFileSync(target, 'utf8').split('\n')[0]) || false;
  });
  const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then(response => response.json());
  const initialTarget = targets.find(target => target.type === 'page' && target.url === 'about:blank')
    || targets.find(target => target.type === 'page');
  const client = await connectDebugger(initialTarget.webSocketDebuggerUrl);
  clients.push(client);
  await client.call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await client.call('Page.navigate', {url: origin});
  const ready = c => eventually(() => c.evaluate(`Boolean(document.querySelector('#chat-input') && !document.querySelector('#chat-send').disabled && document.querySelector('#chat-input').value)`));
  const fill = (c, text) => c.evaluate(`document.querySelector('#chat-input').value=${JSON.stringify(text)};document.querySelector('#chat-input').dispatchEvent(new Event('input',{bubbles:true}));`);
  const submit = c => c.evaluate(`document.querySelector('#chat-composer').requestSubmit();`);
  const snapshot = async name => {
    const image = await client.call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
    fs.writeFileSync(path.join(artifacts, name + '.png'), Buffer.from(image.data, 'base64'));
  };
  let cid;
  let secondId;

  await t.test('creates a conversation and renders real Worker replies safely', async () => {
    await eventually(() => client.evaluate(`Boolean(document.querySelector('#chat-new'))`));
    await client.evaluate(`document.querySelector('#chat-composer').requestSubmit();`);
    assert.equal(client.exceptions.length, 0, 'Submitting before conversation load must be harmless');
    await client.evaluate(`document.querySelector('#chat-new').click();document.querySelector('#chat-create-form').elements.title.value='Chat 验收';document.querySelector('#chat-create-form').requestSubmit();`);
    cid = await eventually(() => client.evaluate(`/^[0-9a-f]{32}$/.test(location.hash.slice(6)) ? location.hash.slice(6) : false`));
    await eventually(() => client.evaluate(`Boolean(document.querySelector('#chat-input') && !document.querySelector('#chat-input').readOnly)`));
    await fill(client, '[SLOW] <script>window.__xss=1</script>\n```python\nprint("ok")\n```');
    await ready(client);
    await submit(client);
    await eventually(() => client.evaluate(`document.querySelector('#chat-send-note')?.textContent.includes('本轮正在处理')`));
    await fill(client, '本轮期间排队的追问');
    await ready(client);
    assert.equal(await client.evaluate(`document.querySelector('#chat-send').disabled`), false);
    await submit(client);
    await eventually(() => client.evaluate(`document.querySelectorAll('.chat-message-assistant').length===2`));
    assert.equal(await client.evaluate('window.__xss'), undefined);
    assert.equal(await client.evaluate(`document.querySelector('#chat-input').value`), '');
    assert.ok(await client.evaluate(`document.querySelector('.chat-message-assistant .chat-code')!==null`));
    assert.match(await client.evaluate(`document.querySelectorAll('.chat-message-assistant')[1].textContent`), /用户消息数=2/);
    await snapshot('chat-desktop');
  });

  await t.test('restores unsent draft and history after reload', async () => {
    await fill(client, '刷新后保留草稿😀');
    await client.call('Page.reload');
    await eventually(() => client.evaluate(`document.querySelector('#chat-input')?.value==='刷新后保留草稿😀' && document.querySelectorAll('.chat-message-assistant').length===2`));
  });

  await t.test('retries the original request after a discarded POST response', async () => {
    await client.evaluate(`window.__originalFetch=window.fetch;window.__posted=[];window.__holdReads=true;window.fetch=async function(url,options={}){
      if(options.method==='POST' && String(url).endsWith('/turns')){
        window.__posted.push(JSON.parse(options.body));
        const response=await window.__originalFetch(url,options);
        if(window.__posted.length===1)throw new TypeError('Injected committed response loss');
        return response;
      }
      if(window.__holdReads && String(url).includes('/turns/') && String(url).split('/').length===6)throw new TypeError('Injected state-read failure');
      return window.__originalFetch(url,options);
    };`);
    await fill(client, '原请求重试验收');
    await ready(client);
    await submit(client);
    await eventually(() => client.evaluate(`document.querySelector('#chat-send')?.textContent==='重试原请求' && !document.querySelector('#chat-send').disabled`));
    await submit(client);
    await eventually(() => client.evaluate('window.__posted.length===2'));
    const ids = await client.evaluate('window.__posted.map(item=>item.clientRequestId)');
    assert.equal(ids[0], ids[1]);
    await client.evaluate('window.__holdReads=false;window.fetch=window.__originalFetch;');
    await eventually(() => client.evaluate(`document.querySelectorAll('.chat-message-assistant').length===3`));
    const messages = await fetch(`${origin}/api/conversations/${cid}/messages`).then(response => response.json());
    assert.equal(messages.messages.filter(message => message.role === 'user' && message.content === '原请求重试验收').length, 1);
  });

  await t.test('shows failure and reads registered context material', async () => {
    await fill(client, '[FAIL]');
    await ready(client);
    await submit(client);
    await eventually(() => client.evaluate(`document.querySelector('#chat-turn-status')?.textContent==='本轮失败'`));
    await client.evaluate(`document.querySelector('#chat-run').open=true;document.querySelector('#chat-artifact').value='context';document.querySelector('#chat-artifact-load').click();`);
    await eventually(() => client.evaluate(`document.querySelector('#chat-artifact-note')?.textContent.includes('已核对登记哈希')`));
    assert.match(await client.evaluate(`document.querySelector('#chat-artifact-text').textContent`), /messagesSha256/);
    await snapshot('chat-failure-material');
  });

  await t.test('switches conversations while a previous response arrives late', async () => {
    const second = await fetch(`${origin}/api/conversations`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: '并发会话'})}).then(response => response.json());
    secondId = second.conversationId;
    await client.evaluate(`window.__originalFetch=window.fetch;window.__lateReady=false;window.fetch=async function(url,options){
      const response=await window.__originalFetch(url,options);
      if(!window.__lateReady && String(url).startsWith('/api/conversations/${cid}/messages')){
        window.__lateReady=true;return new Promise(resolve=>window.__releaseLate=()=>resolve(response));
      }return response;
    };document.querySelector('#chat-refresh-list').click();`);
    await eventually(() => client.evaluate(`window.__lateReady && Boolean(document.querySelector('a[href="#chat/${secondId}"]'))`));
    await client.evaluate(`document.querySelector('a[href="#chat/${secondId}"]').click();`);
    await eventually(() => client.evaluate(`document.querySelector('#chat-title')?.textContent==='并发会话'`));
    await client.evaluate('window.__releaseLate();window.fetch=window.__originalFetch;');
    await pause(150);
    assert.equal(await client.evaluate(`document.querySelector('#chat-title').textContent`), '并发会话');
    assert.equal(await client.evaluate(`document.querySelectorAll('.chat-message').length`), 0);
  });

  await t.test('two tabs preserve the rejected draft without duplicate execution', async () => {
    const target = await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(origin + '/#chat/' + secondId)}`, {method: 'PUT'}).then(response => response.json());
    const other = await connectDebugger(target.webSocketDebuggerUrl);
    clients.push(other);
    await eventually(() => other.evaluate(`Boolean(document.querySelector('#chat-input'))`));
    await fill(client, '[SLOW] 标签页一');
    await fill(other, '[SLOW] 标签页二');
    await Promise.all([ready(client), ready(other)]);
    await Promise.all([submit(client), submit(other)]);
    const loser = await eventually(async () => {
      for (const page of [client, other]) if (await page.evaluate(`document.querySelector('#chat-send-error')?.textContent.includes('草稿已保留')`)) return page;
      return false;
    });
    assert.match(await loser.evaluate(`document.querySelector('#chat-input').value`), /标签页/);
    await eventually(async () => {
      const data = await fetch(`${origin}/api/conversations/${secondId}/messages`).then(response => response.json());
      return data.messages.some(message => message.role === 'assistant');
    });
    const data = await fetch(`${origin}/api/conversations/${secondId}/messages`).then(response => response.json());
    assert.equal(data.messages.filter(message => message.role === 'user').length, 1);
    other.close();
    await fetch(`http://127.0.0.1:${debugPort}/json/close/${target.id}`);
  });

  await t.test('mobile layout fits viewport and coding entry remains reachable', async () => {
    await eventually(() => client.evaluate(`document.querySelector('#chat-turn-status')?.textContent==='回复已保存' && document.querySelectorAll('.chat-message-assistant').length===1`));
    await client.call('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: false});
    await pause(100);
    assert.ok(await client.evaluate('document.documentElement.scrollWidth<=innerWidth+1'));
    await snapshot('chat-mobile');
    await client.evaluate(`document.querySelector('nav a[href="#task"]').click();`);
    await eventually(() => client.evaluate(`Boolean(document.querySelector('#simple-task'))`));
    assert.ok(await client.evaluate(`Boolean(document.querySelector('#browse-target'))`));
    assert.equal(client.exceptions.length, 0, JSON.stringify(client.exceptions));
  });

  fs.writeFileSync(path.join(artifacts, 'browser-fixture.json'), JSON.stringify({
    source: 'scripted_model', database: 'dedicated_temporary_database', actualBrowser: 'headless_chromium',
    browserVersion: await client.call('Browser.getVersion'),
    conversationIds: [cid, secondId],
    injectedFaults: ['discarded successful POST response', 'client-side turn-state read rejection', 'held late GET response', 'scripted Provider failure'],
  }, null, 2));
});
