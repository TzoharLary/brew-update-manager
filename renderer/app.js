const i18n = window.brewI18n;

let snapshot = null;
let activeTab = 'outdated';
const TABLE_COLUMN_COUNT = 9;
const COLUMN_WIDTH_STORAGE_KEY = 'bum_column_widths_v1';

const cardsEl = document.getElementById('cards');
const tbodyEl = document.getElementById('tbody');
const tableTitleEl = document.getElementById('tableTitle');
const updatedAtEl = document.getElementById('updatedAt');
const msgEl = document.getElementById('message');
const loaderEl = document.getElementById('loader');
const loaderTextEl = document.getElementById('loaderText');
const loaderPercentEl = document.getElementById('loaderPercent');
const loaderProgressEl = document.getElementById('loaderProgress');
const loaderProgressMetaEl = document.getElementById('loaderProgressMeta');
const loaderProgressFillEl = document.getElementById('loaderProgressFill');
const loaderPhaseValueEl = document.getElementById('loaderPhaseValue');
const loaderCurrentValueEl = document.getElementById('loaderCurrentValue');
const loaderEtaValueEl = document.getElementById('loaderEtaValue');
const loaderStepEls = Array.from(document.querySelectorAll('#loaderSteps [data-step]'));
const checkNowBtn = document.getElementById('checkNowBtn');
const updateAllBtn = document.getElementById('updateAllBtn');
const languageSelect = document.getElementById('languageSelect');
const checkAppUpdateBtn = document.getElementById('checkAppUpdateBtn');
const installAppUpdateBtn = document.getElementById('installAppUpdateBtn');
const appUpdateStatusEl = document.getElementById('appUpdateStatus');
const settingsSummaryEl = document.getElementById('settingsSummary');
const scheduleEnabledEl = document.getElementById('scheduleEnabled');
const scheduleFrequencyEl = document.getElementById('scheduleFrequency');
const scheduleTimeRowEl = document.getElementById('scheduleTimeRow');
const scheduleTimeEl = document.getElementById('scheduleTime');
const scheduleWeekdayRowEl = document.getElementById('scheduleWeekdayRow');
const scheduleWeekdayEl = document.getElementById('scheduleWeekday');
const scheduleIntervalRowEl = document.getElementById('scheduleIntervalRow');
const scheduleIntervalHoursEl = document.getElementById('scheduleIntervalHours');
const saveScheduleBtn = document.getElementById('saveScheduleBtn');
const scheduleStatusEl = document.getElementById('scheduleStatus');
const brewPathInputEl = document.getElementById('brewPathInput');
const detectBrewPathBtn = document.getElementById('detectBrewPathBtn');
const saveBrewPathBtn = document.getElementById('saveBrewPathBtn');
const clearBrewPathBtn = document.getElementById('clearBrewPathBtn');
const brewPathStatusEl = document.getElementById('brewPathStatus');

let progressPollTimer = null;
let progressPollInFlight = false;
let settingsState = null;
let busyLoading = false;
let appUpdateState = null;
let appUpdateAutoCheckTimer = null;

const APP_UPDATE_AUTO_CHECK_INTERVAL_MS = 60 * 60 * 1000;
const LOADER_PHASE_ORDER = [
  'idle',
  'starting',
  'preparing',
  'brew_update',
  'collecting_outdated',
  'collecting_installed',
  'resolving_dates',
  'translating_descriptions',
  'completed',
  'error',
];

function renderAppUpdateStatus() {
  if (!appUpdateState) {
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.ready'));
    installAppUpdateBtn.disabled = true;
    return;
  }

  if (appUpdateState.updateAvailable) {
    if (appUpdateState.hasInstallAsset) {
      setInlineStatus(appUpdateStatusEl, i18n.t('updates.available', {
        latest: appUpdateState.latestVersion,
        current: appUpdateState.currentVersion,
      }));
      installAppUpdateBtn.disabled = false;
    } else {
      setInlineStatus(appUpdateStatusEl, i18n.t('updates.noInstallAsset'), true);
      installAppUpdateBtn.disabled = true;
    }
  } else {
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.upToDate', {
      current: appUpdateState.currentVersion,
    }));
    installAppUpdateBtn.disabled = true;
  }
}

