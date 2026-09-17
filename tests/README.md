# Tesztek

Adatbázis nélkül futnak (álcursor / DOM stub), így bármikor gyorsan
újrafuttathatók.

```bash
python3 tests/test_login_vs_work.py              # összes = effektív + veszteség
python3 tests/test_users_progress_aggregate.py   # a valós 2026-09-16-i nap
python3 tests/test_string_timestamps.py          # szöveges időpontok ('' / 'null' vég)
python3 tests/test_workday.py                    # nap-ablak, unió, munkamenetek
python3 tests/test_export_workbook.py            # XLSX építő (openpyxl kell hozzá)
node    tests/test_dashboard_pager.mjs           # dashboard lapozó
```

### Mit véd melyik

**`test_login_vs_work.py`** – a „Felhasználói haladás" oldal teljes
idő-modellje. Minden esetben ellenőrzi az invariánst:
`effektív + veszteség == összes idő`. Lefedi az ebédszünetre kijelentkezést,
a párhuzamosan futó WO-kat, a bejelentkezett időn kívüli munkát, és azt az
esetet, amikor valaki egész nap egyetlen futó WO-n dolgozik.

**`test_string_timestamps.py`** – azzal az adatalakkal dolgozik, ahogy a DB
tényleg visszaadja az időpontokat: **szövegként**, és a „még nincs vége" nem
SQL NULL, hanem üres sztring vagy a `'null'` szó. Emiatt esett ki korábban a
napi szűrőből hét sorból hat.

**`test_users_progress_aggregate.py`** – a képernyőképen és a generált
Excelben szereplő valódi 2026-09-16-i adatokból dolgozik.
