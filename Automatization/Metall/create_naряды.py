"""
Создание Google Sheets шаблона НАРЯДЫ
Проект: ОРША ТЕПЛОСЕТИ
Система управления производством металлоконструкций
"""

import os
import time
import gspread
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ============================================================
# НАСТРОЙКИ
# ============================================================
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
SPREADSHEET_TITLE = "НАРЯДЫ — ОРША ТЕПЛОСЕТИ"
DRIVE_FOLDER_NAME = "ЦЕХ МЕТАЛЛА"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Структура строк (0-based индексы):
# Строка 0: отступ сверху (пустая)
# Строка 1: заголовки колонок
# Строки 2-11: данные (10 строк)
# Строка 12: ИТОГО
MARGIN_ROW  = 0
HEADER_ROW  = 1
DATA_START  = 2
DATA_ROWS   = 10
TOTAL_ROW   = DATA_START + DATA_ROWS   # = 12

# Структура колонок (0-based):
# Колонка 0 (A): отступ слева (пустая)
# Колонки 1-14 (B-O): данные
MARGIN_COL  = 0
DATA_COL_S  = 1   # B
DATA_COL_E  = 15  # O+1

# Колонки данных (1-based буквы):
# B=ПОЗ, C=ЭЛЕМЕНТ, D=КОЛ-ВО, E=МАССА ЕД, F=МАССА ВСЕХ,
# G=СУММА, H=ИСПОЛНИТЕЛЬ, I=ДАТА ПЛАН, J=ПРИОРИТЕТ,
# K=ОБЯЗАТЕЛЬНАЯ, L=СТАТУС, M=КОММЕНТАРИЙ, N=ДАТА ФАКТ, O=ССЫЛКА

COL_HEADERS = [
    "ПОЗ. СОГЛАСНО ЧЕРТЕЖА", "ЭЛЕМЕНТ", "КОЛ-ВО", "МАССА ЕД. (кг)",
    "МАССА ВСЕХ (кг)", "СУММА К ОПЛАТЕ", "ИСПОЛНИТЕЛЬ", "ДАТА ПЛАН",
    "ПРИОРИТЕТ", "ОБЯЗАТЕЛЬНАЯ", "СТАТУС", "КОММЕНТАРИЙ",
    "ДАТА ФАКТ", "ССЫЛКА НА ЧЕРТЁЖ",
]

# ============================================================
# ЦВЕТА — принт-дружелюбная палитра, текст всегда чёрный
# ============================================================
def hex_to_rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2],16)/255, "green": int(h[2:4],16)/255, "blue": int(h[4:6],16)/255}

BLACK  = hex_to_rgb("000000")
WHITE  = hex_to_rgb("FFFFFF")

C = {
    "header_bg":   hex_to_rgb("DDE3ED"),
    "row_even":    hex_to_rgb("F4F7FB"),
    "row_odd":     WHITE,
    "formula_bg":  hex_to_rgb("EEF3FA"),
    "total_bg":    hex_to_rgb("C9D4E3"),
    "margin_bg":   WHITE,
    "plan_bg":     hex_to_rgb("FFF9C4"),
    "done_bg":     hex_to_rgb("E8F5E9"),
    "fail_bg":     hex_to_rgb("FFF3E0"),
    "block_bg":    hex_to_rgb("FFEBEE"),
    "sep_bg":      hex_to_rgb("F5F5F5"),
    "block_hdr":   hex_to_rgb("E8ECEF"),
}