async function checkAppUpdate({ silent = false } = {}) {
  checkAppUpdateBtn.disabled = true;
  if (!silent) {
    installAppUpdateBtn.disabled = true;
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.checking'));
  }

  try {
    const payload = await window.brewApp.checkAppUpdate();
    appUpdateState = payload;
    renderAppUpdateStatus();
  } catch (err) {
    if (!silent) {
      appUpdateState = null;
      setInlineStatus(appUpdateStatusEl, i18n.t('updates.failed', { error: err.message }), true);
    }
  } finally {
    checkAppUpdateBtn.disabled = false;
  }
}

function startAutoAppUpdateChecks() {
  if (appUpdateAutoCheckTimer) {
    clearInterval(appUpdateAutoCheckTimer);
  }
  appUpdateAutoCheckTimer = setInterval(() => {
    checkAppUpdate({ silent: true });
  }, APP_UPDATE_AUTO_CHECK_INTERVAL_MS);
}

function stopAutoAppUpdateChecks() {
  if (!appUpdateAutoCheckTimer) return;
  clearInterval(appUpdateAutoCheckTimer);
  appUpdateAutoCheckTimer = null;
}

async function installAppUpdate() {
  if (!appUpdateState?.updateAvailable || !appUpdateState?.hasInstallAsset) {
    return;
  }

  if (!confirm(i18n.t('updates.confirmInstall', { latest: appUpdateState.latestVersion }))) {
    return;
  }

  checkAppUpdateBtn.disabled = true;
  installAppUpdateBtn.disabled = true;
  setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloading'));

  try {
    const payload = await window.brewApp.downloadAndInstallAppUpdate();
    appUpdateState = payload;
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloaded', { path: payload.downloadedPath }));
    installAppUpdateBtn.disabled = false;
  } catch (err) {
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloadFailed', { error: err.message }), true);
    installAppUpdateBtn.disabled = !(appUpdateState?.updateAvailable && appUpdateState?.hasInstallAsset);
  } finally {
    checkAppUpdateBtn.disabled = false;
  }
}

function isBrewPathConfigured() {
  return !!String(settingsState?.settings?.brew_path || '').trim();
}

function applyActionAvailability() {
  const allowed = isBrewPathConfigured();
  checkNowBtn.disabled = busyLoading || !allowed;
  updateAllBtn.disabled = busyLoading || !allowed;
  document.querySelectorAll('.row-update-btn').forEach((btn) => {
    btn.disabled = busyLoading || !allowed;
  });
}

function ensureBrewConfiguredForActions() {
  if (isBrewPathConfigured()) return true;
  showMessage(i18n.t('message.brewPathRequired'), true);
  return false;
}

function setLoading(on, messageKey = 'message.loadingState', params = {}) {
  busyLoading = !!on;
  loaderTextEl.textContent = i18n.t(messageKey, params);
  if (on) {
    resetLoaderProgress();
  }
  loaderEl.style.display = on ? 'flex' : 'none';
  applyActionAvailability();

  if (!on) {
    resetLoaderProgress();
  }
}

function showMessage(text, error = false) {
  msgEl.textContent = text;
  msgEl.style.display = 'block';
  msgEl.classList.toggle('error', !!error);
}

function clearMessage() {
  msgEl.style.display = 'none';
  msgEl.textContent = '';
  msgEl.classList.remove('error');
}

function setInlineStatus(el, text, isError = false) {
  if (!el) return;
  el.textContent = String(text || '').trim();
  el.style.color = isError ? 'var(--danger)' : 'var(--muted)';
}

function parseTimeValue(value) {
  const raw = String(value || '').trim();
  const [h, m] = raw.split(':');
  const hour = Number(h);
  const minute = Number(m);
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) {
    return { hour: 9, minute: 0 };
  }
  return {
    hour: Math.min(23, Math.max(0, hour)),
    minute: Math.min(59, Math.max(0, minute)),
  };
}

function schedulerPayloadFromForm() {
  const { hour, minute } = parseTimeValue(scheduleTimeEl.value);
  return {
    enabled: !!scheduleEnabledEl.checked,
    frequency: String(scheduleFrequencyEl.value || 'daily'),
    hour,
    minute,
    weekday: Number(scheduleWeekdayEl.value || 1),
    interval_hours: Number(scheduleIntervalHoursEl.value || 24),
  };
}

