const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { Readable, Transform } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { spawn, spawnSync } = require('node:child_process');

const UPDATE_MANIFEST_NAME = 'update-manifest.json';
const DEFAULT_MIN_SUPPORTED_VERSION = '1.0.0';
const DELTA_META_SCHEMA_VERSION = 1;
const MAX_STAGE_DIRS_TO_KEEP = 2;
const APP_BUNDLE_COPY_OPTIONS = {
  recursive: true,
  force: true,
  dereference: false,
  verbatimSymlinks: true,
};

function nowIso() {
  return new Date().toISOString();
}

function normalizeVersion(raw) {
  const normalized = String(raw || '').trim().replace(/^v/i, '');
  return normalized.split(/\s+/)[0];
}

function parseSemverDetailed(raw) {
  const normalized = normalizeVersion(raw);
  const withoutBuild = normalized.split('+')[0];
  const [coreRaw, prereleaseRaw = ''] = withoutBuild.split('-', 2);
  const core = coreRaw.split('.').map((part) => Number.parseInt(part, 10));

  const major = Number.isFinite(core[0]) ? core[0] : 0;
  const minor = Number.isFinite(core[1]) ? core[1] : 0;
  const patch = Number.isFinite(core[2]) ? core[2] : 0;
  const prerelease = prereleaseRaw
    ? prereleaseRaw.split('.').map((part) => String(part || '').trim()).filter(Boolean)
    : [];

  return {
    major,
    minor,
    patch,
    prerelease,
  };
}

function parseSemver(raw) {
  const parsed = parseSemverDetailed(raw);
  return [parsed.major, parsed.minor, parsed.patch];
}

function isNumericSemverIdentifier(value) {
  return /^\d+$/.test(String(value || ''));
}

function comparePrereleaseIdentifiers(left, right) {
  const max = Math.max(left.length, right.length);

  for (let i = 0; i < max; i += 1) {
    const leftPart = left[i];
    const rightPart = right[i];

    if (leftPart === undefined) return -1;
    if (rightPart === undefined) return 1;

    const leftNumeric = isNumericSemverIdentifier(leftPart);
    const rightNumeric = isNumericSemverIdentifier(rightPart);

    if (leftNumeric && rightNumeric) {
      const leftNum = Number.parseInt(leftPart, 10);
      const rightNum = Number.parseInt(rightPart, 10);
      if (leftNum > rightNum) return 1;
      if (leftNum < rightNum) return -1;
      continue;
    }

    if (leftNumeric && !rightNumeric) return -1;
    if (!leftNumeric && rightNumeric) return 1;

    if (leftPart > rightPart) return 1;
    if (leftPart < rightPart) return -1;
  }

  return 0;
}

function compareVersions(a, b) {
  const left = parseSemverDetailed(a);
  const right = parseSemverDetailed(b);

  const coreFields = ['major', 'minor', 'patch'];
  for (const field of coreFields) {
    if (left[field] > right[field]) return 1;
    if (left[field] < right[field]) return -1;
  }

  const leftHasPre = left.prerelease.length > 0;
  const rightHasPre = right.prerelease.length > 0;

  if (!leftHasPre && !rightHasPre) return 0;
  if (!leftHasPre) return 1;
  if (!rightHasPre) return -1;

  return comparePrereleaseIdentifiers(left.prerelease, right.prerelease);
}

function isVersionGreater(latest, current) {
  return compareVersions(latest, current) > 0;
}

