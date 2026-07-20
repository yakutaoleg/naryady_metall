import gspread
import time
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from src import config

SCOPES_RO = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]
SCOPES_RW = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly'
]

_gc_cache  = {}   # scopes_key → gspread client
_ss_cache  = {}   # file_id → spreadsheet object
_ws_cache  = {}   # (file_id, sheet_name) → worksheet object
_hdr_cache = {}   # (file_id, sheet_name) → col_map dict

def _api_call(fn, *args, **kwargs):
    """Retry on 429: 10s → 20s → 40s."""
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            if e.response.status_code == 429 and attempt < 3:
                time.sleep(10 * (2 ** attempt))
            else:
                raise

def _gc(scopes=None):
    key = tuple(scopes or SCOPES_RO)
    if key not in _gc_cache:
        creds = Credentials.from_service_account_file(config.GOOGLE_SA_KEY, scopes=list(key))
        _gc_cache[key] = gspread.authorize(creds)
    return _gc_cache[key]

def _ss(file_id: str, rw=False):
    if file_id not in _ss_cache:
        gc = _gc(SCOPES_RW if rw else SCOPES_RO)
        _ss_cache[file_id] = _api_call(gc.open_by_key, file_id)
    return _ss_cache[file_id]

def _ws(file_id: str, sheet_name: str, rw=False):
    key = (file_id, sheet_name)
    if key not in _ws_cache:
        ss = _ss(file_id, rw=rw)
        _ws_cache[key] = _api_call(ss.worksheet, sheet_name)
    return _ws_cache[key]

def _col_map(file_id: str, sheet_name: str, rw=False) -> dict:
    key = (file_id, sheet_name)
    if key not in _hdr_cache:
        ws = _ws(file_id, sheet_name, rw=rw)
        headers = _api_call(ws.row_values, 2)
        _hdr_cache[key] = {h.strip(): i + 1 for i, h in enumerate(headers) if h.strip()}
    return _hdr_cache[key]

def _col_letter(n: int) -> str:
    result = ''
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result

# Сбрасываем ws/header кэши после изменений структуры листа
def _invalidate_ws(file_id: str, sheet_name: str):
    _ws_cache.pop((file_id, sheet_name), None)
    _hdr_cache.pop((file_id, sheet_name), None)


def find_active_files():
    service = build('drive', 'v3', credentials=Credentials.from_service_account_file(
        config.GOOGLE_SA_KEY, scopes=SCOPES_RO))
    active = []

    def search_in_folder(folder_id, project_name):
        q = (f"name = '{config.ACTIVE_FILE_NAME}' "
             f"and mimeType = 'application/vnd.google-apps.spreadsheet' "
             f"and trashed = false and '{folder_id}' in parents")
        result = service.files().list(q=q, fields='files(id,name)', pageSize=50).execute()
        for f in result.get('files', []):
            active.append({'file_id': f['id'], 'file_name': f['name'], 'project_name': project_name})

    search_in_folder(config.DRIVE_FOLDER_ID, 'Без проекта')
    q_folders = (f"mimeType = 'application/vnd.google-apps.folder' and trashed = false "
                 f"and '{config.DRIVE_FOLDER_ID}' in parents")
    folders_res = service.files().list(q=q_folders, fields='files(id,name)', pageSize=100).execute()
    for folder in folders_res.get('files', []):
        search_in_folder(folder['id'], folder['name'])
    return active


def read_sheet(file_id: str, sheet_name: str) -> list[dict]:
    ws = _ws(file_id, sheet_name)
    rows = _api_call(ws.get_all_values)
    if len(rows) < 2:
        return []
    headers = rows[1]
    data = []
    for row in rows[2:]:
        record = dict(zip(headers, row))
        pos = record.get('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', '').strip()
        el  = record.get('ЭЛЕМЕНТ', '').strip()
        if not pos and not el:
            continue
        if pos == 'ИТОГО':
            continue
        data.append(record)
    return data


