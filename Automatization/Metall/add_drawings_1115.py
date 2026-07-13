#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_drawings_1115.py -- добавляет ссылки на чертежи в файл Ось 11-15
"""
import sys, os, json, re
sys.path.insert(0, os.path.abspath('.'))

import gspread
from gspread.utils import ValueInputOption
from google.oauth2.service_account import Credentials
from src import config

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

ID_1115 = '18xFgpV5QTXkNO-jVR4WCjgiHXVpuQ88KkiC9c62gzes'
DRIVE_FILES_PATH = '/root/naryady/drive_files_1115.json'


def _norm_elem(code):
    """Нормализует код/имя элемента: lower, убирает пробелы, тире и точки."""
    return re.sub(r'[\s\-\.]', '', code.lower().strip())


def _norm_part(s):
    """Нормализует название детали: lower, убирает пробелы и точки."""
    return re.sub(r'[\s\.]', '', s.lower())


def _folder_clean(folder_path):
    """Берёт последний компонент пути и убирает числовой префикс.
    Для 1115 нет суффикса (PNG), подпапки включаем по последнему компоненту.
    """
    last = folder_path.split('/')[-1]
    name = re.sub(r'^\d+\s*', '', last)
    return name.strip()


def build_folder_map(files):
    from collections import defaultdict
    folder_map = defaultdict(list)
    for f in files:
        if f['name'] == 'Thumbs.db':
            continue
        clean = _folder_clean(f['folder'])
        if not clean:
            continue
        folder_map[clean].append(f)
        # Для подпапок добавляем также полный путь (без числовых префиксов)
        if '/' in f['folder']:
            parts = f['folder'].split('/')
            full_clean = ' '.join(re.sub(r'^\d+\s*', '', p).strip() for p in parts)
            if full_clean != clean:
                folder_map[full_clean].append(f)
    return folder_map


def find_folder_by_element(folder_map, element_code):
    """Ищет папку по коду или полному имени элемента.
    1. Точное совпадение нормализованного имени (для полных имён: 'Связь СВ1')
    2. Элемент как токен в имени папки (для коротких кодов: 'СВ1')
    3. Элемент как подстрока последнего токена ('К1' в 'К1,2')
    """
    elem_norm = _norm_elem(element_code)

    # 1. Полное нормализованное совпадение
    for folder_name, files in folder_map.items():
        if _norm_elem(folder_name) == elem_norm:
            return folder_name, files

    # 2. Токен в имени папки
    for folder_name, files in folder_map.items():
        for token in folder_name.split():
            if _norm_elem(token) == elem_norm:
                return folder_name, files
            # К1,2 pattern
            m = re.match(r'^([А-Яа-яA-Za-z]+)(\d+),(\d+)$', token)
            if m:
                letters, d1, d2 = m.group(1), m.group(2), m.group(3)
                if elem_norm in (_norm_elem(letters + d1), _norm_elem(letters + d2)):
                    return folder_name, files

    # 3. Элемент как подстрока последнего токена
    for folder_name, files in folder_map.items():
        if folder_name.split():
            last = folder_name.split()[-1]
            if elem_norm in _norm_elem(last):
                return folder_name, files

    # 4. Папка начинается с elem_norm (для "Кронштейн КВ1 (кронштейн кондиционера)")
    for folder_name, files in folder_map.items():
        if _norm_elem(folder_name).startswith(elem_norm) and len(elem_norm) >= 4:
            return folder_name, files

    return None, []


def _match_file(f, name_norm, length_str):
    fname = f['name']
    if fname.lower().endswith('.png'):
        fname = fname[:-4]
    prefix = fname.split(' _ ')[0].strip() if ' _ ' in fname else fname
    m_file_len = re.search(r'\s+[Ll]=([0-9,\.]+)\s*$', prefix)
    if m_file_len:
        file_len = m_file_len.group(1)
        file_base = prefix[:m_file_len.start()].strip()
    else:
        file_len = None
        file_base = prefix
    if _norm_part(file_base) == name_norm:
        if not length_str or not file_len or length_str == file_len:
            return True
    return False


def find_file_by_name(files, part_name):
    m_len = re.search(r'\(([0-9,\.]+)\)\s*$', part_name)
    length_str = m_len.group(1) if m_len else None
    clean_name = re.sub(r'\s*\([^)]*\)\s*$', '', part_name).strip()
    name_norm = _norm_part(clean_name)
    for f in files:
        if _match_file(f, name_norm, length_str):
            return f
    return None


def find_file_global(folder_map, part_name):
    """Fallback: ищет файл по всем папкам."""
    m_len = re.search(r'\(([0-9,\.]+)\)\s*$', part_name)
    length_str = m_len.group(1) if m_len else None
    clean_name = re.sub(r'\s*\([^)]*\)\s*$', '', part_name).strip()
    name_norm = _norm_part(clean_name)
    for folder_name, files in folder_map.items():
        for f in files:
            if _match_file(f, name_norm, length_str):
                return folder_name, f
    return None, None


def find_assembly_file(files):
    """Главный чертёж сборки: файл с .000 в номере, не спецификация."""
    for f in files:
        nl = f['name'].lower()
        if not nl.startswith('спецификация') and '.000.' in f['name']:
            return f
    for f in files:
        if not f['name'].lower().startswith('спецификация'):
            return f
    return None


def make_url(file_id):
    return f'https://drive.google.com/file/d/{file_id}/view?usp=drivesdk'


def process_sheet(ws, folder_map, sheet_type, dry_run=False):
    rows = ws.get_all_values()
    if len(rows) < 3:
        return 0

    headers = rows[1]
    link_col_idx = None
    for i, h in enumerate(headers):
        if 'ССЫЛКА' in h.upper() and 'ЧЕРТЁЖ' in h.upper():
            link_col_idx = i
            break
    if link_col_idx is None:
        print(f'    ССЫЛКА НА ЧЕРТЁЖ не найдена!')
        return 0

    name_col = 1
    elem_col = 2 if sheet_type == 'A' else None

    updates = []
    matched = 0
    unmatched = []

    for row_idx, row in enumerate(rows[2:], start=3):
        if not row:
            continue
        name = row[name_col].strip() if name_col < len(row) else ''
        if not name or name.upper() == 'ИТОГО':
            continue
        existing = row[link_col_idx].strip() if link_col_idx < len(row) else ''
        if existing:
            continue

        if sheet_type == 'A':
            elem = row[elem_col].strip() if elem_col < len(row) else ''
            if not elem:
                continue
            folder_name, folder_files = find_folder_by_element(folder_map, elem)
            if folder_files:
                file_obj = find_file_by_name(folder_files, name)
                if not file_obj:
                    global_folder, file_obj = find_file_global(folder_map, name)
                    if file_obj:
                        print(f'      [fallback] row {row_idx}: "{name}" in "{global_folder}" (elem="{elem}"->"{folder_name}")')
                if file_obj:
                    col_letter = chr(65 + link_col_idx)
                    updates.append({'range': f'{col_letter}{row_idx}', 'values': [[make_url(file_obj['id'])]]})
                    matched += 1
                else:
                    unmatched.append(f'row {row_idx}: no file "{name}" in any folder (elem="{elem}")')
            else:
                global_folder, file_obj = find_file_global(folder_map, name)
                if file_obj:
                    print(f'      [fallback] row {row_idx}: "{name}" in "{global_folder}" (elem="{elem}" unmatched)')
                    col_letter = chr(65 + link_col_idx)
                    updates.append({'range': f'{col_letter}{row_idx}', 'values': [[make_url(file_obj['id'])]]})
                    matched += 1
                else:
                    unmatched.append(f'row {row_idx}: no folder for elem="{elem}", not found globally')
        else:
            # СБОРКА/СВАРКА: B = полное имя сборки
            elem_code = name
            folder_name, folder_files = find_folder_by_element(folder_map, elem_code)
            if folder_files:
                file_obj = find_assembly_file(folder_files)
                if file_obj:
                    col_letter = chr(65 + link_col_idx)
                    updates.append({'range': f'{col_letter}{row_idx}', 'values': [[make_url(file_obj['id'])]]})
                    matched += 1
                else:
                    unmatched.append(f'row {row_idx}: no .000 file in "{folder_name}" for "{elem_code}"')
            else:
                unmatched.append(f'row {row_idx}: no folder for "{elem_code}"')

    if updates and not dry_run:
        ws.batch_update(updates, value_input_option=ValueInputOption.user_entered)
    elif updates and dry_run:
        print(f'    [DRY RUN] updates: {len(updates)}')
        for u in updates[:3]:
            print(f'      {u["range"]}: ...{u["values"][0][0][-40:]}')

    print(f'    matched: {matched}, unmatched: {len(unmatched)}')
    for u in unmatched:
        print(f'      ! {u}')
    return matched


def main(dry_run=False):
    with open(DRIVE_FILES_PATH, encoding='utf-8') as fp:
        files = json.load(fp)
    folder_map = build_folder_map(files)
    print(f'Folders in index: {len(folder_map)}')

    creds = Credentials.from_service_account_file(config.GOOGLE_SA_KEY, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(ID_1115)

    sheet_types = {'ПЛАЗМА': 'A', 'ПИЛА': 'A', 'СБОРКА': 'B', 'СВАРКА': 'B'}
    total = 0
    for sheet_name, stype in sheet_types.items():
        print(f'\n=== {sheet_name} (type {stype}) ===')
        try:
            ws = ss.worksheet(sheet_name)
            n = process_sheet(ws, folder_map, stype, dry_run=dry_run)
            total += n
        except Exception as e:
            import traceback
            print(f'  Error: {e}')
            traceback.print_exc()
    print(f'\nTotal: {total} links {"(dry run)" if dry_run else "written"}')


if __name__ == '__main__':
    dry = '--dry' in sys.argv or '-n' in sys.argv
    main(dry_run=dry)
