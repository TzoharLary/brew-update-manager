# מפת מימוש ומסירת בדיקות — Mac Intel 1.3.4

עודכן: 25.10.2026. בסיס סקירת המקור: `0a0849b`, לאחר fetch של ענף
`feat/unified-intel-134-foundations`. `main` עדיין ב־`a682e4d`.
זהו עדכון תיעוד בלבד: לא הורצו בדיקות מוצר, לא הוחלפה אפליקציה ולא שונו חבילות.

[דוח המימוש](UNIFIED_IMPLEMENTATION_STATUS.md) הוא מקור המצב הנוכחי;
[תוכנית היסוד](INTEL_UPDATE_IMPLEMENTATION_PLAN.md) מגדירה דרישות;
[מסמך המשימות](MAC_MANAGER_TASKS.md) קובע את סדר ההמשך.
המונח ״קיים״ בטבלה הוא ממצא קריאת מקור, ואינו מציין שהדרישה התקבלה במחשב.

## מפת תוכנית היסוד לקוד ולבדיקות

| רכיב בתוכנית | קיים במקור | בדיקות קיימות / ראיות | נותר פתוח |
| --- | --- | --- | --- |
| א — חבילת שינוי ושחזור קוד | `tools/implement-mac-manager.py`, Bundle 004 | CI כולל `unified-check` ו־packaging smoke | replay/restore בעותק זמני, סירוב לדרוס עריכה מאוחרת ועקביות החבילה הנמסרת |
| ב — הצעה, החלטה ובעלות | `backend/update_selection.py`, `backend/provider_ownership.py` | `test_update_selection.py`, `test_provider_ownership.py` | כיסוי מלא של מלאי ללא כפילות ועדכון אצל ספק חדש אחרי restart |
| ג — גילוי ובחירת ספק | `backend/unified_updates.py`, `backend/pixi_provider.py`, `backend/macports_provider.py` | בדיקות unified/selection/Pixi/MacPorts; שלב 3 כולל מקרה Brew עדכני מול Pixi חדש יותר | רגרסיות completeness, מידע חסר ומיפויים; אין כיסוי מעבר לכל חבילה או קבלת Cask |
| ד — תוכנית, נעילה וחזרתיות | `generate_plan`, `validate_request`, `_revalidate_migration`, `execute` ב־`unified_updates.py`; `backend/operation_lock.py` | `test_unified_updates.py`, `test_operation_lock.py`, `stale-plan-ui.cjs` | ניתוק וחזרה, תחרות תהליכים ותגובה מאוחרת לאורך מסלול HTTP/UI מלא |
| ה — מנוע מעבר והפעלה | `backend/migration_engine.py`, `backend/routing_activation.py` | `test_migration_engine.py`, `test_routing_activation.py` | תרחיש מלא דרך המבצע המאוחד: backup → install → smoke → activation → cleanup → ownership |
| ו — כשל ושחזור | `_compensate`, `recovery_plan`, `approve_recovery` ויומן במנוע | בדיקות מניפסט, שינוי עצמאי, lock ומזהים; אינן מכסות את כל רצף ההתאוששות | **שלב העבודה הבא:** fault-injection, הסרה חלקית, restart, resume/rollback ושימור שינוי עצמאי |
| ז — פרופילים ושימוש | `backend/migration_profiles.py`; Pixi מוגבל ל־`jq`/`pandoc`, MacPorts לפרופילים המוגדרים | `test_migration_profiles.py`, בדיקות smoke במנוע | קבלה אמיתית של IWYU ו־Pixi, פקודות ב־PATH הרגיל אחרי restart |
| ח — מלאי, API וממשק | `backend/brew-updates-service.py`, `electron/preload.js`, `renderer/app.js` | בדיקות transport HTTP ותרחישי Electron קיימים; `stale-plan-ui.cjs` נפרד | bypass של legacy, 409 מובנה, אישור עדכון יחיד, תרגומים ומסלולי selected/all מלאים |
| ט — בדיקות | `tools/verify-current-code.py`, תיקיית `tests` | unittest discovery, תחביר ותרחישי UI קיימים | השלמת התרחישים המחייבים בתוכנית; קיום קובץ אינו הוכחת כיסוי הדרישה |
| י — סקריפט וקבלת משתמש | `tools/verify-migration.py --checks/--full` | שמירת דוחות וחתימות מקור קיימת בקוד | `--checks` על המקור הנוכחי בידי המשתמש; `--full` רק אחרי סגירת פערי הקוד והבדיקות |

ה־CI המוזכר בדוח המימוש (`39458648017`, בסיס `555ae7b`) הוא ראיה היסטורית
לבדיקות ממוקדות בלבד. הוא אינו תוצאה על `0a0849b`. בעדכון זה לא אומת מצב CI
חדש ב־GitHub, ואין כאן טענה שכל בדיקות שלב 3 עברו ב־HEAD הנוכחי.

