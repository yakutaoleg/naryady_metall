import sys, os, time, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def _to_float(val):
    try: return float(str(val).replace(",", ".").strip()) or None
    except: return None

from src import sheets, db, logger, config

def _parse_date(s):
    if not s: return None
    s = str(s).strip()
    # ISO формат yyyy-mm-dd
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s
    # Формат дд.мм.гггг
    parts = s.split('.')
    if len(parts) == 3:
        return f'{parts[2]}-{parts[1]}-{parts[0]}'
    return None

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
        'position':     row.get('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', '').strip() or None,
        'element':      row.get('ЭЛЕМЕНТ', '').strip() or None,
        'quantity':     _to_float(row.get('КОЛ-ВО', '') or 0) or None,
        'unit_weight':  _to_float(row.get('МАССА ЕД. (кг)', '') or 0) or None,
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
    }

# UPSERT по row_id (если есть) — иначе fallback на (project_name, sheet_name, row_num)
UPSERT_SQL = '''
INSERT INTO work_orders (
    project_id, project_name, file_id, sheet_name, row_num, row_id,
    position, element, quantity, unit_weight, total_weight, payment_sum,
    executor, date_plan, priority, mandatory, status, comment, date_fact,
    drawing_link, updated_at
) VALUES (
    %(project_id)s, %(project_name)s, %(file_id)s, %(sheet_name)s, %(row_num)s, %(row_id)s,
    %(position)s, %(element)s, %(quantity)s, %(unit_weight)s, %(total_weight)s, %(payment_sum)s,
    %(executor)s, %(date_plan)s, %(priority)s, %(mandatory)s, %(status)s, %(comment)s, %(date_fact)s,
    %(drawing_link)s, NOW()
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
    status    = EXCLUDED.status,
    qty_done  = CASE WHEN EXCLUDED.status = 'ПЛАН' THEN NULL ELSE work_orders.qty_done END,
    comment   = EXCLUDED.comment,
    date_fact = EXCLUDED.date_fact
'''

def _write_row_id_to_sheet(file_id: str, sheet_name: str, row_num: int, row_id: str):
    """Записывает сгенерированный ROW_ID обратно в таблицу."""
    try:
        sheets.write_row_id(file_id, sheet_name, row_num, row_id)
    except Exception as e:
        logger.error(f'Не удалось записать ROW_ID в таблицу {sheet_name} row {row_num}: {e}')