WORK_SHEETS = [
    {"name": "ПЛАЗМА",    "color": hex_to_rgb("E65100"), "type": "ПЛАЗМА"},
    {"name": "ПИЛА",      "color": hex_to_rgb("1565C0"), "type": "ПИЛА"},
    {"name": "СВЕРЛЕНИЕ", "color": hex_to_rgb("6A1B9A"), "type": "СВЕРЛЕНИЕ"},
    {"name": "СБОРКА",    "color": hex_to_rgb("2E7D32"), "type": "СБОРКА"},
    {"name": "СВАРКА",    "color": hex_to_rgb("B71C1C"), "type": "СВАРКА"},
    {"name": "ПОКРАСКА",  "color": hex_to_rgb("880E4F"), "type": "ПОКРАСКА"},
]
REF_SHEETS = [
    {"name": "СОТРУДНИКИ", "color": hex_to_rgb("37474F")},
    {"name": "ТАРИФЫ",     "color": hex_to_rgb("37474F")},
]

# Вспомогательные колонки в СОТРУДНИКИ:
# A(0): отступ  B-E(1-4): основная таблица  F(5): разделитель  G-L(6-11): фильтры
WORK_TYPE_COL = {
    "ПЛАЗМА":    "G",
    "ПИЛА":      "H",
    "СВЕРЛЕНИЕ": "I",
    "СБОРКА":    "J",
    "СВАРКА":    "K",
    "ПОКРАСКА":  "L",
}

TEST_DATA = [
    ["ПС1", "СТ1",    8,  1.47, "", "", "ГАНЖА Иван", "17.06.2026", 1,  "ДА",  "ПЛАН", "", "", ""],
    ["ПС2", "СТ1",   16,  1.31, "", "", "ГАНЖА Иван", "17.06.2026", 2,  "ДА",  "ПЛАН", "", "", ""],
    ["ПС3", "СТ1",   24,  0.32, "", "", "ГАНЖА Иван", "17.06.2026", "",  "",   "ПЛАН", "", "", ""],
    ["ПС4", "СТ1",   12,  1.11, "", "", "ГАНЖА Иван", "17.06.2026", "",  "",   "ПЛАН", "", "", ""],
    ["ПС5", "СТ1-А",  8,  0.67, "", "", "ГАНЖА Иван", "18.06.2026", "",  "",   "",     "", "", ""],
    ["ПС6", "СТ1-А",  4,  0.76, "", "", "ГАНЖА Иван", "18.06.2026", "",  "",   "",     "", "", ""],
    ["ПС7", "Р1",    27,  1.45, "", "", "ГАНЖА Иван", "",           "",  "",   "",     "", "", ""],
    ["ПБ1", "Р1",     2,  3.62, "", "", "ГАНЖА Иван", "",           "",  "",   "",     "", "", ""],
]

EMPLOYEES = [
    ["ГАНЖА Иван",      "ПЛАЗМА",   "@ganzha",   "ДА"],
    ["ПЕРМИНОВ Сергей", "ПИЛА",     "@perminov", "ДА"],
    ["КОЗЛОВ Андрей",   "СБОРКА",   "@kozlov",   "ДА"],
    ["СИДОРОВ Пётр",    "СВАРКА",   "@sidorov",  "ДА"],
    ["ИВАНОВ Дмитрий",  "ПОКРАСКА", "@ivanov",   "ДА"],
]

TARIFFS = [
    ["ПЛАЗМА",   0,  1,    1.25,  "руб/кг"],
    ["ПЛАЗМА",   1,  5,    0.51,  "руб/кг"],
    ["ПЛАЗМА",   5,  10,   0.222, "руб/кг"],
    ["ПЛАЗМА",   10, 9999, 0.1,   "руб/кг"],
    ["ПИЛА",     0,  9999, 0.15,  "руб/кг"],
    ["СБОРКА",   0,  9999, 0.25,  "руб/кг"],
    ["СВАРКА",   0,  9999, 0.234, "руб/кг"],
    ["ПОКРАСКА", 0,  9999, 0.12,  "руб/кг"],
]

# Ширины колонок (px): A=отступ(18), B-O=данные
COL_WIDTHS = [18, 175, 95, 68, 108, 112, 112, 145, 90, 85, 105, 100, 145, 90, 145]

# ============================================================
# HELPERS
# ============================================================
def rng(sid, r1, r2, c1, c2):
    return {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}

