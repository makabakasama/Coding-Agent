import {apiFetch as fetch, canAdministerInstance, initializeAuth, projectMode, renderAuth} from './auth.js';
import {executionAuditHref, mountExecutionAudit, mountProjectAccess, projectAccessHref} from './project-access.js';

const app = document.querySelector('#app');
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const demo = {
 id: 'DEMO-001', project: '示例 Python 项目', revision: 'demo-baseline（虚构版本）',
 current: '空字符串输入时返回 None。', expected: '空字符串输入时返回空列表。',
 scope: 'src/parser.py', allowTests: true, targets: 'tests/test_parser.py',
 diff: '--- a/src/parser.py\n+++ b/src/parser.py\n@@ -2,3 +2,3 @@\n def parse(text):\n     if not text:\n-        return None\n+        return []',
 output: '$ python -m pytest tests/test_parser.py\ncollected 2 items\ntests/test_parser.py ..\n2 passed in 0.04s\nexit code: 0'
};
let draft = null;
const badge = '<span class="badge demo">演示数据</span>';
function heading(kicker, title, subtitle, extra = '') { return `<div class="title-row"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p class="muted">${subtitle}</p></div>${extra}</div>`; }
function contract(data) { return `<dl class="kv"><dt>项目</dt><dd>${escape(data.project)}</dd><dt>代码起点</dt><dd>${escape(data.revision)}</dd><dt>任务说明</dt><dd>${escape(data.current)}</dd><dt>目标与交付</dt><dd>${escape(data.expected)}</dd><dt>允许修改</dt><dd>${escape(data.scope||'不允许修改文件')}</dd><dt>测试权限</dt><dd>${data.allowTests ? '允许运行固定检查' : '不允许运行测试'}</dd><dt>检查目标</dt><dd>${escape(data.allowTests ? data.targets : '暂不检查')}</dd></dl>`; }
function history() { return `<section class="card"><div class="section-head"><h2>任务记录</h2><span class="badge">离线示例 · 3 条</span></div><p class="hint">以下均为虚构演示，不含真实历史运行。选择不同终止情况，审查相应结果。</p><table class="history"><thead><tr><th>任务</th><th>来源</th><th>终止原因</th><th>实际检查</th><th></th></tr></thead><tbody>${[['finished','DEMO-001','模型主动结束','通过 · 2 项'],['interrupted','DEMO-002','中断','未执行'],['failed','DEMO-003','准备失败','未执行']].map(([id,name,reason,check])=>`<tr><td>${name} · 空输入处理</td><td>演示数据</td><td>${reason}</td><td>${check}</td><td><a href="#result/${id}">审查结果 →</a></td></tr>`).join('')}</tbody></table></section>`; }
function demoTask() {
 app.innerHTML = heading('DEMO CONTRACT', '演示任务表单', '约定目标、修改边界和检查方式，再进入独立副本。') + `<div class="grid"><form id="task-form"><section class="card"><div class="section-head"><h2>目标与起点</h2><span class="number">01 / 02</span></div><div class="two"><label>项目<select name="project"><option>示例 Python 项目</option></select><span class="hint">项目发现接口未接通，当前仅提供示例。</span></label><label>执行目录<select name="workspaceMode"><option value="worktree">隔离副本（worktree）</option><option value="direct">直接修改当前仓库</option></select><span class="hint">直接模式读取当前文件，修改会立即出现在项目目录；同一仓库串行执行。</span></label><label>起始版本<input name="revision" required placeholder="填写准确 commit 或版本引用" maxlength="160"></label></div><label>当前行为<textarea name="current" required placeholder="什么输入或操作触发了什么问题？" maxlength="4000"></textarea></label><label>期望行为<textarea name="expected" required placeholder="修改后应该出现什么可观察的结果？" maxlength="4000"></textarea></label></section><section class="card"><div class="section-head"><h2>操作范围与检查</h2><span class="number">02 / 02</span></div><label>允许修改的文件 / 目录<textarea name="scope" required placeholder="每行一个仓库相对路径，例如 src/parser.py" maxlength="4000"></textarea></label><label class="check"><input type="checkbox" name="allowTests" checked>允许运行测试</label><label id="targets-label">固定检查目标<input name="targets" required placeholder="例如 tests/test_parser.py" maxlength="2000"><span class="hint">这里只约定目标，不会执行输入的命令。</span></label><p id="no-check" hidden class="hint">本次明确暂不检查；结果页需要保留“未执行”结论。</p></section><div class="actions"><button type="submit">预览任务约定 →</button><button type="button" class="secondary" id="fill-demo">填入演示需求</button></div></form><div><section class="card"><div class="eyebrow">WORKFLOW</div><h2 style="margin-top:16px">每一步都有可审查的产物</h2><p><strong>01　任务约定</strong><br><span class="muted">目标、起点、范围与检查目标</span></p><p><strong>02　独立副本与执行</strong><br><span class="muted">准备状态、工具记录与实际反馈</span></p><p><strong>03　结果交付</strong><br><span class="muted">修改、检查结果和未完成事项</span></p></section><div class="notice">真实启动尚未接通。预览与下载约定不会创建工作副本或调用模型。请勿在需求中填写密钥。</div><a class="button secondary" href="#execution">浏览执行页演示 →</a></div></div><div style="margin-top:30px">${history()}</div><dialog id="review" aria-label="任务约定核对"><div class="eyebrow">REVIEW CONTRACT</div><h2 style="margin-top:14px">提交前核对</h2><div id="review-content"></div><div class="notice">此约定仅保存在当前页面内存中。下载可留存；刷新后不保留。真实启动未接通。</div><div class="actions"><button id="download">下载任务约定</button><button disabled>准备副本并启动 · 未接通</button><button class="secondary" id="close-review">返回修改</button></div></dialog>`;
 const form = document.querySelector('form');
 function toggleTests() { const enabled = form.elements.allowTests.checked; form.elements.targets.disabled = !enabled; form.elements.targets.required = enabled; document.querySelector('#targets-label').hidden = !enabled; document.querySelector('#no-check').hidden = enabled; }
 form.elements.allowTests.addEventListener('change', toggleTests);
 document.querySelector('#fill-demo').onclick = () => { for (const key of ['project','revision','current','expected','scope','targets']) form.elements[key].value = demo[key]; form.elements.allowTests.checked = true; toggleTests(); };
 form.onsubmit = event => { event.preventDefault(); const data = new FormData(form); draft = Object.fromEntries(data); draft.allowTests = form.elements.allowTests.checked; document.querySelector('#review-content').innerHTML = contract(draft); document.querySelector('#review').showModal(); };
 document.querySelector('#close-review').onclick = () => document.querySelector('#review').close();
 document.querySelector('#download').onclick = () => { const url = URL.createObjectURL(new Blob([JSON.stringify({schemaVersion:1, kind:'local-draft', ...draft}, null, 2)], {type:'application/json'})); const a = document.createElement('a'); a.href=url; a.download='task-contract.json'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); };
}
function execution() {
 app.innerHTML = heading('EXECUTION RECORD', '让每一步都有迹可循', 'DEMO-001 · 修复空字符串输入的返回值', badge) + `<div class="notice">演示数据：这是静态记录预览，没有运行中的 Agent。运行存活状态：<strong>状态暂无法确认</strong>。页面不会用停更推断失败或运行中。</div><div class="steps"><div class="phase done"><strong>01　准备</strong>示例：独立副本已准备</div><div class="phase done"><strong>02　执行</strong>示例：模型主动结束</div><div class="phase done"><strong>03　整理</strong>示例：结果已整理</div></div><div class="grid"><section class="card"><div class="section-head"><h2>工具步骤</h2><span class="badge">静态记录 · 无实时订阅</span></div>${[['01','读取代码','read_file · src/parser.py','观察到空输入分支返回 None。','def parse(text):\n    if not text:\n        return None'],['02','修改代码','replace_lines · src/parser.py','示例补丁修改了一个业务文件。',demo.diff],['03','运行固定检查','pytest · tests/test_parser.py','最近实际反馈（演示）：退出码 0，2 项通过。',demo.output]].map(([n,title,meta,note,output])=>`<article class="event"><div class="meta">STEP ${n} · ${meta}</div><h3>${title}</h3><p class="muted">${note}</p><details><summary>展开完整示例输出</summary><pre>${escape(output)}</pre></details></article>`).join('')}<a href="#result/finished" class="button">审查结果与证据 →</a></section><div><section class="card"><h2>运行概况</h2><dl class="kv"><dt>任务编号</dt><dd>DEMO-001</dd><dt>运行来源</dt><dd>演示数据</dd><dt>当前活动</dt><dd>无实时活动信息</dd><dt>已用步数</dt><dd>4 / 12（含结束决策）</dd><dt>最近更新</dt><dd>无实时更新时间；静态示例</dd><dt>副本入口</dt><dd>未提供 · 演示不创建副本</dd><dt>日志入口</dt><dd>未提供 · 仅有下方内嵌示例输出</dd></dl></section><section class="card"><h2>本轮任务约定</h2>${contract(demo)}</section></div></div>`;
}
function result(variant) {
 const interrupted = variant === 'interrupted', failed = variant === 'failed'; const complete = !interrupted && !failed;
 const id = failed ? 'DEMO-003' : interrupted ? 'DEMO-002' : 'DEMO-001';
 app.innerHTML = heading('RESULT & EVIDENCE', '结束之后，审查实际产物', `${id} · 空输入处理`, badge) + `<div class="notice">演示数据，所有代码、输出和版本均为虚构示例。本页展示普通任务检查，不属于 #5998 / #10525 的独立 G/R/P 实验验收。<p><a class="button" href="#tasks">查看真实任务记录 →</a></p></div><section class="card"><div class="metrics"><div class="metric">终止原因<strong>${failed ? '准备失败' : interrupted ? '中断' : '模型主动结束'}</strong><span class="hint">${failed ? '示例：无法解析起始版本' : interrupted ? '示例：收到停止请求' : '结束决策只表示模型停止'}</span></div><div class="metric">实际检查<strong>${complete ? '通过 · 2 项' : '未执行'}</strong><span class="hint">${complete ? '仅覆盖 tests/test_parser.py' : '没有测试输出，不能判断修复效果'}</span></div><div class="metric">修改文件<strong>${complete ? '1 个文件' : '无保存的修改'}</strong><span class="hint">${complete ? 'src/parser.py · +1 / −1' : '未提供候选补丁'}</span></div></div></section><div class="grid"><div><section class="card"><h2>修改文件与 Diff</h2>${complete ? `<h3>src/parser.py</h3><pre>${escape(demo.diff)}</pre>` : '<div class="empty">无修改记录；没有可审查的 diff。</div>'}</section><section class="card"><h2>检查命令与实际输出</h2>${complete ? `<p class="hint">检查通过仅覆盖这两个示例用例，不表示完整功能验收。</p><details open><summary>python -m pytest tests/test_parser.py · 退出码 0</summary><pre>${escape(demo.output)}</pre></details>` : '<div class="empty">未执行检查；没有命令退出码或实际输出。</div>'}</section><section class="card"><h2>未完成事项</h2><p>${complete ? '未运行完整测试套件；未执行独立副本补丁验收；未验证其他输入场景。' : failed ? '需要确认可解析的起始版本，完成副本准备后才能执行。' : '执行已中断；需要审查已有记录，并决定是否新建后续任务。'}</p><h3>Agent 总结 <span class="badge">说明</span></h3><p class="muted">${complete ? '示例说明：将空输入的返回值调整为空列表。总结仅用于解释，检查输出和 diff 是另外的证据。' : '未提供 Agent 总结。记录缺失不补写为成功。'}</p></section></div><div><section class="card"><h2>起点与任务约定</h2>${contract(demo)}</section><section class="card"><h2>工作副本与日志</h2><p class="muted">副本：未提供（演示不创建副本）</p><p class="muted">完整运行日志：未提供</p><p class="hint">当前只有页面中的示例片段，不能据此复建真实运行。</p></section><div class="actions"><a class="button" href="#task">新建任务</a><a class="button secondary" href="#history">演示记录</a></div></div></div>${history()}`;
}
// M1: real task contracts; demo routes never invoke the creation endpoint.
let pageEpoch = 0;
let pendingCreate = false;
let uncertainCreate = false;
let realDraft = {projectId:'', revision:'', current:'', expected:'', scope:'', targets:'', allowTests:true};
const fieldNames = {projectId:'项目',revision:'起始版本',current:'当前行为',expected:'期望行为',allowedPaths:'允许修改路径',checkTargets:'检查目标',allowTests:'测试权限',maxSteps:'步数上限'};
function errorText(error) {
 const e = error?.error;
 return e ? `${e.message || '请求失败'}${e.code ? `（${e.code}）` : ''}${Array.isArray(e.fields) ? e.fields.map(f=>`\n${fieldNames[f.field] || f.field}：${f.message}`).join('') : ''}` : '后端返回了无法识别的响应。';
}
function validRecord(r) {
 const c=r?.contract;
 return r?.schemaVersion===1 && /^[a-f0-9]{32}$/.test(r.taskId) && r.recordType==='actual' && r.status==='created' && typeof r.createdAt==='string' && !Number.isNaN(Date.parse(r.createdAt)) && c && (/^[a-f0-9]{40,64}$/.test(c.baseCommit)||(c.sourceKind==='directory'&&c.baseCommit===null)) && typeof c.projectId==='string' && typeof c.requestedRevision==='string' && typeof c.current==='string' && typeof c.expected==='string' && Array.isArray(c.allowedPaths) && c.allowedPaths.every(p=>typeof p==='string') && typeof c.allowTests==='boolean' && Array.isArray(c.checkTargets) && c.checkTargets.every(p=>typeof p==='string') && Number.isInteger(c.maxSteps);
}
function task() {
 const epoch=pageEpoch, active=()=>pageEpoch===epoch;
 app.innerHTML=heading('NEW TASK','创建任务','选择本机目标，描述你希望完成的事情。')+`<form id="simple-task"><section class="card"><label>目标文件或目录<div class="path-choice"><input name="targetPath" placeholder="点击浏览选择文件或目录"><button type="button" class="secondary" id="browse-target">浏览…</button></div><span class="hint">从工作目录浏览选择，也可填写其他本机路径。</span></label><label>或选择已有项目<select name="projectId"><option value="">可选，选择文件或目录即可</option></select></label><label>任务描述<textarea name="instruction" required maxlength="4000" placeholder="例如：给新建任务表单增加可编辑的最大执行步数，默认 50。"></textarea></label><label>任务方式<select name="kind"><option value="add_feature">修改代码</option><option value="read_code">只读分析</option><option value="fix_bug">修复问题</option></select></label><details><summary>高级设置（可选）</summary><label>执行方式<select name="workspaceMode"><option value="direct">直接修改当前文件</option><option value="worktree">Git 隔离副本</option></select></label><label>步数上限<input name="maxSteps" type="number" min="1" max="50" step="1" value="50"></label><label>Git 版本<input name="revision" placeholder="默认 HEAD；普通文件使用当前内容"></label><label>额外验收说明<textarea name="expected" maxlength="4000" placeholder="可选，默认按任务描述完成并报告结果"></textarea></label><label>修改范围<textarea name="allowedPaths" placeholder="可选，每行一个相对路径；默认仅所选文件或目录"></textarea></label><label class="check"><input name="allowTests" type="checkbox">允许运行指定检查</label><label class="check"><input name="allowTemporaryChecks" type="checkbox">允许在本机运行 Agent 编写的临时 Python 自测</label><label>检查目标<textarea name="checkTargets" placeholder="可选，仅在允许运行检查时填写 pytest 目标"></textarea></label><p><a href="#advanced-task">打开完整契约表单</a></p></details><p id="simple-error" role="alert"></p><button id="preview-simple">预览并保存</button></section></form><dialog id="simple-review"><h2>确认任务</h2><pre id="simple-review-content"></pre><p>保存后可在任务详情提交执行。直接模式会修改所选目标。</p><p id="simple-review-error" role="alert"></p><button id="save-simple">确认保存</button><button id="cancel-simple" class="secondary">返回修改</button></dialog><dialog id="target-picker" aria-label="选择目标文件或目录"><h2>选择目标文件或目录</h2><p id="picker-path" class="hint"></p><div class="actions"><button type="button" class="secondary" id="picker-up">上一级</button><button type="button" id="picker-directory">选择当前目录</button></div><p id="picker-error" role="alert"></p><div id="picker-entries" class="picker-entries"></div><p class="hint" id="picker-note"></p><button type="button" class="secondary" id="picker-cancel">取消</button></dialog>`;
 const form=document.querySelector('#simple-task'),dialog=document.querySelector('#simple-review');
 if(!canAdministerInstance())form.elements.targetPath.closest('label').hidden=true;
 const save=document.querySelector('#save-simple'),preview=document.querySelector('#preview-simple');
 let payload=null;
 const picker=document.querySelector('#target-picker');
 let parentPath=null;
 async function browse(path) {
  const entries=document.querySelector('#picker-entries');
  document.querySelector('#picker-error').textContent='';
  entries.textContent='正在读取…';
  try {
   const url='/api/local-files'+(path?'?path='+encodeURIComponent(path):'');
   const response=await fetch(url,{cache:'no-store'});
   const data=await response.json();
   if(!response.ok)throw Error(errorText(data));
   if(!active()||!picker.open)return;
   parentPath=data.parent;
   document.querySelector('#picker-path').textContent=data.path;
   document.querySelector('#picker-up').disabled=!parentPath;
   document.querySelector('#picker-directory').onclick=()=>{form.elements.targetPath.value=data.path;picker.close();};
   document.querySelector('#picker-note').textContent=data.truncated?'只显示前 500 项，请进入子目录继续选择。':'';
   entries.replaceChildren();
   for(const item of data.entries){
    const row=document.createElement('div');
    row.className='picker-row';
    const button=document.createElement('button');
    button.type='button';button.className='picker-entry';
    button.textContent=(item.type==='directory'?'📁 ':'📄 ')+item.name;
    button.onclick=()=>item.type==='directory'?browse(item.path):(form.elements.targetPath.value=item.path,picker.close());
    row.append(button);
    if(item.type==='directory'){
     const select=document.createElement('button');
     select.type='button';select.className='secondary picker-select';
     select.textContent='选择此文件夹';
     select.onclick=()=>{form.elements.targetPath.value=item.path;picker.close();};
     row.append(select);
    }
    entries.append(row);
   }
  }catch(error){if(active()&&picker.open){entries.textContent='';document.querySelector('#picker-error').textContent=error.message||'文件夹读取失败';}}
 }
 document.querySelector('#browse-target').onclick=()=>{picker.showModal();browse();};
 document.querySelector('#picker-up').onclick=()=>{if(parentPath)browse(parentPath);};
 document.querySelector('#picker-cancel').onclick=()=>picker.close();
 const lines=text=>text.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
 form.onsubmit=event=>{
  event.preventDefault();if(pendingCreate||uncertainCreate)return;
  const f=form.elements;
  if(!f.targetPath.value.trim()&&!f.projectId.value){document.querySelector('#simple-error').textContent='请填写目标路径或选择已有项目。';return;}
  if(!f.instruction.value.trim()){document.querySelector('#simple-error').textContent='请填写任务描述。';return;}
  const maxSteps=Number(f.maxSteps.value);
  if(!Number.isInteger(maxSteps)||maxSteps<1||maxSteps>50){document.querySelector('#simple-error').textContent='步数上限必须是 1–50 的整数。';return;}
  payload={instruction:f.instruction.value.trim(),kind:f.kind.value,workspaceMode:f.workspaceMode.value,maxSteps};
  if(f.targetPath.value.trim())payload.targetPath=f.targetPath.value.trim();else payload.projectId=f.projectId.value;
  if(f.revision.value.trim())payload.revision=f.revision.value.trim();
  if(f.expected.value.trim())payload.expected=f.expected.value.trim();
  if(lines(f.allowedPaths.value).length)payload.allowedPaths=lines(f.allowedPaths.value);
  payload.allowTests=f.allowTests.checked;payload.checkTargets=f.allowTests.checked?lines(f.checkTargets.value):[];
  if(payload.allowTests && f.kind.value!=='read_code' && f.allowTemporaryChecks.checked)payload.allowTemporaryChecks=true;
  document.querySelector('#simple-review-content').textContent=`目标：${payload.targetPath||payload.projectId}\n任务：${payload.instruction}\n方式：${payload.workspaceMode==='direct'?'直接修改当前文件':'Git 隔离副本'}\n步数上限：${maxSteps}\n检查：${payload.allowTests?'允许指定检查':'本次不运行测试'}${payload.allowTemporaryChecks?'\n允许在本机执行临时 Python 自测':''}`;
  document.querySelector('#simple-review-error').textContent='';dialog.showModal();
 };
 document.querySelector('#cancel-simple').onclick=()=>{if(!pendingCreate)dialog.close();};
 save.onclick=async()=>{
  if(!payload||pendingCreate||uncertainCreate)return;pendingCreate=true;save.disabled=true;preview.disabled=true;
  try{
   const response=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await response.json();
   if(!response.ok){pendingCreate=false;if(active()){save.disabled=false;preview.disabled=false;document.querySelector('#simple-review-error').textContent=errorText(data);}return;}
   if(response.status!==201||!validRecord(data))throw Error('保存响应无法确认');
   pendingCreate=false;if(active())location.hash='/tasks/'+data.taskId;
  }catch{pendingCreate=false;uncertainCreate=true;if(active())document.querySelector('#simple-review-error').textContent='保存结果待确认，请从任务记录核对，不自动重发。';}
 };
 fetch('/api/projects',{cache:'no-store'}).then(async response=>{
  if(!response.ok)throw Error('项目列表不可用');const data=await response.json();if(!active())return;
  if(!Array.isArray(data.projects))throw Error('项目列表格式异常');
  const placeholder=canAdministerInstance()?'可选，选择文件或目录即可':'请选择项目';
  form.elements.projectId.innerHTML=`<option value="">${placeholder}</option>`+data.projects.filter(p=>p.canCreateTask!==false).map(p=>`<option value="${escape(p.projectId)}">${escape(p.name)}</option>`).join('');
 }).catch(()=>{if(active())document.querySelector('#simple-error').textContent=canAdministerInstance()
  ?'已有项目暂无法加载，仍可填写本机路径创建。':'已有项目暂无法加载。';});
}

