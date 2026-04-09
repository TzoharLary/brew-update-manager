const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('brewApp', {
  getState: () => ipcRenderer.invoke('state:get'),
  getProgress: () => ipcRenderer.invoke('progress:get'),
  getUpdateHistory: () => ipcRenderer.invoke('updates:history:get'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  updateScheduler: (scheduler) => ipcRenderer.invoke('settings:scheduler:update', { scheduler }),
  updateBrewPath: (brewPath) => ipcRenderer.invoke('settings:brew-path:update', { brew_path: brewPath }),
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
  updateOne: (name, kind) => ipcRenderer.invoke('update:one', { name, kind }),
  updateAll: () => ipcRenderer.invoke('update:all'),
});
