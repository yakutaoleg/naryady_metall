"""
Находит и исправляет значения ИСПОЛНИТЕЛЬ с лишними пробелами во всех активных проектах.
Запускать из /root/naryady/prod: python3 /root/naryady/fix_executor_spaces.py
"""
import os
import sys

# Грузим окружение из .env
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import gspread
from src import db, sheets as _sheets
from google.oauth2.service_account import Credentials

SA_KEY = os.path.join(os.path.dirname(__file__), 'service_account.json')
SCOPES = ['https://spreadsheets.google.com/feeds',
          'https://www.googleapis.com/auth/drive']

creds = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
gc    = gspread.authorize(creds)

projects = db.fetchall(
    "SELECT project_name, sheet_id FROM projects WHERE status='АКТИВНЫЙ' ORDER BY id",
    []
)
print(f"Проектов: {len(projects)}")

SKIP_SHEETS = {'СОТРУДНИКИ', 'ЗАВИСИМОСТИ', 'ИТОГО', 'Sheet1'}
total_fixed = 0

for proj in projects:
    file_id      = proj['sheet_id']
    project_name = proj['project_name']
    print(f"\n{'='*60}\nПроект: {project_name}")

    try:
        sh = gc.open_by_key(file_id)
    except Exception as e:
        print(f"  ❌ Не удалось открыть: {e}")
        continue

    for ws in sh.worksheets():
        if ws.title in SKIP_SHEETS:
            continue

        try:
            all_values = ws.get_all_values()
        except Exception as e:
            print(f"  ⚠️  {ws.title}: ошибка чтения — {e}")
            continue

        if len(all_values) < 2:
            continue

        header_row = all_values[1]  # строка 2 = заголовки
        if 'ИСПОЛНИТЕЛЬ' not in header_row:
            continue

        col_idx    = header_row.index('ИСПОЛНИТЕЛЬ')
        col_letter = chr(ord('A') + col_idx)

        fixes = []
        for row_i, row in enumerate(all_values):
            if row_i < 2:
                continue
            cell_val = row[col_idx] if col_idx < len(row) else ''
            stripped  = cell_val.strip()
            if cell_val != stripped and stripped:
                fixes.append({'row': row_i + 1, 'old': repr(cell_val), 'new': stripped})

        if not fixes:
            print(f"  ✅ {ws.title}: чисто")
            continue

        print(f"  🔧 {ws.title}: {len(fixes)} ячеек с лишними пробелами")
        for f in fixes:
            print(f"     строка {f['row']}: {f['old']} → {repr(f['new'])}")

        try:
            updates = [{'range': f"{col_letter}{f['row']}", 'values': [[f['new']]]}
                       for f in fixes]
            ws.batch_update(updates, value_input_option='USER_ENTERED')
            print(f"     ✅ исправлено {len(fixes)}")
            total_fixed += len(fixes)
        except Exception as e:
            print(f"     ❌ ошибка записи: {e}")

print(f"\n{'='*60}")
print(f"Готово. Всего исправлено ячеек: {total_fixed}")