function advancedTask() {
 const epoch=pageEpoch;
 app.innerHTML=heading('TASK CONTRACT','选择任务方式','普通任务可编辑约定；已有实验项目使用固定问题和验收流程。')+`<div class="grid"><form id="real-task"><section class="card"><h2>普通任务：目标与起点</h2><label>可创建任务的项目<select name="projectId" required disabled><option value="">正在加载项目…</option></select><span class="hint">固定实验项目显示在右侧，沿用已有 Spec 与独立验收。</span></label><p id="project-state" role="status">正在查询可用项目…</p><button type="button" class="secondary" id="reload-projects">重新加载项目</button><label>执行目录<select name="workspaceMode"><option value="worktree">隔离副本（worktree）</option><option value="direct">直接修改当前仓库</option></select><span class="hint">直接模式读取当前文件，修改会立即出现在项目目录；同一仓库串行执行。</span></label><label>起始版本<input name="revision" required maxlength="160" placeholder="准确 commit 或版本引用，如 main"></label><label>当前行为<textarea name="current" required maxlength="4000"></textarea></label><label>期望行为<textarea name="expected" required maxlength="4000"></textarea></label></section><section class="card"><h2>操作范围与检查</h2><label>允许修改的文件 / 目录<textarea name="scope" required placeholder="每行一项，使用仓库相对路径及 / 分隔符"></textarea></label><label class="check"><input name="allowTests" type="checkbox">允许运行测试</label><label class="check"><input name="allowTemporaryChecks" type="checkbox">允许在本机运行 Agent 编写的临时 Python 自测</label><label id="real-targets">固定检查目标<textarea name="targets" placeholder="每行一个 pytest 路径或 文件.py::测试节点"></textarea><span class="hint">填写目标，不填写 python / pytest 命令或选项。</span></label><p id="real-no-check" hidden>本次明确暂不检查。</p></section><div id="save-error" class="notice" role="alert" hidden style="white-space:pre-wrap"></div><div class="actions"><button id="preview-real" disabled>预览任务约定 →</button></div></form><div><section class="card"><h2>已有实验项目</h2><p class="hint">选择项目后进入固定需求和历史轮次，再明确确认是否启动新的真实模型实验。</p><div id="experiment-projects"><p>正在读取实验项目…</p></div></section><section class="card"><h2>普通任务保存什么</h2><p>保存目标、允许范围和检查约定，解析并固定准确 commit。隔离模式使用提交快照，直接模式使用当前文件。</p><p class="muted">保存不代表已执行、检查通过或完成独立实验验收。步数预算默认为 50。</p></section><div class="notice">请勿填写密钥。实验项目不接受网页自定义检查命令，使用后端登记源码、环境和冻结 G/R。</div><a href="#demo" class="button secondary">打开独立演示表单</a><p class="hint">演示流程不保存真实任务。</p><p><a class="button secondary" href="#tasks">普通任务记录 →</a></p><a href="#projects" class="button secondary">全部项目实验 →</a></div></div><dialog id="real-review" aria-label="真实任务约定核对"><h2>确认并保存任务</h2><div id="real-review-content"></div><p>保存后固定约定；隔离模式使用提交快照，直接模式使用当前文件。准确 commit 将在保存成功后展示；修改需求须新建任务。</p><div id="review-error" role="alert" style="white-space:pre-wrap"></div><div class="actions"><button id="save-real">确认保存（不启动）</button><button class="secondary" id="edit-real">返回修改</button></div></dialog>`;
 const form=document.querySelector('#real-task'), select=form.elements.projectId, preview=document.querySelector('#preview-real'), dialog=document.querySelector('#real-review'), save=document.querySelector('#save-real');
 form.insertAdjacentHTML('afterbegin','<section class="card"><label>任务类型<select name="kind"><option value="fix_bug">修复 Bug</option><option value="read_code">阅读代码</option><option value="add_feature">补充小功能</option></select></label><p class="hint">阅读任务填写背景问题和期望分析，不修改文件；功能任务填写现状和新增行为。</p></section>');
 form.elements.kind.value=realDraft.kind||'fix_bug';
 form.elements.workspaceMode.value=realDraft.workspaceMode||'worktree';
 let projects=[], projectRequest=0, payload;
 const active=()=>pageEpoch===epoch;
 for(const name of ['revision','current','expected','scope','targets']) form.elements[name].value=realDraft[name];
 form.elements.allowTests.checked=realDraft.allowTests;
 form.elements.allowTemporaryChecks.checked=Boolean(realDraft.allowTemporaryChecks);
 function remember(){for(const name of ['projectId','revision','current','expected','scope','targets','kind','workspaceMode']) realDraft[name]=form.elements[name].value; realDraft.allowTests=form.elements.allowTests.checked;realDraft.allowTemporaryChecks=realDraft.allowTests&&form.elements.kind.value!=='read_code'&&form.elements.allowTemporaryChecks.checked;}
 function toggleKind(){
  const kind=form.elements.kind.value,reading=kind==='read_code';
  const texts={read_code:['要弄清的问题','希望得到的分析','例如：任务提交后，代码如何创建并执行 Agent？','例如：按调用顺序解释，标出相关文件与函数。'],fix_bug:['缺陷表现与复现条件','修复后的预期行为','什么输入或操作触发了什么错误？','修复后应该出现什么结果？'],add_feature:['新增功能需求','验收条件','希望增加什么能力，使用场景是什么？','什么输入应产生什么输出，哪些行为需要保留？']}[kind];
  for(const [index,name] of ['current','expected'].entries()){
   const input=form.elements[name];input.closest('label').firstChild.textContent=texts[index];input.placeholder=texts[index+2];
  }
  const revision=form.elements.revision;revision.closest('label').firstChild.textContent='代码起点';revision.placeholder='默认 HEAD；也可填写分支或准确 commit';if(!revision.value)revision.value='HEAD';
  form.elements.scope.disabled=reading;form.elements.scope.required=!reading;
  form.elements.scope.closest('label').hidden=reading;
 }
 form.elements.kind.onchange=()=>{toggleKind();remember();};toggleKind();
 function toggle(){const enabled=form.elements.allowTests.checked; form.elements.targets.disabled=!enabled;form.elements.allowTemporaryChecks.disabled=!enabled||form.elements.kind.value==='read_code';form.elements.targets.required=enabled&&(!form.elements.allowTemporaryChecks.checked||form.elements.kind.value==='read_code');document.querySelector('#real-targets').hidden=!enabled;document.querySelector('#real-no-check').hidden=enabled;}
 function buttons(){preview.disabled=!projects.length || pendingCreate || uncertainCreate;save.disabled=pendingCreate || uncertainCreate || !projects.length;document.querySelector('#edit-real').disabled=pendingCreate;save.textContent=pendingCreate?'正在保存…':'确认保存（不启动）';}
 function showError(message){document.querySelector('#save-error').hidden=false;document.querySelector('#save-error').textContent=message;document.querySelector('#review-error').textContent=message;}
 if(uncertainCreate)showError('保存结果暂无法确认，勿直接重复提交。后端可能已保存成功；本轮没有历史列表或幂等查询能力。请先由维护者核对记录。');
 if(pendingCreate)showError('上一保存请求仍在等待响应，请勿重复提交。');
 form.addEventListener('input',remember);form.addEventListener('change',remember);form.elements.allowTests.onchange=()=>{toggle();remember();};form.elements.allowTemporaryChecks.onchange=toggle;form.elements.kind.addEventListener('change',toggle);toggle();buttons();
 async function loadProjects(){const request=++projectRequest;projects=[];select.disabled=true;select.innerHTML='<option value="">正在加载项目…</option>';const experimentHost=document.querySelector('#experiment-projects');experimentHost.innerHTML='<p>正在读取实验项目…</p>';buttons();const state=document.querySelector('#project-state');state.textContent='正在查询可用项目…';try{const response=await fetch('/api/projects',{cache:'no-store'});const data=await response.json();if(!active()||request!==projectRequest)return;if(!response.ok)throw new Error(errorText(data));if(!Array.isArray(data.projects)||!data.projects.every(p=>typeof p.name==='string'&&/^[a-z0-9_-]{1,64}$/.test(p.projectId)))throw new Error('项目列表响应格式异常。');const experimentProjects=data.projects.filter(p=>p.canStartExperiment===true&&Array.isArray(p.modes)&&p.modes.includes('experiment'));experimentHost.innerHTML=experimentProjects.length?experimentProjects.map(p=>`<p><a href="#projects/${encodeURIComponent(p.projectId)}/tasks">${escape(p.name)} · 打开固定实验需求 →</a></p>`).join(''):'<p class="hint">没有可启动的固定实验项目。</p>';projects=data.projects.filter(p=>p.canCreateTask!==false);select.innerHTML=projects.length?'<option value="">请选择普通任务项目</option>'+projects.map(p=>`<option value="${escape(p.projectId)}">${escape(p.name)}</option>`).join(''):'<option value="">无可用普通任务项目</option>';select.disabled=!projects.length;if(projects.some(p=>p.projectId===realDraft.projectId))select.value=realDraft.projectId;state.textContent=projects.length?'已加载可创建普通任务的项目。':'暂无普通任务项目，可在右侧打开固定实验。';}catch(e){if(!active()||request!==projectRequest)return;experimentHost.innerHTML='<p role="alert">实验项目暂无法读取。</p>';select.innerHTML='<option value="">项目加载失败</option>';state.textContent=`项目不可用：${e.message || '后端连接失败'}`;}if(active()&&request===projectRequest)buttons();}
 document.querySelector('#reload-projects').onclick=loadProjects;
 form.onsubmit=e=>{e.preventDefault();if(!projects.length||pendingCreate||uncertainCreate)return;remember();if(!projects.some(p=>p.projectId===realDraft.projectId))return;const lines=value=>value.trim().split(/\r?\n/).map(s=>s.trim());payload={workspaceMode:realDraft.workspaceMode||'worktree',projectId:realDraft.projectId,revision:realDraft.revision.trim(),kind:realDraft.kind,current:realDraft.current.trim(),expected:realDraft.expected.trim(),allowedPaths:realDraft.kind==='read_code'?[]:lines(realDraft.scope),allowTests:realDraft.allowTests,checkTargets:realDraft.allowTests?lines(realDraft.targets).filter(Boolean):[],maxSteps:50,...(realDraft.allowTemporaryChecks?{allowTemporaryChecks:true}:{})};document.querySelector('#real-review-content').innerHTML=`<p>执行目录：${payload.workspaceMode==='direct'?'直接修改当前仓库':'隔离副本'}</p><p>任务类型：${({read_code:'阅读代码',fix_bug:'修复 Bug',add_feature:'补充小功能'})[payload.kind]}</p>`+contract({project:projects.find(p=>p.projectId===payload.projectId).name,revision:payload.revision,current:payload.current,expected:payload.expected,scope:payload.kind==='read_code'?'只读，不允许修改文件':payload.allowedPaths.join('\n'),allowTests:payload.allowTests,targets:payload.checkTargets.join('\n')});if(payload.allowTemporaryChecks)document.querySelector('#real-review-content').insertAdjacentHTML('beforeend','<p>允许在本机执行临时 Python 自测</p>');document.querySelector('#review-error').textContent='';dialog.showModal();};
 document.querySelector('#edit-real').onclick=()=>dialog.close();
 dialog.addEventListener('cancel',e=>{if(pendingCreate)e.preventDefault();});
 save.onclick=async()=>{if(pendingCreate||uncertainCreate||!payload)return;pendingCreate=true;buttons();let response;
 try{response=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(response.status!==201){if(response.ok)throw new Error('响应状态异常');pendingCreate=false;if(active()){showError(errorText(data));buttons();}return;}if(!validRecord(data))throw new Error('保存响应无法确认');pendingCreate=false;realDraft={projectId:'',revision:'',current:'',expected:'',scope:'',targets:'',allowTests:true};if(active())location.hash=`/tasks/${data.taskId}`;
 }catch{pendingCreate=false;uncertainCreate=true;if(active()){showError('保存结果暂无法确认，勿直接重复提交。后端可能已保存成功；请先由维护者核对记录。');buttons();}}};
 loadProjects();
}
let preparationPending = new Set();
function preparationPanel(taskRecord, {onPrepared=()=>{}}={}) {
 const id=taskRecord.taskId, epoch=pageEpoch, card=document.querySelector('#preparation-card');
 if(taskRecord.contract.workspaceMode==='direct'){
  card.innerHTML='<h2>直接修改当前仓库</h2><p>无需创建或准备工作副本。执行前会保存本轮基线，修改直接写入当前项目目录。</p>';
  return;
 }
 let request=0;
 const active=()=>pageEpoch===epoch;
 const date=value=>value ? new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'long',timeZone:'Asia/Shanghai'}).format(new Date(value)) : '未提供';
 function valid(r){
  if(r?.schemaVersion!==1||r.taskId!==id||!['not_prepared','preparing','prepared','preparation_failed'].includes(r.status))return false;
  if(r.baseCommit!==undefined && r.baseCommit!==taskRecord.contract.baseCommit)return false;
  for(const k of ['startedAt','finishedAt'])if(r[k]!==undefined&&(typeof r[k]!=='string'||Number.isNaN(Date.parse(r[k]))))return false;
  if(r.preparationId!==undefined&&!/^[a-f0-9]{32}$/.test(r.preparationId))return false;
  if(r.workspaceId!==undefined&&r.workspaceId!==r.preparationId)return false;
  if(r.isExecutionSandbox!==undefined&&r.isExecutionSandbox!==false)return false;
  if(r.status==='prepared' && (!r.preparationId||!r.workspaceId||!r.startedAt||!r.finishedAt||!r.baseCommit||r.isExecutionSandbox!==false))return false;
  if(r.status==='preparation_failed'&&(!r.finishedAt||typeof r.error?.code!=='string'||typeof r.error?.message!=='string'))return false;
  return true;
 }
 function draw(r, message=''){
  const status=r?.status, labels={not_prepared:'尚未准备',preparing:'工作副本准备中',prepared:taskRecord.contract.workspaceMode==='direct'?'当前仓库已就绪':'工作副本已准备',preparation_failed:'工作副本准备失败'};
  card.innerHTML=`<h2>${labels[status]||'准备状态暂无法确认'}</h2><p role="status" style="white-space:pre-wrap">${escape(message || ({not_prepared:'从已冻结 commit 单独准备副本；不包含源仓库未提交内容。',preparing:'后端报告正在准备工作副本；不是 Agent 执行中。请查询最新状态。',prepared:'副本准备记录已就绪；Agent 与检查状态见执行区域。',preparation_failed:'失败材料保留；本轮没有重试或清理接口。需要新一轮时请新建任务。'}[status]) || '请先查询最新状态，勿直接重复准备。')}</p>${r?`<dl class="kv"><dt>准备编号</dt><dd>${escape(r.preparationId||'未提供')}</dd><dt>副本标识</dt><dd>${escape(r.workspaceId||'未提供')}</dd><dt>固定 commit</dt><dd>${escape(r.baseCommit||taskRecord.contract.baseCommit)}</dd><dt>入队时间</dt><dd>${escape(displayTime(r.queuedAt))}</dd><dt>调度说明</dt><dd>${escape(({WORKER_INTERRUPTED:'Worker 已中断，保留修改和日志待核实',WORKER_HEARTBEAT_LOST:'Worker 心跳中断，执行状态待核实',EXECUTION_UNCONFIRMED:'执行结果待核实',ALREADY_STARTED:'已有执行材料，未重复执行'}[r.stopReason])||r.stopReason||'未提供')}</dd><dt>开始时间</dt><dd>${escape(date(r.startedAt))}</dd><dt>结束时间</dt><dd>${escape(date(r.finishedAt))}</dd></dl>`:''}${r?.error?`<p role="alert">${escape(r.error.message)}（${escape(r.error.code)}）</p>`:''}<p class="hint">${taskRecord.contract.workspaceMode==='direct'?'本轮直接修改当前仓库文件；现有未提交内容参与执行前基线。':'worktree 仅隔离文件，不是执行沙箱。'}准备阶段不安装依赖或执行测试；实际检查见执行区域。准备接口不提供本机路径或日志下载入口。</p><div class="actions"><button id="prepare-workspace" ${status!=='not_prepared'||preparationPending.has(id)?'disabled':''}>${preparationPending.has(id)?'正在等待准备响应…':'准备工作副本'}</button><button class="secondary" id="query-preparation" ${preparationPending.has(id)?'disabled':''}>查询最新状态</button></div>`;
  const prepare=document.querySelector('#prepare-workspace');
  prepare.hidden=projectMode();
  prepare.onclick=()=>{if(!projectMode()&&status==='not_prepared'&&!preparationPending.has(id))update(true);};
  document.querySelector('#query-preparation').onclick=()=>update(false);
  if(status==='preparation_failed'&&canAdministerInstance()){
   card.insertAdjacentHTML('beforeend','<details><summary>查看失败详情</summary><p id="preparation-diagnostics">正在读取已保存的诊断记录…</p></details>');
   const host=document.querySelector('#preparation-diagnostics');
   fetch(`/api/tasks/${id}/preparation/diagnostics`,{cache:'no-store'}).then(async response=>{const data=await response.json();if(!active()||!host)return;if(!response.ok)throw new Error(errorText(data));host.textContent=data.available?JSON.stringify(data.details,null,2):'本轮未保存详细诊断，无法从现有记录确定原因。';host.style.whiteSpace='pre-wrap';}).catch(e=>{if(active()&&host)host.textContent='诊断暂无法读取：'+e.message;});
  }
 }
 async function update(write=false){
  const seq=++request;
  if(write){preparationPending.add(id);draw(null,'正在请求准备工作副本；不会启动 Agent、模型、测试或依赖安装。');}
  else {draw(null,preparationPending.has(id)?'先前准备请求仍在等待响应；正在查询后端状态。':'正在查询工作副本准备状态…');document.querySelector('#query-preparation').disabled=true;}
  try{const response=await fetch(`/api/tasks/${id}/preparation`,write?{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}:{cache:'no-store'});const data=await response.json();if(write)preparationPending.delete(id);if(!active()||seq!==request)return;
   if(!response.ok){draw(null,errorText(data)+'\n准备状态暂无法确认，请查询最新状态；不自动重发准备请求。');return;}
   if(!valid(data)){draw(null,'准备响应格式异常或版本不匹配，准备状态暂无法确认。请先查询，不自动重发。');return;}
   draw(data);
   if(data.status==='prepared')onPrepared();
  }catch{if(write)preparationPending.delete(id);if(active()&&seq===request)draw(null,write?'准备结果暂无法确认，后端可能已经完成。请先查询状态，勿直接重复准备。':'准备状态暂无法确认：后端不可用或响应无法读取。请重新查询。');}
 }
 update();
}

