import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def _to_float(val):
    try: return float(str(val).replace(",", ".").strip()) or None
    except: return None

from src import sheets, db, logger, config

def _parse_date(s):
    if not s: return None
    parts = str(s).strip().split('.')
    if len(parts) == 3:
        return f'{parts[2]}-{parts[1]}-{parts[0]}'
    return None

def _normalize_row(row: dict, sheet_name: str, row_num: int,
                   project_name: str, file_id: str) -> dict:
    mandatory = row.get('ОБЯЗАТЕЛЬНАЯ', '').strip()
    return {
        'project_name': project_name,
        'file_id':      file_id,
        'sheet_name':   sheet_name,
        'row_num':      row_num,
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

UPSERT_SQL = '''
INSERT INTO work_orders (
    project_name, file_id, sheet_name, row_num,
    position, element, quantity, unit_weight, total_weight, payment_sum,
    executor, date_plan, priority, mandatory, status, comment, date_fact,
    drawing_link, updated_at
) VALUES (
    %(project_name)s, %(file_id)s, %(sheet_name)s, %(row_num)s,
    %(position)s, %(element)s, %(quantity)s, %(unit_weight)s, %(total_weight)s, %(payment_sum)s,
    %(executor)s, %(date_plan)s, %(priority)s, %(mandatory)s, %(status)s, %(comment)s, %(date_fact)s,
    %(drawing_link)s, NOW()
)
ON CONFLICT (project_name, sheet_name, row_num) DO UPDATE SET
    position     = EXCLUDED.position,
    element      = EXCLUDED.element,
    quantity     = EXCLUDED.quantity,
    unit_weight  = EXCLUDED.unit_weight,
    total_weight = EXCLUDED.total_weight,
    payment_sum  = EXCLUDED.payment_sum,
    executor     = EXCLUDED.executor,
    date_plan    = EXCLUDED.date_plan,
    priority     = EXCLUDED.priority,
    mandatory    = EXCLUDED.mandatory,
    drawing_link = EXCLUDED.drawing_link,
    updated_at   = NOW(),
    status  = CASE WHEN work_orders.status IN ('ВЫПОЛНЕНО','БЛОК')
                   THEN work_orders.status ELSE EXCLUDED.status END,
    comment = CASE WHEN work_orders.status IN ('ВЫПОЛНЕНО','БЛОК')
                   THEN work_orders.comment ELSE EXCLUDED.comment END,
    date_fact = CASE WHEN work_orders.status IN ('ВЫПОЛНЕНО','БЛОК')
                     THEN work_orders.date_fact ELSE EXCLUDED.date_fact END
'''

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
                synced = 0
                for i, row in enumerate(rows):
                    r = _normalize_row(row, sheet_name, i+1, project_name, file_id)
                    db.execute(UPSERT_SQL, r)
                    synced += 1
                total_synced += synced
                logger.info(f'  {sheet_name}: {synced} rows')
                logger.audit(
                    action='sync_sheet',
                    details={'project': project_name, 'sheet': sheet_name, 'rows': synced}
                )
            except Exception as e:
                sheet_errors += 1
                logger.error(f'  {sheet_name}: ERROR — {e}')
                logger.audit(
                    action='sync_sheet',
                    details={'project': project_name, 'sheet': sheet_name},
                    result='error', error_msg=str(e)
                )

        if f.get('id') and sheet_errors == 0:
            db.execute(
                "UPDATE projects SET last_synced_at=NOW() WHERE id=%s",
                [f['id']]
            )

    # Автогенерация зависимостей из данных листов
    for f in files:
        try:
            _rebuild_element_dependencies(f['project_name'], f['file_id'])
        except Exception as e:
            logger.error(f'  Зависимости rebuild error ({f["project_name"]}): {e}')

    # Reconcile: снимаем БЛОК с задач, чьи зависимости уже выполнены
    for f in files:
        try:
            _reconcile_blocks(f['project_name'])
        except Exception as e:
            logger.error(f'  Reconcile error ({f["project_name"]}): {e}')

    logger.info(f'Sync done. Total: {total_synced} rows')
    return total_synced


def _rebuild_element_dependencies(project_name: str, file_id: str):
    """Автогенерирует element_dependencies из реальных данных листов."""

    def get_elems(sheet_name, col='ЭЛЕМЕНТ'):
        try:
            ws   = sheets._ws(file_id, sheet_name)
            data = ws.get_all_values()
            if len(data) < 2:
                return set()
            hdrs = data[1]
            idx  = next((i for i, h in enumerate(hdrs) if h.strip() == col), None)
            if idx is None:
                return set()
            return {r[idx].strip() for r in data[2:]
                    if len(r) > idx and r[idx].strip()
                    and r[idx].strip() not in ('ИТОГО', '')}
        except Exception as e:
            logger.error(f'  _rebuild: не удалось прочитать {sheet_name}/{col}: {e}')
            return set()

    pila      = get_elems('ПИЛА')
    plazma    = get_elems('ПЛАЗМА')
    sverlenie = get_elems('СВЕРЛЕНИЕ')
    sborka    = get_elems('СБОРКА')
    svarka    = get_elems('СВАРКА',    'ПОЗ. СОГЛАСНО ЧЕРТЕЖА')
    grunt     = get_elems('ГРУНТОВКА', 'Марка')
    pokraska  = get_elems('ПОКРАСКА',  'Марка')

    deps = []
    plazma_upper     = {e.upper(): e for e in plazma}
    sborka_via_sverl = sborka & sverlenie

    for e in sverlenie & pila:
        deps.append(('СВЕРЛЕНИЕ', e, 'ПИЛА', e))
    for e in sborka & sverlenie:
        deps.append(('СБОРКА', e, 'СВЕРЛЕНИЕ', e))
    for e in (sborka & pila) - sborka_via_sverl:
        deps.append(('СБОРКА', e, 'ПИЛА', e))
    for se in sborka - sborka_via_sverl:
        pm = plazma_upper.get(se.split()[-1].upper())
        if pm:
            deps.append(('СБОРКА', se, 'ПЛАЗМА', pm))
    for e in svarka & sborka:
        deps.append(('СВАРКА', e, 'СБОРКА', e))
    for e in grunt & svarka:
        deps.append(('ГРУНТОВКА', e, 'СВАРКА', e))
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
    """Снимает БЛОК с задач, чьи зависимости уже выполнены (актуализация после ручных правок)."""
    blocked = db.fetchall(
        """SELECT wo.id, wo.element, wo.sheet_name, wo.file_id, wo.row_num
           FROM work_orders wo
           WHERE wo.project_name=%s AND wo.status='БЛОК'
             AND wo.element IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM element_dependencies ed
               WHERE ed.project_name=wo.project_name AND ed.element=wo.element
             )""",
        [project_name]
    )
    unblocked = 0
    for task in blocked:
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position FROM element_dependencies
               WHERE project_name=%s AND element=%s""",
            [project_name, task['element']]
        )
        all_done = True
        for dep in deps:
            pending = db.fetchone(
                """SELECT COUNT(*) as cnt FROM work_orders
                   WHERE project_name=%s AND sheet_name=%s AND element=%s
                     AND status != 'ВЫПОЛНЕНО'""",
                [project_name, dep['requires_sheet'], dep['requires_position']]
            )
            if pending and pending['cnt'] > 0:
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

if __name__ == '__main__':
    run()
