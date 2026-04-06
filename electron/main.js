const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { spawn, spawnSync } = require('node:child_process');

const DEV_APP_ROOT = path.join(__dirname, '..');
const PACKAGED_ASAR_ROOT = path.join(process.resourcesPath, 'app.asar');
const PACKAGED_UNPACKED_ROOT = path.join(process.resourcesPath, 'app.asar.unpacked');

function appRoot({ unpacked = false } = {}) {
  if (!app.isPackaged) {
    return DEV_APP_ROOT;
  }

  return unpacked ? PACKAGED_UNPACKED_ROOT : PACKAGED_ASAR_ROOT;
}

const SERVICE_SCRIPT = path.join(appRoot({ unpacked: true }), 'backend', 'brew-updates-service.py');
const SERVICE_HOST = '127.0.0.1';
const SERVICE_PORT = 8765;
const SERVICE_BASE = `http://${SERVICE_HOST}:${SERVICE_PORT}`;
const REQUIRED_PYTHON = '3.14';
const FIXED_UPDATE_REPO = 'tzoharlary/brew-update-manager';

let resolvedPythonBin = null;

let mainWindow = null;

function normalizeVersion(raw) {
  return String(raw || '').trim().replace(/^v/i, '').split('-')[0];
}

function parseSemver(raw) {
  const normalized = normalizeVersion(raw);
  const parts = normalized.split('.').map((part) => Number.parseInt(part, 10));
  const safe = [
    Number.isFinite(parts[0]) ? parts[0] : 0,
    Number.isFinite(parts[1]) ? parts[1] : 0,
    Number.isFinite(parts[2]) ? parts[2] : 0,
  ];
  return safe;
}

function isVersionGreater(latest, current) {
  const l = parseSemver(latest);
  const c = parseSemver(current);
  for (let i = 0; i < 3; i += 1) {
    if (l[i] > c[i]) return true;
    if (l[i] < c[i]) return false;
  }
  return false;
}

function targetArchLabel() {
  return process.arch === 'arm64' ? 'arm64' : 'x64';
}

function pickInstallerAsset(assets = []) {
  const dmgAssets = assets.filter((asset) => asset?.name && /\.dmg$/i.test(asset.name));
  if (!dmgAssets.length) return null;

  const arch = targetArchLabel();
  const archPattern = new RegExp(`-${arch}\\.dmg$`, 'i');
  return dmgAssets.find((asset) => archPattern.test(asset.name))
    || dmgAssets.find((asset) => String(asset.name).includes(arch))
    || dmgAssets[0];
}

async function fetchLatestRelease() {
  const repo = FIXED_UPDATE_REPO;
  const response = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, {
    headers: {
      'User-Agent': 'homebrew-update-manager',
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`GitHub release check failed (${response.status}): ${detail || 'No details'}`);
  }

  const release = await response.json();
  const assets = Array.isArray(release?.assets) ? release.assets : [];
  const installerAsset = pickInstallerAsset(assets);

  const latestVersion = normalizeVersion(release?.tag_name || release?.name || '');
  if (!latestVersion) {
    throw new Error('Latest GitHub release does not include a valid version tag');
  }

  return {
    repo,
    latestVersion,
    tagName: String(release?.tag_name || ''),
    releasePageUrl: String(release?.html_url || ''),
    installerAsset,
  };
}

function updateCheckPayloadFromRelease(release) {
  const currentVersion = normalizeVersion(app.getVersion());
  const updateAvailable = isVersionGreater(release.latestVersion, currentVersion);
  return {
    repo: release.repo,
    currentVersion,
    latestVersion: release.latestVersion,
    updateAvailable,
    releasePageUrl: release.releasePageUrl,
    hasInstallAsset: !!release.installerAsset,
    assetName: release.installerAsset?.name || null,
    assetUrl: release.installerAsset?.browser_download_url || null,
    arch: targetArchLabel(),
    checkedAt: new Date().toISOString(),
  };
}

async function downloadInstallerAsset(assetUrl, targetPath) {
  const response = await fetch(assetUrl, {
    headers: {
      'User-Agent': 'homebrew-update-manager',
      Accept: 'application/octet-stream',
    },
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`Failed downloading installer (${response.status}): ${detail || 'No details'}`);
  }

  if (!response.body) {
    throw new Error('Installer download stream is empty');
  }

  await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(targetPath));
}

function downloadsDirPath() {
  try {
    return app.getPath('downloads');
  } catch {
    return path.join(os.homedir(), 'Downloads');
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

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || payload.error || `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

async function serviceHealthy() {
  try {
    const res = await fetch(`${SERVICE_BASE}/health`, { method: 'GET' });
    return res.ok;
  } catch {
    return false;
  }
}

async function ensureServiceRunning() {
  if (await serviceHealthy()) return;

  if (!fs.existsSync(SERVICE_SCRIPT)) {
    throw new Error(`Backend service script missing: ${SERVICE_SCRIPT}`);
  }

  const pythonBin = getPythonBin();

  const child = spawn(pythonBin, [
    SERVICE_SCRIPT,
    '--serve',
    '--host',
    SERVICE_HOST,
    '--port',
    String(SERVICE_PORT),
  ], {
    detached: true,
    stdio: 'ignore',
  });
  child.unref();

  for (let i = 0; i < 60; i += 1) {
    if (await serviceHealthy()) return;
    // eslint-disable-next-line no-await-in-loop
    await delay(200);
  }

  throw new Error('Service did not become healthy in time');
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

ipcMain.handle('state:get', async () => fetchJson(`${SERVICE_BASE}/api/state`));
ipcMain.handle('progress:get', async () => fetchJson(`${SERVICE_BASE}/api/progress`));
ipcMain.handle('settings:get', async () => fetchJson(`${SERVICE_BASE}/api/settings`));
ipcMain.handle('settings:scheduler:update', async (_event, payload) => fetchJson(`${SERVICE_BASE}/api/settings/scheduler`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('settings:brew-path:update', async (_event, payload) => fetchJson(`${SERVICE_BASE}/api/settings/brew-path`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('settings:brew-path:auto-detect', async () => fetchJson(`${SERVICE_BASE}/api/brew/auto-detect`));
ipcMain.handle('app-update:check', async () => {
  const release = await fetchLatestRelease();
  return updateCheckPayloadFromRelease(release);
});
ipcMain.handle('app-update:download-install', async () => {
  const release = await fetchLatestRelease();
  const status = updateCheckPayloadFromRelease(release);

  if (!status.updateAvailable) {
    return {
      ...status,
      ok: false,
      reason: 'up-to-date',
    };
  }

  if (!status.hasInstallAsset || !status.assetUrl || !status.assetName) {
    throw new Error('No matching installer asset was found for this Mac architecture');
  }

  const downloadsDir = downloadsDirPath();
  fs.mkdirSync(downloadsDir, { recursive: true });

  const targetPath = path.join(downloadsDir, status.assetName);
  await downloadInstallerAsset(status.assetUrl, targetPath);

  const openError = await shell.openPath(targetPath);
  if (openError) {
    throw new Error(`Installer downloaded but failed to open: ${openError}`);
  }

  return {
    ...status,
    ok: true,
    downloadedPath: targetPath,
  };
});
ipcMain.handle('check:run', async () => fetchJson(`${SERVICE_BASE}/api/check`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({}),
}));
ipcMain.handle('update:one', async (_event, payload) => fetchJson(`${SERVICE_BASE}/api/update-one`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload || {}),
}));
ipcMain.handle('update:all', async () => fetchJson(`${SERVICE_BASE}/api/update-all`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({}),
}));