function schedulerSummaryText(scheduler) {
  if (!scheduler) return i18n.t('settings.loading');
  const status = scheduler.active ? i18n.t('settings.schedulerActive') : i18n.t('settings.schedulerInactive');
  const path = scheduler.launch_agent_path
    ? i18n.t('settings.launchAgentPath', { path: scheduler.launch_agent_path })
    : '';
  return [status, path].filter(Boolean).join(' • ');
}

function updateSchedulerFieldVisibility() {
  const enabled = !!scheduleEnabledEl.checked;
  const frequency = String(scheduleFrequencyEl.value || 'daily');

  scheduleFrequencyEl.disabled = !enabled;
  scheduleTimeEl.disabled = !enabled;
  scheduleWeekdayEl.disabled = !enabled;
  scheduleIntervalHoursEl.disabled = !enabled;

  scheduleTimeRowEl.classList.toggle('hidden', frequency === 'interval');
  scheduleWeekdayRowEl.classList.toggle('hidden', frequency !== 'weekly');
  scheduleIntervalRowEl.classList.toggle('hidden', frequency !== 'interval');
}

function renderSettings() {
  const scheduler = settingsState?.scheduler;
  const brewPath = String(settingsState?.settings?.brew_path || '');

  if (!scheduler) {
    settingsSummaryEl.textContent = i18n.t('settings.loading');
    setInlineStatus(scheduleStatusEl, '');
    setInlineStatus(brewPathStatusEl, '');
    return;
  }

  scheduleEnabledEl.checked = !!scheduler.enabled;
  scheduleFrequencyEl.value = String(scheduler.frequency || 'daily');
  scheduleTimeEl.value = `${String(Number(scheduler.hour ?? 9)).padStart(2, '0')}:${String(Number(scheduler.minute ?? 0)).padStart(2, '0')}`;
  scheduleWeekdayEl.value = String(Number(scheduler.weekday ?? 1));
  scheduleIntervalHoursEl.value = String(Number(scheduler.interval_hours ?? 24));

  brewPathInputEl.value = brewPath;
  settingsSummaryEl.style.color = 'var(--muted)';
  settingsSummaryEl.textContent = schedulerSummaryText(scheduler);
  updateSchedulerFieldVisibility();
  applyActionAvailability();
}

async function loadSettings() {
  try {
    const payload = await window.brewApp.getSettings();
    settingsState = payload;
    renderSettings();
  } catch (err) {
    settingsSummaryEl.textContent = i18n.t('settings.schedulerFailed', { error: err.message });
    settingsSummaryEl.style.color = 'var(--danger)';
  }
}

async function saveScheduler() {
  const scheduler = schedulerPayloadFromForm();
  setInlineStatus(scheduleStatusEl, i18n.t('settings.loading'));

  try {
    const payload = await window.brewApp.updateScheduler(scheduler);
    settingsState = {
      ...(settingsState || {}),
      scheduler: payload.scheduler,
      settings: {
        ...(settingsState?.settings || {}),
      },
    };
    renderSettings();
    setInlineStatus(scheduleStatusEl, i18n.t('settings.schedulerSaved'));
  } catch (err) {
    setInlineStatus(scheduleStatusEl, i18n.t('settings.schedulerFailed', { error: err.message }), true);
  }
}

async function saveBrewPath() {
  const raw = String(brewPathInputEl.value || '').trim();
  setInlineStatus(brewPathStatusEl, i18n.t('settings.loading'));

  try {
    const payload = await window.brewApp.updateBrewPath(raw);
    settingsState = {
      ...(settingsState || {}),
      settings: {
        ...(settingsState?.settings || {}),
        brew_path: payload.brew_path || '',
      },
      scheduler: settingsState?.scheduler,
    };
    brewPathInputEl.value = String(payload.brew_path || '');
    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaved'));
    applyActionAvailability();
    await loadState();
  } catch (err) {
    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaveFailed', { error: err.message }), true);
  }
}

