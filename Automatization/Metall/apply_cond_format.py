"""
Копирует условное форматирование колонки СТАТУС из СВАРКА
на вкладки ПОКРАСКА и ГРУНТОВКА.
"""
import os, time
import gspread
import requests as req_lib
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

SA_KEY  = os.path.join(os.path.dirname(__file__), 'service_account.json')
SCOPES  = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds   = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
gc      = gspread.authorize(creds)

SHEET_ID = '18xFgpV5QTXkNO-jVR4WCjgiHXVpuQ88KkiC9c62gzes'

def refresh():
    creds.refresh(Request())
    return {'Authorization': f'Bearer {creds.token}'}

def api_get(url, params):
    hdrs = refresh()
    for attempt in range(6):
        r = req_lib.get(url, params=params, headers=hdrs)
        if r.status_code == 429:
            print(f"  429, ждём 30с ({attempt+1})...")
            time.sleep(30)
            hdrs = refresh()
            continue
        r.raise_for_status()
        return r.json()

sh = gc.open_by_key(SHEET_ID)
BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"

# Читаем условное форматирование из СВАРКА
print("Читаем conditional formatting СВАРКА...")
data = api_get(BASE, {'fields': 'sheets(properties(title,sheetId),conditionalFormats)'})
time.sleep(2)

svarка_sid   = None
svarка_rules = []
pokraska_sid  = None
gruntovka_sid = None

for s in data.get('sheets', []):
    title = s['properties']['title']
    sid   = s['properties']['sheetId']
    if title == 'СВАРКА':
        svarка_sid   = sid
        svarка_rules = s.get('conditionalFormats', [])
    elif title == 'ПОКРАСКА':
        pokraska_sid = sid
    elif title == 'ГРУНТОВКА':
        gruntovka_sid = sid

print(f"  СВАРКА sheet id: {svarка_sid}, rules: {len(svarка_rules)}")
for i, rule in enumerate(svarка_rules):
    ranges = rule.get('ranges', [])
    fmt    = rule.get('booleanRule', {}) or rule.get('gradientRule', {})
    print(f"  Rule {i}: ranges={ranges}, condition type={rule.get('booleanRule', {}).get('condition', {}).get('type', '?')}")

# Находим правила для СТАТУС (колонка K = индекс 10)
# Ищем правила где startColumnIndex=10 или похожие
status_rules = []
for rule in svarка_rules:
    for rng in rule.get('ranges', []):
        col_start = rng.get('startColumnIndex', -1)
        col_end   = rng.get('endColumnIndex', -1)
        if col_start <= 10 < col_end:
            status_rules.append(rule)
            break

print(f"\n  Правил для СТАТУС (col K): {len(status_rules)}")
for r in status_rules:
    cond = r.get('booleanRule', {}).get('condition', {})
    fmt  = r.get('booleanRule', {}).get('format', {})
    print(f"    cond: {cond.get('type')} {cond.get('values')} -> bg: {fmt.get('backgroundColor')}")

# Строим запросы: добавить те же правила на ПОКРАСКА и ГРУНТОВКА
# В этих вкладках СТАТУС тоже в колонке K (индекс 10)
# Данные с строки 3 (индекс 2) до строки ~106
N_DATA_ROWS = 103
reqs = []

for target_sid in [pokraska_sid, gruntovka_sid]:
    if target_sid is None:
        continue
    for rule in status_rules:
        # Копируем правило, меняем sheetId и range на нужный лист
        new_rule = {
            'ranges': [{
                'sheetId': target_sid,
                'startRowIndex': 2,
                'endRowIndex': 2 + N_DATA_ROWS,
                'startColumnIndex': 10,  # K
                'endColumnIndex': 11,
            }],
        }
        if 'booleanRule' in rule:
            new_rule['booleanRule'] = rule['booleanRule']
        elif 'gradientRule' in rule:
            new_rule['gradientRule'] = rule['gradientRule']

        reqs.append({'addConditionalFormatRule': {
            'rule': new_rule,
            'index': 0
        }})

print(f"\nДобавляем {len(reqs)} правил на ПОКРАСКА + ГРУНТОВКА...")
if reqs:
    sh.batch_update({'requests': reqs})
    print("Готово!")
else:
    print("Правил для СТАТУС не найдено — проверь колонку.")
