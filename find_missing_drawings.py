"""
Находим позиции без чертежей (ССЫЛКА НА ЧЕРТЁЖ пустая).
Только листы СБОРКА и СВАРКА — там чертежи актуальны для сборочных единиц.
Также проверяем ПИЛА и ПЛАЗМА.
"""
import sys
sys.path.insert(0, '/root/naryady/prod/src')
import src.sheets as sh
import src.config as config

FILE_ID = '18xFgpV5QTXkNO-jVR4WCjgiHXVpuQ88KkiC9c62gzes'

svc = sh._service()

for sheet_name in config.WORK_SHEETS:
    cm = sh._col_map(FILE_ID, sheet_name)
    pos_ci  = (cm.get('ПОЗ. СОГЛАСНО ЧЕРТЕЖА') or cm.get('Марка') or 0) - 1
    elem_ci = (cm.get('ЭЛЕМЕНТ') or 0) - 1
    link_ci = (cm.get('ССЫЛКА НА ЧЕРТЁЖ') or 0) - 1

    if link_ci < 0:
        continue

    resp = svc.spreadsheets().get(
        spreadsheetId=FILE_ID,
        ranges=[f'{sheet_name}!A3:ZZ'],
        includeGridData=True,
        fields='sheets(data(rowData(values(userEnteredValue,userEnteredFormat(textFormat(bold))))))'
    ).execute()
    all_rows = resp['sheets'][0].get('data', [{}])[0].get('rowData', [])

    missing = []
    for i, row in enumerate(all_rows):
        vals = row.get('values', [])
        pos_val = ''
        if pos_ci >= 0 and pos_ci < len(vals):
            uev = vals[pos_ci].get('userEnteredValue', {})
            pos_val = uev.get('stringValue', '') or str(uev.get('numberValue', ''))
            bold = vals[pos_ci].get('userEnteredFormat', {}).get('textFormat', {}).get('bold', False)
            if bold:
                continue  # разделитель секции

        if not pos_val:
            continue

        link_val = ''
        if link_ci < len(vals):
            uev = vals[link_ci].get('userEnteredValue', {})
            link_val = uev.get('stringValue', '') or uev.get('formulaValue', '')

        if not link_val:
            elem_val = ''
            if elem_ci >= 0 and elem_ci < len(vals):
                uev = vals[elem_ci].get('userEnteredValue', {})
                elem_val = uev.get('stringValue', '') or str(uev.get('numberValue', ''))
            missing.append((i + 3, pos_val, elem_val))

    if missing:
        print(f'\n[{sheet_name}] нет чертежа: {len(missing)} позиций')
        for row_num, pos, elem in missing:
            e = f' / {elem}' if elem else ''
            print(f'  строка {row_num}: {pos}{e}')
    else:
        print(f'[{sheet_name}] ✅ все чертежи есть')