function archLabel() {
  return process.arch === 'arm64' ? 'arm64' : 'x64';
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function runCommandOrThrow(command, args, options = {}) {
  const res = spawnSync(command, args, {
    stdio: 'pipe',
    encoding: 'utf8',
    ...options,
  });
  if (res.status !== 0) {
    const detail = (res.stderr || res.stdout || '').trim();
    throw new Error(`${command} ${args.join(' ')} failed: ${detail}`);
  }
  return res;
}

function executableExists(filePath) {
  try {
    fs.accessSync(filePath, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function isPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function normalizeMinimumSupportedVersion(raw) {
  return normalizeVersion(raw || DEFAULT_MIN_SUPPORTED_VERSION) || DEFAULT_MIN_SUPPORTED_VERSION;
}

function isManifestVersionMismatch(manifestVersion, releaseVersion) {
  if (!manifestVersion || !releaseVersion) return false;
  return compareVersions(manifestVersion, releaseVersion) !== 0;
}

function resolveBundleExecutableName(appBundlePath) {
  const infoPlistPath = path.join(appBundlePath, 'Contents', 'Info.plist');
  if (fs.existsSync(infoPlistPath)) {
    try {
      const plistRes = runCommandOrThrow('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleExecutable', infoPlistPath]);
      const value = String(plistRes.stdout || '').trim();
      if (value) {
        return value;
      }
    } catch {
      // Ignore plist lookup failure and fall back to bundle-name based detection.
    }
  }

  return path.basename(appBundlePath, '.app');
}

function resolveBundleExecutablePath(appBundlePath) {
  const macOsDir = path.join(appBundlePath, 'Contents', 'MacOS');
  const preferredName = resolveBundleExecutableName(appBundlePath);
  const preferredPath = path.join(macOsDir, preferredName);
  if (executableExists(preferredPath)) {
    return preferredPath;
  }

  if (!fs.existsSync(macOsDir)) {
    return preferredPath;
  }

  const candidates = fs.readdirSync(macOsDir)
    .map((name) => path.join(macOsDir, name))
    .filter((candidate) => {
      try {
        const st = fs.lstatSync(candidate);
        return st.isFile() && executableExists(candidate);
      } catch {
        return false;
      }
    });

  if (candidates.length === 1) {
    return candidates[0];
  }

  return preferredPath;
}

function resolveElectronFrameworkBinaryPath(appBundlePath) {
  return path.join(appBundlePath, 'Contents', 'Frameworks', 'Electron Framework.framework', 'Electron Framework');
}

function validateAppBundleLayout(appBundlePath) {
  const executablePath = resolveBundleExecutablePath(appBundlePath);
  const frameworkPath = resolveElectronFrameworkBinaryPath(appBundlePath);

  if (!executableExists(executablePath)) {
    throw new Error(`App bundle is missing executable: ${executablePath}`);
  }

  if (!fs.existsSync(frameworkPath)) {
    throw new Error(`App bundle is missing Electron framework binary: ${frameworkPath}`);
  }

  return {
    executablePath,
    frameworkPath,
  };
}

function pruneUpdateStageDirs(updatesRoot, { keep = MAX_STAGE_DIRS_TO_KEEP } = {}) {
  ensureDir(updatesRoot);

  const entries = fs.readdirSync(updatesRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith('stage-'))
    .map((entry) => {
      const fullPath = path.join(updatesRoot, entry.name);
      const stats = fs.lstatSync(fullPath);
      return {
        path: fullPath,
        mtimeMs: Number(stats.mtimeMs || 0),
      };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs);

  for (const stale of entries.slice(Math.max(keep, 0))) {
    fs.rmSync(stale.path, { recursive: true, force: true });
  }
}

function currentAppBundlePath() {
  const bundlePath = path.resolve(process.execPath, '..', '..', '..');
  if (bundlePath.endsWith('.app')) {
    return bundlePath;
  }
  return null;
}

function pickDmgAsset(assets = [], arch = archLabel()) {
  const dmgAssets = assets.filter((asset) => asset?.name && /\.dmg$/i.test(asset.name));
  if (!dmgAssets.length) return null;

  const archPattern = new RegExp(`-${arch}\\.dmg$`, 'i');
  return dmgAssets.find((asset) => archPattern.test(asset.name))
    || dmgAssets.find((asset) => String(asset.name).includes(arch))
    || dmgAssets[0];
}

function createTelemetryLogger(filePath) {
  ensureDir(path.dirname(filePath));
  return (event, payload = {}) => {
    try {
      fs.appendFileSync(filePath, `${JSON.stringify({ ts: nowIso(), event, ...payload })}\n`);
    } catch {
      // Ignore telemetry write failures.
    }
  };
}

async function fetchJsonWithRetry(url, { headers = {}, retries = 3, backoffMs = 500, log, label } = {}) {
  let lastError = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, { headers });
      if (!response.ok) {
        const detail = await response.text().catch(() => '');
        throw new Error(`${label || 'request'} failed (${response.status}): ${detail || 'No details'}`);
      }
      return await response.json();
    } catch (error) {
      lastError = error;
      if (log) {
        log('retry', {
          scope: label || 'request',
          attempt,
          retries,
          error: String(error?.message || error),
        });
      }
      if (attempt < retries) {
        // eslint-disable-next-line no-await-in-loop
        await sleep(backoffMs * (2 ** (attempt - 1)));
      }
    }
  }
  throw lastError;
}

async function downloadWithProgress({
  url,
  destination,
  expectedSha256,
  expectedSize = 0,
  retries = 3,
  backoffMs = 500,
  log,
  onProgress,
  phase,
}) {
  let lastError = null;
  const tmpPath = `${destination}.part`;

  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      if (fs.existsSync(tmpPath)) {
        fs.rmSync(tmpPath, { force: true });
      }

      const response = await fetch(url, {
        headers: {
          Accept: 'application/octet-stream',
          'User-Agent': 'homebrew-update-manager',
        },
      });

      if (!response.ok) {
        const detail = await response.text().catch(() => '');
        throw new Error(`Download failed (${response.status}): ${detail || 'No details'}`);
      }

      if (!response.body) {
        throw new Error('Download stream is empty');
      }

      const total = Number(response.headers.get('content-length') || expectedSize || 0);
      const hash = crypto.createHash('sha256');
      const started = Date.now();
      let transferred = 0;
      let lastEmit = 0;

      const meter = new Transform({
        transform(chunk, _enc, callback) {
          transferred += chunk.length;
          hash.update(chunk);

          const now = Date.now();
          if (onProgress && (now - lastEmit > 250 || (total > 0 && transferred >= total))) {
            const elapsedSec = Math.max(0.001, (now - started) / 1000);
            const speedBps = transferred / elapsedSec;
            const percent = total > 0 ? Math.min(100, (transferred / total) * 100) : 0;
            const remaining = total > 0 ? Math.max(0, total - transferred) : 0;
            const etaSeconds = speedBps > 0 ? Math.round(remaining / speedBps) : null;

            onProgress({
              phase,
              attempt,
              retries,
              transferred,
              total,
              percent,
              speedBps,
              etaSeconds,
            });
            lastEmit = now;
          }

          callback(null, chunk);
        },
      });

      await pipeline(Readable.fromWeb(response.body), meter, fs.createWriteStream(tmpPath));

      const digest = hash.digest('hex');
      if (expectedSha256 && digest.toLowerCase() !== String(expectedSha256).toLowerCase()) {
        throw new Error(`Checksum mismatch for ${path.basename(destination)} (expected ${expectedSha256}, got ${digest})`);
      }

      fs.renameSync(tmpPath, destination);

      if (onProgress) {
        onProgress({
          phase,
          attempt,
          retries,
          transferred,
          total,
          percent: total > 0 ? 100 : 0,
          speedBps: null,
          etaSeconds: 0,
          done: true,
        });
      }

      return;
    } catch (error) {
      lastError = error;
      if (fs.existsSync(tmpPath)) {
        fs.rmSync(tmpPath, { force: true });
      }
      if (log) {
        log('download_retry', {
          url,
          phase,
          attempt,
          retries,
          error: String(error?.message || error),
        });
      }
      if (attempt < retries) {
        // eslint-disable-next-line no-await-in-loop
        await sleep(backoffMs * (2 ** (attempt - 1)));
      }
    }
  }

  throw lastError;
}

