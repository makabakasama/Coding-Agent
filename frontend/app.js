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
function contract(data) { return `<dl class="kv"><dt>项目</dt><dd>${escape(data.project)}</dd><dt>起始版本</dt><dd>${escape(data.revision)}</dd><dt>当前行为</dt><dd>${escape(data.current)}</dd><dt>期望行为</dt><dd>${escape(data.expected)}</dd><dt>允许修改</dt><dd>${escape(data.scope)}</dd><dt>测试权限</dt><dd>${data.allowTests ? '允许运行固定检查' : '不允许运行测试'}</dd><dt>检查目标</dt><dd>${escape(data.allowTests ? data.targets : '暂不检查')}</dd></dl>`; }
function history() { return `<section class="card"><div class="section-head"><h2>任务记录</h2><span class="badge">离线示例 · 3 条</span></div><p class="hint">以下均为虚构演示，不含真实历史运行。选择不同终止情况，审查相应结果。</p><table class="history"><thead><tr><th>任务</th><th>来源</th><th>终止原因</th><th>实际检查</th><th></th></tr></thead><tbody>${[['finished','DEMO-001','模型主动结束','通过 · 2 项'],['interrupted','DEMO-002','中断','未执行'],['failed','DEMO-003','准备失败','未执行']].map(([id,name,reason,check])=>`<tr><td>${name} · 空输入处理</td><td>演示数据</td><td>${reason}</td><td>${check}</td><td><a href="#result/${id}">审查结果 →</a></td></tr>`).join('')}</tbody></table></section>`; }
function demoTask() {
 app.innerHTML = heading('DEMO CONTRACT', '演示任务表单', '约定目标、修改边界和检查方式，再进入独立副本。') + `<div class="grid"><form id="task-form"><section class="card"><div class="section-head"><h2>目标与起点</h2><span class="number">01 / 02</span></div><div class="two"><label>项目<select name="project"><option>示例 Python 项目</option></select><span class="hint">项目发现接口未接通，当前仅提供示例。</span></label><label>起始版本<input name="revision" required placeholder="填写准确 commit 或版本引用" maxlength="160"></label></div><label>当前行为<textarea name="current" required placeholder="什么输入或操作触发了什么问题？" maxlength="4000"></textarea></label><label>期望行为<textarea name="expected" required placeholder="修改后应该出现什么可观察的结果？" maxlength="4000"></textarea></label></section><section class="card"><div class="section-head"><h2>操作范围与检查</h2><span class="number">02 / 02</span></div><label>允许修改的文件 / 目录<textarea name="scope" required placeholder="每行一个仓库相对路径，例如 src/parser.py" maxlength="4000"></textarea></label><label class="check"><input type="checkbox" name="allowTests" checked>允许运行测试</label><label id="targets-label">固定检查目标<input name="targets" required placeholder="例如 tests/test_parser.py" maxlength="2000"><span class="hint">这里只约定目标，不会执行输入的命令。</span></label><p id="no-check" hidden class="hint">本次明确暂不检查；结果页需要保留“未执行”结论。</p></section><div class="actions"><button type="submit">预览任务约定 →</button><button type="button" class="secondary" id="fill-demo">填入演示需求</button></div></form><div><section class="card"><div class="eyebrow">WORKFLOW</div><h2 style="margin-top:16px">每一步都有可审查的产物</h2><p><strong>01　任务约定</strong><br><span class="muted">目标、起点、范围与检查目标</span></p><p><strong>02　独立副本与执行</strong><br><span class="muted">准备状态、工具记录与实际反馈</span></p><p><strong>03　结果交付</strong><br><span class="muted">修改、检查结果和未完成事项</span></p></section><div class="notice">真实启动尚未接通。预览与下载约定不会创建工作副本或调用模型。请勿在需求中填写密钥。</div><a class="button secondary" href="#execution">浏览执行页演示 →</a></div></div><div style="margin-top:30px">${history()}</div><dialog id="review" aria-label="任务约定核对"><div class="eyebrow">REVIEW CONTRACT</div><h2 style="margin-top:14px">提交前核对</h2><div id="review-content"></div><div class="notice">此约定仅保存在当前页面内存中。下载可留存；刷新后不保留。真实启动未接通。</div><div class="actions"><button id="download">下载任务约定</button><button disabled>准备副本并启动 · 未接通</button><button class="secondary" id="close-review">返回修改</button></div></dialog>`;
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
 return r?.schemaVersion===1 && /^[a-f0-9]{32}$/.test(r.taskId) && r.recordType==='actual' && r.status==='created' && typeof r.createdAt==='string' && !Number.isNaN(Date.parse(r.createdAt)) && c && /^[a-f0-9]{40,64}$/.test(c.baseCommit) && typeof c.projectId==='string' && typeof c.requestedRevision==='string' && typeof c.current==='string' && typeof c.expected==='string' && Array.isArray(c.allowedPaths) && c.allowedPaths.every(p=>typeof p==='string') && typeof c.allowTests==='boolean' && Array.isArray(c.checkTargets) && c.checkTargets.every(p=>typeof p==='string') && Number.isInteger(c.maxSteps);
}
function task() {
 const epoch=pageEpoch;
 app.innerHTML=heading('TASK CONTRACT','保存一份明确的任务约定','M1 · 保存约定并固定起始版本，尚不准备副本或启动 Agent。')+`<div class="grid"><form id="real-task"><section class="card"><h2>目标与起点</h2><label>项目<select name="projectId" required disabled><option value="">正在加载项目…</option></select></label><p id="project-state" role="status">正在查询可用项目…</p><button type="button" class="secondary" id="reload-projects">重新加载项目</button><label>起始版本<input name="revision" required maxlength="160" placeholder="准确 commit 或版本引用，如 main"></label><label>当前行为<textarea name="current" required maxlength="4000"></textarea></label><label>期望行为<textarea name="expected" required maxlength="4000"></textarea></label></section><section class="card"><h2>操作范围与检查</h2><label>允许修改的文件 / 目录<textarea name="scope" required placeholder="每行一项，使用仓库相对路径及 / 分隔符"></textarea></label><label class="check"><input name="allowTests" type="checkbox">允许运行测试</label><label id="real-targets">固定检查目标<textarea name="targets" placeholder="每行一个 pytest 路径或 文件.py::测试节点"></textarea><span class="hint">填写目标，不填写 python / pytest 命令或选项。</span></label><p id="real-no-check" hidden>本次明确暂不检查。</p></section><div id="save-error" class="notice" role="alert" hidden style="white-space:pre-wrap"></div><div class="actions"><button id="preview-real" disabled>预览任务约定 →</button></div></form><div><section class="card"><h2>本轮保存什么</h2><p>保存目标、允许范围和检查约定，解析并固定准确 commit。源仓库未提交内容不进入本轮。</p><p class="muted">保存不代表已执行、检查通过或完成独立实验验收。步数预算默认为 12。</p><button disabled>准备副本并启动 · 尚未接通</button></section><div class="notice">请勿填写密钥。项目列表由本机配置提供；项目可选择不代表依赖已经准备好。</div><a href="#demo" class="button secondary">打开独立演示表单</a><p class="hint">演示流程不保存真实任务。</p><p><a class="button secondary" href="#tasks">真实任务记录 →</a></p><a href="#history">浏览演示记录 →</a></div></div><dialog id="real-review" aria-label="真实任务约定核对"><h2>确认并保存任务</h2><div id="real-review-content"></div><p>保存后按准确 commit 固定约定，不包含未提交内容。准确 commit 将在保存成功后展示；修改需求须新建任务。</p><div id="review-error" role="alert" style="white-space:pre-wrap"></div><div class="actions"><button id="save-real">确认保存（不启动）</button><button class="secondary" id="edit-real">返回修改</button></div></dialog>`;
 const form=document.querySelector('#real-task'), select=form.elements.projectId, preview=document.querySelector('#preview-real'), dialog=document.querySelector('#real-review'), save=document.querySelector('#save-real');
 let projects=[], projectRequest=0, payload;
 const active=()=>pageEpoch===epoch;
 for(const name of ['revision','current','expected','scope','targets']) form.elements[name].value=realDraft[name];
 form.elements.allowTests.checked=realDraft.allowTests;
 function remember(){for(const name of ['projectId','revision','current','expected','scope','targets']) realDraft[name]=form.elements[name].value; realDraft.allowTests=form.elements.allowTests.checked;}
 function toggle(){const enabled=form.elements.allowTests.checked; form.elements.targets.disabled=!enabled;form.elements.targets.required=enabled;document.querySelector('#real-targets').hidden=!enabled;document.querySelector('#real-no-check').hidden=enabled;}
 function buttons(){preview.disabled=!projects.length || pendingCreate || uncertainCreate;save.disabled=pendingCreate || uncertainCreate || !projects.length;document.querySelector('#edit-real').disabled=pendingCreate;save.textContent=pendingCreate?'正在保存…':'确认保存（不启动）';}
 function showError(message){document.querySelector('#save-error').hidden=false;document.querySelector('#save-error').textContent=message;document.querySelector('#review-error').textContent=message;}
 if(uncertainCreate)showError('保存结果暂无法确认，勿直接重复提交。后端可能已保存成功；本轮没有历史列表或幂等查询能力。请先由维护者核对记录。');
 if(pendingCreate)showError('上一保存请求仍在等待响应，请勿重复提交。');
 form.addEventListener('input',remember);form.addEventListener('change',remember);form.elements.allowTests.onchange=()=>{toggle();remember();};toggle();buttons();
 async function loadProjects(){const request=++projectRequest;projects=[];select.disabled=true;select.innerHTML='<option value="">正在加载项目…</option>';buttons();const state=document.querySelector('#project-state');state.textContent='正在查询可用项目…';try{const response=await fetch('/api/projects',{cache:'no-store'});const data=await response.json();if(!active()||request!==projectRequest)return;if(!response.ok)throw new Error(errorText(data));if(!Array.isArray(data.projects)||!data.projects.every(p=>typeof p.name==='string'&&/^[a-z0-9_-]{1,64}$/.test(p.projectId)))throw new Error('项目列表响应格式异常。');projects=data.projects.filter(p=>p.canCreateTask!==false);select.innerHTML=projects.length?'<option value="">请选择项目</option>'+projects.map(p=>`<option value="${escape(p.projectId)}">${escape(p.name)}</option>`).join(''):'<option value="">无可用项目</option>';select.disabled=!projects.length;if(projects.some(p=>p.projectId===realDraft.projectId))select.value=realDraft.projectId;state.textContent=projects.length?'已加载本机配置的项目。':'暂无可用项目，请先配置 runs/tasks/projects.json。';}catch(e){if(!active()||request!==projectRequest)return;select.innerHTML='<option value="">项目加载失败</option>';state.textContent=`项目不可用：${e.message || '后端连接失败'}`;}if(active()&&request===projectRequest)buttons();}
 document.querySelector('#reload-projects').onclick=loadProjects;
 form.onsubmit=e=>{e.preventDefault();if(!projects.length||pendingCreate||uncertainCreate)return;remember();if(!projects.some(p=>p.projectId===realDraft.projectId))return;const lines=value=>value.trim().split(/\r?\n/).map(s=>s.trim());payload={projectId:realDraft.projectId,revision:realDraft.revision.trim(),kind:'fix_bug',current:realDraft.current.trim(),expected:realDraft.expected.trim(),allowedPaths:lines(realDraft.scope),allowTests:realDraft.allowTests,checkTargets:realDraft.allowTests?lines(realDraft.targets):[],maxSteps:12};document.querySelector('#real-review-content').innerHTML=contract({project:projects.find(p=>p.projectId===payload.projectId).name,revision:payload.revision,current:payload.current,expected:payload.expected,scope:payload.allowedPaths.join('\n'),allowTests:payload.allowTests,targets:payload.checkTargets.join('\n')});document.querySelector('#review-error').textContent='';dialog.showModal();};
 document.querySelector('#edit-real').onclick=()=>dialog.close();
 dialog.addEventListener('cancel',e=>{if(pendingCreate)e.preventDefault();});
 save.onclick=async()=>{if(pendingCreate||uncertainCreate||!payload)return;pendingCreate=true;buttons();let response;
 try{response=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(response.status!==201){if(response.ok)throw new Error('响应状态异常');pendingCreate=false;if(active()){showError(errorText(data));buttons();}return;}if(!validRecord(data))throw new Error('保存响应无法确认');pendingCreate=false;realDraft={projectId:'',revision:'',current:'',expected:'',scope:'',targets:'',allowTests:true};if(active())location.hash=`/tasks/${data.taskId}`;
 }catch{pendingCreate=false;uncertainCreate=true;if(active()){showError('保存结果暂无法确认，勿直接重复提交。后端可能已保存成功；请先由维护者核对记录。');buttons();}}};
 loadProjects();
}
let preparationPending = new Set();
function preparationPanel(taskRecord) {
 const id=taskRecord.taskId, epoch=pageEpoch, card=document.querySelector('#preparation-card');
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
  const status=r?.status, labels={not_prepared:'尚未准备',preparing:'工作副本准备中',prepared:'工作副本已准备',preparation_failed:'工作副本准备失败'};
  card.innerHTML=`<h2>${labels[status]||'准备状态暂无法确认'}</h2><p role="status" style="white-space:pre-wrap">${escape(message || ({not_prepared:'从已冻结 commit 单独准备副本；不包含源仓库未提交内容。',preparing:'后端报告正在准备工作副本；不是 Agent 执行中。请查询最新状态。',prepared:'副本准备记录已就绪；Agent 与检查状态见执行区域。',preparation_failed:'失败材料保留；本轮没有重试或清理接口。需要新一轮时请新建任务。'}[status]) || '请先查询最新状态，勿直接重复准备。')}</p>${r?`<dl class="kv"><dt>准备编号</dt><dd>${escape(r.preparationId||'未提供')}</dd><dt>副本标识</dt><dd>${escape(r.workspaceId||'未提供')}</dd><dt>固定 commit</dt><dd>${escape(r.baseCommit||taskRecord.contract.baseCommit)}</dd><dt>开始时间</dt><dd>${escape(date(r.startedAt))}</dd><dt>结束时间</dt><dd>${escape(date(r.finishedAt))}</dd></dl>`:''}${r?.error?`<p role="alert">${escape(r.error.message)}（${escape(r.error.code)}）</p>`:''}<p class="hint">worktree 仅隔离文件，不是执行沙箱。副本准备本身不安装依赖或验证 LFS/测试；实际检查见执行区域。准备接口不提供本机路径或日志下载入口。</p><div class="actions"><button id="prepare-workspace" ${status!=='not_prepared'||preparationPending.has(id)?'disabled':''}>${preparationPending.has(id)?'正在等待准备响应…':'准备工作副本'}</button><button class="secondary" id="query-preparation" ${preparationPending.has(id)?'disabled':''}>查询最新状态</button></div>`;
  document.querySelector('#prepare-workspace').onclick=()=>{if(status==='not_prepared'&&!preparationPending.has(id))update(true);};
  document.querySelector('#query-preparation').onclick=()=>update(false);
 }
 async function update(write=false){
  const seq=++request;
  if(write){preparationPending.add(id);draw(null,'正在请求准备工作副本；不会启动 Agent、模型、测试或依赖安装。');}
  else {draw(null,preparationPending.has(id)?'先前准备请求仍在等待响应；正在查询后端状态。':'正在查询工作副本准备状态…');document.querySelector('#query-preparation').disabled=true;}
  try{const response=await fetch(`/api/tasks/${id}/preparation`,write?{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}:{cache:'no-store'});const data=await response.json();if(write)preparationPending.delete(id);if(!active()||seq!==request)return;
   if(!response.ok){draw(null,errorText(data)+'\n准备状态暂无法确认，请查询最新状态；不自动重发准备请求。');return;}
   if(!valid(data)){draw(null,'准备响应格式异常或版本不匹配，准备状态暂无法确认。请先查询，不自动重发。');return;}
   draw(data);
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
 const fields=[['类型',feedback.kind],['状态',feedback.status],['实际反馈',feedback.message],['相对路径',feedback.path],['修改路径',Array.isArray(feedback.paths)?feedback.paths.join('、'):undefined],['返回数量',feedback.returned],['总行数',feedback.totalLines],['已应用',typeof feedback.applied==='boolean'?(feedback.applied?'是':'否'):undefined],['测试通过标记',typeof feedback.passed==='boolean'?(feedback.passed?'是':'否'):undefined],['退出码',feedback.exitCode],['超时',typeof feedback.timedOut==='boolean'?(feedback.timedOut?'是':'否'):undefined]];
 return `<dl class="kv">${fields.filter(([,v])=>v!==undefined&&v!==null).map(([label,value])=>`<dt>${label}</dt><dd>${escape(value)}</dd>`).join('')}</dl>${feedback.kind==='test'||Object.hasOwn(feedback,'counts')?`<p class="hint">${escape(countsText(feedback.counts))}</p>`:''}`;
}

// M3 keeps execution state independent from the saved contract and preparation.
let executionPending = new Set();
let executionCleanup = () => {};
const sourceLabel = source => ({configured_provider:'真实模型 · 本机配置 Provider',injected_test_model:'离线脚本执行 · 注入测试模型'}[source] || '运行来源未知');
const displayTime = value => value && !Number.isNaN(Date.parse(value)) ? new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'long',timeZone:'Asia/Shanghai'}).format(new Date(value)) : '未提供';
function executionPanel(taskRecord) {
 const id=taskRecord.taskId, epoch=pageEpoch, base=`/api/tasks/${id}/execution`;
 const root=document.querySelector('#execution-card');
 root.innerHTML=`<h2>Agent 执行</h2><div id="execution-status" aria-live="polite"></div><div class="actions"><button id="start-agent" disabled>单独启动 Agent</button><button class="secondary" id="query-execution">查询执行状态</button></div><div id="recent-feedback"><h3>最近实际反馈</h3><p class="hint">尚未读取到实际反馈摘要。</p></div><div id="execution-events"><h3>逐步记录</h3><p id="event-message" role="status"></p><div id="event-list"></div><button class="secondary" id="more-events" disabled>读取后续事件</button></div><div id="execution-result"><h3>结果与证据</h3><p id="result-message" role="status">尚未查询到完整结果，不据此推断执行成功或失败。</p><div id="result-content"></div><button class="secondary" id="query-result" disabled>查询结果</button></div><dialog id="start-review" aria-label="启动 Agent 确认"><h2>核对并单独启动</h2><div id="start-contract"></div><div class="notice">此操作会启动 Agent。允许测试时会执行仓库代码，可能有副作用或网络访问；worktree 不是沙箱。模型结束不等于检查通过。</div><div class="actions"><button id="confirm-start">确认启动</button><button class="secondary" id="cancel-start">返回</button></div></dialog>`;
 const $=selector=>document.querySelector(selector);
 let state=null, timer=null, closed=false, querying=false, cursor=0, eventBusy=false, resultBusy=false, resultLoaded=false, artifacts=new Map();
 const active=()=>!closed && pageEpoch===epoch;
 executionCleanup=()=>{closed=true;if(timer!==null)clearTimeout(timer);};
 const text=(sel,value)=>{$(sel).textContent=value;};
 function buttons(){const busy=executionPending.has(id);$('#start-agent').disabled=!state?.canStart||state.status!=='not_started'||busy||querying;$('#query-execution').disabled=querying||busy;$('#query-result').disabled=resultBusy||!state||state.status==='not_started';}
 function validState(r){return r?.schemaVersion===1&&r.taskId===id&&['not_started','running','finalizing','terminated','unknown'].includes(r.status)&&typeof r.canStart==='boolean'&&['configured_provider','injected_test_model'].includes(r.source)&&r.isExecutionSandbox===false&&(r.status==='not_started'||/^[a-f0-9]{32}$/.test(r.executionId||''));}
 function draw(r,message=''){
  state=r;
  const labels={not_started:'尚未启动',running:'执行中',finalizing:'正在整理',terminated:'执行已终止',unknown:'状态暂无法确认'};
  const blocks={PREPARATION_REQUIRED:'需要先准备工作副本',WORKSPACE_UNAVAILABLE:'工作副本不可用',WORKSPACE_DIRTY:'工作副本有未提交或未跟踪内容',EXECUTION_CONFIG_UNAVAILABLE:'本机执行配置不可用',EXECUTION_BUSY:'其他任务正在执行',EXECUTION_UNRESOLVED:'存在未核实的旧执行，需维护者处理',ALREADY_STARTED:'本任务已有执行记录'};
  const phases={agent:'Agent 决策与工具',final_checks:'固定最终检查',delivery:'整理交付'};
  $('#execution-status').innerHTML=`<p><strong>${labels[r?.status]||'状态暂无法确认'}</strong>　<span class="badge">${escape(sourceLabel(r?.source))}</span></p>${message?`<p role="alert" style="white-space:pre-wrap">${escape(message)}</p>`:''}${r?`<dl class="kv"><dt>执行编号</dt><dd>${escape(r.executionId||'未提供')}</dd><dt>阶段</dt><dd>${escape(phases[r.phase]||r.phase||'未提供')}</dd><dt>当前活动</dt><dd>${escape(r.activity||'未提供')}</dd><dt>决策步数</dt><dd>${Number.isInteger(r.steps)?r.steps:'未提供'} / ${Number.isInteger(r.maxSteps)?r.maxSteps:taskRecord.contract.maxSteps}</dd><dt>开始时间</dt><dd>${escape(displayTime(r.startedAt))}</dd><dt>最近记录时间</dt><dd>${escape(displayTime(r.updatedAt))}</dd><dt>结束时间</dt><dd>${escape(displayTime(r.finishedAt))}</dd><dt>存活状态</dt><dd>${escape(r.liveness==='confirmed'?'后端已确认':r.liveness==='stopped'?'后端记录已停止':'暂无法确认')}</dd></dl>${r.startBlockReason?`<p class="hint">启动限制：${escape(blocks[r.startBlockReason]||r.startBlockReason)}</p>`:''}`:''}<p class="hint">记录更新时间不是心跳；不根据停更推断完成或死亡。普通任务执行不属于独立 G/R/P 实验验收。</p>`;
  buttons();
 }
 async function readEvents(){
  if(!active()||eventBusy)return;eventBusy=true;$('#more-events').disabled=true;
  const after=cursor;
  try{const response=await fetch(`${base}/events?after=${after}&limit=50`,{cache:'no-store'});const data=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(data));
   if(data.taskId!==id||!Array.isArray(data.events)||!Number.isInteger(data.nextCursor)||data.nextCursor<after||typeof data.hasMore!=='boolean'||!data.events.every((e,i)=>Number.isInteger(e.seq)&&e.seq>(i?data.events[i-1].seq:after)&&typeof e.type==='string')||(data.events.length&&data.nextCursor!==data.events.at(-1).seq)||(!data.events.length&&data.nextCursor!==after))throw new Error('事件分页响应格式异常，未推进游标。');
   const types={phase:'阶段',decision:'模型决策',observation:'工具反馈',model_attempt:'模型请求',finish:'模型主动结束',limit:'步数上限',error:'错误',terminated:'终止记录'};
   const html=data.events.map(e=>`<article class="event"><div class="meta">#${e.seq} · ${escape(displayTime(e.at))}</div><h3>${escape(types[e.type]||e.type)}</h3><p>${escape([e.phase&&`阶段：${e.phase}`,Number.isInteger(e.step)&&`决策步：${e.step}`,e.action&&`动作：${e.action}`,typeof e.ok==='boolean'&&`工具反馈：${e.ok?'成功':'失败'}`,e.attempt!=null&&`请求尝试：${e.attempt}`,e.outcome&&`请求结果：${e.outcome}`].filter(Boolean).join(' · ')||'后端摘要事件；未提供更多公开内容。')}</p>${e.type==='observation'?feedbackHTML(e.feedback):''}</article>`).join('');
   $('#event-list').insertAdjacentHTML('beforeend',html);const recent=data.events.filter(e=>e.type==='observation').at(-1);if(recent)$('#recent-feedback').innerHTML='<h3>最近实际反馈</h3>'+feedbackHTML(recent.feedback);cursor=data.nextCursor;text('#event-message',data.hasMore?'还有后续事件，点击继续读取。':cursor?'已读取当前可用摘要；不是私有原始 trace。':'暂无事件记录。');$('#more-events').disabled=!data.hasMore;
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
   for(const [artifactId,a] of artifacts)$(`#artifact-load-${a.index}`).onclick=()=>readArtifact(artifactId);
   text('#result-message',`${sourceLabel(r.source)} · 已读取实际结果。`);resultLoaded=true;
  }catch(e){if(active()){text('#result-message',`结果暂不可用：${e.message}。不推断任务成功或失败。${resultLoaded?'下方保留上次读取的结果，尚未重新确认。':''}`);}}
  finally{resultBusy=false;if(active())buttons();}
 }
 async function refresh(){
  if(!active()||querying)return;querying=true;if(timer!==null)clearTimeout(timer);buttons();
  try{const response=await fetch(base,{cache:'no-store'});const r=await response.json();if(!active())return;if(!response.ok)throw new Error(errorText(r));if(!validState(r))throw new Error('执行状态响应格式异常。');draw(r);
   if(r.status!=='not_started'){await readEvents();if(!active())return;if(r.resultAvailable===true&&!resultLoaded)await readResult();else if(!r.resultAvailable&&!resultLoaded)text('#result-message',r.status==='terminated'?'已终止，但未提供完整结果文件。':'尚未提供完整结果；不能根据已有材料推断完成。');}
  }catch(e){if(active())draw(null,`执行状态暂无法确认：${e.message}。先查询，勿重复启动。`);}
  finally{querying=false;if(active()){buttons();if(state&&['running','finalizing'].includes(state.status))timer=setTimeout(refresh,2500);}}
 }
 $('#query-execution').onclick=refresh;$('#more-events').onclick=readEvents;$('#query-result').onclick=readResult;
 $('#start-agent').onclick=()=>{if(querying||!state?.canStart||state.status!=='not_started'||executionPending.has(id))return;$('#start-contract').innerHTML=`<p><strong>${escape(sourceLabel(state.source))}</strong></p><p>任务：${escape(id)}</p><p>固定 commit：${escape(taskRecord.contract.baseCommit)}</p>${contract({project:taskRecord.contract.projectId,revision:taskRecord.contract.requestedRevision,current:taskRecord.contract.current,expected:taskRecord.contract.expected,scope:taskRecord.contract.allowedPaths.join('\n'),allowTests:taskRecord.contract.allowTests,targets:taskRecord.contract.checkTargets.join('\n')})}`;$('#start-review').showModal();};
 $('#cancel-start').onclick=()=>$('#start-review').close();
 $('#confirm-start').onclick=async()=>{
  if(!active()||querying||!state?.canStart||state.status!=='not_started'||executionPending.has(id))return;
  executionPending.add(id);$('#start-review').close();if(timer!==null)clearTimeout(timer);draw(null,'已发送启动请求，正在等待响应。请勿重复启动。');
  try{const response=await fetch(base,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const r=await response.json();executionPending.delete(id);if(!active())return;if(!response.ok){draw(null,errorText(r)+'\n请查询权威状态后再操作，不自动重发。');return;}if(![200,202].includes(response.status)||!validState(r))throw new Error('启动响应无法确认');draw(r);await refresh();}
  catch{executionPending.delete(id);if(active())draw(null,'启动结果暂无法确认，后台可能已开始执行。请先查询状态，勿直接重复启动。');}
 };
 draw(null,'正在查询执行状态…');refresh();
}