def fmt(bg=None, fg=None, bold=False, sz=9, ha="LEFT", wrap=False):
    f = {"textFormat": {"bold": bold, "fontSize": sz, "fontFamily": "Arial",
                        "foregroundColor": fg if fg else BLACK},
         "horizontalAlignment": ha,
         "wrapStrategy": "WRAP" if wrap else "CLIP",
         "verticalAlignment": "MIDDLE",
         "padding": {"top": 4, "bottom": 4, "left": 6, "right": 6}}
    if bg:
        f["backgroundColor"] = bg
    return f

def repeat(sid, r1, r2, c1, c2, format_dict):
    return {"repeatCell": {"range": rng(sid, r1, r2, c1, c2),
                           "cell": {"userEnteredFormat": format_dict},
                           "fields": "userEnteredFormat"}}

def merge(sid, r1, r2, c1, c2):
    return {"mergeCells": {"range": rng(sid, r1, r2, c1, c2), "mergeType": "MERGE_ALL"}}

def row_h(sid, r1, r2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": r1, "endIndex": r2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}

def col_w(sid, c1, c2, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}

def freeze(sid, rows=0, cols=0):
    return {"updateSheetProperties": {
        "properties": {"sheetId": sid,
                       "gridProperties": {"frozenRowCount": rows, "frozenColumnCount": cols}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}}

def tab_color(sid, color):
    return {"updateSheetProperties": {
        "properties": {"sheetId": sid, "tabColorStyle": {"rgbColor": color}},
        "fields": "tabColorStyle"}}

def border_req(sid, r1, r2, c1, c2):
    inner = {"style": "SOLID", "colorStyle": {"rgbColor": hex_to_rgb("B0BEC5")}}
    outer = {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": hex_to_rgb("78909C")}}
    return {"updateBorders": {"range": rng(sid, r1, r2, c1, c2),
                              "top": outer, "bottom": outer,
                              "left": outer, "right": outer,
                              "innerHorizontal": inner, "innerVertical": inner}}

def validation(sid, r1, r2, col, ctype, values=None):
    cond = {"type": ctype}
    if values:
        cond["values"] = [{"userEnteredValue": v} for v in values]
    return {"setDataValidation": {
        "range": rng(sid, r1, r2, col, col+1),
        "rule": {"condition": cond, "showCustomUi": True, "strict": False}}}

def cond_status(sid, value, bg):
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [rng(sid, DATA_START, TOTAL_ROW, 11, 12)],  # L = index 11
        "booleanRule": {
            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
            "format": {"backgroundColor": bg,
                       "textFormat": {"foregroundColor": BLACK, "bold": True}}}}, "index": 0}}

def protect_range(sid, c1, c2):
    return {"addProtectedRange": {"protectedRange": {
        "range": rng(sid, DATA_START, TOTAL_ROW, c1, c2),
        "description": "Заполняется автоматически по формуле",
        "warningOnly": True}}}