async function clearBrewPath() {
  brewPathInputEl.value = '';
  await saveBrewPath();
  setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathCleared'));
  snapshot = { counts: {}, packages: [] };
  renderCards(snapshot);
  renderTable();
  updatedAtEl.textContent = '';
  showMessage(i18n.t('message.brewPathRequired'), true);
  applyActionAvailability();
}

async function detectBrewPath() {
  setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathScanning'));

  try {
    const payload = await window.brewApp.autoDetectBrewPath();
    const path = String(payload?.recommended_path || '');
    const count = i18n.formatNumber((payload?.candidates || []).length);
    const scanTime = payload?.scan_timestamp ? formatDate(payload.scan_timestamp) : i18n.t('common.unavailable');

    if (!path) {
      setInlineStatus(
        brewPathStatusEl,
        i18n.t('settings.brewPathDetectNone', { count, time: scanTime }),
        true,
      );
      return;
    }

    setInlineStatus(
      brewPathStatusEl,
      `${i18n.t('settings.brewPathDetected', { path })}\n${i18n.t('settings.brewPathScanSummary', { count, time: scanTime })}`,
    );
    if (confirm(i18n.t('settings.useDetectedConfirm', { path }))) {
      brewPathInputEl.value = path;
      await saveBrewPath();
    }
  } catch (err) {
    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaveFailed', { error: err.message }), true);
  }
}

function resetLoaderProgress() {
  loaderProgressEl.style.display = 'none';
  loaderProgressMetaEl.textContent = '';
  loaderProgressFillEl.style.width = '0%';
  if (loaderPercentEl) loaderPercentEl.textContent = '0%';
  if (loaderPhaseValueEl) loaderPhaseValueEl.textContent = i18n.t('progress.phase.idle');
  if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = i18n.t('progress.currentNone');
  if (loaderEtaValueEl) loaderEtaValueEl.textContent = i18n.t('common.dash');
  loaderStepEls.forEach((stepEl) => {
    stepEl.classList.remove('active', 'done');
  });
}

function formatEta(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  if (!Number.isFinite(total) || total <= 0) {
    return '';
  }
  if (total < 60) {
    return `${i18n.formatNumber(total)}s`;
  }
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins >= 60) {
    const hours = Math.floor(mins / 60);
    const remMins = mins % 60;
    return `${i18n.formatNumber(hours)}h ${i18n.formatNumber(remMins)}m`;
  }
  return `${i18n.formatNumber(mins)}m ${i18n.formatNumber(secs)}s`;
}

function progressPhaseLabel(phase) {
  const key = `progress.phase.${String(phase || 'idle')}`;
  const translated = i18n.t(key);
  return translated === key ? i18n.t('progress.phase.idle') : translated;
}

function renderLoaderSteps(phase) {
  const currentIndex = LOADER_PHASE_ORDER.indexOf(String(phase || 'idle'));
  loaderStepEls.forEach((stepEl) => {
    stepEl.classList.remove('active', 'done');
    const step = String(stepEl.dataset.step || '').trim();
    const stepIndex = LOADER_PHASE_ORDER.indexOf(step);
    if (stepIndex === -1 || currentIndex === -1) return;

    if (stepIndex < currentIndex) {
      stepEl.classList.add('done');
      return;
    }

    if (stepIndex === currentIndex) {
      stepEl.classList.add('active');
    }
  });
}

function renderCheckProgress(progress) {
  if (!progress || !loaderEl || loaderEl.style.display !== 'flex') {
    return;
  }

  loaderProgressEl.style.display = 'block';

  const done = Number(progress.done || 0);
  const total = Number(progress.total || 0);
  const percent = Number(progress.percent ?? (total > 0 ? Math.round((done / total) * 100) : 0));
  const clampedPercent = Math.max(0, Math.min(100, percent));
  loaderProgressFillEl.style.width = `${clampedPercent}%`;

  const phaseLabel = progressPhaseLabel(progress.phase);
  const countLabel = total > 0
    ? i18n.t('progress.summary', {
      done: i18n.formatNumber(done),
      total: i18n.formatNumber(total),
      percent: i18n.formatNumber(clampedPercent),
    })
    : i18n.t('progress.summaryUnknown');
  const etaText = typeof progress.eta_seconds === 'number' && progress.eta_seconds > 0
    ? i18n.t('progress.eta', { eta: formatEta(progress.eta_seconds) })
    : (progress.running ? i18n.t('progress.noEta') : i18n.t('progress.doneNow'));
  const current = String(progress.current_package || '').trim();

  loaderTextEl.textContent = progress.message || i18n.t('message.runningCheck');
  if (loaderPercentEl) loaderPercentEl.textContent = `${clampedPercent}%`;
  if (loaderPhaseValueEl) loaderPhaseValueEl.textContent = phaseLabel;
  if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = current || i18n.t('progress.currentNone');
  if (loaderEtaValueEl) loaderEtaValueEl.textContent = etaText;

  renderLoaderSteps(progress.phase);

  loaderProgressMetaEl.textContent = countLabel;
}