def write_row_ids(file_id: str, sheet_name: str, row_ids: list[tuple[int, str]]):
    """Записывает сгенерированные ROW_ID обратно в таблицу одним батчем."""
    cm = _col_map(file_id, sheet_name, rw=True)
    col_row_id = cm.get('ROW_ID')
    if not col_row_id or not row_ids:
        return
    ws = _ws(file_id, sheet_name, rw=True)
    updates = [
        {'range': f'{_col_letter(col_row_id)}{row_num + 2}', 'values': [[row_id]]}
        for row_num, row_id in row_ids
    ]
    _api_call(ws.batch_update, updates)


def insert_remainder_row(file_id: str, sheet_name: str, after_row_num: int, row_data: dict):
    """Вставляет строку-остаток сразу после указанной, копируя формат."""
    cm = _col_map(file_id, sheet_name, rw=True)
    ws = _ws(file_id, sheet_name, rw=True)
    ss = _ss(file_id, rw=True)

    source_sheet_row = after_row_num + 2
    insert_at        = source_sheet_row + 1
    max_col          = max(cm.values())

    # 1. Вставляем пустую строку
    _api_call(ws.insert_rows, [[]], row=insert_at)

    # 2. Копируем только формат
    _api_call(ss.batch_update, {'requests': [{
        'copyPaste': {
            'source': {
                'sheetId': ws.id,
                'startRowIndex': source_sheet_row - 1, 'endRowIndex': source_sheet_row,
                'startColumnIndex': 0, 'endColumnIndex': max_col,
            },
            'destination': {
                'sheetId': ws.id,
                'startRowIndex': insert_at - 1, 'endRowIndex': insert_at,
                'startColumnIndex': 0, 'endColumnIndex': max_col,
            },
            'pasteType': 'PASTE_FORMAT',
        }
    }]})

    # 3. Записываем значения одним вызовом
    new_row = [''] * max_col
    for header, col_idx in cm.items():
        if header in row_data:
            new_row[col_idx - 1] = row_data[header]
    _api_call(ws.update, values=[new_row],
              range_name=f'A{insert_at}:{_col_letter(max_col)}{insert_at}')

    # Сбрасываем ws-кэш: структура листа изменилась
    _invalidate_ws(file_id, sheet_name)


def delete_row(file_id: str, sheet_name: str, row_num: int):
    """Удаляет строку по row_num (0-indexed, данные начинаются с row 3)."""
    ws = _ws(file_id, sheet_name, rw=True)
    _api_call(ws.delete_rows, row_num + 2)
    _invalidate_ws(file_id, sheet_name)


def update_cell_by_header(file_id: str, sheet_name: str, row_num: int, header: str, value):
    cm  = _col_map(file_id, sheet_name, rw=True)
    col = cm.get(header)
    if not col:
        return
    ws = _ws(file_id, sheet_name, rw=True)
    _api_call(ws.update, values=[[value]],
              range_name=f'{_col_letter(col)}{row_num + 2}')


def update_task_status(file_id: str, sheet_name: str, row_num: int,
                       status: str, comment: str = None, date_fact: str = None, qty_done: int = None):
    """Обновляет статус/комментарий/дату/выполнено одним батч-запросом."""
    cm = _col_map(file_id, sheet_name, rw=True)
    ws = _ws(file_id, sheet_name, rw=True)

    sheet_row = row_num + 2
    updates = []

    if cm.get('СТАТУС'):
        updates.append({'range': f'{_col_letter(cm["СТАТУС"])}{sheet_row}',   'values': [[status]]})
    if comment is not None and cm.get('КОММЕНТАРИЙ'):
        updates.append({'range': f'{_col_letter(cm["КОММЕНТАРИЙ"])}{sheet_row}', 'values': [[comment]]})
    if date_fact is not None and cm.get('ДАТА ФАКТ'):
        updates.append({'range': f'{_col_letter(cm["ДАТА ФАКТ"])}{sheet_row}',  'values': [[date_fact]]})
    if qty_done is not None and cm.get('ВЫПОЛНЕНО'):
        updates.append({'range': f'{_col_letter(cm["ВЫПОЛНЕНО"])}{sheet_row}',  'values': [[qty_done]]})

    if updates:
        _api_call(ws.batch_update, updates)
