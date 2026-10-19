# אילו עדכוני Homebrew יכולים לקבל מסלול בינארי ב־MacPorts

נבדק ב־19.10.2026 עבור macOS 26.7.1, ‏Darwin 25, ‏Intel x86_64. MacPorts 2.12.7 והקטלוג המעודכן שלו שימשו לבדיקה. **לא הותקנו או הועברו חבילות.** מספר ה־ports המותקנים נשאר אפס.

## התוצאה וההמלצה

Homebrew מדווח על **27 נוסחאות מיושנות שעבורן נדרשת בנייה** במטא־דאטה המקומי. עבור 11 נמצאו ב־MacPorts ארכיונים מדויקים וקובצי חתימה לכל עץ תלויות ההרצה, בגרסת upstream שאינה ישנה מיעד Homebrew. אלה מועמדים למעבר; הם עדיין לא עברו בדיקת התקנה, הרצה או התאמה לפרויקטים ולנתונים שלך.

**הפיילוט הראשון המומלץ הוא terminal-notifier**, כי נמצא ארכיון יחיד ללא תלויות נוספות. לפני שינוי המסלול צריך לבדוק שליחת התראה מהאפליקציה: גרסה 3 שינתה אפשרויות ביחס לגרסה 2.

אם יש שימוש פעיל בכלי הפיתוח, המועמדים הבאים הם **LLVM יחד עם include-what-you-use**, ו־**Open MPI** בתוך פרויקט מתאים. ב־LLVM נבדקו גם `llvm-23` וגם `clang-23`; אין כאן הוכחה שכל כלי ותכונה בנוסחת LLVM של Homebrew מכוסים באותם ports. Open MPI נבדק דרך `openmpi-default`; בחירת compiler/Fortran והצרכנים צריכה להיבדק לפני מעבר.

PostgreSQL 17 הוא מועמד בינארי, אך אינו פיילוט של החלפת פקודה: צריך תכנית נתונים, extensions, locale ושירות. אין סיבה להעביר ספריות בודדות רק משום שהן זמינות: ספריית MacPorts תחת `/opt/local` אינה מתקנת צרכן שקושר לספריית Homebrew תחת `/usr/local`.

## 11 החבילות עם סגירה בינארית זמינה בגרסה מתאימה

| חבילה ב־Homebrew | מסלול MacPorts שנבדק | גרסת upstream | ארכיונים ייחודיים בקבוצה | המלצה |
|---|---|---|---:|---|
| terminal-notifier | terminal-notifier | 3.1.0 | 1 | פיילוט ראשון; לבדוק אפשרויות והתראה אמיתית |
| llvm | llvm-23 + clang-23 | 23.1.3 | 27 | יחד עם סביבת הפיתוח והצרכנים; לא החלפת ספריות גלובלית |
| include-what-you-use | include-what-you-use | 0.27 | 28 | לתכנן יחד עם LLVM/Clang ובדיקת compile_commands של פרויקט |
| open-mpi | openmpi-default | 5.0.11 | 28 | להעביר סביבת MPI וצרכן; לבדוק compiler ו־Fortran |
| postgresql@17 | postgresql17 + postgresql17-server | 17.11 | 22 | מעבר שירות ונתונים נפרד; להשאיר major 17 |
| nss | nss | 3.131 | 6 | רק יחד עם הצרכן; Poppler הנוכחי עדיין אינו מסלול מתאים |
| openjpeg | openjpeg | 2.5.4 | 14 | ספרייה/כלים; לבדוק צרכני PDF ו־OCR לפני העברה |
| p11-kit | p11-kit | 0.26.5 | 10 | ספרייה/רכיב אמון; להעביר במסגרת גרף הצרכן |
| pango | pango | 1.58.2 | 47 | ספרייה עם תלויות GUI; לא תחליף עצמאי ל־Pango של Homebrew |
| py3cairo | py314-cairo | 1.29.2 | 43 | שייך ל־Python של MacPorts; לבדוק interpreter ופרויקט |
| unbound | unbound | 1.26.1 | 6 | שירות/ספרייה; לבדוק תצורה, פורטים וצרכנים |

מספרי הארכיונים חופפים בין חבילות ואין לחברם. גרסאות revision של Homebrew (`_1`, `_2`) ו־MacPorts אינן אותה סדרת גרסאות. התאמת upstream אינה מוכיחה שכל שינוי אריזה או patch של Homebrew קיים גם ב־MacPorts; יש לבדוק זאת בתכנית המעבר. גודל העץ אינו זמן התקנה שנמדד.

## 7 שהגרסה שלהן מתאימה אבל חסר ארכיון

| חבילה | החסם המדויק שנמצא |
|---|---|
| gpgme | `libunistring-1.5_0.darwin_25.x86_64.tbz2` |
| gpgmepp | אותו ארכיון libunistring חסר |
| php | אותו ארכיון libunistring חסר |
| tesseract | אותו ארכיון libunistring חסר; מעבר OCR דורש גם שימור שפות |
| wget | אותו ארכיון libunistring חסר |
| nmap | `nmap-7.992_0+pcre+ssl.darwin_25.x86_64.tbz2` |
| socat | `socat-1.8.1.3_0.darwin_25.x86_64.tbz2` |

הכתובות המדויקות החזירו 404 בשלושה שרתי HTTPS רשמיים שנבדקו. אין כאן טענה שאין ארכיון אצל כל מקור בעולם. אין לעקוף את החסם על ידי שינוי מערכת היעד, הורדת גרסה או החלפת ספרייה ידנית. אפשר להריץ את הבדיקה מחדש כשהארכיונים יפורסמו.

## 6 שגרסת MacPorts שלהן ישנה מהיעד

