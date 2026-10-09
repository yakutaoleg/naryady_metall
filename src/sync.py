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
    sheets.insert_remainder_row(file_id, sheet_name, row_num, remainder_data, verify=False)

    sheets.update_task_status(
        file_id=file_id, sheet_name=sheet_name, row_num=row_num,
        status='ВЫПОЛНЕНО', date_fact=today, qty_done=qty_done, verify=False,
    )
    sheets.update_cell_by_header(file_id, sheet_name, row_num, 'КОЛ-ВО', qty_done, verify=False)
    if ps_done is not None:
        sheets.update_cell_by_header(file_id, sheet_name, row_num, 'СУММА К ОПЛАТЕ', ps_done, verify=False)

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
                               AND r2.element    = work_orders.element
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
        # Все строки upstream для этого элемента/позиции.
        # ЭЛЕМЕНТ может быть объединён через запятую (одна деталь — компонент
        # нескольких сборок, напр. "П4, П5") — ловим это через ILIKE-границы.
        rows = db.fetchall(
            """SELECT position, status, COALESCE(quantity, 0) as qty FROM work_orders
               WHERE project_name=%s AND sheet_name=%s
                 AND (element=%s OR position=%s
                      OR element LIKE %s OR element LIKE %s OR element LIKE %s)""",
            [project_name, d['requires_sheet'], rp, rp,
             rp + ', %', '%, ' + rp, '%, ' + rp + ', %']
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


def _update_block_column(file_id: str, sheet_name: str, project_name: str, rows: list[dict] = None,
                         raw_values: list = None):
    """Пересчитывает колонку БЛОК. Пишет в лист ТОЛЬКО ячейки, значение которых изменилось.
    raw_values — уже прочитанные значения листа (batchGet), чтобы не читать лист повторно."""
    cm = sheets._col_map(file_id, sheet_name, rw=True)
    block_col = cm.get('БЛОК')
    row_id_col = cm.get('ROW_ID')
    if not block_col:
        return
    ws = sheets._ws(file_id, sheet_name, rw=True)

    row_id_to_sheet_row = {}   # ROW_ID → номер строки листа
    current_block = {}         # номер строки листа → текущее значение БЛОК
    itogo_rows = []            # [(номер строки, текущий БЛОК)] — у ИТОГО БЛОК очищается
    all_vals = raw_values
    if all_vals is None and row_id_col:
        try:
            all_vals = sheets._api_call(ws.get_all_values)
        except Exception as e:
            logger.warning(f'_update_block_column: не удалось прочитать ROW_ID {sheet_name}: {e}')
            all_vals = None
    if all_vals is not None and row_id_col:
        bi = block_col - 1
        for idx, row_data in enumerate(all_vals[2:], start=3):
            cur_b = row_data[bi] if bi < len(row_data) else ''
            if any(c.strip() == 'ИТОГО' for c in row_data):
                itogo_rows.append((idx, cur_b))
                continue
            cell_idx = row_id_col - 1
            if cell_idx < len(row_data):
                rid = row_data[cell_idx].strip()
                if rid:
                    row_id_to_sheet_row[rid] = idx
                    current_block[idx] = cur_b

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
        val = pos_block_val.get(key, '')
        if row_id_col and current_block.get(sheet_row, None) == val:
            continue   # в листе уже то же значение — запись не нужна
        updates.append({
            'range': f'{sheets._col_letter(block_col)}{sheet_row}',
            'values': [[val]]
        })
    for i in range(0, len(updates), 100):
        sheets._api_call(ws.batch_update, updates[i:i+100])

    # Очищаем БЛОК у строк ИТОГО (только если там что-то есть)
    clear_rows = [r for r, cur_b in itogo_rows if cur_b]
    if clear_rows:
        blok_letter = sheets._col_letter(block_col)
        clear_upd = [{'range': f'{blok_letter}{r}', 'values': [['']]} for r in clear_rows]
        for i in range(0, len(clear_upd), 100):
            try:
                sheets._api_call(ws.batch_update, clear_upd[i:i+100])
            except Exception as e:
                logger.warning(f'_update_block_column: очистка ИТОГО {sheet_name}: {e}')


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
    _SERVICE_NAMES = {'ИСПОЛНИТЕЛЬ', 'ИСПОЛНИТЕЛИ', 'ФИО', 'НЕ УКАЗАН', '-', '—'}
    for row in rows:
        name = row.get('ИСПОЛНИТЕЛЬ', '').strip()
        if name.upper() in _SERVICE_NAMES:
            continue
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

    # Читаем все листы одним batch-запросом (1 API call вместо 8)
    _SHEET_RANGES = ['ПИЛА!A:Z', 'ПЛАЗМА!A:Z', 'СВЕРЛЕНИЕ!A:Z',
                     'СБОРКА!A:Z', 'СВАРКА!A:Z', 'ГРУНТОВКА!A:Z', 'ПОКРАСКА!A:Z']
    _batch_data: dict = {}
    try:
        _gc_rb = sheets._gc(sheets.SCOPES_RO)
        _ss_rb = _gc_rb.open_by_key(file_id)
        _resp = sheets._api_call(
            _ss_rb.values_batch_get,
            ranges=_SHEET_RANGES,
            params={'valueRenderOption': 'FORMATTED_VALUE', 'majorDimension': 'ROWS'},
        )
        for _vr in (_resp or {}).get('valueRanges', []):
            _title = _vr.get('range', '').split('!')[0].strip("'")
            _batch_data[_title] = _vr.get('values', [])
    except Exception as _be:
        logger.warning(f'  _rebuild batch_get failed ({project_name}): {_be}')

    def get_elems(sheet_name, col='ЭЛЕМЕНТ'):
        try:
            data = _batch_data.get(sheet_name)
            if data is None:
                # fallback: отдельный запрос если batch не вернул этот лист
                ws   = sheets._ws(file_id, sheet_name)
                data = sheets._api_call(ws.get_all_values)
            if not data or len(data) < 2:
                return set()
            hdrs = data[1]
            idx  = next((i for i, h in enumerate(hdrs) if h.strip() == col), None)
            if idx is None and col != "ЭЛЕМЕНТ":
                idx = next((i for i, h in enumerate(hdrs) if h.strip() == "ЭЛЕМЕНТ"), None)
            if idx is None:
                return set()
            result = set()
            for r in data[2:]:
                if len(r) <= idx:
                    continue
                v = r[idx].strip()
                if not v or v == 'ИТОГО':
                    continue
                # ЭЛЕМЕНТ может быть объединён через запятую (одна деталь — компонент
                # нескольких сборок, напр. "П4, П5") — разбиваем на отдельные теги,
                # иначе зависимость не находится ни для одной из них.
                for part in v.split(','):
                    part = part.strip()
                    if part:
                        result.add(part)
            return result
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
    pila_all = pila | pila_pos  # ЭЛЕМЕНТ + ПОЗ. СОГЛАСНО ЧЕРТЕЖА из ПИЛА

    # СВЕРЛЕНИЕ <- ПИЛА (по любой колонке) + ПЛАЗМА (прямое совпадение или last-word)
    for e in sverlenie & pila_all:
        deps.append(('СВЕРЛЕНИЕ', e, 'ПИЛА', e))
    for se in sverlenie:
        if se in plazma:
            deps.append(('СВЕРЛЕНИЕ', se, 'ПЛАЗМА', se))
        else:
            pm = plazma_upper.get(se.split()[-1].upper())
            if pm:
                deps.append(('СВЕРЛЕНИЕ', se, 'ПЛАЗМА', pm))

    # СБОРКА <- ПИЛА + ПЛАЗМА (прямое совпадение или last-word)
    for e in sborka & pila_all:
        deps.append(('СБОРКА', e, 'ПИЛА', e))
    for se in sborka:
        if se in plazma:
            deps.append(('СБОРКА', se, 'ПЛАЗМА', se))
        else:
            pm = plazma_upper.get(se.split()[-1].upper())
            if pm:
                deps.append(('СБОРКА', se, 'ПЛАЗМА', pm))

    # СВАРКА <- СБОРКА + СВЕРЛЕНИЕ (оба должны быть выполнены)
    for e in svarka & sborka:
        deps.append(('СВАРКА', e, 'СБОРКА', e))
    for e in svarka & sverlenie:
        deps.append(('СВАРКА', e, 'СВЕРЛЕНИЕ', e))

    # ГРУНТОВКА <- СВАРКА
    for e in grunt & svarka:
        deps.append(('ГРУНТОВКА', e, 'СВАРКА', e))
    # ГРУНТОВКА <- ПИЛА или ПЛАЗМА (прямые элементы: нет в СВАРКА)
    for e in grunt - svarka:
        if e in pila_all:
            deps.append(('ГРУНТОВКА', e, 'ПИЛА', e))
        elif e in plazma:
            deps.append(('ГРУНТОВКА', e, 'ПЛАЗМА', e))
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
             AND (wo.comment IS NULL OR wo.comment = '')
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


def _sync_employees_sheet(file_id: str, project_name: str):
    """Обновляет левую таблицу листа СОТРУДНИКИ из БД (cols A:F, строки 4+)."""
    employees = db.fetchall(
        "SELECT full_name, specialization, telegram_username "
        "FROM employees WHERE is_active=true ORDER BY full_name",
        []
    )

    emp_rows = []
    for emp in employees:
        specs = emp['specialization'] or []
        tg    = emp['telegram_username'] or ''
        emp_rows.append(['', emp['full_name'], ', '.join(specs), tg, 'ДА', ''])

    # Заполняем до 100 строк пустыми чтобы затереть старые данные
    MAX_ROWS = 100
    while len(emp_rows) < MAX_ROWS:
        emp_rows.append(['', '', '', '', '', ''])

    try:
        _h_emp = _sheet_hash(emp_rows)
        _st_emp = _state_get(project_name, '__employees__')
        if _st_emp and _st_emp['content_hash'] == _h_emp and float(_st_emp['age'] or 0) < FULL_REFRESH_SEC:
            return
        ws = sheets._ws(file_id, 'СОТРУДНИКИ', rw=True)
        sheets._wapi_call(ws.update, emp_rows, 'A4')
        _state_set(project_name, '__employees__', _h_emp)
        logger.info(f'  [{project_name}] СОТРУДНИКИ: обновлено {len(employees)} сотрудников')
    except Exception as e:
        if 'WorksheetNotFound' in type(e).__name__ or 'not found' in str(e).lower():
            logger.info(f'  [{project_name}] СОТРУДНИКИ: лист отсутствует, пропускаем')
        else:
            logger.warning(f'  [{project_name}] СОТРУДНИКИ: ошибка обновления — {e}')


def _repair_orphan_partials(dry_run: bool = False) -> list:
    """Достраивает частичное выполнение, оборванное падением процесса.

    Признак: строка ВЫПОЛНЕНО с qty_done < quantity (в листе КОЛ-ВО не успели уменьшить),
    история подтверждает ИМЕННО эту строку (по row_id), строки-остатка нет.
    Достраиваем: КОЛ-ВО/МАССА/СУММА = выполненная часть + строка-остаток ПЛАН.
    Строки моложе 5 минут не трогаем (обработчик бота мог быть ещё в работе)."""
    import uuid as _uuid
    rows = db.fetchall(
        """SELECT wo.id, wo.project_name, wo.file_id, wo.sheet_name, wo.row_num, wo.row_id,
                  wo.position, wo.element, wo.quantity, wo.qty_done, wo.payment_sum,
                  wo.unit_weight, wo.total_weight, wo.drawing_link, wo.qty_holes
           FROM work_orders wo
           WHERE wo.status = 'ВЫПОЛНЕНО' AND wo.qty_done IS NOT NULL AND wo.qty_done < wo.quantity
             AND wo.project_name IN (SELECT project_name FROM projects WHERE status='АКТИВНЫЙ')
             AND wo.row_num > 0 AND wo.row_id IS NOT NULL
             AND wo.date_fact >= CURRENT_DATE - 2
             AND wo.updated_at < NOW() - INTERVAL '5 minutes'
             AND (SELECT COALESCE(SUM(h.qty), 0) FROM work_order_history h WHERE h.row_id = wo.row_id) = wo.qty_done
             AND NOT EXISTS (
                 SELECT 1 FROM work_orders r
                 WHERE r.project_name = wo.project_name AND r.sheet_name = wo.sheet_name AND r.id <> wo.id
                   AND COALESCE(r.position, '') = COALESCE(wo.position, '')
                   AND COALESCE(r.element, '') = COALESCE(wo.element, '')
                   AND r.status IN ('ПЛАН', 'ЧАСТИЧНО', 'БЛОК')
                   AND r.quantity = wo.quantity - wo.qty_done)""",
        []
    )
    if dry_run or not rows:
        return [dict(r) for r in rows]
    for t in rows:
        try:
            orig, done = float(t['quantity']), int(t['qty_done'])
            remaining = int(orig) - done
            ps, tw = float(t['payment_sum'] or 0), float(t['total_weight'] or 0)
            ps_done, ps_rem = round(ps / orig * done, 2), round(ps / orig * remaining, 2)
            tw_done, tw_rem = round(tw / orig * done, 3), round(tw / orig * remaining, 3)
            new_row_id = str(_uuid.uuid4())
            with db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE work_orders SET quantity=%s, total_weight=%s, payment_sum=%s WHERE id=%s",
                        [done, tw_done if tw else None, ps_done if ps else None, t['id']])
                    cur.execute(
                        "INSERT INTO work_orders (project_name, file_id, sheet_name, row_num, row_id, position, element, "
                        "quantity, unit_weight, total_weight, payment_sum, executor, date_plan, priority, mandatory, "
                        "status, drawing_link, qty_holes) "
                        "SELECT project_name, file_id, sheet_name, -%s, %s, position, element, %s, unit_weight, %s, %s, "
                        "       NULL, NULL, priority, false, 'ПЛАН', drawing_link, qty_holes FROM work_orders WHERE id=%s",
                        [t['id'], new_row_id, remaining, tw_rem if tw else None, ps_rem if ps else None, t['id']])
            f, s, rn = t['file_id'], t['sheet_name'], t['row_num']
            sheets.update_cell_by_header(f, s, rn, 'КОЛ-ВО', done)
            if tw:
                sheets.update_cell_by_header(f, s, rn, 'МАССА ВСЕХ (кг)', tw_done)
            if ps:
                sheets.update_cell_by_header(f, s, rn, 'СУММА К ОПЛАТЕ', ps_done)
            sheets.insert_remainder_row(f, s, rn, {
                'ЭЛЕМЕНТ': t['element'] or '', 'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': t['position'] or '',
                'КОЛ-ВО': remaining,
                'МАССА ЕД. (кг)': float(t['unit_weight']) if t['unit_weight'] else '',
                'МАССА ВСЕХ (кг)': tw_rem if tw else '', 'СУММА К ОПЛАТЕ': ps_rem if ps else '',
                'СТАТУС': 'ПЛАН', 'ОБЯЗАТЕЛЬНАЯ': 'НЕТ', 'ИСПОЛНИТЕЛЬ': '', 'ROW_ID': new_row_id,
                'ССЫЛКА НА ЧЕРТЁЖ': t['drawing_link'] or '',
            })
            logger.info(f"  repair_orphan_partials: {t['project_name']}/{s} id={t['id']} {done}+{remaining}")
            logger.audit(action='auto_repair_partial',
                         details={'task_id': t['id'], 'project': t['project_name'], 'sheet': s,
                                  'done': done, 'remainder': remaining, 'remainder_row_id': new_row_id},
                         result='success')
        except Exception as e:
            logger.error(f"  repair_orphan_partials id={t['id']}: {e}")
    return [dict(r) for r in rows]


