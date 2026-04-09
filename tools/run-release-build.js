#!/usr/bin/env node

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const command = process.platform === 'win32' ? 'npx.cmd' : 'npx';
const args = ['electron-builder', '--publish', 'never', '--mac', 'dmg', '--x64', '--arm64'];
const distPath = path.join(__dirname, '..', 'dist');

function cleanDist(target) {
  if (!fs.existsSync(target)) return;

  for (const name of fs.readdirSync(target)) {
    fs.rmSync(path.join(target, name), {
      recursive: true,
      force: true,
      maxRetries: 8,
      retryDelay: 120,
    });
  }

  fs.rmSync(target, {
    recursive: true,
    force: true,
    maxRetries: 8,
    retryDelay: 120,
  });
}

if (fs.existsSync(distPath)) {
  cleanDist(distPath);
}
fs.mkdirSync(distPath, { recursive: true });

console.log('[release-build] Building DMG for x64 + arm64');
const res = spawnSync(command, args, { stdio: 'inherit' });

if (res.status !== 0) {
  process.exit(res.status || 1);
}

console.log('[release-build] Building update manifest + full/delta assets');
const manifestRes = spawnSync(process.execPath, [path.join(__dirname, 'build-update-manifest.js')], {
  stdio: 'inherit',
  env: {
    ...process.env,
  },
});

if (manifestRes.status !== 0) {
  process.exit(manifestRes.status || 1);
}