function validCounts(counts) {
 return counts!==null && typeof counts==='object' && ['total','passed','failed','errors','skipped'].every(k=>Number.isInteger(counts[k])&&counts[k]>=0) && counts.total===counts.passed+counts.failed+counts.errors+counts.skipped;
}
function countsText(counts) {
 return validCounts(counts)?`总计 ${counts.total} · 通过 ${counts.passed} · 失败 ${counts.failed} · 错误 ${counts.errors} · 跳过 ${counts.skipped}`:'计数未提供或无法核验（不是 0）';
}
function checkLabel(check) {
 const labels={passed:'通过',failed:'失败',skipped:'跳过',not_run:'未执行',unavailable:'不可用'};
 if(check?.status==='passed' && (!validCounts(check.counts)||check.counts.passed===0||check.counts.failed!==0||check.counts.errors!==0))return '无法核验（接口报告通过，计数证据不足或矛盾）';
 return labels[check?.status]||check?.status||'未提供';
}
function feedbackHTML(feedback) {
 if(!feedback||typeof feedback!=='object')return '<p class="hint">实际反馈摘要：未提供，不补推成功或零计数。</p>';
 const fields=[
  ['类型',feedback.kind],['状态',feedback.status],['实际反馈',feedback.message],['观察编号',feedback.observationId],
  ['相对路径',feedback.path],['修改路径',Array.isArray(feedback.paths)?feedback.paths.join('、'):undefined],
  ['可见范围',feedback.scope],['查询范围',feedback.scopePath],
  ['列举完整',typeof feedback.complete==='boolean'?(feedback.complete?'是':'否'):undefined],
  ['扫描完整',typeof feedback.scanComplete==='boolean'?(feedback.scanComplete?'是':'否'):undefined],
  ['停止原因',feedback.stopReason],['本页扫描文件',feedback.scannedFiles],
  ['累计匹配行',feedback.matchedLinesTotal],
  ['累计跳过',feedback.skippedCounts&&typeof feedback.skippedCounts==='object'?Object.entries(feedback.skippedCounts).map(([key,value])=>`${key}=${value}`).join(' · '):undefined],
  ['可继续查询',typeof feedback.hasMore==='boolean'?(feedback.hasMore?'是':'否'):undefined],
  ['返回数量',feedback.returned],['总行数',feedback.totalLines],
  ['已应用',typeof feedback.applied==='boolean'?(feedback.applied?'是':'否'):undefined],
  ['测试通过标记',typeof feedback.passed==='boolean'?(feedback.passed?'是':'否'):undefined],
  ['检查分类',feedback.classification?.code],['分类依据',feedback.classification?.reason],
  ['退出码',feedback.exitCode],['超时',typeof feedback.timedOut==='boolean'?(feedback.timedOut?'是':'否'):undefined]
 ];
 return `<dl class="kv">${fields.filter(([,v])=>v!==undefined&&v!==null).map(([label,value])=>`<dt>${label}</dt><dd>${escape(value)}</dd>`).join('')}</dl>${feedback.kind==='test'||Object.hasOwn(feedback,'counts')?`<p class="hint">${escape(countsText(feedback.counts))}</p>`:''}`;
}

function progressHTML(progress, budget) {
 const budgetText=budget&&Number.isInteger(budget.remaining_decisions)
  ? `<p>已用决策 ${escape(budget.used_decisions)} / ${escape(budget.max_decisions)} · 剩余 ${escape(budget.remaining_decisions)}${budget.remaining_includes_current?'（包含本次决策）':''}</p>`
  : '<p class="hint">决策预算未提供。</p>';
 if(!progress||!Array.isArray(progress.steps))return budgetText+'<p class="hint">步骤记录未提供。</p>';
 const labels={pending:'待处理',in_progress:'进行中（最后保存状态）',completed:'模型声明完成',blocked:'受阻'};
 const evidence=new Map((Array.isArray(progress.evidence)?progress.evidence:[]).map(item=>[item.id,item]));
 const stale=new Set(Array.isArray(progress.stale_evidence_refs)?progress.stale_evidence_refs:[]);
 const rows=progress.steps.map(step=>{
  const refs=Array.isArray(step.evidence_refs)?step.evidence_refs:[];
  const facts=refs.map(ref=>{
   const fact=evidence.get(ref);
   let description=ref;
   if(fact){
    let outcome='已记录工具观察';
    if(fact.passed===true)outcome='检查通过';
    else if(fact.passed===false)outcome='检查失败';
    else if(fact.ok===false)outcome='工具失败';
    else if(fact.applied===true)outcome='已编辑';
    else if(fact.applied===false)outcome='修改未应用';
    description+=` · ${fact.action} · ${outcome}`;
    if(fact.path||fact.file)description+=` · ${fact.path||fact.file}`;
    if(fact.consistent===false)description+=' · 文件一致性待核实';
    if(fact.scan_complete===false||fact.complete===false)description+=' · 查询覆盖不完整';
   }
   if(stale.has(ref))description+=' · 通过检查已因后续编辑过期';
   return `<li>${escape(description)}</li>`;
  }).join('');
  return `<article class="event"><h4>${escape(step.id)} · ${escape(labels[step.status]||step.status)}</h4><p style="white-space:pre-wrap">${escape(step.goal)}</p>${step.blocked_reason?`<p role="status">阻塞原因：${escape(step.blocked_reason)}</p>`:''}${facts?`<ul>${facts}</ul>`:'<p class="hint">尚无证据引用。</p>'}</article>`;
 }).join('');
 return budgetText+`<p class="hint">计划修订 ${escape(progress.revision)} · 当前步骤 ${escape(progress.current_step_id||'未指定')} · 以下步骤为模型报告，实际检查与范围审查单独展示。</p>${rows||'<p>本轮尚未建立步骤列表。</p>'}`;
}

