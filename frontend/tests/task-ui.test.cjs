// Offline behavior checks with a minimal DOM harness; not browser or HTTP integration.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../app.js'),'utf8');
const block=source.slice(source.indexOf('// M1:'),source.indexOf('\nasync function render()'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function harness(fetch){
 const nodes=new Map();
 const node=()=>{
  const children=new Map();
  return {value:'',checked:false,disabled:false,hidden:false,innerHTML:'',textContent:'',
   insertAdjacentHTML(position,html){this.innerHTML+=html},addEventListener(){},showModal(){this.open=true},close(){this.open=false},
   append(){},before(){},querySelectorAll(){return []},
   querySelector(selector){if(!children.has(selector))children.set(selector,node());return children.get(selector);}};
 };
 const form=node();form.elements=Object.fromEntries(['projectId','revision','current','expected','scope','targets','allowTests','allowTemporaryChecks','kind','workspaceMode'].map(k=>[k,node()]));for(const input of Object.values(form.elements)){input.label={firstChild:{textContent:''},hidden:false};input.closest=()=>input.label;}
 for(const id of ['#simple-task','#simple-review','#simple-review-content','#simple-error','#simple-review-error','#save-simple','#preview-simple','#cancel-simple','#undo-status','#query-undo','#undo-task','#undo-review','#undo-files','#confirm-undo','#cancel-undo','#round-workspace-mode','#round-max-steps','#refresh-project-history','#history-report-links','#history-material-load-0','#history-material-note-0','#history-material-text-0','#task-list-content','#retry-task-list','#real-task','#project-state','#experiment-projects','#preview-real','#real-review','#save-real','#real-targets','#real-no-check','#save-error','#review-error','#edit-real','#reload-projects','#real-review-content','#preparation-card','#prepare-workspace','#query-preparation','#execution-card','#execution-status','#start-agent','#query-execution','#event-message','#event-list','#more-events','#result-message','#result-content','#query-result','#start-review','#start-contract','#confirm-start','#cancel-start','#recent-feedback','#artifact-note-0','#artifact-text-0','#artifact-load-0','#experiment-run-control','#experiment-run-message','#start-experiment-run','#query-experiment-run','#experiment-start-review','#confirm-experiment-start','#cancel-experiment-start'])nodes.set(id,node());nodes.set('#real-task',form);nodes.get('#round-max-steps').value='50';nodes.get('#round-workspace-mode').value='worktree';
 for(const id of ['#purge-review','#purge-summary','#confirm-purge','#cancel-purge','#task-purge-0','#task-purge-1','#task-lifecycle-1','#task-reconcile-0','#task-reconcile-1','#task-lifecycle-0','#task-lifecycle-message','#delete-review','#delete-summary','#confirm-delete','#cancel-delete','#query-execution-diagnostics','#execution-diagnostics','#query-execution-recovery','#execution-recovery-status','#release-execution-owner'])nodes.set(id,node());
 const simple=node();simple.elements=Object.fromEntries(['targetPath','projectId','instruction','kind','workspaceMode','maxSteps','revision','expected','allowedPaths','allowTests','allowTemporaryChecks','checkTargets'].map(k=>[k,node()]));simple.elements.kind.value='add_feature';simple.elements.workspaceMode.value='direct';simple.elements.maxSteps.value='50';nodes.set('#simple-task',simple);
 nodes.set('.card',node());
 const app=node(), location={hash:''};const context=vm.createContext({app,location,fetch,document:{querySelector:s=>nodes.get(s),createElement:node},heading:()=>'',contract:()=>'',escape:s=>String(s),projectMode:()=>false,canAdministerInstance:()=>true,console,Intl,Date,setTimeout:()=>1,clearTimeout:()=>{}});vm.runInContext(block,context);
 for(const id of ['#browse-target','#target-picker','#picker-cancel','#picker-up','#picker-error','#picker-directory','#picker-entries','#picker-path','#picker-note'])nodes.set(id,node());
 return {context,nodes,form,app,location,run:s=>vm.runInContext(s,context)};
}
const ok=data=>({ok:true,status:200,json:async()=>data});
const record={schemaVersion:1,taskId:'a'.repeat(32),recordType:'actual',status:'created',createdAt:'2026-10-31T02:00:00Z',contract:{projectId:'local',requestedRevision:'main',baseCommit:'b'.repeat(40),kind:'fix_bug',current:'old',expected:'new',allowedPaths:['a.py'],allowTests:true,checkTargets:['tests/a.py'],maxSteps:12}};
async function ready(post){const calls=[];const h=harness(async(url,options)=>{calls.push({url,options});return url==='/api/projects'?ok({projects:[{projectId:'local',name:'Local'}]}):post(url,options)});h.calls=calls;h.run('advancedTask()');await tick();Object.assign(h.form.elements.projectId,{value:'local'});for(const [k,v] of Object.entries({revision:'main',current:'old',expected:'new',scope:'a.py\nb.py',targets:'tests/a.py\ntests/b.py'}))h.form.elements[k].value=v;h.form.onsubmit({preventDefault(){}});return h;}
test('task type changes questions and hides edit scope for code reading',async()=>{const h=await ready(async()=>ok(record));for(const [kind,current,expected] of [['read_code','要弄清的问题','希望得到的分析'],['add_feature','新增功能需求','验收条件'],['fix_bug','缺陷表现与复现条件','修复后的预期行为']]){h.form.elements.kind.value=kind;h.form.elements.kind.onchange();assert.equal(h.form.elements.current.label.firstChild.textContent,current);assert.equal(h.form.elements.expected.label.firstChild.textContent,expected);assert.equal(h.form.elements.scope.label.hidden,kind==='read_code');}});
test('ordinary requirement renders rounds and creates a snapshot without starting execution',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});if(o?.method==='POST')return {...ok({...record,contract:{...record.contract,projectId:'local'}}),status:201};if(url.endsWith('/rounds'))return ok({rounds:[{roundId:'20261102-140000_1234abcd',runTaskId:record.taskId,createdAt:record.createdAt,baseCommit:'b'.repeat(40)}]});return ok({mode:'ordinary',title:'Requirement',contract:record.contract})});h.nodes.set('#new-ordinary-round',{disabled:false});h.nodes.set('#ordinary-round-message',{});assert.equal(await h.run("projectHistory(['local','tasks','local-abc'])"),'ordinary');assert.match(h.app.innerHTML,/全部运行轮次/);assert.match(h.app.innerHTML,new RegExp('#/tasks/'+record.taskId));await h.nodes.get('#new-ordinary-round').onclick();assert.equal(calls.filter(c=>c.o?.method==='POST').length,1);assert.ok(calls.every(c=>!c.url.includes('/execution')));assert.equal(h.location.hash,'/tasks/'+record.taskId);});
test('task kinds use the same save flow and reading submits no writable paths',async()=>{for(const kind of ['read_code','add_feature']){let body;const h=await ready(async(url,o)=>{body=JSON.parse(o.body);return {...ok(record),status:201}});h.form.elements.kind.value=kind;h.form.elements.kind.onchange();assert.equal(h.form.elements.scope.disabled,kind==='read_code');h.form.onsubmit({preventDefault(){}});await h.nodes.get('#save-real').onclick();assert.equal(body.kind,kind);assert.deepEqual(body.allowedPaths,kind==='read_code'?[]:['a.py','b.py']);}});
test('empty or unavailable projects disable submission',async()=>{for(const fetch of [async()=>ok({projects:[]}),async()=>{throw Error('offline')}]){const h=harness(fetch);h.run('advancedTask()');await tick();assert.equal(h.nodes.get('#preview-real').disabled,true);}});
test('task page exposes registered experiment projects separately from ordinary creation',async()=>{const h=harness(async()=>ok({projects:[{projectId:'coding-agent',name:'Coding Agent',modes:['ordinary'],canCreateTask:true,canStartExperiment:false},{projectId:'browser-use',name:'browser-use',modes:['experiment'],canCreateTask:false,canStartExperiment:true},{projectId:'pydantic-ai',name:'pydantic-ai',modes:['experiment'],canCreateTask:false,canStartExperiment:true}]}));h.run('advancedTask()');await tick();const html=h.nodes.get('#experiment-projects').innerHTML;assert.match(html,/#projects\/browser-use\/tasks/);assert.match(html,/#projects\/pydantic-ai\/tasks/);assert.match(html,/固定实验需求/);assert.deepEqual(Array.from(h.form.elements.projectId.innerHTML.matchAll(/value="([^"]+)"/g)).map(m=>m[1]),['coding-agent']);});
test('POST sends only contract fields, arrays, boolean; duplicates blocked; success uses full id',async()=>{let resolve;const h=await ready(()=>new Promise(r=>resolve=r));const first=h.nodes.get('#save-real').onclick();await h.nodes.get('#save-real').onclick();assert.equal(h.calls.length,2);const payload=JSON.parse(h.calls[1].options.body);assert.deepEqual(payload.allowedPaths,['a.py','b.py']);assert.deepEqual(payload.checkTargets,['tests/a.py','tests/b.py']);assert.equal(payload.allowTests,true);assert.deepEqual(Object.keys(payload).sort(),['projectId','revision','kind','current','expected','allowedPaths','allowTests','checkTargets','maxSteps','workspaceMode'].sort());resolve({...ok(record),status:201});await first;assert.equal(h.location.hash,'/tasks/'+record.taskId);});
test('field error retains input and permits deliberate correction',async()=>{const h=await ready(async()=>({ok:false,status:400,json:async()=>({error:{code:'INVALID_REVISION',message:'版本无法解析',fields:[{field:'revision',message:'选择有效版本'}]}})}));await h.nodes.get('#save-real').onclick();assert.equal(h.form.elements.revision.value,'main');assert.match(h.nodes.get('#save-error').textContent,/起始版本/);assert.equal(h.nodes.get('#save-real').disabled,false);assert.equal(h.location.hash,'');});
test('POST network loss becomes unknown and is never automatically resent',async()=>{const h=await ready(async()=>{throw Error('offline')});await h.nodes.get('#save-real').onclick();await h.nodes.get('#save-real').onclick();assert.equal(h.calls.length,2);assert.match(h.nodes.get('#save-error').textContent,/保存结果暂无法确认/);assert.equal(h.nodes.get('#save-real').disabled,true);});
test('no-tests sends an empty targets array',async()=>{const h=await ready(async()=>({...ok(record),status:201}));h.form.elements.allowTests.checked=false;h.form.onsubmit({preventDefault(){}});await h.nodes.get('#save-real').onclick();const p=JSON.parse(h.calls[1].options.body);assert.equal(p.allowTests,false);assert.deepEqual(p.checkTargets,[]);});
test('navigation invalidates a delayed POST success without redirecting',async()=>{let resolve;const h=await ready(()=>new Promise(r=>resolve=r));const promise=h.nodes.get('#save-real').onclick();h.run('pageEpoch++');resolve({...ok(record),status:201});await promise;assert.equal(h.location.hash,'');});
test('detail uses only GET; missing and corrupt errors are displayed distinctly',async()=>{for(const code of ['TASK_NOT_FOUND','TASK_RECORD_CORRUPT']){const calls=[];const h=harness(async(url,options)=>{calls.push(options);return {ok:false,status:code==='TASK_NOT_FOUND'?404:500,json:async()=>({error:{code,message:code,fields:[]}})}});await h.run(`taskDetail('${record.taskId}')`);assert.equal(calls.length,1);assert.equal(calls[0].method,undefined);assert.match(h.app.innerHTML,new RegExp(code));}});
test('detail does not accept a mismatched id or render a stale response',async()=>{let resolve;const h=harness(()=>new Promise(r=>resolve=r));const promise=h.run(`taskDetail('${record.taskId}')`);h.run('pageEpoch++');h.app.innerHTML='new page';resolve(ok(record));await promise;assert.equal(h.app.innerHTML,'new page');const wrong=harness(async()=>ok({...record,taskId:'c'.repeat(32)}));await wrong.run(`taskDetail('${record.taskId}')`);assert.match(wrong.app.innerHTML,/损坏/);});

const prep=(status,extra={})=>({schemaVersion:1,taskId:record.taskId,status,...extra});
const prepared=prep('prepared',{preparationId:'d'.repeat(32),workspaceId:'d'.repeat(32),baseCommit:record.contract.baseCommit,isExecutionSandbox:false,startedAt:record.createdAt,finishedAt:record.createdAt});
function panel(h){h.run(`preparationPanel(${JSON.stringify(record)})`);}
test('preparation initially queries; explicit POST uses empty JSON and blocks duplicates',async()=>{let resolve;const calls=[];const h=harness(async(url,options)=>{calls.push({url,options});return options.method==='POST'?new Promise(r=>resolve=r):ok(prep('not_prepared'))});panel(h);await tick();assert.equal(calls.length,1);assert.equal(calls[0].options.method,undefined);h.nodes.get('#prepare-workspace').onclick();h.nodes.get('#prepare-workspace').onclick();assert.equal(calls.length,2);assert.equal(calls[1].options.body,'{}');resolve(ok(prepared));await tick();assert.match(h.nodes.get('#preparation-card').innerHTML,/工作副本已准备/);assert.doesNotMatch(h.nodes.get('#preparation-card').innerHTML,/单独启动 Agent/);});
test('preparation lost POST response is unknown then recovered with GET only',async()=>{const methods=[];let reads=0;const h=harness(async(url,options)=>{methods.push(options.method||'GET');if(options.method==='POST')throw Error('lost');return ok(reads++?prepared:prep('not_prepared'))});panel(h);await tick();h.nodes.get('#prepare-workspace').onclick();await tick();assert.match(h.nodes.get('#preparation-card').innerHTML,/准备结果暂无法确认/);h.nodes.get('#prepare-workspace').onclick();assert.deepEqual(methods,['GET','POST']);await h.nodes.get('#query-preparation').onclick();assert.deepEqual(methods,['GET','POST','GET']);assert.match(h.nodes.get('#preparation-card').innerHTML,/工作副本已准备/);});
test('sparse preparing and interrupted failure render without assuming workspace identity',async()=>{for(const value of [prep('preparing'),prep('preparation_failed',{finishedAt:record.createdAt,error:{code:'PREPARATION_INTERRUPTED',message:'准备中断'}})]){const h=harness(async()=>ok(value));panel(h);await tick();const html=h.nodes.get('#preparation-card').innerHTML;assert.match(html,/未提供/);assert.match(html,/id="prepare-workspace" disabled/);assert.doesNotMatch(html,/undefined/);}});
test('corrupt response, error and commit mismatch never enable preparation',async()=>{for(const result of [ok({...prepared,baseCommit:'e'.repeat(40)}),ok({...prepared,status:'unknown'}),{ok:false,json:async()=>({error:{code:'PREPARATION_RECORD_CORRUPT',message:'准备记录损坏',fields:[]}})}]){const h=harness(async()=>result);panel(h);await tick();assert.match(h.nodes.get('#preparation-card').innerHTML,/准备状态暂无法确认/);assert.match(h.nodes.get('#preparation-card').innerHTML,/id="prepare-workspace" disabled/);}});
test('late preparation response cannot overwrite another task page',async()=>{let resolve;const h=harness(()=>new Promise(r=>resolve=r));panel(h);h.run('pageEpoch++');h.nodes.get('#preparation-card').innerHTML='another task';resolve(ok(prepared));await tick();assert.equal(h.nodes.get('#preparation-card').innerHTML,'another task');});

const executionState={schemaVersion:1,taskId:record.taskId,status:'not_started',canStart:true,startBlockReason:null,source:'injected_test_model',isExecutionSandbox:false};
const running={...executionState,executionId:'f'.repeat(32),status:'running',canStart:false,startBlockReason:'ALREADY_STARTED',steps:1,maxSteps:12,phase:'agent',activity:'model_or_tool',liveness:'confirmed',resultAvailable:false};
const ended={...running,status:'terminated',liveness:'stopped',resultAvailable:true,stopReason:'agent_reported_finished'};
const resultRecord={schemaVersion:1,taskId:record.taskId,executionId:running.executionId,source:'injected_test_model',stopReason:'agent_reported_finished',agent:{status:'agent_reported_finished',summary:'explanation'},finalChecks:{status:'skipped',targets:[],checks:[],reason:'TESTS_NOT_AUTHORIZED'},review:{status:'passed',changedPaths:['a.py'],outOfScopePaths:[],protectedChanges:[]},incomplete:['no independent verification'],artifacts:[{artifactId:'diff',kind:'diff'}],independentlyVerified:false};
const emptyEvents={taskId:record.taskId,events:[],nextCursor:0,hasMore:false};
function execPanel(h){h.run(`executionPanel(${JSON.stringify(record)})`);}

test('preparing a workspace refreshes execution readiness without reloading',async()=>{
 let ready=false;
 const calls=[];
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(url.endsWith('/preparation')){
   if(o.method==='POST')ready=true;
   return ok(ready?prepared:prep('not_prepared'));
  }
  return ok({...executionState,canStart:ready,startBlockReason:ready?null:'PREPARATION_REQUIRED'});
 });
 h.run(`const refreshExecution=executionPanel(${JSON.stringify(record)});preparationPanel(${JSON.stringify(record)},{onPrepared:refreshExecution});`);
 await tick();
 assert.equal(h.nodes.get('#start-agent').disabled,true);
 await h.nodes.get('#prepare-workspace').onclick();await tick();
 assert.match(h.nodes.get('#preparation-card').innerHTML,/工作副本已准备/);
 assert.equal(h.nodes.get('#start-agent').disabled,false);
 assert.equal(calls.filter(c=>c.o.method==='POST').length,1);
 assert.ok(calls.filter(c=>c.url.endsWith('/execution')).length>=2);
});

test('preparation completion rechecks readiness after an older execution query finishes',async()=>{
 let resolveOld,executionReads=0;
 const h=harness(async(url)=>{
  if(url.endsWith('/preparation'))return ok(prepared);
  if(++executionReads===1)return new Promise(resolve=>{resolveOld=resolve});
  return ok(executionState);
 });
 h.run(`const refreshExecution=executionPanel(${JSON.stringify(record)});preparationPanel(${JSON.stringify(record)},{onPrepared:refreshExecution});`);
 await tick();
 assert.equal(executionReads,1);
 resolveOld(ok({...executionState,canStart:false,startBlockReason:'PREPARATION_REQUIRED'}));
 await tick();await tick();
 assert.equal(executionReads,2);
 assert.equal(h.nodes.get('#start-agent').disabled,false);
});

test('prepared workspace still respects execution blockers returned by the backend',async()=>{
 const h=harness(async(url)=>ok(url.endsWith('/preparation')?prepared:{...executionState,canStart:false,startBlockReason:'WORKSPACE_DIRTY'}));
 h.run(`const refreshExecution=executionPanel(${JSON.stringify(record)});preparationPanel(${JSON.stringify(record)},{onPrepared:refreshExecution});`);
 await tick();await tick();
 assert.equal(h.nodes.get('#start-agent').disabled,true);
 assert.match(h.nodes.get('#execution-status').innerHTML,/未提交或未跟踪/);
});
test('unknown historical execution without an execution id remains reviewable',async()=>{const h=harness(async(url)=>ok(url.includes('/events')?{taskId:record.taskId,events:[],nextCursor:0,hasMore:false}:{schemaVersion:1,taskId:record.taskId,status:'unknown',source:'injected_test_model',canStart:false,isExecutionSandbox:false}));execPanel(h);await tick();assert.doesNotMatch(h.nodes.get('#execution-status').innerHTML,/响应格式异常/);assert.match(h.nodes.get('#execution-status').innerHTML,/状态暂无法确认/);assert.equal(h.nodes.get('#start-agent').disabled,true);});
test('recovery only posts after a positive check and keeps the expected old identity',async()=>{const calls=[],owner={taskId:'c'.repeat(32),executionId:'d'.repeat(32)};const h=harness(async(url,o)=>{calls.push({url,o});if(url.endsWith('/recovery'))return ok(o.method==='POST'?{owner,canRelease:false,reason:'RELEASED',released:{releasedAt:'now'}}:{owner,canRelease:true,reason:null,released:null});return ok(executionState)});execPanel(h);await tick();assert.equal(calls.filter(c=>c.o.method==='POST').length,0);await h.nodes.get('#query-execution-recovery').onclick();assert.match(h.nodes.get('#execution-recovery-status').innerHTML,new RegExp(owner.taskId));const first=h.nodes.get('#release-execution-owner').onclick();await h.nodes.get('#release-execution-owner').onclick();await first;const posts=calls.filter(c=>c.o.method==='POST');assert.equal(posts.length,1);assert.equal(posts[0].url,`/api/tasks/${owner.taskId}/execution/recovery`);assert.deepEqual(JSON.parse(posts[0].o.body),{executionId:owner.executionId});assert.match(h.nodes.get('#execution-recovery-status').innerHTML,/已解除/);});
test('unconfirmed recovery cannot send a release request',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});return ok(url.endsWith('/recovery')?{owner:{taskId:record.taskId,executionId:'d'.repeat(32)},canRelease:false,reason:'ACTIVITY_UNCONFIRMED',released:null}:executionState)});execPanel(h);await tick();await h.nodes.get('#query-execution-recovery').onclick();await h.nodes.get('#release-execution-owner').onclick();assert.ok(calls.every(c=>c.o.method!=='POST'));assert.match(h.nodes.get('#execution-recovery-status').innerHTML,/不能解除/);});
test('execution diagnostics are read on demand and absent logs do not imply success',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});return url.endsWith('/diagnostics')?ok({available:false,errors:[],redacted:true}):ok(executionState)});execPanel(h);await tick();assert.equal(calls.filter(c=>c.url.endsWith('/diagnostics')).length,0);await h.nodes.get('#query-execution-diagnostics').onclick();assert.match(h.nodes.get('#execution-diagnostics').textContent,/无法补回旧异常/);assert.ok(calls.every(c=>c.o.method===undefined));});
test('execution GET never starts a model and uses authority rather than prepared',async()=>{let calls=[];const h=harness(async(url,o)=>{calls.push({url,o});return ok({...executionState,canStart:false,startBlockReason:'EXECUTION_CONFIG_UNAVAILABLE'})});execPanel(h);await tick();assert.equal(calls.length,1);assert.equal(calls[0].o.method,undefined);assert.equal(h.nodes.get('#start-agent').disabled,true);assert.match(h.nodes.get('#execution-status').innerHTML,/配置不可用/);});
test('execution explicit confirm sends only empty JSON once; injected source shown',async()=>{let resolve,started=false;const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});if(o.method==='POST'){started=true;return new Promise(r=>resolve=r)}if(url.endsWith('/events?after=0&limit=50'))return ok(emptyEvents);return ok(started?running:executionState)});execPanel(h);await tick();assert.match(h.nodes.get('#execution-status').innerHTML,/离线脚本执行/);h.nodes.get('#start-agent').onclick();const first=h.nodes.get('#confirm-start').onclick();await h.nodes.get('#confirm-start').onclick();assert.equal(calls.filter(c=>c.o.method==='POST').length,1);assert.equal(calls.find(c=>c.o.method==='POST').o.body,'{}');resolve({...ok(running),status:202});await first;assert.equal(h.nodes.get('#start-agent').disabled,true);});
test('lost execution POST response locks start until authoritative GET and never auto resends',async()=>{let posts=0;const h=harness(async(url,o)=>{if(o.method==='POST'){posts++;throw Error('lost')}return ok(posts?{...running,status:'unknown',liveness:'unconfirmed'}:executionState)});execPanel(h);await tick();await h.nodes.get('#confirm-start').onclick();assert.match(h.nodes.get('#execution-status').innerHTML,/启动结果暂无法确认/);assert.equal(h.nodes.get('#start-agent').disabled,true);await h.nodes.get('#confirm-start').onclick();assert.equal(posts,1);await h.nodes.get('#query-execution').onclick();assert.match(h.nodes.get('#execution-status').innerHTML,/暂无法确认/);assert.equal(posts,1);});
test('cursor pages append once and malformed page cannot advance the cursor',async()=>{const urls=[];const h=harness(async(url)=>{urls.push(url);if(!url.includes('/events?'))return ok({...running,status:'unknown'});if(url.includes('after=0'))return ok({taskId:record.taskId,events:[{seq:1,type:'decision',step:1,action:'read_file'}],nextCursor:1,hasMore:true});return ok({taskId:record.taskId,events:[{seq:1,type:'finish'}],nextCursor:1,hasMore:false})});execPanel(h);await tick();await h.nodes.get('#more-events').onclick();await h.nodes.get('#more-events').onclick();assert.equal(urls.filter(u=>u.includes('after=1')).length,2);assert.match(h.nodes.get('#event-message').textContent,/格式异常/);assert.equal((h.nodes.get('#event-list').innerHTML.match(/<article/g)||[]).length,1);});
test('result separates finish, skipped checks and review; material truncation and pages are explicit',async()=>{const h=harness(async(url)=>{if(url.endsWith('/result'))return ok(resultRecord);if(url.includes('/artifacts/'))return ok({artifactId:'diff',kind:'diff',content:'<script>diff',offset:0,nextOffset:12,totalChars:12,hasMore:false,redacted:true,truncated:true});if(url.includes('/events?'))return ok(emptyEvents);return ok(ended)});execPanel(h);await tick();assert.match(h.nodes.get('#result-content').innerHTML,/模型主动结束/);assert.match(h.nodes.get('#result-content').innerHTML,/跳过/);assert.match(h.nodes.get('#result-content').innerHTML,/未做独立/);await h.nodes.get('#artifact-load-0').onclick();assert.equal(h.nodes.get('#artifact-text-0').textContent,'<script>diff');assert.match(h.nodes.get('#artifact-note-0').textContent,/已截断/);});
test('unavailable result and stale execution response never invent success',async()=>{let resolve;const h=harness(()=>new Promise(r=>resolve=r));execPanel(h);h.run('pageEpoch++');h.nodes.get('#execution-status').innerHTML='other task';resolve(ok(running));await tick();assert.equal(h.nodes.get('#execution-status').innerHTML,'other task');const missing=harness(async(url)=>url.includes('/events?')?ok(emptyEvents):ok({...ended,resultAvailable:false}));execPanel(missing);await tick();assert.match(missing.nodes.get('#result-message').textContent,/未提供完整结果/);assert.equal(missing.nodes.get('#result-content').innerHTML,'');});
test('observation renders actual feedback and latest summary without inventing missing fields',async()=>{const h=harness(async(url)=>url.includes('/events?')?ok({taskId:record.taskId,events:[{seq:1,type:'observation',step:1,action:'read_file',ok:true,feedback:{kind:'read',status:'ok',message:'读取 3 行',path:'src/a.py',returned:3,totalLines:10}},{seq:2,type:'observation',step:2,action:'pytest',ok:false,feedback:{kind:'test',status:'failed',message:'收集失败',exitCode:2,timedOut:false,counts:null}}],nextCursor:2,hasMore:false}):ok({...running,status:'unknown'}));execPanel(h);await tick();assert.match(h.nodes.get('#event-list').innerHTML,/读取 3 行/);assert.match(h.nodes.get('#event-list').innerHTML,/src\/a.py/);assert.match(h.nodes.get('#recent-feedback').innerHTML,/收集失败/);assert.match(h.nodes.get('#recent-feedback').innerHTML,/不是 0/);assert.match(h.nodes.get('#recent-feedback').innerHTML,/>2</);assert.doesNotMatch(h.nodes.get('#recent-feedback').innerHTML,/读取 3 行/);});
test('all skipped result preserves skipped counts and exit zero does not mean passed',async()=>{const counts={total:3,passed:0,failed:0,errors:0,skipped:3};const r={...resultRecord,finalChecks:{status:'skipped',counts,targets:['test_a.py'],reason:'ALL_TESTS_SKIPPED',checks:[{target:'test_a.py',status:'skipped',exitCode:0,timedOut:false,counts,reason:'ALL_TESTS_SKIPPED'}]}};const h=harness(async(url)=>url.endsWith('/result')?ok(r):url.includes('/events?')?ok(emptyEvents):ok(ended));execPanel(h);await tick();const html=h.nodes.get('#result-content').innerHTML;assert.match(html,/固定最终检查<strong>跳过/);assert.match(html,/跳过 3/);assert.match(html,/ALL_TESTS_SKIPPED/);assert.doesNotMatch(html,/固定最终检查<strong>通过/);});
test('unknown or contradictory check counts cannot render an unsupported pass',async()=>{for(const counts of [null,{total:2,passed:0,failed:0,errors:0,skipped:2},{total:3,passed:1,failed:0,errors:0,skipped:1}]){const r={...resultRecord,finalChecks:{status:'passed',counts,targets:['test_a.py'],reason:'TEST_COUNTS_UNAVAILABLE',checks:[{target:'test_a.py',status:'passed',exitCode:0,timedOut:false,counts,reason:'TEST_COUNTS_UNAVAILABLE'}]}};const h=harness(async(url)=>url.endsWith('/result')?ok(r):url.includes('/events?')?ok(emptyEvents):ok(ended));execPanel(h);await tick();assert.match(h.nodes.get('#result-content').innerHTML,/无法核验（接口报告通过/);assert.doesNotMatch(h.nodes.get('#result-content').innerHTML,/固定最终检查<strong>通过/);}});
test('mixed passed and skipped counts remain visible and absent feedback stays absent',async()=>{const h=harness(async()=>ok(executionState));assert.match(h.run(`countsText(${JSON.stringify({total:3,passed:2,failed:0,errors:0,skipped:1})})`),/通过 2.*跳过 1/);assert.equal(h.run(`checkLabel(${JSON.stringify({status:'passed',counts:{total:3,passed:2,failed:0,errors:0,skipped:1}})})`),'通过');assert.match(h.run('feedbackHTML(undefined)'),/未提供/);assert.match(h.run('countsText(null)'),/不是 0/);});
test('discovery feedback displays incomplete search scope and cumulative evidence',async()=>{
 const h=harness(async()=>ok(executionState));
 const feedback={kind:'search_code',status:'partial',message:'本次结果覆盖不完整',scope:'supported_text_files',scopePath:'src',returned:0,scanComplete:false,stopReason:'file_limit',scannedFiles:1500,matchedLinesTotal:4,skippedCounts:{too_large:1,read_errors:0},hasMore:true};
 const html=h.run(`feedbackHTML(${JSON.stringify(feedback)})`);
 assert.match(html,/查询范围<\/dt><dd>src/);
 assert.match(html,/扫描完整<\/dt><dd>否/);
 assert.match(html,/停止原因<\/dt><dd>file_limit/);
 assert.match(html,/本页扫描文件<\/dt><dd>1500/);
 assert.match(html,/累计匹配行<\/dt><dd>4/);
 assert.match(html,/too_large=1/);
 assert.match(html,/可继续查询<\/dt><dd>是/);
 assert.match(html,/返回数量<\/dt><dd>0/);
 assert.doesNotMatch(html,/未找到/);
 const legacy=h.run(`feedbackHTML(${JSON.stringify({kind:'search_code',returned:0})})`);
 assert.doesNotMatch(legacy,/扫描完整|累计匹配行|可继续查询/);
});
test('artifact pagination appends text, retains offset after read error, and preserves redaction limits',async()=>{const urls=[];let secondCalls=0;const h=harness(async(url)=>{urls.push(url);if(url.endsWith('/result'))return ok(resultRecord);if(url.includes('/events?'))return ok(emptyEvents);if(url.includes('/artifacts/')){if(url.includes('offset=0&'))return ok({artifactId:'diff',kind:'diff',content:'first',offset:0,nextOffset:5,totalChars:11,hasMore:true,redacted:true,truncated:false});if(secondCalls++===0)return {ok:false,json:async()=>({error:{code:'STORAGE_UNAVAILABLE',message:'材料无法读取',fields:[]}})};return ok({artifactId:'diff',kind:'diff',content:'second',offset:5,nextOffset:11,totalChars:11,hasMore:false,redacted:true,truncated:false})}return ok(ended)});execPanel(h);await tick();await h.nodes.get('#artifact-load-0').onclick();assert.equal(h.nodes.get('#artifact-text-0').textContent,'first');await h.nodes.get('#artifact-load-0').onclick();assert.match(h.nodes.get('#artifact-note-0').textContent,/不视作空输出/);await h.nodes.get('#artifact-load-0').onclick();assert.equal(h.nodes.get('#artifact-text-0').textContent,'firstsecond');assert.equal(urls.filter(u=>u.includes('offset=5&')).length,2);});
test('local patch export evidence never becomes an independent verification claim',async()=>{const r={...resultRecord,patchExport:{status:'not_exported',sha256:null,bytes:null,reason:'KNOWN_SECRET_DETECTED'},review:{...resultRecord.review,status:'failed',outOfScopePaths:['tests/a.py']}};const h=harness(async(url)=>url.endsWith('/result')?ok(r):url.includes('/events?')?ok(emptyEvents):ok(ended));execPanel(h);await tick();const html=h.nodes.get('#result-content').innerHTML;assert.match(html,/KNOWN_SECRET_DETECTED/);assert.match(html,/交付范围审查<strong>失败/);assert.match(html,/不证明可重放或独立验收/);assert.match(html,/未导出/);});

