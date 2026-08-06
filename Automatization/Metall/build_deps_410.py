"""
build_deps_410.py - строит element_dependencies для Нефтеспецстрой ОСИ 4-10
и заполняет колонку БЛОК во всех листах.

Логика (из business.md):
 Отправочный элемент переходит на СБОРКУ только когда ВСЕ его детали
 готовы на ПИЛЕ И на ПЛАЗМЕ (и на СВЕРЛЕНИИ, если там есть).
"""

import sys
sys.path.insert(0, '/root/naryady/prod')
from dotenv import load_dotenv
load_dotenv('/root/naryady/prod/.env')
from src import db, sheets

PROJ = 'Нефтеспецстрой ОСИ 4-10'
FILE_ID = '1oQBW3uXv2n5H0WAuOvX1El7ln_hGt26t2FDKuSXUiNE'

def norm(s):
    """Нормализация: нижний регистр, без пробелов, З->3 (кириллическое З путают с цифрой 3)."""
    return s.lower().replace(' ', '').replace('з', '3')

def get_elements(sheet):
    rows = db.fetchall(
        "SELECT DISTINCT element FROM work_orders "
        "WHERE project_name=%s AND sheet_name=%s AND element IS NOT NULL AND element!=''",
        [PROJ, sheet]
    )
    return [r['element'] for r in rows]

def get_positions(sheet):
    rows = db.fetchall(
        "SELECT DISTINCT position FROM work_orders "
        "WHERE project_name=%s AND sheet_name=%s AND position IS NOT NULL AND position!=''",
        [PROJ, sheet]
    )
    return [r['position'] for r in rows]

plazma_elems    = get_elements('ПЛАЗМА')
pila_elems      = get_elements('ПИЛА')
sverlenie_elems = get_elements('СВЕРЛЕНИЕ')
sborka_pos      = get_positions('СБОРКА')
svarka_pos      = get_positions('СВАРКА')

print('ПЛАЗМА elements:', len(plazma_elems))
print('ПИЛА elements:', len(pila_elems))
print('СВЕРЛЕНИЕ elements:', len(sverlenie_elems))
print('СБОРКА positions:', len(sborka_pos))
print('СВАРКА positions:', len(svarka_pos))

def build_lookup(lst):
    return {norm(x): x for x in lst}

plazma_lookup    = build_lookup(plazma_elems)
pila_lookup      = build_lookup(pila_elems)
sverlenie_lookup = build_lookup(sverlenie_elems)

deps = set()
unmatched = []

for pos in sborka_pos:
    k = norm(pos)
    found_any = False

    if k in plazma_lookup:
        deps.add((pos, 'ПЛАЗМА', plazma_lookup[k], 'СБОРКА'))
        deps.add((pos, 'ПЛАЗМА', plazma_lookup[k], 'СВАРКА'))
        found_any = True

    if k in pila_lookup:
        deps.add((pos, 'ПИЛА', pila_lookup[k], 'СБОРКА'))
        deps.add((pos, 'ПИЛА', pila_lookup[k], 'СВАРКА'))
        found_any = True

    if k in sverlenie_lookup:
        deps.add((pos, 'СВЕРЛЕНИЕ', sverlenie_lookup[k], 'СБОРКА'))
        deps.add((pos, 'СВЕРЛЕНИЕ', sverlenie_lookup[k], 'СВАРКА'))
        found_any = True

    # СВАРКА всегда ждёт СБОРКУ
    deps.add((pos, 'СБОРКА', pos, 'СВАРКА'))

    if not found_any:
        unmatched.append(pos)

# СВАРКА позиции которых нет в СБОРКЕ
for pos in svarka_pos:
    if pos not in sborka_pos:
        k = norm(pos)
        if k in plazma_lookup:
            deps.add((pos, 'ПЛАЗМА', plazma_lookup[k], 'СВАРКА'))
        if k in pila_lookup:
            deps.add((pos, 'ПИЛА', pila_lookup[k], 'СВАРКА'))

deps_list = sorted(deps)