// M3 keeps execution state independent from the saved contract and preparation.
let executionPending = new Set();
let executionCleanup = () => {};
const sourceLabel = source => ({configured_provider:'真实模型 · 本机配置 Provider',injected_test_model:'离线脚本执行 · 注入测试模型'}[source] || '运行来源未知');
const displayTime = value => value && !Number.isNaN(Date.parse(value)) ? new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'long',timeZone:'Asia/Shanghai'}).format(new Date(value)) : '未提供';
function executionPanel(taskRecord) {
 const id=taskRecord.taskId, epoch=pageEpoch, base=`/api/tasks/${id}/execution`;
 const root=document.querySelector('#execution-card');
 root.innerHTML=`<h2>Agent 执行</h2><div id="execution-status" aria-live="polite"></div><div class="actions"><button id="start-agent" disabled>提交执行队列</button><button class="secondary" id="query-execution">查询执行状态</button></div><div id="recent-feedback"><h3>最近实际反馈</h3><p class="hint">尚未读取到实际反馈摘要。</p></div><div id="execution-events"><h3>逐步记录</h3><p id="event-message" role="status"></p><div id="event-list"></div><button class="secondary" id="more-events" disabled>读取后续事件</button></div><div id="execution-result"><h3>结果与证据</h3><p id="result-message" role="status">尚未查询到完整结果，不据此推断执行成功或失败。</p><div id="result-content"></div><button class="secondary" id="query-result" disabled>查询结果</button></div><dialog id="start-review" aria-label="启动 Agent 确认"><h2>核对并单独启动</h2><div id="start-contract"></div><div class="notice">此操作会持久入队，由独立 Worker 执行 Agent。允许测试时会执行仓库代码，可能有副作用或网络访问；worktree 不是沙箱。模型结束不等于检查通过。</div><div class="actions"><button id="confirm-start">确认启动</button><button class="secondary" id="cancel-start">返回</button></div></dialog>`;
 const $=selector=>document.querySelector(selector);
 if(projectMode()){
  $('#start-agent').textContent='提交执行申请';
  $('#confirm-start').textContent='确认提交';
  $('#start-review h2').textContent='核对执行申请';
  $('#start-review .notice').textContent='实例管理员放行后才会入队。放行前会复核申请人权限及此处列出的执行范围。';
 }
 let state=null, timer=null, closed=false, querying=false, refreshRequested=false, cursor=0, eventBusy=false, resultBusy=false, resultLoaded=false, artifacts=new Map();
 const active=()=>!closed && pageEpoch===epoch;
 let undoState=null, undoBusy=false;
 if(taskRecord.contract.workspaceMode==='direct'&&canAdministerInstance()){
  root.insertAdjacentHTML('beforeend','<section><h3>撤回本轮修改</h3><p id="undo-status" role="status">撤回前会检查后续修改。先查询可撤回状态。</p><div class="actions"><button class="secondary" id="query-undo">检查能否撤回</button><button id="undo-task" disabled>撤回本轮修改</button></div><dialog id="undo-review"><h3>确认撤回本轮修改</h3><p>恢复本轮执行前的文件，新建文件将被移除。发现后续编辑时会停止。</p><pre id="undo-files"></pre><button id="confirm-undo">确认撤回</button><button class="secondary" id="cancel-undo">取消</button></dialog></section>');
  $('#query-undo').onclick=queryUndo;
  $('#undo-task').onclick=()=>{if(undoBusy||!undoState?.canUndo||!state?.executionId)return;$('#undo-files').textContent=undoState.files.join('\n');$('#undo-review').showModal();};
  $('#cancel-undo').onclick=()=>$('#undo-review').close();
  $('#confirm-undo').onclick=async()=>{
   if(undoBusy||!undoState?.canUndo||!state?.executionId)return;
   undoBusy=true;undoState=null;$('#undo-review').close();$('#undo-task').disabled=true;$('#query-undo').disabled=true;
   try{
    const response=await fetch(base+'/undo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({executionId:state.executionId})});
    const data=await response.json();if(!active())return;
    if(!response.ok)throw Error(errorText(data));
    if(data.taskId!==id||data.status!=='undone')throw Error('撤回结果无法确认');
    $('#undo-status').textContent='本轮修改已撤回，原执行结果保留。刷新相关页面或重载服务以查看恢复后的代码。';
   }catch(e){if(active())$('#undo-status').textContent='撤回结果待核对：'+e.message+'。请重新检查，不自动重发。';}
   finally{undoBusy=false;if(active())$('#query-undo').disabled=false;}
  };
 }
 async function queryUndo(){
  if(!active()||undoBusy)return;undoBusy=true;$('#query-undo').disabled=true;$('#undo-task').disabled=true;undoState=null;
  try{
   const response=await fetch(base+'/undo',{cache:'no-store'}),data=await response.json();if(!active())return;
   if(!response.ok)throw Error(errorText(data));
   if(data.taskId!==id||typeof data.canUndo!=='boolean'||!Array.isArray(data.files)||!Array.isArray(data.conflicts))throw Error('撤回状态响应异常');
   undoState=data;
   const reasons={EXECUTION_NOT_TERMINATED:'执行尚未确认结束',UNDO_EVIDENCE_UNAVAILABLE:'此轮缺少安全撤回所需的执行后记录',SCOPE_REVIEW_INCOMPLETE:'范围审查不完整，需人工核对',REPOSITORY_IDENTITY_CHANGED:'仓库版本或身份已变化，需人工核对'};
   $('#undo-status').textContent=data.status==='undone'?'本轮修改已撤回':data.conflicts.length?'存在后续修改，无法撤回：'+data.conflicts.join('、'):data.canUndo?'可撤回 '+data.files.length+' 个文件的本轮修改':data.status==='no_changes'?'本轮没有可撤回修改':reasons[data.reason]||data.reason||'当前无法撤回';
   $('#undo-task').disabled=!data.canUndo;
  }catch(e){if(active())$('#undo-status').textContent='无法读取撤回状态：'+e.message;}
  finally{undoBusy=false;if(active())$('#query-undo').disabled=false;}
 }

 root.insertAdjacentHTML('beforeend','<details><summary>执行错误详情</summary><button class="secondary" id="query-execution-diagnostics">读取错误记录</button><pre id="execution-diagnostics">按需读取；没有记录不表示执行成功。</pre></details>');
 $('#query-execution-diagnostics').onclick=async()=>{
  const button=$('#query-execution-diagnostics'),host=$('#execution-diagnostics');button.disabled=true;
  try{const response=await fetch(base+'/diagnostics',{cache:'no-store'}),data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));host.textContent=data.available?JSON.stringify(data.errors,null,2):'本轮没有已保存的执行错误详情；无法补回旧异常，也不据此推断成功。';}
  catch(e){if(active())host.textContent='错误详情暂无法读取：'+e.message;}
  finally{if(active())button.disabled=false;}
 };
 root.insertAdjacentHTML('beforeend','<section class="card"><h3>异常恢复</h3><p>先核实旧执行和关联进程；解除只影响遗留占用，保留原记录，不重跑模型。</p><button class="secondary" id="query-execution-recovery">核实遗留占用</button><div id="execution-recovery-status" role="status"></div><button id="release-execution-owner" hidden disabled>解除遗留占用（保留记录）</button></section>');
 let recovery=null;
 const recoveryLabels={NO_STALE_OWNER:'没有需要解除的遗留占用',EXECUTION_ACTIVE:'有活动执行，不能解除',ASSOCIATED_PROCESS_ACTIVE:'仍有关联进程，不能解除',ACTIVITY_UNCONFIRMED:'进程状态无法核实，不能解除',RELEASED:'遗留占用已解除，原记录保留'};
 function drawRecovery(data){
  recovery=data;const owner=data.owner,button=$('#release-execution-owner');
  $('#execution-recovery-status').innerHTML=`<p>${escape(recoveryLabels[data.reason]||data.reason||(data.canRelease?'后端已核实当前没有关联活动执行，可手动解除。':'未确认可解除'))}</p>${owner?`<p>旧执行：${escape(owner.executionId)}<br><a href="#/tasks/${escape(owner.taskId)}">查看旧执行及错误详情 →</a></p>`:''}${data.released?`<p>恢复记录：${escape(data.released.releasedAt)}</p>`:''}`;
  button.hidden=!data.canRelease;button.disabled=!data.canRelease;
 }
 $('#query-execution-recovery').onclick=async()=>{
  const button=$('#query-execution-recovery');button.disabled=true;$('#release-execution-owner').disabled=true;
  try{const response=await fetch(base+'/recovery',{cache:'no-store'}),data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));drawRecovery(data);}
  catch(e){if(active()){$('#execution-recovery-status').textContent='恢复检查失败：'+e.message;recovery=null;}}
  finally{if(active())button.disabled=false;}
 };
 $('#release-execution-owner').onclick=async()=>{
  if(!active()||!recovery?.canRelease||!recovery.owner)return;
  const owner=recovery.owner;recovery=null;$('#release-execution-owner').disabled=true;
  try{const response=await fetch(`/api/tasks/${owner.taskId}/execution/recovery`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({executionId:owner.executionId})}),data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));drawRecovery(data);await refresh();}
  catch(e){if(active())$('#execution-recovery-status').textContent='解除结果暂无法确认：'+e.message+'。请重新核实，不自动重发。';}
 };
 executionCleanup=()=>{closed=true;if(timer!==null)clearTimeout(timer);};
 const text=(sel,value)=>{$(sel).textContent=value;};
 const canSubmit=()=>state?.status==='not_started'&&(state.canRequest??state.canStart);
 function buttons(){const busy=executionPending.has(id);$('#start-agent').disabled=!canSubmit()||busy||querying;$('#query-execution').disabled=querying||busy;$('#query-result').disabled=resultBusy||!state||['not_started','queued'].includes(state.status);}
 function validState(r){return r?.schemaVersion===1&&r.taskId===id&&['not_started','queued','running','finalizing','terminated','unknown'].includes(r.status)&&typeof r.canStart==='boolean'&&['configured_provider','injected_test_model'].includes(r.source)&&r.isExecutionSandbox===false&&(['not_started','queued'].includes(r.status)||r.status==='unknown'&&r.executionId===undefined||/^[a-f0-9]{32}$/.test(r.executionId||''));}
 function draw(r,message=''){
  state=r;
  const labels={not_started:'尚未启动',queued:'已入队，等待 Worker',running:'执行中',finalizing:'正在整理',terminated:'执行已终止',unknown:'状态暂无法确认'};
  const blocks={PREPARATION_REQUIRED:projectMode()?'Worker 领取并通过授权复核后准备副本':'需要先准备工作副本',WORKSPACE_UNAVAILABLE:'工作副本不可用',WORKSPACE_DIRTY:'工作副本有未提交或未跟踪内容',EXECUTION_CONFIG_UNAVAILABLE:'本机执行配置不可用',EXECUTION_BUSY:'其他任务正在执行',EXECUTION_UNRESOLVED:'存在未核实的旧执行，需维护者处理',ALREADY_STARTED:'本任务已有执行记录'};
  const phases={queue:'等待调度',agent:'Agent 决策与工具',final_checks:'固定最终检查',delivery:'整理交付'};
  $('#execution-status').innerHTML=`<p><strong>${labels[r?.status]||'状态暂无法确认'}</strong>　<span class="badge">${escape(sourceLabel(r?.source))}</span></p>${message?`<p role="alert" style="white-space:pre-wrap">${escape(message)}</p>`:''}${r?`<dl class="kv"><dt>执行编号</dt><dd>${escape(r.executionId||'未提供')}</dd><dt>阶段</dt><dd>${escape(phases[r.phase]||r.phase||'未提供')}</dd><dt>当前活动</dt><dd>${escape(r.activity||'未提供')}</dd><dt>决策步数</dt><dd>${Number.isInteger(r.steps)?r.steps:'未提供'} / ${Number.isInteger(r.maxSteps)?r.maxSteps:taskRecord.contract.maxSteps}</dd><dt>入队时间</dt><dd>${escape(displayTime(r.queuedAt))}</dd><dt>调度说明</dt><dd>${escape(({WORKER_INTERRUPTED:'Worker 已中断，保留修改和日志待核实',WORKER_HEARTBEAT_LOST:'Worker 心跳中断，执行状态待核实',EXECUTION_UNCONFIRMED:'执行结果待核实',ALREADY_STARTED:'已有执行材料，未重复执行'}[r.stopReason])||r.stopReason||'未提供')}</dd><dt>开始时间</dt><dd>${escape(displayTime(r.startedAt))}</dd><dt>最近记录时间</dt><dd>${escape(displayTime(r.updatedAt))}</dd><dt>结束时间</dt><dd>${escape(displayTime(r.finishedAt))}</dd><dt>存活状态</dt><dd>${escape(r.liveness==='confirmed'?'后端已确认':r.liveness==='stopped'?'后端记录已停止':'暂无法确认')}</dd></dl>${r.startBlockReason?`<p class="hint">启动限制：${escape(blocks[r.startBlockReason]||r.startBlockReason)}</p>`:''}`:''}<p class="hint">记录更新时间不是心跳；不根据停更推断完成或死亡。普通任务执行不属于独立 G/R/P 实验验收。</p>`;
  if(r)$('#execution-status').insertAdjacentHTML('beforeend',`<section><h3>最后保存的执行步骤</h3>${progressHTML(r.progress,r.budget)}</section>`);
  if(r?.executionRequest){
   const a=document.createElement('a');
   a.href=projectAccessHref(r.executionRequest.projectId,r.executionRequest.requestId);
   a.textContent={pending:'待管理员放行',approved:'申请已放行',rejected:'申请已拒绝'}[r.executionRequest.status];
   $('#execution-status').prepend(a);
  }
  if(projectMode()&&r?.executionId){
   const a=document.createElement('a');
   a.href=executionAuditHref(taskRecord.contract.projectId,r.executionId);
   a.textContent='查看授权与执行审计';
   $('#execution-status').append(a);
   if(r.authorizationDenied){
    const stop=document.createElement('p');
    stop.textContent='领取时授权复核未通过，本轮未启动。';
    $('#execution-status').prepend(stop);
   }
  }
  buttons();
 }
 async function readEvents(){
  if(!active()||eventBusy)return;eventBusy=true;$('#more-events').disabled=true;
  const after=cursor;
  try{const response=await fetch(`${base}/events?after=${after}&limit=50`,{cache:'no-store'});const data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));
   if(data.taskId!==id||!Array.isArray(data.events)||!Number.isInteger(data.nextCursor)||data.nextCursor<after||typeof data.hasMore!=='boolean'||!data.events.every((e,i)=>Number.isInteger(e.seq)&&e.seq>(i?data.events[i-1].seq:after)&&typeof e.type==='string')||(data.events.length&&data.nextCursor!==data.events.at(-1).seq)||(!data.events.length&&data.nextCursor!==after))throw new Error('事件分页响应格式异常，未推进游标。');
   const types={phase:'阶段',decision:'模型决策',observation:'工具反馈',context:'决策上下文',progress_updated:'步骤更新',model_usage:'模型用量',model_attempt:'模型请求',finish:'模型主动结束',finish_rejected:'完成声明被退回，继续执行',limit:'步数上限',error:'错误',terminated:'终止记录'};
   const html=data.events.map(e=>{
    const summary=[e.phase&&`阶段：${e.phase}`,Number.isInteger(e.step)&&`决策步：${e.step}`,e.action&&`动作：${e.action}`,typeof e.ok==='boolean'&&`工具反馈：${e.ok?'成功':'失败'}`,e.attempt!=null&&`请求尝试：${e.attempt}`,e.outcome&&`请求结果：${e.outcome}`].filter(Boolean).join(' · ');
    let detail=e.type==='observation'?feedbackHTML(e.feedback):'';
    if(e.type==='progress_updated')detail+=`<details><summary>已保存步骤修订</summary>${progressHTML(e.progress,e.budget)}</details>`;
    if(e.type==='context')detail+=`<p class="hint">历史消息 ${escape(e.context?.history_messages??'未提供')} · 状态尾 ${escape(e.context?.state_chars??'未提供')} 字符 · 输入 ${escape(e.context?.input_chars??'未提供')} 字符</p>`;
    if(e.type==='model_usage')detail+=`<p>${e.available?escape(JSON.stringify(e.usage)):'Provider 未返回可识别的实际用量字段'}</p>`;
    return `<article class="event"><div class="meta">#${e.seq} · ${escape(displayTime(e.at))}</div><h3>${escape(types[e.type]||e.type)}</h3><p>${escape(summary||'后端摘要事件；未提供更多公开内容。')}</p>${detail}</article>`;
   }).join('');
   $('#event-list').insertAdjacentHTML('beforeend',html);
   const recent=data.events.filter(e=>e.type==='observation').at(-1);if(recent)$('#recent-feedback').innerHTML='<h3>最近实际反馈</h3>'+feedbackHTML(recent.feedback);cursor=data.nextCursor;text('#event-message',data.hasMore?'还有后续事件，点击继续读取。':cursor?'已读取当前可用摘要；不是私有原始 trace。':'暂无事件记录。');$('#more-events').disabled=!data.hasMore;
  }catch(e){if(active()){text('#event-message',`事件暂无法读取：${e.message}`);$('#more-events').disabled=false;}}
  finally{eventBusy=false;}
 }
 function artifactWidgets(result){
  return result.artifacts.map((a,i)=>{artifacts.set(a.artifactId,{...a,index:i,offset:0,busy:false});return `<details><summary>${escape(a.kind)} · ${escape(a.artifactId)}</summary><p id="artifact-note-${i}" class="hint">按需读取公开脱敏文本；不提供私有原始 trace。</p><pre id="artifact-text-${i}">尚未读取</pre><button class="secondary" id="artifact-load-${i}">读取材料</button></details>`;}).join('');
 }
 async function readArtifact(artifactId){
  const a=artifacts.get(artifactId);if(!a||a.busy||!active())return;a.busy=true;const button=$(`#artifact-load-${a.index}`);button.disabled=true;
  try{const response=await fetch(`${base}/artifacts/${encodeURIComponent(artifactId)}?offset=${a.offset}&limit=16000`,{cache:'no-store'});const data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));if(data.artifactId!==artifactId||typeof data.content!=='string'||data.offset!==a.offset||!Number.isInteger(data.nextOffset)||data.nextOffset<a.offset||!Number.isInteger(data.totalChars)||data.totalChars<data.nextOffset||typeof data.hasMore!=='boolean'||typeof data.redacted!=='boolean'||typeof data.truncated!=='boolean'||(data.hasMore&&data.nextOffset===a.offset))throw new Error('材料分页响应格式异常。');
   const pre=$(`#artifact-text-${a.index}`);pre.textContent=(a.offset?pre.textContent:'')+data.content;a.offset=data.nextOffset;text(`#artifact-note-${a.index}`,`${data.redacted?'公开脱敏文本':'脱敏状态未确认'}；已读取 ${a.offset} / ${data.totalChars} 字符。${data.truncated?'公开内容已截断，本机原件另行保留。':'接口未标记截断；分页读完也不表示原始材料字节一致。'}${data.hasMore?'还有后续内容。':'当前公开文本已读完。'}${data.totalChars===0?'输出为空；是否执行应以检查记录为准。':''}`);button.textContent=data.hasMore?'读取下一段':'读取完毕';button.disabled=!data.hasMore;
  }catch(e){if(active()){text(`#artifact-note-${a.index}`,`材料不可用：${e.message}。不视作空输出。`);button.disabled=false;}}
  finally{a.busy=false;}
 }
 async function readResult(){
  if(!active()||resultBusy)return;resultBusy=true;buttons();text('#result-message','正在查询实际结果…');
  try{const response=await fetch(`${base}/result`,{cache:'no-store'});const r=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(r));
   if(r?.schemaVersion!==1||r.taskId!==id||r.executionId!==state?.executionId||r.source!==state?.source||r.independentlyVerified!==false||!Array.isArray(r.finalChecks?.checks)||!Array.isArray(r.finalChecks.targets)||!['passed','failed','skipped','not_run'].includes(r.finalChecks.status)||!['passed','failed','unavailable'].includes(r.review?.status)||!Array.isArray(r.review.changedPaths)||!Array.isArray(r.review.outOfScopePaths)||!Array.isArray(r.review.protectedChanges)||!Array.isArray(r.incomplete)||!Array.isArray(r.artifacts)||!r.artifacts.every(a=>typeof a.artifactId==='string'&&typeof a.kind==='string')||new Set(r.artifacts.map(a=>a.artifactId)).size!==r.artifacts.length)throw new Error('结果响应格式异常或与执行记录不匹配。');
   const checks={passed:'通过',failed:'失败',skipped:'跳过',not_run:'未执行',unavailable:'不可用'};
   const reasons={agent_reported_finished:'模型主动结束',step_limit:'步数上限',agent_error:'Agent 出错',execution_error:'执行出错',policy_violation:'策略限制'};
   artifacts=new Map();
   $('#result-content').innerHTML=`<div class="metrics"><div class="metric">停止原因<strong>${escape(reasons[r.stopReason]||r.stopReason||'未提供')}</strong></div><div class="metric">固定最终检查<strong>${escape(checkLabel(r.finalChecks))}</strong></div><div class="metric">交付范围审查<strong>${checks[r.review.status]}</strong></div></div><p class="hint">检查仅覆盖冻结目标；检查退出码 0 不代替范围审查。未做独立 G/R/P 验收。</p><h3>修改与范围</h3><p>修改文件：${escape(r.review.changedPaths.join('、')||'无记录的修改')}</p><p>越界路径：${escape(r.review.outOfScopePaths.join('、')||'无记录')}</p><p>受保护变动：${escape(r.review.protectedChanges.join('、')||'无记录')}</p><h3>固定最终检查</h3><p>覆盖目标：${escape(r.finalChecks.targets.join('、')||'无')}</p><p>原因：${escape(r.finalChecks.reason||'未提供')}</p><p class="hint">${escape(countsText(r.finalChecks.counts))}</p>${r.finalChecks.checks.length?r.finalChecks.checks.map(c=>`<details open><summary>${escape(c.target)} · ${escape(checkLabel(c))}</summary><p>退出码：${escape(c.exitCode??'未提供')}；超时：${c.timedOut===true?'是':c.timedOut===false?'否':'未提供'}</p><p class="hint">${escape(countsText(c.counts))}；原因：${escape(c.reason||'未提供')}</p><p class="hint">stdout：${escape(c.stdoutArtifactId||'未提供')}；stderr：${escape(c.stderrArtifactId||'未提供')}。实际文本见下方登记材料。</p></details>`).join(''):'<p class="empty">无逐项检查记录，不补写通过结果。</p>'}<h3>未完成事项</h3><p style="white-space:pre-wrap">${escape(r.incomplete.join('\n')||'接口未列出未完成事项；不表示完整功能验收通过。')}</p><h3>本机补丁导出</h3><dl class="kv"><dt>状态</dt><dd>${escape(({saved:'已保存本机补丁',not_exported:'未导出',unavailable:'不可用'}[r.patchExport?.status])||'未提供')}</dd><dt>SHA256</dt><dd>${escape(r.patchExport?.sha256??'未提供')}</dd><dt>字节数</dt><dd>${escape(r.patchExport?.bytes??'未提供')}</dd><dt>原因</dt><dd>${escape(r.patchExport?.reason??'未提供')}</dd></dl><p class="hint">仅描述本机补丁留存，不提供原始补丁下载，不证明可重放或独立验收。</p><h3>Agent 总结（说明）</h3><p style="white-space:pre-wrap">${escape(r.agent?.summary||'未提供')}</p><p class="hint">Agent 状态：${escape(r.agent?.status||'未提供')}。总结不代替 diff、实际输出或范围审查。</p><h3>Diff 与完整公开记录</h3><p class="hint">以下是登记的受控材料，支持分页；脱敏/截断文本不能作为字节一致的补丁验收材料。原始记录仅留本机，当前没有私有 trace 下载。</p>${artifactWidgets(r)||'<p class="empty">未登记材料；不伪造 diff 或检查输出。</p>'}`;
   const completionLabels={completed:'模型报告已完成',already_satisfied:'模型报告现有代码已满足',blocked:'受阻',incomplete:'未完成'};
   $('#result-content').insertAdjacentHTML('afterbegin',`<section><h3>步骤与交付证据</h3>${progressHTML(r.progress,r.budget)}</section>`);
   if(r.tokenAccounting)$('#result-content').insertAdjacentHTML('afterbegin',`<p class="hint">已结算 token：${escape(r.tokenAccounting.accountedTokens)} / ${escape(r.tokenAccounting.hardLimitTokens)}</p>`);
   if(r.finalizationReport)$('#result-content').insertAdjacentHTML('afterbegin',`<p class="notice">未完成：${escape(r.finalizationReport.reason)}</p>`);
   const acceptanceLabels={needs_review:'待审查',not_assessed:'未评定'};
   const gaps=r.remaining_gaps||[];
   $('#result-content').insertAdjacentHTML('afterbegin',`<section><h3>需求完成状态</h3><p><strong>${escape(acceptanceLabels[r.acceptance_status]||'历史记录未提供验收状态')}</strong></p><p>${escape(completionLabels[r.completion?.status]||'历史记录未提供模型结果')} · ${escape(r.execution_status||r.agent?.status||'未记录执行状态')}</p>${gaps.length?`<h4>待验证</h4><ul>${gaps.map(gap=>`<li>${escape(gap.scenario)}。${escape(gap.verification_goal)}</li>`).join('')}</ul>`:''}<p style="white-space:pre-wrap">${escape(r.completion?.evidence||'未提供依据')}</p></section>`);
   if(taskRecord.contract.workspaceMode==='direct')$('#result-content').insertAdjacentHTML('afterbegin','<p class="notice">本轮直接修改当前项目目录，文件变更已保存在原项目中。下方 Diff 展示本轮相对执行前的修改。网页源码变化需刷新页面，后端源码变化需重启对应服务加载。</p>');
   if(r.delivery?.status==='partial')$('#result-content').insertAdjacentHTML('afterbegin','<p class="notice">部分交付：常规结果保存失败，当前显示终止凭据中的已保存内容。材料可能缺失，请查看执行错误详情。</p>');
   const reviewHost = document.createElement('section');
   reviewHost.innerHTML = '<h3>人工审查</h3><button data-decision="accepted">接收本版</button> <button class="secondary" data-decision="changes_requested">要求修改</button><p role="status"></p>';
   $('#result-content').append(reviewHost);
   const reviewButtons = [...reviewHost.querySelectorAll('button')];
   if(taskRecord.projectRole&&!['admin','maintainer'].includes(taskRecord.projectRole))
    reviewButtons.forEach(button => {button.hidden=true;});
   const reviewMessage = reviewHost.querySelector('p');
   async function showReview(decision) {
    reviewButtons.forEach(button => {button.disabled = true;});
    try {
     const options = decision ? {method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({executionId:r.executionId,decision})} : {cache:'no-store'};
     const response = await fetch(`/api/tasks/${id}/review`,options);
     const review = await response.json();
     if (!response.ok) throw new Error(errorText(review));
     reviewMessage.textContent = review ? projectProgress({userReview:review}) : '尚待用户审查';
    } catch(error) {reviewMessage.textContent=error.message;}
    finally {reviewButtons.forEach(button => {button.disabled = false;});}
   }
   reviewButtons.forEach(button => {button.onclick=()=>showReview(button.dataset.decision);});
   showReview();
   for(const [artifactId,a] of artifacts)$(`#artifact-load-${a.index}`).onclick=()=>readArtifact(artifactId);
   text('#result-message',`${sourceLabel(r.source)} · 已读取实际结果。`);resultLoaded=true;
  }catch(e){if(active()){text('#result-message',`结果暂不可用：${e.message}。不推断任务成功或失败。${resultLoaded?'下方保留上次读取的结果，尚未重新确认。':''}`);}}
  finally{resultBusy=false;if(active())buttons();}
 }
 async function refresh(){
  if(!active())return;
  if(querying){refreshRequested=true;return;}
  querying=true;if(timer!==null)clearTimeout(timer);buttons();
  try{const response=await fetch(base,{cache:'no-store'});const r=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(r));if(!validState(r))throw new Error('执行状态响应格式异常。');draw(r);
   if(r.authorizationDenied)text('#result-message','本轮在领取时被拒绝，没有执行结果。');
   else if(r.status!=='not_started'){await readEvents();if(!active())return;if(r.resultAvailable===true&&!resultLoaded)await readResult();else if(!r.resultAvailable&&!resultLoaded)text('#result-message',r.status==='terminated'?'已终止，但未提供完整结果文件。':'尚未提供完整结果；不能根据已有材料推断完成。');}
  }catch(e){if(active())draw(null,`执行状态暂无法确认：${e.message}。先查询，勿重复启动。`);}
  finally{querying=false;if(active()){buttons();if(refreshRequested){refreshRequested=false;refresh();}else if(state&&(['queued','running','finalizing'].includes(state.status)||state.stopReason==='WORKER_HEARTBEAT_LOST'||state.executionRequest?.status==='pending'))timer=setTimeout(refresh,2500);}}
 }
 $('#query-execution').onclick=refresh;$('#more-events').onclick=readEvents;$('#query-result').onclick=readResult;
 $('#start-agent').onclick=()=>{if(querying||!canSubmit()||executionPending.has(id))return;$('#start-contract').innerHTML=`<p><strong>${escape(sourceLabel(state.source))}</strong></p><p>任务：${escape(id)}</p><p>固定 commit：${escape(taskRecord.contract.baseCommit||'当前文件（无 Git 版本）')}</p>${contract({project:taskRecord.contract.projectId,revision:taskRecord.contract.requestedRevision,current:taskRecord.contract.current,expected:taskRecord.contract.expected,scope:taskRecord.contract.allowedPaths.join('\n'),allowTests:taskRecord.contract.allowTests,targets:taskRecord.contract.checkTargets.join('\n')})}`;$('#start-review').showModal();};
 $('#cancel-start').onclick=()=>$('#start-review').close();
 $('#confirm-start').onclick=async()=>{
  if(!active()||querying||!canSubmit()||executionPending.has(id))return;
  executionPending.add(id);$('#start-review').close();if(timer!==null)clearTimeout(timer);draw(null,'已发送启动请求，正在等待响应。请勿重复启动。');
  try{const response=await fetch(base,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const r=await response.json();executionPending.delete(id);if(!active())return;if(!response.ok){draw(null,errorText(r)+'\n请查询权威状态后再操作，不自动重发。');return;}if(projectMode()&&response.status===202&&r.executionRequest?.targetId===id){await refresh();return;}if(![200,202].includes(response.status)||!validState(r))throw new Error('启动响应无法确认');draw(r);await refresh();}
  catch{executionPending.delete(id);if(active())draw(null,projectMode()?'提交结果暂无法确认。请先查询状态，勿直接重复提交。':'启动结果暂无法确认，后台可能已开始执行。请先查询状态，勿直接重复启动。');}
 };
 draw(null,'正在查询执行状态…');refresh();
 return refresh;
}

