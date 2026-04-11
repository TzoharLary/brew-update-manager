#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { spawnSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const DIST_DIR = path.join(ROOT, 'dist');
const MANIFEST_NAME = 'update-manifest.json';
const CHECKSUMS_NAME = 'update-checksums.txt';
const REPO = process.env.UPDATE_REPO || 'tzoharlary/brew-update-manager';
const STRICT_DELTA_VALIDATION = String(process.env.STRICT_DELTA_VALIDATION || 'true').toLowerCase() !== 'false';
const APP_COPY_OPTIONS = {
  recursive: true,
  force: true,
  dereference: false,
  verbatimSymlinks: true,
};

const { applyDeltaArchive } = require(path.join(ROOT, 'electron', 'app-updater'));

function normalizeVersion(raw) {
  return String(raw || '').trim().replace(/^v/i, '').split('-')[0];
}

function runOrThrow(command, args, options = {}) {
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

function commandAvailable(command) {
  const probe = spawnSync(command, ['--version'], {
    stdio: 'ignore',
  });
  return probe.status === 0;
}

function safeDetach(mountPoint) {
  try {
    runOrThrow('hdiutil', ['detach', mountPoint, '-quiet']);
  } catch {
    // ignore detach failures in cleanup
  }
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
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

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function encodeAssetName(name) {
  return encodeURIComponent(name).replace(/%2F/g, '/');
}

function releaseAssetUrl(tag, fileName) {
  return `https://github.com/${REPO}/releases/download/${tag}/${encodeAssetName(fileName)}`;
}

function canonicalAssetName(name) {
  return String(name || '').trim().replace(/\s+/g, '.');
}

async function sha256OfFile(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
    stream.on('error', reject);
  });
}

function downloadFileWithCurl(url, destination, headers = {}) {
  const args = [
    '-L',
    '--fail',
    '--retry',
    '8',
    '--retry-delay',
    '2',
    '--retry-all-errors',
    '--connect-timeout',
    '20',
    '-C',
    '-',
    '-o',
    destination,
  ];

  for (const [key, value] of Object.entries(headers || {})) {
    args.push('-H', `${key}: ${value}`);
  }

  args.push(url);
  runOrThrow('curl', args);
}

async function downloadFile(url, destination, headers = {}) {
  ensureDir(path.dirname(destination));

  const requestHeaders = {
    'User-Agent': 'homebrew-update-manager',
    Accept: 'application/octet-stream',
    ...headers,
  };

  let lastError = null;

  if (commandAvailable('curl')) {
    try {
      downloadFileWithCurl(url, destination, requestHeaders);
      return;
    } catch (error) {
      lastError = error;
      console.warn(`[update-manifest] curl download fallback failed: ${error.message}`);
    }
  }

  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      fs.rmSync(destination, { force: true });

      const response = await fetch(url, {
        headers: requestHeaders,
      });

      if (!response.ok) {
        const detail = await response.text().catch(() => '');
        throw new Error(`Download failed (${response.status}) for ${url}: ${detail || 'No details'}`);
      }

      if (!response.body) {
        throw new Error(`Empty response body while downloading ${url}`);
      }

      await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(destination));
      return;
    } catch (error) {
      lastError = error;
      if (attempt < 3) {
        console.warn(`[update-manifest] retrying download (${attempt}/3) for ${url}: ${error.message}`);
      }
    }
  }

  if (lastError) {
    throw lastError;
  }

  throw new Error(`Download failed for ${url}`);
}

async function fetchJson(url, headers = {}) {
  const response = await fetch(url, {
    headers: {
      'User-Agent': 'homebrew-update-manager',
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      ...headers,
    },
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`Request failed (${response.status}) for ${url}: ${detail || 'No details'}`);
  }

  return response.json();
}

function buildApiHeaders() {
  if (!process.env.GITHUB_TOKEN) {
    return {};
  }
  return {
    Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
  };
}

async function loadPreviousReleaseBundle(currentVersion) {
  const apiHeaders = buildApiHeaders();
  const releases = await fetchJson(`https://api.github.com/repos/${REPO}/releases?per_page=8`, apiHeaders);
  const previousRelease = (Array.isArray(releases) ? releases : []).find((release) => {
    const version = normalizeVersion(release?.tag_name || release?.name || '');
    return !!version && version !== currentVersion;
  });

  if (!previousRelease) {
    return null;
  }

  let manifest = null;

  const manifestAsset = (Array.isArray(previousRelease.assets) ? previousRelease.assets : [])
    .find((asset) => String(asset?.name || '') === MANIFEST_NAME);

  if (manifestAsset?.browser_download_url) {
    manifest = await fetchJson(manifestAsset.browser_download_url, {});
  }

  return {
    release: previousRelease,
    manifest,
  };
}