def build_work_sheet_requests(sid, work_type):
    reqs = []

    # Высоты строк
    reqs.append(row_h(sid, MARGIN_ROW, MARGIN_ROW+1, 16))
    reqs.append(row_h(sid, HEADER_ROW, HEADER_ROW+1, 32))
    reqs.append(row_h(sid, DATA_START, TOTAL_ROW, 26))
    reqs.append(row_h(sid, TOTAL_ROW, TOTAL_ROW+1, 30))

    # Ширины колонок A-O
    for i, w in enumerate(COL_WIDTHS):
        reqs.append(col_w(sid, i, i+1, w))

    # Отступы
    reqs.append(repeat(sid, 0, TOTAL_ROW+1, MARGIN_COL, MARGIN_COL+1, fmt(bg=WHITE)))
    reqs.append(repeat(sid, MARGIN_ROW, MARGIN_ROW+1, 0, DATA_COL_E, fmt(bg=WHITE)))

    # Заголовки
    reqs.append(repeat(sid, HEADER_ROW, HEADER_ROW+1, DATA_COL_S, DATA_COL_E,
        fmt(bg=C["header_bg"], bold=True, sz=9, ha="CENTER", wrap=False)))

    # Данные — базовый фон
    reqs.append(repeat(sid, DATA_START, TOTAL_ROW, DATA_COL_S, DATA_COL_E,
        fmt(bg=WHITE, sz=9)))

    # Чередование строк
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [rng(sid, DATA_START, TOTAL_ROW, DATA_COL_S, DATA_COL_E)],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": "=MOD(ROW();2)=0"}]},
            "format": {"backgroundColor": C["row_even"]}}}, "index": 0}})

    # Формульные колонки F(5) и G(6)
    reqs.append(repeat(sid, DATA_START, TOTAL_ROW, 5, 7, fmt(bg=C["formula_bg"], sz=9)))

    # Строка ИТОГО
    reqs.append(merge(sid, TOTAL_ROW, TOTAL_ROW+1, DATA_COL_S, DATA_COL_S+2))
    reqs.append(repeat(sid, TOTAL_ROW, TOTAL_ROW+1, DATA_COL_S, DATA_COL_E,
        fmt(bg=C["total_bg"], bold=True, sz=9)))

    # Границы
    reqs.append(border_req(sid, HEADER_ROW, TOTAL_ROW+1, DATA_COL_S, DATA_COL_E))

    # Статусы — условное форматирование (col L = index 11)
    for value, bg in [
        ("ПЛАН",         C["plan_bg"]),
        ("ВЫПОЛНЕНО",    C["done_bg"]),
        ("НЕ ВЫПОЛНЕНО", C["fail_bg"]),
        ("БЛОК",         C["block_bg"]),
    ]:
        reqs.append(cond_status(sid, value, bg))

    # Валидация ИСПОЛНИТЕЛЬ (col H = index 7)
    col_letter = WORK_TYPE_COL.get(work_type, "G")
    reqs.append(validation(sid, DATA_START, TOTAL_ROW, 7, "ONE_OF_RANGE",
                           values=[f"=СОТРУДНИКИ!${col_letter}$4:${col_letter}$50"]))

    # Валидация ОБЯЗАТЕЛЬНАЯ (col K = index 10)
    reqs.append(validation(sid, DATA_START, TOTAL_ROW, 10, "ONE_OF_LIST",
                           values=["ДА", "НЕТ"]))

    # Валидация ПРИОРИТЕТ (col J = index 9)
    reqs.append({"setDataValidation": {
        "range": rng(sid, DATA_START, TOTAL_ROW, 9, 10),
        "rule": {"condition": {"type": "NUMBER_BETWEEN",
                               "values": [{"userEnteredValue": "1"},
                                          {"userEnteredValue": "10"}]},
                 "showCustomUi": True, "strict": False}}})

    # Валидация СТАТУС (col L = index 11)
    reqs.append(validation(sid, DATA_START, TOTAL_ROW, 11, "ONE_OF_LIST",
                           values=["ПЛАН", "ВЫПОЛНЕНО", "НЕ ВЫПОЛНЕНО", "БЛОК"]))

    # Защита формульных колонок F, G
    reqs.append(protect_range(sid, 5, 7))

    return reqs