def _compute_block(project_name: str, sheet_name: str, position: str, element: str) -> str:
    """Вычисляет значение колонки БЛОК.
    requires_position в element_dependencies — это имя ЭЛЕМЕНТА в листе-зависимости,
    а не имя позиции. Проверяем что ВСЕ позиции с таким элементом выполнены.
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
    for d in deps:
        if d['requires_sheet'] in blocked:
            continue
        # Считаем: сколько всего позиций с таким элементом и сколько НЕ выполнено
        # Проверяем и element и position — СБОРКА/СВАРКА хранят ключ в position, ПЛАЗМА/ПИЛА — в element
        rp = d['requires_position']
        not_done = db.fetchone(
            """SELECT COUNT(*) as cnt FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND (element=%s OR position=%s)
                 AND status != 'ВЫПОЛНЕНО'""",
            [project_name, d['requires_sheet'], rp, rp]
        )
        # Если хоть одна не выполнена (или позиций нет вообще — тоже не готово)
        total = db.fetchone(
            """SELECT COUNT(*) as cnt FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND (element=%s OR position=%s)""",
            [project_name, d['requires_sheet'], rp, rp]
        )
        if not total or total['cnt'] == 0 or (not_done and not_done['cnt'] > 0):
            blocked.append(d['requires_sheet'])
    return '✅' if not blocked else '⛔ ' + ', '.join(blocked)


def _update_block_column(file_id: str, sheet_name: str, project_name: str, rows: list[dict] = None):
    """Обновляет колонку БЛОК используя row_num из БД — корректно при пустых строках в листе."""
    cm = sheets._col_map(file_id, sheet_name, rw=True)
    block_col = cm.get('БЛОК')
    if not block_col:
        return
    ws = sheets._ws(file_id, sheet_name, rw=True)

    work_rows = db.fetchall(
        """SELECT row_num, position, element, status FROM work_orders
           WHERE project_name=%s AND sheet_name=%s AND row_num > 0""",
        [project_name, sheet_name]
    )
    updates = []
    for r in work_rows:
        if r['status'] == 'ВЫПОЛНЕНО':
            updates.append({
                'range': f'{sheets._col_letter(block_col)}{r["row_num"] + 2}',
                'values': [['']]
            })
            continue
        val = _compute_block(project_name, sheet_name, r['position'], r['element'])
        updates.append({
            'range': f'{sheets._col_letter(block_col)}{r["row_num"] + 2}',
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
    """Автосоздаёт/обновляет сотрудников из поля ИСПОЛНИТЕЛЬ."""
    if sheet_name not in config.WORK_SHEETS:
        return
    seen = set()
    for row in rows:
        name = row.get('ИСПОЛНИТЕЛЬ', '').strip()
        if name and name not in seen:
            seen.add(name)
            db.execute(UPSERT_EMPLOYEE_SQL, [name, sheet_name, sheet_name, sheet_name])


def run():
    logger.info('Sync started')
    total_synced = 0

    projects = db.fetchall(
        "SELECT id, project_name, sheet_id FROM projects WHERE status='АКТИВНЫЙ'",
        []
    )
    files = [{'id': p['id'], 'file_id': p['sheet_id'], 'project_name': p['project_name']} for p in projects]

    logger.info(f'Found {len(files)} active project(s) in DB')

    for f in files:
        file_id      = f['file_id']
        project_name = f['project_name']
        logger.info(f'Processing: {project_name} ({file_id})')

        sheet_errors = 0
        for sheet_name in config.WORK_SHEETS:
            try:
                rows = sheets.read_sheet(file_id, sheet_name)
                _upsert_employees(rows, sheet_name)
                pending_row_ids = []  # [(sheet_row_num, new_uuid)]
                synced = 0
                for i, row in enumerate(rows):
                    r = _normalize_row(row, sheet_name, i+1, project_name, file_id, f['id'])
                    # Если ROW_ID пустой — генерируем
                    if not r['row_id']:
                        new_id = str(uuid.uuid4())
                        r['row_id'] = new_id
                        pending_row_ids.append((i+1, new_id))
                    db.execute(UPSERT_SQL, r)
                    synced += 1
                total_synced += synced

                # Записываем сгенерированные ROW_ID обратно в таблицу
                if pending_row_ids:
                    sheets.write_row_ids(file_id, sheet_name, pending_row_ids)
                    logger.info(f'  {sheet_name}: записано {len(pending_row_ids)} новых ROW_ID')

                logger.info(f'  {sheet_name}: {synced} rows')
                logger.audit(action='sync_sheet',
                             details={'project': project_name, 'sheet': sheet_name, 'rows': synced})
                _update_block_column(file_id, sheet_name, project_name, rows)
                time.sleep(2)
            except Exception as e:
                if 'WorksheetNotFound' in type(e).__name__ or 'not found' in str(e).lower():
                    logger.info(f'  {sheet_name}: лист отсутствует, пропускаем')
                else:
                    sheet_errors += 1
                    logger.error(f'  {sheet_name}: ERROR — {e}')
                    logger.audit(action='sync_sheet',
                                 details={'project': project_name, 'sheet': sheet_name},
                                 result='error', error_msg=str(e))

        if f.get('id') and sheet_errors == 0:
            db.execute(
                "UPDATE projects SET last_synced_at=NOW() WHERE id=%s",
                [f['id']]
            )

    # Синхронизация вкладки ЗАВИСИМОСТИ
    for f in files:
        file_id      = f['file_id']
        project_name = f['project_name']
        try:
            # ЗАВИСИМОСТИ: заголовки на строке 1, данные со строки 2
            ws_z = sheets._ws(file_id, 'ЗАВИСИМОСТИ')
            all_vals = sheets._api_call(ws_z.get_all_values)
            if len(all_vals) < 2:
                logger.info(f'  ЗАВИСИМОСТИ: пусто')
                continue
            z_headers = all_vals[0]
            rows = [dict(zip(z_headers, r)) for r in all_vals[1:] if any(c.strip() for c in r)]
            db.execute(
                "DELETE FROM element_dependencies WHERE project_name=%s",
                [project_name]
            )
            count = 0
            for row in rows:
                waiting_sheet = row.get('КТО ЖДЁТ (специализация)', '').strip()
                element       = row.get('ЭЛЕМЕНТ (который ждёт)', '').strip()
                req_sheet     = row.get('ЗАВИСИТ ОТ (специализация)', '').strip()
                req_pos       = row.get('ПОЗИЦИЯ (должна быть выполнена)', '').strip()
                if not element or not req_sheet or not req_pos:
                    continue
                db.execute(
                    """INSERT INTO element_dependencies
                       (project_name, waiting_sheet, element, requires_sheet, requires_position)
                       VALUES (%s, %s, %s, %s, %s)""",
                    [project_name, waiting_sheet, element, req_sheet, req_pos]
                )
                count += 1
            logger.info(f'  ЗАВИСИМОСТИ: {count} записей')
        except Exception as e:
            logger.error(f'  ЗАВИСИМОСТИ: ERROR — {e}')

    db.execute(
        "UPDATE work_orders SET mandatory=true WHERE status='ЧАСТИЧНО' AND (mandatory IS NULL OR mandatory=false)",
        []
    )
    logger.info(f'Sync done. Total: {total_synced} rows')
    return total_synced

if __name__ == '__main__':
    run()
