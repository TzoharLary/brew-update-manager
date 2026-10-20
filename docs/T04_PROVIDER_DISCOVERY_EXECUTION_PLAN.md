# T04 — תוכנית ביצוע: גילוי והשוואת ספקי עדכון ל־Mac Intel

תאריך: 20.10.2026. מיושם בענף `feat/unified-intel-134-foundations` בלבד. מבוסס על [התוכנית המחייבת](INTEL_UPDATE_IMPLEMENTATION_PLAN.md), סעיף 7, ועל [דוח המצב](UNIFIED_IMPLEMENTATION_STATUS.md).

## יעד ומסירה

סריקה יחידה מציגה לכל התקנה פיזית מועמדות מהספק הנוכחי ומחלופות מוכרות Homebrew/MacPorts/Pixi גם אם Homebrew אינו מסמן `outdated`. בחירה היא גרסת upstream היציבה והגבוהה ביותר **מבין מסלולים בינאריים מאומתים שניתן לבצע**, ובשוויון שומרים בעלים נוכחי. סטטוס ספק שנכשל נשמר כמידע חסר, ללא המצאת ״העדכני ביותר״. פעולה לא נתמכת נשארת ברשימת חלופות כחסומה.

## סדר צעדי המימוש

1. **איסוף ואמת־מקור**: להוציא איסוף חלופות לפונקציה שניתן לבדוק עם fake service; לאסוף Homebrew גם כשה־outdated=false; MacPorts גם אם אין `offer` ישן ורק למיפוי מאומת; Pixi למיפויי CLI בעלי התאמה גם כשהמקור Homebrew. לשמור זהויות `homebrew:formula`, `macports:`, `pixi:` נפרדות. Cask מקבל חסם מפורש ולא מסלול משוער.
2. **T02 ו־scope**: עבור כל אפשרות מעבר לבצע סיווג מקור עדכני מתוך `installation_evidence` המשותף, ללא חיפוש פרויקטים פרטיים; לאפשר רק standalone ללא consumers/roles/gaps, וללא טענת התאמה אם הסריקה חלקית. לא להשתמש במיפוי שם או `--force-bottle` לבדם כראיה. להחריג ngrok ותלויות מוחרגות.
3. **בינאריים/ביצוע**: לשמר preflight Homebrew bottle ו־MacPorts archives, ולגלות Pixi osx-64 עם lock מדויק, provenance, hashes וסגירת תלויות. מסלול מעבר `ready` ידרוש פרופיל מותקן, preflight מאומת, גיבוי, בדיקת שימוש, הפעלה אטומית, cleanup ושחזור; ללא כל אלה `execution_supported=false` והסיבה מוצגת. אין לגזור נתיבי shell ממשתמש או מהרשת.
4. **אלגוריתם/תצוגה**: לגרום ל־`generate_plan` להזין `update_selection.select` בכל החלופות; לתת `discovery_complete` אמת רק כאשר שלושת המסלולים הרלוונטיים נבדקו או ידוע שאינם רלוונטיים; שגיאה בספק יחיד אינה חוסמת עדכון כשיר מספק אחר. להוסיף `reason_codes` מובחנים ותיאורי מסלול בעברית/אנגלית.
5. **בדיקות וביקורת**: fixtures לכל מצב (Brew current with newer port, Brew binary available but Port higher, Pixi newer but unsupported, failed provider + valid provider, excluded, duplicate physical install, version suffix, stale plan). בדיקות מבודדות בלבד; CI syntax+tests focused+guarded `unified-check`. לבחון hash generator vs emitted files, תיעוד, היעדר שינויי התקנה אמיתיים, ו־`main` שלא שונה.

## גבולות בטיחות מפורשים

- אין מעבר אוטומטי לספק אחר שאין לו פרופיל עסקה בטוח; מיפוי `PIXI_TOOLS` לבדו אינו הרשאה להסרת Homebrew.
- כאשר פיילוט מעבר מלא תלוי בתיקוני T03 (recovery/ownership), העבודה תחסום את הביצוע ותציין חסם ממוקד ולא תכריז על T04 סגור.
- אין להתקין Pixi, MacPorts או Homebrew, להסיר חבילות, לשנות PATH, לפרסם release או להריץ קבלה אמיתית במהלך המימוש.
- מסמך תוכנית זה אינו דוח הצלחה; דוח הביקורת יתווסף רק עם קומיטים/תוצאות שנבדקו.

## נקודות ביקורת סופיות

