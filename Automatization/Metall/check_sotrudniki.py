import sys, os, time
import gspread
import requests as req_lib
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

SA_KEY  = os.path.join(os.path.dirname(__file__), 'service_account.json')
SCOPES  = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds   = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
gc      = gspread.authorize(creds)

SHEET_ID = '18xFgpV5QTXkNO-jVR4WCjgiHXVpuQ88KkiC9c62gzes'
sh = gc.open_by_key(SHEET_ID)
ws = sh.worksheet('СОТРУДНИКИ')

# Строки 1-5 чтобы увидеть заголовки правой таблицы
rows = ws.get('A1:P5')
for i, row in enumerate(rows):
    print(f"Строка {i+1}: {row}")