function loadJsonSafe(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
}

function ensureSafeRelativePath(rawPath, context = 'path') {
  const normalizedInput = String(rawPath || '').trim().replace(/\\/g, '/');
  if (!normalizedInput) {
    throw new Error(`Delta ${context} is empty`);
  }

  if (path.posix.isAbsolute(normalizedInput)) {
    throw new Error(`Delta ${context} must be relative: ${normalizedInput}`);
  }

  const normalized = path.posix.normalize(normalizedInput);
  if (normalized === '.' || normalized === '..' || normalized.startsWith('../') || normalized.includes('/../')) {
    throw new Error(`Delta ${context} is unsafe: ${normalizedInput}`);
  }

  return normalized;
}

function validateDeltaMeta(meta, {
  expectedFromVersion = '',
  expectedToVersion = '',
  expectedArch = '',
} = {}) {
  if (!meta || typeof meta !== 'object') {
    throw new Error('Delta package meta.json is missing or invalid');
  }

  const schemaVersion = Number(meta.schemaVersion);
  if (!Number.isInteger(schemaVersion) || schemaVersion !== DELTA_META_SCHEMA_VERSION) {
    throw new Error(`Unsupported delta schema version: ${meta.schemaVersion}`);
  }

  if (!Array.isArray(meta.removedPaths)) {
    throw new Error('Delta package removedPaths is missing or invalid');
  }

  if (expectedFromVersion) {
    const fromVersion = normalizeVersion(meta.fromVersion || '');
    if (fromVersion !== normalizeVersion(expectedFromVersion)) {
      throw new Error(`Delta fromVersion mismatch (expected ${expectedFromVersion}, got ${meta.fromVersion || 'unknown'})`);
    }
  }

  if (expectedToVersion) {
    const toVersion = normalizeVersion(meta.toVersion || '');
    if (toVersion && toVersion !== normalizeVersion(expectedToVersion)) {
      throw new Error(`Delta toVersion mismatch (expected ${expectedToVersion}, got ${meta.toVersion || 'unknown'})`);
    }
  }

  if (expectedArch) {
    const metaArch = String(meta.arch || '').trim().toLowerCase();
    if (metaArch && metaArch !== String(expectedArch).trim().toLowerCase()) {
      throw new Error(`Delta arch mismatch (expected ${expectedArch}, got ${meta.arch || 'unknown'})`);
    }
  }
}

function findFirstAppBundle(rootDir) {
  const entries = fs.readdirSync(rootDir, { withFileTypes: true });
  const appEntry = entries.find((entry) => entry.isDirectory() && entry.name.endsWith('.app'));
  if (!appEntry) {
    return null;
  }
  return path.join(rootDir, appEntry.name);
}

