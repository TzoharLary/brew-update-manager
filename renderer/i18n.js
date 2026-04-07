(() => {
  const STRINGS = {
    en: {
      app: {
        title: 'Homebrew Update Manager',
        subtitle: 'Modern desktop manager for Homebrew packages',
      },
      actions: {
        languageLabel: 'Language',
        checkNow: 'Start check',
        updateAll: 'Update all outdated Homebrew packages',
        packageScope: 'These controls update Homebrew packages (formulae/casks), not the app version.',
      },
      updates: {
        title: 'App version updates',
        scopeHelp: 'This section updates the Homebrew Update Manager app itself (from GitHub Releases).',
        check: 'Check for app update',
        install: 'Download app installer',
        ready: 'App update channel is connected. Ready to check for new versions.',
        checking: 'Checking for Homebrew Update Manager app updates...',
        upToDate: 'Homebrew Update Manager is up to date (v{{current}}).',
        available: 'A new app version is available: v{{latest}} (current v{{current}}).',
        noInstallAsset: 'Update found but no installer asset was found for this Mac architecture.',
        failed: 'App update check failed: {{error}}',
        confirmInstall: 'Download and open the app installer for v{{latest}} now?',
        downloading: 'Downloading app installer...',
        downloaded: 'Installer downloaded and opened: {{path}}',
        downloadFailed: 'App update install step failed: {{error}}',
      },
      settings: {
        title: 'Automation & Environment',
        loading: 'Loading settings...',
        schedulerTitle: 'Automatic checks',
        enableScheduler: 'Enable automatic checks',
        frequency: 'Frequency',
        frequencyDaily: 'Daily',
        frequencyWeekly: 'Weekly',
        frequencyInterval: 'Every X hours',
        time: 'Time',
        weekday: 'Weekday',
        intervalHours: 'Interval (hours)',
        daySunday: 'Sunday',
        dayMonday: 'Monday',
        dayTuesday: 'Tuesday',
        dayWednesday: 'Wednesday',
        dayThursday: 'Thursday',
        dayFriday: 'Friday',
        daySaturday: 'Saturday',
        saveScheduler: 'Save schedule',
        schedulerSaved: 'Schedule saved.',
        schedulerFailed: 'Failed to save schedule: {{error}}',
        schedulerActive: 'Auto-check is enabled',
        schedulerInactive: 'Auto-check is disabled',
        summaryAutoEnabled: 'Auto-check: enabled',
        summaryAutoDisabled: 'Auto-check: disabled',
        summaryBrewPathSet: 'Brew path: configured',
        summaryBrewPathMissing: 'Brew path: not configured',
        summaryAppUpToDate: 'App status: up to date',
        summaryAppUpdateAvailable: 'App status: update available',
        summaryAppCheckFailed: 'App status: last check failed',
        summaryAppUnknown: 'App status: not checked yet',
        summaryAppUpdateCheckedAt: 'Last app update check: {{date}}',
        summaryAppUpdateNeverChecked: 'Last app update check: not checked yet',
        launchAgentPath: 'LaunchAgent: {{path}}',
        brewTitle: 'Homebrew path',
        brewPath: 'Custom brew path (optional)',
        detectBrewPath: 'Auto-detect path',
        saveBrewPath: 'Save path',
        clearBrewPath: 'Use automatic only',
        brewPathSaved: 'Brew path saved.',
        brewPathCleared: 'Using automatic brew detection.',
        brewPathSaveFailed: 'Failed to save brew path: {{error}}',
        brewPathScanning: 'Scanning this Mac for valid Homebrew paths...',
        brewPathDetected: 'Detected and verified path: {{path}}',
        brewPathScanSummary: 'Verified paths found: {{count}} • Scan time: {{time}}',
        brewPathDetectNone: 'Scan completed: no valid Homebrew executable was found. Verified paths found: {{count}} • Scan time: {{time}}',
        useDetectedConfirm: 'Use detected brew path?\n{{path}}',
      },
      history: {
        title: 'Recent package updates',
        help: 'Shows the latest package updates and when they were performed.',
        empty: 'No package updates have been recorded yet.',
        statusLatest: 'Verified latest',
        statusOk: 'Updated (verification pending)',
        statusFail: 'Update failed',
        kind: 'Type: {{kind}}',
        version: 'Version: {{version}}',
      },
      tabs: {
        outdated: 'Needs Update',
        installed: 'All Installed',
      },
      cards: {
        total: 'Installed Total',
        outdated: 'Outdated Total',
        formulae: 'Formulae',
        casks: 'Casks',
        outdatedFormulae: 'Outdated Formulae',
        outdatedCasks: 'Outdated Casks',
      },
      table: {
        loading: 'Loading...',
        package: 'Package',
        type: 'Type',
        currentVersion: 'Current Version',
        latestVersion: 'Latest Version',
        currentReleaseDate: 'Current Version Date',
        latestReleaseDate: 'Latest Version Date',
        daysOutdated: 'Days Outdated',
        daysValue: '{{days}} day(s)',
        status: 'Status',
        action: 'Action',
        noUpdates: 'No updates available 🎉',
        noPackages: 'No packages found.',
        showingOutdated: 'Showing {{count}} package(s) that need updates',
        showingAll: 'Showing all {{total}} installed package(s) ({{outdated}} outdated)',
      },
      status: {
        needsUpdate: 'Needs update',
        upToDate: 'Up to date',
      },
      buttons: {
        update: 'Update',
      },
      message: {
        loadingState: 'Loading package state...',
        runningCheck: 'Starting check (brew update + full scan)...',
        checkSuccess: 'Check completed successfully.',
        checkFailed: 'Check failed:\n{{error}}',
        loadFailed: 'Failed to load state:\n{{error}}',
        brewPathRequired: 'Set and save a Homebrew path in settings before using scans or updates.',
        updatingOne: 'Updating {{name}}...',
        updateOneSuccess: 'Updated {{name}} successfully.',
        updateOneLatestConfirmed: "Great news! '{{name}}' is now fully updated to version {{version}}.",
        updateOneFailed: 'Update failed for {{name}}:\n{{error}}',
        updateOneRequestFailed: 'Update request failed:\n{{error}}',
        updatingAll: 'Updating all outdated packages...',
        updateAllSuccess: 'Update all complete. success={{success}}, failed={{failed}}',
        updateAllLatestConfirmed: 'Done! {{count}} package(s) were verified as fully up to date.',
        updateAllFailed: 'Update-all failed:\n{{error}}',
      },
      progress: {
        panelHint: 'This live view shows exactly what the check is doing right now.',
        phaseLabel: 'Phase',
        currentLabel: 'Current item',
        etaLabel: 'ETA',
        summary: 'Progress {{done}} / {{total}} ({{percent}}%)',
        summaryUnknown: 'Progress information is being prepared...',
        currentNone: 'Preparing...',
        doneNow: 'Done',
        eta: 'ETA {{eta}}',
        noEta: 'Estimating…',
        phase: {
          idle: 'Idle',
          starting: 'Starting',
          preparing: 'Preparing',
          brew_update: 'Running brew update',
          collecting_outdated: 'Collecting outdated packages',
          collecting_installed: 'Collecting installed packages',
          resolving_dates: 'Resolving release dates',
          translating_descriptions: 'Translating descriptions',
          completed: 'Completed',
          error: 'Failed',
        },
      },
      confirm: {
        updateOne: "Update {{kind}} '{{name}}' now?",
        updateAll: 'Update ALL outdated packages now? This may take a while.',
      },
      common: {
        unavailable: 'Unavailable',
        unknown: 'unknown',
        dash: '—',
      },
      updatedAt: 'Last updated: {{date}}',
    },
    he: {
      app: {
        title: 'מנהל עדכוני Homebrew',
        subtitle: 'אפליקציית דסקטופ מודרנית לניהול חבילות Homebrew',
      },
      actions: {
        languageLabel: 'שפה',
        checkNow: 'התחל בדיקה',
        updateAll: 'עדכן את כל חבילות Homebrew המיושנות',
        packageScope: 'הכפתורים כאן מעדכנים חבילות Homebrew (חבילות בנייה/אפליקציות mac), ולא את גרסת האפליקציה.',
      },
      updates: {
        title: 'עדכוני גרסת אפליקציה',
        scopeHelp: 'החלק הזה מעדכן את אפליקציית Homebrew Update Manager עצמה (מ־GitHub Releases).',
        check: 'בדוק אם יש עדכון לאפליקציה',
        install: 'הורד מתקין אפליקציה',
        ready: 'ערוץ עדכוני האפליקציה מחובר. אפשר לבדוק גרסאות חדשות.',
        checking: 'בודק עדכונים לאפליקציית Homebrew Update Manager...',
        upToDate: 'אפליקציית Homebrew Update Manager מעודכנת (v{{current}}).',
        available: 'נמצאה גרסה חדשה לאפליקציה: v{{latest}} (נוכחי v{{current}}).',
        noInstallAsset: 'נמצא עדכון, אבל לא נמצא קובץ התקנה מתאים לארכיטקטורה של המק הזה.',
        failed: 'בדיקת עדכון אפליקציה נכשלה: {{error}}',
        confirmInstall: 'להוריד ולפתוח עכשיו מתקין אפליקציה לגרסה v{{latest}}?',
        downloading: 'מוריד מתקין אפליקציה...',
        downloaded: 'המתקין הורד ונפתח: {{path}}',
        downloadFailed: 'שלב התקנת עדכון האפליקציה נכשל: {{error}}',
      },
      settings: {
        title: 'אוטומציה וסביבה',
        loading: 'טוען הגדרות...',
        schedulerTitle: 'בדיקות אוטומטיות',
        enableScheduler: 'הפעל בדיקות אוטומטיות',
        frequency: 'תדירות',
        frequencyDaily: 'יומי',
        frequencyWeekly: 'שבועי',
        frequencyInterval: 'כל X שעות',
        time: 'שעה',
        weekday: 'יום בשבוע',
        intervalHours: 'מרווח (שעות)',
        daySunday: 'ראשון',
        dayMonday: 'שני',
        dayTuesday: 'שלישי',
        dayWednesday: 'רביעי',
        dayThursday: 'חמישי',
        dayFriday: 'שישי',
        daySaturday: 'שבת',
        saveScheduler: 'שמור תזמון',
        schedulerSaved: 'התזמון נשמר.',
        schedulerFailed: 'שמירת התזמון נכשלה: {{error}}',
        schedulerActive: 'בדיקה אוטומטית פעילה',
        schedulerInactive: 'בדיקה אוטומטית כבויה',
        summaryAutoEnabled: 'בדיקה אוטומטית: פעילה',
        summaryAutoDisabled: 'בדיקה אוטומטית: כבויה',
        summaryBrewPathSet: 'נתיב brew: הוגדר',
        summaryBrewPathMissing: 'נתיב brew: לא הוגדר',
        summaryAppUpToDate: 'סטטוס אפליקציה: מעודכנת',
        summaryAppUpdateAvailable: 'סטטוס אפליקציה: קיים עדכון',
        summaryAppCheckFailed: 'סטטוס אפליקציה: בדיקה אחרונה נכשלה',
        summaryAppUnknown: 'סטטוס אפליקציה: עדיין לא נבדקה',
        summaryAppUpdateCheckedAt: 'בדיקת עדכון אחרונה לאפליקציה: {{date}}',
        summaryAppUpdateNeverChecked: 'בדיקת עדכון אחרונה לאפליקציה: עדיין לא בוצעה',
        launchAgentPath: 'קובץ LaunchAgent: {{path}}',
        brewTitle: 'נתיב Homebrew',
        brewPath: 'נתיב brew מותאם (לא חובה)',
        detectBrewPath: 'אתר נתיב אוטומטית',
        saveBrewPath: 'שמור נתיב',
        clearBrewPath: 'חזור לזיהוי אוטומטי',
        brewPathSaved: 'נתיב brew נשמר.',
        brewPathCleared: 'המערכת תחפש brew אוטומטית.',
        brewPathSaveFailed: 'שמירת נתיב brew נכשלה: {{error}}',
        brewPathScanning: 'מבצע סריקה במחשב כדי לאתר נתיבי Homebrew תקינים...',
        brewPathDetected: 'נתיב שזוהה ואומת: {{path}}',
        brewPathScanSummary: 'נמצאו נתיבים מאומתים: {{count}} • זמן סריקה: {{time}}',
        brewPathDetectNone: 'הסריקה הסתיימה: לא נמצא קובץ Homebrew תקין להרצה. נמצאו נתיבים מאומתים: {{count}} • זמן סריקה: {{time}}',
        useDetectedConfirm: 'להשתמש בנתיב שזוהה?\n{{path}}',
      },
      history: {
        title: 'חבילות שעודכנו לאחרונה',
        help: 'כאן אפשר לראות אילו חבילות עודכנו לאחרונה ומתי זה קרה.',
        empty: 'עדיין לא נרשמו עדכוני חבילות.',
        statusLatest: 'אומת: הגרסה הכי עדכנית',
        statusOk: 'עודכן (בדיקת גרסה בהמתנה)',
        statusFail: 'העדכון נכשל',
        kind: 'סוג: {{kind}}',
        version: 'גרסה: {{version}}',
      },
      tabs: {
        outdated: 'דורש עדכון',
        installed: 'כל המותקנות',
      },
      cards: {
        total: 'סה״כ מותקנות',
        outdated: 'סה״כ מיושנות',
        formulae: 'חבילות בנייה',
        casks: 'אפליקציות mac',
        outdatedFormulae: 'חבילות בנייה מיושנות',
        outdatedCasks: 'אפליקציות mac מיושנות',
      },
      table: {
        loading: 'טוען...',
        package: 'חבילה',
        type: 'סוג',
        currentVersion: 'גרסה נוכחית',
        latestVersion: 'גרסה אחרונה',
        currentReleaseDate: 'תאריך גרסה נוכחית',
        latestReleaseDate: 'תאריך גרסה אחרונה',
        daysOutdated: 'ימים מאז מיושן',
        daysValue: '{{days}} ימים',
        status: 'סטטוס',
        action: 'פעולה',
        noUpdates: 'אין עדכונים זמינים 🎉',
        noPackages: 'לא נמצאו חבילות.',
        showingOutdated: 'מוצגות {{count}} חבילות שדורשות עדכון',
        showingAll: 'מוצגות כל {{total}} החבילות המותקנות ({{outdated}} מיושנות)',
      },
      status: {
        needsUpdate: 'דורש עדכון',
        upToDate: 'מעודכן',
      },
      buttons: {
        update: 'עדכן',
      },
      message: {
        loadingState: 'טוען מצב חבילות...',
        runningCheck: 'מתחיל בדיקה (brew update + סריקה מלאה)...',
        checkSuccess: 'הבדיקה הסתיימה בהצלחה.',
        checkFailed: 'הבדיקה נכשלה:\n{{error}}',
        loadFailed: 'טעינת המצב נכשלה:\n{{error}}',
        brewPathRequired: 'צריך להגדיר ולשמור נתיב Homebrew בהגדרות לפני שאפשר לבצע סריקות או עדכונים.',
        updatingOne: 'מעדכן את {{name}}...',
        updateOneSuccess: '{{name}} עודכנה בהצלחה.',
        updateOneLatestConfirmed: "עדכון מעולה! '{{name}}' עכשיו בגרסה הכי עדכנית: {{version}}.",
        updateOneFailed: 'העדכון נכשל עבור {{name}}:\n{{error}}',
        updateOneRequestFailed: 'בקשת העדכון נכשלה:\n{{error}}',
        updatingAll: 'מעדכן את כל החבילות המיושנות...',
        updateAllSuccess: 'עדכון כולל הסתיים. הצליחו={{success}}, נכשלו={{failed}}',
        updateAllLatestConfirmed: 'עדכון הושלם! אומתו {{count}} חבילות כמעודכנות לגרסה הכי חדשה.',
        updateAllFailed: 'עדכון כולל נכשל:\n{{error}}',
      },
      progress: {
        panelHint: 'החלון הזה מציג בזמן אמת מה הבדיקה עושה כרגע.',
        phaseLabel: 'שלב',
        currentLabel: 'פריט נוכחי',
        etaLabel: 'זמן משוער',
        summary: 'התקדמות {{done}} / {{total}} ({{percent}}%)',
        summaryUnknown: 'פרטי ההתקדמות עדיין נטענים...',
        currentNone: 'מכין נתונים...',
        doneNow: 'הושלם',
        eta: 'זמן משוער {{eta}}',
        noEta: 'מחשב זמן…',
        phase: {
          idle: 'ממתין',
          starting: 'מתחיל',
          preparing: 'מכין נתונים',
          brew_update: 'מריץ brew update',
          collecting_outdated: 'אוסף חבילות מיושנות',
          collecting_installed: 'אוסף חבילות מותקנות',
          resolving_dates: 'מחשב תאריכי גרסאות',
          translating_descriptions: 'מתרגם תיאורים',
          completed: 'הסתיים',
          error: 'נכשל',
        },
      },
      confirm: {
        updateOne: "לעדכן עכשיו את {{name}} מסוג {{kind}}?",
        updateAll: 'לעדכן עכשיו את כל החבילות המיושנות? זה עשוי לקחת זמן.',
      },
      common: {
        unavailable: 'לא זמין',
        unknown: 'לא ידוע',
        dash: '—',
      },
      updatedAt: 'עודכן לאחרונה: {{date}}',
    },
  };

  function dotGet(obj, path) {
    return path.split('.').reduce((acc, key) => (acc && acc[key] !== undefined ? acc[key] : undefined), obj);
  }

  function interpolate(template, params = {}) {
    return String(template).replace(/\{\{\s*(\w+)\s*\}\}/g, (_, key) => {
      if (params[key] === undefined || params[key] === null) return '';
      return String(params[key]);
    });
  }

  function detectDefaultLang() {
    const saved = localStorage.getItem('bum_language');
    if (saved === 'he' || saved === 'en') return saved;
    const nav = navigator.language || 'en';
    return nav.toLowerCase().startsWith('he') ? 'he' : 'en';
  }

  const i18n = {
    lang: detectDefaultLang(),
    t(key, params = {}) {
      const primary = dotGet(STRINGS[this.lang], key);
      const fallback = dotGet(STRINGS.en, key);
      const value = primary ?? fallback ?? key;
      return interpolate(value, params);
    },
    setLang(nextLang) {
      this.lang = nextLang === 'he' ? 'he' : 'en';
      localStorage.setItem('bum_language', this.lang);
      document.documentElement.lang = this.lang;
      document.documentElement.dir = this.lang === 'he' ? 'rtl' : 'ltr';
    },
    formatDate(value) {
      if (!value) return this.t('common.unavailable');
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return value;
      return new Intl.DateTimeFormat(this.lang === 'he' ? 'he-IL' : 'en-US', {
        dateStyle: 'medium',
        timeStyle: 'short',
      }).format(d);
    },
    formatNumber(value) {
      const langCode = this.lang === 'he' ? 'he-IL' : 'en-US';
      return new Intl.NumberFormat(langCode).format(Number(value || 0));
    },
    applyStatic() {
      document.querySelectorAll('[data-i18n]').forEach((node) => {
        const key = node.getAttribute('data-i18n');
        if (!key) return;
        node.textContent = this.t(key);
      });
    },
  };

  i18n.setLang(i18n.lang);
  window.brewI18n = i18n;
})();