test('real task list reads GET only, renders summary/status and links to existing detail',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'修复空输入',executionStatus:'terminated',resultAvailable:true}]})});await h.run('taskList()');assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/tasks');assert.equal(calls[0].o.method,undefined);const html=h.nodes.get('#task-list-content').innerHTML;assert.match(html,/修复空输入/);assert.match(html,/执行已终止/);assert.match(html,new RegExp('#/tasks/'+record.taskId));assert.match(html,/有结果记录/);assert.doesNotMatch(html,/DEMO-/);});
test('real list distinguishes empty/error and ignores late responses after navigation',async()=>{const empty=harness(async()=>ok({tasks:[]}));await empty.run('taskList()');assert.match(empty.nodes.get('#task-list-content').innerHTML,/新建第一个任务/);const failed=harness(async()=>{throw Error('offline')});await failed.run('taskList()');assert.match(failed.nodes.get('#task-list-content').innerHTML,/这不表示没有任务/);let resolve;const stale=harness(()=>new Promise(r=>resolve=r));const promise=stale.run('taskList()');stale.run('pageEpoch++');stale.nodes.get('#task-list-content').innerHTML='other page';resolve(ok({tasks:[]}));await promise;assert.equal(stale.nodes.get('#task-list-content').innerHTML,'other page');});
test('history navigation is read-only and lists registered projects and rounds without inventing verification',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});if(url==='/api/projects')return ok({projects:[{projectId:'browser-use',name:'browser-use'}]});if(url.endsWith('/rounds'))return ok({rounds:[{roundId:'r1',entry:'prepare',sourceFormat:'round_v1',agentStatus:'step_limit',verificationCount:2}]});return ok({taskKey:'browser-use-5998',title:'#5998',mode:'experiment'})});await h.run('projectHistory()');assert.match(h.app.innerHTML,/#projects\/browser-use\/tasks/);await h.run("projectHistory(['browser-use','tasks','browser-use-5998'])");assert.match(h.app.innerHTML,/step_limit/);assert.match(h.app.innerHTML,/prepare 目录不代表从未运行模型/);assert.match(h.app.innerHTML,/不是第二次模型运行/);assert.ok(calls.every(c=>c.o.method===undefined));assert.doesNotMatch(h.app.innerHTML,/id="confirm-start"/);});
test('history read failure does not become empty or successful evidence',async()=>{const h=harness(async()=>({ok:false,json:async()=>({error:{code:'STORAGE_UNAVAILABLE',message:'历史不可读',fields:[]}})}));await h.run('projectHistory()');assert.match(h.app.innerHTML,/历史不可读/);assert.match(h.app.innerHTML,/不将读取失败当作空历史/);});
test('registered experiment starts only after explicit confirmation and sends empty JSON',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});if(url.endsWith('/runs/status'))return ok({run:null,active:false,canStart:true,liveness:'not_assessed'});if(url.endsWith('/runs'))return {...ok({projectId:'browser-use',taskKey:'browser-use-5998',roundId:'20261102-140000_1234abcd',status:'running'}),status:202};throw Error('unexpected request')});await h.run("experimentStartControl('/api/projects/browser-use/tasks/browser-use-5998','browser-use','browser-use-5998')");assert.equal(calls.length,1);h.nodes.get('#start-experiment-run').onclick();assert.equal(calls.length,1);const first=h.nodes.get('#confirm-experiment-start').onclick();await h.nodes.get('#confirm-experiment-start').onclick();assert.equal(calls.filter(c=>c.o?.method==='POST').length,1);assert.equal(calls.find(c=>c.o?.method==='POST').o.body,'{}');await first;assert.equal(h.location.hash,'#projects/browser-use/tasks/browser-use-5998/rounds/20261102-140000_1234abcd');});
test('experiment status query can recover after a failed read',async()=>{let reads=0;const h=harness(async()=>{if(++reads===1)throw Error('offline');return ok({run:null,active:false,canStart:true,liveness:'not_assessed'})});await h.run("experimentStartControl('/fixture','browser-use','browser-use-5998')");assert.match(h.nodes.get('#experiment-run-control').innerHTML,/实验状态暂无法确认/);assert.doesNotMatch(h.nodes.get('#experiment-run-control').innerHTML,/id="query-experiment-run" disabled/);await h.nodes.get('#query-experiment-run').onclick();assert.equal(reads,2);assert.doesNotMatch(h.nodes.get('#experiment-run-control').innerHTML,/id="start-experiment-run" disabled/);});
test('uncertain experiment start requires a status read before another confirmation',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});if(o?.method==='POST')throw Error('connection lost');return ok({run:null,active:false,canStart:true,liveness:'not_assessed'})});await h.run("experimentStartControl('/fixture','browser-use','browser-use-5998')");await h.nodes.get('#confirm-experiment-start').onclick();await h.nodes.get('#confirm-experiment-start').onclick();assert.equal(calls.filter(c=>c.o?.method==='POST').length,1);assert.match(h.nodes.get('#experiment-run-control').innerHTML,/id="start-experiment-run" disabled/);await h.nodes.get('#query-experiment-run').onclick();assert.equal(calls.filter(c=>c.o?.method==='POST').length,1);});