async function pollCheckProgress() {
  if (progressPollInFlight) return;
  progressPollInFlight = true;
  try {
    const payload = await window.brewApp.getProgress();
    if (payload?.progress) {
      renderCheckProgress(payload.progress);
    }
  } catch {
    // Ignore transient polling errors while check is in progress.
  } finally {
    progressPollInFlight = false;
  }
}

function startProgressPolling() {
  stopProgressPolling();
  pollCheckProgress();
  progressPollTimer = setInterval(pollCheckProgress, 750);
}

function stopProgressPolling() {
  if (progressPollTimer) {
    clearInterval(progressPollTimer);
    progressPollTimer = null;
  }
  progressPollInFlight = false;
}

function formatDate(value) {
  return i18n.formatDate(value);
}

function formatDaysOutdated(pkg) {
  if (!pkg?.outdated) {
    return i18n.t('common.dash');
  }
  if (pkg.days_since_outdated === null || pkg.days_since_outdated === undefined) {
    return i18n.t('common.unavailable');
  }
  return i18n.t('table.daysValue', { days: i18n.formatNumber(pkg.days_since_outdated) });
}

function kindLabel(kind) {
  const value = String(kind || 'formula');
  if (i18n.lang !== 'he') {
    return value;
  }
  return value === 'cask' ? 'קאסק' : 'פורמולה';
}

function packageDescription(pkg) {
  const he = String(pkg?.description_he || '').trim();
  const en = String(pkg?.description || '').trim();
  if (i18n.lang === 'he') {
    return he || en;
  }
  return en || he;
}

function renderCards(data) {
  const counts = data?.counts || {};
  const cards = [
    [i18n.t('cards.total'), counts.total ?? 0],
    [i18n.t('cards.outdated'), counts.outdated_total ?? 0],
    [i18n.t('cards.formulae'), counts.formulae ?? 0],
    [i18n.t('cards.casks'), counts.casks ?? 0],
    [i18n.t('cards.outdatedFormulae'), counts.outdated_formulae ?? 0],
    [i18n.t('cards.outdatedCasks'), counts.outdated_casks ?? 0],
  ];

  cardsEl.innerHTML = '';
  cards.forEach(([label, value]) => {
    const div = document.createElement('div');
    div.className = 'card';
    div.innerHTML = `<div class="k">${label}</div><div class="v">${i18n.formatNumber(value)}</div>`;
    cardsEl.appendChild(div);
  });
}

function getFilteredPackages() {
  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];
  if (activeTab === 'outdated') {
    return pkgs.filter((pkg) => pkg.outdated);
  }
  return pkgs;
}