## פקודת בדיקות הקוד למשתמש

הפקודה הבאה מיועדת לבדיקות המקור הקיימות בלבד. אפשר להריץ אותה מהענף
המקומי `codex/unified-status-handoff`, שנוצר מבסיס הסקירה לעיל:

```bash
/usr/local/bin/python3.14 "/Users/tzoharlary/Documents/Projects/brew-update-manager/tools/verify-migration.py" --checks
```

תנאי הקדם שהסקריפט בודק: macOS Intel מקורי, Python 3.14, Node וכלי macOS,
ו־electron-builder שכבר מותקן מקומית. ריצת הבדיקות דורשת גם את Electron
המקומי. חסר כלי נרשם בדוח; אין התקנת תלויות אוטומטית.

`--checks` מפעיל את `verify-current-code.py` עם:

- בדיקת תחביר Python ו־JavaScript וקובצי הבדיקה הרלוונטיים.
- כל בדיקות `test_*.py` הקיימות דרך unittest discovery.
- `service-http.cjs`: בדיקת transport באמצעות שרת HTTP מקומי ומבוקר;
  זו אינה בדיקת כל routes של backend האמיתי.
- `progress-ui.cjs` ו־`operation-results-ui.cjs` ב־Electron עם נתונים מבודדים.
- `git diff --check` וחתימות קובצי המקור.

הסקריפט אינו מריץ בנפרד את `stale-plan-ui.cjs`, את `unified-check`, אריזה,
או את כל תרחישי הכשל/הממשק שעדיין נדרשים בתוכנית. בדיקת תחביר JS אינה
הרצת תרחיש JS. הפער הזה צריך להיסגר בשלב הרחבת חבילת הבדיקות לפני מסירת
קבלת מוצר מלאה; אין לפרש `--checks=pass` כאישור לכל דרישות התוכנית.

## דוחות ופירוש תוצאות

הדוחות נשמרים תחת:

```text
~/.local/share/brew-update-manager/recovery/unified-intel-1.3.4/runs/
```

הסקריפט החיצוני יוצר `run-<id>/summary.json`, `summary.md`,
`code-manifest.json` ולוג `isolated-code-suite.log`. ריצת הבדיקות הפנימית
יוצרת גם `checks-<id>/summary.json`, `summary.md`, `code-sha256.json`
ולוג נפרד לכל שלב. בדוח החיצוני שעבר, `checks_report` מפנה לדוח הפנימי.
אם הריצה נכשלה, יש לבדוק את שני הדוחות ואת הלוג; אין צורך להעתיק פלט ארוך.

ב־`--checks` מוצלח `code_checks=pass`; שדות `packaged_app`,
`simulated_scenarios`, `live_migration`, `restart_verification` נשארים
`not_run` בדוח החיצוני. בדיקות fixture קיימות עשויות לרוץ בתוך unittest,
אך אין להסיק מכך כיסוי של כל הסימולציות המתוכננות.

קודי היציאה החיצוניים במימוש הנוכחי: `0` הצלחה, `1` כשל, `2` חסימה.
הדוח הפנימי יכול לרשום `unverified` וקוד `2`, למשל ב־timeout; העטיפה
מתרגמת כרגע כל יציאה לא־אפס של התהליך הפנימי לכשל וקוד `1`.
לכן צריך לשמר את תוצאת הדוח הפנימי ולא להסיק כשל בדיקה מאומת מקוד העטיפה בלבד.
התאמת סיווג השגיאות לדרישת התוכנית נשארת פתוחה בשלב הסקריפט.

## גבול קבלה והמשך עבודה

`--full` כבר קיים, ו־`--acceptance` נכנס לאותו מסלול. הוא עשוי לגבות ולהחליף
את `/Applications/Mac Update Manager.app` **לפני** אישור YES לשינוי חבילות.
אין להריץ אותו לצורך בדיקות קוד או כקבלה מאושרת בשלב הנוכחי.

המשך הפיתוח המדויק הוא שלב 4 ב־fixtures בלבד: בדיקות דרך `unified_updates.execute`
לכשל בכל נקודת עסקה, ובפרט הסרה חלקית כשה־keg עדיין קיים. במקור הנוכחי
`_compensate` מעתיק את גיבוי המקור רק אם תיקיית המקור חסרה; קיומה לבדו אינו
מוכיח שתוכנה שלם. יש להוכיח שחזור בטוח או חסימה עם `needs_attention`, בלי
לדרוס שינוי עצמאי, ולבחון resume לאחר restart. זהו פער סקירה, לא תוצאת ניסוי כשל.

לאחר מכן: השלמת גילוי/השוואה, API/UI, חבילת בדיקות וגנרטור/אריזה,
ורק אז קבלת משתמש אמיתית. T03/T04/T07 נשארות פתוחות. T05/T06 מחוץ לשלב זה.