- [ ] סריקה אוספת לפחות את כל הספקים **הרלוונטיים** לכל התקנה שמיפויה מוכר, ללא תלות ב־Brew outdated.
- [ ] החלופות כוללות גם חסמים מפורטים, ואין ״ready״ ללא adapter ועסקה תואמת.
- [ ] מצבים של שוויון/כשל ספק/שתי התקנות/unknown version/מניעת downgrade נבדקו.
- [ ] generated files תואמים ל־generator ובדיקת CI ממוקדת עברה.
- [ ] נרשמו בבירור מסלולים שעוד אינם מבצעים מעבר ודרישות בדיקת הקבלה.

## התקדמות קוד עד 555ae7b — 20.10.2026

הסעיפים שלמעלה היו **תוכנית**, ולא הוכחה לביצוע. בזמן בדיקת הענף התברר:

- **מומש:** `collect_candidates` מחפש חלופות ממופות גם אם Homebrew אינו outdated; Homebrew revisions מזוהים בנפרד מגרסת upstream; מיפוי MacPorts ריק לא יוצר port מדומה; Pixi נבדק עם מגבלת זמן מצטברת, וחסר ראיות T02 מחזיר חסם ולא התרת התקנה.
- **מומש:** פרופילי `jq` ו־`pandoc` בלבד למעבר Homebrew→Pixi, עם בדיקות שימוש, `_material_pixi` לצורכי preflight, מסלול התקנה ביומן עסקה, רישום בעלות לאחר אימות וניקוי רשומה מוגן במקרה rollback.
- **טרם מחובר:** `_pixi_bridge` ב־`unified_updates.py` מסמן את כל ההצעות החדשות למעבר Homebrew→Pixi כ־`execution_supported=False`, גם כשמנוע העסקה מסוגל לתכנן מעבר לפרופיל. אין לסמן את המעבר כ־ready לפני השלמת החיבור והסקירה.
- **אימות ממוקד:** [CI #39458648017](https://github.com/TzoharLary/brew-update-manager/actions/runs/39458648017) עבר 63 בדיקות Python, בדיקת JS, תחביר, `unified-check` קריאה בלבד של 37 קבצים ו־packaging smoke. בדיקות קבלה אמיתיות לא בוצעו.

### צעד ההמשך המדויק (שלב 2 בתוכנית המשותפת)

1. לסקור את `_pixi_bridge`, `collect_candidates`, `update_selection.select` וה־`migration_engine` מקצה לקצה, לרבות ניתוב `plan/approve`, fingerprint ו־revalidation.
2. לבדוק הבדל בין version upstream לבין revisions, תרחיש source במיפוי, צרכנים, T02 שאינו מעודכן, lock/hash/closure, הגבלות 180 שניות, duplicate installations והחרגות.
3. להגדיר תנאי חובה שבהם Pixi יכול להיבחר לביצוע לעומת חסימה; אין להתיר התקנה בגלל עצם קיומו של `PIXI_TOOLS`.
4. למסור מפת פערים וסדר שינויי קוד לפני שלב 3; לא לבצע מעבר אמיתי, לא לשנות PATH ולא להכריז ש־T04 הושלמה.

### בדיקות פתוחות גם לאחר הקוד הקיים

- [ ] חיבור `_pixi_bridge` למנגנון אישור המעבר המאומת.
- [ ] הוכחת ביצוע ושחזור מלאים ב־fixtures עם כשלים בכל שלב והגנה מפני עריכה עצמאית.
- [ ] מעבר Homebrew→Pixi ב־Mac Intel בריצת קבלה מפורשת של המשתמש.
- [ ] בדיקות HTTP/Electron וגילוי עתידי של עדכון אצל הספק החדש לאחר restart.

---

## שלב 2 — דוח סקירת חוזים ובטיחות לקראת חיבור Homebrew→Pixi

**סטטוס:** סקירת קוד הושלמה ב־20.10.2026 על בסיס `53abc72`; שלב 3 (מימוש החיבור) **טרם בוצע**.
הסקירה היא סטטית של הקוד והבדיקות הקיימות, **לא** הוכחת הרצה על Mac או תוצאת התקנה.
לא הופעלו פקודות חבילות, לא שונו PATH או ההתקנות, ולא שונה קוד מוצר במסגרת שלב 2.

### 1. נתיב הנתונים שנבדק

```text
POST /api/check
 -> mac_manager._combined(refresh=True)
 -> installation_evidence.annotate(...): סיווג T02 ומיפוי לכלי Homebrew
 -> unified_updates.generate_plan
 -> collect_candidates -> _pixi_bridge -> pixi_provider.discover(osx-64)
 -> update_selection.select -> persisted plan / 15-minute fingerprint
 -> UI: update selected / all / one (request_id, decision_id, material_fingerprint)
 -> unified_updates.validate_request -> execute
 -> migration_engine._material / approve / _run
 -> backup -> pixi install --locked -> metadata+functional smoke
 -> routing -> shell PATH check -> guarded brew uninstall
 -> postcheck -> installed.json + ownership routes -> refresh/recovery
```

**הפרדה חשובה:** `pixi_provider.discover` מאמת מועמדות בינארית ו־lock, אך אינו מתיר מעבר; `migration_profiles.pixi_get` מתיר רק `jq` ו־`pandoc`; `migration_engine.plan` יוצר תוכנית עסקה אחרי preflight, וה־executor צריך להוכיח מחדש את התאמתה לפני כל מוטציה.

### 2. ממצאים מאומתים בקוד

| מזהה | דחיפות | ממצא וקוד תומך | השלכה | טיפול נדרש |
| --- | --- | --- | --- | --- |
| P0-A | חוסם הפעלה | `backend/unified_updates.py:228-245`: `_pixi_bridge` משאיר `execution_supported=False`, `profile_id=None`, `migration_plan_id` חסר גם לאחר `discover` מוצלח. `update_selection.py:117-151` חוסם הצעה זו. | מנוע Pixi החדש אינו נבחר לעדכון. | לחבר פרופיל קבוע ותוכנית `migration_engine.plan(... provider="pixi", pixi_candidate=verified, classification=...)` רק לאחר כל תנאי הסף, ולהציב `ready` רק עם מזהה ו־fingerprint תקינים. |
| P0-B | חוסם הפעלה | `backend/unified_updates.py:535-539, 550-556`: ה־executor מאמת מעבר דרך `migration_engine._material(s, logical_id)` **ללא provider**, ולכן ברירת המחדל היא MacPorts. | הצעת Pixi שתסומן `ready` תיבדק בטעות לפי נתיב MacPorts, ועלולה להיכשל ב־`plan_changed` או לפרש חוזה לא מתאים. | לנתב במפורש לפי `selected_candidate.provider` / `target_provider`, ולהצליב עם תוכנית העסקה השמורה, `profile_version`, `migration_fingerprint`, `generation_id` ונתוני Pixi. לא להסיר את האימות. |
| P0-C | חוסם הפעלה | `backend/mac_manager.py:168-186` מעביר סיווג T02 מלא מה־supplemental ל־packages בבדיקה מלאה; אולם `installation_evidence.py:281-306` בסריקה `refresh=False` משחזר סיווג רק מ־`previous.migrations`, **לא** מרשומת `previous.packages`. `unified_updates.py:527-539` קורא `compute_snapshot(False)` לפני מימוש, ו־`mac_manager.py:204` שומר את המצב החדש. `migration_engine.py:94-125` דורש סיווג טרי/מתאים. | עבור כלי שהיה supplemental בלבד (למשל jq עם Homebrew bottle), סיווג `standalone_candidate` עלול להפוך ל־`unclassified` לפני אישור העסקה; פעולה נכונה תידחה גם ללא שינוי התקנה. | לשמר סיווג **רק** מרשומת התקנה פיזית תואמת עם fingerprint תקף ותוקף זמן, או להפיק סיווג טרי לפני ביצוע. בדיקות חייבות להוכיח שסיווג מיושן/שונה/חסר נחסם. |
| P1-D | חשוב | `backend/migration_engine.py:297-310` מקבל מועמד Pixi נעול ויוצר תוכנית; אבל `approve:648-659` בונה `_material(... provider="pixi")` **ללא pixi_candidate**, כך ש־`_material_pixi:175` מגלה מועמד מחדש. | גם ללא שינוי קוד אפשר לקבל lock/closure שונה, timeout או חסם ממקור חיצוני בין תכנון לאישור. זה אינו אישור בטיחות אוטומטי. | להגדיר במדויק revalidation: תוכנית/מקור/דור דורשים התאמה מהותית, ונתיב גילוי מחדש מוגבל בזמן ויכול רק לדחות כשיש שינוי; לא ליפול לגרסה או lock אחרים. |
| P1-E | חשוב | `update_selection.py:145-151` בודק פרופיל כללי אך **לא** מחייב תוכנית migration; `unified_updates.validate_request:475-479` דורש `migration_plan_id` ו־fingerprint רק בזמן לחיצה. | אם יוצב `execution_supported=True` לפני תוכנית מאומתת, ה־UI עלול להציג `ready` שה־backend ידחה. | גם בזמן selection לדרוש ראיית תוכנית תקפה ומצומדת לפרופיל, מקור, ספק ויעד; בדיקת השרת חייבת להישאר בנוסף. |
| P1-F | חשוב לשלב 4 | `migration_engine._run:540-645`: יש backup/locked install/smoke/routing/cleanup/postcheck/ownership, אבל ברירות כשל אחרי cleanup יוצרות `needs_attention`, והמנוע לא מכוסה בבדיקות end-to-end של כל השלבים. `_compensate:445-469` משחזר קג רק כשהתיקייה חסרה; נדרש מבחן להסרה חלקית כשהיא עדיין קיימת. | אי אפשר להבטיח שחזור מלא במקרה כשל ביניים או פגיעה חלקית במקור. | לשמור חסימה עד הבדיקות הרלוונטיות; לכתוב fault-injection ו־resume/rollback מדומים לכל שלב, לשמור שינוי עצמאי, ורק לאחר מכן קבלת Mac אמיתית. |
| P2-G | המשך | `backend/brew-updates-service.py` מפעיל unified תחת `run_with_progress` ומנעול בין תהליכים; בקשת migration legacy חדשה נדחית ב־`review_required`, recovery נפרד. טיפול ב־`MigrationConflict` ו־409/500 אינו אחיד. | חסמי ביצוע טכניים עלולים להופיע כהודעת כשל כללית, ו־UI/HTTP לא נבדקו במלואם. | בשלב 6–7 להבטיח סטטוס 409 מובנה והצגת מסלול בדיקה מחדש; אין להמציא אישור migration חדש שעוקף את ה־executor המאוחד. |

### 3. הגנות חיוביות שנמצאו — לשמר

- `migration_engine._material_pixi:164-214`: native Intel בלבד, allowlist של פרופילים, ngrok/החרגות, איסור רשומת tool קיימת, בעלות shim, השוואת upstream ללא downgrade, lock/hash ומיפוי מדויק.
- `_fresh_classification:94-125`: fingerprint של מלאי המקור, חותמת זמן (עד 15 דקות), ללא gaps/consumers/shared roles; `_consumers` מבצע `brew uses --installed` מחדש.
- `pixi_provider.discover/install_locked/verify_generation`: osx-64, manifest+lock מדויקים, URL/channel מוגבלים, רשומות `conda-meta` וארטיפקטים תואמים, התקנה חדשה בלבד.
- `migration_profiles.smoke_pixi`: בודק version ותפקוד ממשי של `jq` ו־`pandoc` דרך executables מוגדרים.
- `routing_activation`: מסרב ל־shim זר או משתנה, כותב wrappers בשלבים, ומאמת login shell לפני הסרת מקור.
- `migration_engine._run`: מקור מוסר רק אחרי גיבוי, התקנת היעד ואימות שימוש; רישום בעלות מתבצע רק אחרי postcheck; כשל אחרי cleanup אינו מסומן הצלחה אוטומטית.
- `provider_ownership.commit_route`: דורש יעד פיזי יחיד ורשומת journal לפני commit; אין לייצר רשומת port פיקטיבית ב־installed.json.
- `unified_updates.validate_request`: דוחה תוכנית שפג תוקפה, החלטות כפולות, fingerprint שונה או החלטה לא מוכנה; שכבת ה־API שומרת request IDs ומנעול פעולה.

### 4. מפת שינויים מצומצמת לשלב 3

1. **קודם P0-C:** ב־`installation_evidence.annotate` / `mac_manager._combined` לתקן שימור בטוח של classification ב־snapshot `refresh=False`. ליצור fixtures לסריקה מלאה של jq עם `classification_only`, אחריה `compute_snapshot(False)`, ולוודא שראיה אמיתית נשמרת רק עד פקיעת התוקף/שינוי fingerprint. בלי fallback לסיווג בלתי מאומת.
2. **P0-A + P1-E:** ב־`unified_updates._pixi_bridge` וב־`collect_candidates` להפעיל `migration_engine.plan` רק ל־`pixi_get(name)` תואם ל־`PIXI_TOOLS[name]`, T02 טרי, ללא consumers, עם candidate `osx-64` ב־closure נעול ו־version חדש. לתעד `migration_plan_id`, `migration_fingerprint`, `profile_id`, `profile_version`; רק אז `execution_supported=True`.
3. **P0-B + P1-D:** ב־`unified_updates.execute` ו־`migration_engine.approve` להעביר יעד provider מפורש, לאמת תוכנית Pixi שנשמרה ונתוני lock/closure ומספר generation, ולחסום כל שינוי מהותי. אין מעבר מספק אחר או שינוי ספק סמוי.
4. **בדיקות ממוקדות:** קוד `test_unified_updates.py`, `test_migration_engine.py`, בדיקות T02, provider-version ו־`test_update_selection.py`: jq/pandoc `ready` עם preflight מלא, כלי ממופה ללא profile חסום, T02 ישן/שונה, שינוי lock, revision, שינוי בעלות, תוכנית שפג תוקפה, חבילת Homebrew רגילה שנבחרת כש־Pixi חסום, וגם בדיקה שמקור ה־Pixi לא עבר בטעות בנתיב MacPorts.
5. **גנרטור וביקורת:** לעדכן `tools/implement-mac-manager.py` באותם קובצי קוד/בדיקות, לבדוק `unified-check`, syntax ו־CI ממוקד. אין להריץ `--full`, לשנות חבילות, להגדיל allowlist ללא smoke או להתקין Pixi במחשב בזמן כתיבת הקוד.

**שער כניסה למצב `ready`:** כל P0 סגור בקוד + fixtures חיוביים ושליליים עברו + `validate_request` ו־`execute` יודעים לאמת/להפעיל *בדיוק* את תוכנית Pixi שנבחרה. אם תנאי חסר, משאירים `execution_supported=False`, גם אם `discover` מצא גרסה חדשה.

### 5. מה לא בוצע בשלב 2

- לא שונה `execution_supported` ולא הופעל מסלול Homebrew→Pixi בבקשת העדכון.
- לא נערכו קוד backend, UI, בדיקות מוצר או סקריפט התקנה; השינויים הם תיעוד ועותק התיעוד בגנרטור בלבד.
- לא הורצו בדיקות קבלה על Mac או התקנה/הסרה/חילופי PATH.
- 63 בדיקות ה־CI מהבסיס `555ae7b` הן רגרסיות ממוקדות; הן אינן מוכיחות את התרחישים החסרים שתועדו כאן.

## תוצאת שלב 3 — מימוש מוגן של Pixi bridge (20.10.2026)

שלב 2 לעיל הוא דוח ממצאים היסטורי; בעקבותיו בוצעו תיקוני קוד ב־`f2543f0` עד `ff48877`.

| ממצא משלב 2 | טיפול בשלב 3 | סטטוס |
| --- | --- | --- |
| P0-A | `_pixi_bridge` מייצר תוכנית `migration_engine.plan` מוגנת לפרופילי `jq`/`pandoc` ורק אז מחזיר executable | מומש; קבלה חיה ממתינה |
| P0-B | `_revalidate_migration` מאמת את ספק היעד המפורש לפני ביצוע, עם תוכנית נעולה, fingerprint, generation ומאפייני מועמד מדויקים | מומש; fault-injection ממתין |
| P0-C | סיווג T02 נשמר בסריקה חלקית רק אם מקור פיזי, fingerprint, גרסה וזמן תואמים; אחרת נחסם | מומש ונבדק במקרים ממוקדים |
| P1-D | `migration_engine.approve` ממשיך לבצע `discover` מחודש ומשווה לתוכנית הישנה; שינוי lock/closure/version מבטל את ההרצה | נבדק במוק/fixture; אינטגרציה מלאה ממתינה |
| P1-E | דרישת מזהה תוכנית ו־fingerprint כבר בשכבת `update_selection` ובדיקת זהות מפורטת שוב ב־`execute` | מומש ונבדק במקרים ממוקדים |
| P1-F / P2-G | כשל אחרי cleanup, rollback/restart, HTTP 409, ממשק legacy | פתוח לשלב 4 ולאחריו |

**אין הרחבת allowlist** מעבר ל־`jq`/`pandoc`, אין fallback לבניית מקור, ואין הרשאת מעבר לכל חבילה ממופה שאין לה פרופיל עסקה. סקריפט `--full` וקבלת Mac Intel עדיין לא בוצעו.