test('history registered materials load by ID and old verification cannot be upgraded',async()=>{const calls=[];const h=harness(async(url,o)=>{calls.push({url,o});return ok({artifactId:'verify-02-report',kind:'report',content:'historical report',offset:0,nextOffset:17,totalChars:17,hasMore:false,redacted:true,truncated:true})});h.run("historyMaterials('/api/projects/p/tasks/t/rounds/r',[{artifactId:'verify-02-report',kind:'report',label:'第二次验收',available:true}],'verify-02-report')");await h.nodes.get('#history-material-load-0').onclick();assert.match(calls[0].url,/artifacts\/verify-02-report\?offset=0/);assert.equal(calls[0].o.method,undefined);assert.equal(h.nodes.get('#history-material-text-0').textContent,'historical report');assert.match(h.nodes.get('#history-material-note-0').textContent,/已截断/);assert.match(h.run("verificationVerdict({sourceFormat:'legacy_verification',rawStatus:'independently_verified',independentlyVerified:true})"),/不升级/);});


test('queued execution disables resubmission and stays queryable',async()=>{
 const queued={...executionState,executionId:'c'.repeat(32),status:'queued',canStart:false,startBlockReason:'ALREADY_STARTED',queuedAt:record.createdAt};
 const calls=[];
 const h=harness(async(url,o)=>{calls.push({url,o});return ok(queued)});
 execPanel(h);await tick();
 assert.match(h.nodes.get('#execution-status').innerHTML,/已入队，等待 Worker/);
 assert.equal(h.nodes.get('#start-agent').disabled,true);
 assert.equal(h.nodes.get('#query-result').disabled,true);
 assert.equal(calls.filter(c=>c.o?.method==='POST').length,0);
});

