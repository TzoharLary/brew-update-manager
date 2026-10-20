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
