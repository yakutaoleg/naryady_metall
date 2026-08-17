"""
apply_format.py — применяет единый форматный стандарт из sheet_standards.py
ко всем рабочим вкладкам указанного файла.
Аргументы: <file_id> [sheet_name]  (если sheet_name не задан — все вкладки)
"""
import sys, copy
sys.path.insert(0, '/root/naryady/prod')

from src.sheets import _service, _col_map, _ws
from src.sheet_standards import (
    HEADER_CELL_FORMAT, DATA_CELL_FORMAT, COLUMN_OVERRIDES, ITOGO_CELL_FORMAT
)
from src import config

FILE_ID = sys.argv[1] if len(sys.argv) > 1 else None
TARGET_SHEET = sys.argv[2] if len(sys.argv) > 2 else None

if not FILE_ID:
    print('Usage: apply_format.py <file_id> [sheet_name]')
    sys.exit(1)

svc = _service()
sheets_to_process = [TARGET_SHEET] if TARGET_SHEET else config.WORK_SHEETS

def _merge(base, override):
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and k in result and isinstance(result[k], dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result

def build_format_request(sheet_id, start_row, end_row, start_col, end_col, fmt):
    fields_parts = []
    if 'textFormat' in fmt:
        for f in fmt['textFormat']:
            fields_parts.append('userEnteredFormat.textFormat.' + f)
    for f in ['horizontalAlignment', 'verticalAlignment', 'wrapStrategy',
              'backgroundColor', 'numberFormat']:
        if f in fmt:
            fields_parts.append('userEnteredFormat.' + f)
    return {
        'repeatCell': {
            'range': {
                'sheetId': sheet_id,
                'startRowIndex': start_row,
                'endRowIndex': end_row,
                'startColumnIndex': start_col,
                'endColumnIndex': end_col,
            },
            'cell': {'userEnteredFormat': fmt},
            'fields': ','.join(fields_parts),
        }
    }

def find_itogo_rows(file_id, sheet_name, cm, n_rows):
    """Ищет строки с ИТОГО — проверяет все возможные позиционные колонки."""
    found = set()
    for candidate in ['ЭЛЕМЕНТ', 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка']:
        if candidate not in cm:
            continue
        col_letter = chr(ord('A') + cm[candidate] - 1)
        resp = svc.spreadsheets().values().get(
            spreadsheetId=file_id,
            range=f'{sheet_name}!{col_letter}3:{col_letter}{n_rows}'
        ).execute()
        for i, row in enumerate(resp.get('values', [])):
            val = row[0].strip() if row else ''
            if 'ИТОГО' in val.upper():
                found.add(i + 2)  # 0-based row index
    return sorted(found)

for sheet_name in sheets_to_process:
    print(f'\nОбработка: {sheet_name}')
    try:
        ws = _ws(FILE_ID, sheet_name)
        sid = ws.id
        n_rows = ws.row_count
    except Exception as e:
        print(f'  Пропуск: {e}')
        continue

    cm = _col_map(FILE_ID, sheet_name)
    n_cols = max(cm.values()) if cm else 20
    requests = []

    # 1. Заголовок (строка 2, индекс 1)
    requests.append(build_format_request(sid, 1, 2, 0, n_cols, HEADER_CELL_FORMAT))

    # 2. Данные (строки 3+, индекс 2+)
    requests.append(build_format_request(sid, 2, n_rows, 0, n_cols, DATA_CELL_FORMAT))

    # 3. Переопределения по колонкам
    for col_name, override in COLUMN_OVERRIDES.items():
        ci = cm.get(col_name)
        if ci is None:
            continue
        ci -= 1
        col_fmt = _merge(DATA_CELL_FORMAT, override)
        requests.append(build_format_request(sid, 2, n_rows, ci, ci + 1, col_fmt))

    # 4. ИТОГО строки
    itogo_rows = find_itogo_rows(FILE_ID, sheet_name, cm, n_rows)
    for row_idx in itogo_rows:
        requests.append(build_format_request(sid, row_idx, row_idx + 1, 0, n_cols, ITOGO_CELL_FORMAT))
    if itogo_rows:
        print(f'  ИТОГО найдено в строках: {[r+1 for r in itogo_rows]}')

    svc.spreadsheets().batchUpdate(
        spreadsheetId=FILE_ID,
        body={'requests': requests}
    ).execute()
    print(f'  ✅ {len(requests)} запросов применено')

print('\nГотово.')