import fcntl
import hashlib
import json as _json_s
import os as _os_s

_STATE_DIR = '/root/naryady/prod/state'
FULL_REFRESH_SEC = 6 * 3600   # полная обработка листа не реже раза в 6 часов, даже если он не менялся


def _ensure_sync_state():
    db.execute(
        "CREATE TABLE IF NOT EXISTS sync_state ("
        "project_name text NOT NULL, sheet_name text NOT NULL, content_hash text, "
        "full_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (project_name, sheet_name))", [])


def _sheet_hash(values) -> str:
    return hashlib.sha1(_json_s.dumps(values, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def _state_get(project_name: str, sheet_name: str):
    return db.fetchone(
        "SELECT content_hash, EXTRACT(EPOCH FROM (now() - full_at)) AS age "
        "FROM sync_state WHERE project_name=%s AND sheet_name=%s", [project_name, sheet_name])


def _state_set(project_name: str, sheet_name: str, h: str):
    db.execute(
        "INSERT INTO sync_state (project_name, sheet_name, content_hash, full_at) VALUES (%s,%s,%s,now()) "
        "ON CONFLICT (project_name, sheet_name) DO UPDATE SET content_hash=EXCLUDED.content_hash, full_at=now()",
        [project_name, sheet_name, h])


class _ProjectLock:
    """Межпроцессная блокировка проекта: два прохода синка не обрабатывают один проект одновременно."""
    def __init__(self, project_id):
        self.path = f'{_STATE_DIR}/sync_project_{project_id}.lock'
        self.fd = None

    def acquire(self, wait_sec: int = 0) -> bool:
        _os_s.makedirs(_STATE_DIR, exist_ok=True)
        self.fd = open(self.path, 'w')
        deadline = time.time() + wait_sec
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() >= deadline:
                    self.fd.close()
                    self.fd = None
                    return False
                time.sleep(2)

    def release(self):
        if self.fd:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()
            finally:
                self.fd = None


TITLES_TTL_SEC = 3600


def _existing_sheets(project_name: str, file_id: str, force: bool = False) -> list:
    """Рабочие листы, которые реально существуют в таблице проекта. Запрашивать несуществующий лист
    в batchGet нельзя (400 «Unable to parse range» → откат на чтение по листам = 7 запросов вместо 1)."""
    if not force:
        st = _state_get(project_name, '__titles__')
        if st and st['content_hash'] and float(st['age'] or 0) < TITLES_TTL_SEC:
            try:
                names = _json_s.loads(st['content_hash'])
                return [s for s in config.WORK_SHEETS if s in names]
            except Exception:
                pass
    try:
        ss = sheets._ss(file_id)
        titles = [w.title for w in sheets._api_call(ss.worksheets)]
    except Exception as e:
        logger.warning(f'  {project_name}: не удалось получить список листов ({e}), работаем по полному списку')
        return list(config.WORK_SHEETS)
    _state_set(project_name, '__titles__', _json_s.dumps(titles, ensure_ascii=False))
    return [s for s in config.WORK_SHEETS if s in titles]


def _delete_stale_positional(project_name: str, sheet_name: str, nums: list, ids: list) -> int:
    """Старые копии строк: row_id ещё есть в листе, но уже на другой позиции. Проверка «призраков» по одному row_id
    их пропускала, и они дублировали задачу (в т.ч. оплату). Сверяем пары (row_num, row_id)."""
    r = db.fetchone(
        """
        WITH del AS (
            DELETE FROM work_orders w
            WHERE w.project_name = %s AND w.sheet_name = %s
              AND w.row_num > 0 AND w.row_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM unnest(%s::int[], %s::text[]) AS s(rn, rid)
                  WHERE s.rn = w.row_num AND s.rid = w.row_id::text)
            RETURNING 1
        ) SELECT COUNT(*) AS cnt FROM del
        """,
        [project_name, sheet_name, [int(n) for n in nums], [str(i) for i in ids]]
    )
    return int(r['cnt']) if r else 0


def _api_total() -> int:
    return sum(sheets.api_stats().values())


def run(progress_cb=None, only_project=None, force=False):
    """
    progress_cb(phase, data) — опциональный коллбек для отображения прогресса в боте.
    phase: 'project_start' | 'sheet_done' | 'project_done' | 'deps_done' | 'sync_done'
    only_project — id или название проекта: синкать только его (кнопка в боте, новый проект).
    force — не пропускать листы, которые не менялись.
    """
    logger.info('Sync started' + (f' (проект: {only_project}, force={force})' if only_project else ''))
    _ensure_sync_state()
    sheets.api_stats(reset=True)
    total_synced = 0
    try:
        _repair_orphan_partials()
    except Exception as _e_rp:
        logger.warning(f'repair_orphan_partials: {_e_rp}')

    projects = db.fetchall(
        "SELECT id, project_name, sheet_id FROM projects WHERE status='АКТИВНЫЙ'",
        []
    )
    if only_project is not None:
        projects = [p for p in projects if p['id'] == only_project or p['project_name'] == only_project]
    files = [{'id': p['id'], 'file_id': p['sheet_id'], 'project_name': p['project_name']} for p in projects]

    logger.info(f'Found {len(files)} active project(s) in DB')
    total_sheets = len(files) * len(config.WORK_SHEETS)
    sheets_done  = 0

    for f in files:
        file_id      = f['file_id']
        project_name = f['project_name']
        _lock = _ProjectLock(f['id'])
        if not _lock.acquire(wait_sec=300 if only_project is not None else 0):
            logger.info(f'  {project_name}: уже обрабатывается другим процессом — пропуск')
            sheets_done += len(config.WORK_SHEETS)
            f['changed'] = False
            continue
        _calls0 = _api_total()
        logger.info(f'Processing: {project_name} ({file_id})')

        if progress_cb:
            progress_cb('project_start', {'project': project_name})

        sheet_errors = 0
        project_changed = False

        # B: Читаем все листы проекта одним API-вызовом
        _existing = _existing_sheets(project_name, file_id, force)
        try:
            _batch_data = sheets.read_all_sheets_batch(file_id, _existing) if _existing else {}
            logger.info(f'  batchGet: загружено {len(_batch_data)} листов')
        except Exception as _be:
            logger.warning(f'  batchGet failed, fallback to per-sheet: {_be}')
            _batch_data = {}

        for sheet_name in config.WORK_SHEETS:
            if sheet_name not in _existing:
                sheets_done += 1
                logger.info(f'  {sheet_name}: лист отсутствует, пропускаем')
                if progress_cb:
                    progress_cb('sheet_done', {
                        'project': project_name, 'sheet': sheet_name, 'rows': 0,
                        'sheets_done': sheets_done, 'total_sheets': total_sheets, 'splits': 0,
                    })
                continue
            try:
                _pre = _batch_data.get(sheet_name)
                _h = _sheet_hash(_pre) if _pre is not None else None
                _degraded = False
                if _h is not None and not force:
                    _st = _state_get(project_name, sheet_name)
                    if _st and _st['content_hash'] == _h and float(_st['age'] or 0) < FULL_REFRESH_SEC:
                        sheets_done += 1
                        if progress_cb:
                            progress_cb('sheet_done', {
                                'project': project_name, 'sheet': sheet_name,
                                'rows': max(0, len(_pre) - 2), 'sheets_done': sheets_done,
                                'total_sheets': total_sheets, 'splits': 0, 'skipped': True,
                            })
                        continue
                rows = sheets.read_sheet(file_id, sheet_name,
                                         preloaded=_pre)
                _upsert_employees(rows, sheet_name)
                pending_row_ids = []
                seen_row_ids = []
                seen_row_nums = []
                pending_status_fix = []
                pending_mandatory_fix = []
                synced = 0
                for i, row in enumerate(rows):
                    r = _normalize_row(row, sheet_name, i+1, project_name, file_id, f['id'])
                    if not r['row_id']:
                        new_id = str(uuid.uuid4())
                        r['row_id'] = new_id
                        pending_row_ids.append((i+1, new_id))
                    seen_row_ids.append(r['row_id'])
                    seen_row_nums.append(i + 1)
                    db.execute(UPSERT_SQL, r)
                    synced += 1
                    # Авто-ПЛАН: строка заполнена, но статус пустой
                    if not row.get('СТАТУС', '').strip() and (
                        r['position'] or r['element']
                    ) and r['quantity']:
                        pending_status_fix.append((i + 1, r['row_id']))
                    # Авто-НЕТ: строка заполнена, ОБЯЗАТЕЛЬНАЯ пустая
                    if not row.get('ОБЯЗАТЕЛЬНАЯ', '').strip() and (
                        r['position'] or r['element']
                    ) and r['quantity'] and r.get('status') != 'ИТОГО':
                        pending_mandatory_fix.append((i + 1, r['row_id']))
                total_synced += synced
                if synced > 0:
                    project_changed = True

                if pending_row_ids:
                    sheets.write_row_ids(file_id, sheet_name, pending_row_ids)
                    logger.info(f'  {sheet_name}: записано {len(pending_row_ids)} новых ROW_ID')

                # Авто-ПЛАН: ставим СТАТУС = 'ПЛАН' в лист для строк с пустым статусом
                if pending_status_fix:
                    try:
                        cm_fix = sheets._col_map(file_id, sheet_name, rw=True)
                        status_col = cm_fix.get('СТАТУС')
                        if status_col:
                            ws_fix = sheets._ws(file_id, sheet_name, rw=True)
                            stat_letter = sheets._col_letter(status_col)
                            fix_upd = [
                                {'range': f'{stat_letter}{rn + 2}', 'values': [['ПЛАН']]}
                                for rn, _ in pending_status_fix
                            ]
                            for i in range(0, len(fix_upd), 100):
                                sheets._api_call(ws_fix.batch_update, fix_upd[i:i+100])
                            logger.info(f'  {sheet_name}: авто-ПЛАН {len(pending_status_fix)} строк')
                    except Exception as _sfix:
                        logger.warning(f'  {sheet_name}: авто-ПЛАН пропущен: {_sfix}'); _degraded = True

                # Авто-НЕТ: ставим ОБЯЗАТЕЛЬНАЯ = 'НЕТ' для строк с пустым полем
                if pending_mandatory_fix:
                    try:
                        cm_mfix = sheets._col_map(file_id, sheet_name, rw=True)
                        mand_col = cm_mfix.get('ОБЯЗАТЕЛЬНАЯ')
                        if mand_col:
                            ws_mfix = sheets._ws(file_id, sheet_name, rw=True)
                            mand_letter = sheets._col_letter(mand_col)
                            mfix_upd = [
                                {'range': f'{mand_letter}{rn + 2}', 'values': [['НЕТ']]}
                                for rn, _ in pending_mandatory_fix
                            ]
                            for i in range(0, len(mfix_upd), 100):
                                sheets._api_call(ws_mfix.batch_update, mfix_upd[i:i+100])
                            logger.info(f'  {sheet_name}: авто-НЕТ (ОБЯЗАТЕЛЬНАЯ) {len(pending_mandatory_fix)} строк')
                    except Exception as _mfix:
                        logger.warning(f'  {sheet_name}: авто-НЕТ пропущен: {_mfix}'); _degraded = True

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
                    _stale_n = _delete_stale_positional(project_name, sheet_name, seen_row_nums, seen_row_ids)
                    if _stale_n:
                        logger.info(f'  {sheet_name}: удалено {_stale_n} устаревших копий строк (row_id на другой позиции)')

                try:
                    _apply_status_format_batch(file_id, sheet_name, rows)
                except Exception as _fmt_err:
                    logger.warning(f'  {sheet_name}: статус-форматирование пропущено: {_fmt_err}'); _degraded = True

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
                _update_block_column(file_id, sheet_name, project_name, rows, raw_values=_pre)
                if _h is not None and not _degraded:
                    _state_set(project_name, sheet_name, _h)

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
        f['changed'] = project_changed
        if progress_cb:
            progress_cb('project_done', {'project': project_name, 'errors': sheet_errors})

        _lock.release()
        # Пауза между проектами — только если проект реально обращался к API (даём сбросить per-minute квоту)
        if f != files[-1] and (_api_total() - _calls0) > 6:
            logger.info(f'  Пауза 15s перед следующим проектом...')
            time.sleep(15)

    # Автогенерация зависимостей — только для проектов где были изменения
    changed_files = [f for f in files if f.get('changed')]
    if changed_files:
        logger.info(f'Пересчёт зависимостей для {len(changed_files)} из {len(files)} проекта(ов)')
        for f in changed_files:
            try:
                _rebuild_element_dependencies(f['project_name'], f['file_id'])
            except Exception as e:
                logger.error(f'  Зависимости rebuild error ({f["project_name"]}): {e}')

        # Обновляем КОЛОНКУ БЛОК только для изменённых проектов
        for f in changed_files:
            for sheet_name in ['СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА']:
                try:
                    _update_block_column(f['file_id'], sheet_name, f['project_name'])
                except Exception as e:
                    logger.warning(f'  БЛОК update after deps ({f["project_name"]}/{sheet_name}): {e}')

        # Reconcile: снимаем БЛОК с задач, чьи зависимости выполнены
        for f in changed_files:
            try:
                _reconcile_blocks(f['project_name'])
            except Exception as e:
                logger.error(f'  Reconcile error ({f["project_name"]}): {e}')
    else:
        logger.info('Зависимости: изменений нет, пересчёт пропущен')

    if progress_cb:
        progress_cb('deps_done', {})

    # Обновляем лист СОТРУДНИКИ во всех проектах из БД
    for f in files:
        try:
            _sync_employees_sheet(f['file_id'], f['project_name'])
            if f != files[-1]:
                time.sleep(2)
        except Exception as e:
            logger.warning(f'  _sync_employees_sheet error ({f["project_name"]}): {e}')

    db.execute(
        "UPDATE work_orders SET mandatory=true WHERE status='ЧАСТИЧНО' AND (mandatory IS NULL OR mandatory=false)",
        []
    )
    logger.info(f'Sync done. Total: {total_synced} rows')
    logger.info(f'  Обращений к Sheets API за проход: {_api_total()} {sheets.api_stats()}')

    if progress_cb:
        progress_cb('sync_done', {'total': total_synced})

    return total_synced

if __name__ == '__main__':
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument('--project', type=int, default=None, help='id проекта (только он)')
    _ap.add_argument('--force', action='store_true', help='не пропускать неизменённые листы')
    _ap.add_argument('--progress-file', default=None, help='файл для построчного JSON-прогресса (для бота)')
    _args = _ap.parse_args()
    _cb = None
    if _args.progress_file:
        def _cb(phase, data, _pf=_args.progress_file):
            try:
                with open(_pf, 'a', encoding='utf-8') as _fh:
                    _fh.write(_json_s.dumps({'phase': phase, 'data': data}, ensure_ascii=False, default=str) + '\n')
            except Exception:
                pass
    run(progress_cb=_cb, only_project=_args.project, force=_args.force)
