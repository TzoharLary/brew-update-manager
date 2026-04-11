const i18n = window.brewI18n;

let snapshot = null;
let activeTab = 'outdated';
const TABLE_COLUMN_COUNT = 10;
const COLUMN_WIDTH_STORAGE_KEY = 'bum_column_widths_v1';
const APP_UPDATE_META_STORAGE_KEY = 'bum_app_update_meta_v1';
const TABLE_SORT_STORAGE_KEY = 'bum_table_sort_v1';
const SORTABLE_COLUMN_KEYS = new Set([
  'package',
  'type',
  'currentVersion',
  'latestVersion',
  'currentDate',
  'latestDate',
  'daysOutdated',
  'status',
]);

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
const loaderCancelBtn = document.getElementById('loaderCancelBtn');
const updateLivePanelEl = document.getElementById('updateLivePanel');
const updateLiveTotalEl = document.getElementById('updateLiveTotal');
const updateLiveCompletedEl = document.getElementById('updateLiveCompleted');
const updateLiveRemainingEl = document.getElementById('updateLiveRemaining');
const updateLiveFailedEl = document.getElementById('updateLiveFailed');
const updateLiveCompletedListEl = document.getElementById('updateLiveCompletedList');
const updateLiveRemainingListEl = document.getElementById('updateLiveRemainingList');
const updateLiveFailedListEl = document.getElementById('updateLiveFailedList');
const checkNowBtn = document.getElementById('checkNowBtn');
const updateAllBtn = document.getElementById('updateAllBtn');
const updateSelectedBtn = document.getElementById('updateSelectedBtn');
const languageSelect = document.getElementById('languageSelect');
const checkAppUpdateBtn = document.getElementById('checkAppUpdateBtn');
const installAppUpdateBtn = document.getElementById('installAppUpdateBtn');
const appUpdateStatusEl = document.getElementById('appUpdateStatus');
const appUpdateProgressBoxEl = document.getElementById('appUpdateProgressBox');
const appUpdateProgressPhaseEl = document.getElementById('appUpdateProgressPhase');
const appUpdateProgressPercentEl = document.getElementById('appUpdateProgressPercent');
const appUpdateProgressFillEl = document.getElementById('appUpdateProgressFill');
const appUpdateProgressMetaEl = document.getElementById('appUpdateProgressMeta');
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
const updateHistoryListEl = document.getElementById('updateHistoryList');
const updateHistoryStatusEl = document.getElementById('updateHistoryStatus');
const packageSearchInputEl = document.getElementById('packageSearchInput');
const packageSearchClearBtnEl = document.getElementById('packageSearchClearBtn');
const packageTypeFilterEl = document.getElementById('packageTypeFilter');
const packageStatusFilterEl = document.getElementById('packageStatusFilter');
const packageFilterResetBtnEl = document.getElementById('packageFilterResetBtn');
const selectVisibleOutdatedBtn = document.getElementById('selectVisibleOutdatedBtn');
const clearSelectionBtn = document.getElementById('clearSelectionBtn');
const selectedSummaryEl = document.getElementById('selectedSummary');
const historySearchInputEl = document.getElementById('historySearchInput');
const historyKindFilterEl = document.getElementById('historyKindFilter');
const historyStatusFilterEl = document.getElementById('historyStatusFilter');
const historySortSelectEl = document.getElementById('historySortSelect');
const historyFiltersClearBtnEl = document.getElementById('historyFiltersClearBtn');

let progressPollTimer = null;
let progressPollInFlight = false;
let settingsState = null;
let busyLoading = false;
let appUpdateState = null;
let appUpdateAutoCheckTimer = null;
let updateHistoryItems = [];
let historyLoadInFlight = false;
let historyLoadError = '';
let appUpdateMeta = loadAppUpdateMeta();
let packageSearchTerm = '';
let packageTypeFilter = 'all';
let packageStatusFilter = 'all';
let historySearchTerm = '';
let historyKindFilter = 'all';
let historyStatusFilter = 'all';
let historySortBy = 'newest';
let appUpdateProgressState = null;
let appUpdateProgressEnabled = false;
let disposeAppUpdateProgress = null;
let tableSortState = loadSavedTableSort();
const selectedPackageKeys = new Set();
let cancelInFlight = false;

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
  'canceled',
  'completed',
  'error',
];

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = bytes;
  let idx = 0;
  while (size >= 1024 && idx < units.length - 1) {
    size /= 1024;
    idx += 1;
  }
  const digits = size >= 100 || idx === 0 ? 0 : 1;
  return `${i18n.formatNumber(Number(size.toFixed(digits)))} ${units[idx]}`;
}

function appUpdatePhaseLabel(phase) {
  const map = {
    check_metadata: i18n.t('updates.phaseCheckMetadata'),
    check_done: i18n.t('updates.phaseCheckDone'),
    download_delta: i18n.t('updates.phaseDownloadDelta'),
    verify_delta: i18n.t('updates.phaseVerifyDelta'),
    apply_delta: i18n.t('updates.phaseApplyDelta'),
    fallback_full: i18n.t('updates.phaseFallbackFull'),
    download_full: i18n.t('updates.phaseDownloadFull'),
    verify_full: i18n.t('updates.phaseVerifyFull'),
    schedule_restart: i18n.t('updates.phaseScheduleRestart'),
    error: i18n.t('updates.phaseError'),
  };
  return map[String(phase || '')] || i18n.t('updates.progressUnknown');
}

function clearAppUpdateProgress() {
  appUpdateProgressState = null;
  if (appUpdateProgressBoxEl) appUpdateProgressBoxEl.classList.remove('visible');
  if (appUpdateProgressPercentEl) appUpdateProgressPercentEl.textContent = '0%';
  if (appUpdateProgressFillEl) appUpdateProgressFillEl.style.width = '0%';
  if (appUpdateProgressPhaseEl) appUpdateProgressPhaseEl.textContent = i18n.t('updates.progressUnknown');
  if (appUpdateProgressMetaEl) appUpdateProgressMetaEl.textContent = '';
}