async function taskDetail(id){
 const epoch=pageEpoch;
 app.innerHTML=heading('SAVED CONTRACT','正在读取任务','按编号查询已保存约定，不会再次创建任务。');
 function failed(message){app.innerHTML=heading('SAVED CONTRACT','任务读取失败','未将读取错误当作任务不存在。')+`<section class="card"><p role="alert" style="white-space:pre-wrap">${escape(message)}</p><a class="button" href="#task">返回新建任务</a></section>`;}
 if(!/^[a-f0-9]{32}$/.test(id)){failed('任务编号格式错误：需要完整 32 位小写十六进制编号。');return;}
 try{const response=await fetch(`/api/tasks/${id}`,{cache:'no-store'});const data=await response.json();if(epoch!==pageEpoch)return;if(!response.ok){failed(errorText(data));return;}if(!validRecord(data)||data.taskId!==id){failed('任务记录响应损坏或格式不符合约定，无法展示。');return;}const c=data.contract;const created=new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'long',timeZone:'Asia/Shanghai'}).format(new Date(data.createdAt));app.innerHTML=heading('SAVED CONTRACT','任务工作台',data.taskId,'<span class="badge">真实保存 · 普通任务</span>')+`<div class="notice">任务约定已固定。准备、Agent 执行和最终检查分别查询；保存或准备成功不表示修复完成。</div><section class="card"><h2>固定版本与来源</h2><dl class="kv"><dt>任务编号</dt><dd>${escape(data.taskId)}</dd><dt>创建时间</dt><dd>${escape(created)}（北京时间）</dd><dt>准确 commit</dt><dd>${escape(c.baseCommit)}</dd><dt>起始引用</dt><dd>${escape(c.requestedRevision)}</dd><dt>步数上限</dt><dd>${c.maxSteps}</dd></dl><p class="hint">仅包含所选 commit，不包含源仓库未提交内容。保存约定不证明路径可写、检查存在或执行安全。</p></section><section class="card"><h2>已保存的任务约定</h2>${contract({project:c.projectId,revision:c.requestedRevision,current:c.current,expected:c.expected,scope:c.allowedPaths.join('\n'),allowTests:c.allowTests,targets:c.checkTargets.join('\n')})}</section><section class="card" id="preparation-card" aria-label="工作副本准备"></section><section class="card" id="execution-card" aria-label="Agent 执行与结果"></section><div class="actions"><a class="button secondary" href="#task">新建另一项任务</a><a class="button secondary" href="#tasks">返回真实任务记录</a></div><p class="hint">约定已固定；需求变化请新建任务。可收藏当前地址，刷新仅查询这份记录。</p>`; preparationPanel(data); executionPanel(data);
 }catch{if(epoch===pageEpoch)failed('后端不可用或响应无法读取，请确认本地服务正常后刷新。不会自动创建或重发任务。');}
}