test('worker interruption keeps unknown status and never offers automatic retry',async()=>{
 const h=harness(async(url)=>url.endsWith('/events')?ok({events:[],nextCursor:0,hasMore:false}):ok({...running,status:'unknown',stopReason:'WORKER_INTERRUPTED',liveness:'unconfirmed'}));
 execPanel(h);await tick();
 assert.match(h.nodes.get('#execution-status').innerHTML,/保留修改和日志待核实/);
 assert.equal(h.nodes.get('#start-agent').disabled,true);
});


test('project history shows newest maxSteps requirement first and reloads new records',async()=>{
 const old={taskKey:'coding-agent-old',title:'Old requirement',mode:'ordinary',roundCount:1,createdAt:'2026-11-02T01:00:00Z'};
 const latest={taskKey:'coding-agent-maxsteps',title:'Modify maxSteps in frontend',mode:'ordinary',roundCount:1,createdAt:'2026-11-09T02:56:38Z'};
 let reads=0;
 const h=harness(async(url,o)=>{assert.equal(url,'/api/projects/coding-agent/tasks');assert.equal(o.cache,'no-store');return ok({tasks:++reads===1?[old,latest]:[old,latest,{...latest,taskKey:'coding-agent-new',title:'New requirement',createdAt:'2026-11-10T00:00:00Z'}]})});
 await h.run("projectHistory(['coding-agent','tasks'])");
 assert.ok(h.app.innerHTML.indexOf(latest.title)<h.app.innerHTML.indexOf(old.title));
 assert.match(h.app.innerHTML,/coding-agent-maxsteps/);
 assert.match(h.app.innerHTML,/创建时间/);
 await h.nodes.get('#refresh-project-history').onclick();
 assert.equal(reads,2);
 assert.ok(h.app.innerHTML.indexOf('New requirement')<h.app.innerHTML.indexOf(latest.title));
});