function renderAppUpdateProgress() {
  if (!appUpdateProgressBoxEl) return;
  if (!appUpdateProgressEnabled || !appUpdateProgressState) {
    appUpdateProgressBoxEl.classList.remove('visible');
    return;
  }

  appUpdateProgressBoxEl.classList.add('visible');
  const rawPercent = Number(appUpdateProgressState.percent || 0);
  const percent = Math.max(0, Math.min(100, Math.round(rawPercent)));
  const phase = String(appUpdateProgressState.phase || '');

  if (appUpdateProgressPercentEl) appUpdateProgressPercentEl.textContent = `${percent}%`;
  if (appUpdateProgressFillEl) appUpdateProgressFillEl.style.width = `${percent}%`;
  if (appUpdateProgressPhaseEl) appUpdateProgressPhaseEl.textContent = appUpdatePhaseLabel(phase);

  const meta = [];
  const downloadedBytes = Number(appUpdateProgressState.downloadedBytes ?? appUpdateProgressState.transferred ?? 0);
  const totalBytes = Number(appUpdateProgressState.totalBytes ?? appUpdateProgressState.total ?? 0);
  if (totalBytes > 0) {
    meta.push(`${formatBytes(downloadedBytes)} / ${formatBytes(totalBytes)}`);
  }

  const speedBps = Number(appUpdateProgressState.speedBps || 0);
  if (speedBps > 0) {
    meta.push(i18n.t('updates.progressSpeed', { value: formatBytes(speedBps) }));
  }

  const etaSeconds = Number(appUpdateProgressState.etaSeconds || 0);
  if (etaSeconds > 0) {
    meta.push(i18n.t('updates.progressEta', { value: formatEta(Math.round(etaSeconds)) }));
  } else if (phase === 'download_delta' || phase === 'download_full') {
    meta.push(i18n.t('updates.progressNoEta'));
  }

  const mode = String(appUpdateProgressState.mode || appUpdateProgressState.modeUsed || '');
  if (mode === 'delta') {
    meta.push(i18n.t('updates.modeDelta'));
  } else if (mode === 'full') {
    meta.push(i18n.t('updates.modeFull'));
  }

  if (appUpdateProgressMetaEl) {
    appUpdateProgressMetaEl.textContent = meta.join(' • ');
  }
}

function shouldAllowInstall(state) {
  if (!state?.updateAvailable) return false;
  return !!(state.canInstall ?? state.hasInstallAsset);
}

function handleAppUpdateProgress(payload) {
  if (!payload || typeof payload !== 'object') return;
  if (!appUpdateProgressEnabled && payload.phase !== 'error') return;
  appUpdateProgressState = payload;

  if (payload.phase === 'fallback_full' && payload.fallbackReason) {
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.fallbackToFull', {
      reason: String(payload.fallbackReason),
    }));
  }

  renderAppUpdateProgress();
}

function initAppUpdateProgressBridge() {
  if (disposeAppUpdateProgress || !window.brewApp?.onAppUpdateProgress) return;
  disposeAppUpdateProgress = window.brewApp.onAppUpdateProgress(handleAppUpdateProgress);
}