function findReleaseDmgAssetByArch(assets = [], arch) {
  const list = (Array.isArray(assets) ? assets : []).filter((asset) => /\.dmg$/i.test(String(asset?.name || '')));
  if (!list.length) return null;

  const archPattern = new RegExp(`[-_. ]${arch}\\.dmg$`, 'i');
  return list.find((asset) => archPattern.test(String(asset?.name || '')))
    || list.find((asset) => String(asset?.name || '').toLowerCase().includes(String(arch || '').toLowerCase()))
    || null;
}

function findCurrentDmgByArch(version) {
  const entries = fs.readdirSync(DIST_DIR, { withFileTypes: true });
  const byArch = {};

  for (const entry of entries) {
    if (!entry.isFile()) continue;
    if (!entry.name.endsWith('.dmg')) continue;
    if (!entry.name.includes(`-${version}-`)) continue;

    const match = entry.name.match(/-(x64|arm64)\.dmg$/i);
    if (!match) continue;

    byArch[match[1].toLowerCase()] = {
      name: entry.name,
      path: path.join(DIST_DIR, entry.name),
    };
  }

  return byArch;
}

function findFirstAppBundle(dirPath) {
  const stack = [dirPath];
  while (stack.length) {
    const current = stack.pop();
    const entries = fs.readdirSync(current, { withFileTypes: true });
    for (const entry of entries) {
      const child = path.join(current, entry.name);
      if (entry.isDirectory() && entry.name.endsWith('.app')) {
        return child;
      }
      if (entry.isDirectory()) {
        stack.push(child);
      }
    }
  }
  return null;
}

function findAbsoluteSymlinkEntries(rootPath) {
  const absoluteLinks = [];
  const stack = [rootPath];

  while (stack.length) {
    const current = stack.pop();
    const entries = fs.readdirSync(current, { withFileTypes: true });

    for (const entry of entries) {
      const child = path.join(current, entry.name);
      const stats = fs.lstatSync(child);

      if (stats.isSymbolicLink()) {
        const linkTarget = fs.readlinkSync(child);
        if (path.isAbsolute(linkTarget)) {
          absoluteLinks.push({
            relativePath: path.relative(rootPath, child),
            target: linkTarget,
          });
        }
        continue;
      }

      if (stats.isDirectory()) {
        stack.push(child);
      }
    }
  }

  return absoluteLinks;
}

function assertNoAbsoluteSymlinks(rootPath, scopeLabel) {
  const absoluteLinks = findAbsoluteSymlinkEntries(rootPath);
  if (!absoluteLinks.length) {
    return;
  }

  const preview = absoluteLinks
    .slice(0, 5)
    .map((item) => `${item.relativePath} -> ${item.target}`)
    .join('; ');

  throw new Error(
    `${scopeLabel} contains absolute symlinks (${absoluteLinks.length}). `
    + `This breaks portable update bundles. Examples: ${preview}`,
  );
}

function copyAppFromDmg(dmgPath, workRoot, arch) {
  const mountPoint = fs.mkdtempSync(path.join(os.tmpdir(), `bum-mount-${arch}-`));
  let attached = false;
  try {
    runOrThrow('hdiutil', ['attach', dmgPath, '-nobrowse', '-readonly', '-mountpoint', mountPoint, '-quiet']);
    attached = true;

    const appPath = findFirstAppBundle(mountPoint);
    if (!appPath) {
      throw new Error(`No .app bundle found inside ${path.basename(dmgPath)}`);
    }

    const archWorkRoot = path.join(workRoot, `current-${arch}`);
    ensureDir(archWorkRoot);
    const target = path.join(archWorkRoot, path.basename(appPath));
    fs.rmSync(target, { recursive: true, force: true });
    fs.cpSync(appPath, target, APP_COPY_OPTIONS);
    assertNoAbsoluteSymlinks(target, `Copied app from DMG (${arch})`);
    return target;
  } finally {
    if (attached) {
      safeDetach(mountPoint);
    }
    fs.rmSync(mountPoint, { recursive: true, force: true });
  }
}

