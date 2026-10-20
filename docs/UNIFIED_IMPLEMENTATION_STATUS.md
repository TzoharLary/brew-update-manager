# Unified Intel updater — מימוש בענף 1.3.4

עודכן 20.10.2026. ענף `feat/unified-intel-134-foundations`, [טיוטת PR #1](https://github.com/TzoharLary/brew-update-manager/pull/1). `main` והתקנות ה־Mac לא שונו; אין release.

## קיים בקוד

- Candidate/Decision חוצה Homebrew, MacPorts ו־Pixi, גרסאות יציבות, closure בינארית, fingerprint, בעלות ומזהי בקשות.
- `backend/unified_updates.py`: תכנון וקבלת החלטות שמורות ל־15 דקות, endpoint מאוחד לביצוע selected/all/one, מניעת בקשה כפולה.
- `backend/pixi_provider.py`: גילוי גרסאות, lock osx-64 ואימות סביבה מנוהלת; **רק עדכון Pixi שכבר מנוהל**, ללא מעבר Homebrew→Pixi.
- `backend/migration_engine.py`: מעבר מוגבל Homebrew→MacPorts לכלים עם פרופיל מאומת, גיבוי, journal, ניתוב וסקירת resume/rollback.
- `renderer` / `electron`: הצגת החלטה ושימוש בבקשה המאוחדת. תיקוני `0f2f999` ו־`855a995` מסמנים תוכנית שפג תוקפה, חוסמים עדכון ומבקשים סריקה חדשה.
- `tools/implement-mac-manager.py` כולל Bundle 004; `tools/verify-migration.py` כולל `--checks` ו־`--full`.

## הוכח בפועל

[CI #39449649595](https://github.com/TzoharLary/brew-update-manager/actions/runs/39449649595) עבר: תחביר Python/JS, **10 בדיקות Python ממוקדות**, בדיקת JS לתוכנית שפג תוקפה, `unified-check` קריאה בלבד של 33 קבצים ו־packaging smoke. אין בכך אישור לסוויטות HTTP/Electron/Python המלאות או לקבלת Mac.

## פערי קוד

1. **T04:** הסריקה אוספת Homebrew רק כשהוא outdated, MacPorts רק למיפוי עם הצעה קיימת, ו־Pixi רק אם הוא כבר מנהל את ההתקנה. אין בחירה מלאה בין חלופות עבור כל התקנה. Homebrew→Pixi אינו נתיב מעבר ניתן לביצוע.
2. **T07:** עדיין קיימים נתיבי עדכון legacy ואשף מעבר ישן; נדרשת אחידות בטיחות, הודעות 409, תרגום ובדיקות ממשק.
3. **T03:** מנוע מעבר קיים, אך חסר כיסוי בדיקות מבוקר לכל מקרי failure/restart/recovery, ובדיקת קבלה אמיתית.
4. **רוחב תמיכה:** Cask, ספריות, שירותים או מעבר ללא פרופיל מאומת נשארים חסומים/חלקיים; לא להמציא `ready`.
5. **בדיקות ומסירה:** רגרסיות HTTP/Electron, שחזור Bundle 004 והרצת `--checks`/קבלה מקומית.

## תנאי מסירה

הסקריפט `--full` **נכתב**, אך הוא עשוי להחליף אפליקציה ולבקש YES לפעולות חבילה; אין להפעילו ללא מוכנות וקבלה מבוקרת. אין לסגור T03/T04/T07 או למזג לענף הראשי לפני ראיות מתאימות. [תוכנית מקור](INTEL_UPDATE_IMPLEMENTATION_PLAN.md) · [משימות](MAC_MANAGER_TASKS.md).
