// Explicit opt-in acceptance against an installed backend. No migration calls.
const {app,BrowserWindow,ipcMain}=require('electron');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const os=require('node:os');const http=require('node:http');
const root=process.env.BREW_PROGRESS_APP_ROOT;
const base=process.env.LIVE_SERVICE_URL;
const output=process.env.LIVE_ARTIFACT_DIR;
if(!root||!base||!output)throw Error('Set BREW_PROGRESS_APP_ROOT, LIVE_SERVICE_URL and LIVE_ARTIFACT_DIR explicitly');
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(base))throw Error('Local backend required');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'mac-live-ui-')));
fs.mkdirSync(output,{recursive:true});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function api(route,body){return new Promise((resolve,reject)=>{
 const data=body===undefined?null:JSON.stringify(body);
 const req=http.request(base+route,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','Content-Length':Buffer.byteLength(data)}:{},timeout:0},res=>{
  let text='';res.setEncoding('utf8');res.on('data',c=>text+=c);res.on('end',()=>{try{const result=JSON.parse(text);if(res.statusCode>=400)reject(Error(result.detail||result.error));else resolve(result);}catch(e){reject(e);}});
 });req.on('error',reject);if(data)req.write(data);req.end();
});}
let ready;const initialized=new Promise(resolve=>{ready=resolve;});
let requests=0,finished=0,lastResponse=null;
for(const [channel,route] of [['state:get','/api/state'],['settings:get','/api/settings'],['updates:history:get','/api/update-history'],['progress:get','/api/progress']])ipcMain.handle(channel,()=>api(route));
ipcMain.handle('app-update:check',()=>{ready();return {ok:true,updateAvailable:false};});
ipcMain.handle('check:run',async()=>{requests++;try{lastResponse=await api('/api/check',{});return lastResponse;}finally{finished++;}});
ipcMain.handle('update:all',async()=>{
 if(process.env.LIVE_ZERO_INSTALL!=='1')throw Error('Real update execution was not opted in');
 const plan=await api('/api/update-plan',{});
 if(plan.items.some(p=>p.ready))throw Error('Acceptance refuses updates: at least one installation would be attempted');
 requests++;try{lastResponse=await api('/api/update-all',{});assert.equal(lastResponse.updated_count,0);assert.equal(lastResponse.attempted_count,0);return lastResponse;}finally{finished++;}
});
const watchdog=setTimeout(()=>{console.error('Live acceptance timed out');app.exit(1);},15*60*1000);
app.whenReady().then(async()=>{
 const win=new BrowserWindow({show:false,width:1200,height:1100,webPreferences:{preload:path.join(root,'electron/preload.js'),contextIsolation:true,backgroundThrottling:false}});
 const js=code=>win.webContents.executeJavaScript(code);
 try{
  const health=await api('/health');assert.equal(health.operation.running,false);
  await win.loadFile(path.join(root,'renderer/index.html'));await initialized;
  await js(`window.confirm=()=>true;window.alert=()=>{};void 0;`);
  const action=process.env.LIVE_ZERO_INSTALL==='1'?'updateAllBtn':'checkNowBtn';
  console.log('Invoking packaged renderer button:',action,'against',base);
  await js(`document.getElementById('${action}').click();`);
  for(let attempt=0;attempt<1700&&!finished;attempt++){await delay(500);if(attempt%20===0){const p=await api('/api/progress');console.log('Live progress:',p.progress.phase,p.progress.current_package||'—');}}
  assert.equal(requests,1);assert.equal(finished,1);assert.ok(lastResponse?.progress,'Missing final captured progress');
  await js(`(async()=>{const until=Date.now()+10000;while(busyLoading&&Date.now()<until)await new Promise(r=>setTimeout(r,20));if(busyLoading)throw Error('Busy state did not finish');})()`);
  const results=[];
  for(const lang of ['he','en']){
   await js(`i18n.setLang('${lang}');refreshStaticText();`);
   const before=await js(`({status:operationStatusBadgeEl.dataset.status,loader:loaderEl.classList.contains('visible'),summary:document.getElementById('operationResultSummary').textContent,offers:snapshot.migrations?.length,classified:snapshot.migrations?.filter(o=>!!o.classification).length})`);
   assert.equal(before.loader,false);assert.ok(['completed','warning'].includes(before.status));
   if(action==='checkNowBtn')assert.equal(before.offers,before.classified);
   await js(`refreshStatusBtn.click();`);await delay(1000);
   assert.equal(await js(`operationStatusBadgeEl.dataset.status`),before.status);
   await js(`(()=>{document.getElementById('operationResultSummary').scrollIntoView({block:'start'});})()`);
   win.showInactive();
   await js(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
   await delay(250);fs.writeFileSync(path.join(output,`result-${lang}.png`),(await win.webContents.capturePage()).toPNG());
   results.push({lang,...before});
  }
  fs.writeFileSync(path.join(output,'response.json'),JSON.stringify(lastResponse,null,2));
  const hash=file=>require('node:crypto').createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const unpacked=root.endsWith('app.asar')?root+'.unpacked':root;
  const code={version:JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8')).version,
    renderer:hash(path.join(root,'renderer/app.js')),backend:hash(path.join(unpacked,'backend/brew-updates-service.py')),
    evidence:hash(path.join(unpacked,'backend/installation_evidence.py'))};
  fs.writeFileSync(path.join(output,'ui-acceptance.json'),JSON.stringify({root,base,action,health,results,code},null,2));
  console.log('Packaged renderer + real backend acceptance: PASS',JSON.stringify(results));
  clearTimeout(watchdog);app.exit(0);
 }catch(e){console.error(e);app.exit(1);}
});