function renderAppUpdateStatus() {
  if (!appUpdateState) {
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.ready'));
    installAppUpdateBtn.disabled = true;
    renderSettingsSummary();
    renderAppUpdateProgress();
    return;
  }

  if (appUpdateState.updateAvailable) {
    if (shouldAllowInstall(appUpdateState)) {
      const lines = [
        i18n.t('updates.available', {
          latest: appUpdateState.latestVersion,
          current: appUpdateState.currentVersion,
        }),
      ];

      if (appUpdateState.supportsDelta) {
        lines.push(i18n.t('updates.availableDelta'));
      } else {
        lines.push(i18n.t('updates.availableFullOnly'));
      }

      setInlineStatus(appUpdateStatusEl, lines.join('\n'));
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

  renderSettingsSummary();
  renderAppUpdateProgress();
}

function loadAppUpdateMeta() {
  try {
    const raw = localStorage.getItem(APP_UPDATE_META_STORAGE_KEY);
    if (!raw) {
      return { status: 'unknown', checkedAt: '' };
    }
    const parsed = JSON.parse(raw);
    return {
      status: String(parsed?.status || 'unknown'),
      checkedAt: String(parsed?.checkedAt || ''),
    };
  } catch {
    return { status: 'unknown', checkedAt: '' };
  }
}

function saveAppUpdateMeta() {
  localStorage.setItem(APP_UPDATE_META_STORAGE_KEY, JSON.stringify(appUpdateMeta));
}

function markAppUpdateCheck(status) {
  appUpdateMeta = {
    status: String(status || 'unknown'),
    checkedAt: new Date().toISOString(),
  };
  saveAppUpdateMeta();
}

async function checkAppUpdate({ silent = false } = {}) {
  checkAppUpdateBtn.disabled = true;
  appUpdateProgressEnabled = !silent;
  if (!silent) {
    clearAppUpdateProgress();
  }

  if (!silent) {
    installAppUpdateBtn.disabled = true;
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.checking'));
  }

  try {
    const payload = await window.brewApp.checkAppUpdate();
    appUpdateState = payload;
    markAppUpdateCheck(payload?.updateAvailable ? 'update-available' : 'up-to-date');
    renderAppUpdateStatus();
  } catch (err) {
    markAppUpdateCheck('error');
    if (!silent) {
      appUpdateState = null;
      setInlineStatus(appUpdateStatusEl, i18n.t('updates.failed', { error: err.message }), true);
    }
    renderSettingsSummary();
  } finally {
    appUpdateProgressEnabled = false;
    clearAppUpdateProgress();
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
  if (!shouldAllowInstall(appUpdateState)) {
    return;
  }

  appUpdateProgressEnabled = true;
  clearAppUpdateProgress();
  checkAppUpdateBtn.disabled = true;
  installAppUpdateBtn.disabled = true;
  installAppUpdateBtn.textContent = i18n.t('updates.installing');
  setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloading'));

  try {
    const payload = await window.brewApp.downloadAndInstallAppUpdate();
    appUpdateState = payload;

    if (payload?.restartScheduled) {
      setInlineStatus(appUpdateStatusEl, i18n.t('updates.restartingSoon'));
      showMessage(i18n.t('updates.restartingSoon'));
      return;
    }

    const modeUsed = String(payload?.modeUsed || payload?.mode || '').toLowerCase();
    const modeText = modeUsed === 'delta' ? i18n.t('updates.modeDelta') : i18n.t('updates.modeFull');
    setInlineStatus(
      appUpdateStatusEl,
      `${i18n.t('updates.downloaded', { path: payload.downloadedPath })}\n${modeText}`,
    );
    installAppUpdateBtn.disabled = !shouldAllowInstall(appUpdateState);
    appUpdateProgressEnabled = false;
  } catch (err) {
    setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloadFailed', { error: err.message }), true);
    installAppUpdateBtn.disabled = !shouldAllowInstall(appUpdateState);
    appUpdateProgressEnabled = false;
  } finally {
    installAppUpdateBtn.textContent = i18n.t('updates.install');
    checkAppUpdateBtn.disabled = false;
    renderAppUpdateProgress();
  }
}

function isBrewPathConfigured() {
  return !!String(settingsState?.settings?.brew_path || '').trim();
}

function applyActionAvailability() {
  const allowed = isBrewPathConfigured();
  const selectedCount = selectedPackagesFromSnapshot().length;
  const visibleOutdatedCount = getFilteredPackages().filter((pkg) => pkg?.outdated).length;

  checkNowBtn.disabled = busyLoading || !allowed;
  updateAllBtn.disabled = busyLoading || !allowed;

  if (updateSelectedBtn) {
    updateSelectedBtn.disabled = busyLoading || !allowed || selectedCount === 0;
  }

  if (selectVisibleOutdatedBtn) {
    selectVisibleOutdatedBtn.disabled = busyLoading || !allowed || visibleOutdatedCount === 0;
  }

  if (clearSelectionBtn) {
    clearSelectionBtn.disabled = busyLoading || selectedCount === 0;
  }

  document.querySelectorAll('.row-update-btn').forEach((btn) => {
    btn.disabled = busyLoading || !allowed;
  });

  document.querySelectorAll('.row-select-checkbox').forEach((checkbox) => {
    checkbox.disabled = busyLoading || !allowed;
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

function parseBusyOperationInfo(errorMessage) {
  const raw = String(errorMessage || '');
  const match = raw.match(/Operation '([^']+)' is already running since ([^\n]+)/i);
  if (!match) {
    return null;
  }

  return {
    name: String(match[1] || '').trim(),
    sinceRaw: String(match[2] || '').trim(),
  };
}

function formatOperationErrorMessage(errorLike) {
  const raw = String(errorLike?.message || errorLike || '').trim();
  const busy = parseBusyOperationInfo(raw);
  if (!busy) {
    return raw;
  }

  return i18n.t('message.operationBusy', {
    name: busy.name || i18n.t('common.unknown'),
    since: formatDate(busy.sinceRaw),
  });
}

function progressPackageLabel(item) {
  if (!item || typeof item !== 'object') {
    return String(item || '').trim();
  }
  const label = String(item.label || '').trim();
  if (label) return label;
  const name = String(item.name || '').trim();
  const kind = String(item.kind || '').trim();
  if (name && kind) return `${kind}:${name}`;
  return name || kind;
}

function renderProgressPackageList(items, { mode = 'generic', limit = 8 } = {}) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    return i18n.t('progress.noItems');
  }

  const rows = list.slice(0, limit).map((item) => {
    const label = progressPackageLabel(item) || i18n.t('common.unknown');
    if (mode === 'completed') {
      if (item?.canceled) return `🛑 ${label}`;
      if (item?.skipped) return `⏭ ${label}`;
      if (item?.ok) return `✅ ${label}`;
      const detail = String(item?.error || '').trim();
      return detail ? `❌ ${label} — ${detail}` : `❌ ${label}`;
    }
    if (mode === 'failed') {
      const detail = String(item?.error || '').trim();
      return detail ? `❌ ${label} — ${detail}` : `❌ ${label}`;
    }
    return `• ${label}`;
  });

  const remaining = list.length - rows.length;
  if (remaining > 0) {
    rows.push(`… ${i18n.t('message.moreItems', { count: i18n.formatNumber(remaining) })}`);
  }

  return rows.join('\n');
}

function renderUpdateLivePanel(progress) {
  if (!updateLivePanelEl) return;

  const operationName = String(progress?.operation_name || '').trim().toLowerCase();
  const planned = Array.isArray(progress?.planned_packages) ? progress.planned_packages : [];
  const completed = Array.isArray(progress?.completed_packages) ? progress.completed_packages : [];
  const remaining = Array.isArray(progress?.remaining_packages) ? progress.remaining_packages : [];
  const failed = Array.isArray(progress?.failed_packages) ? progress.failed_packages : [];
  const isUpdateOperation = operationName.startsWith('update');

  if (!isUpdateOperation) {
    updateLivePanelEl.classList.remove('visible');
    return;
  }

  updateLivePanelEl.classList.add('visible');

  const total = planned.length || Number(progress?.total || 0);
  const done = completed.length || Number(progress?.done || 0);
  const remainingCount = remaining.length || Math.max(0, total - done);
  const failedCount = failed.length;

  if (updateLiveTotalEl) updateLiveTotalEl.textContent = i18n.formatNumber(total);
  if (updateLiveCompletedEl) updateLiveCompletedEl.textContent = i18n.formatNumber(done);
  if (updateLiveRemainingEl) updateLiveRemainingEl.textContent = i18n.formatNumber(remainingCount);
  if (updateLiveFailedEl) updateLiveFailedEl.textContent = i18n.formatNumber(failedCount);

  if (updateLiveCompletedListEl) {
    updateLiveCompletedListEl.textContent = renderProgressPackageList(completed, { mode: 'completed', limit: 8 });
  }
  if (updateLiveRemainingListEl) {
    updateLiveRemainingListEl.textContent = renderProgressPackageList(remaining, { mode: 'generic', limit: 8 });
  }
  if (updateLiveFailedListEl) {
    updateLiveFailedListEl.textContent = renderProgressPackageList(failed, { mode: 'failed', limit: 6 });
  }
}

function setUpdateHistoryStatus(text, isError = false) {
  if (!updateHistoryStatusEl) return;
  const value = String(text || '').trim();
  updateHistoryStatusEl.style.display = value ? 'block' : 'none';
  setInlineStatus(updateHistoryStatusEl, value, isError);
}

function withTimeout(promise, timeoutMs, timeoutMessage) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(timeoutMessage));
    }, timeoutMs);

    promise
      .then((value) => {
        clearTimeout(timer);
        resolve(value);
      })
      .catch((error) => {
        clearTimeout(timer);
        reject(error);
      });
  });
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

  const autoText = scheduler.active
    ? i18n.t('settings.summaryAutoEnabled')
    : i18n.t('settings.summaryAutoDisabled');
  const brewText = isBrewPathConfigured()
    ? i18n.t('settings.summaryBrewPathSet')
    : i18n.t('settings.summaryBrewPathMissing');

  let appStatusText = i18n.t('settings.summaryAppUnknown');
  if (appUpdateMeta.status === 'up-to-date') {
    appStatusText = i18n.t('settings.summaryAppUpToDate');
  } else if (appUpdateMeta.status === 'update-available') {
    appStatusText = i18n.t('settings.summaryAppUpdateAvailable');
  } else if (appUpdateMeta.status === 'error') {
    appStatusText = i18n.t('settings.summaryAppCheckFailed');
  }

  const checkedText = appUpdateMeta.checkedAt
    ? i18n.t('settings.summaryAppUpdateCheckedAt', { date: formatDate(appUpdateMeta.checkedAt) })
    : i18n.t('settings.summaryAppUpdateNeverChecked');

  return [autoText, brewText, appStatusText, checkedText].join(' • ');
}

