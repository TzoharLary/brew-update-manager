#!/usr/bin/env node

const { spawnSync } = require('node:child_process');

const arch = process.arch === 'arm64' ? 'arm64' : 'x64';
const command = process.platform === 'win32' ? 'npx.cmd' : 'npx';
const args = ['electron-builder', '--publish', 'never', '--mac', 'dmg', `--${arch}`];

console.log(`[quick-build] Building DMG for current arch only: ${arch}`);
const res = spawnSync(command, args, { stdio: 'inherit' });

if (res.status !== 0) {
  process.exit(res.status || 1);
}
