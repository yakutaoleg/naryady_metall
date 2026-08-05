import sys, os, time, uuid, traceback as _tb
from datetime import datetime as _dt

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
    # ISO формат yyyy-mm-dd
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s
    # Формат дд.мм.гггг
    parts = s.split('.')
    if len(parts) == 3:
        return f'{parts[2]}-{parts[1]}-{parts[0]}'
    return None

def _get(row: dict, *keys) -> str:
    """Первое непустое значение из row по списку ключей (для алиасов колонок)."""
    for k in keys:
        v = row.get(k, '')
        if v:
            return str(v).strip()
    return ''

def _do_auto_split(file_id: str, sheet_name: str, project_name: str, project_id: int,
                   row: dict, row_num: int, qty_done: int, remaining: int):
    """Разделяет строку с ЧАСТИЧНО+ВЫПОЛНЕНО вручную на ВЫПОЛНЕНО + ПЛАН(остаток)."""
    row_id = row.get('ROW_ID', '').strip() or None

    # Пропорциональная СУММА К ОПЛАТЕ
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

    # --- Обновляем БД: оригинал → ВЫПОЛНЕНО ---
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

    # --- Вставляем остаток в БД (phantom, синк почистит при следующем запуске) ---
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

    # --- Разделяем строку в Google Sheets ---
    # Формируем remainder_data в стандартных именах (insert_remainder_row сам применит алиасы)
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

    # Обновляем оригинальную строку в листе: статус → ВЫПОЛНЕНО, сумма → пропорция
    sheets.update_task_status(
        file_id=file_id, sheet_name=sheet_name, row_num=row_num,
        status='ВЫПОЛНЕНО', date_fact=today, qty_done=qty_done,
    )
    # Обновляем КОЛ-ВО оригинальной строки до qty_done (было total)
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
        # Стандартные имена + алиасы для ПОКРАСКА/ГРУНТОВКА ОСИ 11-15 и Щучин
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
    }

# UPSERT по row_id (если есть) — иначе fallback на (project_name, sheet_name, row_num)
UPSERT_SQL = '''
INSERT INTO work_orders (
    project_id, project_name, file_id, sheet_name, row_num, row_id,
    position, element, quantity, unit_weight, total_weight, payment_sum,
    executor, date_plan, priority, mandatory, status, comment, date_fact,
    drawing_link, qty_holes, updated_at
) VALUES (
    %(project_id)s, %(project_name)s, %(file_id)s, %(sheet_name)s, %(row_num)s, %(row_id)s,
    %(position)s, %(element)s, %(quantity)s, %(unit_weight)s, %(total_weight)s, %(payment_sum)s,
    %(executor)s, %(date_plan)s, %(priority)s, %(mandatory)s, %(status)s, %(comment)s, %(date_fact)s,
    %(drawing_link)s, %(qty_holes)s, NOW()
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
                    THEN 'ВЫПОЛНЕНО'   -- сплит: есть строка-остаток, лист ещё не обновился
                    ELSE EXCLUDED.status  -- одна строка = реальная правка, доверяем листу
                END,
    qty_done  = CASE WHEN EXCLUDED.status = 'ПЛАН' THEN NULL ELSE work_orders.qty_done END,
    comment   = EXCLUDED.comment,
    date_fact = EXCLUDED.date_fact,
    qty_holes = EXCLUDED.qty_holes
'''

def _apply_status_format_batch(file_id: str, sheet_name: str, rows: list):
    """Батч-форматирование всей колонки СТАТУС листа одним запросом."""
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

                # Форматируем колонку СТАТУС: цвет + жирный для всех строк листа
                try:
                    _apply_status_format_batch(file_id, sheet_name, rows)
                except Exception as _fmt_err:
                    logger.warning(f'  {sheet_name}: статус-форматирование пропущено: {_fmt_err}')

                # Стандарт колонок — раз в час на лист
                _std_key = (file_id, sheet_name)
                _now = _dt.utcnow().timestamp()
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
                    # Ловим ЧАСТИЧНО, а также любой статус где ВЫПОЛНЕНО заполнено частично
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
                    # Проверяем DB: если уже ВЫПОЛНЕНО — лист просто не успел обновиться
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
                        logger.info(f'  {sheet_name}: row {i+1} уже split в БД, пропускаем повторный сплит')
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

                # Удаляем призраки: placeholder-записи (row_num < 0) у которых
                # уже есть реальная запись с тем же row_id и позитивным row_num
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
                time.sleep(2)
            except Exception as e:
                if 'WorksheetNotFound' in type(e).__name__ or 'not found' in str(e).lower():
                    logger.info(f'  {sheet_name}: лист отсутствует, пропускаем')
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
            logger.alert(f'🔴 Sync ЗАВИСИМОСТИ ERROR\n{project_name}\n{e}')

    db.execute(
        "UPDATE work_orders SET mandatory=true WHERE status='ЧАСТИЧНО' AND (mandatory IS NULL OR mandatory=false)",
        []
    )
    logger.info(f'Sync done. Total: {total_synced} rows')
    return total_synced

if __name__ == '__main__':
    run()