function fsEntryType(filePath) {
  try {
    const stats = fs.lstatSync(filePath);
    if (stats.isSymbolicLink()) return 'symlink';
    if (stats.isDirectory()) return 'dir';
    if (stats.isFile()) return 'file';
    return 'other';
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      return 'missing';
    }
    throw error;
  }
}

function removePathIfExists(filePath) {
  fs.rmSync(filePath, { recursive: true, force: true });
}

function mergeDeltaNode(sourcePath, targetPath) {
  const sourceType = fsEntryType(sourcePath);
  const targetType = fsEntryType(targetPath);

  if (sourceType === 'dir') {
    if (targetType !== 'missing' && targetType !== 'dir') {
      removePathIfExists(targetPath);
    }
    ensureDir(targetPath);

    const entries = fs.readdirSync(sourcePath, { withFileTypes: true });
    for (const entry of entries) {
      mergeDeltaNode(path.join(sourcePath, entry.name), path.join(targetPath, entry.name));
    }
    return;
  }

  ensureDir(path.dirname(targetPath));

  if (sourceType === 'symlink') {
    removePathIfExists(targetPath);
    const linkTarget = fs.readlinkSync(sourcePath);
    fs.symlinkSync(linkTarget, targetPath);
    return;
  }

  if (targetType !== 'missing' && targetType !== 'file') {
    removePathIfExists(targetPath);
  }

  fs.cpSync(sourcePath, targetPath, {
    recursive: false,
    force: true,
    dereference: false,
  });
}

function mergeDeltaTree(sourceRoot, targetRoot) {
  if (!fs.existsSync(sourceRoot)) {
    return;
  }

  const entries = fs.readdirSync(sourceRoot, { withFileTypes: true });
  for (const entry of entries) {
    mergeDeltaNode(path.join(sourceRoot, entry.name), path.join(targetRoot, entry.name));
  }
}

function applyDeltaArchive({
  sourceAppPath,
  deltaArchivePath,
  outputAppPath,
  workDir,
  expectedFromVersion = '',
  expectedToVersion = '',
  expectedArch = '',
}) {
  if (!fs.existsSync(sourceAppPath)) {
    throw new Error(`Current app bundle not found at ${sourceAppPath}`);
  }

  fs.rmSync(outputAppPath, { recursive: true, force: true });
  fs.cpSync(sourceAppPath, outputAppPath, APP_BUNDLE_COPY_OPTIONS);

  const deltaExtractDir = path.join(workDir, 'delta-extract');
  ensureDir(deltaExtractDir);
  runCommandOrThrow('tar', ['-xzf', deltaArchivePath, '-C', deltaExtractDir]);

  const metaPath = path.join(deltaExtractDir, 'meta.json');
  const filesRoot = path.join(deltaExtractDir, 'files');
  const meta = loadJsonSafe(metaPath);
  validateDeltaMeta(meta, {
    expectedFromVersion,
    expectedToVersion,
    expectedArch,
  });

  if (fs.existsSync(filesRoot)) {
    mergeDeltaTree(filesRoot, outputAppPath);
  }

  const removed = [...meta.removedPaths].sort((a, b) => String(b).length - String(a).length);
  for (const relPath of removed) {
    const safeRelPath = ensureSafeRelativePath(relPath, 'removed path');
    const target = path.join(outputAppPath, safeRelPath);
    if (fs.existsSync(target)) {
      fs.rmSync(target, { recursive: true, force: true });
    }
  }

  const executablePath = resolveBundleExecutablePath(outputAppPath);
  if (!executableExists(executablePath)) {
    throw new Error('Delta apply completed but resulting app bundle executable is missing');
  }

  return outputAppPath;
}

function extractFullArchive({ archivePath, outputDir }) {
  ensureDir(outputDir);
  runCommandOrThrow('tar', ['-xzf', archivePath, '-C', outputDir]);
  const appBundle = findFirstAppBundle(outputDir);
  if (!appBundle) {
    throw new Error('Full package extraction did not produce an app bundle');
  }
  return appBundle;
}