function renderSettingsSummary() {
  const scheduler = settingsState?.scheduler;
  if (!scheduler) {
    settingsSummaryEl.style.color = 'var(--muted)';
    settingsSummaryEl.textContent = i18n.t('settings.loading');
    return;
  }

  settingsSummaryEl.style.color = 'var(--muted)';
  settingsSummaryEl.textContent = schedulerSummaryText(scheduler);
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
  renderSettingsSummary();
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
    renderSettingsSummary();
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
  selectedPackageKeys.clear();
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

  if (loaderCancelBtn) {
    loaderCancelBtn.disabled = true;
  }

  if (updateLivePanelEl) {
    updateLivePanelEl.classList.remove('visible');
  }
  if (updateLiveTotalEl) updateLiveTotalEl.textContent = '0';
  if (updateLiveCompletedEl) updateLiveCompletedEl.textContent = '0';
  if (updateLiveRemainingEl) updateLiveRemainingEl.textContent = '0';
  if (updateLiveFailedEl) updateLiveFailedEl.textContent = '0';
  if (updateLiveCompletedListEl) updateLiveCompletedListEl.textContent = i18n.t('progress.noItems');
  if (updateLiveRemainingListEl) updateLiveRemainingListEl.textContent = i18n.t('progress.noItems');
  if (updateLiveFailedListEl) updateLiveFailedListEl.textContent = i18n.t('progress.noItems');
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

  if (loaderCancelBtn) {
    const operationName = String(progress.operation_name || '').trim().toLowerCase();
    const canCancel = !!progress.running && !!operationName;
    const cancelAlreadyRequested = !!progress.cancel_requested;
    loaderCancelBtn.disabled = !canCancel || cancelInFlight || cancelAlreadyRequested;
  }

  renderLoaderSteps(progress.phase);
  renderUpdateLivePanel(progress);

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

async function cancelCurrentOperation() {
  if (cancelInFlight) return;
  cancelInFlight = true;

  if (loaderCancelBtn) {
    loaderCancelBtn.disabled = true;
  }

  try {
    await window.brewApp.cancelOperation();
    showMessage(i18n.t('message.cancelRequestSent'));
  } catch (err) {
    showMessage(i18n.t('message.cancelRequestFailed', { error: formatOperationErrorMessage(err) }), true);
  } finally {
    cancelInFlight = false;
    await pollCheckProgress();
  }
}

function formatDate(value) {
  return i18n.formatDate(value);
}

function updateTableFilterControls() {
  if (packageSearchInputEl) {
    packageSearchInputEl.placeholder = i18n.t('table.searchPlaceholder');
    packageSearchInputEl.setAttribute('aria-label', i18n.t('table.searchLabel'));
  }

  if (packageTypeFilterEl) {
    packageTypeFilterEl.value = packageTypeFilter;
  }

  if (packageStatusFilterEl) {
    packageStatusFilterEl.value = packageStatusFilter;
  }

  if (packageSearchClearBtnEl) {
    packageSearchClearBtnEl.disabled = !packageSearchTerm;
  }

  if (packageFilterResetBtnEl) {
    const hasAnyFilter = !!packageSearchTerm || packageTypeFilter !== 'all' || packageStatusFilter !== 'all';
    packageFilterResetBtnEl.disabled = !hasAnyFilter;
  }
}

function updateHistoryFilterControls() {
  if (historySearchInputEl) {
    historySearchInputEl.placeholder = i18n.t('history.searchPlaceholder');
    historySearchInputEl.setAttribute('aria-label', i18n.t('history.searchLabel'));
  }

  if (historyKindFilterEl) {
    historyKindFilterEl.value = historyKindFilter;
  }

  if (historyStatusFilterEl) {
    historyStatusFilterEl.value = historyStatusFilter;
  }

  if (historySortSelectEl) {
    historySortSelectEl.value = historySortBy;
  }

  if (historyFiltersClearBtnEl) {
    const hasAnyFilter = !!historySearchTerm
      || historyKindFilter !== 'all'
      || historyStatusFilter !== 'all'
      || historySortBy !== 'newest';
    historyFiltersClearBtnEl.disabled = !hasAnyFilter;
  }
}

function updateSearchControls() {
  updateTableFilterControls();
  updateHistoryFilterControls();
}

function packageMatchesSearch(pkg, normalizedTerm) {
  if (!normalizedTerm) return true;
  const text = [
    pkg?.name,
    pkg?.description,
    pkg?.description_he,
    pkg?.kind,
    pkg?.installed_version,
    pkg?.latest_version,
  ]
    .map((value) => String(value || '').toLowerCase())
    .join(' ');
  return text.includes(normalizedTerm);
}

function packageKindValue(pkg) {
  return String(pkg?.kind || 'formula').trim().toLowerCase();
}


function packageSelectionKey(name, kind) {
  const normalizedName = String(name || '').trim();
  const normalizedKind = String(kind || '').trim().toLowerCase();
  if (!normalizedName || !normalizedKind) return '';
  return `${normalizedKind}:${normalizedName}`;
}

function packageSelectionKeyFromPkg(pkg) {
  return packageSelectionKey(pkg?.name, pkg?.kind);
}

function selectedPackagesFromSnapshot() {
  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];
  return pkgs
    .filter((pkg) => pkg?.outdated && selectedPackageKeys.has(packageSelectionKeyFromPkg(pkg)))
    .map((pkg) => ({
      name: String(pkg?.name || '').trim(),
      kind: packageKindValue(pkg),
    }))
    .filter((pkg) => pkg.name && (pkg.kind === 'formula' || pkg.kind === 'cask'));
}

function pruneSelectedPackages() {
  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];
  const validKeys = new Set(
    pkgs
      .filter((pkg) => pkg?.outdated)
      .map((pkg) => packageSelectionKeyFromPkg(pkg))
      .filter(Boolean),
  );

  let changed = false;
  Array.from(selectedPackageKeys).forEach((key) => {
    if (!validKeys.has(key)) {
      selectedPackageKeys.delete(key);
      changed = true;
    }
  });

  return changed;
}

function updateSelectionSummary(filteredList = null) {
  const selectedCount = selectedPackagesFromSnapshot().length;
  const selectedCountText = i18n.formatNumber(selectedCount);

  if (selectedSummaryEl) {
    selectedSummaryEl.textContent = i18n.t('table.selectedCount', { count: selectedCountText });
  }

  if (updateSelectedBtn) {
    updateSelectedBtn.textContent = i18n.t('actions.updateSelectedCount', { count: selectedCountText });
  }

  const visibleList = Array.isArray(filteredList) ? filteredList : getFilteredPackages();
  const visibleOutdated = visibleList.filter((pkg) => pkg?.outdated).length;

  if (selectVisibleOutdatedBtn) {
    selectVisibleOutdatedBtn.disabled = busyLoading || !isBrewPathConfigured() || visibleOutdated === 0;
  }

  if (clearSelectionBtn) {
    clearSelectionBtn.disabled = busyLoading || selectedCount === 0;
  }
}

function togglePackageSelected(pkg, shouldSelect) {
  const key = packageSelectionKeyFromPkg(pkg);
  if (!key) return;
  if (shouldSelect) {
    selectedPackageKeys.add(key);
    return;
  }
  selectedPackageKeys.delete(key);
}

