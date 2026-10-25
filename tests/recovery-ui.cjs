'use strict';
// Exercise the real recovery handlers with a minimal DOM and controlled IPC.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../renderer/mac-manager.js'), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.disabled = false; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  setAttribute() {}
}
(async () => {
  for (const lang of ['he', 'en']) {
    for (const scenario of ['resume', 'rollback', 'conflict', 'lost-response']) {
      const panel = new Element('details');
      const messages = [], finished = [];
      let calls = 0, reviews = 0, reloads = 0;
      const context = {
        Date, i18n: {lang}, busyLoading: false, lastKnownOperationRunning: false,
        operationRequestGeneration: 0,
        snapshot: {pending_recovery: [{transaction_id: 'a'.repeat(32), logical_id: 'jq', stage: 'cleanup'}]},
        document: {getElementById: () => panel, createElement: tag => new Element(tag)},
        setLoading: busy => { context.busyLoading = busy; },
        beginOperationRequest: () => { context.operationRequestGeneration++; },
        startProgressPolling: () => {},
        finishOperationRequest: async result => { finished.push(result); },
        showMessage: (...args) => messages.push(args),
        loadState: async () => {
          reloads++;
          if (scenario !== 'conflict') context.snapshot.pending_recovery = [];
          context.renderMacManager();
        },
        window: {brewApp: {
          migrationRecoveryPlan: async (id, action) => {
            reviews++; assert.equal(id, 'a'.repeat(32));
            return {plan: {id: 'b'.repeat(32), preview: {action}}};
          },
          migrationApprove: async id => {
            calls++; assert.equal(id, 'b'.repeat(32));
            await Promise.resolve();
            if (scenario === 'conflict') throw Error('recovery_state_changed');
            if (scenario === 'lost-response') throw Error('connection lost');
            return {ok: true, migration: {status: scenario === 'rollback' ? 'rolled_back' : 'completed'}};
          }
        }}
      };
      vm.createContext(context);
      vm.runInContext(source, context);
      context.renderMacManager();
      const item = panel.children.find(child => child.tag === 'div');
      const reviewButtons = item.children.filter(child => child.tag === 'button');
      assert.equal(reviewButtons.length, 2);
      assert.equal(calls, 0, 'review must not start a mutation');
      const button = reviewButtons[scenario === 'resume' ? 0 : 1];
      assert.ok(button.textContent.includes(lang === 'he' ? 'סקירת' : 'Review'));
      await button.onclick();
      const approve = item.children.filter(child => child.tag === 'button').at(-1);
      await Promise.all([approve.onclick(), approve.onclick()]);
      assert.equal(calls, 1, `${lang}/${scenario}: no double submit or automatic retry`);
      assert.equal(reviews, 1);
      assert.equal(reloads, 1, 'refresh after success, conflict or lost response');
      assert.equal(context.busyLoading, false);
      assert.equal(finished.length, 1);
      if (scenario === 'conflict' || scenario === 'lost-response') {
        assert.equal(finished[0], null);
        assert.equal(messages.length, 1);
      } else {
        assert.equal(finished[0].migration.status, scenario === 'rollback' ? 'rolled_back' : 'completed');
      }
      if (scenario !== 'conflict') assert.equal(context.snapshot.pending_recovery.length, 0);
    }
  }
  console.log('Recovery UI: 8 Hebrew/English scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