function copyAppBundle(sourceAppPath, workRoot, scopeLabel) {
  if (!fs.existsSync(sourceAppPath)) {
    throw new Error(`App bundle path does not exist: ${sourceAppPath}`);
  }

  const scopedRoot = path.join(workRoot, scopeLabel);
  ensureDir(scopedRoot);

  const target = path.join(scopedRoot, path.basename(sourceAppPath));
  fs.rmSync(target, { recursive: true, force: true });
  fs.cpSync(sourceAppPath, target, APP_COPY_OPTIONS);
  assertNoAbsoluteSymlinks(target, `Copied app bundle (${scopeLabel})`);
  return target;
}

function findCurrentBuildAppByArch(arch) {
  const candidateDirs = arch === 'arm64'
    ? ['mac-arm64', 'mac']
    : ['mac', 'mac-arm64'];

  for (const dirName of candidateDirs) {
    const fullDir = path.join(DIST_DIR, dirName);
    if (!fs.existsSync(fullDir) || !fs.lstatSync(fullDir).isDirectory()) {
      continue;
    }

    const appPath = findFirstAppBundle(fullDir);
    if (appPath) {
      return appPath;
    }
  }

  return null;
}

function createTarGz(sourcePath, destinationPath) {
  runOrThrow('tar', ['-czf', destinationPath, '-C', path.dirname(sourcePath), path.basename(sourcePath)]);
}

function extractTarGz(archivePath, outputDir) {
  ensureDir(outputDir);
  runOrThrow('tar', ['-xzf', archivePath, '-C', outputDir]);
}

async function hashFileForCompare(filePath) {
  return sha256OfFile(filePath);
}

async function collectTreeEntries(rootPath, rel = '', map = new Map()) {
  const abs = rel ? path.join(rootPath, rel) : rootPath;
  const stats = fs.lstatSync(abs);

  if (rel) {
    if (stats.isSymbolicLink()) {
      map.set(rel, { type: 'symlink', link: fs.readlinkSync(abs) });
      return map;
    }
    if (stats.isFile()) {
      map.set(rel, { type: 'file', size: stats.size, hash: await hashFileForCompare(abs) });
      return map;
    }
    if (stats.isDirectory()) {
      map.set(rel, { type: 'dir' });
    }
  }

  if (stats.isDirectory()) {
    const entries = fs.readdirSync(abs, { withFileTypes: true })
      .map((entry) => entry.name)
      .sort((a, b) => a.localeCompare(b));

    for (const name of entries) {
      const childRel = rel ? path.join(rel, name) : name;
      // eslint-disable-next-line no-await-in-loop
      await collectTreeEntries(rootPath, childRel, map);
    }
  }

  return map;
}

function describeEntry(entry) {
  if (!entry) return 'missing';
  if (entry.type === 'file') return `file(hash=${entry.hash})`;
  if (entry.type === 'symlink') return `symlink(target=${entry.link})`;
  return entry.type;
}

function toPosixPath(value) {
  return String(value || '').replace(/\\/g, '/');
}

function resolveSymlinkEndpoint(entriesMap, relPath, linkValue, maxDepth = 32) {
  const relPosix = toPosixPath(relPath);
  const linkPosix = toPosixPath(linkValue);

  if (!linkPosix) {
    return relPosix;
  }

  if (path.posix.isAbsolute(linkPosix)) {
    return `abs:${path.posix.normalize(linkPosix)}`;
  }

  let current = path.posix.normalize(path.posix.join(path.posix.dirname(relPosix), linkPosix));
  const visited = new Set([relPosix]);

  for (let depth = 0; depth < maxDepth; depth += 1) {
    if (visited.has(current)) {
      return `cycle:${current}`;
    }
    visited.add(current);

    const targetEntry = entriesMap.get(current);
    if (!targetEntry || targetEntry.type !== 'symlink') {
      return current;
    }

    const targetLink = toPosixPath(targetEntry.link || '');
    if (!targetLink) {
      return current;
    }

    if (path.posix.isAbsolute(targetLink)) {
      return `abs:${path.posix.normalize(targetLink)}`;
    }

    current = path.posix.normalize(path.posix.join(path.posix.dirname(current), targetLink));
  }

  return `depth:${current}`;
}

