"""Заполнение листа ЗАВИСИМОСТИ количеством деталей на сборку (колонки F «ДЕТАЛЬ» и G «КОЛ-ВО НА 1 СБОРКУ»).

Источник — файл сборок проекта (например «ВСЕ_сборочные…xlsx»): блок «Поз.=Б4в Количество=1» и под ним детали с
количеством на одну сборку. Строки добавляются в конец листа, существующие строки не трогаются; повторный запуск
заменяет прежние строки с деталями.
"""
import io
import logging

import openpyxl

from src import deps_parts as dp

log = logging.getLogger('component_deps')

DETAIL_SHEETS = ('ПЛАЗМА', 'ПИЛА')          # откуда сборка берёт детали
WAIT_SHEET = 'СБОРКА'


def _pw():
    from src import project_wizard as pw      # поздний импорт: избегаем циклов
    return pw


def parse_files(files):
    """files — список {'id','mime','name'}. → (asm_qty, need_total) по всем файлам вместе."""
    pw = _pw()
    asm_all, need_all = {}, {}
    for xf in files:
        raw = pw._download_excel(xf['id'], xf.get('mime', ''))
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = list(wb.active.iter_rows(values_only=True))
        wb.close()
        asm, need = dp.parse_assembly_components(rows)
        for a, q in asm.items():
            asm_all[a] = asm_all.get(a, 0.0) + q
        for a, parts in need.items():
            d = need_all.setdefault(a, {})
            for p, v in parts.items():
                d[p] = d.get(p, 0.0) + v
    return asm_all, need_all


def _col(header, name):
    return next((i for i, h in enumerate(header) if str(h).strip() == name), None)


