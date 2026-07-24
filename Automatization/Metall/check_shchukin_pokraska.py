"""Проверяет содержимое ПОКРАСКА и ГРУНТОВКА в Щучин Кули."""
import os
import gspread
from google.oauth2.service_account import Credentials

SA_KEY = os.path.join(os.path.dirname(__file__), 'service_account.json')
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds  = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
gc     = gspread.authorize(creds)

sh = gc.open_by_key('1RgfBpEwqh4U6P9ivBJtEXvlvlaGFaL4E0lHLW95CKuY')

for tab in ['ПОКРАСКА', 'ГРУНТОВКА']:
    ws   = sh.worksheet(tab)
    rows = ws.get_all_values()
    print(f"\n=== {tab} ({len(rows)} строк) ===")
    for i, row in enumerate(rows[:5]):
        print(f"  [{i+1}] {row}")
    print(f"  ...")
    print(f"  [{len(rows)}] {rows[-1]}")
