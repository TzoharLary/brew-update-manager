const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const net = require('node:net');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { createAppUpdater } = require('./app-updater');

const DEV_APP_ROOT = path.join(__dirname, '..');
const PACKAGED_ASAR_ROOT = path.join(process.resourcesPath, 'app.asar');
const PACKAGED_UNPACKED_ROOT = path.join(process.resourcesPath, 'app.asar.unpacked');

function appRoot({ unpacked = false } = {}) {
  if (!app.isPackaged) {
    return DEV_APP_ROOT;
  }

  return unpacked ? PACKAGED_UNPACKED_ROOT : PACKAGED_ASAR_ROOT;
}

function resolveServiceScriptCandidates() {
  return [
    path.join(appRoot({ unpacked: true }), 'backend', 'brew-updates-service.py'),
    path.join(appRoot({ unpacked: false }), 'backend', 'brew-updates-service.py'),
    path.join(process.resourcesPath, 'backend', 'brew-updates-service.py'),
  ];
}

function resolveServiceScriptPath() {
  const candidates = resolveServiceScriptCandidates();
  const resolved = candidates.find((candidate) => fs.existsSync(candidate)) || candidates[0];
  return {
    resolved,
    candidates,
  };
}

const SERVICE_SCRIPT_INFO = resolveServiceScriptPath();
const SERVICE_SCRIPT = SERVICE_SCRIPT_INFO.resolved;
const SERVICE_HOST = '127.0.0.1';
const SERVICE_PORT_MIN = 8765;
const SERVICE_PORT_MAX = 8795;
let activeServicePort = SERVICE_PORT_MIN;
const REQUIRED_PYTHON = '3.14';
const FIXED_UPDATE_REPO = 'tzoharlary/brew-update-manager';

let resolvedPythonBin = null;

let mainWindow = null;
let appUpdater = null;

function broadcastAppUpdateProgress(payload) {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send('app-update:progress', payload);
    }
  }
}

