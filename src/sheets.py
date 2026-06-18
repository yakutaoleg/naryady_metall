import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from src import config

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]

def _creds():
    return Credentials.from_service_account_file(config.GOOGLE_SA_KEY, scopes=SCOPES)

def find_active_files():
    service = build('drive', 'v3', credentials=_creds())
    active = []

    def search_in_folder(folder_id, project_name):
        q = (
            f"name = '{config.ACTIVE_FILE_NAME}' "
            f"and mimeType = 'application/vnd.google-apps.spreadsheet' "
            f"and trashed = false "
            f"and '{folder_id}' in parents"
        )
        result = service.files().list(q=q, fields='files(id,name)', pageSize=50).execute()
        for f in result.get('files', []):
            active.append({
                'file_id': f['id'],
                'file_name': f['name'],
                'project_name': project_name
            })

    search_in_folder(config.DRIVE_FOLDER_ID, 'Без проекта')

    q_folders = (
        f"mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false "
        f"and '{config.DRIVE_FOLDER_ID}' in parents"
    )
    folders_res = service.files().list(q=q_folders, fields='files(id,name)', pageSize=100).execute()
    for folder in folders_res.get('files', []):
        search_in_folder(folder['id'], folder['name'])

    return active

def read_sheet(file_id: str, sheet_name: str) -> list[dict]:
    gc = gspread.authorize(_creds())
    ss = gc.open_by_key(file_id)
    ws = ss.worksheet(sheet_name)
    rows = ws.get_all_values()
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


def update_task_status(file_id: str, sheet_name: str, row_num: int,
                       status: str, comment: str = None, date_fact: str = None):
    """Write status/comment/date_fact back to Google Sheets."""
    from google.oauth2.service_account import Credentials as _Creds
    scopes_rw = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive.readonly'
    ]
    creds = _Creds.from_service_account_file(config.GOOGLE_SA_KEY, scopes=scopes_rw)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(file_id)
    ws = ss.worksheet(sheet_name)
    sheet_row = row_num + 2  # row 1=margin, row 2=headers, row 3+=data
    # Columns: L=STATUS(12), M=COMMENT(13), N=DATE_FACT(14) — 1-based
    ws.update(values=[[status]], range_name=f'L{sheet_row}')
    if comment is not None:
        ws.update(values=[[comment]], range_name=f'M{sheet_row}')
    if date_fact is not None:
        ws.update(values=[[date_fact]], range_name=f'N{sheet_row}')
