# -*- coding: utf-8 -*-
"""
create_naryad.py — универсальное заполнение нарядного файла из оригинала.

Использование:
  python create_naryad.py --source <ORIG_ID> --target <TARGET_ID> --subtable 2

  --source    ID оригинального Google Sheets файла (с подтаблицами)
  --subtable  Номер подтаблицы (1=первая=главная, 2=Ось 4-10, 3=Ось 11-15)
              По умолчанию: 2
  --label     Название для логов (необязательно)
  --sheets    Через запятую листы для обработки (по умолчанию все нарядные)
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../'))

import gspread
from gspread.utils import ValueInputOption
from google.oauth2.service_account import Credentials

# ─── Конфиг ────────────────────────────────────────────────────────────────────

try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prod'))
    from src import config
    SA_KEY = config.GOOGLE_SA_KEY
except Exception:
    SA_KEY = os.environ.get('GOOGLE_SA_KEY', 'service_account.json')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

NARYAD_SHEETS = ['ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА']

GRAY  = {"red": 0.851, "green": 0.851, "blue": 0.851}
WHITE = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

# ─── Вспомогательные функции ───────────────────────────────────────────────────

def _v(row, idx):
    try:
        return row[idx].strip()
    except IndexError:
        return ''

def _num(val):
    """'5,578' → 5.578  |  пустая строка → ''"""
    if not val:
        return ''
    try:
        return float(val.replace(',', '.').replace(' ', ''))
    except ValueError:
        return val

def _is_zero(val):
    try:
        return float(str(val).replace(',', '.')) == 0
    except Exception:
        return str(val).strip() in ('', '0')

def _looks_like_header(text):
    text = text.upper()
    return any(kw in text for kw in ('ПОЗ.', 'ЧЕРТЕЖ', 'КОМПЛЕКТ', 'НАРЯД', 'КОЛ-ВО НА', 'МАССА ДЕТ'))

# ─── Поиск подтаблиц в оригинале ───────────────────────────────────────────────

def find_subtables(all_rows):
    """
    Возвращает список подтаблиц:
    [
      {
        'header_row': int,          # индекс строки с заголовками (0-based)
        'data_start': int,          # индекс первой строки данных
        'pos_col': int,             # колонка позиции
        'name_col': int | None,     # колонка названия элемента (None если нет)
        'qty_col': int | None,
        'mass_col': int | None,
        'totalmass_col': int | None,
        'pay_col': int | None,
        'has_name': bool,
      }
    ]
    """
    subtables = []

    # Ищем все строки где есть 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА'
    for row_idx, row in enumerate(all_rows):
        for col_idx, cell in enumerate(row):
            if 'ПОЗ.' in cell.upper() and 'ЧЕРТЕЖ' in cell.upper():
                st = _parse_subtable_header(all_rows, row_idx, col_idx)
                subtables.append(st)
                break  # одна подтаблица на строку

    # Если заголовки в разных строках — сортируем по (row, col)
    subtables.sort(key=lambda x: (x['header_row'], x['pos_col']))

    # Убираем дубликаты (одна и та же строка найдена дважды)
    seen = set()
    unique = []
    for st in subtables:
        key = (st['header_row'], st['pos_col'])
        if key not in seen:
            seen.add(key)
            unique.append(st)

    return unique

def _parse_subtable_header(all_rows, header_row_idx, pos_col):
    """Парсит заголовок подтаблицы и возвращает описание колонок."""
    row = all_rows[header_row_idx]

    # Строим col_map начиная с pos_col
    col_map = {}
    for i in range(pos_col, min(pos_col + 20, len(row))):
        h = row[i].strip().upper()
        if h:
            col_map[h] = i

    def find_col(*keywords):
        """Ищет колонку по ключевым словам (все должны присутствовать)."""
        for h, idx in col_map.items():
            if all(kw in h for kw in keywords):
                return idx
        return None

    qty_col       = find_col('КОЛ-ВО') or find_col('ВСЕГО') or find_col('ЭЛЕМЕНТ')
    mass_col      = find_col('МАССА', 'ЕД') or find_col('МАССА ЭЛЕМ')
    totalmass_col = find_col('МАССА ВСЕХ') or find_col('МАССА ВС')
    pay_col       = find_col('СУММА') or find_col('ОПЛАТ')

    # Если масса единицы не найдена — берём первую колонку с 'МАССА'
    if mass_col is None:
        mass_col = find_col('МАССА')
    # Если масса всех не найдена — берём вторую колонку с 'МАССА'
    if totalmass_col is None and mass_col is not None:
        for h, idx in col_map.items():
            if 'МАССА' in h and idx != mass_col:
                totalmass_col = idx
                break

    # Определяем есть ли колонка с названием элемента
    # Она идёт сразу после pos_col и содержит текст (не числовой заголовок)
    name_col = None
    has_name = False
    if pos_col + 1 < len(row):
        next_header = row[pos_col + 1].strip().upper()
        # Если следующая колонка — не числовой заголовок и не пустая
        if next_header and not any(kw in next_header for kw in
                ('КОЛ-ВО', 'МАССА', 'СУММА', 'ОПЛАТ', 'ВРЕМЯ', 'РЕЗ', 'ПОДПИСЬ', 'ИСПОЛН', 'ОТМЕТКА')):
            name_col = pos_col + 1
            has_name = True

    # data_start: ищем первую строку после header_row_idx с непустым pos_col
    data_start = header_row_idx + 1
    for i in range(header_row_idx + 1, min(header_row_idx + 5, len(all_rows))):
        cell = _v(all_rows[i], pos_col)
        if cell and not _looks_like_header(cell):
            data_start = i
            break

    return {
        'header_row':    header_row_idx,
        'data_start':    data_start,
        'pos_col':       pos_col,
        'name_col':      name_col,
        'qty_col':       qty_col,
        'mass_col':      mass_col,
        'totalmass_col': totalmass_col,
        'pay_col':       pay_col,
        'has_name':      has_name,
    }

# ─── Извлечение данных ──────────────────────────────────────────────────────────

def extract_data(all_rows, st):
    """Извлекает данные из подтаблицы st."""
    rows = []
    cur_pos = ''

    for row in all_rows[st['data_start']:]:
        pos  = _v(row, st['pos_col'])
        name = _v(row, st['name_col']) if st['name_col'] is not None else ''
        qty  = _v(row, st['qty_col']) if st['qty_col'] is not None else ''
        mass = _v(row, st['mass_col']) if st['mass_col'] is not None else ''
        totw = _v(row, st['totalmass_col']) if st['totalmass_col'] is not None else ''
        pay  = _v(row, st['pay_col']) if st['pay_col'] is not None else ''

        if pos:
            cur_pos = pos

        # Пропускаем заголовки
        if _looks_like_header(pos) or _looks_like_header(name):
            continue

        # Пропускаем пустые строки
        if not cur_pos and not name:
            continue

        # Пропускаем нулевые строки (позиция не входит в эти оси)
        if _is_zero(qty) and _is_zero(totw):
            continue

        if st['has_name']:
            # Тип A: ПЛАЗМА/ПИЛА — B=название, C=позиция
            if not name:
                continue
            rows.append(['', name, cur_pos, _num(qty), _num(mass), _num(totw), _num(pay),
                         '', '', 'НЕТ', 'ПЛАН', '', '', ''])
        else:
            # Тип B: СБОРКА/СВАРКА — B=позиция, нет колонки C
            rows.append(['', cur_pos, _num(qty), _num(mass), _num(totw), _num(pay),
                         '', '', 'НЕТ', 'ПЛАН', '', '', ''])

    return rows

# ─── Запись в шаблон ────────────────────────────────────────────────────────────

def write_to_target(ws_dest, data_rows, has_name):
    """Очищает, пишет данные + ИТОГО."""
    existing = ws_dest.get_all_values()
    if len(existing) > 2:
        ws_dest.batch_clear([f'A3:N{len(existing)}'])

    if not data_rows:
        print(f'    нет данных для {ws_dest.title}')
        return 0

    last_data = 2 + len(data_rows)

    if has_name:
        itogo = ['', 'ИТОГО', '',
                 f'=SUM(D3:D{last_data})', '',
                 f'=SUM(F3:F{last_data})',
                 f'=SUM(G3:G{last_data})',
                 '', '', '', '', '', '', '']
    else:
        itogo = ['', 'ИТОГО',
                 f'=SUM(C3:C{last_data})', '',
                 f'=SUM(E3:E{last_data})',
                 f'=SUM(F3:F{last_data})',
                 '', '', '', '', '', '', '', '']

    all_rows = data_rows + [[''] * 14, itogo]

    ws_dest.update(
        values=all_rows,
        range_name=f'A3:N{2 + len(all_rows)}',
        value_input_option=ValueInputOption.user_entered
    )

    return len(data_rows)

def apply_formatting(spreadsheet, sheet_names):
    """Копирует формат строки 3 на все строки, красит ИТОГО в серый."""
    fmt_requests = []
    itogo_requests = []

    for ws in spreadsheet.worksheets():
        if ws.title not in sheet_names:
            continue

        rows = ws.get_all_values()
        total = len(rows)
        if total < 3:
            continue

        num_cols = len(rows[0]) if rows else 14

        # Копируем формат строки 3 → все строки
        fmt_requests.append({
            "copyPaste": {
                "source": {
                    "sheetId": ws.id,
                    "startRowIndex": 2, "endRowIndex": 3,
                    "startColumnIndex": 0, "endColumnIndex": num_cols
                },
                "destination": {
                    "sheetId": ws.id,
                    "startRowIndex": 2, "endRowIndex": total,
                    "startColumnIndex": 0, "endColumnIndex": num_cols
                },
                "pasteType": "PASTE_FORMAT",
                "pasteOrientation": "NORMAL"
            }
        })

        # Находим строку ИТОГО
        for i, row in enumerate(rows):
            if row and row[1].strip().upper() == 'ИТОГО':
                # Серый + жирный на ИТОГО
                itogo_requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": i, "endRowIndex": i + 1,
                            "startColumnIndex": 0, "endColumnIndex": num_cols
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": GRAY,
                                "textFormat": {"bold": True, "fontSize": 9}
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat)"
                    }
                })
                # Белый фон на пустую строку перед ИТОГО
                if i > 0:
                    itogo_requests.append({
                        "repeatCell": {
                            "range": {
                                "sheetId": ws.id,
                                "startRowIndex": i - 1, "endRowIndex": i,
                                "startColumnIndex": 0, "endColumnIndex": num_cols
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": WHITE,
                                    "textFormat": {"bold": False}
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,textFormat)"
                        }
                    })
                break

    if fmt_requests:
        spreadsheet.batch_update({"requests": fmt_requests})
    if itogo_requests:
        spreadsheet.batch_update({"requests": itogo_requests})

# ─── Основная логика ────────────────────────────────────────────────────────────

def process_sheet(ws_orig, ws_dest, subtable_num, sheet_name):
    """Обрабатывает один лист."""
    all_rows = ws_orig.get_all_values()
    subtables = find_subtables(all_rows)

    if not subtables:
        print(f'  {sheet_name}: подтаблицы не найдены, пропускаем')
        return False

    print(f'  {sheet_name}: найдено подтаблиц: {len(subtables)}', end='')
    for i, st in enumerate(subtables):
        print(f' | #{i+1} pos_col={st["pos_col"]} has_name={st["has_name"]}', end='')
    print()

    if subtable_num > len(subtables):
        print(f'  {sheet_name}: подтаблица #{subtable_num} не найдена (всего {len(subtables)})')
        return False

    st = subtables[subtable_num - 1]
    data = extract_data(all_rows, st)
    count = write_to_target(ws_dest, data, st['has_name'])
    print(f'  {sheet_name}: записано {count} строк')
    return True

def main():
    parser = argparse.ArgumentParser(description='Заполнение нарядного файла из оригинала')
    parser.add_argument('--source',   required=True,  help='ID оригинального файла Google Sheets')
    parser.add_argument('--target',   required=True,  help='ID шаблона наряда для заполнения')
    parser.add_argument('--subtable', type=int, default=2, help='Номер подтаблицы (1=главная, 2=вторая, ...)')
    parser.add_argument('--label',    default='',     help='Название для логов')
    parser.add_argument('--sheets',   default='',     help='Листы через запятую (по умолчанию все нарядные)')
    args = parser.parse_args()

    label = args.label or f'Подтаблица #{args.subtable}'
    target_sheets = [s.strip().upper() for s in args.sheets.split(',')] if args.sheets else NARYAD_SHEETS

    print(f'\n=== Создание наряда: {label} ===')
    print(f'Источник: {args.source}')
    print(f'Цель:     {args.target}')
    print(f'Листы:    {target_sheets}')

    creds = Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
    gc = gspread.authorize(creds)

    orig   = gc.open_by_key(args.source)
    target = gc.open_by_key(args.target)

    processed_sheets = []

    for sheet_name in target_sheets:
        # Пробуем найти лист в оригинале (любой регистр)
        orig_ws = None
        for ws in orig.worksheets():
            if ws.title.upper() == sheet_name:
                orig_ws = ws
                break

        if orig_ws is None:
            print(f'  {sheet_name}: лист не найден в оригинале, пропускаем')
            continue

        target_ws = None
        for ws in target.worksheets():
            if ws.title.upper() == sheet_name:
                target_ws = ws
                break

        if target_ws is None:
            print(f'  {sheet_name}: лист не найден в целевом файле, пропускаем')
            continue

        ok = process_sheet(orig_ws, target_ws, args.subtable, sheet_name)
        if ok:
            processed_sheets.append(sheet_name)

    if processed_sheets:
        print(f'\nПрименяем форматирование...')
        apply_formatting(target, processed_sheets)
        print('Форматирование применено.')

    print(f'\nГотово! Обработано листов: {len(processed_sheets)}/{len(target_sheets)}')

if __name__ == '__main__':
    main()