async function taskDetail(id){
 const epoch=pageEpoch;
 app.innerHTML=heading('SAVED CONTRACT','正在读取任务','按编号查询已保存约定，不会再次创建任务。');
 function failed(message){app.innerHTML=heading('SAVED CONTRACT','任务读取失败','未将读取错误当作任务不存在。')+`<section class="card"><p role="alert" style="white-space:pre-wrap">${escape(message)}</p><a class="button" href="#task">返回新建任务</a></section>`;}
 if(!/^[a-f0-9]{32}$/.test(id)){failed('任务编号格式错误：需要完整 32 位小写十六进制编号。');return;}
 try{const response=await fetch(`/api/tasks/${id}`,{cache:'no-store'});const data=await response.json();if(epoch!==pageEpoch)return;if(!response.ok){failed(errorText(data));return;}if(!validRecord(data)||data.taskId!==id){failed('任务记录响应损坏或格式不符合约定，无法展示。');return;}const c=data.contract;const created=new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'long',timeZone:'Asia/Shanghai'}).format(new Date(data.createdAt));app.innerHTML=heading('SAVED CONTRACT','任务工作台',data.taskId,'<span class="badge">真实保存 · 普通任务</span>')+`<div class="notice">任务约定已固定。准备、Agent 执行和最终检查分别查询；保存或准备成功不表示修复完成。</div><section class="card"><h2>固定版本与来源</h2><dl class="kv"><dt>任务编号</dt><dd>${escape(data.taskId)}</dd><dt>创建时间</dt><dd>${escape(created)}（北京时间）</dd><dt>准确 commit</dt><dd>${escape(c.baseCommit||'当前文件（无 Git 版本）')}</dd><dt>起始引用</dt><dd>${escape(c.requestedRevision)}</dd><dt>执行目录</dt><dd>${c.workspaceMode==='direct'?'直接修改当前仓库（文件变更立即可见）':'隔离 worktree（结果需另行应用到主目录）'}</dd><dt>步数上限</dt><dd>${c.maxSteps}</dd></dl><p class="hint">${c.workspaceMode==='direct'?'直接操作当前仓库文件，包含现有未提交内容；执行期间请避免同时编辑相同文件。':'隔离副本仅包含所选 commit，不包含源仓库未提交内容。'}保存约定不证明检查通过。</p></section><section class="card"><h2>已保存的任务约定</h2>${contract({project:c.projectId,revision:c.requestedRevision,current:c.current,expected:c.expected,scope:c.allowedPaths.join('\n'),allowTests:c.allowTests,targets:c.checkTargets.join('\n')})}</section><section class="card" id="preparation-card" aria-label="工作副本准备"></section><section class="card" id="execution-card" aria-label="Agent 执行与结果"></section><div class="actions"><a class="button secondary" href="#task">新建另一项任务</a><a class="button secondary" href="#tasks">返回真实任务记录</a></div><p class="hint">约定已固定；需求变化请新建任务。可收藏当前地址，刷新仅查询这份记录。</p>`; const refreshExecution=executionPanel(data); preparationPanel(data,{onPrepared:refreshExecution});
 app.insertAdjacentHTML('afterbegin',`<p class="badge">任务类型：${escape(({read_code:'阅读代码 · 只读分析',fix_bug:'修复 Bug',add_feature:'补充小功能'})[c.kind]||c.kind)}</p>`);
 ordinaryOrganization(id);
 }catch{if(epoch===pageEpoch)failed('后端不可用或响应无法读取，请确认本地服务正常后刷新。不会自动创建或重发任务。');}
}