def read_sheet_data(ss):
    """→ (elements {lower: имя в листе СБОРКА}, pos_spec {позиция_lower: лист}, pos_total {(лист, позиция_lower): штук},
    standalone {(лист, позиция_lower)}: «готовый элемент» — деталь нужна не только в сборках)."""
    elements, pos_spec, pos_total, standalone = {}, {}, {}, set()
    try:
        v = ss.worksheet(WAIT_SHEET).get_all_values()
        ie = _col(v[1], 'ЭЛЕМЕНТ') if len(v) > 1 else None
        if ie is not None:
            for r in v[2:]:
                if ie < len(r) and r[ie].strip():
                    elements.setdefault(r[ie].strip().lower(), r[ie].strip())
    except Exception as e:
        log.warning(f'не удалось прочитать {WAIT_SHEET}: {e}')
    for spec in DETAIL_SHEETS:
        try:
            v = ss.worksheet(spec).get_all_values()
        except Exception:
            continue
        if len(v) < 2:
            continue
        ip, iq, ie = _col(v[1], 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА'), _col(v[1], 'КОЛ-ВО'), _col(v[1], 'ЭЛЕМЕНТ')
        if ip is None or iq is None:
            continue
        for r in v[2:]:
            if ip < len(r) and r[ip].strip() and iq < len(r):
                p = r[ip].strip().lower()
                q = dp._num(r[iq]) or 0.0
                pos_spec.setdefault(p, spec)
                pos_total[(spec, p)] = pos_total.get((spec, p), 0.0) + q
                if ie is not None and ie < len(r) and 'готовый' in r[ie].strip().lower():
                    standalone.add((spec, p))
    return elements, pos_spec, pos_total, standalone


def build_rows(asm_qty, need_total, elements, pos_spec, pos_total, standalone=frozenset()):
    """→ (rows для листа, отчёт). Строка: [СБОРКА, сборка, лист детали, сборка(метка), деталь, штук на 1 сборку]."""
    per = dp.per_assembly(asm_qty, need_total)
    rows, unresolved, not_in_sheet = [], [], []
    for a, parts in per.items():
        a_sheet = elements.get(a.lower())
        if not a_sheet:
            not_in_sheet.append(a)
            continue
        for p, q in sorted(parts.items()):
            spec = pos_spec.get(p.lower())
            if not spec:
                unresolved.append((a, p))
                continue
            rows.append([WAIT_SHEET, a_sheet, spec, a_sheet, p, round(q, 4)])
    # сверка: сколько деталей нужно всем сборкам по файлу и сколько заложено в листах деталей
    need_by_pos = {}
    for a, parts in need_total.items():
        for p, v in parts.items():
            need_by_pos[p.lower()] = need_by_pos.get(p.lower(), 0.0) + v
    mismatches = []
    for p, need in sorted(need_by_pos.items()):
        spec = pos_spec.get(p)
        if not spec:
            continue
        have = pos_total.get((spec, p), 0.0)
        if have < need - 0.01:
            mismatches.append((spec, p, have, need, 'нехватка'))          # в листе деталей меньше, чем нужно сборкам
        elif have > need + 0.01 and (spec, p) not in standalone:
            mismatches.append((spec, p, have, need, 'избыток'))           # больше, и это не «готовый элемент»
    report = {'assemblies': len(per), 'rows': len(rows), 'unresolved': unresolved, 'not_in_sheet': not_in_sheet,
              'mismatches': mismatches}
    return rows, report


def fill_component_deps(sheet_id, files, dry_run=False):
    """Пишет строки с деталями в лист ЗАВИСИМОСТИ. → отчёт (dict)."""
    pw = _pw()
    asm_qty, need_total = parse_files(files)
    gc = pw._gc(pw.SCOPES_RW)
    ss = pw._w(gc.open_by_key, sheet_id)
    elements, pos_spec, pos_total, standalone = read_sheet_data(ss)
    rows, report = build_rows(asm_qty, need_total, elements, pos_spec, pos_total, standalone)
    report['written'] = 0
    if dry_run or not rows:
        return report
    ws = ss.worksheet('ЗАВИСИМОСТИ')
    vals = pw._w(ws.get_all_values)
    # прежние строки с деталями (колонка F заполнена) очищаем, чтобы повторный запуск не плодил дубли
    old = [i for i, r in enumerate(vals[1:], start=2) if len(r) > 5 and r[5].strip()]
    if old:
        pw._w(ws.batch_clear, [f'B{i}:G{i}' for i in old])
    next_row = max(len(vals), 1) + 1
    need_rows = next_row + len(rows) - 1
    if ws.row_count < need_rows:
        pw._w(ws.add_rows, need_rows - ws.row_count + 20)
    pw._w(ws.update, [list(dp.PART_COLS)], range_name='F1', value_input_option='RAW')
    pw._w(ws.update, rows, range_name=f'B{next_row}', value_input_option='RAW')
    report['written'] = len(rows)
    log.info(f'component_deps: записано {len(rows)} строк с деталями, сборок {report["assemblies"]}')
    return report


def assembly_files_from_source_map(source_map):
    """Файлы листа СБОРКА из source_map мастера проектов (только активные записи)."""
    out, seen = [], set()
    for item in source_map or []:
        if item.get('spec') != WAIT_SHEET:
            continue
        for e in item.get('entries', []):
            xf = e.get('file')
            if xf and e.get('active', True) and xf['id'] not in seen:
                seen.add(xf['id'])
                out.append(xf)
    return out


def find_assembly_files(folder_id):
    """Файлы сборок в папке проекта (для проектов, созданных до появления колонок): имя содержит «сборочн»."""
    pw = _pw()
    scan = pw.scan_drive_folder(folder_id)
    found, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            if 'id' in o and 'name' in o and str(o['name']).lower().endswith('.xlsx') and 'сборочн' in str(o['name']).lower():
                if o['id'] not in seen:
                    seen.add(o['id'])
                    found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(scan)
    return found


if __name__ == '__main__':
    # python -m src.component_deps <project_id> [--dry-run]: заполнить колонки F/G листа ЗАВИСИМОСТИ для созданного ранее проекта
    import sys
    sys.path.insert(0, '/root/naryady/prod')
    from src import db
    pid = int(sys.argv[1])
    pr = db.fetchone('SELECT project_name, sheet_id, folder_id FROM projects WHERE id=%s', [pid])
    fl = find_assembly_files(pr['folder_id'])
    print('файлы сборок:', [f['name'] for f in fl])
    r = fill_component_deps(pr['sheet_id'], fl, dry_run='--dry-run' in sys.argv)
    print({k: (len(v) if isinstance(v, list) else v) for k, v in r.items()})
    for m in r['mismatches']:
        print('  расхождение:', m)
