const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('brewApp', {
  getState: () => ipcRenderer.invoke('state:get'),
  getProgress: () => ipcRenderer.invoke('progress:get'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  updateScheduler: (scheduler) => ipcRenderer.invoke('settings:scheduler:update', { scheduler }),
  updateBrewPath: (brewPath) => ipcRenderer.invoke('settings:brew-path:update', { brew_path: brewPath }),
  autoDetectBrewPath: () => ipcRenderer.invoke('settings:brew-path:auto-detect'),
  checkAppUpdate: () => ipcRenderer.invoke('app-update:check'),
  downloadAndInstallAppUpdate: () => ipcRenderer.invoke('app-update:download-install'),
  runCheckNow: () => ipcRenderer.invoke('check:run'),
  updateOne: (name, kind) => ipcRenderer.invoke('update:one', { name, kind }),
  updateAll: () => ipcRenderer.invoke('update:all'),
});