async function taskList(deleted=false) {
 const epoch=pageEpoch;
 app.innerHTML=heading('TASK RECORDS',deleted?'任务回收站':'真实任务记录','最近保存的最多 50 项，按创建时间从新到旧。',`<a class="button secondary" href="${deleted?'#tasks':'#trash'}">${deleted?'返回任务记录':'回收站'}</a> <a class="button" href="#task">新建任务</a>`)+`<section class="card" id="task-list-content" aria-live="polite"><p>正在读取任务记录…</p></section><p id="task-lifecycle-message" role="status"></p><p class="hint">删除后可在回收站恢复，日志、Diff、工作目录和代码修改均保留。恢复后需要手动提交已退出队列的任务。执行中和待核实的任务暂不能删除。每次删除仅影响选中的轮次。回收站支持彻底删除，日志、Diff 和撤回材料将随本轮清除。</p><dialog id="delete-review"><h2>移入回收站</h2><p id="delete-summary"></p><p>本轮将从任务记录和项目历史隐藏，可在回收站恢复。</p><div class="actions"><button id="confirm-delete">确认删除</button><button class="secondary" id="cancel-delete">取消</button></div></dialog><dialog id="purge-review"><h2>确认彻底删除本轮</h2><p id="purge-summary"></p><p class="notice">本轮数据库记录、日志、Diff、撤回备份和专属隔离副本将永久清除，无法恢复。直接模式的项目源码修改继续保留。</p><div class="actions"><button id="confirm-purge">确认彻底删除</button><button class="secondary" id="cancel-purge">取消</button></div></dialog>`;
 const content=document.querySelector('#task-list-content');
 try{
  const response=await fetch(deleted?'/api/trash':'/api/tasks',{cache:'no-store'});const data=await response.json();if(pageEpoch!==epoch)return;
  if(!response.ok)throw new Error(errorText(data));
  const statuses={not_started:'尚未执行',queued:'已入队',running:'执行中',finalizing:'正在整理',terminated:'执行已终止',unknown:'状态暂无法确认'};
  if(!Array.isArray(data.tasks)||data.tasks.length>50||!data.tasks.every(t=>/^[a-f0-9]{32}$/.test(t.taskId)&&typeof t.createdAt==='string'&&!Number.isNaN(Date.parse(t.createdAt))&&typeof t.projectId==='string'&&typeof t.current==='string'&&Object.hasOwn(statuses,t.executionStatus)&&typeof t.resultAvailable==='boolean'))throw new Error('任务列表响应格式异常，不能作为空列表展示。');
  const removed=new Set(), operations=new Map(), purgeOperations=new Set();
  let selected=null, selectedPurge=null;
  const dialog=document.querySelector('#delete-review');
  const message=document.querySelector('#task-lifecycle-message');
  function drawRows(){
   const visible=data.tasks.filter(task=>!removed.has(task.taskId));
   content.innerHTML=visible.length?`<div class="table-scroll"><table class="history task-records"><thead><tr><th>需求摘要 / 任务编号</th><th>项目</th><th>创建时间（北京时间）</th><th>执行状态</th><th>结果</th><th>操作</th></tr></thead><tbody>${visible.map(t=>{
    const index=data.tasks.indexOf(t), unknown=operations.get(t.taskId)==='unknown';
    return `<tr><td><div class="task-summary">${escape(t.current.length>160?t.current.slice(0,160)+'…':t.current)}</div><small class="muted">${escape(t.taskId)}</small></td><td>${escape(t.projectId)}</td><td>${escape(displayTime(t.createdAt))}</td><td>${statuses[t.executionStatus]}</td><td>${t.resultAvailable?'有结果记录':'未提供结果'}</td><td><a href="#/tasks/${t.taskId}">查看详情 →</a> <button class="secondary" id="task-lifecycle-${index}" ${unknown||t.purgePending||!deleted&&['running','finalizing','unknown'].includes(t.executionStatus)?'disabled':''}>${unknown?'操作待核实':deleted?'恢复':'删除'}</button>${deleted?` <button class="secondary" id="task-purge-${index}" ${unknown?'disabled':''}>${t.purgePending?'重试清理':'彻底删除'}</button>`:''}${unknown?` <button class="secondary" id="task-reconcile-${index}">核实本轮状态</button>`:''}</td></tr>`;
   }).join('')}</tbody></table></div>`:`<div class="empty"><p>${operations.size?'正在提交操作…':deleted?'回收站为空。':'尚无保存的任务。'}</p><a class="button" href="#task">新建第一个任务</a></div>`;
   visible.forEach(task=>{
    const index=data.tasks.indexOf(task);
    document.querySelector(`#task-lifecycle-${index}`).onclick=()=>{
     if(operations.has(task.taskId)||task.purgePending)return;
     if(deleted)return change(task);
     selected=task;
     document.querySelector('#delete-summary').textContent=task.current+' · '+task.taskId;
     dialog.showModal();
    };
    if(deleted){
     document.querySelector(`#task-purge-${index}`).onclick=()=>{
      if(operations.has(task.taskId))return;
      selectedPurge=task;
      document.querySelector('#purge-summary').textContent=task.current+' · '+task.taskId;
      document.querySelector('#purge-review').showModal();
     };
    }
    if(operations.get(task.taskId)==='unknown'){
     document.querySelector(`#task-reconcile-${index}`).onclick=()=>reconcile(task);
    }
   });
  }
  async function reconcile(task){
   const button=document.querySelector(`#task-reconcile-${data.tasks.indexOf(task)}`);
   if(button)button.disabled=true;
   try{
    const response=await fetch(`/api/tasks/${task.taskId}/${purgeOperations.has(task.taskId)?'purge':'lifecycle'}`,{cache:'no-store'});
    const result=await response.json();
    if(pageEpoch!==epoch)return;
    if(!response.ok)throw new Error(errorText(result));
    const permanent=purgeOperations.has(task.taskId);
    if(result.taskId!==task.taskId||(permanent?typeof result.purged!=='boolean':typeof result.deleted!=='boolean'))throw new Error('核实响应格式异常');
    if(permanent&&result.status==='purging'){
     task.purgePending=true;
     operations.delete(task.taskId);
     message.textContent='清理未完成，可点击本轮的“重试清理”继续。';
    }else if(permanent?result.purged:result.deleted===!deleted){
     operations.delete(task.taskId);
     removed.add(task.taskId);
     message.textContent=permanent?'已确认彻底删除。':deleted?'已确认恢复。':'已确认移入回收站。';
    }else{
     message.textContent='当前记录尚未反映本次操作，结果仍待核实，可稍后再次查询。';
    }
   }catch(e){
    if(pageEpoch===epoch)message.textContent=`操作结果待核实：${e.message}。可单独查询本轮状态。`;
   }finally{
    if(pageEpoch===epoch)drawRows();
   }
  }
  async function change(task,permanent=false){
   if(operations.has(task.taskId)||removed.has(task.taskId)||pageEpoch!==epoch)return;
   if(permanent)purgeOperations.add(task.taskId);
   else purgeOperations.delete(task.taskId);
   operations.set(task.taskId,'pending');
   removed.add(task.taskId);
   dialog.close();
   document.querySelector('#purge-review').close();
   drawRows();
   message.textContent=permanent?'正在后台彻底删除…':deleted?'正在后台确认恢复…':'正在后台确认删除…';
   try{
    const response=await fetch(`/api/tasks/${task.taskId}/${permanent?'purge':'lifecycle'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(permanent?{confirmTaskId:task.taskId}:{deleted:!deleted})});
    const result=await response.json();
    if(pageEpoch!==epoch)return;
    if(!response.ok&&response.status>=400&&response.status<500){
     operations.delete(task.taskId);
     removed.delete(task.taskId);
     message.textContent=`${permanent?'彻底删除':deleted?'恢复':'删除'}失败：${errorText(result)}。已恢复该行。`;
     drawRows();
     return;
    }
    if(!response.ok)throw new Error(errorText(result));
    if(result.taskId!==task.taskId||(permanent?result.purged!==true:result.deleted!==!deleted))throw new Error('操作响应格式异常');
    operations.delete(task.taskId);
    message.textContent=permanent?'已彻底删除。':deleted?'已恢复。':'已移入回收站。';
    drawRows();
   }catch(e){
    if(pageEpoch!==epoch)return;
    operations.set(task.taskId,'unknown');
    removed.delete(task.taskId);
    message.textContent=`操作结果待核实：${e.message}。正在单独查询本轮状态…`;
    drawRows();
    await reconcile(task);
   }
  }
  drawRows();
  document.querySelector('#confirm-delete').onclick=()=>{
   const task=selected;
   selected=null;
   if(task)return change(task);
  };
  document.querySelector('#cancel-delete').onclick=()=>dialog.close();
  document.querySelector('#confirm-purge').onclick=()=>{
   const task=selectedPurge;
   selectedPurge=null;
   if(task)return change(task,true);
  };
  document.querySelector('#cancel-purge').onclick=()=>document.querySelector('#purge-review').close();
 }catch(e){if(pageEpoch!==epoch)return;content.innerHTML=`<p role="alert" style="white-space:pre-wrap">任务记录暂无法读取：${escape(e.message)}。这不表示没有任务。</p><button class="secondary" id="retry-task-list">重新读取</button>`;document.querySelector('#retry-task-list').onclick=()=>taskList(deleted);}
}

function historyMaterials(roundPath, artifacts, preferredRef=null) {
 const host=document.querySelector('#history-report-links');
 const valid=Array.isArray(artifacts)?artifacts.filter(a=>typeof a.artifactId==='string'&&typeof a.label==='string'&&typeof a.available==='boolean'):[];
 const ordered=[...valid].sort((a,b)=>Number(b.artifactId===preferredRef)-Number(a.artifactId===preferredRef));
 host.innerHTML='<p class="hint">登记报告与日志按脱敏文本读取；哈希只是原报告字段，本次未重新核验。分页/截断文本不作为原始可重放补丁。</p>'+ (ordered.length?ordered.map((a,i)=>`<details ${a.artifactId===preferredRef?'open':''}><summary>${escape(a.label)} · ${escape(a.kind)}${a.available?'':' · 未提供材料'}</summary><p id="history-material-note-${i}" class="hint">${a.available?'尚未读取':'材料缺失，不能当作空报告或通过。'}</p><pre id="history-material-text-${i}">尚未读取</pre><button class="secondary" id="history-material-load-${i}" ${a.available?'':'disabled'}>读取报告 / 材料</button></details>`).join(''):'<p class="empty">没有登记报告材料，不补造报告引用。</p>');
 const epoch=pageEpoch;
 ordered.forEach((a,i)=>{let offset=0,busy=false;const button=document.querySelector(`#history-material-load-${i}`);button.onclick=async()=>{if(busy||!a.available||epoch!==pageEpoch)return;busy=true;button.disabled=true;const note=document.querySelector(`#history-material-note-${i}`),pre=document.querySelector(`#history-material-text-${i}`);try{const response=await fetch(`${roundPath}/artifacts/${encodeURIComponent(a.artifactId)}?offset=${offset}&limit=16000`,{cache:'no-store'});const data=await response.json();if(epoch!==pageEpoch)return;if(!response.ok)throw new Error(errorText(data));if(data.artifactId!==a.artifactId||typeof data.content!=='string'||data.offset!==offset||!Number.isInteger(data.nextOffset)||data.nextOffset<offset||!Number.isInteger(data.totalChars)||data.totalChars<data.nextOffset||typeof data.hasMore!=='boolean'||typeof data.redacted!=='boolean'||typeof data.truncated!=='boolean'||data.hasMore&&data.nextOffset===offset)throw new Error('历史材料分页格式异常');pre.textContent=(offset?pre.textContent:'')+data.content;offset=data.nextOffset;note.textContent=`${data.redacted?'脱敏公开文本':'脱敏标志未确认'} · ${offset} / ${data.totalChars} 字符。${data.truncated?'公开材料已截断；不是完整原件。':'接口未标记截断；不表示字节一致或已核验。'}${data.hasMore?'还有后续内容。':'当前公开文本已读完。'}`;button.textContent=data.hasMore?'读取下一段':'读取完毕';button.disabled=!data.hasMore;}catch(e){if(epoch===pageEpoch){note.textContent=`材料读取失败：${e.message}。不视为空材料。`;button.disabled=false;}}finally{busy=false;}};});
}
function verificationVerdict(v){return v.sourceFormat==='verification_v1'&&v.rawStatus==='independently_verified'&&v.independentlyVerified===true?'报告记录独立验收通过（非本次重验）':v.sourceFormat==='legacy_verification'?'旧格式报告；不升级为新协议验收通过':'按原报告审查；未确认独立验收通过';}

// Registered history is read-only; these routes never expose execution actions.
const historyValue = value => value===null||value===undefined||value===''?'未记录':typeof value==='object'?JSON.stringify(value):String(value);
function historyFields(pairs){return `<dl class="kv">${pairs.map(([label,value])=>`<dt>${escape(label)}</dt><dd>${escape(historyValue(value))}</dd>`).join('')}</dl>`;}
const historyHref = parts => '#projects/'+parts.map(encodeURIComponent).join('/');
async function openRequirementDiscussion(taskPath) {
    const response = await fetch(taskPath+'/discussion', {method:'POST',
     headers:{'Content-Type':'application/json'}, body:'{}'});
    const record = await response.json();
    if (!response.ok) throw new Error(errorText(record));
    return record.conversationId;
}
function projectProgress(task) {
 if (task.origin === 'discussion' && task.roundCount === 0) return '需求讨论中';
 if (task.userReview) return task.userReview.decision === 'accepted' ? '用户已接收' : '用户要求修改';
 return ({queued:'排队中',running:'执行中',unknown:'执行待核实',terminated:'已终止 · 待审查'})[task.dispatchStatus]
   || (task.mode === 'ordinary' ? '暂无调度记录' : '见实验记录');
}
async function ordinaryOrganization(id){
 const epoch=pageEpoch;
 try{const response=await fetch(`/api/tasks/${id}/organization`,{cache:'no-store'}),info=await response.json();
  if(pageEpoch!==epoch||!response.ok||info?.mode!=='ordinary'||!info.taskKey)return;
  app.insertAdjacentHTML('afterbegin',`<p><a class="button secondary" href="${historyHref([info.projectId,'tasks',info.taskKey])}">返回所属需求与全部轮次 →</a> · ${escape(info.round_id)}</p>`);
 }catch{}
}
function ordinaryRoundControl(taskPath,project,task){
 const epoch=pageEpoch, button=document.querySelector('#new-ordinary-round');let busy=false;
 button.onclick=async()=>{if(busy||epoch!==pageEpoch)return;
  const input=document.querySelector('#round-max-steps');
  const maxSteps=Number(input.value);
  const workspaceMode=document.querySelector('#round-workspace-mode').value;
  if(!Number.isInteger(maxSteps)||maxSteps<1||maxSteps>50){document.querySelector('#ordinary-round-message').textContent='步数上限必须是 1–50 的整数。';return;}
  busy=true;button.disabled=true;
  document.querySelector('#ordinary-round-message').textContent='正在保存新轮次，不会启动模型…';
  try{const response=await fetch(taskPath+'/rounds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({maxSteps,workspaceMode})}),record=await response.json();
   if(epoch!==pageEpoch)return;
   if(!response.ok){busy=false;button.disabled=false;throw new Error(errorText(record));}
   if(response.status!==201||!validRecord(record)||record.contract.projectId!==project)throw new Error('保存响应无法确认');
   location.hash='/tasks/'+record.taskId;
  }catch(e){if(epoch===pageEpoch)document.querySelector('#ordinary-round-message').textContent=`${e.message}。请重新打开需求轮次列表核对，不自动重发。`;}
 };
}
async function experimentStartControl(taskPath, projectId, taskKey) {
 const epoch=pageEpoch, host=document.querySelector('#experiment-run-control');
 const active=()=>pageEpoch===epoch;
 let busy=false,state=null;
 const labels={starting:'正在建立新轮次',running:'新轮次执行中',completed:'新轮次已完成',failed:'新轮次未通过',interrupted:'新轮次已中断'};
 function draw(message=''){
  const run=state?.run;
  host.innerHTML=`<p id="experiment-run-message" role="status">${escape(message||(run?`${labels[run.status]||run.status}${run.roundId?' · '+run.roundId:''}`:'当前没有进行中的实验轮次。'))}</p>${state?.startBlockReason?`<p class="hint">启动暂不可用：${escape(state.startBlockReason)}</p>`:''}<dl class="kv"><dt>当前存活</dt><dd>${escape(state?.liveness||'未查询')}</dd><dt>最近轮次</dt><dd>${escape(run?.roundId||'无')}</dd><dt>最近结论</dt><dd>${escape(run?.conclusion||'未记录')}</dd></dl><div class="actions"><button id="start-experiment-run" ${!state?.canStart||busy?'disabled':''}>${busy?'正在启动…':'开始新一轮真实实验'}</button><button class="secondary" id="query-experiment-run" ${busy?'disabled':''}>查询运行状态</button>${run?.roundId?`<a class="button secondary" href="${historyHref([projectId,'tasks',taskKey,'rounds',run.roundId])}">查看最近轮次 →</a>`:''}</div>`;
  document.querySelector('#query-experiment-run').onclick=load;
  document.querySelector('#start-experiment-run').onclick=()=>{if(!state?.canStart||busy)return;document.querySelector('#experiment-start-review').showModal();};
 }
 async function load(){
  if(!active()||busy)return;busy=true;let message='';draw('正在查询实验运行状态…');
  try{const response=await fetch(`${taskPath}/runs/status`,{cache:'no-store'});const data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));if(!data||typeof data.canStart!=='boolean'||typeof data.active!=='boolean'||!['confirmed','not_assessed'].includes(data.liveness)||data.run!==null&&(typeof data.run!=='object'||typeof data.run.status!=='string'))throw new Error('实验状态响应格式异常');state=data;}
  catch(e){if(active()){state=null;message=`实验状态暂无法确认：${e.message}。不自动重发启动请求。`;}}
  finally{busy=false;}
  if(active())draw(message);
 }
 document.querySelector('#cancel-experiment-start').onclick=()=>document.querySelector('#experiment-start-review').close();
 document.querySelector('#confirm-experiment-start').onclick=async()=>{
  if(!active()||busy||!state?.canStart)return;busy=true;document.querySelector('#experiment-start-review').close();draw('已请求启动真实实验；正在等待轮次编号。');
  try{const response=await fetch(`${taskPath}/runs`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));if(response.status!==202||data?.projectId!==projectId||data?.taskKey!==taskKey||!/^[0-9]{8}-[0-9]{6}_[a-f0-9]{8}$/.test(data.roundId||''))throw new Error('启动响应无法确认');location.hash=historyHref([projectId,'tasks',taskKey,'rounds',data.roundId]);}
  catch(e){if(active()){busy=false;state=null;draw(`启动结果暂无法确认：${e.message}。请查询运行状态，勿直接重复启动。`);}}
 };
 await load();
}
let projectHistoryRequest=0;
async function projectHistory(parts=[]) {
 const request=++projectHistoryRequest;
 const epoch=pageEpoch, active=()=>pageEpoch===epoch&&request===projectHistoryRequest;
 const shell=(title,body)=>{app.innerHTML=heading('PROJECT HISTORY',escape(title),'按项目查看需求与运行轮次，读取记录不会自动启动任务。','<span class="badge">项目记录</span>')+`<div class="actions"><a class="button secondary" href="#projects">项目历史</a><a class="button secondary" href="#tasks">普通任务记录</a>${projectMode()&&parts[0]?`<a class="button secondary" href="${escape(projectAccessHref(parts[0]))}">成员与执行审批</a>`:''}</div><div style="margin-top:20px">${body}</div>`;};
 shell('项目历史','<section class="card"><p role="status">正在读取历史记录…</p></section>');
 const get=async path=>{const response=await fetch(path,{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(errorText(data));return data;};
 const table=(headers,rows)=>`<div class="table-scroll"><table class="history"><thead><tr>${headers.map(h=>`<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
 try{
  if(!parts.length){const data=await get('/api/projects');if(!active())return;if(!Array.isArray(data.projects))throw new Error('项目列表格式异常');shell('项目与需求',`<section class="card"><h2>选择项目</h2>${data.projects.length?table(['项目','记录类型','入口'],data.projects.map(p=>`<tr><td>${escape(p.name)}<br><small>${escape(p.projectId)}</small></td><td>${escape(({available:'实验记录',missing:'历史根目录缺失',not_applicable:'普通需求'}[p.historyAvailability])||historyValue(p.historyAvailability))}</td><td><a href="${historyHref([p.projectId,'tasks'])}">查看需求 →</a>${projectMode()?` · <a href="${escape(projectAccessHref(p.projectId))}">成员与执行审批</a>`:''}</td></tr>`)):'<p class="empty">没有已登记项目。</p>'}</section>`);return;}
  const [projectId,taskSegment,taskKey,roundSegment,roundId,verifySegment,verifyId]=parts;
  if(taskSegment!=='tasks'||(roundSegment&&roundSegment!=='rounds')||(verifySegment&&verifySegment!=='verifications')||parts.length>7)throw new Error('历史页面地址无效');
  const taskBase=`/api/projects/${encodeURIComponent(projectId)}/tasks`,taskPath=taskKey?`${taskBase}/${encodeURIComponent(taskKey)}`:taskBase;
  if(!taskKey){
   const data=await get(taskBase);if(!active())return;
   if(!Array.isArray(data.tasks))throw new Error('需求列表格式异常');
   const tasks=[...data.tasks].sort((a,b)=>(Date.parse(b.createdAt)||0)-(Date.parse(a.createdAt)||0)||String(b.taskKey).localeCompare(String(a.taskKey)));
   shell(`${projectId} · 需求`, `<section class="card"><div class="actions"><button class="secondary" id="refresh-project-history">重新加载需求</button></div><p class="hint">按需求创建时间从新到旧排列。</p>${tasks.length?table(['需求','创建时间（北京时间）','模式','轮次','进展','入口'],tasks.map(t=>`<tr><td>${escape(t.title||t.taskKey)}<br><small>${escape(t.taskKey)}</small>${t.issueId?`<br><small>#${escape(t.issueId)}</small>`:''}</td><td>${escape(displayTime(t.createdAt))}</td><td>${escape(t.mode==='experiment'?'历史实验':t.mode==='ordinary'?'普通任务':historyValue(t.mode))}</td><td>${escape(historyValue(t.roundCount))}</td><td>${escape(projectProgress(t))}</td><td><a href="${historyHref([projectId,'tasks',t.taskKey])}">查看需求与轮次 →</a></td></tr>`)):'<p class="empty">当前项目没有已登记需求；普通任务也可从原任务记录入口查找。</p>'}</section>`);
   if (data.canDiscuss) {
    const draft = document.createElement('details');
    draft.className = 'card';
    draft.innerHTML = '<summary>新建需求讨论</summary><form><label>标题<input name="title" maxlength="120" required></label><label>背景<textarea name="current" maxlength="4000" required></textarea></label><label>预期结果<textarea name="expected" maxlength="4000" required></textarea></label><button>保存需求并打开讨论</button><p role="status"></p></form>';
    app.querySelector('.card').before(draft);
    const form = draft.querySelector('form');
    let requestId = crypto.randomUUID().replaceAll('-','');
    let saved = null;
    form.oninput = () => {requestId = crypto.randomUUID().replaceAll('-','');saved=null;};
    form.onsubmit = async event => {
     event.preventDefault();
     const fields = [...form.querySelectorAll('input,textarea,button')];
     if (fields[0].disabled) return;
     const payload = {...Object.fromEntries(new FormData(form)),requestId};
     fields.forEach(field => {field.disabled=true;});
     try {
      if (!saved) {
       const res = await fetch(taskBase,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
       const item = await res.json();
       if (!res.ok) throw new Error(errorText(item));
       saved = item.taskKey;
      }
      const chatId = await openRequirementDiscussion(taskBase+'/'+encodeURIComponent(saved));
      if (active()) location.hash = 'chat/'+chatId;
     } catch(error) {
      if(active()) {form.querySelector('p').textContent=error.message;fields.forEach(field=>{field.disabled=false;});}
     }
    };
   }
   const discussion = document.createElement('section');
   discussion.className = 'card';
   discussion.innerHTML = '<h2>项目讨论</h2><ul></ul><button class="secondary">读取项目会话</button><p role="status"></p>';
   if (data.canDiscuss||projectMode()) app.append(discussion);
   const more = discussion.querySelector('button');
   let cursor = null;
   more.onclick = async () => {
    more.disabled = true;
    try {
     const query = new URLSearchParams({project:projectId,limit:'25'});
     if (cursor) query.set('cursor',cursor);
     const page = await get('/api/conversations?' + query);
     if (!active()) return;
     for (const chat of page.conversations) {
      const link = document.createElement('a');
      link.href = '#chat/' + encodeURIComponent(chat.conversationId);
      link.textContent = chat.title;
      const item = document.createElement('li');
      item.append(link);
      discussion.querySelector('ul').append(item);
     }
     cursor = page.nextCursor;
     more.hidden = !page.hasMore;
     more.textContent = '更多会话';
     discussion.querySelector('p').textContent = discussion.querySelector('li') ? '' : '暂无项目会话';
    } catch(error) {discussion.querySelector('p').textContent=error.message;}
    finally {more.disabled=false;}
   };
   document.querySelector('#refresh-project-history').onclick=()=>projectHistory(parts);
   return;
  }
  if(!roundId&&!['browser-use-5998','pydantic-ai-10525'].includes(taskKey)){
   const [task,data]=await Promise.all([get(taskPath),get(taskPath+'/rounds')]);if(!active())return;
   if(task.mode!=='ordinary'||!Array.isArray(data.rounds))throw new Error('普通需求格式异常');
   const c=task.contract;
    shell(task.title||taskKey,`<section class="card"><h2>普通需求</h2>${historyFields([['项目',projectId],['需求编号',taskKey],['任务类型',c.kind],['背景 / 当前行为',c.current],['期望分析 / 行为',c.expected],['原版本引用',c.requestedRevision],['允许修改',c.kind==='read_code'?'只读':c.allowedPaths.join('、')]])}<p>Git 任务每轮重新解析版本；普通文件按当前内容执行。旧轮次快照保留。</p><label>新轮次执行目录<select id="round-workspace-mode"><option value="worktree" ${c.workspaceMode!=='direct'?'selected':''}>隔离副本（worktree）</option><option value="direct" ${c.workspaceMode==='direct'?'selected':''}>直接修改当前仓库</option></select></label><label>新轮次最大执行次数<input id="round-max-steps" type="number" min="1" max="50" step="1" value="50" required><span class="hint">设置本轮 LLM 最大执行步数（1–50）。</span></label><button id="new-ordinary-round">准备新一轮（不启动模型）</button><p id="ordinary-round-message" role="status"></p></section><section class="card"><h2>全部运行轮次</h2>${table(['轮次','创建时间','固定版本','入口','会话与审查'],data.rounds.map(r=>`<tr><td>${escape(r.roundId)}</td><td>${escape(displayTime(r.createdAt))}</td><td>${escape(r.baseCommit||'当前文件（无 Git 版本）')}</td><td><a href="#/tasks/${escape(r.runTaskId)}">查看执行与结果 →</a></td><td>${r.conversationId?`<a href="#chat/${escape(r.conversationId)}">原讨论</a> · `:''}<button class="secondary" data-review-task="${escape(r.runTaskId)}">围绕此版继续</button>${r.parentTaskId?`<p><a href="#/tasks/${escape(r.parentTaskId)}">上一版交付</a> · ${escape(r.feedback)}</p>`:''}${r.userReview?`<p>${escape(projectProgress(r))}</p>`:''}</td></tr>`))}</section>`);
   const overview = document.createElement('section');
   overview.className = 'card';
   const accepted = data.rounds.filter(r => r.userReview?.decision === 'accepted')
    .sort((a,b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))[0];
   overview.innerHTML = `<h2>需求讨论与交付</h2>
    <button class="secondary" id="open-requirement-chat">打开需求讨论</button>
    <p>${accepted ? `最近已接收版本：<a href="#/tasks/${escape(accepted.runTaskId)}">${escape(accepted.roundId)}</a> · ${escape(displayTime(accepted.userReview.at))}` : '尚无人工接收的版本。'}</p>`;
   app.querySelector('.card').before(overview);
   const openButton = overview.querySelector('button');
   if(task.canDevelop===false){
    openButton.hidden=true;
    const newRound=document.querySelector('#new-ordinary-round');
    newRound.hidden=true;
    document.querySelector('#round-workspace-mode').closest('label').hidden=true;
    document.querySelector('#round-max-steps').closest('label').hidden=true;
   }
   openButton.onclick = async () => {
    openButton.disabled = true;
    try {
     const chatId = await openRequirementDiscussion(taskPath);
     if (active()) location.hash = 'chat/' + chatId;
    } catch(error) {
     if (active()) {openButton.disabled=false;document.querySelector('#ordinary-round-message').textContent=error.message;}
    }
   };
   for (const button of app.querySelectorAll('[data-review-task]')) {
    if(task.canDevelop===false)button.hidden=true;
    const round = data.rounds.find(item => item.runTaskId === button.dataset.reviewTask);
    button.onclick = async () => {
     button.disabled = true;
     try {
      const status = await get(`/api/tasks/${round.runTaskId}/execution`);
      if (status.status !== 'terminated' || !status.resultAvailable)
       throw new Error('本轮还没有可审查的终止交付。');
      const delivery = await get(`/api/tasks/${round.runTaskId}/execution/result`);
      if (delivery.patchExport?.status !== 'saved' || delivery.review?.status !== 'passed')
       throw new Error('这版缺少完整补丁或范围审查。可在 Chat 选择继续需求，按当前版本开新轮。');
      if (!active()) return;
      const chatId = round.conversationId || await openRequirementDiscussion(taskPath);
      if(active()) location.hash = `chat/${chatId}/${round.runTaskId}`;
     } catch(error) {
      if(active()) {button.disabled=false;document.querySelector('#ordinary-round-message').textContent=error.message;}
     }
    };
   }
   ordinaryRoundControl(taskPath,projectId,taskKey);return 'ordinary';
  }
  if(!roundId){const [task,roundData]=await Promise.all([get(taskPath),get(taskPath+'/rounds')]);if(!active())return;if(!Array.isArray(roundData.rounds))throw new Error('轮次列表格式异常');shell(task.title||taskKey,`<section class="card"><h2>需求身份</h2>${historyFields([['项目',projectId],['需求',taskKey],['模式',task.mode],['Issue',task.issueId],['需求引用',task.requirementRef],['需求版本',task.requirementRevision],['历史可用性',task.historyAvailability]])}<p class="hint">当前需求引用不是历史轮次快照，缺失版本不补造。</p><div id="legacy-history-links"></div></section><section class="card"><h2>多轮记录</h2><p class="hint">prepare 目录不代表从未运行模型；verify-02 是同轮追加验收，不是第二次模型运行。历史运行状态不用于推断当前存活。</p>${roundData.rounds.length?table(['轮次 / 来源','原总报告结论','Agent 停止状态','P 导出','独立验收次数','入口'],roundData.rounds.filter(r=>r.association!=='unlinked'&&r.taskKey!==null).map(r=>`<tr><td>${escape(r.roundId)}<br>${escape(historyValue(r.entry))} / ${escape(historyValue(r.sourceFormat))}<br>${escape(r.createdAt?displayTime(r.createdAt):'时间未记录')}</td><td>${escape(historyValue(r.originalRunConclusion))}</td><td>${escape(historyValue(r.agentStatus))}</td><td>${escape(historyValue(r.exportStatus))}</td><td>${escape(historyValue(r.verificationCount))}</td><td><a href="${historyHref([projectId,'tasks',taskKey,'rounds',r.roundId])}">查看轮次 →</a></td></tr>`)):'<p class="empty">没有可明确关联的轮次，不补造运行。</p>'}</section><section class="card"><h2>未关联材料</h2>${roundData.rounds.filter(r=>r.association==='unlinked'||r.taskKey===null).map(r=>`<p><a href="${historyHref([projectId,'tasks',taskKey,'rounds',r.roundId])}">${escape(r.roundId)} →</a> · ${escape(historyValue(r.sourceFormat))} · 不推断归属</p>`).join('')||'<p class="hint">没有返回未关联材料。</p>'}<p>${escape(Array.isArray(roundData.warnings)?roundData.warnings.join('；'):'')}</p></section>`);return;}
  const roundPath=`${taskPath}/rounds/${encodeURIComponent(roundId)}`;
  const [round,verifyData]=await Promise.all([get(roundPath),get(roundPath+'/verifications')]);if(!active())return;if(!Array.isArray(verifyData.verifications))throw new Error('独立验收列表格式异常');
  if(verifySegment){const v=verifyData.verifications.find(v=>v.verificationId===verifyId);if(!v)throw new Error('未找到该次独立验收记录');shell(`${verifyId} · 独立验收报告`, `<p><a href="${historyHref([projectId,'tasks',taskKey,'rounds',roundId])}">← 返回所属轮次</a></p><section class="card"><h2>这一次独立验收</h2>${historyFields([['轮次',roundId],['验收编号',v.verificationId],['来源格式',v.sourceFormat],['原报告状态',v.rawStatus],['报告判读',verificationVerdict(v)],['原独立验收标志',v.independentlyVerified],['候选 P SHA256',v.candidateSha256],['内容完整性',v.integrityStatus],['时间',v.createdAt?displayTime(v.createdAt):null]])}<p class="notice">仅展示原报告记录，不是本次重新验收；读取哈希不表示本次已核验文件完整性。追加 verify 不等于重新运行模型。</p><h3>原报告中的检查记录</h3>${Array.isArray(v.tests)&&v.tests.length?table(['目标','退出码','超时','计数','符合预期'],v.tests.map(t=>`<tr><td>${escape(historyValue(t.target))}</td><td>${escape(historyValue(t.exitCode))}</td><td>${escape(historyValue(t.timedOut))}</td><td>${escape(historyValue(t.counts))}</td><td>${escape(historyValue(t.matchesExpectation))}</td></tr>`)):'<p class="hint">未记录逐项检查。</p>'}<p>${escape(Array.isArray(v.warnings)?v.warnings.join('；'):'')}</p><div id="history-report-links"></div></section>`);historyMaterials(roundPath,round.artifacts,v.reportRef);return;}
  shell(`${round.association==='unlinked'?'未关联材料':taskKey} · ${roundId}`,`<p><a href="${historyHref([projectId,'tasks',taskKey])}">← 返回需求轮次列表</a></p><section class="card"><h2>本轮身份与证据</h2>${historyFields([['项目',projectId],['需求',round.taskKey],['导航登记',taskKey],['轮次',roundId],['入口类型',round.entry],['模式',round.mode],['归属需求',round.taskKey],['关联状态',round.association],['准备状态',round.preparationStatus],['内容完整性',round.integrityStatus],['来源格式',round.sourceFormat],['原总报告结论',round.originalRunConclusion],['Agent 停止状态',round.agentStatus],['P 导出状态',round.exportStatus],['固定 G',round.baseCommit],['冻结 R SHA256',round.regressionSha256],['候选 P SHA256',round.candidateSha256],['需求快照',round.requirementSnapshotRef],['证据状态',round.evidenceState],['存活评估',round.liveness]])}<p class="notice">${round.association==='unlinked'?'未关联材料：不能声称属于该需求的一轮修复。':''}模型结束、固定检查、补丁导出与独立验收分开审查。历史记录不表示实时执行；缺失字段保持未记录。</p><p style="white-space:pre-wrap">${escape(Array.isArray(round.warnings)?round.warnings.join('\n'):historyValue(round.warnings))}</p></section><section class="card"><h2>分阶段报告</h2><div id="history-report-links"><p class="hint">正在整理后端登记的报告材料。</p></div></section><section class="card"><h2>各次独立验收</h2>${verifyData.verifications.length?table(['验收','来源格式','原报告状态','独立验收标志','完整性','入口'],verifyData.verifications.map(v=>`<tr><td>${escape(v.verificationId)}</td><td>${escape(historyValue(v.sourceFormat))}</td><td>${escape(historyValue(v.rawStatus))}</td><td>${escape(verificationVerdict(v))}<br>原标志：${escape(historyValue(v.independentlyVerified))}</td><td>${escape(historyValue(v.integrityStatus))}</td><td><a href="${historyHref([projectId,'tasks',taskKey,'rounds',roundId,'verifications',v.verificationId])}">查看报告 →</a></td></tr>`)):'<p class="empty">没有独立验收报告，不表示失败或通过。</p>'}</section>`);historyMaterials(roundPath,round.artifacts);
 }catch(e){if(active())shell('历史读取失败',`<section class="card"><p role="alert">${escape(e.message)}。不将读取失败当作空历史，也不补写缺失证据。</p><a href="#projects">返回项目历史</a></section>`);}
}

let chatCleanup = () => {};
async function render() {
 executionCleanup();
 chatCleanup();
 chatCleanup = () => {};
 pageEpoch++;
 if (!renderAuth(app)) return;
 const epoch=pageEpoch;
 const hash=location.hash.slice(1);
 if(!hash || hash==='chat' || hash.startsWith('chat/')) {
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash==='#chat'));
  document.querySelector('#breadcrumb').textContent='工作台 / Chat';
  const parts=hash.split('/');
  const conversationId=parts[1] || null;
  const reviewTask = parts[2] || null;
  if(parts.length>3 || reviewTask && !/^[0-9a-f]{32}$/.test(reviewTask) || conversationId && !/^[0-9a-f]{32}$/.test(conversationId)) {
   app.innerHTML='<p role="alert">会话链接无效。</p><a class="button" href="#chat">返回 Chat</a>';
  } else {
   app.innerHTML='<p class="muted">正在载入会话…</p>';
   try {
    const {mountChat}=await import('./chat.js');
    if(pageEpoch!==epoch)return;
    chatCleanup=mountChat(app,conversationId,reviewTask);
   } catch {
    if(pageEpoch===epoch)app.innerHTML='<p role="alert">Chat 页面加载失败，请刷新重试。</p>';
   }
  }
 }
 else if(hash==='projects'||hash.startsWith('projects/')) {
  const parts=hash.split('/').slice(1).map(decodeURIComponent);
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash==='#projects'));
  if(projectMode()&&parts[1]==='access'&&parts.length<=3){
   document.querySelector('#breadcrumb').textContent='工作台 / 项目成员与执行审批';
   await mountProjectAccess(app,parts[0],parts[2],()=>pageEpoch===epoch);
   return;
  }
  if(projectMode()&&parts[1]==='audit'&&parts.length===3){
   document.querySelector('#breadcrumb').textContent='工作台 / 执行审计';
   await mountExecutionAudit(app,parts[0],parts[2],()=>pageEpoch===epoch);
   return;
  }
  const mode=await projectHistory(parts);
  if(pageEpoch===epoch&&mode!=='ordinary'&&parts.length===3&&parts[1]==='tasks'){
   app.insertAdjacentHTML('beforeend',`<section class="card"><h2>运行控制</h2><div id="experiment-run-control"><p>正在查询实验运行状态…</p></div><dialog id="experiment-start-review" aria-label="启动真实实验确认"><h2>确认启动真实实验</h2><p><strong>${escape(parts[2])}</strong></p><p>本次会创建新的工作副本，调用已配置模型，随后导出候选补丁并在新副本中执行独立验收。</p><p class="notice">需求和检查目标由后端登记；网页不会提交自定义命令、环境或密钥。模型结束不等于独立验收通过。</p><div class="actions"><button id="confirm-experiment-start">确认启动</button><button class="secondary" id="cancel-experiment-start">取消</button></div></dialog></section>`);
   await experimentStartControl(`/api/projects/${encodeURIComponent(parts[0])}/tasks/${encodeURIComponent(parts[2])}`,parts[0],parts[2]);
  }else if(pageEpoch===epoch&&parts.length===5&&parts[1]==='tasks'&&parts[3]==='rounds'){
   try{
    const roundPath=`/api/projects/${encodeURIComponent(parts[0])}/tasks/${encodeURIComponent(parts[2])}/rounds/${encodeURIComponent(parts[4])}`;
    const response=await fetch(roundPath,{cache:'no-store'}),round=await response.json();
    if(pageEpoch===epoch&&response.ok){
     app.insertAdjacentHTML('beforeend',`<section class="card"><h2>当前执行状态</h2><dl class="kv"><dt>阶段</dt><dd>${escape(round.stage||'未记录')}</dd><dt>原总报告</dt><dd>${escape(round.originalRunConclusion||'未记录')}</dd><dt>准备</dt><dd>${escape(round.preparationStatus||'未记录')}</dd><dt>Agent</dt><dd>${escape(round.agentStatus||'未记录')}</dd><dt>P 导出</dt><dd>${escape(round.exportStatus||'未记录')}</dd></dl><p class="hint">运行期间自动刷新；模型结束、补丁导出与独立验收分别判断。</p></section>`);
     if(['running','preparing','starting_agent','selecting_verification','verifying'].includes(round.originalRunConclusion||round.stage))setTimeout(()=>{if(pageEpoch===epoch)render();},2500);
    }
   }catch{}
  }
  document.querySelector('#breadcrumb').textContent='工作台 / 项目历史';
 }
 else if(hash.startsWith('/tasks/')) { taskDetail(hash.slice(7)); document.querySelector('#breadcrumb').textContent='工作台 / 已保存约定'; }
 else {
 const [route,variant]=hash.split('/'); const page=route||'task';
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash===`#${page==='demo'?'task':page}`));
 document.querySelector('#breadcrumb').textContent='工作台 / '+({task:'任务约定',tasks:'真实任务记录',trash:'任务回收站',demo:'演示表单',execution:'执行记录（演示）',result:'结果证据（演示）',history:'演示记录'}[page]||'任务约定');
 if(page==='tasks')taskList();else if(page==='trash')taskList(true);else if(page==='advanced-task')advancedTask();else if(page==='demo') demoTask(); else if(page==='execution')execution();else if(page==='result')result(variant);else if(page==='history')app.innerHTML=heading('DEMO RECORDS','演示记录','以下仅为虚构演示，不包含真实任务历史。')+history();else task();
 }
 window.scrollTo(0,0);
}
initializeAuth(render).then(() => {
 window.addEventListener('hashchange', render);
 render();
});
