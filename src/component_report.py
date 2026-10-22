"""Сухой прогон: что изменится в блокировках СБОРКИ проекта, если включить количество деталей. Ничего не пишет.
Запуск: python src/component_report.py <project_id>"""
import sys
sys.path.insert(0, '/root/naryady/prod')
from src import db, sheets, deps_parts as dp, component_deps as cd, sync


def main(project_id):
    p = db.fetchone('SELECT project_name, sheet_id, folder_id FROM projects WHERE id=%s', [project_id])
    files = cd.find_assembly_files(p['folder_id'])
    print('файлы сборок:', [f['name'] for f in files])
    pw = cd._pw()
    asm_qty, need_total = cd.parse_files(files)
    ss = pw._gc(pw.SCOPES_RW).open_by_key(p['sheet_id'])
    elements, pos_spec, pos_total, standalone = cd.read_sheet_data(ss)
    rows, rep = cd.build_rows(asm_qty, need_total, elements, pos_spec, pos_total, standalone)
    print(f"сборок в файле: {rep['assemblies']}, строк с деталями: {rep['rows']}")
    print('сборки из файла, которых нет в листе СБОРКА:', rep['not_in_sheet'][:15], len(rep['not_in_sheet']))
    print('детали без листа (нет ни в ПЛАЗМЕ, ни в ПИЛЕ):', rep['unresolved'][:15], len(rep['unresolved']))
    print('расхождения «нужно по сборкам» vs «в листе деталей»:', len(rep['mismatches']))
    for m in rep['mismatches'][:25]:
        print('   ', m)

    # контекст как после записи строк в лист
    parts_rows = [(r[1], r[2], str(r[4]).lower(), float(r[5])) for r in rows]
    done_asm = {r['element']: float(r['d'] or 0) for r in db.fetchall(
        "SELECT element, SUM(quantity) AS d FROM work_orders WHERE project_name=%s AND sheet_name='СБОРКА' "
        "AND status='ВЫПОЛНЕНО' AND row_num>0 GROUP BY element", [p['project_name']])}
    stats = {}
    for rs in ('ПЛАЗМА', 'ПИЛА'):
        for r in db.fetchall(
                "SELECT lower(trim(position)) AS p, SUM(quantity) AS t, SUM(quantity) FILTER (WHERE status='ВЫПОЛНЕНО') AS d "
                "FROM work_orders WHERE project_name=%s AND sheet_name=%s AND position IS NOT NULL GROUP BY 1",
                [p['project_name'], rs]):
            stats[(rs, r['p'])] = (float(r['t'] or 0), float(r['d'] or 0))
    ctx = dp.Ctx(parts_rows, done_asm, stats)

    cur = {}
    v = ss.worksheet('СБОРКА').get_all_values()
    h = v[1]
    ie, ib, ik, ist = h.index('ЭЛЕМЕНТ'), h.index('БЛОК'), h.index('КОЛ-ВО'), h.index('СТАТУС')
    changed, same, n = [], 0, 0
    for r in v[2:]:
        if ie >= len(r) or not r[ie].strip() or r[ie].strip().upper() == 'ИТОГО':
            continue
        n += 1
        el = r[ie].strip()
        qty = int(float(str(r[ik]).replace(',', '.') or 0)) if r[ik] else 0
        old = r[ib].replace('\n', ' / ')
        res = dp.evaluate(ctx, el, qty)
        new = dp.format_block(res, qty) if res else '(нет данных, прежняя логика)'
        new1 = new.replace('\n', ' / ')
        if r[ist].strip().upper() == 'ВЫПОЛНЕНО':
            continue
        if old.strip() != new1.strip():
            changed.append((el, qty, old, new1))
        else:
            same += 1
    print(f'\nсборок в листе: {n}; без изменений: {same}; изменится: {len(changed)}')
    for c in changed:
        print('  ', c)


if __name__ == '__main__':
    main(int(sys.argv[1]))