function renderTable() {
  const all = Array.isArray(snapshot?.packages) ? snapshot.packages : [];
  const list = getFilteredPackages();
  const outdatedCount = all.filter((pkg) => pkg.outdated).length;

  tableTitleEl.textContent = activeTab === 'outdated'
    ? i18n.t('table.showingOutdated', { count: i18n.formatNumber(list.length) })
    : i18n.t('table.showingAll', {
      total: i18n.formatNumber(all.length),
      outdated: i18n.formatNumber(outdatedCount),
    });

  tbodyEl.innerHTML = '';
  if (!list.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = TABLE_COLUMN_COUNT;
    td.className = 'muted';
    td.textContent = activeTab === 'outdated' ? i18n.t('table.noUpdates') : i18n.t('table.noPackages');
    tr.appendChild(td);
    tbodyEl.appendChild(tr);
    return;
  }

  list.forEach((pkg) => {
    const tr = document.createElement('tr');

    const tdName = document.createElement('td');
    tdName.innerHTML = `
      <div class="name-main">${pkg.name || i18n.t('common.unknown')}</div>
      <div class="name-sub">${packageDescription(pkg)}</div>
    `;

    const tdKind = document.createElement('td');
    tdKind.innerHTML = `<span class="kind">${kindLabel(pkg.kind)}</span>`;

    const tdCurrentVersion = document.createElement('td');
    tdCurrentVersion.className = 'mono small';
    tdCurrentVersion.textContent = pkg.installed_version || i18n.t('common.unknown');

    const tdLatestVersion = document.createElement('td');
    tdLatestVersion.className = 'mono small';
    tdLatestVersion.textContent = pkg.latest_version || i18n.t('common.unknown');

    const tdCurrentDate = document.createElement('td');
    tdCurrentDate.className = 'small';
    tdCurrentDate.textContent = formatDate(pkg.current_release_date);

    const tdLatestDate = document.createElement('td');
    tdLatestDate.className = 'small';
    tdLatestDate.textContent = formatDate(pkg.latest_release_date || pkg.release_date);

    const tdDaysOutdated = document.createElement('td');
    tdDaysOutdated.className = 'small mono';
    tdDaysOutdated.textContent = formatDaysOutdated(pkg);

    const tdStatus = document.createElement('td');
    tdStatus.innerHTML = pkg.outdated
      ? `<span class="status-warn">${i18n.t('status.needsUpdate')}</span>`
      : `<span class="status-ok">${i18n.t('status.upToDate')}</span>`;

    const tdAction = document.createElement('td');
    if (pkg.outdated) {
      const btn = document.createElement('button');
      btn.className = 'row-update-btn';
      btn.textContent = i18n.t('buttons.update');
      btn.addEventListener('click', () => updateOne(pkg.name, pkg.kind));
      tdAction.appendChild(btn);
    } else {
      tdAction.innerHTML = `<span class="muted small">${i18n.t('common.dash')}</span>`;
    }

    [
      tdName,
      tdKind,
      tdCurrentVersion,
      tdLatestVersion,
      tdCurrentDate,
      tdLatestDate,
      tdDaysOutdated,
      tdStatus,
      tdAction,
    ].forEach((td) => tr.appendChild(td));

    tbodyEl.appendChild(tr);
  });
}

function refreshStaticText() {
  i18n.applyStatic();
  languageSelect.value = i18n.lang;
  document.title = i18n.t('app.title');
  if (snapshot?.updated_at) {
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });
  }
  renderCards(snapshot || { counts: {} });
  renderTable();
  renderSettings();
  renderAppUpdateStatus();
  applyActionAvailability();
}

async function loadState() {
  clearMessage();

  if (!isBrewPathConfigured()) {
    snapshot = { counts: {}, packages: [] };
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = '';
    showMessage(i18n.t('message.brewPathRequired'), true);
    applyActionAvailability();
    return;
  }

  setLoading(true, 'message.loadingState');
  try {
    snapshot = await window.brewApp.getState();
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });
  } catch (err) {
    showMessage(i18n.t('message.loadFailed', { error: err.message }), true);
  } finally {
    setLoading(false, 'message.loadingState');
  }
}

async function checkNow() {
  if (!ensureBrewConfiguredForActions()) return;
  clearMessage();
  setLoading(true, 'message.runningCheck');
  startProgressPolling();
  try {
    const res = await window.brewApp.runCheckNow();
    snapshot = res.snapshot || res;
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });
    showMessage(i18n.t('message.checkSuccess'));
  } catch (err) {
    showMessage(i18n.t('message.checkFailed', { error: err.message }), true);
  } finally {
    stopProgressPolling();
    setLoading(false, 'message.loadingState');
  }
}