test('new round sends its selected budget instead of inheriting the old requirement budget',async()=>{
 let body;
 const h=harness(async(url,o)=>{body=JSON.parse(o.body);return {...ok({...record,contract:{...record.contract,maxSteps:50}}),status:201}});
 h.nodes.set('#new-ordinary-round',{disabled:false});h.nodes.set('#ordinary-round-message',{});
 h.run("ordinaryRoundControl('/api/projects/local/tasks/old','local','old')");
 await h.nodes.get('#new-ordinary-round').onclick();
 assert.deepEqual(body,{maxSteps:50,workspaceMode:'worktree'});
 assert.equal(h.location.hash,'/tasks/'+record.taskId);
});


test('direct execution mode is saved explicitly in the task contract',async()=>{
 const h=await ready(async()=>({...ok(record),status:201}));
 h.form.elements.workspaceMode.value='direct';
 h.form.onsubmit({preventDefault(){}});
 assert.match(h.nodes.get('#real-review-content').innerHTML,/直接修改当前仓库/);
 await h.nodes.get('#save-real').onclick();
 assert.equal(JSON.parse(h.calls.at(-1).options.body).workspaceMode,'direct');
});


test('direct tasks can submit without preparation requests or a preparation button',async()=>{
 const calls=[];
 const h=harness(async(url,o)=>{calls.push({url,o});return ok(executionState)});
 const direct={...record,contract:{...record.contract,workspaceMode:'direct'}};
 h.run(`executionPanel(${JSON.stringify(direct)});preparationPanel(${JSON.stringify(direct)});`);
 await tick();
 assert.match(h.nodes.get('#preparation-card').innerHTML,/无需创建或准备工作副本/);
 assert.doesNotMatch(h.nodes.get('#preparation-card').innerHTML,/id="prepare-workspace"/);
 assert.ok(calls.every(c=>!c.url.includes('/preparation')));
 assert.equal(h.nodes.get('#start-agent').disabled,false);
});