function selectVisibleOutdated() {
  const visible = getFilteredPackages().filter((pkg) => pkg?.outdated);
  if (!visible.length) return;
  visible.forEach((pkg) => {
    const key = packageSelectionKeyFromPkg(pkg);
    if (key) selectedPackageKeys.add(key);
  });
  renderTable();
}

function clearSelection() {
  if (!selectedPackageKeys.size) return;
  selectedPackageKeys.clear();
  renderTable();
}

function packageMatchesTypeFilter(pkg) {
  if (packageTypeFilter === 'all') return true;
  return packageKindValue(pkg) === packageTypeFilter;
}

function packageMatchesStatusFilter(pkg) {
  if (packageStatusFilter === 'all') return true;
  if (packageStatusFilter === 'needs_update') return !!pkg?.outdated;
  if (packageStatusFilter === 'up_to_date') return !pkg?.outdated;
  return true;
}

function isSortableColumnKey(key) {
  return SORTABLE_COLUMN_KEYS.has(String(key || '').trim());
}

function normalizeSortText(value) {
  return String(value || '').trim();
}

function parseSortDate(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  const stamp = Date.parse(raw);
  return Number.isFinite(stamp) ? stamp : null;
}

function compareTextValues(left, right, direction = 'asc') {
  const a = normalizeSortText(left);
  const b = normalizeSortText(right);
  const cmp = a.localeCompare(b, i18n.lang === 'he' ? 'he' : 'en', {
    numeric: true,
    sensitivity: 'base',
  });
  return direction === 'desc' ? -cmp : cmp;
}

function compareNumberValues(left, right, direction = 'asc') {
  const hasLeft = Number.isFinite(left);
  const hasRight = Number.isFinite(right);

  if (!hasLeft && !hasRight) return 0;
  if (!hasLeft) return 1;
  if (!hasRight) return -1;

  const cmp = left === right ? 0 : (left > right ? 1 : -1);
  return direction === 'desc' ? -cmp : cmp;
}

function comparePackagesByColumn(a, b, key, direction) {
  let cmp = 0;

  switch (key) {
    case 'package':
      cmp = compareTextValues(a?.name, b?.name, direction);
      break;
    case 'type':
      cmp = compareTextValues(a?.kind, b?.kind, direction);
      break;
    case 'currentVersion':
      cmp = compareTextValues(a?.installed_version, b?.installed_version, direction);
      break;
    case 'latestVersion':
      cmp = compareTextValues(a?.latest_version, b?.latest_version, direction);
      break;
    case 'currentDate':
      cmp = compareNumberValues(parseSortDate(a?.current_release_date), parseSortDate(b?.current_release_date), direction);
      break;
    case 'latestDate':
      cmp = compareNumberValues(
        parseSortDate(a?.latest_release_date || a?.release_date),
        parseSortDate(b?.latest_release_date || b?.release_date),
        direction,
      );
      break;
    case 'daysOutdated': {
      const daysA = Number(a?.days_since_outdated);
      const daysB = Number(b?.days_since_outdated);
      cmp = compareNumberValues(
        Number.isFinite(daysA) ? daysA : null,
        Number.isFinite(daysB) ? daysB : null,
        direction,
      );
      break;
    }
    case 'status':
      cmp = compareNumberValues(a?.outdated ? 0 : 1, b?.outdated ? 0 : 1, direction);
      break;
    default:
      cmp = 0;
      break;
  }

  if (cmp !== 0) return cmp;
  return compareTextValues(a?.name, b?.name, 'asc');
}

function applyTableSort(list) {
  const key = String(tableSortState?.key || '').trim();
  const direction = tableSortState?.direction === 'desc' ? 'desc' : 'asc';

  if (!key || !isSortableColumnKey(key)) {
    return list;
  }

  return [...list].sort((a, b) => comparePackagesByColumn(a, b, key, direction));
}

function loadSavedTableSort() {
  try {
    const raw = localStorage.getItem(TABLE_SORT_STORAGE_KEY);
    if (!raw) {
      return { key: '', direction: 'asc' };
    }

    const parsed = JSON.parse(raw);
    const key = String(parsed?.key || '').trim();
    const direction = parsed?.direction === 'desc' ? 'desc' : 'asc';

    if (!isSortableColumnKey(key)) {
      return { key: '', direction };
    }

    return { key, direction };
  } catch {
    return { key: '', direction: 'asc' };
  }
}

function saveTableSort() {
  localStorage.setItem(TABLE_SORT_STORAGE_KEY, JSON.stringify({
    key: String(tableSortState?.key || '').trim(),
    direction: tableSortState?.direction === 'desc' ? 'desc' : 'asc',
  }));
}

function toggleTableSort(key) {
  if (!isSortableColumnKey(key)) {
    return;
  }

  const normalizedKey = String(key);
  if (tableSortState?.key === normalizedKey) {
    tableSortState = {
      key: normalizedKey,
      direction: tableSortState.direction === 'asc' ? 'desc' : 'asc',
    };
  } else {
    tableSortState = {
      key: normalizedKey,
      direction: 'asc',
    };
  }

  saveTableSort();
}

function renderTableSortIndicators() {
  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));

  headers.forEach((th) => {
    const key = String(th.dataset.colKey || '').trim();
    const label = th.querySelector('.th-label');
    const sortable = isSortableColumnKey(key);

    th.classList.remove('sortable', 'sorted-asc', 'sorted-desc');

    if (!sortable) {
      th.removeAttribute('aria-sort');
      if (label) {
        label.dataset.sortIndicator = '';
      }
      return;
    }

    th.classList.add('sortable');
    const isActive = tableSortState?.key === key;
    const direction = tableSortState?.direction === 'desc' ? 'desc' : 'asc';

    if (isActive) {
      th.classList.add(direction === 'desc' ? 'sorted-desc' : 'sorted-asc');
      th.setAttribute('aria-sort', direction === 'desc' ? 'descending' : 'ascending');
    } else {
      th.setAttribute('aria-sort', 'none');
    }

    if (label) {
      label.dataset.sortIndicator = isActive
        ? (direction === 'desc' ? ' ▼' : ' ▲')
        : '';
    }
  });
}

function initTableSorting() {
  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));

  headers.forEach((th) => {
    const key = String(th.dataset.colKey || '').trim();
    if (!isSortableColumnKey(key)) {
      return;
    }

    th.setAttribute('tabindex', '0');

    const onSort = () => {
      toggleTableSort(key);
      renderTable();
    };

    th.addEventListener('click', (event) => {
      if (event.target.closest('[data-resizer]')) {
        return;
      }
      onSort();
    });

    th.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') {
        return;
      }
      event.preventDefault();
      onSort();
    });
  });

  renderTableSortIndicators();
}

function findPackageInSnapshot(name, kind) {
  const list = Array.isArray(snapshot?.packages) ? snapshot.packages : [];
  return list.find((pkg) => String(pkg?.name || '') === String(name || '') && String(pkg?.kind || '') === String(kind || ''));
}