function symlinkEntriesEquivalent(relPath, expectedEntry, actualEntry, expectedMap, actualMap) {
  const expectedLink = toPosixPath(expectedEntry.link || '');
  const actualLink = toPosixPath(actualEntry.link || '');
  if (expectedLink === actualLink) {
    return true;
  }

  const expectedEndpoint = resolveSymlinkEndpoint(expectedMap, relPath, expectedLink);
  const actualEndpoint = resolveSymlinkEndpoint(actualMap, relPath, actualLink);
  if (expectedEndpoint === actualEndpoint) {
    return true;
  }

  const expectedTarget = expectedMap.get(expectedEndpoint);
  const actualTarget = actualMap.get(actualEndpoint);
  if (!expectedTarget || !actualTarget || expectedTarget.type !== actualTarget.type) {
    return false;
  }

  if (expectedTarget.type === 'file') {
    return expectedTarget.hash === actualTarget.hash;
  }

  if (expectedTarget.type === 'dir') {
    return true;
  }

  if (expectedTarget.type === 'symlink') {
    return toPosixPath(expectedTarget.link || '') === toPosixPath(actualTarget.link || '');
  }

  return false;
}

function compareEntryMaps(expectedMap, actualMap) {
  const mismatches = [];

  for (const [relPath, expected] of expectedMap.entries()) {
    const actual = actualMap.get(relPath);
    if (!actual) {
      mismatches.push(`${relPath}: missing in applied output`);
      continue;
    }

    if (expected.type !== actual.type) {
      mismatches.push(`${relPath}: type mismatch expected=${describeEntry(expected)} actual=${describeEntry(actual)}`);
      continue;
    }

    if (expected.type === 'file' && expected.hash !== actual.hash) {
      mismatches.push(`${relPath}: file hash mismatch`);
      continue;
    }

    if (expected.type === 'symlink' && !symlinkEntriesEquivalent(relPath, expected, actual, expectedMap, actualMap)) {
      mismatches.push(`${relPath}: symlink target mismatch expected=${expected.link} actual=${actual.link}`);
    }
  }

  for (const relPath of actualMap.keys()) {
    if (!expectedMap.has(relPath)) {
      mismatches.push(`${relPath}: unexpected entry in applied output`);
    }
  }

  return mismatches;
}

async function validateDeltaRoundTrip({
  fromVersion,
  toVersion,
  arch,
  previousAppPath,
  currentAppPath,
  deltaArchivePath,
  workspace,
}) {
  const validationRoot = path.join(workspace, `validation-${arch}`);
  fs.rmSync(validationRoot, { recursive: true, force: true });
  ensureDir(validationRoot);

  const outputAppPath = path.join(validationRoot, path.basename(previousAppPath));
  applyDeltaArchive({
    sourceAppPath: previousAppPath,
    deltaArchivePath,
    outputAppPath,
    workDir: path.join(validationRoot, 'work'),
    expectedFromVersion: fromVersion,
    expectedToVersion: toVersion,
    expectedArch: arch,
  });

  const [expectedMap, actualMap] = await Promise.all([
    collectTreeEntries(currentAppPath),
    collectTreeEntries(outputAppPath),
  ]);

  const mismatches = compareEntryMaps(expectedMap, actualMap);
  if (mismatches.length > 0) {
    throw new Error(`Round-trip validation mismatch (${mismatches.length}): ${mismatches.slice(0, 5).join('; ')}`);
  }
}

function copyDeltaEntry(sourceRoot, targetRoot, relPath, type) {
  const safeRelPath = ensureSafeRelativePath(relPath, 'entry path');
  const src = path.join(sourceRoot, safeRelPath);
  const dst = path.join(targetRoot, safeRelPath);
  ensureDir(path.dirname(dst));

  if (type === 'dir') {
    ensureDir(dst);
    return;
  }

  if (type === 'symlink') {
    const linkTarget = fs.readlinkSync(src);
    fs.rmSync(dst, { recursive: true, force: true });
    fs.symlinkSync(linkTarget, dst);
    return;
  }

  fs.cpSync(src, dst, { recursive: true, force: true, dereference: false });
}