async function updateOne(name, kind) {
  if (!ensureBrewConfiguredForActions()) return;
  if (!confirm(i18n.t('confirm.updateOne', { name, kind: kindLabel(kind) }))) return;

  clearMessage();
  setLoading(true, 'message.updatingOne', { name });

  try {
    const res = await window.brewApp.updateOne(name, kind);
    snapshot = res.snapshot;
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });

    if (res?.result?.ok) {
      showMessage(i18n.t('message.updateOneSuccess', { name }));
    } else {
      const detail = res?.result?.stderr || res?.result?.stdout || i18n.t('common.unknown');
      showMessage(i18n.t('message.updateOneFailed', { name, error: detail }), true);
    }
  } catch (err) {
    showMessage(i18n.t('message.updateOneRequestFailed', { error: err.message }), true);
  } finally {
    setLoading(false, 'message.loadingState');
  }
}

async function updateAll() {
  if (!ensureBrewConfiguredForActions()) return;
  if (!confirm(i18n.t('confirm.updateAll'))) return;

  clearMessage();
  setLoading(true, 'message.updatingAll');
  try {
    const res = await window.brewApp.updateAll();
    snapshot = res.snapshot;
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });
    showMessage(i18n.t('message.updateAllSuccess', {
      success: i18n.formatNumber(res.updated_count || 0),
      failed: i18n.formatNumber(res.failed_count || 0),
    }));
  } catch (err) {
    showMessage(i18n.t('message.updateAllFailed', { error: err.message }), true);
  } finally {
    setLoading(false, 'message.loadingState');
  }
}

function loadSavedColumnWidths() {
  try {
    const raw = localStorage.getItem(COLUMN_WIDTH_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function saveColumnWidths() {
  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));
  const payload = {};
  headers.forEach((th) => {
    const key = String(th.dataset.colKey || '').trim();
    if (!key) return;
    payload[key] = Math.round(th.getBoundingClientRect().width);
  });
  localStorage.setItem(COLUMN_WIDTH_STORAGE_KEY, JSON.stringify(payload));
}

function initColumnResizing() {
  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));
  const saved = loadSavedColumnWidths();

  headers.forEach((th) => {
    const key = String(th.dataset.colKey || '').trim();
    if (!key) return;
    const savedWidth = Number(saved[key]);
    if (Number.isFinite(savedWidth) && savedWidth > 40) {
      th.style.width = `${savedWidth}px`;
    }

    const handle = th.querySelector('[data-resizer]');
    if (!handle) return;

    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault();

      const minWidth = Number(th.dataset.minWidth || 80);
      const startX = event.clientX;
      const startWidth = th.getBoundingClientRect().width;
      const isRtl = document.documentElement.dir === 'rtl';
      const direction = isRtl ? -1 : 1;

      handle.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';

      const onMove = (moveEvent) => {
        const delta = (moveEvent.clientX - startX) * direction;
        const nextWidth = Math.max(minWidth, Math.round(startWidth + delta));
        th.style.width = `${nextWidth}px`;
      };

      const onUp = () => {
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        handle.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        saveColumnWidths();
      };

      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
    });
  });
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    activeTab = tab.dataset.tab;
    renderTable();
  });
});

languageSelect.addEventListener('change', () => {
  i18n.setLang(languageSelect.value);
  refreshStaticText();
});

checkNowBtn.addEventListener('click', checkNow);
updateAllBtn.addEventListener('click', updateAll);
checkAppUpdateBtn.addEventListener('click', () => checkAppUpdate({ silent: false }));
installAppUpdateBtn.addEventListener('click', installAppUpdate);
scheduleEnabledEl.addEventListener('change', updateSchedulerFieldVisibility);
scheduleFrequencyEl.addEventListener('change', updateSchedulerFieldVisibility);
saveScheduleBtn.addEventListener('click', saveScheduler);
detectBrewPathBtn.addEventListener('click', detectBrewPath);
saveBrewPathBtn.addEventListener('click', saveBrewPath);
clearBrewPathBtn.addEventListener('click', clearBrewPath);
window.addEventListener('beforeunload', stopAutoAppUpdateChecks);

initColumnResizing();
updateSchedulerFieldVisibility();
refreshStaticText();

async function initializeApp() {
  await loadSettings();
  applyActionAvailability();
  await loadState();
  renderAppUpdateStatus();
  await checkAppUpdate({ silent: true });
  startAutoAppUpdateChecks();
}

initializeApp();