function historyStatusMeta(item) {
  if (!item?.ok) {
    return { text: i18n.t('history.statusFail'), className: 'fail' };
  }
  if (item?.verified_latest) {
    return { text: i18n.t('history.statusLatest'), className: 'ok' };
  }
  return { text: i18n.t('history.statusOk'), className: 'warn' };
}

function historyStatusKey(item) {
  if (!item?.ok) return 'fail';
  if (item?.verified_latest) return 'latest';
  return 'pending';
}

function historyMatchesSearch(item, normalizedTerm) {
  if (!normalizedTerm) return true;
  const text = [
    item?.name,
    item?.kind,
    item?.latest_version,
    item?.installed_version,
    historyStatusKey(item),
  ]
    .map((value) => String(value || '').toLowerCase())
    .join(' ');
  return text.includes(normalizedTerm);
}

function historyMatchesKindFilter(item) {
  if (historyKindFilter === 'all') return true;
  return String(item?.kind || 'formula').trim().toLowerCase() === historyKindFilter;
}

function historyMatchesStatusFilterItem(item) {
  if (historyStatusFilter === 'all') return true;
  return historyStatusKey(item) === historyStatusFilter;
}

function historyTimestamp(item) {
  const stamp = Date.parse(String(item?.timestamp || '').trim());
  return Number.isFinite(stamp) ? stamp : 0;
}

function sortHistoryItems(items) {
  const list = [...items];
  switch (historySortBy) {
    case 'oldest':
      return list.sort((a, b) => historyTimestamp(a) - historyTimestamp(b));
    case 'name_asc':
      return list.sort((a, b) => compareTextValues(a?.name, b?.name, 'asc'));
    case 'name_desc':
      return list.sort((a, b) => compareTextValues(a?.name, b?.name, 'desc'));
    case 'newest':
    default:
      return list.sort((a, b) => historyTimestamp(b) - historyTimestamp(a));
  }
}

function getFilteredHistoryItems() {
  const base = Array.isArray(updateHistoryItems) ? updateHistoryItems : [];
  const filtered = base.filter((item) => {
    if (!historyMatchesSearch(item, historySearchTerm)) return false;
    if (!historyMatchesKindFilter(item)) return false;
    if (!historyMatchesStatusFilterItem(item)) return false;
    return true;
  });

  return sortHistoryItems(filtered);
}

function renderUpdateHistory(items = [], { filtered = false } = {}) {
  if (!updateHistoryListEl) return;

  updateHistoryListEl.innerHTML = '';
  if (!Array.isArray(items) || items.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'muted small';
    empty.textContent = filtered ? i18n.t('history.emptyFiltered') : i18n.t('history.empty');
    updateHistoryListEl.appendChild(empty);
    return;
  }

  items.slice(0, 40).forEach((item) => {
    const status = historyStatusMeta(item);

    const root = document.createElement('div');
    root.className = 'history-item';

    const top = document.createElement('div');
    top.className = 'history-top';

    const name = document.createElement('div');
    name.className = 'history-name';
    name.textContent = String(item?.name || i18n.t('common.unknown'));

    const when = document.createElement('div');
    when.className = 'history-when';
    when.textContent = formatDate(item?.timestamp);

    top.appendChild(name);
    top.appendChild(when);

    const meta = document.createElement('div');
    meta.className = 'history-meta';

    const info = document.createElement('div');
    const kindText = i18n.t('history.kind', { kind: kindLabel(item?.kind) });
    const version = String(item?.latest_version || item?.installed_version || i18n.t('common.unknown'));
    const versionText = i18n.t('history.version', { version });
    info.textContent = `${kindText} • ${versionText}`;

    const badge = document.createElement('span');
    badge.className = `history-badge ${status.className}`;
    badge.textContent = status.text;

    meta.appendChild(info);
    meta.appendChild(badge);

    root.appendChild(top);
    root.appendChild(meta);
    updateHistoryListEl.appendChild(root);
  });
}

function renderHistoryPanel() {
  const hasFilters = !!historySearchTerm
    || historyKindFilter !== 'all'
    || historyStatusFilter !== 'all'
    || historySortBy !== 'newest';

  const items = getFilteredHistoryItems();
  renderUpdateHistory(items, { filtered: hasFilters });

  if (historyLoadInFlight) {
    setUpdateHistoryStatus(i18n.t('history.loading'));
  } else if (historyLoadError) {
    setUpdateHistoryStatus(i18n.t('history.loadFailed', { error: historyLoadError }), true);
  } else {
    setUpdateHistoryStatus('');
  }

  updateHistoryFilterControls();
}

async function loadUpdateHistory() {
  historyLoadInFlight = true;
  historyLoadError = '';
  renderHistoryPanel();

  try {
    const payload = await withTimeout(
      window.brewApp.getUpdateHistory(),
      5000,
      i18n.t('history.loadTimeout'),
    );
    updateHistoryItems = Array.isArray(payload?.items) ? payload.items : [];
  } catch (err) {
    historyLoadError = String(err?.message || i18n.t('common.unknown'));
  } finally {
    historyLoadInFlight = false;
    renderHistoryPanel();
  }
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
  return value === 'cask' ? 'אפליקציית mac' : 'חבילת בנייה';
}

function packageDescription(pkg) {
  const he = String(pkg?.description_he || '').trim();
  const en = String(pkg?.description || '').trim();
  if (i18n.lang === 'he') {
    return he || en;
  }
  return en || he;
}

function skippedReasonLabel(reason) {
  const value = String(reason || '').trim();
  if (value === 'not_outdated') {
    return i18n.t('message.skippedReasonNotOutdated');
  }
  return i18n.t('common.unknown');
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
  const byTab = activeTab === 'outdated'
    ? pkgs.filter((pkg) => pkg.outdated)
    : pkgs;

  const byType = byTab.filter((pkg) => packageMatchesTypeFilter(pkg));
  const byStatus = byType.filter((pkg) => packageMatchesStatusFilter(pkg));

  const filtered = packageSearchTerm
    ? byStatus.filter((pkg) => packageMatchesSearch(pkg, packageSearchTerm))
    : byStatus;

  return applyTableSort(filtered);
}