async function buildDeltaArchive({
  fromVersion,
  toVersion,
  arch,
  previousFullArchive,
  previousAppPath,
  currentAppPath,
  outputPath,
  workspace,
}) {
  let resolvedPreviousAppPath = previousAppPath;

  if (!resolvedPreviousAppPath) {
    const prevExtractDir = path.join(workspace, `prev-${arch}`);
    fs.rmSync(prevExtractDir, { recursive: true, force: true });
    ensureDir(prevExtractDir);

    extractTarGz(previousFullArchive, prevExtractDir);
    resolvedPreviousAppPath = findFirstAppBundle(prevExtractDir);
    if (!resolvedPreviousAppPath) {
      throw new Error(`Previous full archive for ${arch} does not contain an app bundle`);
    }
  }

  if (!fs.existsSync(resolvedPreviousAppPath)) {
    throw new Error(`Previous app bundle path is invalid for ${arch}`);
  }

  const prevEntries = await collectTreeEntries(resolvedPreviousAppPath);
  const currEntries = await collectTreeEntries(currentAppPath);

  const removedPaths = [];
  const changedEntries = [];

  for (const [relPath, prevInfo] of prevEntries.entries()) {
    if (!currEntries.has(relPath)) {
      removedPaths.push(relPath);
      continue;
    }

    const currInfo = currEntries.get(relPath);
    if (prevInfo.type !== currInfo.type) {
      changedEntries.push({ relPath, type: currInfo.type });
      continue;
    }

    if (currInfo.type === 'file' && prevInfo.hash !== currInfo.hash) {
      changedEntries.push({ relPath, type: currInfo.type });
      continue;
    }

    if (currInfo.type === 'symlink' && prevInfo.link !== currInfo.link) {
      changedEntries.push({ relPath, type: currInfo.type });
    }
  }

  for (const [relPath, currInfo] of currEntries.entries()) {
    if (!prevEntries.has(relPath)) {
      changedEntries.push({ relPath, type: currInfo.type });
    }
  }

  const stageDir = path.join(workspace, `delta-stage-${arch}`);
  const filesDir = path.join(stageDir, 'files');
  fs.rmSync(stageDir, { recursive: true, force: true });
  ensureDir(filesDir);

  const seen = new Set();
  for (const item of changedEntries) {
    if (seen.has(item.relPath)) continue;
    seen.add(item.relPath);
    copyDeltaEntry(currentAppPath, filesDir, item.relPath, item.type);
  }

  const normalizedRemovedPaths = removedPaths
    .map((relPath) => ensureSafeRelativePath(relPath, 'removed path'))
    .sort((a, b) => b.length - a.length);

  const meta = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    fromVersion,
    toVersion,
    arch,
    removedPaths: normalizedRemovedPaths,
    changedCount: seen.size,
  };

  fs.writeFileSync(path.join(stageDir, 'meta.json'), JSON.stringify(meta, null, 2));
  runOrThrow('tar', ['-czf', outputPath, '-C', stageDir, '.']);

  if (STRICT_DELTA_VALIDATION) {
    await validateDeltaRoundTrip({
      fromVersion,
      toVersion,
      arch,
      previousAppPath: resolvedPreviousAppPath,
      currentAppPath,
      deltaArchivePath: outputPath,
      workspace,
    });
  }

  return {
    changedCount: seen.size,
    removedCount: normalizedRemovedPaths.length,
  };
}

function assetRecord(tag, fileName, sha256, size, packageType) {
  return {
    name: fileName,
    url: releaseAssetUrl(tag, fileName),
    sha256,
    size,
    packageType,
  };
}