test('direct undo checks readiness, requires confirmation and preserves execution identity',async()=>{
 const calls=[];
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(url.endsWith('/undo'))return ok({taskId:record.taskId,canUndo:o.method!=='POST',status:o.method==='POST'?'undone':'ready',conflicts:[],files:['a.py']});
  if(url.includes('/events?'))return ok(emptyEvents);
  return ok({...ended,resultAvailable:false});
 });
 const direct={...record,contract:{...record.contract,workspaceMode:'direct'}};
 h.run(`executionPanel(${JSON.stringify(direct)})`);await tick();
 await h.nodes.get('#query-undo').onclick();
 assert.equal(h.nodes.get('#undo-task').disabled,false);
 h.nodes.get('#undo-task').onclick();
 assert.equal(calls.filter(c=>c.o.method==='POST').length,0);
 await h.nodes.get('#confirm-undo').onclick();
 const post=calls.find(c=>c.o.method==='POST');
 assert.deepEqual(JSON.parse(post.o.body),{executionId:running.executionId});
 assert.match(h.nodes.get('#undo-status').textContent,/已撤回/);
 assert.equal(h.nodes.get('#undo-task').disabled,true);
});


test('simple task accepts an unregistered file with only a description and path',async()=>{
 const calls=[];
 const saved={...record,contract:{...record.contract,sourceKind:'directory',workspaceMode:'direct',baseCommit:null,maxSteps:50,targetFile:'plain.py',allowedPaths:['plain.py']}};
 const h=harness(async(url,o)=>{calls.push({url,o});return url==='/api/projects'?ok({projects:[]}):{...ok(saved),status:201}});
 h.run('task()');await tick();
 const form=h.nodes.get('#simple-task');
 form.elements.targetPath.value='E:/unregistered/plain.py';form.elements.instruction.value='Fix this function';
 form.onsubmit({preventDefault(){}});
 assert.equal(h.nodes.get('#simple-review').open,true);
 await h.nodes.get('#save-simple').onclick();
 const payload=JSON.parse(calls.find(c=>c.o?.method==='POST').o.body);
 assert.equal(payload.targetPath,'E:/unregistered/plain.py');
 assert.equal(payload.instruction,'Fix this function');
 assert.equal(payload.workspaceMode,'direct');assert.equal(payload.maxSteps,50);
 assert.equal(payload.allowTests,false);assert.deepEqual(payload.checkTargets,[]);
 assert.equal(payload.revision,undefined);assert.equal(payload.allowedPaths,undefined);
 assert.equal(h.location.hash,'/tasks/'+record.taskId);
});

