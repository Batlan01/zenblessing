# Tesztek

Adatbázis nélkül futnak (álcursor / DOM stub), így bármikor gyorsan
újrafuttathatók.

```bash
python3 tests/test_workday.py                    # nap-ablak, unió, login idő
python3 tests/test_users_progress_aggregate.py   # a 2026-09-16-i valós nap újraszámolva
python3 tests/test_export_workbook.py            # XLSX építő (openpyxl kell hozzá)
node    tests/test_dashboard_pager.mjs           # dashboard lapozó
```

A `test_users_progress_aggregate.py` a képernyőképen és a generált Excelben
szereplő valódi 2026-09-16-i adatokból dolgozik, és egymás mellett mutatja a
régi és az új számítás eredményét.
