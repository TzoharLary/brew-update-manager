// Button/IPC/finally regression fixture. Never invokes a package manager.
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = process.env.BREW_PROGRESS_APP_ROOT || path.resolve(__dirname,'..');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'mac-results-ui-')));
const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
let ready; const initialized = new Promise(resolve=>{ready=resolve;});
let model={progress:{running:false,phase:'idle'}, response:null, error:null, offline:false, calls:0};
const state={updated_at:new Date().toISOString(),counts:{outdated_total:1},packages:[
  {name:'llvm',kind:'formula',provider:'homebrew',outdated:true,installed_version:'22.1.5',latest_version:'23.1.3_2',update_block_reason:'source_required'}]};
ipcMain.handle('state:get',()=>state);
ipcMain.handle('settings:get',()=>({settings:{brew_path:'/fixture/brew',binary_only:true},scheduler:{enabled:false}}));
ipcMain.handle('updates:history:get',()=>({items:[]}));
ipcMain.handle('app-update:check',()=>{ready();return {ok:true,updateAvailable:false};});
ipcMain.handle('progress:get',()=>{if(model.offline)throw Error('fixture offline');return {progress:model.progress,parallel_check:model.parallel||{running:false}};});
for(const channel of ['check:run','update:all','update:one','update:selected'])ipcMain.handle(channel,async()=>{
  model.calls++;
  await delay(30);
  model.progress=model.response?.progress||model.progress;
  if(model.error)throw Error(model.error);
  return model.response;
});
const watchdog=setTimeout(()=>{console.error('Operation fixture timed out');app.exit(1);},60000);
app.whenReady().then(async()=>{
  const win=new BrowserWindow({show:false,width:1100,height:1100,webPreferences:{preload:path.join(root,'electron/preload.js'),contextIsolation:true,backgroundThrottling:false}});
  const js=code=>win.webContents.executeJavaScript(code);
  try {
    await win.loadFile(path.join(root,'renderer/index.html'));await initialized;
    await js(`window.confirm=()=>true;window.alert=()=>{};void 0;`);
    for(const lang of ['he','en']) {
      await js(`(async()=>{i18n.setLang(${JSON.stringify(lang)});refreshStaticText();await loadState({showLoader:false});})()`);
      const versions=await js(`(()=>{const row=Array.from(tbodyEl.rows).find(r=>r.textContent.includes('llvm'));return Array.from(row.querySelectorAll('bdi')).map(e=>({dir:e.dir,value:e.textContent}));})()`);
      assert.deepEqual(versions,[{dir:'ltr',value:'22.1.5'},{dir:'ltr',value:'23.1.3_2'}],lang+': isolated package revisions');
      for(const scenario of ['check','partial','cancel','error','zero','deferred','mixed']) {
        const phase=scenario==='cancel'?'canceled':scenario==='error'?'error':'completed';
        const update=['zero','deferred','mixed'].includes(scenario);
        const stamp=new Date().toISOString();
        const completed=scenario==='mixed'?[{name:'done',ok:true},{name:'bad',ok:false},{name:'same',ok:true,skipped:true,reason:'not_outdated'}]:[];
        const progress={running:false,operation_name:update?'update_all':'check',phase,started_at:stamp,
          current_package:null,partial:scenario==='partial',error:scenario==='error'?'fixture failure':null,
          completed_packages:completed,failed_packages:completed.filter(x=>!x.ok),remaining_packages:[],
          deferred_packages:scenario==='deferred'?[{name:'llvm',reason:'source_required'}]:[],planned_packages:[],done:0,total:0};
        model={progress:{running:false,phase:'idle'},response:{ok:true,snapshot:state,progress,canceled:phase==='canceled',updated_count:scenario==='mixed'?1:0,failed_count:scenario==='mixed'?1:0,results:[]},calls:0};
        await js(`retainedOperationProgress=null;operationStartedFloor=0;operationStatusUnverified=false;lastKnownOperationRunning=false;setLoading(false);`);
        await js(`document.getElementById('${update?'updateAllBtn':'checkNowBtn'}').click();`);
        for(let attempt=0;attempt<100 && !model.calls;attempt++)await delay(10);
        await js(`(async()=>{const until=Date.now()+4000;while(busyLoading&&Date.now()<until)await new Promise(r=>setTimeout(r,10));if(busyLoading)throw Error('busy did not finish');})()`);
        assert.equal(model.calls,1,lang+': button IPC '+scenario);
        const actual=await js(`({status:operationStatusBadgeEl.dataset.status,loader:loaderEl.classList.contains('visible'),summary:document.getElementById('operationResultSummary').textContent,hidden:document.getElementById('operationResultSummary').hidden,disabled:checkNowBtn.disabled})`);
        const expected=scenario==='cancel'?'canceled':scenario==='error'?'failed':['partial','deferred'].includes(scenario)?'warning':'completed';
        assert.equal(actual.status,expected,lang+': '+scenario);assert.equal(actual.loader,false,lang+': loader '+scenario);
        assert.equal(actual.hidden,false);assert.equal(actual.disabled,false);
        if(scenario==='deferred')assert.ok(actual.summary.includes('llvm'));
        if(scenario==='mixed')assert.match(actual.summary,/1/);
        await js(`document.getElementById('refreshStatusBtn').click();`);await delay(70);
        assert.equal(await js(`operationStatusBadgeEl.dataset.status`),expected,lang+': refresh retained '+scenario);
        await js(`refreshStaticText();`);
        assert.equal(await js(`operationStatusBadgeEl.dataset.status`),expected,lang+': localization retained '+scenario);
      }
      // Polling can arrive late from a prior operation, or regress the same finished run.
      const stale=await js(`(()=>{const stamp=new Date().toISOString();operationStartedFloor=0;
        retainedOperationProgress=null;renderCheckProgress({running:false,phase:'completed',operation_name:'check',started_at:stamp});
        renderCheckProgress({running:true,phase:'preparing',operation_name:'check',started_at:stamp});
        renderCheckProgress({running:true,phase:'preparing',operation_name:'check',started_at:'2000-01-01T00:00:00Z'});
        return operationStatusBadgeEl.dataset.status;})()`);
      assert.equal(stale,'completed',lang+': late progress ignored');
      // A rejected request plus unreachable status remains unverified, then recovers.
      model={progress:{running:false,phase:'idle'},response:null,error:'fixture network failure',offline:false,calls:0};
      await js(`retainedOperationProgress=null;operationStartedFloor=0;operationStatusUnverified=false;lastKnownOperationRunning=false;setLoading(false);`);
      // Initial live-status read succeeds; switch off service when action is received.
      ipcMain.removeHandler('check:run');ipcMain.handle('check:run',async()=>{model.calls++;model.offline=true;throw Error('fixture network failure');});
      await js(`checkNowBtn.click();`);await delay(100);
      assert.equal(await js(`operationStatusBadgeEl.dataset.status`),'unverified',lang+': offline');
      model.offline=false;model.progress={running:false,phase:'completed',operation_name:'check',started_at:new Date().toISOString()};
      await js(`pollCheckProgress();`);await delay(50);
      assert.equal(await js(`operationStatusBadgeEl.dataset.status`),'completed',lang+': reconnect');
      ipcMain.removeHandler('check:run');ipcMain.handle('check:run',async()=>{model.calls++;await delay(30);model.progress=model.response.progress;return model.response;});
      // A background check does not overwrite the foreground update's progress.
      const parallel=await js(`(async()=>{retainedOperationProgress=null;operationStartedFloor=0;
        renderCheckProgress({running:true,phase:'updating_packages',operation_name:'update_all',started_at:new Date().toISOString()});
        await syncParallelCheckState({running:true,started_at:new Date().toISOString()});
        return {name:lastKnownOperationName,status:operationStatusBadgeEl.dataset.status};})()`);
      assert.equal(parallel.name,'update_all');assert.equal(parallel.status,'running');
      const classifications=await js(`(()=>{snapshot={counts:{},packages:[],migrations:[
        {name:'tool',status:'binary_available',executable:false,classification:{category:'standalone_candidate',reasons:['complete_local_evidence_without_shared_roles'],gaps:[],consumers:[]}},
        {name:'service',status:'archive_unknown',executable:false,classification:{category:'service',gaps:['fixture_gap'],consumers:[{name:'consumer'}]}},
        {name:'old',status:'archive_missing',executable:false}]};renderMacManager();const panel=document.getElementById('macManagerPanel');
        return {count:panel.querySelectorAll('.migration-classification').length,text:panel.textContent,buttons:panel.querySelectorAll('button').length};})()`);
      assert.equal(classifications.count,3);assert.equal(classifications.buttons,0);assert.ok(classifications.text.includes('consumer'));assert.ok(classifications.text.includes('fixture_gap'));
      await js(`stopProgressPolling();retainedOperationProgress=null;lastKnownOperationRunning=false;lastKnownParallelCheckRunning=false;setLoading(false);`);
      console.log(lang+': button completion, zero/deferred/mixed, refresh, cancel/error, stale, offline recovery, parallel and classification: PASS');
    }
    clearTimeout(watchdog);app.exit(0);
  } catch(error) {console.error(error);app.exit(1);}
});
