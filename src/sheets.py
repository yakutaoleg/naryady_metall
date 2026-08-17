import gspread
import time
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from src import config

from src.sheet_standards import COL_WIDTHS, HEADER_ROW_HEIGHT, STATUS_COLORS, DATE_COLUMNS, DROPDOWN_RULES


def apply_column_standards(file_id: str, sheet_name: str):
    """Применяет стандартные ширины колонок и высоту строки заголовков.
    Вызывать после создания/изменения структуры листа."""
    cm = _col_map(file_id, sheet_name, rw=True)
    ws = _ws(file_id, sheet_name, rw=True)
    sid = ws.id

    requests = []

    # Ширины колонок
    for header, col_1based in cm.items():
        ci = col_1based - 1  # 0-based
        if header == "ROW_ID":
            requests.append({
                "updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS",
                              "startIndex": ci, "endIndex": ci + 1},
                    "properties": {"hiddenByUser": True, "pixelSize": 1},
                    "fields": "hiddenByUser,pixelSize",
                }
            })
            continue
        target = COL_WIDTHS.get(header)
        if target is None:
            continue
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": ci, "endIndex": ci + 1},
                "properties": {"pixelSize": target},
                "fields": "pixelSize",
            }
        })

    # Высота строки заголовков (строка 2 = index 1)
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": HEADER_ROW_HEIGHT},
            "fields": "pixelSize",
        }
    })


    requests.append({
        'updateSheetProperties': {
            'properties': {'sheetId': sid, 'gridProperties': {'frozenRowCount': 2}},
            'fields': 'gridProperties.frozenRowCount',
        }
    })

    from src.sheet_standards import TABLE_BORDERS
    _all_rows   = _api_call(ws.get_all_values)
    _data_rows  = len(_all_rows)          # включая строку заголовка (строка 2)
    _max_col    = max(cm.values())        # правая граница по реальным колонкам
    requests.append({
        'updateBorders': {
            'range': {'sheetId': sid, 'startRowIndex': 1, 'endRowIndex': _data_rows,
                      'startColumnIndex': 1, 'endColumnIndex': _max_col},
            'top':             TABLE_BORDERS['top'],
            'bottom':          TABLE_BORDERS['bottom'],
            'left':            TABLE_BORDERS['left'],
            'right':           TABLE_BORDERS['right'],
            'innerHorizontal': TABLE_BORDERS['bottom'],
            'innerVertical':   TABLE_BORDERS['right'],
        }
    })

    if requests:
        _api_call(_service().spreadsheets().batchUpdate(
            spreadsheetId=file_id, body={"requests": requests}).execute)




_svc_cache = {}

def _service():
    if 'v4' not in _svc_cache:
        from googleapiclient.discovery import build
        creds = __import__('google.oauth2.service_account', fromlist=['Credentials']).Credentials.from_service_account_file(
            config.GOOGLE_SA_KEY, scopes=SCOPES_RW)
        _svc_cache['v4'] = build('sheets', 'v4', credentials=creds)
    return _svc_cache['v4']

SCOPES_RO = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]
SCOPES_RW = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly'
]

_gc_cache  = {}   # scopes_key → gspread client
# Нестандартные листы (ОСИ 11-15, Щучин) имеют другие заголовки.
# Ключ — стандартное имя, значение — реальное имя в листе.
_SHEET_COL_ALIASES = {
    'ПОКРАСКА': {
        'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': 'Марка',
        'ЭЛЕМЕНТ':               'Поверхность\nЭлемент (м²)',
        'КОЛ-ВО':               'Кол-во',
        'МАССА ЕД. (кг)':       'Покраска\nза (м²)',
    },
    'ГРУНТОВКА': {
        'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': 'Марка',
        'ЭЛЕМЕНТ':               'Поверхность\nЭлемент (м²)',
        'КОЛ-ВО':               'Кол-во',
        'МАССА ЕД. (кг)':       'Грунтовка\nза (м²)',
    },
}

_ss_cache  = {}   # (file_id, rw) → spreadsheet object
_ws_cache  = {}   # (file_id, sheet_name, rw) → worksheet object
_hdr_cache = {}   # (file_id, sheet_name) → col_map dict

