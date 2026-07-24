"""Читает данные из листа площадей покраски (Щучин)."""
import os
import gspread
from google.oauth2.service_account import Credentials

SA_KEY = os.path.join(os.path.dirname(__file__), 'service_account.json')
SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds  = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
gc     = gspread.authorize(creds)

SOURCE_ID = '1uljciArcIHiMRKpW1c5ZSafPkU1Lu0Fv'
sh = gc.open_by_key(SOURCE_ID)

print("Листы:")
for ws in sh.worksheets():
    print(f"  gid={ws.id}  title='{ws.title}'")

# gid=608177243
ws = sh.get_worksheet_by_id(608177243)
print(f"\nЛист: '{ws.title}'")
rows = ws.get_all_values()
print(f"Строк: {len(rows)}")
for i, row in enumerate(rows[:15]):
    print(f"  [{i+1}] {row}")
if len(rows) > 15:
    print(f"  ...")
    print(f"  [{len(rows)}] {rows[-1]}")