def build_sotrudniki_requests(sid):
    reqs = []

    # Ширины: A=отступ, B-E=основная, F=разделитель, G-L=фильтры
    for i, w in enumerate([18, 155, 115, 140, 65, 18, 115, 115, 115, 115, 115, 115]):
        reqs.append(col_w(sid, i, i+1, w))

    # Высоты
    reqs.append(row_h(sid, 0, 1, 16))
    reqs.append(row_h(sid, 1, 2, 32))
    reqs.append(row_h(sid, 2, 3, 30))
    reqs.append(row_h(sid, 3, 53, 25))

    # Отступы
    reqs.append(repeat(sid, 0, 53, 0, 1, fmt(bg=WHITE)))
    reqs.append(repeat(sid, 0, 1, 0, 12, fmt(bg=WHITE)))

    # Левый блок: B-E (основная таблица)
    reqs.append(merge(sid, 1, 2, 1, 5))
    reqs.append(repeat(sid, 1, 2, 1, 5,
        fmt(bg=C["block_hdr"], bold=True, sz=10, ha="CENTER")))
    reqs.append(repeat(sid, 2, 3, 1, 5,
        fmt(bg=C["header_bg"], bold=True, sz=9, ha="CENTER")))
    reqs.append(repeat(sid, 3, 53, 1, 5, fmt(bg=WHITE, sz=9)))
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [rng(sid, 3, 53, 1, 5)],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": "=MOD(ROW();2)=0"}]},
            "format": {"backgroundColor": C["row_even"]}}}, "index": 0}})
    reqs.append(border_req(sid, 1, 53, 1, 5))
    reqs.append(validation(sid, 3, 53, 4, "ONE_OF_LIST", values=["ДА", "НЕТ"]))

    # Разделитель F(5)
    reqs.append(repeat(sid, 0, 53, 5, 6, fmt(bg=C["sep_bg"])))

    # Правый блок: G-L (фильтры по специализации)
    reqs.append(merge(sid, 1, 2, 6, 12))
    reqs.append(repeat(sid, 1, 2, 6, 12,
        fmt(bg=C["block_hdr"], bold=True, sz=10, ha="CENTER")))
    reqs.append(repeat(sid, 2, 3, 6, 12,
        fmt(bg=C["header_bg"], bold=True, sz=9, ha="CENTER")))
    reqs.append(repeat(sid, 3, 53, 6, 12, fmt(bg=WHITE, sz=9)))
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [rng(sid, 3, 53, 6, 12)],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": "=MOD(ROW();2)=0"}]},
            "format": {"backgroundColor": C["row_even"]}}}, "index": 0}})
    reqs.append(border_req(sid, 1, 53, 6, 12))

    return reqs


def build_tarify_requests(sid):
    reqs = []

    for i, w in enumerate([18, 130, 100, 100, 80, 100]):
        reqs.append(col_w(sid, i, i+1, w))

    reqs.append(row_h(sid, 0, 1, 16))
    reqs.append(row_h(sid, 1, 2, 32))
    reqs.append(row_h(sid, 2, 3, 30))
    reqs.append(row_h(sid, 3, 53, 25))

    reqs.append(repeat(sid, 0, 53, 0, 1, fmt(bg=WHITE)))
    reqs.append(repeat(sid, 0, 1, 0, 6, fmt(bg=WHITE)))

    reqs.append(merge(sid, 1, 2, 1, 6))
    reqs.append(repeat(sid, 1, 2, 1, 6,
        fmt(bg=C["block_hdr"], bold=True, sz=10, ha="CENTER")))
    reqs.append(repeat(sid, 2, 3, 1, 6,
        fmt(bg=C["header_bg"], bold=True, sz=9, ha="CENTER")))
    reqs.append(repeat(sid, 3, 53, 1, 6, fmt(bg=WHITE, sz=9)))
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [rng(sid, 3, 53, 1, 6)],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": "=MOD(ROW();2)=0"}]},
            "format": {"backgroundColor": C["row_even"]}}}, "index": 0}})
    reqs.append(border_req(sid, 1, 53, 1, 6))
    return reqs