| חבילה | יעד Homebrew | MacPorts שנפתר |
|---|---|---|
| cryptography | 50.0.2 | 48.0.0 |
| gnupg | 2.5.24 | 2.5.21 |
| net-snmp | 5.9.5.2 | 5.9.4 |
| openldap | 2.7.1 | 2.6.13 |
| poppler | 26.10.0 | 26.02.0 |
| simdjson | 5.0.5 | 4.6.1 |

GnuPG ו־Poppler נתקלים בנוסף ב־libunistring החסר. Poppler ב־MacPorts אף ישן מהגרסה 26.04 שכבר מותקנת אצלך; אין להציג מעבר כזה כהשלמת עדכון.

## 3 ללא מיפוי מאומת

`libtensorflow`, ‏`merve` ו־`whisper.cpp`. לשניים הראשונים לא הוגדר מיפוי מוכח לחבילת ספק תואמת, והשם `whisper.cpp` אינו נפתר בקטלוג MacPorts שנבדק. קיים port בשם `whisper`, אך התאמת שם חלקית אינה מוכיחה שזה אותו מוצר; הוא לא הוחלף אוטומטית במיפוי.

## מה אומת בפועל

1. הבדיקה הראשונה הורצה מחדש מול מלאי Homebrew והקטלוג המקומי; ngrok מוחרג. Node כבר הוסר מ־Homebrew ולכן נותרו 27, לעומת 28 בבדיקה שלפני הסרתו.
2. מנוע MacPorts הרשמי העריך את ה־Portfiles ואת אפשרויות ברירת המחדל למחשב. נבדקו 27 שמות port מועמדים עבור 25 זהויות Homebrew ממופות; 26 שמות נפתרו ואחד לא.
3. נעשה שימוש ב־`mportdepends` במצב binary-only: תלויות `lib` ו־`run`, ללא תלויות build/test/fetch שאינן נדרשות להתקנת ארכיון. מסד רישום ורישום עבודה פרטיים נוצרו תחת `/tmp`, בלי שינוי ההתקנות הגלובליות.
4. שמות הארכיונים התקבלו מ־`get_portimage_name` ו־`get_canonical_archs` של MacPorts, ולא נבנו מניחוש של שם, וריאנטים או OS. כך נכללים גם קובצי `darwin_any.noarch` המתאימים לכל מעבד, לצד תלויות Intel שלהם.
5. עבור 161 ארכיונים ייחודיים בוצעו בקשות HEAD לכתובת הארכיון ול־`.sig`, סוג החתימה המוגדר במקור הרשמי הנוכחי. נבדקו השרת הראשי ושני mirrors רשמיים כגיבוי. 158 נמצאו עם קובץ חתימה ו־3 ארכיונים מדויקים חסרו.
6. כל סגירה טרנזיטיבית נבדקה שוב באמצעות מעבר בגרף התלויות הישירות שנפלט; לא נמצאו פערי cache בין שורשים.

**לא הורדו גופי ארכיוני התוכנה, לא אומתו חתימות קריפטוגרפיות על תוכנם, ולא נבדקה התנהגות לאחר התקנה.** המסקנה היא זמינות מסלול בינארי לפי מטא־דאטה, ולא הכרזה שהמעבר כבר הוכח או בוצע. HEAD אינו תחליף לשלב ההורדה, אימות החתימה ובדיקות השימוש לפני הפעלה.

## תוצרים ושחזור הבדיקה

- [מטריצה תמציתית של כל 27 החבילות](package-matrix.tsv)
- [דו״ח כל הארכיונים והתגובות המדויקות](archive-coverage.json)
- [בדיקת Homebrew העדכנית](homebrew.json), [שמות בלבד](homebrew.source-required.txt)
- [מיפוי שמות ושורשים מורכבים](root-map.json)
- [גרף ה־ports והארכיונים שנפתר](resolved/nodes.tsv), [שיוך ארכיונים לשורשים](resolved/root-nodes.tsv), [תוצאות הפתרון](resolved/root-results.tsv)
- [בדיקות עקביות והוכחת אפס התקנות](validation.json), [הסבר ממשק הבדיקה](probe-research/API-NOTES.md)

פקודות מתיקיית הפרויקט; השלב השני מקבל במפורש רשימת שמות MacPorts, ובדיקה של חבילות אחרות דורשת מיפוי מאומת משלהן:

```sh
python3 tools/check-brew-binaries.py --output docs/macports-binary-coverage-2026-10-19/homebrew.json

/opt/local/libexec/macports/bin/tclsh8.6 \
  docs/macports-binary-coverage-2026-10-19/probe-research/resolve.tcl \
  docs/macports-binary-coverage-2026-10-19/resolved \
  "${(@f)$(cat docs/macports-binary-coverage-2026-10-19/port-roots.txt)}"

python3 tools/check-macports-archives.py \
  --resolved docs/macports-binary-coverage-2026-10-19/resolved \
  --root-map docs/macports-binary-coverage-2026-10-19/root-map.json \
  --brew-report docs/macports-binary-coverage-2026-10-19/homebrew.json \
  --output docs/macports-binary-coverage-2026-10-19/archive-coverage.json
```

הסקריפט Tcl מסיר רק את רמז ה־prefetch הספקולטיבי של MacPorts כדי שבדיקות הרשת יבוצעו במפורש בשלב HEAD. הוא אינו מפעיל install או archivefetch. הפרטים ומגבלותיו מתועדים בקובץ הממשק.

מקורות רשמיים: [תלויות רקורסיביות וסוגיהן](https://man.macports.org/port-deps.1.html), [ארכיונים וחתימות](https://man.macports.org/port-archivefetch.1.html), [שרתי הארכיונים](https://packages.macports.org/). המקור המקומי והארכיונים נבדקו באותו יום; זמינותם וגרסאותיהם עשויות להשתנות.
