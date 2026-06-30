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
    files = sheets.find_active_files()
    logger.info(f'Found {len(files)} active file(s)')

    for f in files:
        file_id      = f['file_id']
        project_name = f['project_name']
        logger.info(f'Processing: {project_name} ({file_id})')

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
                logger.error(f'  {sheet_name}: ERROR — {e}')
                logger.audit(
                    action='sync_sheet',
                    details={'project': project_name, 'sheet': sheet_name},
                    result='error', error_msg=str(e)
                )

    # Синхронизация вкладки "Зависимости"
    for f in files:
        file_id      = f['file_id']
        project_name = f['project_name']
        try:
            rows = sheets.read_sheet(file_id, 'Зависимости')
            db.execute(
                "DELETE FROM element_dependencies WHERE project_name=%s",
                [project_name]
            )
            count = 0
            for row in rows:
                element   = row.get('ЭЛЕМЕНТ', '').strip()
                req_sheet = row.get('СПЕЦИАЛИЗАЦИЯ', '').strip()
                req_pos   = row.get('ПОЗИЦИИ', '').strip()
                if not element or not req_sheet or not req_pos:
                    continue
                for pos in [p.strip() for p in req_pos.split(',') if p.strip()]:
                    db.execute(
                        """INSERT INTO element_dependencies
                           (project_name, element, requires_sheet, requires_position)
                           VALUES (%s, %s, %s, %s)""",
                        [project_name, element, req_sheet, pos]
                    )
                    count += 1
            logger.info(f'  Зависимости: {count} записей')
        except Exception as e:
            logger.error(f'  Зависимости: ERROR — {e}')

    logger.info(f'Sync done. Total: {total_synced} rows')
    return total_synced

if __name__ == '__main__':
    run()
