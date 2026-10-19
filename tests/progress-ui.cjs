// Run with node_modules/.bin/electron tests/progress-ui.cjs.
// All IPC responses are fixtures; this never starts the backend or Homebrew.
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = process.env.BREW_PROGRESS_APP_ROOT || path.resolve(__dirname, '..');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'brew-progress-ui-'));
app.setPath('userData', profile);
const watchdog = setTimeout(() => { console.error('UI fixture timed out'); app.exit(1); }, 45000);
let signalReady;
const initialized = new Promise(resolve => { signalReady = resolve; });

for (const [channel, payload] of Object.entries({
  'state:get': { counts: {}, packages: [] },
  'progress:get': { progress: { running: false, phase: 'idle' } },
  'updates:history:get': { items: [] },
  'settings:get': { scheduler: { enabled: false }, brew_path: '/fixture/brew' },
  'app-update:check': { ok: true, updateAvailable: false },
})) ipcMain.handle(channel, () => {
  if (channel === 'app-update:check') signalReady();
  return payload;
});

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    show: false, width: 1100, height: 1100,
    webPreferences: { preload: path.join(root, 'electron/preload.js'), contextIsolation: true,
      backgroundThrottling: false },
  });
  try {
    await win.loadFile(path.join(root, 'renderer/index.html'));
    await initialized;
    if (process.env.BREW_PROGRESS_PREVIEW_DIR) win.showInactive();
    console.log('Fixture renderer loaded');
    for (const lang of ['he', 'en']) {
      const result = await win.webContents.executeJavaScript(`(() => {
        i18n.setLang(${JSON.stringify(lang)});
        refreshStaticText();
        const stamp = new Date(Date.now() - 620000).toISOString();
        const step = new Date(Date.now() - 140000).toISOString();
        const fixture = {
          running: true, operation_name: 'update_selected', phase: 'updating_packages',
          message: 'Updating ada-url', current_package: 'formula:ada-url',
          done: 2, total: 3, percent: 67,
          package_activity: {
            target: 'ada-url', subject: 'cmake', stage: 'building',
            started_at: stamp, stage_started_at: step, last_output_at: step,
            technical_tail: ['==> Installing ada-url dependency: cmake', '==> make', '<unsafe>'],
          },
        };
        renderCheckProgress(fixture);
        const panel = document.getElementById('packageActivity');
        const active = !panel.hidden;
        const title = document.getElementById('packageActivityTitle').textContent;
        const explanation = document.getElementById('packageActivityExplanation').textContent;
        const quiet = document.getElementById('packageActivityQuiet').textContent;
        const collapsed = !panel.querySelector('details').open;
        panel.querySelector('details').open = true;
        const expanded = getComputedStyle(document.getElementById('packageActivityTechnical')).display !== 'none';
        panel.querySelector('details').open = false;
        const escaped = !panel.querySelector('unsafe');
        const before = document.getElementById('packageActivityTitle').getBoundingClientRect();
        const fits = panel.scrollWidth <= panel.clientWidth && before.width > 0;
        renderCheckProgress({ ...fixture, running: false, phase: 'completed' });
        const cleared = panel.hidden;
        renderCheckProgress(fixture);
        const rect = panel.getBoundingClientRect();
        return { active, title, explanation, quiet, collapsed, expanded, escaped, fits, cleared,
          rect: { x: Math.floor(rect.x), y: Math.floor(rect.y),
                  width: Math.ceil(rect.width), height: Math.ceil(rect.height) } };
      })()`);
      for (const key of ['active', 'collapsed', 'expanded', 'escaped', 'fits', 'cleared']) {
        assert.equal(result[key], true, `${lang}: ${key}`);
      }
      assert.match(result.title, /cmake/);
      assert.match(result.explanation, /ada-url/);
      assert.ok(!result.quiet.includes('progress.activity'));
      const policy = await win.webContents.executeJavaScript(`(() => {
        snapshot = { counts: { excluded_total: 1, managed_total: 1 }, packages: [
          { name: 'ngrok', kind: 'cask', excluded: true, outdated: false, installed_version: '3.38.0' },
          { name: 'node', kind: 'formula', provider: 'native', outdated: false, installed_version: '26.12.0' },
          { name: 'llvm', kind: 'formula', outdated: true, update_block_reason: 'source_required' },
        ] };
        activeTab = 'installed'; renderTable();
        const rows = Array.from(document.querySelectorAll('#tbody tr'));
        const ngrok = rows.find(row => row.textContent.includes('ngrok'));
        const llvm = rows.find(row => row.textContent.includes('llvm'));
        const excluded = ngrok && !ngrok.querySelector('button') && !ngrok.querySelector('input');
        const blocked = llvm && !llvm.querySelector('button') && !llvm.querySelector('input');
        const translated = !ngrok.textContent.includes('policy.') && !llvm.textContent.includes('policy.');
        renderCheckProgress({ running: false, operation_name: 'update_all', phase: 'completed',
          deferred_packages: [{ name: 'llvm' }] });
        return { excluded: !!excluded, blocked: !!blocked, translated,
          deferred: !document.getElementById('deferredUpdateSummary').hidden && document.getElementById('deferredUpdateSummary').textContent.includes('llvm') };
      })()`);
      for (const key of ['excluded', 'blocked', 'translated', 'deferred']) assert.equal(policy[key], true, lang + ': ' + key);
      const providers = await win.webContents.executeJavaScript(`(() => {
        snapshot = {counts:{}, providers:{homebrew:{ok:false},macports:{ok:true}},
          migrations:[{name:'terminal-notifier',ports:['terminal-notifier'],executable:true}],
          packages:[{name:'wget',kind:'formula',provider:'homebrew',outdated:true},
                    {name:'wget',kind:'port',provider:'macports',outdated:true}]};
        activeTab='installed'; renderTable();
        const panel=document.getElementById('macManagerPanel');
        const result={rows:document.querySelectorAll('#tbody tr').length,
          independent:packageSelectionKey('wget','port')!==packageSelectionKey('wget','formula'),
          proposal:panel.textContent.includes('terminal-notifier'),
          translated:!panel.textContent.includes('undefined'),
          enabled:isBrewPathConfigured(),located:!!document.querySelector('.wrap #macManagerPanel'),
          collapsible:panel.tagName==='DETAILS'};
        snapshot={counts:{},packages:[]};renderTable();return result;
      })()`);
      const terminal = await win.webContents.executeJavaScript(`(() => {
        const rows = [];
        for (const phase of ['completed','error','canceled']) {
          renderCheckProgress({running:false,operation_name:'check',phase,current_package:null});
          rows.push({phase,current:loaderCurrentValueEl.textContent,eta:loaderEtaValueEl.textContent});
        }
        return rows;
      })()`);
      for (const row of terminal) {
        assert.equal(row.current,'—',lang+': terminal current item');
        if (row.phase !== 'completed') assert.equal(row.eta,'—',lang+': terminal ETA');
      }
      const automatic = await win.webContents.executeJavaScript(`(() => {
        snapshot={packages:[{name:'llvm',kind:'formula',provider:'homebrew',outdated:true,update_block_reason:'source_required',alternative_status:'binary_available'}],
          providers:{macports:{ok:true,catalog:{status:'fresh'}}},
          migrations:[{name:'llvm',ports:['llvm-23'],status:'binary_available',executable:false},
                      {name:'missing',ports:[],status:'mapping_unverified'}]};
        activeTab='outdated';renderTable();
        const panel=document.getElementById('macManagerPanel');
        const labels=Array.from(panel.querySelectorAll('button')).map(b=>b.textContent);
        const result={automatic:panel.textContent.includes(macAlternativeLabel('binary_available')),
          unknown:panel.textContent.includes(macAlternativeLabel('mapping_unverified')),
          noManualChecks:labels.length===0,
          tableStatus:document.getElementById('tbody').textContent.includes(macAlternativeLabel('binary_available'))};
        snapshot={counts:{},packages:[]};renderTable();return result;
      })()`);
      for(const key of ['automatic','unknown','noManualChecks','tableStatus'])assert.equal(automatic[key],true,lang+': '+key);
      assert.equal(providers.rows,2);
      for (const key of ['independent','proposal','translated','enabled','located','collapsible']) assert.equal(providers[key],true,lang+': '+key);
      if (process.env.BREW_PROGRESS_PREVIEW_DIR) {
        fs.mkdirSync(process.env.BREW_PROGRESS_PREVIEW_DIR, { recursive: true });
        const rect = await win.webContents.executeJavaScript(`(async () => {
          const panel = document.getElementById('packageActivity');
          panel.scrollIntoView({ block: 'center' });
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          await new Promise(resolve => setTimeout(resolve, 250));
          const rect = panel.getBoundingClientRect();
          if (panel.hidden || rect.width <= 0 || rect.height <= 0) throw new Error('Activity panel is not visible');
          return { x: Math.max(0, Math.floor(rect.x)), y: Math.max(0, Math.floor(rect.y)),
            width: Math.ceil(rect.width), height: Math.ceil(rect.height) };
        })()`);
        const screenshot = await win.webContents.capturePage(rect);
        fs.writeFileSync(path.join(process.env.BREW_PROGRESS_PREVIEW_DIR, `progress-${lang}.png`), screenshot.toPNG());
      }
      console.log(`${lang}: PASS — ${result.title}`);
    }
    clearTimeout(watchdog);
    app.exit(0);
  } catch (error) {
    console.error(error);
    app.exit(1);
  }
});