test('simple task keeps custom targets usable when project loading fails',async()=>{
 const h=harness(async()=>{throw Error('offline')});h.run('task()');await tick();
 assert.match(h.nodes.get('#simple-error').textContent,/仍可填写本机路径/);
 assert.equal(h.nodes.get('#preview-simple').disabled,false);
});


test('deletion requires confirmation, sends one request, and removes only selected round',async()=>{
 const calls=[];
 let deleted=false;
 const task={taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'删除该轮',executionStatus:'terminated',resultAvailable:true};
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(o?.method==='POST'){deleted=true;return ok({taskId:record.taskId,deleted:true});}
  return ok({tasks:deleted?[]:[task]});
 });
 await h.run('taskList()');
 h.nodes.get('#task-lifecycle-0').onclick();
 assert.equal(h.nodes.get('#delete-review').open,true);
 assert.equal(calls.length,1);
 const first=h.nodes.get('#confirm-delete').onclick();
 await h.nodes.get('#confirm-delete').onclick();
 await first;
 assert.equal(calls.filter(c=>c.o.method==='POST').length,1);
 assert.deepEqual(JSON.parse(calls.find(c=>c.o.method==='POST').o.body),{deleted:true});
 assert.match(h.nodes.get('#task-list-content').innerHTML,/尚无保存的任务/);
});

test('trash restoration uses an explicit action without submitting execution',async()=>{
 const calls=[];
 let restored=false;
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(o?.method==='POST'){restored=true;return ok({taskId:record.taskId,deleted:false});}
  return ok({tasks:restored?[]:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Restore',executionStatus:'not_started',resultAvailable:false}]});
 });
 await h.run('taskList(true)');
 assert.equal(calls[0].url,'/api/trash');
 h.nodes.get('#task-lifecycle-0').onclick();await tick();
 assert.deepEqual(JSON.parse(calls.find(c=>c.o.method==='POST').o.body),{deleted:false});
 assert.ok(calls.every(c=>!c.url.includes('/execution')));
 assert.match(h.nodes.get('#task-list-content').innerHTML,/回收站为空/);
});

test('lost delete response is not automatically retried',async()=>{
 let posts=0;
 const h=harness(async(url,o)=>{
  if(o?.method==='POST'){posts++;throw Error('lost response');}
  return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Delete',executionStatus:'terminated',resultAvailable:true}]});
 });
 await h.run('taskList()');h.nodes.get('#task-lifecycle-0').onclick();
 await h.nodes.get('#confirm-delete').onclick();
 await h.nodes.get('#confirm-delete').onclick();
 assert.equal(posts,1);
 assert.match(h.nodes.get('#task-lifecycle-message').textContent,/待核实/);
});

test('completion outcome is visible separately from model finish and checks',async()=>{
 const h=harness(async(url)=>url.includes('/events?')?ok(emptyEvents):url.endsWith('/result')?ok({...resultRecord,completion:{status:'incomplete',evidence:'Control is still hardcoded',modelReported:true}}):ok(ended));
 execPanel(h);await tick();await tick();
 assert.match(h.nodes.get('#result-content').innerHTML,/需求完成状态/);
 assert.match(h.nodes.get('#result-content').innerHTML,/未完成/);
 assert.match(h.nodes.get('#result-content').innerHTML,/Control is still hardcoded/);
});

test('completed model report keeps needs_review and the remaining behavioral gaps visible',async()=>{
 const result={...resultRecord,completion:{status:'completed',modelReported:true},execution_status:'agent_reported_finished',acceptance_status:'needs_review',remaining_gaps:[{scenario:'Independent branches',verification_goal:'Preserve each branch order'}]};
 const h=harness(async(url)=>url.includes('/events?')?ok(emptyEvents):url.endsWith('/result')?ok(result):ok(ended));
 execPanel(h);await tick();await tick();
 const html=h.nodes.get('#result-content').innerHTML;
 assert.match(html,/待审查/);assert.match(html,/模型报告已完成/);assert.match(html,/Independent branches/);assert.match(html,/Preserve each branch order/);
});


test('optimistic delete removes the row before the server replies and avoids list refetch',async()=>{
 let resolve;
 const calls=[];
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(o?.method==='POST')return new Promise(r=>resolve=r);
  return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Immediate removal',executionStatus:'terminated',resultAvailable:true}]});
 });
 await h.run('taskList()');h.nodes.get('#task-lifecycle-0').onclick();
 const saving=h.nodes.get('#confirm-delete').onclick();
 assert.equal(h.nodes.get('#delete-review').open,false);
 assert.doesNotMatch(h.nodes.get('#task-list-content').innerHTML,/Immediate removal/);
 assert.match(h.nodes.get('#task-list-content').innerHTML,/正在提交操作/);
 resolve(ok({taskId:record.taskId,deleted:true}));await saving;
 assert.equal(calls.filter(c=>c.url==='/api/tasks').length,1);
 assert.equal(calls.length,2);
 assert.match(h.nodes.get('#task-list-content').innerHTML,/尚无保存的任务/);
});

