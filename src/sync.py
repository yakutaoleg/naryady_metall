import sys, os, time, uuid, traceback as _tb
from datetime import datetime as _dt, timezone

_col_std_last: dict = {}
COL_STD_INTERVAL = 3600
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def _to_float(val):
    try: return float(str(val).replace(",", ".").strip()) or None
    except: return None

from src import sheets, db, logger, config

def _parse_date(s):
    if not s: return None
    s = str(s).strip()
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        try:
            y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
            if 1 <= m <= 12 and 1 <= d <= 31:
                return s
        except Exception:
            pass
        return None
    parts = s.split('.')
    if len(parts) == 3:
        try:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            if 1 <= m <= 12 and 1 <= d <= 31 and y >= 2000:
                return f'{y:04d}-{m:02d}-{d:02d}'
        except Exception:
            pass
        return None
    return None

def _get(row: dict, *keys) -> str:
    for k in keys:
        v = row.get(k, '')
        if v:
            return str(v).strip()
    return ''

def _do_auto_split(file_id: str, sheet_name: str, project_name: str, project_id: int,
                   row: dict, row_num: int, qty_done: int, remaining: int):
    """Разделяет строку с ЧАСТИЧНО+ВЫПОЛНЕНО вручную на ВЫПОЛНЕНО + ПЛАН(остаток)."""
    row_id = row.get('ROW_ID', '').strip() or None

    kol_str = _get(row, 'КОЛ-ВО', 'Кол-во')
    qty_total = int(float(kol_str or 1))
    ps_total = _to_float(row.get('СУММА К ОПЛАТЕ', '') or 0) or 0.0
    if qty_total > 0 and ps_total:
        ps_done      = round(ps_total / qty_total * qty_done,    4)
        ps_remaining = round(ps_total / qty_total * remaining,   4)
    else:
        ps_done = ps_remaining = None

    new_row_id = str(uuid.uuid4())
    today = __import__('datetime').date.today().isoformat()

    if row_id:
        orig = db.fetchone(
            "SELECT id FROM work_orders WHERE file_id=%s AND row_id=%s", [file_id, row_id]
        )
    else:
        orig = db.fetchone(
            "SELECT id FROM work_orders WHERE file_id=%s AND sheet_name=%s AND row_num=%s",
            [file_id, sheet_name, row_num]
        )
    if not orig:
        logger.error(f'_do_auto_split: не нашли запись {sheet_name} row_num={row_num}')
        return
    orig_id = orig['id']

    db.execute(
        "UPDATE work_orders SET status='ВЫПОЛНЕНО', qty_done=%s, quantity=%s, date_fact=CURRENT_DATE WHERE id=%s",
        [qty_done, qty_done, orig_id]
    )

    db.execute(
        "INSERT INTO work_orders "
        "  (project_id, project_name, file_id, sheet_name, row_num, row_id, "
        "   position, element, quantity, unit_weight, payment_sum, "
        "   date_plan, mandatory, status) "
        "SELECT project_id, project_name, file_id, sheet_name, -%s, %s, "
        "  position, element, %s, unit_weight, %s, "
        "  date_plan, false, 'ПЛАН' "
        "FROM work_orders WHERE id=%s",
        [orig_id, new_row_id, remaining, ps_remaining, orig_id]
    )

    uw_str = _get(row, 'МАССА ЕД. (кг)', 'Покраска\nза (м²)', 'Грунтовка\nза (м²)')
    remainder_data = {
        'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': _get(row, 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка'),
        'ЭЛЕМЕНТ':               _get(row, 'ЭЛЕМЕНТ', 'Поверхность\nЭлемент (м²)'),
        'КОЛ-ВО':               remaining,
        'МАССА ЕД. (кг)':       uw_str,
        'МАССА ВСЕХ (кг)':      '',
        'СУММА К ОПЛАТЕ':       ps_remaining if ps_remaining is not None else '',
        'СТАТУС':                'ПЛАН',
        'ОБЯЗАТЕЛЬНАЯ':          'НЕТ',
        'ИСПОЛНИТЕЛЬ':           '',
        'ROW_ID':                new_row_id,
        'ССЫЛКА НА ЧЕРТЁЖ':     row.get('ССЫЛКА НА ЧЕРТЁЖ', '') or '',
    }
    sheets.insert_remainder_row(file_id, sheet_name, row_num, remainder_data)

    sheets.update_task_status(
        file_id=file_id, sheet_name=sheet_name, row_num=row_num,
        status='ВЫПОЛНЕНО', date_fact=today, qty_done=qty_done,
    )
    sheets.update_cell_by_header(file_id, sheet_name, row_num, 'КОЛ-ВО', qty_done)
    if ps_done is not None:
        sheets.update_cell_by_header(file_id, sheet_name, row_num, 'СУММА К ОПЛАТЕ', ps_done)

    logger.info(f'  {sheet_name}: авто-сплит строка {row_num} → '
                f'ВЫПОЛНЕНО({qty_done}) + ПЛАН(остаток {remaining})')
    logger.audit(
        action='auto_split',
        details={
            'project':   project_name,
            'sheet':     sheet_name,
            'position':  _get(row, 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка'),
            'element':   _get(row, 'ЭЛЕМЕНТ', 'Поверхность\nЭлемент (м²)'),
            'row_num':   row_num,
            'orig_id':   orig_id,
            'qty_done':  qty_done,
            'remaining': remaining,
        }
    )


def _normalize_row(row: dict, sheet_name: str, row_num: int,
                   project_name: str, file_id: str, project_id: int = None) -> dict:
    mandatory = row.get('ОБЯЗАТЕЛЬНАЯ', '').strip()
    return {
        'project_id':   project_id,
        'project_name': project_name,
        'file_id':      file_id,
        'sheet_name':   sheet_name,
        'row_num':      row_num,
        'row_id':       row.get('ROW_ID', '').strip() or None,
        'position':     _get(row, 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка') or None,
        'element':      _get(row, 'ЭЛЕМЕНТ', 'Поверхность\nЭлемент (м²)') or None,
        'quantity':     _to_float(_get(row, 'КОЛ-ВО', 'Кол-во') or 0) or None,
        'unit_weight':  _to_float(_get(row, 'МАССА ЕД. (кг)', 'Покраска\nза (м²)', 'Грунтовка\nза (м²)') or 0) or None,
        'total_weight': _to_float(row.get('МАССА ВСЕХ (кг)', '') or 0) or None,
        'payment_sum':  _to_float(row.get('СУММА К ОПЛАТЕ', '') or 0) or None,
        'executor':     row.get('ИСПОЛНИТЕЛЬ', '').strip() or None,
        'date_plan':    _parse_date(row.get('ДАТА ПЛАН')),
        'priority':     int(row.get('ПРИОРИТЕТ', '') or 0) or None,
        'mandatory':    True if mandatory == 'ДА' else (False if mandatory == 'НЕТ' else None),
        'status':       row.get('СТАТУС', 'ПЛАН').strip() or 'ПЛАН',
        'comment':      row.get('КОММЕНТАРИЙ', '').strip() or None,
        'date_fact':    _parse_date(row.get('ДАТА ФАКТ')),
        'drawing_link': row.get('ССЫЛКА НА ЧЕРТЁЖ', '').strip() or None,
        'qty_holes':    int(_to_float(row.get('КОЛ-ВО ОТВЕРСТИЙ', '') or 0) or 0) or None,
        'qty_done':     _to_float(row.get('ВЫПОЛНЕНО', '') or 0) or None,
    }

UPSERT_SQL = '''
INSERT INTO work_orders (
    project_id, project_name, file_id, sheet_name, row_num, row_id,
    position, element, quantity, unit_weight, total_weight, payment_sum,
    executor, date_plan, priority, mandatory, status, comment, date_fact,
    drawing_link, qty_holes, qty_done, updated_at
) VALUES (
    %(project_id)s, %(project_name)s, %(file_id)s, %(sheet_name)s, %(row_num)s, %(row_id)s,
    %(position)s, %(element)s, %(quantity)s, %(unit_weight)s, %(total_weight)s, %(payment_sum)s,
    %(executor)s, %(date_plan)s, %(priority)s, %(mandatory)s, %(status)s, %(comment)s, %(date_fact)s,
    %(drawing_link)s, %(qty_holes)s, %(qty_done)s, NOW()
)
ON CONFLICT (project_name, sheet_name, row_num) DO UPDATE SET
    project_id   = EXCLUDED.project_id,
    row_id       = COALESCE(EXCLUDED.row_id, work_orders.row_id),
    position     = EXCLUDED.position,
    element      = EXCLUDED.element,
    quantity     = EXCLUDED.quantity,
    unit_weight  = EXCLUDED.unit_weight,
    total_weight = EXCLUDED.total_weight,
    payment_sum  = EXCLUDED.payment_sum,
    executor     = EXCLUDED.executor,
    date_plan    = EXCLUDED.date_plan,
    priority     = EXCLUDED.priority,
    mandatory    = CASE WHEN EXCLUDED.status = 'ЧАСТИЧНО' THEN true
                        ELSE EXCLUDED.mandatory END,
    drawing_link = EXCLUDED.drawing_link,
    updated_at   = NOW(),
    status    = CASE
                    WHEN work_orders.status = 'ВЫПОЛНЕНО' AND EXCLUDED.status = 'ПЛАН'
                         AND EXISTS (
                             SELECT 1 FROM work_orders r2
                             WHERE r2.file_id   = work_orders.file_id
                               AND r2.sheet_name = work_orders.sheet_name
                               AND r2.position   = work_orders.position
                               AND r2.status     = 'ПЛАН'
                               AND r2.id        != work_orders.id
                         )
                    THEN 'ВЫПОЛНЕНО'
                    ELSE EXCLUDED.status
                END,
    qty_done  = CASE WHEN EXCLUDED.status = 'ПЛАН' THEN NULL ELSE work_orders.qty_done END,
    comment   = EXCLUDED.comment,
    date_fact = EXCLUDED.date_fact,
    qty_holes = EXCLUDED.qty_holes
'''

def _apply_status_format_batch(file_id: str, sheet_name: str, rows: list):
    cm = sheets._col_map(file_id, sheet_name, rw=True)
    col_idx = cm.get('СТАТУС')
    if not col_idx:
        return
    ws = sheets._ws(file_id, sheet_name, rw=True)
    col_0 = col_idx - 1

    requests = []
    for i, row in enumerate(rows):
        status = row.get('СТАТУС', '').strip()
        style = sheets.STATUS_COLORS.get(status)
        if not style:
            continue
        sheet_row = i + 3
        requests.append({
            'repeatCell': {
                'range': {
                    'sheetId': ws.id,
                    'startRowIndex': sheet_row - 1,
                    'endRowIndex':   sheet_row,
                    'startColumnIndex': col_0,
                    'endColumnIndex':   col_0 + 1,
                },
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': style['bg'],
                        'textFormat': {'bold': style['bold']},
                        'horizontalAlignment': 'CENTER',
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)',
            }
        })
    if not requests:
        return
    svc = sheets._service()
    for i in range(0, len(requests), 200):
        svc.spreadsheets().batchUpdate(
            spreadsheetId=file_id,
            body={'requests': requests[i:i+200]}
        ).execute()


def _write_row_id_to_sheet(file_id: str, sheet_name: str, row_num: int, row_id: str):
    try:
        sheets.write_row_id(file_id, sheet_name, row_num, row_id)
    except Exception as e:
        logger.error(f'Не удалось записать ROW_ID в таблицу {sheet_name} row {row_num}: {e}')

def _compute_block(project_name: str, sheet_name: str, position: str, element: str,
                   own_qty: int = None) -> str:
    """Вычисляет значение колонки БЛОК.
    own_qty — кол-во задачи в текущем листе.
    Для каждой upstream-позиции считаем сколько сборок можно сделать:
      avail = floor(done_i * own_qty / total_i)
    Итоговый доступный объём = min по всем позициям.
    """
    lookup = (position or element or '').strip()
    if not lookup:
        return ''
    deps = db.fetchall(
        """SELECT requires_sheet, requires_position
           FROM element_dependencies
           WHERE project_name=%s AND waiting_sheet=%s AND element=%s""",
        [project_name, sheet_name, lookup]
    )
    if not deps:
        return ''
    blocked = []
    seen_sheets = set()
    for d in deps:
        if d['requires_sheet'] in seen_sheets:
            continue
        seen_sheets.add(d['requires_sheet'])
        rp = d['requires_position']
        # Все строки upstream для этого элемента/позиции
        rows = db.fetchall(
            """SELECT position, status, COALESCE(quantity, 0) as qty FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND (element=%s OR position=%s)""",
            [project_name, d['requires_sheet'], rp, rp]
        )
        if not rows:
            continue
        # Группируем по позиции и считаем min доступных сборок
        pos_totals = {}
        pos_done   = {}
        for r in rows:
            p = r['position'] or rp
            pos_totals[p] = pos_totals.get(p, 0) + int(r['qty'])
            if r['status'] == 'ВЫПОЛНЕНО':
                pos_done[p] = pos_done.get(p, 0) + int(r['qty'])
        total_all = sum(pos_totals.values())
        done_all  = sum(pos_done.values())
        if total_all == 0 or done_all >= total_all:
            # Всё готово — не блокирует
            continue
        if own_qty and own_qty > 0:
            # Считаем общее количество элемента по всем строкам текущего листа
            total_element_row = db.fetchone(
                """SELECT COALESCE(SUM(quantity), 0) as total FROM work_orders
                   WHERE project_name=%s AND sheet_name=%s
                     AND (position=%s OR element=%s)""",
                [project_name, sheet_name, lookup, lookup]
            )
            total_elem = int(total_element_row['total'] or own_qty)
            # Уже выполненные в текущем листе потребили часть upstream-материала
            done_vyp_row = db.fetchone(
                """SELECT COALESCE(SUM(quantity), 0) as done FROM work_orders
                   WHERE project_name=%s AND sheet_name=%s
                     AND (position=%s OR element=%s) AND status='ВЫПОЛНЕНО'""",
                [project_name, sheet_name, lookup, lookup]
            )
            done_vypolneno = int(done_vyp_row['done'] or 0)
            # NET — для решения о блокировке; RAW — для отображения мастеру
            min_avail_net = total_elem
            min_avail_raw = total_elem
            for p, total_i in pos_totals.items():
                if total_i > 0:
                    done_i = pos_done.get(p, 0)
                    avail_raw = int(done_i * total_elem / total_i)
                    avail_net = avail_raw - done_vypolneno
                    if avail_net < min_avail_net:
                        min_avail_net = avail_net
                    if avail_raw < min_avail_raw:
                        min_avail_raw = avail_raw
            if min_avail_net >= own_qty:
                continue  # upstream покрывает все оставшиеся — не блокирует
            blocked.append((d['requires_sheet'], min_avail_raw, total_elem))
        else:
            blocked.append((d['requires_sheet'], done_all, total_all))
    if not blocked:
        return '✅'
    parts = []
    for sheet, avail, total in blocked:
        if avail > 0:
            parts.append(f'✅ {avail} из {total}\n⛔ {sheet}')
        else:
            parts.append(f'⛔ {sheet}')
    return '\n'.join(parts)


def _update_block_column(file_id: str, sheet_name: str, project_name: str, rows: list[dict] = None):
    cm = sheets._col_map(file_id, sheet_name, rw=True)
    block_col = cm.get('БЛОК')
    row_id_col = cm.get('ROW_ID')
    if not block_col:
        return
    ws = sheets._ws(file_id, sheet_name, rw=True)

    # Строим map ROW_ID → реальный номер строки через чтение листа
    row_id_to_sheet_row = {}
    if row_id_col:
        try:
            all_vals = sheets._api_call(ws.get_all_values)
            # row 1 = заголовок, row 2 = подзаголовок, данные с row 3 (idx=2)
            for idx, row_data in enumerate(all_vals[2:], start=3):
                cell_idx = row_id_col - 1
                if cell_idx < len(row_data):
                    rid = row_data[cell_idx].strip()
                    if rid:
                        row_id_to_sheet_row[rid] = idx
        except Exception as e:
            logger.warning(f'_update_block_column: не удалось прочитать ROW_ID {sheet_name}: {e}')

    work_rows = db.fetchall(
        """SELECT row_num, row_id, position, element, status,
                  COALESCE(quantity, 0) as qty
           FROM work_orders
           WHERE project_name=%s AND sheet_name=%s AND row_num > 0""",
        [project_name, sheet_name]
    )
    # Сначала считаем БЛОК для ПЛАН-строк, чтобы ВЫПОЛНЕНО могли взять то же значение
    pos_block_val = {}
    for r in work_rows:
        if r['status'] != 'ВЫПОЛНЕНО':
            key = (r['position'] or r['element'] or '').strip()
            if key and key not in pos_block_val:
                pos_block_val[key] = _compute_block(
                    project_name, sheet_name, r['position'], r['element'],
                    own_qty=int(r['qty']))

    updates = []
    for r in work_rows:
        rid = (r.get('row_id') or '').strip()
        if row_id_col:
            # Если ROW_ID-колонка есть — используем только map; строки без id в листе пропускаем
            sheet_row = row_id_to_sheet_row.get(rid)
            if not sheet_row:
                continue
        else:
            sheet_row = r['row_num'] + 2

        key = (r['position'] or r['element'] or '').strip()
        if r['status'] == 'ВЫПОЛНЕНО':
            # Дублируем БЛОК от одноимённой ПЛАН-строки (если есть)
            val = pos_block_val.get(key, '')
        else:
            val = pos_block_val.get(key, '')
        updates.append({
            'range': f'{sheets._col_letter(block_col)}{sheet_row}',
            'values': [[val]]
        })
    for i in range(0, len(updates), 100):
        sheets._api_call(ws.batch_update, updates[i:i+100])


UPSERT_EMPLOYEE_SQL = '''
INSERT INTO employees (full_name, specialization, role, is_active)
VALUES (%s, ARRAY[%s], 'worker', true)
ON CONFLICT (full_name) DO UPDATE
SET specialization = CASE
    WHEN %s = ANY(employees.specialization) THEN employees.specialization
    ELSE array_append(employees.specialization, %s)
END
'''

def _upsert_employees(rows: list[dict], sheet_name: str):
    if sheet_name not in config.WORK_SHEETS:
        return
    seen = set()
    for row in rows:
        name = row.get('ИСПОЛНИТЕЛЬ', '').strip()
        if name and name not in seen:
            seen.add(name)
            db.execute(UPSERT_EMPLOYEE_SQL, [name, sheet_name, sheet_name, sheet_name])


def _rebuild_element_dependencies(project_name: str, file_id: str):
    """Синхронизирует element_dependencies из листа ЗАВИСИМОСТИ (если есть), иначе авто-генерирует."""

    try:
        ws_z = sheets._ws(file_id, 'ЗАВИСИМОСТИ')
        z_data = sheets._api_call(ws_z.get_all_values)
        if len(z_data) > 1:
            deps_from_sheet = []
            for row in z_data[1:]:
                if len(row) >= 5 and row[1].strip() and row[2].strip() and row[3].strip() and row[4].strip():
                    deps_from_sheet.append((row[1].strip(), row[2].strip(), row[3].strip(), row[4].strip()))
            if deps_from_sheet:
                with db.transaction() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM element_dependencies WHERE project_name=%s", [project_name])
                        for w, e, r, p in deps_from_sheet:
                            cur.execute(
                                "INSERT INTO element_dependencies "
                                "(project_name, waiting_sheet, element, requires_sheet, requires_position) "
                                "VALUES (%s,%s,%s,%s,%s)",
                                [project_name, w, e, r, p]
                            )
                from collections import Counter
                pair_counts = Counter(f'{r}->{w}' for w, e, r, p in deps_from_sheet)
                summary = ', '.join(f'{k}:{v}' for k, v in sorted(pair_counts.items()))
                logger.info(f'  Зависимости ({project_name}): {len(deps_from_sheet)} записей [{summary}]')
                return
    except Exception as _ze:
        if 'WorksheetNotFound' not in str(type(_ze)) and 'not found' not in str(_ze).lower():
            logger.warning(f'  ЗАВИСИМОСТИ sheet error ({project_name}): {_ze}')

    def get_elems(sheet_name, col='ЭЛЕМЕНТ'):
        try:
            ws   = sheets._ws(file_id, sheet_name)
            data = sheets._api_call(ws.get_all_values)
            if len(data) < 2:
                return set()
            hdrs = data[1]
            idx  = next((i for i, h in enumerate(hdrs) if h.strip() == col), None)
            if idx is None and col != "ЭЛЕМЕНТ":
                idx = next((i for i, h in enumerate(hdrs) if h.strip() == "ЭЛЕМЕНТ"), None)
            if idx is None:
                return set()
            return {r[idx].strip() for r in data[2:]
                    if len(r) > idx and r[idx].strip()
                    and r[idx].strip() not in ('ИТОГО', '')}
        except Exception as e:
            logger.error(f'  _rebuild: не удалось прочитать {sheet_name}/{col}: {e}')
            return set()

    pila      = get_elems('ПИЛА')
    pila_pos  = get_elems('ПИЛА', 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА')  # все позиции ПИЛА включая простые профили
    plazma    = get_elems('ПЛАЗМА')
    sverlenie = get_elems('СВЕРЛЕНИЕ', 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА')
    sborka    = get_elems('СБОРКА')
    svarka    = get_elems('СВАРКА',    'ПОЗ. СОГЛАСНО ЧЕРТЕЖА')
    grunt     = get_elems('ГРУНТОВКА', 'Марка')
    pokraska  = get_elems('ПОКРАСКА',  'Марка')

    deps = []
    plazma_upper = {e.upper(): e for e in plazma}

    # СВЕРЛЕНИЕ <- ПИЛА (прямое) + ПЛАЗМА (last-word)
    for e in sverlenie & pila:
        deps.append(('СВЕРЛЕНИЕ', e, 'ПИЛА', e))
    for se in sverlenie:
        pm = plazma_upper.get(se.split()[-1].upper())
        if pm:
            deps.append(('СВЕРЛЕНИЕ', se, 'ПЛАЗМА', pm))

    # СБОРКА <- ПИЛА (прямое) + ПЛАЗМА (last-word), НЕ зависит от СВЕРЛЕНИЯ
    for e in sborka & pila:
        deps.append(('СБОРКА', e, 'ПИЛА', e))
    for se in sborka:
        pm = plazma_upper.get(se.split()[-1].upper())
        if pm:
            deps.append(('СБОРКА', se, 'ПЛАЗМА', pm))

    # СВАРКА <- СБОРКА + СВЕРЛЕНИЕ (оба должны быть выполнены)
    for e in svarka & sborka:
        deps.append(('СВАРКА', e, 'СБОРКА', e))
    for e in svarka & sverlenie:
        deps.append(('СВАРКА', e, 'СВЕРЛЕНИЕ', e))

    # ГРУНТОВКА <- СВАРКА, ПОКРАСКА <- ГРУНТОВКА
    for e in grunt & svarka:
        deps.append(('ГРУНТОВКА', e, 'СВАРКА', e))
    # ГРУНТОВКА <- ПИЛА (простые профили: есть в ГРУНТОВКА и ПИЛА.ПОЗ, нет в СВАРКА)
    for e in grunt - svarka:
        if e in pila_pos:
            deps.append(('ГРУНТОВКА', e, 'ПИЛА', e))
    for e in pokraska & grunt:
        deps.append(('ПОКРАСКА', e, 'ГРУНТОВКА', e))

    pair_counts = {}
    for w, e, r, p in deps:
        key = f'{r}->{w}'
        pair_counts[key] = pair_counts.get(key, 0) + 1

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM element_dependencies WHERE project_name=%s",
                [project_name]
            )
            for w, e, r, p in deps:
                cur.execute(
                    """INSERT INTO element_dependencies
                       (project_name, waiting_sheet, element, requires_sheet, requires_position)
                       VALUES (%s, %s, %s, %s, %s)""",
                    [project_name, w, e, r, p]
                )

    summary = ', '.join(f'{k}:{v}' for k, v in sorted(pair_counts.items()))
    logger.info(f'  Зависимости ({project_name}): {len(deps)} записей [{summary}]')
    if len(deps) == 0:
        logger.error(f'  Зависимости ({project_name}): ВНИМАНИЕ — 0 записей, возможен сбой матчинга!')


def _reconcile_blocks(project_name: str):
    """Снимает БЛОК с задач, чьи зависимости уже выполнены."""
    blocked = db.fetchall(
        """SELECT wo.id, wo.element, wo.position, wo.sheet_name, wo.file_id, wo.row_num
           FROM work_orders wo
           WHERE wo.project_name=%s AND wo.status='БЛОК'
             AND (wo.element IS NOT NULL OR wo.position IS NOT NULL)
             AND EXISTS (
               SELECT 1 FROM element_dependencies ed
               WHERE ed.project_name=wo.project_name
                 AND (ed.element=wo.element OR ed.element=wo.position)
             )""",
        [project_name]
    )
    unblocked = 0
    for task in blocked:
        lookup = (task['element'] or task['position'] or '').strip()
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position FROM element_dependencies
               WHERE project_name=%s AND waiting_sheet=%s AND element=%s""",
            [project_name, task['sheet_name'], lookup]
        )
        all_done = True
        for dep in deps:
            pending = db.fetchone(
                """SELECT COALESCE(SUM(quantity), 0) as qty_pending FROM work_orders
                   WHERE project_name=%s AND sheet_name=%s
                     AND (element=%s OR position=%s)
                     AND status != 'ВЫПОЛНЕНО'""",
                [project_name, dep['requires_sheet'], dep['requires_position'], dep['requires_position']]
            )
            if pending and int(pending['qty_pending'] or 0) > 0:
                all_done = False
                break
        if all_done:
            db.execute(
                "UPDATE work_orders SET status='ПЛАН', comment=NULL WHERE id=%s AND status='БЛОК'",
                [task['id']]
            )
            try:
                sheets.update_task_status(
                    task['file_id'], task['sheet_name'], task['row_num'], 'ПЛАН', comment=''
                )
            except Exception as e:
                logger.error(f'  Reconcile sheets update error: {e}')
            unblocked += 1
    if unblocked:
        logger.info(f'  Reconcile ({project_name}): разблокировано {unblocked} задач')


def run(progress_cb=None):
    """
    progress_cb(phase, data) — опциональный коллбек для отображения прогресса в боте.
    phase: 'project_start' | 'sheet_done' | 'project_done' | 'deps_done' | 'sync_done'
    """
    logger.info('Sync started')
    total_synced = 0

    projects = db.fetchall(
        "SELECT id, project_name, sheet_id FROM projects WHERE status='АКТИВНЫЙ'",
        []
    )
    files = [{'id': p['id'], 'file_id': p['sheet_id'], 'project_name': p['project_name']} for p in projects]

    logger.info(f'Found {len(files)} active project(s) in DB')
    total_sheets = len(files) * len(config.WORK_SHEETS)
    sheets_done  = 0

    for f in files:
        file_id      = f['file_id']
        project_name = f['project_name']
        logger.info(f'Processing: {project_name} ({file_id})')

        if progress_cb:
            progress_cb('project_start', {'project': project_name})

        sheet_errors = 0

        # B: Читаем все листы проекта одним API-вызовом
        try:
            _batch_data = sheets.read_all_sheets_batch(file_id, config.WORK_SHEETS)
            logger.info(f'  batchGet: загружено {len(_batch_data)} листов')
        except Exception as _be:
            logger.warning(f'  batchGet failed, fallback to per-sheet: {_be}')
            _batch_data = {}

        for sheet_name in config.WORK_SHEETS:
            try:
                rows = sheets.read_sheet(file_id, sheet_name,
                                         preloaded=_batch_data.get(sheet_name))
                _upsert_employees(rows, sheet_name)
                pending_row_ids = []
                seen_row_ids = []
                synced = 0
                for i, row in enumerate(rows):
                    r = _normalize_row(row, sheet_name, i+1, project_name, file_id, f['id'])
                    if not r['row_id']:
                        new_id = str(uuid.uuid4())
                        r['row_id'] = new_id
                        pending_row_ids.append((i+1, new_id))
                    seen_row_ids.append(r['row_id'])
                    db.execute(UPSERT_SQL, r)
                    synced += 1
                total_synced += synced

                if pending_row_ids:
                    sheets.write_row_ids(file_id, sheet_name, pending_row_ids)
                    logger.info(f'  {sheet_name}: записано {len(pending_row_ids)} новых ROW_ID')

                # Призраки: строки в БД которых больше нет в листе
                # Защита: пропускаем если лист прочитан пустым
                if seen_row_ids:
                    ghost_del = db.fetchone(
                        """
                        WITH del AS (
                            DELETE FROM work_orders
                            WHERE project_name = %s AND sheet_name = %s
                              AND row_id IS NOT NULL
                              AND row_id::text <> ALL(%s)
                            RETURNING 1
                        ) SELECT COUNT(*) AS cnt FROM del
                        """,
                        [project_name, sheet_name, [str(r) for r in seen_row_ids]]
                    )
                    if ghost_del and ghost_del['cnt']:
                        logger.info(f'  {sheet_name}: удалено {ghost_del["cnt"]} призраков')

                try:
                    _apply_status_format_batch(file_id, sheet_name, rows)
                except Exception as _fmt_err:
                    logger.warning(f'  {sheet_name}: статус-форматирование пропущено: {_fmt_err}')

                _std_key = (file_id, sheet_name)
                _now = _dt.now(timezone.utc).timestamp()
                if _now - _col_std_last.get(_std_key, 0) > COL_STD_INTERVAL:
                    try:
                        sheets.apply_column_standards(file_id, sheet_name)
                        _col_std_last[_std_key] = _now
                    except Exception as _std_err:
                        logger.warning(f'  {sheet_name}: apply_column_standards пропущено: {_std_err}')

                # Авто-сплит ЧАСТИЧНО строк где ВЫПОЛНЕНО заполнено вручную
                splits_needed = []
                for i, row in enumerate(rows):
                    status_s = row.get('СТАТУС', '').strip()
                    done_str = row.get('ВЫПОЛНЕНО', '').strip()
                    if not done_str:
                        continue
                    if status_s in ('ВЫПОЛНЕНО', 'БЛОК', ''):
                        continue
                    kol_str = _get(row, 'КОЛ-ВО', 'Кол-во')
                    try:
                        qty_total_s = int(float(kol_str or 0))
                        qty_done_s  = int(float(done_str))
                    except (ValueError, TypeError):
                        continue
                    remaining_s = qty_total_s - qty_done_s
                    if qty_done_s <= 0 or remaining_s <= 0:
                        continue
                    row_id_check = row.get('ROW_ID', '').strip()
                    if row_id_check:
                        db_rec = db.fetchone(
                            "SELECT status FROM work_orders WHERE file_id=%s AND row_id=%s",
                            [file_id, row_id_check]
                        )
                    else:
                        db_rec = db.fetchone(
                            "SELECT status FROM work_orders WHERE file_id=%s AND sheet_name=%s AND row_num=%s",
                            [file_id, sheet_name, i + 1]
                        )
                    if db_rec and db_rec['status'] == 'ВЫПОЛНЕНО':
                        logger.info(f'  {sheet_name}: row {i+1} уже split в БД, пропускаем')
                        continue
                    splits_needed.append((i + 1, row, qty_done_s, remaining_s))

                if splits_needed:
                    logger.info(f'  {sheet_name}: авто-сплит {len(splits_needed)} строк')
                    for row_num_s, row_s, qty_done_s, remaining_s in reversed(splits_needed):
                        try:
                            _do_auto_split(file_id, sheet_name, project_name, f['id'],
                                           row_s, row_num_s, qty_done_s, remaining_s)
                        except Exception as split_e:
                            logger.error(
                                f'  {sheet_name} авто-сплит row {row_num_s}: {split_e}\n'
                                f'{_tb.format_exc()}'
                            )
                            logger.alert(f'🔴 Sync авто-сплит ERROR\n{project_name} / {sheet_name} row {row_num_s}\n{split_e}')

                # Удаляем призраки
                cleaned = db.fetchone("""
                    WITH deleted AS (
                        DELETE FROM work_orders ghost
                        WHERE ghost.project_name = %s AND ghost.sheet_name = %s
                          AND ghost.row_num < 0
                          AND EXISTS (
                              SELECT 1 FROM work_orders real
                              WHERE real.row_id = ghost.row_id
                                AND real.project_name = ghost.project_name
                                AND real.sheet_name = ghost.sheet_name
                                AND real.row_num > 0
                          )
                        RETURNING 1
                    ) SELECT COUNT(*) AS cnt FROM deleted
                """, (project_name, sheet_name))
                if cleaned and cleaned['cnt']:
                    logger.info(f'  {sheet_name}: удалено {cleaned["cnt"]} призраков')

                logger.info(f'  {sheet_name}: {synced} rows')
                logger.audit(action='sync_sheet',
                             details={'project': project_name, 'sheet': sheet_name, 'rows': synced})
                _update_block_column(file_id, sheet_name, project_name, rows)

                sheets_done += 1
                if progress_cb:
                    progress_cb('sheet_done', {
                        'project':      project_name,
                        'sheet':        sheet_name,
                        'rows':         synced,
                        'sheets_done':  sheets_done,
                        'total_sheets': total_sheets,
                        'splits':       len(splits_needed),
                    })

                time.sleep(2)
            except Exception as e:
                sheets_done += 1
                if 'WorksheetNotFound' in type(e).__name__ or 'not found' in str(e).lower():
                    logger.info(f'  {sheet_name}: лист отсутствует, пропускаем')
                    if progress_cb:
                        progress_cb('sheet_done', {
                            'project': project_name, 'sheet': sheet_name,
                            'rows': 0, 'sheets_done': sheets_done,
                            'total_sheets': total_sheets, 'splits': 0,
                        })
                elif 'work_orders_project_id_fkey' in str(e):
                    logger.warning(f'  {sheet_name}: проект удалён из БД, пропускаем ({project_name})')
                else:
                    sheet_errors += 1
                    logger.error(f'  {sheet_name}: ERROR — {e}')
                    logger.alert(f'🔴 Sync ERROR\n{project_name} / {sheet_name}\n{e}')
                    logger.audit(action='sync_sheet',
                                 details={'project': project_name, 'sheet': sheet_name},
                                 result='error', error_msg=str(e))

        if f.get('id') and sheet_errors == 0:
            db.execute(
                "UPDATE projects SET last_synced_at=NOW() WHERE id=%s",
                [f['id']]
            )
        if progress_cb:
            progress_cb('project_done', {'project': project_name, 'errors': sheet_errors})

        # Пауза между проектами — даём Google Sheets API сбросить per-minute квоту
        if f != files[-1]:
            logger.info(f'  Пауза 15s перед следующим проектом...')
            time.sleep(15)

    # Автогенерация зависимостей
    for f in files:
        try:
            _rebuild_element_dependencies(f['project_name'], f['file_id'])
        except Exception as e:
            logger.error(f'  Зависимости rebuild error ({f["project_name"]}): {e}')

    # Обновляем колонку БЛОК в листах после пересборки зависимостей
    for f in files:
        for sheet_name in ['СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА']:
            try:
                _update_block_column(f['file_id'], sheet_name, f['project_name'])
            except Exception as e:
                logger.warning(f'  БЛОК update after deps ({f["project_name"]}/{sheet_name}): {e}')

    # Reconcile: снимаем БЛОК с задач, чьи зависимости выполнены
    for f in files:
        try:
            _reconcile_blocks(f['project_name'])
        except Exception as e:
            logger.error(f'  Reconcile error ({f["project_name"]}): {e}')

    if progress_cb:
        progress_cb('deps_done', {})

    db.execute(
        "UPDATE work_orders SET mandatory=true WHERE status='ЧАСТИЧНО' AND (mandatory IS NULL OR mandatory=false)",
        []
    )
    logger.info(f'Sync done. Total: {total_synced} rows')

    if progress_cb:
        progress_cb('sync_done', {'total': total_synced})

    return total_synced

if __name__ == '__main__':
    run()
