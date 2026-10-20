const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('brewApp', {
  migrationPlan: (name) => ipcRenderer.invoke('migration:plan', {name}),
  migrationApprove: (id) => ipcRenderer.invoke('migration:approve', {id}),
  migrationRecoveryPlan: (transaction_id, action) => ipcRenderer.invoke('migration:recovery-plan', {transaction_id, action}),
  macportsSync: () => ipcRenderer.invoke('macports:sync'),
  getState: () => ipcRenderer.invoke('state:get'),
  getUpdatePlan: () => ipcRenderer.invoke('update:plan:get'),
  getProgress: () => ipcRenderer.invoke('progress:get'),
  getUpdateHistory: () => ipcRenderer.invoke('updates:history:get'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  updateScheduler: (scheduler) => ipcRenderer.invoke('settings:scheduler:update', { scheduler }),
  updateBrewPath: (brewPath) => ipcRenderer.invoke('settings:brew-path:update', { brew_path: brewPath }),
  updatePolicy: (policy) => ipcRenderer.invoke('settings:update-policy', policy),
  autoDetectBrewPath: () => ipcRenderer.invoke('settings:brew-path:auto-detect'),
  checkAppUpdate: () => ipcRenderer.invoke('app-update:check'),
  downloadAndInstallAppUpdate: () => ipcRenderer.invoke('app-update:download-install'),
  onAppUpdateProgress: (handler) => {
    if (typeof handler !== 'function') {
      return () => {};
    }
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on('app-update:progress', listener);
    return () => ipcRenderer.removeListener('app-update:progress', listener);
  },
  runCheckNow: () => ipcRenderer.invoke('check:run'),
  updateOne: (name, kind, decision) => ipcRenderer.invoke('update:one', decision || { name, kind }),
  updateAll: (payload) => ipcRenderer.invoke('update:all', payload || {}),
  updateSelected: (packages) => ipcRenderer.invoke('update:selected', Array.isArray(packages) ? { packages } : packages),
  cancelOperation: () => ipcRenderer.invoke('operation:cancel'),
});