print(f'\nЗависимостей построено: {len(deps_list)}')
print(f'Позиций СБОРКИ без upstream: {len(unmatched)}')
if unmatched:
    print('  Без совпадений:', sorted(unmatched))

print('\nПримеры (первые 25):')
for d in deps_list[:25]:
    print(f'  [{d[3]}] {d[0]!r:20s} <- [{d[1]}] {d[2]!r}')

import sys
answer = sys.stdin.readline().strip()
if answer.lower() != 'y':
    print('Отмена.')
    sys.exit(0)

# Записываем в БД
deleted = db.execute("DELETE FROM element_dependencies WHERE project_name=%s", [PROJ])
print(f'\nУдалено старых: {deleted}')

inserted = 0
for elem, req_sheet, req_pos, wait_sheet in deps_list:
    db.execute(
        "INSERT INTO element_dependencies "
        "(project_name, element, requires_sheet, requires_position, waiting_sheet) "
        "VALUES (%s, %s, %s, %s, %s)",
        [PROJ, elem, req_sheet, req_pos, wait_sheet]
    )
    inserted += 1
print(f'Вставлено: {inserted}')

# Заполняем БЛОК
def col_letter(n):
    s = ''
    while n > 0:
        n -= 1
        s = chr(ord('A') + n % 26) + s
        n //= 26
    return s

print('\nЗаполняем БЛОК...')

for sheet_name in ['ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА']:
    cm = sheets._col_map(FILE_ID, sheet_name)
    block_col = cm.get('БЛОК')
    if not block_col:
        print(f'  {sheet_name}: нет БЛОК')
        continue

    ws = sheets._ws(FILE_ID, sheet_name, rw=True)
    work_rows = db.fetchall(
        "SELECT row_num, position, element, status FROM work_orders "
        "WHERE project_name=%s AND sheet_name=%s AND row_num > 0 ORDER BY row_num",
        [PROJ, sheet_name]
    )

    updates = []
    for r in work_rows:
        if r['status'] == 'ВЫПОЛНЕНО':
            val = ''
        else:
            lookup = (r['position'] or r['element'] or '').strip()
            if not lookup:
                val = ''
            else:
                dep_rows = db.fetchall(
                    "SELECT requires_sheet, requires_position FROM element_dependencies "
                    "WHERE project_name=%s AND waiting_sheet=%s AND element=%s",
                    [PROJ, sheet_name, lookup]
                )
                if not dep_rows:
                    val = ''
                else:
                    blocked = []
                    seen_sheets = set()
                    for d in dep_rows:
                        rs = d['requires_sheet']
                        if rs in seen_sheets:
                            continue
                        seen_sheets.add(rs)
                        rp = d['requires_position']
                        not_done = db.fetchone(
                            "SELECT COUNT(*) as cnt FROM work_orders "
                            "WHERE project_name=%s AND sheet_name=%s "
                            "AND (element=%s OR position=%s) AND status != 'ВЫПОЛНЕНО'",
                            [PROJ, rs, rp, rp]
                        )
                        total = db.fetchone(
                            "SELECT COUNT(*) as cnt FROM work_orders "
                            "WHERE project_name=%s AND sheet_name=%s "
                            "AND (element=%s OR position=%s)",
                            [PROJ, rs, rp, rp]
                        )
                        if not total or total['cnt'] == 0 or (not_done and not_done['cnt'] > 0):
                            blocked.append(rs)
                    val = '✅' if not blocked else '⛔ ' + ', '.join(sorted(set(blocked)))

        cell = f"{col_letter(block_col)}{r['row_num'] + 2}"
        updates.append({'range': cell, 'values': [[val]]})

    if updates:
        ws.batch_update(updates)
        blok_cnt = sum(1 for u in updates if u['values'][0][0].startswith('⛔'))
        ok_cnt   = sum(1 for u in updates if u['values'][0][0] == '✅')
        print(f'  {sheet_name}: {len(updates)} строк (⛔={blok_cnt}, ✅={ok_cnt})')
    else:
        print(f'  {sheet_name}: нет строк')

print('\nГОТОВО!')