function executableExists(filePath) {
  if (!filePath) return false;
  try {
    fs.accessSync(filePath, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function isPython314(pythonBin) {
  const probe = spawnSync(
    pythonBin,
    ['-c', 'import sys; raise SystemExit(0 if (sys.version_info.major, sys.version_info.minor)==(3,14) else 1)'],
    { stdio: 'ignore' },
  );
  return probe.status === 0;
}

function resolvePythonBin() {
  const fromEnv = process.env.PYTHON_BIN ? [process.env.PYTHON_BIN] : [];
  const candidates = [
    ...fromEnv,
    '/opt/homebrew/bin/python3.14',
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3.14',
    '/usr/local/bin/python3',
    'python3.14',
    'python3',
  ];

  for (const candidate of candidates) {
    if (candidate.includes('/') && !executableExists(candidate)) {
      continue;
    }
    if (isPython314(candidate)) {
      return candidate;
    }
  }

  throw new Error(
    `Python ${REQUIRED_PYTHON} not found. Please install Python ${REQUIRED_PYTHON} and make sure it is available in PATH.`,
  );
}

function getPythonBin() {
  if (!resolvedPythonBin) {
    resolvedPythonBin = resolvePythonBin();
  }
  return resolvedPythonBin;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function serviceBase() {
  return `http://${SERVICE_HOST}:${activeServicePort}`;
}

function serviceUrl(pathname) {
  return `${serviceBase()}${pathname}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || payload.error || `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

async function serviceHealthyOnPort(port) {
  try {
    const res = await fetch(`http://${SERVICE_HOST}:${port}/health`, { method: 'GET' });
    if (!res.ok) return false;

    const payload = await res.json().catch(() => null);
    if (!payload || typeof payload !== 'object') return false;
    return payload.ok === true && payload.app === 'Homebrew Update Manager';
  } catch {
    return false;
  }
}

function isPortAvailable(port) {
  return new Promise((resolve) => {
    const server = net.createServer();

    server.once('error', () => {
      resolve(false);
    });

    server.once('listening', () => {
      server.close(() => resolve(true));
    });

    server.listen(port, SERVICE_HOST);
  });
}

async function selectServicePort() {
  for (let port = SERVICE_PORT_MIN; port <= SERVICE_PORT_MAX; port += 1) {
    // eslint-disable-next-line no-await-in-loop
    if (await serviceHealthyOnPort(port)) {
      return { port, alreadyRunning: true };
    }
  }

  for (let port = SERVICE_PORT_MIN; port <= SERVICE_PORT_MAX; port += 1) {
    // eslint-disable-next-line no-await-in-loop
    if (await isPortAvailable(port)) {
      return { port, alreadyRunning: false };
    }
  }

  throw new Error(`No available backend port found in range ${SERVICE_PORT_MIN}-${SERVICE_PORT_MAX}`);
}

async function ensureServiceRunning() {
  const selection = await selectServicePort();
  activeServicePort = selection.port;

  if (selection.alreadyRunning) return;

  if (!fs.existsSync(SERVICE_SCRIPT)) {
    const tried = SERVICE_SCRIPT_INFO.candidates
      .map((candidate) => `- ${candidate}`)
      .join('\n');
    throw new Error(`Backend service script missing: ${SERVICE_SCRIPT}\nTried:\n${tried}`);
  }

  const pythonBin = getPythonBin();

  const child = spawn(pythonBin, [
    SERVICE_SCRIPT,
    '--serve',
    '--host',
    SERVICE_HOST,
    '--port',
    String(activeServicePort),
  ], {
    detached: true,
    stdio: 'ignore',
  });
  child.unref();

  for (let i = 0; i < 60; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    if (await serviceHealthyOnPort(activeServicePort)) return;
    // eslint-disable-next-line no-await-in-loop
    await delay(200);
  }

  throw new Error(`Service did not become healthy in time on port ${activeServicePort}`);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1340,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    title: 'Homebrew Update Manager',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(appRoot(), 'renderer', 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function focusMainWindow() {
  if (!mainWindow) {
    createWindow();
    return;
  }

  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.focus();
}

async function bootstrap() {
  await ensureServiceRunning();
  createWindow();
}

// Single-instance behavior: additional launches focus existing window.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    focusMainWindow();
  });
}

app.whenReady().then(async () => {
  try {
    appUpdater = createAppUpdater({
      app,
      repo: FIXED_UPDATE_REPO,
    });

    const marker = appUpdater.readAndConsumeUpdateMarker();
    if (marker?.status === 'rolled_back') {
      dialog.showErrorBox(
        'Homebrew Update Manager',
        'The previous app update failed to start correctly and was rolled back to the last working version.',
      );
    }

    await bootstrap();
  } catch (error) {
    dialog.showErrorBox('Homebrew Update Manager', `Failed to start backend service:\n${error.message}`);
    app.quit();
    return;
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    } else {
      focusMainWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

ipcMain.handle('state:get', async () => fetchJson(serviceUrl('/api/state')));
ipcMain.handle('progress:get', async () => fetchJson(serviceUrl('/api/progress')));
ipcMain.handle('updates:history:get', async () => fetchJson(serviceUrl('/api/update-history')));
ipcMain.handle('settings:get', async () => fetchJson(serviceUrl('/api/settings')));
ipcMain.handle('settings:scheduler:update', async (_event, payload) => fetchJson(serviceUrl('/api/settings/scheduler'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('settings:brew-path:update', async (_event, payload) => fetchJson(serviceUrl('/api/settings/brew-path'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('settings:brew-path:auto-detect', async () => fetchJson(serviceUrl('/api/brew/auto-detect')));
ipcMain.handle('app-update:check', async () => {
  if (!appUpdater) {
    throw new Error('App updater is not initialized');
  }

  return appUpdater.checkForUpdate({
    onProgress: broadcastAppUpdateProgress,
  });
});
ipcMain.handle('app-update:download-install', async () => {
  if (!appUpdater) {
    throw new Error('App updater is not initialized');
  }

  return appUpdater.downloadAndInstallUpdate({
    onProgress: broadcastAppUpdateProgress,
  });
});
ipcMain.handle('check:run', async () => fetchJson(serviceUrl('/api/check'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({}),
}));
ipcMain.handle('update:one', async (_event, payload) => fetchJson(serviceUrl('/api/update-one'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('update:all', async () => fetchJson(serviceUrl('/api/update-all'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({}),
}));
ipcMain.handle('update:selected', async (_event, payload) => fetchJson(serviceUrl('/api/update-selected'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('operation:cancel', async () => fetchJson(serviceUrl('/api/cancel-operation'), {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({}),
}));