function writeApplyScript() {
  const scriptPath = path.join(os.tmpdir(), `bum-apply-update-${Date.now()}.sh`);
  const script = `#!/bin/bash
set -euo pipefail

TARGET_APP="$1"
STAGED_APP="$2"
BACKUP_APP="$3"
MARKER_FILE="$4"
CURRENT_PID="$5"
TARGET_EXEC="$6"
STAGE_ROOT="$7"
TARGET_FRAMEWORK="$8"

FINALIZED="0"

mkdir -p "$(dirname "$MARKER_FILE")"

write_marker() {
  local status="$1"
  echo "{\"status\":\"$status\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$MARKER_FILE"
}

cleanup_stage_root() {
  if [[ -n "$STAGE_ROOT" && -d "$STAGE_ROOT" ]]; then
    rm -rf "$STAGE_ROOT" 2>/dev/null || true
  fi
}

restore_backup() {
  if [[ "$FINALIZED" == "1" ]]; then
    return
  fi

  rm -rf "$TARGET_APP" 2>/dev/null || true
  if [[ -d "$BACKUP_APP" ]]; then
    mv "$BACKUP_APP" "$TARGET_APP" 2>/dev/null || true
    open "$TARGET_APP" >/dev/null 2>&1 || true
  fi

  write_marker "rolled_back"
  cleanup_stage_root
}

trap restore_backup ERR
trap restore_backup EXIT

is_bundle_layout_valid() {
  if [[ ! -d "$TARGET_APP" ]]; then
    return 1
  fi
  if [[ ! -x "$TARGET_EXEC" ]]; then
    return 1
  fi
  if [[ ! -f "$TARGET_FRAMEWORK" ]]; then
    return 1
  fi
  return 0
}

staged_layout_valid() {
  local staged_exec="$STAGED_APP/Contents/MacOS/$(basename "$TARGET_EXEC")"
  local staged_framework="$STAGED_APP/Contents/Frameworks/Electron Framework.framework/Electron Framework"

  if [[ ! -x "$staged_exec" ]]; then
    return 1
  fi
  if [[ ! -f "$staged_framework" ]]; then
    return 1
  fi
  return 0
}

current_target_pid() {
  local pid=""
  pid="$(pgrep -f "$TARGET_EXEC" | head -n 1 || true)"
  if [[ -z "$pid" ]]; then
    local target_basename
    target_basename="$(basename "$TARGET_EXEC")"
    if [[ -n "$target_basename" ]]; then
      pid="$(pgrep -x "$target_basename" | head -n 1 || true)"
    fi
  fi
  echo "$pid"
}

write_marker "pending"

while kill -0 "$CURRENT_PID" 2>/dev/null; do
  sleep 0.5
done

if [[ ! -d "$STAGED_APP" ]]; then
  write_marker "failed_staged_missing"
  exit 1
fi

if ! staged_layout_valid; then
  write_marker "failed_staged_layout"
  exit 1
fi

rm -rf "$BACKUP_APP" 2>/dev/null || true
if [[ -d "$TARGET_APP" ]]; then
  mv "$TARGET_APP" "$BACKUP_APP"
fi

mv "$STAGED_APP" "$TARGET_APP"

if ! is_bundle_layout_valid; then
  write_marker "failed_target_layout"
  exit 1
fi

open "$TARGET_APP" >/dev/null 2>&1 || true

stable_pid=""
stable_hits=0

for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  if ! is_bundle_layout_valid; then
    stable_pid=""
    stable_hits=0
    continue
  fi

  pid="$(current_target_pid)"
  if [[ -n "$pid" ]]; then
    if [[ "$pid" == "$stable_pid" ]]; then
      stable_hits=$((stable_hits + 1))
    else
      stable_pid="$pid"
      stable_hits=1
    fi
    if [[ "$stable_hits" -ge 3 ]]; then
      rm -rf "$BACKUP_APP" 2>/dev/null || true
      write_marker "ok"
      cleanup_stage_root
      FINALIZED="1"
      trap - ERR EXIT
      rm -f "$0" || true
      exit 0
    fi
  else
    stable_pid=""
    stable_hits=0
  fi
done
exit 1
`;
  fs.writeFileSync(scriptPath, script, { mode: 0o700 });
  return scriptPath;
}

function scheduleApplyOnQuit({ app, stagedAppPath, markerFile, stageRoot }) {
  const targetApp = currentAppBundlePath();
  if (!targetApp) {
    throw new Error('Unable to resolve current app bundle for apply-on-quit');
  }

  const targetLayout = validateAppBundleLayout(targetApp);
  const backupApp = `${targetApp}.rollback`;
  const targetExec = targetLayout.executablePath;
  const targetFramework = targetLayout.frameworkPath;
  const applyScript = writeApplyScript();

  const helper = spawn('bash', [
    applyScript,
    targetApp,
    stagedAppPath,
    backupApp,
    markerFile,
    String(process.pid),
    targetExec,
    stageRoot,
    targetFramework,
  ], {
    detached: true,
    stdio: 'ignore',
  });

  helper.unref();

  setTimeout(() => {
    app.quit();
  }, 650);

  return {
    restartScheduled: true,
    targetApp,
  };
}

function findManifestAsset(assets = []) {
  return assets.find((asset) => String(asset?.name || '') === UPDATE_MANIFEST_NAME) || null;
}

function releaseHeaders() {
  const headers = {
    'User-Agent': 'homebrew-update-manager',
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };

  if (process.env.GITHUB_TOKEN) {
    headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  }

  return headers;
}