def build_data_updates(sheet_name, work_type, test_data=None):
    """
    Строки (1-based): 1=отступ, 2=заголовки, 3-12=данные, 13=ИТОГО
    Колонки: A=отступ, B=ПОЗ...O=ССЫЛКА
    F = МАССА ВСЕХ = D*E
    G = СУММА = F * тариф (из ТАРИФЫ, смещённые на 1: B=тип,C=от,D=до,E=ставка)
    """
    updates = []

    updates.append({"range": f"{sheet_name}!B2", "values": [COL_HEADERS]})

    def f_formula(rn):
        return f'=IF(D{rn}="";"";D{rn}*E{rn})'

    def g_formula(rn, wt):
        return (f'=IF(F{rn}="";"";F{rn}*IFERROR(INDEX(ТАРИФЫ!E:E;'
                f'MATCH(1;(ТАРИФЫ!B:B="{wt}")*(ТАРИФЫ!C:C<=E{rn})*(ТАРИФЫ!D:D>E{rn});0));0))')

    if test_data:
        for i, row in enumerate(test_data):
            rn = DATA_START + i + 1   # 1-based row (3, 4, ...)
            row = list(row)
            row[4] = f_formula(rn)
            row[5] = g_formula(rn, work_type)
            updates.append({"range": f"{sheet_name}!B{rn}", "values": [row]})

    start_i = len(test_data) if test_data else 0
    for i in range(start_i, DATA_ROWS):
        rn = DATA_START + i + 1
        updates.append({"range": f"{sheet_name}!F{rn}",
                        "values": [[f_formula(rn), g_formula(rn, work_type)]]})

    tn = TOTAL_ROW + 1
    updates.append({"range": f"{sheet_name}!B{tn}", "values": [["ИТОГО"]]})
    updates.append({"range": f"{sheet_name}!D{tn}", "values": [[f"=SUM(D3:D{TOTAL_ROW})"]]})
    updates.append({"range": f"{sheet_name}!F{tn}", "values": [[f"=SUM(F3:F{TOTAL_ROW})"]]})
    updates.append({"range": f"{sheet_name}!G{tn}", "values": [[f"=SUM(G3:G{TOTAL_ROW})"]]})

    return updates


def send_requests(sheets_service, ss_id, requests, batch_size=30, delay=5):
    total = len(requests)
    print(f"  Всего: {total}")
    for i in range(0, total, batch_size):
        batch = requests[i:i+batch_size]
        for attempt in range(3):
            try:
                sheets_service.spreadsheets().batchUpdate(
                    spreadsheetId=ss_id, body={"requests": batch}).execute()
                break
            except Exception as e:
                err = str(e)
                if "429" in err:
                    wait = (attempt + 1) * 10
                    print(f"  Rate limit, жду {wait}с...")
                    time.sleep(wait)
                else:
                    print(f"  Ошибка: {e}")
                    break
        print(f"  {min(i+batch_size, total)}/{total}")
        if i + batch_size < total:
            time.sleep(delay)