def _api_call(fn, *args, **kwargs):
    """Retry on 429/503: 60s → 90s → 120s → 180s (per-minute quota windows)."""
    import copy
    waits = [60, 90, 120, 180]
    for attempt in range(5):
        try:
            # gspread batch_update мутирует ranges в переданном списке (добавляет 'Sheet'! prefix).
            # При retry это приводит к двойному/тройному префиксу — передаём deepcopy.
            _args = (copy.deepcopy(args[0]),) + args[1:] if args and getattr(fn, '__name__', '') == 'batch_update' else args
            return fn(*_args, **kwargs)
        except APIError as e:
            if e.response.status_code in (429, 503) and attempt < 4:
                wait = waits[min(attempt, len(waits) - 1)]
                import logging; logging.getLogger(__name__).warning(f'Sheets API {e.response.status_code}, attempt {attempt+1}/5, ждём {wait}s')
                time.sleep(wait)
            else:
                raise

def _gc(scopes=None):
    key = tuple(scopes or SCOPES_RO)
    if key not in _gc_cache:
        creds = Credentials.from_service_account_file(config.GOOGLE_SA_KEY, scopes=list(key))
        _gc_cache[key] = gspread.authorize(creds)
    return _gc_cache[key]

def _ss(file_id: str, rw=False):
    key = (file_id, rw)
    if key not in _ss_cache:
        gc = _gc(SCOPES_RW if rw else SCOPES_RO)
        _ss_cache[key] = _api_call(gc.open_by_key, file_id)
    return _ss_cache[key]

def _ws(file_id: str, sheet_name: str, rw=False):
    key = (file_id, sheet_name)
    key = (file_id, sheet_name, rw)
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

def _actual_header(file_id: str, sheet_name: str, standard: str) -> str:
    """Возвращает реальное имя колонки: если стандартное есть — оно; иначе пробует алиас."""
    cm = _col_map(file_id, sheet_name)
    if standard in cm:
        return standard
    alias = _SHEET_COL_ALIASES.get(sheet_name, {}).get(standard, standard)
    return alias if alias in cm else standard

def _col_letter(n: int) -> str:
    result = ''
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result

# Сбрасываем ws/header кэши после изменений структуры листа
def _invalidate_ws(file_id: str, sheet_name: str):
    _ws_cache.pop((file_id, sheet_name, True), None)
    _ws_cache.pop((file_id, sheet_name, False), None)
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



def read_all_sheets_batch(file_id: str, sheet_names: list) -> dict:
    """Читает все листы одним API-вызовом (batchGet). Возвращает {sheet_name: raw_rows}."""
    svc = _service()
    request = svc.spreadsheets().values().batchGet(
        spreadsheetId=file_id,
        ranges=sheet_names,
        valueRenderOption='FORMATTED_VALUE',
    )
    result = _api_call(request.execute)
    out = {}
    for vr in result.get('valueRanges', []):
        rng  = vr.get('range', '')
        name = rng.split('!')[0].strip("'")
        out[name] = vr.get('values', [])
    return out


def read_sheet(file_id: str, sheet_name: str, preloaded: list = None) -> list[dict]:
    if preloaded is not None:
        rows = preloaded
    else:
        ws = _ws(file_id, sheet_name)
        rows = _api_call(ws.get_all_values)
    if len(rows) < 2:
        return []
    headers = rows[1]
    # Определяем реальные имена колонок для фильтрации (с учётом алиасов)
    _aliases = _SHEET_COL_ALIASES.get(sheet_name, {})
    pos_col = _aliases.get('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА')
    el_col  = _aliases.get('ЭЛЕМЕНТ', 'ЭЛЕМЕНТ')
    data = []
    for row in rows[2:]:
        record = dict(zip(headers, row))
        pos = record.get(pos_col, '') or record.get('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', '')
        pos = pos.strip()
        el  = record.get(el_col, '') or record.get('ЭЛЕМЕНТ', '')
        el  = el.strip()
        if not pos and not el:
            continue
        if pos == "ИТОГО" or any(v.strip() == "ИТОГО" for v in row):
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
    # rev_aliases: реальное_имя → стандартное_имя (для нестандартных листов)
    _aliases = _SHEET_COL_ALIASES.get(sheet_name, {})
    _rev = {v: k for k, v in _aliases.items()}
    new_row = [''] * max_col
    for header, col_idx in cm.items():
        key = header if header in row_data else _rev.get(header, header)
        if key in row_data:
            new_row[col_idx - 1] = row_data[key]
    _api_call(ws.update, values=[new_row],
              range_name=f'A{insert_at}:{_col_letter(max_col)}{insert_at}')

    # Сбрасываем ws-кэш: структура листа изменилась
    _invalidate_ws(file_id, sheet_name)


