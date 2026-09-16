import os
import os, ast, openpyxl
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'routes', 'api.py'), encoding='utf-8').read()
tree = ast.parse(src); lines = src.splitlines(True)
parts = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'EXPORT_CONFIG':
        parts['cfg'] = ''.join(lines[node.lineno-1:node.end_lineno])
    if isinstance(node, ast.FunctionDef) and node.name == '_build_export_workbook':
        parts['fn'] = ''.join(lines[node.lineno-1:node.end_lineno])
ns = {'openpyxl': openpyxl}
exec(parts['cfg'] + "\n" + parts['fn'], ns)

from datetime import datetime
d = lambda h, m: datetime(2026, 9, 16, h, m)
users_data = {
    "Durcovicova Henrieta": [
        [261543, "1166174", d(9,30), d(10,48), 1.29, 5, 5, 0.43, 2.17, 1.29, 0.87, "Completed, sent to TEST"],
        [261543, "1166400", d(11,57), d(13,35), 1.63, 5, 5, 0.11, 0.55, 1.63, -1.09, "Completed, sent to TEST"],
    ],
    "Daridova Renáta": [
        [261272, "1114945", d(12,26), None, 2.57, 54, 0, 0.12, 6.48, 0.0, 6.48, "IN: EMI"],
    ],
    "Ugyanaz a hosszú név ami levágódik 31-nél": [
        [1, "X", d(7,0), d(8,0), 1.0, 1, 1, 1.0, 1.0, 1.0, 0.0, "Completed"]],
    "Ugyanaz a hosszú név ami levágódik máshol": [
        [2, "Y", d(7,0), d(8,0), 1.0, 1, 1, 1.0, 1.0, 1.0, 0.0, "Completed"]],
    "Rossz/jel:lel [van]*?": [
        [3, "Z", d(7,0), d(8,0), 1.0, 1, 1, 1.0, 1.0, 1.0, 0.0, "Completed"]],
}
wb = ns['_build_export_workbook'](users_data, "2026-09-16", "day", ns['EXPORT_CONFIG'])
wb.save('/tmp/test_day.xlsx')
print("DAY sheets:", wb.sheetnames)
ws = wb["Összesítő"]
for r in ws.iter_rows(min_row=4, max_row=ws.max_row, values_only=True):
    print([str(x) if x is not None else "" for x in r])
print("--- Durcovicova sheet ---")
ws2 = wb["Durcovicova Henrieta"]
for r in ws2.iter_rows(min_row=1, max_row=ws2.max_row, values_only=True):
    print([str(x)[:24] if x is not None else "" for x in r])

month_data = {"A": [[1, "P", d(7,0), d(8,0), 10, 10, 0.5, 5.0, 1.0, 4.0]]}
wb2 = ns['_build_export_workbook'](month_data, "2026-09", "month", ns['EXPORT_CONFIG'])
wb2.save('/tmp/test_month.xlsx')
print("MONTH sheets:", wb2.sheetnames)
wsm = wb2["Összesítő"]
for r in wsm.iter_rows(min_row=4, max_row=wsm.max_row, values_only=True):
    print([str(x) if x is not None else "" for x in r])
print("OK")
