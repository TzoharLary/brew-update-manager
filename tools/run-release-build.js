#!/usr/bin/env node

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const command = process.platform === 'win32' ? 'npx.cmd' : 'npx';
const args = ['electron-builder', '--publish', 'never', '--mac', 'dmg', '--x64', '--arm64'];
const distPath = path.join(__dirname, '..', 'dist');

if (fs.existsSync(distPath)) {
  fs.rmSync(distPath, { recursive: true, force: true });
}
fs.mkdirSync(distPath, { recursive: true });

console.log('[release-build] Building DMG for x64 + arm64');
const res = spawnSync(command, args, { stdio: 'inherit' });

if (res.status !== 0) {
  process.exit(res.status || 1);
}
