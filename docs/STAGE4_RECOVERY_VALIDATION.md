# שלב 4 — ביצוע ושחזור: מסירה ואימות מבודד

תאריך: 25.10.2026. ענף: `feat/unified-intel-134-foundations`.
בסיס ההשלמה: `b958280`; מסמך זה מתאר את השינויים שנוספו מעליו.

**שלב 4 הושלם ברמת קוד ובדיקות מבודדות.** T03/T04/T07 בכללותן נשארות
פתוחות: גילוי והשוואה נוספים, איחוד הממשק הכללי, חבילת הבדיקות המלאה,
אריזה וקבלת Mac Intel הם שלבים נפרדים. אין כאן אישור `--full` או הפצה.

## תרחישים ותוצאות

| תחום | ההתנהגות שנבדקה | הראיה |
| --- | --- | --- |
| גיבוי | כשל העתקה או process exit לפני receipt משאירים מקור שניתן לאמת מול snapshot טרום הגיבוי; מקור ששונה אינו מסומן כמשוחזר | recovery integration: backup copy failure / process exit |
| התקנה ואימות | כשל install/smoke עוצר לפני הסרת מקור; אין resume עיוור להתקנה שלא סומנה גמורה | migration recovery: install, smoke, incomplete install |
| ניתוב | מתועדת כוונה לפני כל כתיבת shim; rollback מטפל גם במצב לפני/אחרי הכתיבה והחלפת המצביע | process exits ב־generations_ready, shim_write_pending, shim_written, pointer_switch_pending ואחרי switch |
| הסרת מקור | הסרה חלקית ושגיאת timeout נשארות needs_attention; קוד 0 עם keg קיים אינו הצלחה; מקור שהשתנה אינו מוסר | recovery: partial/noop uninstall, independent edit, real timeout process |
| שחזור מקור | מוחזרים קבצים חסרים בלבד מתוך גיבוי מאומת; תוכן, קישורים והרשאות שנשארו חייבים להתאים; אין force/overwrite | manifests, partial/full removal, symlink/extra-file/edit rejection, relink failure/retry |
| בעלות ו־Pixi metadata | לפני commit נשמרות תמונות before/after; rollback מחזיר רק את הרשומה של העסקה ומשמר חבילות אחרות | metadata crash matrix: record intent/write, ownership intent/write/receipt |
| שחזור שנקטע | ניסיון חוזר מזהה מצביע, shims ורשומות שכבר שוחזרו; אינו מוחק שינוי עצמאי או חוזר על package install/uninstall | process exits אחרי restore ניתוב, installed.json ו־routes.json; idempotent multi-command routing |
| ביטול | checkpoints לפני שלבי המוטציה; פיצוי אינו יורש scan cancellation; תהליך פקודה מסתיים לפני שחרור הפעולה | cancellation בכל checkpoint, סיום תהליך Python מדומה ובדיקת PID שנקצר |
| תוכניות recovery | שינוי מקור/metadata אחרי preview מחייב תוכנית חדשה; שינוי shim או בעלות חוסם דריסה | stale recovery plan, edited shim/mode, changed ownership |
| בקשת העדכון | resume/rollback מיישבים את תוצאת הבקשה המקורית; rollback אינו נספר כעדכון מוצלח; פעולות שלא בוצעו נדחות | batch resume/rollback, read-only status projection אחרי crash, deferred items, replay |
| HTTP | handlers אמיתיים ו־operation lock עם ספקים מדומים: review/approve/status, שגיאה מובנית 409, כשל refresh אחרי שחזור אינו מוחק הצלחה פיזית | localhost ThreadingHTTPServer, recovery integration |
| ממשק recovery | review אינו מפעיל שינוי; לחיצה כפולה אינה שולחת פעמיים; conflict/תגובה שאבדה גוררים refresh בלי retry | recovery-ui.cjs, שמונה תרחישים בעברית ובאנגלית |

## מה הורץ

- **100 בדיקות Python ממוקדות עברו**: 35 migration recovery, 16 recovery
  integration, 10 migration engine, 27 unified updates, 6 ownership,
  5 routing, בדיקת operation lock אחת.
- **8 תרחישי recovery UI עברו** ב־Node/VM עם DOM ו־IPC מבוקרים.
- `stale-plan-ui.cjs` עבר.
- בדיקת תחביר לקבצים ששונו, `git diff --check` ובדיקת התאמת Bundle 004
  מתועדות במסירה; לא הורצה התקנה או אריזה במסגרת שלב זה.

הבדיקות פועלות בתיקיות זמניות, עם ספקים מדומים. בדיקות routing, כתיבת
metadata ויומנים משתמשות בקוד האמיתי; בדיקות HTTP משתמשות בשרת מקומי אמיתי.
בדיקות process exit מפעילות fork ו־os._exit; הן מדולגות בפלטפורמה ללא fork.
בדיקות timeout/cancel מפעילות תהליכי Python ייעודיים בלבד, לא מנהלי חבילות.

## מסירת הבדיקות והמשך

שני קובצי הבדיקות החדשים נכללים ב־unittest discovery של `--checks` וב־CI
הממוקד. `recovery-ui.cjs` ו־`stale-plan-ui.cjs` מופעלים כעת גם בסקריפט
המרכזי, מעבר לבדיקת תחביר. הגנרטור מכיל את אותם קבצים ושומר על הגנות
baseline/restore; אין שינוי במניפסטי שחזור קיימים.

זו אינה ריצת `--checks` מלאה או Electron מלא, ואינה קבלה אמיתית של
Homebrew/MacPorts/Pixi. אין שינוי לחבילות המותקנות או לאפליקציה 1.3.3.
transactions היסטוריות שחסרה בהן ראיית before/after אינן מקבלות היתר
להסיר בעלות committed לא מזוהה; הן נשארות חסומות במקום להמציא שחזור בטוח.

**נקודת ההמשך: שלב 5 — גילוי והשוואת ספקים**, לפי סדר העבודה שבמסמך
המשימות. קבלת המוצר והבדיקות המלאות יימסרו למשתמש בשלב המסירה המתאים.
