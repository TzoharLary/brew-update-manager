#!/usr/bin/env node

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const arch = process.arch === 'arm64' ? 'arm64' : 'x64';
const command = process.platform === 'win32' ? 'npx.cmd' : 'npx';
const args = ['electron-builder', '--publish', 'never', '--mac', 'dmg', `--${arch}`];
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

console.log(`[quick-build] Building DMG for current arch only: ${arch}`);
const res = spawnSync(command, args, { stdio: 'inherit' });

if (res.status !== 0) {
  process.exit(res.status || 1);
}