def delete_row(file_id: str, sheet_name: str, row_num: int):
    """Удаляет строку по row_num (0-indexed, данные начинаются с row 3)."""
    ws = _ws(file_id, sheet_name, rw=True)
    _api_call(ws.delete_rows, row_num + 2)
    _invalidate_ws(file_id, sheet_name)


def apply_status_format(file_id: str, sheet_name: str, row_num: int, status: str):
    """Применяет цвет фона и жирный шрифт к ячейке СТАТУС по row_num."""
    style = STATUS_COLORS.get(status)
    if not style:
        return
    cm = _col_map(file_id, sheet_name, rw=True)
    col_idx = cm.get('СТАТУС')
    if not col_idx:
        return
    ws = _ws(file_id, sheet_name, rw=True)
    sheet_row = row_num + 2  # row_num — 0-based данные (строка 1), +2 = номер строки листа
    col_0 = col_idx - 1      # 0-based для Sheets API

    body = {'requests': [{
        'repeatCell': {
            'range': {
                'sheetId': ws.id,
                'startRowIndex': sheet_row - 1,
                'endRowIndex':   sheet_row,
                'startColumnIndex': col_0,
                'endColumnIndex':   col_0 + 1,
            },
            'cell': {
                'userEnteredFormat': {
                    'backgroundColor': style['bg'],
                    'textFormat': {'bold': style['bold']},
                    'horizontalAlignment': 'CENTER',
                }
            },
            'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)',
        }
    }]}
    try:
        _service().spreadsheets().batchUpdate(
            spreadsheetId=file_id, body=body).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("apply_status_format error: %s", e)


def update_cell_by_header(file_id: str, sheet_name: str, row_num: int, header: str, value):
    cm  = _col_map(file_id, sheet_name, rw=True)
    col = cm.get(_actual_header(file_id, sheet_name, header))
    if not col:
        return
    ws = _ws(file_id, sheet_name, rw=True)
    _api_call(ws.update, values=[[value]],
              range_name=f'{_col_letter(col)}{row_num + 2}')


# Следующая специализация по цепочке (для автозаполнения колонки БЛОК)
_NEXT_SPEC = {
    'ПЛАЗМА':    'СВЕРЛЕНИЕ',
    'ПИЛА':      'СВЕРЛЕНИЕ',
    'СВЕРЛЕНИЕ': 'СБОРКА',
    'СБОРКА':    'СВАРКА',
    'СВАРКА':    'ГРУНТОВКА',
    'ГРУНТОВКА': 'ПОКРАСКА',
}


def update_task_status(file_id: str, sheet_name: str, row_num: int,
                       status: str, comment: str = None, date_fact: str = None,
                       qty_done: int = None, block_val=None):
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

    # Колонка БЛОК: можно передать block_val явно, иначе авто по NEXT_SPEC
    next_spec = _NEXT_SPEC.get(sheet_name.upper())
    if cm.get('БЛОК') and (next_spec or block_val is not None):
        if block_val is not None:
            blok_to_write = block_val
        else:
            blok_to_write = f'⛔ {next_spec}' if status == 'БЛОК' else ''
        updates.append({'range': f'{_col_letter(cm["БЛОК"])}{sheet_row}', 'values': [[blok_to_write]]})

    if updates:
        _api_call(ws.batch_update, updates)
    # Применяем цветовое форматирование к ячейке СТАТУС
    apply_status_format(file_id, sheet_name, row_num, status)