function renderTable() {
  renderTableSortIndicators();
  pruneSelectedPackages();

  const all = Array.isArray(snapshot?.packages) ? snapshot.packages : [];
  const list = getFilteredPackages();
  const outdatedCount = all.filter((pkg) => pkg.outdated).length;
  const searchRaw = String(packageSearchInputEl?.value || '').trim();
  const hasAdvancedFilters = packageTypeFilter !== 'all' || packageStatusFilter !== 'all';

  if (packageSearchTerm) {
    tableTitleEl.textContent = activeTab === 'outdated'
      ? i18n.t('table.showingOutdatedSearch', {
        count: i18n.formatNumber(list.length),
        query: searchRaw,
      })
      : i18n.t('table.showingAllSearch', {
        count: i18n.formatNumber(list.length),
        query: searchRaw,
      });
  } else if (hasAdvancedFilters) {
    tableTitleEl.textContent = activeTab === 'outdated'
      ? i18n.t('table.showingOutdatedFiltered', { count: i18n.formatNumber(list.length) })
      : i18n.t('table.showingAllFiltered', { count: i18n.formatNumber(list.length) });
  } else {
    tableTitleEl.textContent = activeTab === 'outdated'
      ? i18n.t('table.showingOutdated', { count: i18n.formatNumber(list.length) })
      : i18n.t('table.showingAll', {
        total: i18n.formatNumber(all.length),
        outdated: i18n.formatNumber(outdatedCount),
      });
  }

  tbodyEl.innerHTML = '';
  if (!list.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = TABLE_COLUMN_COUNT;
    td.className = 'muted';
    if (packageSearchTerm) {
      td.textContent = i18n.t('table.noSearchResults', { query: searchRaw || packageSearchTerm });
    } else if (hasAdvancedFilters) {
      td.textContent = i18n.t('table.noFilteredResults');
    } else {
      td.textContent = activeTab === 'outdated' ? i18n.t('table.noUpdates') : i18n.t('table.noPackages');
    }
    tr.appendChild(td);
    tbodyEl.appendChild(tr);
    updateSelectionSummary(list);
    applyActionAvailability();
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

    const tdSelect = document.createElement('td');
    if (pkg.outdated) {
      const wrap = document.createElement('label');
      wrap.className = 'row-select-wrap';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'row-select-checkbox';
      checkbox.checked = selectedPackageKeys.has(packageSelectionKeyFromPkg(pkg));
      checkbox.setAttribute('aria-label', i18n.t('table.selectPackage', { name: pkg.name || '' }));
      checkbox.addEventListener('change', () => {
        togglePackageSelected(pkg, checkbox.checked);
        updateSelectionSummary(list);
        applyActionAvailability();
      });

      wrap.appendChild(checkbox);
      tdSelect.appendChild(wrap);
    } else {
      tdSelect.innerHTML = `<span class="muted small">${i18n.t('common.dash')}</span>`;
    }

    const tdAction = document.createElement('td');
    if (pkg.outdated) {
      const btn = document.createElement('button');
      btn.className = 'row-update-btn';
      btn.textContent = i18n.t('buttons.update');
      btn.addEventListener('click', () => updateOne(pkg.name, pkg.kind, btn));
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
      tdSelect,
      tdAction,
    ].forEach((td) => tr.appendChild(td));

    tbodyEl.appendChild(tr);
  });

  updateSelectionSummary(list);
  applyActionAvailability();
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
  renderHistoryPanel();
  renderAppUpdateStatus();
  updateSearchControls();
  applyActionAvailability();
}

async function loadState() {
  clearMessage();

  if (!isBrewPathConfigured()) {
    snapshot = { counts: {}, packages: [] };
    selectedPackageKeys.clear();
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
    showMessage(i18n.t('message.checkFailed', { error: formatOperationErrorMessage(err) }), true);
  } finally {
    stopProgressPolling();
    setLoading(false, 'message.loadingState');
  }
}

async function updateOne(name, kind, sourceBtn = null) {
  if (!ensureBrewConfiguredForActions()) return;
  if (!confirm(i18n.t('confirm.updateOne', { name, kind: kindLabel(kind) }))) return;

  if (sourceBtn) {
    sourceBtn.textContent = i18n.t('buttons.updating');
  }

  clearMessage();
  showMessage(i18n.t('message.updatingOne', { name }));
  setLoading(true, 'message.updatingOne', { name });
  startProgressPolling();

  try {
    const res = await window.brewApp.updateOne(name, kind);
    snapshot = res.snapshot;
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });

    if (res?.result?.ok) {
      let messageText = i18n.t('message.updateOneSuccess', { name });
      if (res?.inventory_refresh_error) {
        messageText = `${messageText}\n${i18n.t('message.inventoryRefreshWarning', {
          error: res.inventory_refresh_error,
        })}`;
      }
      showMessage(messageText);

      const pkg = findPackageInSnapshot(name, kind);
      const verifiedLatest = !!(pkg && !pkg.outdated);
      if (verifiedLatest) {
        const version = pkg.latest_version || pkg.installed_version || i18n.t('common.unknown');
        alert(i18n.t('message.updateOneLatestConfirmed', { name, version }));
      }
    } else {
      const detail = res?.result?.stderr || res?.result?.stdout || i18n.t('common.unknown');
      showMessage(i18n.t('message.updateOneFailed', { name, error: detail }), true);
    }

    await loadUpdateHistory();
  } catch (err) {
    showMessage(i18n.t('message.updateOneRequestFailed', { error: formatOperationErrorMessage(err) }), true);
  } finally {
    stopProgressPolling();
    setLoading(false, 'message.loadingState');
    if (sourceBtn) {
      sourceBtn.textContent = i18n.t('buttons.update');
    }
  }
}

async function updateSelected() {
  if (!ensureBrewConfiguredForActions()) return;

  const selected = selectedPackagesFromSnapshot();
  if (!selected.length) {
    showMessage(i18n.t('message.noSelectedPackages'), true);
    return;
  }

  if (!confirm(i18n.t('confirm.updateSelected', { count: i18n.formatNumber(selected.length) }))) return;

  if (updateSelectedBtn) {
    updateSelectedBtn.textContent = i18n.t('buttons.updating');
  }

  clearMessage();
  showMessage(i18n.t('message.updatingSelected', { count: i18n.formatNumber(selected.length) }));
  setLoading(true, 'message.updatingSelected', { count: i18n.formatNumber(selected.length) });
  startProgressPolling();

  try {
    const res = await window.brewApp.updateSelected(selected);
    snapshot = res.snapshot;
    pruneSelectedPackages();
    renderCards(snapshot);
    renderTable();
    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });

    let messageText = i18n.t('message.updateSelectedSuccess', {
      success: i18n.formatNumber(res.updated_count || 0),
      failed: i18n.formatNumber(res.failed_count || 0),
      skipped: i18n.formatNumber(res.skipped_count || 0),
    });

    const results = Array.isArray(res?.results) ? res.results : [];
    const skippedResults = results.filter((item) => item?.skipped);
    if (skippedResults.length > 0) {
      const detailParts = skippedResults
        .slice(0, 5)
        .map((item) => `${String(item?.name || i18n.t('common.unknown'))} (${skippedReasonLabel(item?.reason)})`);

      const remaining = skippedResults.length - detailParts.length;
      if (remaining > 0) {
        detailParts.push(i18n.t('message.moreItems', { count: i18n.formatNumber(remaining) }));
      }

      messageText = `${messageText}\n${i18n.t('message.updateSelectedSkippedDetails', {
        packages: detailParts.join(', '),
      })}`;
    }

    if (res?.inventory_refresh_error) {
      messageText = `${messageText}
${i18n.t('message.inventoryRefreshWarning', {
        error: res.inventory_refresh_error,
      })}`;
    }

    showMessage(messageText, Number(res?.failed_count || 0) > 0);

    const verifiedLatestCount = results.reduce((acc, result) => {
      if (!result?.ok || result?.skipped) return acc;
      const pkg = findPackageInSnapshot(result?.name, result?.kind);
      return pkg && !pkg.outdated ? acc + 1 : acc;
    }, 0);
    if (verifiedLatestCount > 0) {
      alert(i18n.t('message.updateSelectedLatestConfirmed', { count: i18n.formatNumber(verifiedLatestCount) }));
    }

    await loadUpdateHistory();
  } catch (err) {
    showMessage(i18n.t('message.updateSelectedFailed', { error: formatOperationErrorMessage(err) }), true);
  } finally {
    stopProgressPolling();
    setLoading(false, 'message.loadingState');
    updateSelectionSummary();
  }
}