test('one rejected delete restores only its row while another deletion remains pending',async()=>{
 const decisions=new Map();
 const secondId='c'.repeat(32);
 const task=id=>({taskId:id,createdAt:record.createdAt,projectId:'local',current:id===record.taskId?'First row':'Second row',executionStatus:'terminated',resultAvailable:true});
 const h=harness(async(url,o)=>{
  if(o?.method==='POST')return new Promise(resolve=>decisions.set(url,resolve));
  return ok({tasks:[task(record.taskId),task(secondId)]});
 });
 await h.run('taskList()');
 h.nodes.get('#task-lifecycle-0').onclick();const first=h.nodes.get('#confirm-delete').onclick();
 h.nodes.get('#task-lifecycle-1').onclick();const second=h.nodes.get('#confirm-delete').onclick();
 decisions.get(`/api/tasks/${record.taskId}/lifecycle`)({ok:false,status:409,json:async()=>({error:{code:'TASK_BUSY',message:'Worker claimed this task'}})});
 await first;
 assert.match(h.nodes.get('#task-list-content').innerHTML,/First row/);
 assert.doesNotMatch(h.nodes.get('#task-list-content').innerHTML,/Second row/);
 assert.match(h.nodes.get('#task-lifecycle-message').textContent,/已恢复该行/);
 decisions.get(`/api/tasks/${secondId}/lifecycle`)(ok({taskId:secondId,deleted:true}));await second;
 assert.match(h.nodes.get('#task-list-content').innerHTML,/First row/);
 assert.doesNotMatch(h.nodes.get('#task-list-content').innerHTML,/Second row/);
});

test('lost mutation response reconciles only that task and confirms a committed deletion',async()=>{
 const calls=[];
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(o?.method==='POST')throw Error('response lost');
  if(url.endsWith('/lifecycle'))return ok({taskId:record.taskId,deleted:true});
  return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Committed task',executionStatus:'terminated',resultAvailable:true}]});
 });
 await h.run('taskList()');h.nodes.get('#task-lifecycle-0').onclick();
 await h.nodes.get('#confirm-delete').onclick();
 assert.deepEqual(calls.map(c=>c.url),['/api/tasks',`/api/tasks/${record.taskId}/lifecycle`,`/api/tasks/${record.taskId}/lifecycle`]);
 assert.equal(calls.filter(c=>c.o?.method==='POST').length,1);
 assert.doesNotMatch(h.nodes.get('#task-list-content').innerHTML,/Committed task/);
 assert.match(h.nodes.get('#task-lifecycle-message').textContent,/已确认移入回收站/);
});

test('unconfirmed deletion can be checked again without resending the mutation',async()=>{
 let posts=0,checks=0;
 const h=harness(async(url,o)=>{
  if(o?.method==='POST'){posts++;throw Error('connection interrupted');}
  if(url.endsWith('/lifecycle'))return ok({taskId:record.taskId,deleted:++checks>1});
  return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Pending task',executionStatus:'terminated',resultAvailable:true}]});
 });
 await h.run('taskList()');h.nodes.get('#task-lifecycle-0').onclick();
 await h.nodes.get('#confirm-delete').onclick();
 assert.match(h.nodes.get('#task-list-content').innerHTML,/操作待核实/);
 await h.nodes.get('#task-reconcile-0').onclick();
 assert.equal(posts,1);
 assert.equal(checks,2);
 assert.doesNotMatch(h.nodes.get('#task-list-content').innerHTML,/Pending task/);
});

test('late delete completion cannot overwrite another page',async()=>{
 let resolve;
 const h=harness(async(url,o)=>o?.method==='POST'?new Promise(r=>resolve=r):ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Delete',executionStatus:'terminated',resultAvailable:true}]}));
 await h.run('taskList()');h.nodes.get('#task-lifecycle-0').onclick();
 const saving=h.nodes.get('#confirm-delete').onclick();
 h.run('pageEpoch++');h.nodes.get('#task-list-content').innerHTML='Other page';
 resolve(ok({taskId:record.taskId,deleted:true}));await saving;
 assert.equal(h.nodes.get('#task-list-content').innerHTML,'Other page');
});


test('permanent deletion requires a second confirmation and removes the row optimistically',async()=>{
 let resolve;
 const calls=[];
 const h=harness(async(url,o)=>{
  calls.push({url,o});
  if(o?.method==='POST')return new Promise(r=>resolve=r);
  return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Permanent task',executionStatus:'terminated',resultAvailable:true}]});
 });
 await h.run('taskList(true)');
 h.nodes.get('#task-purge-0').onclick();
 assert.equal(h.nodes.get('#purge-review').open,true);
 assert.equal(calls.length,1);
 const deleting=h.nodes.get('#confirm-purge').onclick();
 assert.equal(h.nodes.get('#purge-review').open,false);
 assert.doesNotMatch(h.nodes.get('#task-list-content').innerHTML,/Permanent task/);
 assert.equal(calls[1].url,`/api/tasks/${record.taskId}/purge`);
 assert.deepEqual(JSON.parse(calls[1].o.body),{confirmTaskId:record.taskId});
 resolve(ok({taskId:record.taskId,purged:true,status:'purged'}));await deleting;
 assert.equal(calls.length,2);
 assert.match(h.nodes.get('#task-lifecycle-message').textContent,/已彻底删除/);
});

test('saved progress shows model claims, stale checks, blocking and decision budget',()=>{
 const h=harness(async()=>ok({}));
 const progress={revision:2,current_step_id:null,steps:[
  {id:'check',goal:'Verify parser',status:'completed',evidence_refs:['obs_old'],blocked_reason:''},
  {id:'delivery',goal:'Deliver caller',status:'blocked',evidence_refs:[],blocked_reason:'Caller is unresolved'}
 ],evidence:[{id:'obs_old',action:'run_tests',passed:true,ok:true}],stale_evidence_refs:['obs_old']};
 const budget={used_decisions:9,max_decisions:20,remaining_decisions:11,remaining_includes_current:true};
 const html=h.run(`progressHTML(${JSON.stringify(progress)},${JSON.stringify(budget)})`);
 assert.match(html,/模型声明完成/);
 assert.match(html,/检查通过/);
 assert.match(html,/后续编辑过期/);
 assert.match(html,/Caller is unresolved/);
 assert.match(html,/剩余 11（包含本次决策）/);
 assert.match(h.run('progressHTML(undefined,undefined)'),/步骤记录未提供/);
});

test('partial permanent deletion offers a retry and keeps restoration disabled',async()=>{
 let posts=0;
 const h=harness(async(url,o)=>{
  if(o?.method==='POST'){
   posts++;
   return posts===1?{ok:false,status:503,json:async()=>({error:{code:'PURGE_INCOMPLETE',message:'busy file'}})}:ok({taskId:record.taskId,purged:true,status:'purged'});
  }
  if(url.endsWith('/purge'))return ok({taskId:record.taskId,purged:false,status:'purging'});
  return ok({tasks:[{taskId:record.taskId,createdAt:record.createdAt,projectId:'local',current:'Pending cleanup',executionStatus:'terminated',resultAvailable:true}]});
 });
 await h.run('taskList(true)');h.nodes.get('#task-purge-0').onclick();
 await h.nodes.get('#confirm-purge').onclick();
 assert.match(h.nodes.get('#task-list-content').innerHTML,/重试清理/);
 assert.match(h.nodes.get('#task-list-content').innerHTML,/id="task-lifecycle-0" disabled/);
 h.nodes.get('#task-purge-0').onclick();await h.nodes.get('#confirm-purge').onclick();
 assert.equal(posts,2);
 assert.match(h.nodes.get('#task-list-content').innerHTML,/回收站为空/);
});
