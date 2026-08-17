import sys, os
sys.path.insert(0, os.path.abspath('.'))
from src import db

projects = db.fetchall('SELECT id, project_name, sheet_id FROM projects ORDER BY id')
for p in projects:
    pname = p['project_name']
    sid = p['sheet_id']
    rows = db.fetchall(
        'SELECT sheet_name, COUNT(*) as cnt FROM work_orders WHERE file_id=%s GROUP BY sheet_name ORDER BY sheet_name',
        [sid]
    )
    print(f'=== {pname} ===')
    if rows:
        for r in rows:
            sn = r['sheet_name']
            cnt = r['cnt']
            print(f'  {sn}: {cnt}')
    else:
        print('  (нет данных в work_orders)')

# Также проверим колонку work_orders.project_name
print()
print('=== project_name в work_orders ===')
vals = db.fetchall('SELECT DISTINCT project_name, file_id FROM work_orders')
for v in vals:
    print(f'  project_name={v["project_name"]!r}  file_id={v["file_id"]!r}')
