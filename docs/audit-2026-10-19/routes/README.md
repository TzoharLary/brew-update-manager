# מיפוי מסלולים ל־27 הפריטים שנדחו — 19.10.2026

המיפוי הוא מחקר מועמדים בלבד עבור Intel x86_64 ב־macOS 26.7.1. לא הותקנה, הוסרה, עודכנה או הורצה חבילה; לא בוצע פתרון תלויות חדש. ngrok מוחרג. קובצי המערכת והאפליקציה נקראו בלבד, וכל פלט המחקר נכתב בתיקייה הזאת.

הקלט הוא תכנית האפליקציה מ־2026-10-19 04:19:49 UTC ומלאי האפליקציה מ־04:22:29 UTC. נשמרו SHA-256 של שני הקבצים והעתק ממוקד של הרשומות ב־[app-input-snapshot.json](app-input-snapshot.json). ראיות conda והפתרונות הקודמים הן מ־18.10; בדיקות המטא־דאטה הממוקדות החדשות הן מ־19.10. תאריכי המקור מופיעים בכל מועמד.

## מה ניתן להסיק

- קיימים שישה פתרונות Pixi מתועדים מאתמול שהגרסה שננעלה בהם שווה **לגרסת upstream** הנוכחית: include-what-you-use 0.27, PostgreSQL 17.11, Pycairo 1.29.2, socat 1.8.1.3, Tesseract 5.5.3 ו־wget 1.25.0. אלה אינם אימות הרצה או מעבר, וב־Pycairo הפתרון השתמש ב־Python 3.15, שאולי אינו מתאים לצרכנים.
- הפתרונות הישנים PHP 8.5.9, Poppler 26.09.0 ו־whisper.cpp 1.9.3 אינם מגיעים ליעדים הנוכחיים 8.5.11_1, 26.10.0 ו־1.9.5. כשלי האפליקציה הנוכחיים ב־Poppler וב־whisper נשמרו במיפוי.
- קיימים ארכיוני MacPorts ל־Darwin 25 Intel בגרסאות upstream המבוקשות של gpgme/gpgmepp, NSS, LLVM 23, PHP 8.5, PostgreSQL 17, terminal-notifier ו־Unbound. **לא נבדק שכל התלויות והווריאנטים זמינים כארכיונים**; קיימת התקנה אפשרית רק לאחר בדיקת הגרף כולו. [אינדקס הארכיונים הרשמי](https://packages.macports.org/), [הסבר על ארכיוני MacPorts](https://guide.macports.org/#using.binaries).
- ל־Nmap 7.992 קיים מתקין Intel רשמי. ל־terminal-notifier 3.1.0 קיים ZIP רשמי; התיעוד מצהיר שהוא universal, אך הקובץ עצמו לא הורד וחתכי המכונה לא נבדקו. בגרסה 3 יש שינויי אפשרויות והתנהגות שצריך לבדוק מול האפליקציה. [Nmap](https://nmap.org/download), [terminal-notifier](https://github.com/julienXX/terminal-notifier).
- cryptography 50.0.2 מופיעה בקטלוג conda ל־osx-64, אבל בתשובת PyPI שנבדקה יש חמש חבילות macOS, כולן ARM64, ואפס x86_64/universal2. ל־Pycairo 1.29.2 אין שם חבילות macOS מוכנות. לכן uv אינו מסלול בינארי אוטומטי לשתי הספריות הללו. [מטא־דאטה cryptography](https://pypi.org/pypi/cryptography/50.0.2/json), [מטא־דאטה Pycairo](https://pypi.org/pypi/pycairo/1.29.2/json).
- לא הוכח מסלול בינארי בגרסת היעד ל־libtensorflow, OpenLDAP, Poppler, whisper.cpp או merve במקורות שנבדקו. אין כאן טענה שאין מסלול בעולם. upstream של TensorFlow מסיים את הפצת C ל־macOS x86 ב־2.16; conda מחזיק 2.18 ל־Intel, מתחת ל־2.21 המבוקש. release של merve מצרף מקורות בלבד, ושל whisper 1.9.5 ללא assets. [TensorFlow C](https://www.tensorflow.org/install/lang_c), [merve releases](https://github.com/nodejs/merve/releases/tag/v1.2.2), [whisper releases](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.5).

מספר build של conda, revision של MacPorts ו־`_N` של Homebrew אינם אותה גרסה. התאמת upstream בלבד אינה מוכיחה ששינוי בנייה או תיקון שנוסף ב־Homebrew `_2` קיים אצל ספק אחר. במיוחד ב־wget, OpenJPEG, Net-SNMP ו־libtensorflow יש להבדיל עדכון upstream משינוי build.

## סדר מעבר לפי צרכנים

אלה קבוצות תכנון על סמך **קבלות ההתקנה**, לא הוכחה שכל הקישורים נטענים או שכל האפשרויות בשימוש. הגרף הנמדד נמצא ב־[homebrew-direct-edges.json](../inventory/homebrew-direct-edges.json) והרשומות ב־[homebrew-rows.json](../inventory/homebrew-rows.json).

| קבוצה | הפריטים שנדחו השייכים אליה | הפעולה המוצעת |
|---|---|---|
| תיקון מצב התקנה | LLVM; merve | להכריע קודם מה פעיל ומה פגום. LLVM פעיל לפי המלאי הוא 22.1.5; התיקייה 23.1.3_2 חסרת קבלה ואינה פעילה, אף שהאפליקציה מציגה אותה כמותקנת. ראיות הקישור הקודמות מצביעות על libz3.4.15 חסר ועל merve → libsimdutf.34 חסר. |
| פקודות עצמאיות יחסית | socat, wget, Nmap; terminal-notifier כבדיקת שילוב | פיילוט נפרד עם גרסה, פלט, אפשרויות ונתיב פעיל לפני בחירת shim. terminal-notifier צריך בדיקת התראה מהאפליקציה הארוזה. |
| מהדר ו־IWYU | LLVM, include-what-you-use | להתאים Clang, משאבי headers ו־compile_commands של פרויקט אחד; אין להניח שחבילת llvm כוללת את כל כלי Homebrew. |
| PDF | Poppler, GnuPG, GPGME, GPGMEPP, NSS, p11-kit, Unbound, OpenJPEG | מעבר Poppler כפקודות עשוי להשתמש בגרף אחר של הספק. אין צורך לשדרג בנפרד כל ספרייה ישנה כדי להפעיל פקודה מבודדת; אין להסיר את הישנות בלי בדיקת צרכנים. |
| OCR | Tesseract, Pango, OpenJPEG | להעביר פקודת OCR ונתוני שפות יחד; לבדוק קובצי עברית/אנגלית ו־traineddata. צרכני libtesseract נפרדים מפיילוט CLI. |
| PHP | PHP, Net-SNMP, OpenLDAP | לבדוק extensions, php.ini, פרויקטים ו־FPM אם הוא בשימוש. OpenLDAP 2.7.1 עדיין ללא מועמד מוכח במקורות שנבדקו. |
| Node הישן ב־Homebrew | merve, simdjson | ה־Node המנוהל כבר נבדק בריצה הקודמת; זה אינו מתקן את הספריות המקושרות של Node הישן. יש גם מסלול תלות ל־MongoDB במלאי; אין להסיר לפי PATH בלבד. simdjson אינו simdutf. |
| MPI | Open-MPI, ובהמשך ARPACK/igraph | סביבה אחת עם compiler wrappers, MPI וצרכן שנבנה ונבדק יחד. |
| Python | cryptography עבור Ansible הישן; Pycairo לפרויקט Python | לזהות interpreter ו־ABI בפועל; סביבה חדשה אינה מעדכנת imports בסביבה הקודמת. |
| שירותים/מודלים | PostgreSQL; שימוש שירותי אפשרי ב־Unbound/LDAP/SNMP; TensorFlow; Whisper | לבדוק שימוש בפועל. PostgreSQL נשאר major 17 ונבדק מול עותק משוחזר ו־extensions; שירות DNS דורש תצורה ופורט נפרדים. מודלים דורשים נתונים ואימות תוצאות. |

עדכון minor של PostgreSQL אינו דורש כשלעצמו `pg_upgrade`; החלפת ספק עדיין דורשת התאמת binaries, extensions, locale, תצורה ושירות. אין כאן אישור לגעת ב־PGDATA. [תיעוד PostgreSQL 17](https://www.postgresql.org/docs/17/pgupgrade.html).

## הקבצים לשילוב

- [deferred-route-matrix.json](deferred-route-matrix.json): 27 רשומות עם היעד המדויק, סיבת הדחייה, consumer_groups, מסלול מוצע, פעולה הבאה ורשימת מועמדים עם מקור ותאריך. `runtime_verified=false` ו־`migration_verified=false` בכל הרשומות.
- [deferred-route-matrix.tsv](deferred-route-matrix.tsv): אותה תמצית לחיבור למלאי הכולל; `installed_version` הוא תצוגת האפליקציה, ו־`inventory_active_version` הוא זיהוי הקג הפעיל לפי המלאי הטרי.
- [matrix-table.md](matrix-table.md): טבלת כל 27 הפריטים.
- [fresh-metadata.json](fresh-metadata.json): תשובות, כתובות, מועדי בדיקה, SHA-256 וקבצים שמורים; נשמרו גם כשלים/404. בקשה הוגבלה ל־3MB, עם עד שש בקשות במקביל; אין כאן הורדת ארכיוני תוכנה.
- [primary-source-receipts.json](primary-source-receipts.json): תיעוד ניסיונות שמירת מקורות נוספים. הורדת מטא־דאטה מ־Nmap דרך urllib נכשלה באימות תעודה, בעוד דף המפרסם נקרא בכלי הגלישה; לא בוצע עקיפת TLS ולא נשמר digest מאומת. יש להשלים אימות לפני התקנה.
- [providers.md](providers.md): יכולות ומגבלות לתמיכה עתידית בחבילות נוספות.

קוד העזר [build-matrix.py](build-matrix.py) בונה את המטריצה מחדש מהראיות השמורות ומהקלט המקומי; אינו מתקין או מריץ חבילות. אין להשתמש במטריצה כתכנית הפעלה אוטומטית: `recommended_next_action` מתאר שלב אימות הבא בלבד.
