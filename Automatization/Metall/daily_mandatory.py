# -*- coding: utf-8 -*-
"""
daily_mandatory.py — ставит mandatory=true на незакрытые задачи прошедших дней.
Запускается по cron в 20:00 MSK (18:00 UTC).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gspread
from src import db, config
from google.oauth2.service_account import Credentials

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly',
]

def _creds():
    return Credentials.from_service_account_file(config.GOOGLE_SA_KEY, scopes=SCOPES)

def _col_letter(n: int) -> str:
    result = ''
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result

def run():
    # 1. Найти задачи которые нужно пометить обязательными
    rows = db.fetchall(
        """SELECT id, file_id, sheet_name, row_num
           FROM work_orders
           WHERE status = 'ПЛАН'
             AND date_plan IS NOT NULL
             AND date_plan < CURRENT_DATE
             AND mandatory = false""",
        []
    )
    if not rows:
        print("mandatory: нет задач для обновления")
        return

    # 2. Обновить БД
    db.execute(
        """UPDATE work_orders
           SET mandatory = true
           WHERE status = 'ПЛАН'
             AND date_plan IS NOT NULL
             AND date_plan < CURRENT_DATE
             AND mandatory = false""",
        []
    )
    print(f"mandatory: обновлено {len(rows)} строк в БД")

    # 3. Записать ДА в колонку ОБЯЗАТЕЛЬНАЯ в Google Sheets
    gc = gspread.authorize(_creds())
    # Кэш: file_id+sheet_name -> (ws, col_mandatory)
    ws_cache = {}
    written = 0
    for r in rows:
        key = (r['file_id'], r['sheet_name'])
        if key not in ws_cache:
            try:
                ss = gc.open_by_key(r['file_id'])
                ws = ss.worksheet(r['sheet_name'])
                headers = ws.row_values(2)
                col_map = {h.strip(): i + 1 for i, h in enumerate(headers) if h.strip()}
                col_mandatory = col_map.get('ОБЯЗАТЕЛЬНАЯ')
                ws_cache[key] = (ws, col_mandatory)
            except Exception as e:
                print(f"  ошибка открытия {r['sheet_name']}: {e}")
                ws_cache[key] = (None, None)

        ws, col_mandatory = ws_cache[key]
        if ws is None or col_mandatory is None:
            continue

        sheet_row = r['row_num'] + 2
        try:
            ws.update(values=[['ДА']], range_name=f'{_col_letter(col_mandatory)}{sheet_row}')
            written += 1
        except Exception as e:
            print(f"  ошибка записи строки {sheet_row}: {e}")

    print(f"mandatory: записано ДА в таблицу: {written} строк")

if __name__ == '__main__':
    run()