function createAppUpdater({ app, repo }) {
  const updatesRoot = path.join(app.getPath('userData'), 'updates');
  const telemetryFile = path.join(updatesRoot, 'telemetry.log');
  const markerFile = path.join(updatesRoot, 'last-update-result.json');
  const log = createTelemetryLogger(telemetryFile);

  async function fetchLatestRelease() {
    return fetchJsonWithRetry(`https://api.github.com/repos/${repo}/releases/latest`, {
      headers: releaseHeaders(),
      retries: 3,
      backoffMs: 500,
      log,
      label: 'latest_release',
    });
  }

  async function loadLatestManifest(release) {
    const assets = Array.isArray(release?.assets) ? release.assets : [];
    const manifestAsset = findManifestAsset(assets);
    if (!manifestAsset?.browser_download_url) {
      return null;
    }

    const manifest = await fetchJsonWithRetry(manifestAsset.browser_download_url, {
      headers: {
        'User-Agent': 'homebrew-update-manager',
        Accept: 'application/json',
      },
      retries: 3,
      backoffMs: 500,
      log,
      label: 'update_manifest',
    });

    return {
      manifest,
      manifestAsset,
    };
  }

  async function checkForUpdate({ onProgress } = {}) {
    const started = Date.now();
    const currentVersion = normalizeVersion(app.getVersion());
    const arch = archLabel();

    if (onProgress) {
      onProgress({ phase: 'check_metadata', percent: 0, message: 'Checking update metadata' });
    }

    log('check_start', { currentVersion, arch });

    const release = await fetchLatestRelease();
    const latestVersionFromTag = normalizeVersion(release?.tag_name || release?.name || '');

    let manifestBundle = null;
    try {
      manifestBundle = await loadLatestManifest(release);
    } catch (error) {
      log('manifest_load_failed', { error: String(error?.message || error) });
      manifestBundle = null;
    }

    if (manifestBundle?.manifest) {
      const manifest = manifestBundle.manifest;
      const versions = isPlainObject(manifest.versions) ? manifest.versions : {};
      const manifestLatestVersion = normalizeVersion(manifest.latestVersion || '');
      const versionMismatch = isManifestVersionMismatch(manifestLatestVersion, latestVersionFromTag);
      const minimumSupportedVersion = normalizeMinimumSupportedVersion(manifest.minimumSupportedVersion);

      let latestVersion = manifestLatestVersion || latestVersionFromTag;
      if (versionMismatch && latestVersionFromTag) {
        latestVersion = latestVersionFromTag;
        log('manifest_version_mismatch', {
          manifestLatestVersion,
          releaseLatestVersion: latestVersionFromTag,
          currentVersion,
          arch,
        });
      }

      let latestEntry = latestVersion ? versions[latestVersion] || null : null;
      if (!latestEntry && manifestLatestVersion && versions[manifestLatestVersion]) {
        latestEntry = versions[manifestLatestVersion];
        latestVersion = manifestLatestVersion;
      }

      const archAssets = latestEntry?.assets?.[arch] || null;
      const fullAsset = archAssets?.full || null;
      const deltaAsset = Array.isArray(archAssets?.deltas)
        ? archAssets.deltas.find((item) => normalizeVersion(item.fromVersion) === currentVersion)
        : null;

      const updateAvailable = !!latestVersion && isVersionGreater(latestVersion, currentVersion);
      const withinSupport = compareVersions(currentVersion, minimumSupportedVersion) >= 0;

      if (fullAsset) {
        const payload = {
          ok: true,
          mode: 'manifest',
          repo,
          currentVersion,
          latestVersion,
          updateAvailable,
          releasePageUrl: String(release?.html_url || ''),
          manifestUrl: String(manifestBundle.manifestAsset?.browser_download_url || ''),
          minimumSupportedVersion,
          withinSupport,
          manifestVersionMismatch: versionMismatch,
          hasInstallAsset: true,
          canInstall: true,
          fullAsset,
          deltaAsset,
          deltaAvailable: !!deltaAsset,
          supportsDelta: !!deltaAsset && withinSupport,
          preferredPath: (deltaAsset && withinSupport) ? 'delta' : 'full',
          arch,
          checkedAt: nowIso(),
        };

        log('check_done', {
          mode: payload.mode,
          updateAvailable: payload.updateAvailable,
          deltaAvailable: payload.deltaAvailable,
          durationMs: Date.now() - started,
        });

        if (onProgress) {
          onProgress({ phase: 'check_done', percent: 100, mode: payload.preferredPath, updateAvailable: payload.updateAvailable });
        }

        return payload;
      }

      log('manifest_missing_full_asset', {
        currentVersion,
        latestVersion,
        arch,
        versionMismatch,
      });
    }

    const assets = Array.isArray(release?.assets) ? release.assets : [];
    const installerAsset = pickDmgAsset(assets, arch);
    const latestVersion = latestVersionFromTag;
    const updateAvailable = isVersionGreater(latestVersion, currentVersion);

    const payload = {
      ok: true,
      mode: 'legacy-dmg',
      repo,
      currentVersion,
      latestVersion,
      updateAvailable,
      hasInstallAsset: !!installerAsset,
      canInstall: !!installerAsset,
      deltaAvailable: false,
      supportsDelta: false,
      preferredPath: 'full',
      releasePageUrl: String(release?.html_url || ''),
      assetName: installerAsset?.name || null,
      assetUrl: installerAsset?.browser_download_url || null,
      arch,
      checkedAt: nowIso(),
    };

    log('check_done', {
      mode: payload.mode,
      updateAvailable: payload.updateAvailable,
      deltaAvailable: false,
      durationMs: Date.now() - started,
    });

    if (onProgress) {
      onProgress({ phase: 'check_done', percent: 100, mode: payload.preferredPath, updateAvailable: payload.updateAvailable });
    }

    return payload;
  }

  async function prepareFullStagedApp({ fullAsset, stageRoot, onProgress }) {
    const archivePath = path.join(stageRoot, path.basename(fullAsset.name || 'full-update.tar.gz'));

    if (onProgress) {
      onProgress({ phase: 'download_full', percent: 0, message: 'Downloading full package' });
    }

    await downloadWithProgress({
      url: fullAsset.url,
      destination: archivePath,
      expectedSha256: fullAsset.sha256,
      expectedSize: Number(fullAsset.size || 0),
      retries: 3,
      backoffMs: 700,
      log,
      onProgress: onProgress ? (event) => onProgress({ ...event, mode: 'full' }) : undefined,
      phase: 'download_full',
    });

    if (onProgress) {
      onProgress({ phase: 'verify_full', percent: 100, message: 'Verifying full package' });
    }

    const extractedRoot = path.join(stageRoot, 'full-extract');
    const stagedApp = extractFullArchive({ archivePath, outputDir: extractedRoot });
    return stagedApp;
  }

  async function prepareDeltaStagedApp({
    deltaAsset,
    stageRoot,
    onProgress,
    expectedFromVersion = '',
    expectedToVersion = '',
    expectedArch = '',
  }) {
    const sourceAppPath = currentAppBundlePath();
    if (!sourceAppPath) {
      throw new Error('Current app bundle path is unavailable for delta apply');
    }

    const deltaArchivePath = path.join(stageRoot, path.basename(deltaAsset.name || 'delta-update.tar.gz'));

    if (onProgress) {
      onProgress({ phase: 'download_delta', percent: 0, message: 'Downloading delta package' });
    }

    await downloadWithProgress({
      url: deltaAsset.url,
      destination: deltaArchivePath,
      expectedSha256: deltaAsset.sha256,
      expectedSize: Number(deltaAsset.size || 0),
      retries: 3,
      backoffMs: 700,
      log,
      onProgress: onProgress ? (event) => onProgress({ ...event, mode: 'delta' }) : undefined,
      phase: 'download_delta',
    });

    if (onProgress) {
      onProgress({ phase: 'verify_delta', percent: 100, message: 'Verifying delta package' });
    }

    if (onProgress) {
      onProgress({ phase: 'apply_delta', percent: 0, message: 'Applying delta package' });
    }

    const stagedAppPath = path.join(stageRoot, path.basename(sourceAppPath));
    const appliedApp = applyDeltaArchive({
      sourceAppPath,
      deltaArchivePath,
      outputAppPath: stagedAppPath,
      workDir: path.join(stageRoot, 'delta-work'),
      expectedFromVersion,
      expectedToVersion,
      expectedArch,
    });

    if (onProgress) {
      onProgress({ phase: 'apply_delta', percent: 100, message: 'Delta package applied' });
    }

    return appliedApp;
  }

  async function installFromManifest(checkPayload, { onProgress } = {}) {
    const started = Date.now();

    try {
      pruneUpdateStageDirs(updatesRoot, { keep: MAX_STAGE_DIRS_TO_KEEP });
    } catch (error) {
      log('stage_prune_failed', { error: String(error?.message || error) });
    }

    const stageRoot = fs.mkdtempSync(path.join(updatesRoot, `stage-${checkPayload.latestVersion}-${checkPayload.arch}-`));

    let modeUsed = 'full';
    let fallbackUsed = false;
    let fallbackReason = '';
    let stagedAppPath = null;

    try {
      if (checkPayload.deltaAvailable && checkPayload.deltaAsset && checkPayload.withinSupport) {
        try {
          stagedAppPath = await prepareDeltaStagedApp({
            deltaAsset: checkPayload.deltaAsset,
            stageRoot,
            onProgress,
            expectedFromVersion: checkPayload.currentVersion,
            expectedToVersion: checkPayload.latestVersion,
            expectedArch: checkPayload.arch,
          });
          modeUsed = 'delta';
          log('delta_apply_success', {
            from: checkPayload.currentVersion,
            to: checkPayload.latestVersion,
            arch: checkPayload.arch,
          });
        } catch (error) {
          fallbackUsed = true;
          fallbackReason = String(error?.message || error);
          log('delta_apply_failed', {
            from: checkPayload.currentVersion,
            to: checkPayload.latestVersion,
            arch: checkPayload.arch,
            error: fallbackReason,
          });
          if (onProgress) {
            onProgress({
              phase: 'fallback_full',
              percent: 0,
              message: 'Delta failed, falling back to full package',
              fallbackReason,
            });
          }
        }
      }

      if (!stagedAppPath) {
        stagedAppPath = await prepareFullStagedApp({
          fullAsset: checkPayload.fullAsset,
          stageRoot,
          onProgress,
        });
        modeUsed = 'full';
      }

      validateAppBundleLayout(stagedAppPath);

      if (onProgress) {
        onProgress({ phase: 'schedule_restart', percent: 100, message: 'Update staged. Restarting application…', modeUsed, fallbackUsed });
      }

      if (!app.isPackaged) {
        log('install_staged_dev_mode', {
          from: checkPayload.currentVersion,
          to: checkPayload.latestVersion,
          modeUsed,
          fallbackUsed,
          durationMs: Date.now() - started,
        });

        return {
          ...checkPayload,
          ok: true,
          restartScheduled: false,
          modeUsed,
          fallbackUsed,
          fallbackReason,
          downloadedPath: stagedAppPath,
        };
      }

      const applyResult = scheduleApplyOnQuit({
        app,
        stagedAppPath,
        markerFile,
        stageRoot,
      });

      log('install_staged', {
        from: checkPayload.currentVersion,
        to: checkPayload.latestVersion,
        modeUsed,
        fallbackUsed,
        durationMs: Date.now() - started,
      });

      return {
        ...checkPayload,
        ok: true,
        restartScheduled: !!applyResult.restartScheduled,
        modeUsed,
        fallbackUsed,
        fallbackReason,
      };
    } catch (error) {
      if (fs.existsSync(stageRoot)) {
        fs.rmSync(stageRoot, { recursive: true, force: true });
      }

      log('install_failed', {
        from: checkPayload.currentVersion,
        to: checkPayload.latestVersion,
        modeUsed,
        fallbackUsed,
        error: String(error?.message || error),
        durationMs: Date.now() - started,
      });
      throw error;
    }
  }

  async function installLegacyDmg(checkPayload, { onProgress } = {}) {
    if (!checkPayload.assetUrl || !checkPayload.assetName) {
      throw new Error('No legacy DMG installer asset is available');
    }

    const downloadsDir = app.getPath('downloads');
    ensureDir(downloadsDir);
    const targetPath = path.join(downloadsDir, checkPayload.assetName);

    await downloadWithProgress({
      url: checkPayload.assetUrl,
      destination: targetPath,
      expectedSha256: '',
      expectedSize: 0,
      retries: 3,
      backoffMs: 700,
      log,
      onProgress,
      phase: 'download_full',
    });

    const openError = await require('electron').shell.openPath(targetPath);
    if (openError) {
      throw new Error(`Installer downloaded but failed to open: ${openError}`);
    }

    log('legacy_install_opened', {
      from: checkPayload.currentVersion,
      to: checkPayload.latestVersion,
    });

    return {
      ...checkPayload,
      ok: true,
      restartScheduled: false,
      modeUsed: 'legacy-dmg',
      fallbackUsed: false,
      fallbackReason: '',
      downloadedPath: targetPath,
    };
  }

  async function downloadAndInstallUpdate({ onProgress } = {}) {
    const checkPayload = await checkForUpdate({ onProgress });

    if (!checkPayload.updateAvailable) {
      return {
        ...checkPayload,
        ok: false,
        reason: 'up-to-date',
      };
    }

    if (!checkPayload.hasInstallAsset) {
      throw new Error('No compatible install asset found for this architecture');
    }

    if (checkPayload.mode === 'manifest') {
      return installFromManifest(checkPayload, { onProgress });
    }

    return installLegacyDmg(checkPayload, { onProgress });
  }

  function readAndConsumeUpdateMarker() {
    if (!fs.existsSync(markerFile)) {
      return null;
    }

    const marker = loadJsonSafe(markerFile);
    if (!marker || typeof marker !== 'object') {
      fs.rmSync(markerFile, { force: true });
      return null;
    }

    const status = String(marker.status || '').trim();
    if (status === 'ok' || status === 'rolled_back') {
      fs.rmSync(markerFile, { force: true });
    }

    log('update_marker', marker);
    return marker;
  }

  return {
    checkForUpdate,
    downloadAndInstallUpdate,
    readAndConsumeUpdateMarker,
  };
}

module.exports = {
  applyDeltaArchive,
  createAppUpdater,
  normalizeVersion,
  parseSemver,
  compareVersions,
  isVersionGreater,
  archLabel,
};
