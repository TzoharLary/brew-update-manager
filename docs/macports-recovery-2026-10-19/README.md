# תיקון MacPorts ואיחוד Node — 19.10.2026

**הטיפול הושלם:** MacPorts 2.12.7 מותקן ומתאים ל־macOS 26 Tahoe ול־Intel. Node 26.12.0 נשאר ההתקנה הפעילה, וקג Homebrew השבור 26.0.0 הוסר לאחר גיבוי ובדיקת צרכנים.

## MacPorts

- מסד הרישום הישן נפתח לקריאה בלבד: נמצאו אפס ports ואפס תלויות. לכן לא היה אוסף חבילות שהיה צריך להתקין מחדש אחרי שדרוג מערכת ההפעלה.
- הורד המתקין הרשמי `MacPorts-2.12.7-26-Tahoe.pkg` מהמהדורה הרשמית ב־GitHub. SHA-256 הושווה גם ל־digest של asset וגם לקובץ checksums של המפרסם: `588f14ccd3c6199559a070a80e6cddd6871721a08cc434775bf54378fd667910`.
- macOS אישר חתימת Developer ID Installer של Joshua Root ‏(QTA3A3B7F3) ו־notarization מהימן. תנאי החבילה כוללים macOS 26 ו־x86_64/arm64. קובץ Tcl של MacPorts נבדק ומכיל חתך x86_64.
- גובו מסד הרישום, תצורת MacPorts וקובצי startup של המשתמש לפני ההתקנה. ההתקנה בוצעה בהרשאת מנהל באמצעות מנגנון ההזדהות של macOS, בלי לקבל סיסמה בצ׳אט.
- ניסיון התקנה מתוך Documents נכשל בגישה לקובץ. אותו מתקין מאומת הועתק לנתיב זמני נגיש תחת `/private/tmp` והותקן בהצלחה. `installer-staged.log` כולל `The upgrade was successful`.
- המתקין רענן את עץ החבילות: PortIndex הישן מ־2024 הוחלף בקטלוג מ־19.10.2026. לא הותקנו ports ולא הופעל `port migrate` או עדכון חבילות קיים.
- `macports.conf` הוגדר ל־`buildfromsource never`: התקנות ports רגילות ידרשו ארכיון מוכן וייעצרו אם אינו זמין. קובץ התצורה הקודם נשמר גם בסיומת `.pre-binary-only-20261019`.
- בדיקות: `port version`, ‏`port installed`, ‏`port info wget` ו־`port -N -b -y install wget` עברו. מספר ports נשאר אפס. ה־dry-run חישב תלויות ודילג על ביצוע; הוא אינו אימות שקיימים כל הארכיונים או שכלי מסוים נבדק בהרצה.

התקנת MacPorts הבסיסית והקטלוג תקינים. עדיין לא הועברו חבילות Homebrew ל־MacPorts, ובדיקת סגירת הארכיונים הבינאריים לכל קבוצת חבילות היא שלב נפרד.

## Node

- `/usr/local/bin/node` הישן הצביע לקג Homebrew ‏26.0.0, שנכשל בטעינת `libada.3.dylib`. הפקודה הפעילה דרך המנהל כבר הייתה Node 26.12.0 תקין.
- סריקת קבלות מצאה רק דרישת Node עקיפה בקבלת MongoDB 8.2.9, ללא `declared_directly`. רכיב mongosh שקושר את השרשרת אינו מותקן כרגע בנתיבים שנבדקו. `brew uses --installed node` לא החזיר צרכנים; נתון זה לבדו אינו מכסה את נוסחת MongoDB שהושמטה ממלאי Brew.
- `otool -L` על mongod/mongos לא מצא קישור ל־Node או libnode; `mongod --version` עבר. לא נמצאו הפניות ל־Node הישן ב־Program/ProgramArguments של LaunchAgents/LaunchDaemons שנבדקו. אין כאן בדיקה של כל סקריפט או פרויקט במחשב.
- גובו קג Node ואומתו 1,876 קבצים רגילים וקישורים לפני הסרה. הוסר באמצעות `brew uninstall --formula node`, בלי התעלמות מתלויות ובלי autoremove.
- `/usr/local/bin/node` נוצר כקישור לפקודת Node המנוהלת הקיימת. כך נשמר תאימות לנתיב המפורש בלי להחזיק executable נוסף. נתוני npm הגלובליים נשמרו.
- בדיקות לאחר ההסרה: Node 26.12.0, קוד URL ב־Node, ‏npm בנתיב הישן וגרסת mongod עברו. npm הישן 11.17.0 נשאר בנתיב הגלובלי; סביבת המנהל מספקת npm 11.20.0. התקנות npm לא אוחדו במסגרת הפעולה הזאת.
- מלאי האפליקציה רוענן: Node המנוהל נשאר מוצג עם `brew_installation_present=false`. שתי החרגות ngrok נשמרו. לא הופעלה פעולת עדכון/הסרה עבור ngrok.

נתוני MongoDB לא נקראו או הועברו, ולא הופעל שרת מול מסד נתונים לצורך הבדיקה. עדכון עתידי של נוסחת MongoDB דרך Homebrew עשוי לדרוש שוב את שרשרת התלויות שלו; הסרת הקג אינה משנה את מתכון הנוסחה. תכנון המעבר העתידי צריך להביא זאת בחשבון.

## ראיות ושחזור

- [קבלת טיפול ונתיבי הגיבוי](recovery-receipt.json)
- [תוצאת התקנת MacPorts](installer-staged.log), [בדיקות MacPorts](macports-validation.json)
- [צרכני Node בקבלות](node-receipt-consumers.json), [הפניות משירותים](node-launch-references.json), [בדיקות Node לאחר האיחוד](node-after.json)
- [מלאי האפליקציה לאחר הסרת קג Node](app-state-after-node.json)
- [מטא־דאטה של קובצי המתקין](release-assets.json)

קובצי גיבוי נשמרים תחת `~/.local/share/brew-update-manager/recovery/` בנתיב המדויק שבקבלה. הם לא נמחקו. הסקריפט `tools/install-macports-tahoe.command` נשמר כאפשרות הרצה ידנית של אותו מתקין; אין צורך להריץ אותו שוב לאחר הצלחת הטיפול.

מקורות: [מתקין MacPorts הרשמי](https://www.macports.org/install.php), [משמעות port migrate](https://man.macports.org/port-migrate.1.html), [מדיניות התקנות בינאריות](https://guide.macports.org/#internals.configuration-files.macports-conf).