def find_drive_folder(drive_service, folder_name):
    results = drive_service.files().list(
        q=(f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
           f" and trashed=false"),
        fields="files(id, name)"
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def main():
    print("Подключение к Google API...")
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)

    all_sheets = WORK_SHEETS + REF_SHEETS
    print(f"Создание '{SPREADSHEET_TITLE}'...")
    body = {
        "properties": {"title": SPREADSHEET_TITLE, "locale": "ru_RU"},
        "sheets": [{"properties": {"title": cfg["name"],
                                   "gridProperties": {"rowCount": 100, "columnCount": 20}}}
                   for cfg in all_sheets]
    }
    result = sheets_service.spreadsheets().create(body=body).execute()
    ss_id = result["spreadsheetId"]
    print(f"ID: {ss_id}")

    print(f"Перемещение в '{DRIVE_FOLDER_NAME}'...")
    folder_id = find_drive_folder(drive_service, DRIVE_FOLDER_NAME)
    if folder_id:
        file_meta = drive_service.files().get(fileId=ss_id, fields="parents").execute()
        old_parents = ",".join(file_meta.get("parents", []))
        drive_service.files().update(fileId=ss_id, addParents=folder_id,
                                     removeParents=old_parents, fields="id").execute()
        print(f"  OK")
    else:
        print(f"  Папка не найдена, файл в корне Drive")

    spreadsheet = gc.open_by_key(ss_id)
    sheet_map = {ws.title: ws for ws in spreadsheet.worksheets()}

    # ---- Шаг 1: форматирование (merge до freeze) ----
    print("Форматирование...")
    fmt_reqs = []
    freeze_reqs = []

    for cfg in WORK_SHEETS:
        ws = sheet_map[cfg["name"]]
        fmt_reqs.append(tab_color(ws.id, cfg["color"]))
        fmt_reqs.extend(build_work_sheet_requests(ws.id, cfg["type"]))
        freeze_reqs.append(freeze(ws.id, rows=HEADER_ROW+1, cols=0))

    sot_id = sheet_map["СОТРУДНИКИ"].id
    tar_id = sheet_map["ТАРИФЫ"].id
    fmt_reqs.append(tab_color(sot_id, REF_SHEETS[0]["color"]))
    fmt_reqs.append(tab_color(tar_id, REF_SHEETS[1]["color"]))
    fmt_reqs.extend(build_sotrudniki_requests(sot_id))
    fmt_reqs.extend(build_tarify_requests(tar_id))
    fmt_reqs.append({"updateSpreadsheetProperties": {
        "properties": {"locale": "ru_RU"}, "fields": "locale"}})

    send_requests(sheets_service, ss_id, fmt_reqs, batch_size=30, delay=5)

    # ---- Шаг 2: freeze ----
    print("Заморозка...")
    send_requests(sheets_service, ss_id, freeze_reqs, batch_size=10, delay=2)

    # ---- Шаг 3: данные ----
    print("Заполнение данных...")
    all_updates = []

    for cfg in WORK_SHEETS:
        test = TEST_DATA if cfg["name"] == "ПЛАЗМА" else None
        all_updates.extend(build_data_updates(cfg["name"], cfg["type"], test))

    # СОТРУДНИКИ
    all_updates.append({"range": "СОТРУДНИКИ!B2",
                        "values": [["ОСНОВНОЙ СПРАВОЧНИК СОТРУДНИКОВ"]]})
    all_updates.append({"range": "СОТРУДНИКИ!B3",
                        "values": [["ФИО", "СПЕЦИАЛИЗАЦИЯ", "TELEGRAM USERNAME", "АКТИВЕН"]]})
    for i, emp in enumerate(EMPLOYEES):
        all_updates.append({"range": f"СОТРУДНИКИ!B{i+4}", "values": [emp]})

    all_updates.append({"range": "СОТРУДНИКИ!G2",
                        "values": [["СПИСОК СОТРУДНИКОВ ПО СПЕЦИАЛИЗАЦИИ (для выпадающих списков)"]]})
    all_updates.append({"range": "СОТРУДНИКИ!G3",
                        "values": [list(WORK_TYPE_COL.keys())]})
    for work_type, col_letter in WORK_TYPE_COL.items():
        formula = f'=IFERROR(FILTER($B$4:$B$100;$C$4:$C$100="{work_type}");"")'
        all_updates.append({
            "range": f"СОТРУДНИКИ!{col_letter}4",
            "values": [[formula]]
        })

    # ТАРИФЫ
    all_updates.append({"range": "ТАРИФЫ!B2", "values": [["ТАРИФЫ НА ВИДЫ РАБОТ"]]})
    all_updates.append({"range": "ТАРИФЫ!B3",
                        "values": [["ТИП РАБОТЫ", "УСЛОВИЕ ОТ", "УСЛОВИЕ ДО",
                                    "СТАВКА", "ЕДИНИЦА"]]})
    for i, tar in enumerate(TARIFFS):
        all_updates.append({"range": f"ТАРИФЫ!B{i+4}", "values": [tar]})

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=ss_id,
        body={"valueInputOption": "USER_ENTERED", "data": all_updates}
    ).execute()

    url = f"https://docs.google.com/spreadsheets/d/{ss_id}"
    print(f"\nГОТОВО!")
    print(f"Ссылка: {url}")
    return url


if __name__ == "__main__":
    main()