async function main() {
  if (!fs.existsSync(DIST_DIR)) {
    throw new Error('dist directory does not exist. Run release build first.');
  }

  const pkg = readJson(path.join(ROOT, 'package.json'));
  const version = normalizeVersion(pkg.version);
  if (!version) {
    throw new Error('Invalid package version in package.json');
  }

  const tag = process.env.UPDATE_TAG || `v${version}`;
  const minSupported = normalizeVersion(process.env.MIN_SUPPORTED_VERSION || '');

  const dmgByArch = findCurrentDmgByArch(version);
  for (const arch of ['x64', 'arm64']) {
    if (!dmgByArch[arch]) {
      throw new Error(`Missing DMG for ${arch}. Expected artifact with suffix -${arch}.dmg`);
    }
  }

  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), `bum-manifest-${version}-`));
  const releaseAssets = [];
  const currentApps = {};
  const fullByArch = {};
  const dmgRecords = {};

  console.log(`[update-manifest] Building updater assets for v${version}`);
  console.log(`[update-manifest] Strict delta validation: ${STRICT_DELTA_VALIDATION ? 'enabled' : 'disabled'}`);

  for (const arch of ['x64', 'arm64']) {
    const dmg = dmgByArch[arch];
    const buildAppPath = findCurrentBuildAppByArch(arch);
    const appPath = buildAppPath
      ? copyAppBundle(buildAppPath, tempRoot, `current-${arch}-build`)
      : copyAppFromDmg(dmg.path, tempRoot, arch);

    if (buildAppPath) {
      console.log(`[update-manifest] ${arch}: using build app bundle ${path.relative(ROOT, buildAppPath)}`);
    }

    currentApps[arch] = appPath;

    const fullName = canonicalAssetName(dmg.name.replace(/\.dmg$/i, '-full.tar.gz'));
    const fullPath = path.join(DIST_DIR, fullName);
    createTarGz(appPath, fullPath);

    const [fullSha, dmgSha] = await Promise.all([
      sha256OfFile(fullPath),
      sha256OfFile(dmg.path),
    ]);

    const fullSize = fs.statSync(fullPath).size;
    const dmgSize = fs.statSync(dmg.path).size;

    const fullRecord = assetRecord(tag, fullName, fullSha, fullSize, 'tar.gz');
    const dmgRecord = assetRecord(tag, canonicalAssetName(dmg.name), dmgSha, dmgSize, 'dmg');

    fullByArch[arch] = {
      ...fullRecord,
      path: fullPath,
    };
    dmgRecords[arch] = dmgRecord;

    releaseAssets.push(fullRecord, dmgRecord);
    console.log(`[update-manifest] ${arch}: full package ${fullName}`);
  }

  let previousBundle = null;
  try {
    previousBundle = await loadPreviousReleaseBundle(version);
    if (previousBundle?.manifest) {
      console.log(`[update-manifest] Found previous manifest from ${previousBundle.release.tag_name}`);
    } else if (previousBundle?.release) {
      console.log(`[update-manifest] Previous release found without manifest: ${previousBundle.release.tag_name}`);
    }
  } catch (error) {
    console.warn(`[update-manifest] Could not load previous manifest: ${error.message}`);
    previousBundle = null;
  }

  const previousRelease = previousBundle?.release || null;
  const previousManifest = previousBundle?.manifest || null;
  const previousLatest = normalizeVersion(
    previousManifest?.latestVersion
      || previousRelease?.tag_name
      || previousRelease?.name
      || '',
  );
  const previousVersions = previousManifest && typeof previousManifest.versions === 'object'
    ? previousManifest.versions
    : {};

  const deltaByArch = { x64: [], arm64: [] };

  if (previousLatest && previousLatest !== version) {
    const deltaValidationErrors = [];

    for (const arch of ['x64', 'arm64']) {
      let sourceType = '';
      let sourceUrl = '';
      let sourceSha256 = '';

      const prevFull = previousVersions?.[previousLatest]?.assets?.[arch]?.full;
      if (prevFull?.url) {
        sourceType = 'manifest-full';
        sourceUrl = String(prevFull.url);
        sourceSha256 = String(prevFull.sha256 || '');
      } else {
        const prevDmgAsset = findReleaseDmgAssetByArch(previousRelease?.assets, arch);
        if (prevDmgAsset?.browser_download_url) {
          sourceType = 'legacy-dmg';
          sourceUrl = String(prevDmgAsset.browser_download_url);
          sourceSha256 = '';
        }
      }

      if (!sourceType || !sourceUrl) {
        console.log(`[update-manifest] ${arch}: no usable previous asset source, skipping delta`);
        if (STRICT_DELTA_VALIDATION) {
          deltaValidationErrors.push(`${arch}: no usable previous asset source`);
        }
        continue;
      }

      try {
        const deltaName = canonicalAssetName(`${pkg.build?.productName || 'Homebrew Update Manager'}-${previousLatest}-to-${version}-${arch}-delta.tar.gz`);
        const deltaPath = path.join(DIST_DIR, deltaName);

        let deltaInfo = null;

        if (sourceType === 'manifest-full') {
          const prevArchivePath = path.join(tempRoot, `prev-${previousLatest}-${arch}-full.tar.gz`);
          await downloadFile(sourceUrl, prevArchivePath);

          if (sourceSha256) {
            const actual = await sha256OfFile(prevArchivePath);
            if (String(actual).toLowerCase() !== String(sourceSha256).toLowerCase()) {
              throw new Error(`Previous full archive checksum mismatch (${actual})`);
            }
          }

          deltaInfo = await buildDeltaArchive({
            fromVersion: previousLatest,
            toVersion: version,
            arch,
            previousFullArchive: prevArchivePath,
            currentAppPath: currentApps[arch],
            outputPath: deltaPath,
            workspace: path.join(tempRoot, `delta-work-${arch}`),
          });
        } else {
          const prevDmgPath = path.join(tempRoot, `prev-${previousLatest}-${arch}.dmg`);
          await downloadFile(sourceUrl, prevDmgPath);

          const legacyWorkRoot = path.join(tempRoot, `legacy-prev-${arch}`);
          ensureDir(legacyWorkRoot);
          const prevAppPath = copyAppFromDmg(prevDmgPath, legacyWorkRoot, arch);

          deltaInfo = await buildDeltaArchive({
            fromVersion: previousLatest,
            toVersion: version,
            arch,
            previousAppPath: prevAppPath,
            currentAppPath: currentApps[arch],
            outputPath: deltaPath,
            workspace: path.join(tempRoot, `delta-work-${arch}`),
          });
        }

        const deltaSha = await sha256OfFile(deltaPath);
        const deltaSize = fs.statSync(deltaPath).size;

        const deltaRecord = {
          fromVersion: previousLatest,
          toVersion: version,
          ...assetRecord(tag, deltaName, deltaSha, deltaSize, 'tar.gz'),
        };

        deltaByArch[arch].push(deltaRecord);
        releaseAssets.push(deltaRecord);

        console.log(`[update-manifest] ${arch}: delta ${deltaName} (${deltaInfo.changedCount} changed, ${deltaInfo.removedCount} removed, source=${sourceType})`);
      } catch (error) {
        console.warn(`[update-manifest] ${arch}: delta generation skipped: ${error.message}`);
        if (STRICT_DELTA_VALIDATION) {
          deltaValidationErrors.push(`${arch}: ${error.message}`);
        }
      }
    }

    if (STRICT_DELTA_VALIDATION && deltaValidationErrors.length > 0) {
      throw new Error(`Strict delta validation failed for ${previousLatest} -> ${version}: ${deltaValidationErrors.join(' | ')}`);
    }
  } else {
    console.log('[update-manifest] No previous version found. Delta assets skipped for this release.');
  }

  const mergedVersions = {
    ...(previousVersions || {}),
  };

  mergedVersions[version] = {
    releasedAt: new Date().toISOString(),
    assets: {
      x64: {
        full: assetRecord(tag, fullByArch.x64.name, fullByArch.x64.sha256, fullByArch.x64.size, 'tar.gz'),
        dmg: dmgRecords.x64,
        deltas: deltaByArch.x64,
      },
      arm64: {
        full: assetRecord(tag, fullByArch.arm64.name, fullByArch.arm64.sha256, fullByArch.arm64.size, 'tar.gz'),
        dmg: dmgRecords.arm64,
        deltas: deltaByArch.arm64,
      },
    },
  };

  const manifest = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    latestVersion: version,
    minimumSupportedVersion: minSupported || normalizeVersion(previousManifest?.minimumSupportedVersion || '') || '1.0.0',
    versions: mergedVersions,
  };

  const manifestPath = path.join(DIST_DIR, MANIFEST_NAME);
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  releaseAssets.push({
    name: MANIFEST_NAME,
    sha256: await sha256OfFile(manifestPath),
    size: fs.statSync(manifestPath).size,
    packageType: 'json',
    url: releaseAssetUrl(tag, MANIFEST_NAME),
  });

  const checksumsLines = releaseAssets
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((asset) => `${asset.sha256}  ${asset.name}`);
  fs.writeFileSync(path.join(DIST_DIR, CHECKSUMS_NAME), `${checksumsLines.join('\n')}\n`);

  console.log(`[update-manifest] Wrote ${MANIFEST_NAME}`);
  console.log(`[update-manifest] Wrote ${CHECKSUMS_NAME}`);

  fs.rmSync(tempRoot, { recursive: true, force: true });
}

main().catch((error) => {
  console.error(`[update-manifest] ERROR: ${error.message}`);
  process.exit(1);
});