async function updateAll() {
  if (!ensureBrewConfiguredForActions()) return;
  if (!confirm(i18n.t('confirm.updateAll'))) return;

  updateAllBtn.textContent = i18n.t('buttons.updating');

  clearMessage();
  showMessage(i18n.t('message.updatingAll'));
  setLoading(true, 'message.updatingAll');
  startProgressPolling();
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

    if (res?.inventory_refresh_error) {
      showMessage(`${msgEl.textContent}\n${i18n.t('message.inventoryRefreshWarning', {
        error: res.inventory_refresh_error,
      })}`);
    }

    const results = Array.isArray(res?.results) ? res.results : [];
    const verifiedLatestCount = results.reduce((acc, result) => {
      if (!result?.ok) return acc;
      const pkg = findPackageInSnapshot(result?.name, result?.kind);
      return pkg && !pkg.outdated ? acc + 1 : acc;
    }, 0);
    if (verifiedLatestCount > 0) {
      alert(i18n.t('message.updateAllLatestConfirmed', { count: i18n.formatNumber(verifiedLatestCount) }));
    }

    await loadUpdateHistory();
  } catch (err) {
    showMessage(i18n.t('message.updateAllFailed', { error: formatOperationErrorMessage(err) }), true);
  } finally {
    stopProgressPolling();
    setLoading(false, 'message.loadingState');
    updateAllBtn.textContent = i18n.t('actions.updateAll');
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

if (packageSearchInputEl) {
  packageSearchInputEl.addEventListener('input', () => {
    packageSearchTerm = String(packageSearchInputEl.value || '').trim().toLowerCase();
    updateSearchControls();
    renderTable();
  });
}

if (packageSearchClearBtnEl) {
  packageSearchClearBtnEl.addEventListener('click', () => {
    if (!packageSearchInputEl) return;
    packageSearchInputEl.value = '';
    packageSearchTerm = '';
    updateSearchControls();
    renderTable();
    packageSearchInputEl.focus();
  });
}

if (packageTypeFilterEl) {
  packageTypeFilterEl.addEventListener('change', () => {
    packageTypeFilter = String(packageTypeFilterEl.value || 'all');
    updateSearchControls();
    renderTable();
  });
}

if (packageStatusFilterEl) {
  packageStatusFilterEl.addEventListener('change', () => {
    packageStatusFilter = String(packageStatusFilterEl.value || 'all');
    updateSearchControls();
    renderTable();
  });
}

if (packageFilterResetBtnEl) {
  packageFilterResetBtnEl.addEventListener('click', () => {
    packageSearchTerm = '';
    packageTypeFilter = 'all';
    packageStatusFilter = 'all';
    if (packageSearchInputEl) {
      packageSearchInputEl.value = '';
      packageSearchInputEl.focus();
    }
    updateSearchControls();
    renderTable();
  });
}

if (historySearchInputEl) {
  historySearchInputEl.addEventListener('input', () => {
    historySearchTerm = String(historySearchInputEl.value || '').trim().toLowerCase();
    renderHistoryPanel();
  });
}

if (historyKindFilterEl) {
  historyKindFilterEl.addEventListener('change', () => {
    historyKindFilter = String(historyKindFilterEl.value || 'all');
    renderHistoryPanel();
  });
}

if (historyStatusFilterEl) {
  historyStatusFilterEl.addEventListener('change', () => {
    historyStatusFilter = String(historyStatusFilterEl.value || 'all');
    renderHistoryPanel();
  });
}

if (historySortSelectEl) {
  historySortSelectEl.addEventListener('change', () => {
    historySortBy = String(historySortSelectEl.value || 'newest');
    renderHistoryPanel();
  });
}

if (historyFiltersClearBtnEl) {
  historyFiltersClearBtnEl.addEventListener('click', () => {
    historySearchTerm = '';
    historyKindFilter = 'all';
    historyStatusFilter = 'all';
    historySortBy = 'newest';
    if (historySearchInputEl) {
      historySearchInputEl.value = '';
      historySearchInputEl.focus();
    }
    renderHistoryPanel();
  });
}

checkNowBtn.addEventListener('click', checkNow);
updateAllBtn.addEventListener('click', updateAll);
if (updateSelectedBtn) updateSelectedBtn.addEventListener('click', updateSelected);
if (selectVisibleOutdatedBtn) selectVisibleOutdatedBtn.addEventListener('click', selectVisibleOutdated);
if (clearSelectionBtn) clearSelectionBtn.addEventListener('click', clearSelection);
if (loaderCancelBtn) loaderCancelBtn.addEventListener('click', cancelCurrentOperation);
checkAppUpdateBtn.addEventListener('click', () => checkAppUpdate({ silent: false }));
installAppUpdateBtn.addEventListener('click', installAppUpdate);
scheduleEnabledEl.addEventListener('change', updateSchedulerFieldVisibility);
scheduleFrequencyEl.addEventListener('change', updateSchedulerFieldVisibility);
saveScheduleBtn.addEventListener('click', saveScheduler);
detectBrewPathBtn.addEventListener('click', detectBrewPath);
saveBrewPathBtn.addEventListener('click', saveBrewPath);
clearBrewPathBtn.addEventListener('click', clearBrewPath);
window.addEventListener('beforeunload', () => {
  stopAutoAppUpdateChecks();
  if (typeof disposeAppUpdateProgress === 'function') {
    disposeAppUpdateProgress();
    disposeAppUpdateProgress = null;
  }
});

initColumnResizing();
initTableSorting();
updateSchedulerFieldVisibility();
updateSearchControls();
refreshStaticText();
initAppUpdateProgressBridge();

async function initializeApp() {
  await loadSettings();
  await loadUpdateHistory();
  applyActionAvailability();
  await loadState();
  renderAppUpdateStatus();
  await checkAppUpdate({ silent: true });
  startAutoAppUpdateChecks();
}

initializeApp();