async function taskList() {
 const epoch=pageEpoch;
 app.innerHTML=heading('TASK RECORDS','真实任务记录','最近保存的最多 50 项，按创建时间从新到旧。','<a class="button" href="#task">新建任务</a>')+'<section class="card" id="task-list-content" aria-live="polite"><p>正在读取任务记录…</p></section><p class="hint">这里只列出本机保存的任务，不包含虚构演示列表。已保存任务也可能来自离线注入环境；具体运行来源与实际检查见详情，执行终止不表示检查通过。</p><a href="#history">查看独立演示记录 →</a>';
 const content=document.querySelector('#task-list-content');
 try{
  const response=await fetch('/api/tasks',{cache:'no-store'});const data=await response.json();if(pageEpoch!==epoch)return;
  if(!response.ok)throw new Error(errorText(data));
  const statuses={not_started:'尚未执行',running:'执行中',finalizing:'正在整理',terminated:'执行已终止',unknown:'状态暂无法确认'};
  if(!Array.isArray(data.tasks)||data.tasks.length>50||!data.tasks.every(t=>/^[a-f0-9]{32}$/.test(t.taskId)&&typeof t.createdAt==='string'&&!Number.isNaN(Date.parse(t.createdAt))&&typeof t.projectId==='string'&&typeof t.current==='string'&&Object.hasOwn(statuses,t.executionStatus)&&typeof t.resultAvailable==='boolean'))throw new Error('任务列表响应格式异常，不能作为空列表展示。');
  content.innerHTML=data.tasks.length?`<div class="table-scroll"><table class="history task-records"><thead><tr><th>需求摘要 / 任务编号</th><th>项目</th><th>创建时间（北京时间）</th><th>执行状态</th><th>结果</th><th>操作</th></tr></thead><tbody>${data.tasks.map(t=>`<tr><td><div class="task-summary">${escape(t.current.length>160?t.current.slice(0,160)+'…':t.current)}</div><small class="muted">${escape(t.taskId)}</small></td><td>${escape(t.projectId)}</td><td>${escape(displayTime(t.createdAt))}</td><td>${statuses[t.executionStatus]}</td><td>${t.resultAvailable?'有结果记录':'未提供结果'}</td><td><a href="#/tasks/${t.taskId}">查看详情 →</a></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><p>尚无保存的任务。</p><a class="button" href="#task">新建第一个任务</a></div>';
 }catch(e){if(pageEpoch!==epoch)return;content.innerHTML=`<p role="alert" style="white-space:pre-wrap">任务记录暂无法读取：${escape(e.message)}。这不表示没有任务。</p><button class="secondary" id="retry-task-list">重新读取</button>`;document.querySelector('#retry-task-list').onclick=taskList;}
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
async function projectHistory(parts=[]) {
 const epoch=pageEpoch, active=()=>pageEpoch===epoch;
 const shell=(title,body)=>{app.innerHTML=heading('PROJECT HISTORY',escape(title),'真实保存的历史材料 · 只读浏览，不启动模型、测试或验收。','<span class="badge">历史记录</span>')+`<div class="actions"><a class="button secondary" href="#projects">项目历史</a><a class="button secondary" href="#tasks">普通任务记录</a></div><div style="margin-top:20px">${body}</div>`;};
 shell('项目历史','<section class="card"><p role="status">正在读取历史记录…</p></section>');
 const get=async path=>{const response=await fetch(path,{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(errorText(data));return data;};
 const table=(headers,rows)=>`<div class="table-scroll"><table class="history"><thead><tr>${headers.map(h=>`<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
 try{
  if(!parts.length){const data=await get('/api/projects');if(!active())return;if(!Array.isArray(data.projects))throw new Error('项目列表格式异常');shell('项目历史',`<section class="card"><h2>选择项目</h2>${data.projects.length?table(['项目','历史可用性','入口'],data.projects.map(p=>`<tr><td>${escape(p.name)}<br><small>${escape(p.projectId)}</small></td><td>${escape(({available:'历史可读',missing:'历史根目录缺失',not_applicable:'普通任务'}[p.historyAvailability])||historyValue(p.historyAvailability))}</td><td>${p.historyAvailability==='not_applicable'?'<a href="#tasks">普通任务记录 →</a>':`<a href="${historyHref([p.projectId,'tasks'])}">查看需求 →</a>`}</td></tr>`)):'<p class="empty">没有已登记项目。</p>'}</section>`);return;}
  const [projectId,taskSegment,taskKey,roundSegment,roundId,verifySegment,verifyId]=parts;
  if(taskSegment!=='tasks'||(roundSegment&&roundSegment!=='rounds')||(verifySegment&&verifySegment!=='verifications')||parts.length>7)throw new Error('历史页面地址无效');
  const taskBase=`/api/projects/${encodeURIComponent(projectId)}/tasks`,taskPath=taskKey?`${taskBase}/${encodeURIComponent(taskKey)}`:taskBase;
  if(!taskKey){const data=await get(taskBase);if(!active())return;if(!Array.isArray(data.tasks))throw new Error('需求列表格式异常');shell(`${projectId} · 需求`, `<section class="card">${data.tasks.length?table(['需求','模式','轮次','入口'],data.tasks.map(t=>`<tr><td>${escape(t.title||t.taskKey)}<br><small>${escape(t.issueId?'#'+t.issueId:'未记录 Issue')}</small></td><td>${escape(t.mode==='experiment'?'历史实验':t.mode==='ordinary'?'普通任务':historyValue(t.mode))}</td><td>${escape(historyValue(t.roundCount))}</td><td><a href="${historyHref([projectId,'tasks',t.taskKey])}">查看需求与轮次 →</a></td></tr>`)):'<p class="empty">当前项目没有已登记需求；普通任务也可从原任务记录入口查找。</p>'}</section>`);return;}
  if(!roundId){const [task,roundData]=await Promise.all([get(taskPath),get(taskPath+'/rounds')]);if(!active())return;if(!Array.isArray(roundData.rounds))throw new Error('轮次列表格式异常');shell(task.title||taskKey,`<section class="card"><h2>需求身份</h2>${historyFields([['项目',projectId],['需求',taskKey],['模式',task.mode],['Issue',task.issueId],['需求引用',task.requirementRef],['需求版本',task.requirementRevision],['历史可用性',task.historyAvailability]])}<p class="hint">当前需求引用不是历史轮次快照，缺失版本不补造。</p><div id="legacy-history-links"></div></section><section class="card"><h2>多轮记录</h2><p class="hint">prepare 目录不代表从未运行模型；verify-02 是同轮追加验收，不是第二次模型运行。历史运行状态不用于推断当前存活。</p>${roundData.rounds.length?table(['轮次 / 来源','原总报告结论','Agent 停止状态','P 导出','独立验收次数','入口'],roundData.rounds.filter(r=>r.association!=='unlinked'&&r.taskKey!==null).map(r=>`<tr><td>${escape(r.roundId)}<br>${escape(historyValue(r.entry))} / ${escape(historyValue(r.sourceFormat))}<br>${escape(r.createdAt?displayTime(r.createdAt):'时间未记录')}</td><td>${escape(historyValue(r.originalRunConclusion))}</td><td>${escape(historyValue(r.agentStatus))}</td><td>${escape(historyValue(r.exportStatus))}</td><td>${escape(historyValue(r.verificationCount))}</td><td><a href="${historyHref([projectId,'tasks',taskKey,'rounds',r.roundId])}">查看轮次 →</a></td></tr>`)):'<p class="empty">没有可明确关联的轮次，不补造运行。</p>'}</section><section class="card"><h2>未关联材料</h2>${roundData.rounds.filter(r=>r.association==='unlinked'||r.taskKey===null).map(r=>`<p><a href="${historyHref([projectId,'tasks',taskKey,'rounds',r.roundId])}">${escape(r.roundId)} →</a> · ${escape(historyValue(r.sourceFormat))} · 不推断归属</p>`).join('')||'<p class="hint">没有返回未关联材料。</p>'}<p>${escape(Array.isArray(roundData.warnings)?roundData.warnings.join('；'):'')}</p></section>`);return;}
  const roundPath=`${taskPath}/rounds/${encodeURIComponent(roundId)}`;
  const [round,verifyData]=await Promise.all([get(roundPath),get(roundPath+'/verifications')]);if(!active())return;if(!Array.isArray(verifyData.verifications))throw new Error('独立验收列表格式异常');
  if(verifySegment){const v=verifyData.verifications.find(v=>v.verificationId===verifyId);if(!v)throw new Error('未找到该次独立验收记录');shell(`${verifyId} · 独立验收报告`, `<p><a href="${historyHref([projectId,'tasks',taskKey,'rounds',roundId])}">← 返回所属轮次</a></p><section class="card"><h2>这一次独立验收</h2>${historyFields([['轮次',roundId],['验收编号',v.verificationId],['来源格式',v.sourceFormat],['原报告状态',v.rawStatus],['报告判读',verificationVerdict(v)],['原独立验收标志',v.independentlyVerified],['候选 P SHA256',v.candidateSha256],['内容完整性',v.integrityStatus],['时间',v.createdAt?displayTime(v.createdAt):null]])}<p class="notice">仅展示原报告记录，不是本次重新验收；读取哈希不表示本次已核验文件完整性。追加 verify 不等于重新运行模型。</p><h3>原报告中的检查记录</h3>${Array.isArray(v.tests)&&v.tests.length?table(['目标','退出码','超时','计数','符合预期'],v.tests.map(t=>`<tr><td>${escape(historyValue(t.target))}</td><td>${escape(historyValue(t.exitCode))}</td><td>${escape(historyValue(t.timedOut))}</td><td>${escape(historyValue(t.counts))}</td><td>${escape(historyValue(t.matchesExpectation))}</td></tr>`)):'<p class="hint">未记录逐项检查。</p>'}<p>${escape(Array.isArray(v.warnings)?v.warnings.join('；'):'')}</p><div id="history-report-links"></div></section>`);historyMaterials(roundPath,round.artifacts,v.reportRef);return;}
  shell(`${round.association==='unlinked'?'未关联材料':taskKey} · ${roundId}`,`<p><a href="${historyHref([projectId,'tasks',taskKey])}">← 返回需求轮次列表</a></p><section class="card"><h2>本轮身份与证据</h2>${historyFields([['项目',projectId],['需求',round.taskKey],['导航登记',taskKey],['轮次',roundId],['入口类型',round.entry],['模式',round.mode],['归属需求',round.taskKey],['关联状态',round.association],['准备状态',round.preparationStatus],['内容完整性',round.integrityStatus],['来源格式',round.sourceFormat],['原总报告结论',round.originalRunConclusion],['Agent 停止状态',round.agentStatus],['P 导出状态',round.exportStatus],['固定 G',round.baseCommit],['冻结 R SHA256',round.regressionSha256],['候选 P SHA256',round.candidateSha256],['需求快照',round.requirementSnapshotRef],['证据状态',round.evidenceState],['存活评估',round.liveness]])}<p class="notice">${round.association==='unlinked'?'未关联材料：不能声称属于该需求的一轮修复。':''}模型结束、固定检查、补丁导出与独立验收分开审查。历史记录不表示实时执行；缺失字段保持未记录。</p><p style="white-space:pre-wrap">${escape(Array.isArray(round.warnings)?round.warnings.join('\n'):historyValue(round.warnings))}</p></section><section class="card"><h2>分阶段报告</h2><div id="history-report-links"><p class="hint">正在整理后端登记的报告材料。</p></div></section><section class="card"><h2>各次独立验收</h2>${verifyData.verifications.length?table(['验收','来源格式','原报告状态','独立验收标志','完整性','入口'],verifyData.verifications.map(v=>`<tr><td>${escape(v.verificationId)}</td><td>${escape(historyValue(v.sourceFormat))}</td><td>${escape(historyValue(v.rawStatus))}</td><td>${escape(verificationVerdict(v))}<br>原标志：${escape(historyValue(v.independentlyVerified))}</td><td>${escape(historyValue(v.integrityStatus))}</td><td><a href="${historyHref([projectId,'tasks',taskKey,'rounds',roundId,'verifications',v.verificationId])}">查看报告 →</a></td></tr>`)):'<p class="empty">没有独立验收报告，不表示失败或通过。</p>'}</section>`);historyMaterials(roundPath,round.artifacts);
 }catch(e){if(active())shell('历史读取失败',`<section class="card"><p role="alert">${escape(e.message)}。不将读取失败当作空历史，也不补写缺失证据。</p><a href="#projects">返回项目历史</a></section>`);}
}

function render() {
 executionCleanup();
 pageEpoch++;
 const hash=location.hash.slice(1);
 if(hash==='projects'||hash.startsWith('projects/')) { const parts=hash.split('/').slice(1).map(decodeURIComponent);document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash==='#projects'));projectHistory(parts);document.querySelector('#breadcrumb').textContent='工作台 / 项目历史（只读）'; }
 else if(hash.startsWith('/tasks/')) { taskDetail(hash.slice(7)); document.querySelector('#breadcrumb').textContent='工作台 / 已保存约定'; }
 else {
 const [route,variant]=hash.split('/'); const page=route||'task';
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash===`#${page==='demo'?'task':page}`));
 document.querySelector('#breadcrumb').textContent='工作台 / '+({task:'任务约定',tasks:'真实任务记录',demo:'演示表单',execution:'执行记录（演示）',result:'结果证据（演示）',history:'演示记录'}[page]||'任务约定');
 if(page==='tasks')taskList();else if(page==='demo') demoTask(); else if(page==='execution')execution();else if(page==='result')result(variant);else if(page==='history')app.innerHTML=heading('DEMO RECORDS','演示记录','以下仅为虚构演示，不包含真实任务历史。')+history();else task();
 }
 window.scrollTo(0,0);
}
window.addEventListener('hashchange',render);render();
