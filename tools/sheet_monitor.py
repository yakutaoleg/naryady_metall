"""
sheet_monitor.py - мониторинг Google Sheets.
Запускается по cron каждые 15 минут (08:00-00:00).
Шлёт Telegram-алерт Олегу (340620064) только если есть нарушения.
Новые нарушения выделяются отдельно.
"""

import sys, os, json, hashlib, requests, time
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, '/root/naryady/prod')

from dotenv import load_dotenv
load_dotenv('/root/naryady/prod/.env')

import src.sheets as sh
import src.sheet_standards as std
from src.sheet_standards import DEFAULT_ROW_VALUES, ALLOWED_BG_COLORS
from src import db

TG_TOKEN    = os.environ['TG_TOKEN']
ADMIN_CHAT  = 340620064
FILE_ID     = '18xFgpV5QTXkNO-jVR4WCjgiHXVpuQ88KkiC9c62gzes'
STATE_FILE  = '/root/naryady/prod/logs/monitor_state.json'

# Минимальный порог зависимостей — если меньше, значит матчинг сломался
DEPS_MIN_THRESHOLD = 50

SHEETS_CONFIG = {
    'ПИЛА':      {'num_cols': {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ'}},
    'СВАРКА':    {'num_cols': {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ'}},
    'СВЕРЛЕНИЕ': {'num_cols': {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ', 'КОЛ-ВО ОТВЕРСТИЙ'}},
    'ПОКРАСКА':  {'num_cols': {'Кол-во', 'СУММА К ОПЛАТЕ'}},
    'ГРУНТОВКА': {'num_cols': {'Кол-во', 'СУММА К ОПЛАТЕ'}},
}

SKIP_FONT_CHECK = {'СТАТУС', 'ОБЯЗАТЕЛЬНАЯ', 'ИСПОЛНИТЕЛЬ', 'БЛОК'}
ALLOWED_BG = ALLOWED_BG_COLORS

# Следующая специализация по цепочке (для проверки колонки БЛОК)
NEXT_SPEC = {
    'ПЛАЗМА':    'СВЕРЛЕНИЕ',
    'ПИЛА':      'СВЕРЛЕНИЕ',
    'СВЕРЛЕНИЕ': 'СБОРКА',
    'СБОРКА':    'СВАРКА',
    'СВАРКА':    'ГРУНТОВКА',
    'ГРУНТОВКА': 'ПОКРАСКА',
}

# Категории для группировки — (ключ, метка)
CATEGORIES = [
    ('ВЫПОЛНЕНО_БЕЗ_ДАТЫ',       'ВЫПОЛНЕНО без ДАТА ФАКТ'),
    ('ВЫПОЛНЕНО_БЕЗ_ИСПОЛНИТЕЛЯ', 'ВЫПОЛНЕНО без ИСПОЛНИТЕЛЯ'),
    ('ПЛАН_С_ДАТОЙ',              'ПЛАН + ДАТА ФАКТ заполнена'),
    ('ДАТА_ПОРЯДОК',              'ДАТА ФАКТ раньше ДАТА ПЛАН'),
    ('КОЛ-ВО_НОЛЬ',              'КОЛ-ВО пустое или 0'),
    ('ШИРИНА',                    'Изменилась ширина колонки'),
    ('ШРИФТ_ЗАГОЛОВКА',           'Шрифт/размер заголовка изменён'),
    ('ФОН_ЗАГОЛОВКА',             'Фон заголовка изменён'),
    ('ДРОПДАУН',                  'Дропдаун отсутствует/повреждён'),
    ('ДАТА_КАЛЕНДАРЬ',             'Календарь на дата-колонке отсутствует'),
    ('ИТОГО_БЛОК',                'В строке ИТОГО заполнен БЛОК'),
    ('ОФОРМЛЕНИЕ',                'Нестандартное оформление строки'),
    ('ТИП_ДАННЫХ',                'Неверный тип данных в ячейке'),
    ('БЛОК_КОЛОНКА',              'Колонка БЛОК: неактуальная информация'),
    ('БЛОК_ЗАВИСАНИЕ',            'БЛОК-зависание: зависимость уже выполнена'),
    ('ЗАВИСИМОСТИ_ЗДОРОВЬЕ',      'Зависимости: сбой автогенерации'),
    ('ЧАСТИЧНО_БЕЗ_ВЫПОЛНЕНО',   'ЧАСТИЧНО без заполненного ВЫПОЛНЕНО'),
    ('СТАТУС_НЕ_ЗАПОЛНЕН',        'СТАТУС пустой у заполненной строки'),
    ('ОБЯЗАТЕЛЬНАЯ_НЕ_ЗАПОЛНЕНА', 'ОБЯЗАТЕЛЬНАЯ пустая у заполненной строки'),
    ('СОТРУДНИКИ_ФОРМУЛА',         'СОТРУДНИКИ: правая таблица без формул'),
]
CAT_KEYS = {k for k, _ in CATEGORIES}


def send_alert(text):
    requests.post(
        'https://api.telegram.org/bot' + TG_TOKEN + '/sendMessage',
        json={'chat_id': ADMIN_CHAT, 'text': text, 'parse_mode': 'HTML'},
        timeout=10,
    )


def fp(issue):
    return hashlib.md5(issue.encode()).hexdigest()


def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f).get('seen', []))
    except Exception:
        return set()


def save_state(seen_fps):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'seen': list(seen_fps), 'updated': datetime.now().isoformat()}, f)


def bg_hex(color):
    r = round(color.get('red',   1) * 255)
    g = round(color.get('green', 1) * 255)
    b = round(color.get('blue',  1) * 255)
    return '%02X%02X%02X' % (r, g, b)


def is_date(val):
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            datetime.strptime(val.strip(), fmt)
            return True
        except ValueError:
            pass
    return False


def parse_date(val):
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            pass
    return None


def is_number(val):
    try:
        float(val.replace(',', '.').replace('\xa0', '').replace(' ', ''))
        return True
    except ValueError:
        return False


def categorize(issue_text):
    """Возвращает ключ категории по тексту нарушения."""
    t = issue_text
    if 'ВЫПОЛНЕНО, нет ДАТА ФАКТ'   in t: return 'ВЫПОЛНЕНО_БЕЗ_ДАТЫ'
    if 'ВЫПОЛНЕНО, нет ИСПОЛНИТЕЛЬ'  in t: return 'ВЫПОЛНЕНО_БЕЗ_ИСПОЛНИТЕЛЯ'
    if 'ПЛАН + ДАТА ФАКТ'            in t: return 'ПЛАН_С_ДАТОЙ'
    if 'ДАТА ФАКТ (' in t and ') < ДАТА ПЛАН' in t: return 'ДАТА_ПОРЯДОК'
    if 'КОЛ-ВО пустое'              in t: return 'КОЛ-ВО_НОЛЬ'
    if t.startswith('ШИРИНА')        : return 'ШИРИНА'
    if 'ЗАГОЛОВКА' in t and ('шрифт' in t or 'размер' in t or 'жирн' in t): return 'ШРИФТ_ЗАГОЛОВКА'
    if 'ФОН ЗАГОЛОВКА'              in t: return 'ФОН_ЗАГОЛОВКА'
    if t.startswith('ДРОПДАУН')      : return 'ДРОПДАУН'
    if t.startswith('ДАТА_КАЛЕНДАРЬ'): return 'ДАТА_КАЛЕНДАРЬ'
    if 'ИТОГО' in t and 'БЛОК' in t  : return 'ИТОГО_БЛОК'
    if t.startswith('ОФОРМЛЕНИЕ')    : return 'ОФОРМЛЕНИЕ'
    if t.startswith('ТИП')           : return 'ТИП_ДАННЫХ'
    if t.startswith('БЛОК_КОЛОНКА')  : return 'БЛОК_КОЛОНКА'
    if t.startswith('БЛОК_ЗАВИСАНИЕ'): return 'БЛОК_ЗАВИСАНИЕ'
    if t.startswith('ЗАВИСИМОСТИ')   : return 'ЗАВИСИМОСТИ_ЗДОРОВЬЕ'
    if t.startswith('ЧАСТИЧНО')      : return 'ЧАСТИЧНО_БЕЗ_ВЫПОЛНЕНО'
    if t.startswith('СТАТУС_НЕ_ЗАПОЛНЕН')        : return 'СТАТУС_НЕ_ЗАПОЛНЕН'
    if t.startswith('ОБЯЗАТЕЛЬНАЯ_НЕ_ЗАПОЛНЕНА')   : return 'ОБЯЗАТЕЛЬНАЯ_НЕ_ЗАПОЛНЕНА'
    return 'ПРОЧЕЕ'


def short_ref(issue_text):
    """Выбирает короткую подпись: вкладка + строка + позиция."""
    import re
    m = re.search(r'(\w+) стр\.(\d+) «([^»]{1,25})', issue_text)
    if m:
        return m.group(1) + ' стр.' + m.group(2)
    m2 = re.search(r'\| (\w+) [«]([^»]+)', issue_text)
    if m2:
        return m2.group(1) + ' «' + m2.group(2)[:15] + '»'
    return issue_text[:40]


def check_sheet(sheet_name, cfg, file_id=None, project_name=""):
    issues = []
    gc = sh._gc()
    ss = gc.open_by_key(file_id or FILE_ID)

    for _attempt in range(5):
        try:
            resp = ss._spreadsheets_get({
                'fields': 'sheets(properties(title),data(columnMetadata,rowData(values(formattedValue,effectiveFormat,dataValidation))))',
                'ranges': sheet_name + '!1:200',
                'includeGridData': True,
            })
            break
        except Exception as _e:
            if '429' in str(_e) and _attempt < 4:
                wait = 60 + _attempt * 30
                print(f'[MONITOR] 429 на {sheet_name} (попытка {_attempt+1}/5), жду {wait}s', flush=True)
                time.sleep(wait)
            else:
                print(f'[MONITOR] Ошибка на {sheet_name}: {_e}', flush=True)
                raise
    sheet_data = next(x for x in resp['sheets'] if x['properties']['title'] == sheet_name)
    col_meta  = sheet_data['data'][0].get('columnMetadata', [])
    all_rows  = sheet_data['data'][0].get('rowData', [])

    if len(all_rows) < 3:
        return ['ПРОЧЕЕ | ' + sheet_name + ': не удалось прочитать данные']

    header_cells = all_rows[1].get('values', [])
    col_map = sh._col_map(file_id or FILE_ID, sheet_name)
    rev     = {v: k for k, v in col_map.items()}

    # Правило 6: ширины колонок
    for i, meta in enumerate(col_meta):
        col_name   = rev.get(i + 1, '')
        expected_w = std.COL_WIDTHS.get(col_name)
        if expected_w is None:
            continue
        actual_w = meta.get('pixelSize', 0)
        if actual_w != expected_w:
            issues.append('ШИРИНА | ' + sheet_name + ' «' + col_name + '»: ' + str(actual_w) + 'px (ожидается ' + str(expected_w) + 'px)')

    # Правило 6: форматирование заголовка
    for i, cell in enumerate(header_cells):
        col_name = rev.get(i + 1, '')
        if not col_name:
            continue
        fmt   = cell.get('effectiveFormat', {})
        tf    = fmt.get('textFormat', {})
        bghex = bg_hex(fmt.get('backgroundColor', {}))
        if tf.get('fontFamily') and tf.get('fontFamily') != 'Arial':
            issues.append('ШРИФТ_ЗАГОЛОВКА | ' + sheet_name + ' «' + col_name + '»: шрифт ' + tf['fontFamily'])
        if tf.get('fontSize') and tf.get('fontSize') != 9:
            issues.append('ШРИФТ_ЗАГОЛОВКА | ' + sheet_name + ' «' + col_name + '»: размер ' + str(tf['fontSize']) + 'pt')
        if not tf.get('bold'):
            issues.append('ШРИФТ_ЗАГОЛОВКА | ' + sheet_name + ' «' + col_name + '»: не жирный')
        if bghex.upper() not in ('EEEEEE', 'EFEFEF'):
            issues.append('ФОН_ЗАГОЛОВКА | ' + sheet_name + ' «' + col_name + '»: фон #' + bghex)

    # Правило 7: дропдауны
    first_data = all_rows[2].get('values', [])
    for col_name in ('СТАТУС', 'ИСПОЛНИТЕЛЬ'):
        col_num = col_map.get(col_name)
        if not col_num:
            continue
        i = col_num - 1
        if i >= len(first_data):
            issues.append('ДРОПДАУН | ' + sheet_name + ': нет ячейки «' + col_name + '»')
            continue
        dv   = first_data[i].get('dataValidation', {})
        cond = dv.get('condition', {})
        if not cond:
            issues.append('ДРОПДАУН | ' + sheet_name + ': отсутствует «' + col_name + '»')
        elif col_name == 'СТАТУС':
            vals    = {v.get('userEnteredValue', '') for v in cond.get('values', [])}
            missing = {'ПЛАН', 'ВЫПОЛНЕНО', 'БЛОК', 'ЧАСТИЧНО'} - vals
            if missing:
                issues.append('ДРОПДАУН | ' + sheet_name + ': СТАТУС не хватает ' + str(missing))

    # Правило 7б: календарь на дата-колонках
    for col_name in ('ДАТА ПЛАН', 'ДАТА ФАКТ'):
        col_num = col_map.get(col_name)
        if not col_num:
            continue
        i = col_num - 1
        if i >= len(first_data):
            issues.append('ДАТА_КАЛЕНДАРЬ | ' + sheet_name + ': нет ячейки «' + col_name + '»')
            continue
        dv   = first_data[i].get('dataValidation', {})
        cond = dv.get('condition', {})
        if not cond or cond.get('type') != 'DATE_IS_VALID':
            issues.append('ДАТА_КАЛЕНДАРЬ | ' + sheet_name + ': отсутствует календарь «' + col_name + '»')

    # Строка ИТОГО
    itogo_idx = None
    for idx, row in enumerate(all_rows):
        if any(c.get('formattedValue', '') == 'ИТОГО' for c in row.get('values', [])):
            itogo_idx = idx
            break

    # Правило 8
    blok_col = col_map.get('БЛОК')
    if itogo_idx is not None and blok_col:
        itogo_cells = all_rows[itogo_idx].get('values', [])
        if blok_col - 1 < len(itogo_cells):
            blok_val = itogo_cells[blok_col - 1].get('formattedValue', '')
            if blok_val:
                issues.append('ИТОГО_БЛОК | ' + sheet_name + ': «' + blok_val + '»')

    # Строки данных
    data_end     = itogo_idx if itogo_idx else len(all_rows)
    status_col   = col_map.get('СТАТУС')
    datefact_col = col_map.get('ДАТА ФАКТ')
    dateplan_col = col_map.get('ДАТА ПЛАН')
    exec_col     = col_map.get('ИСПОЛНИТЕЛЬ')
    qty_col      = col_map.get('КОЛ-ВО') or col_map.get('Кол-во')
    pos_col      = col_map.get('ПОЗ. СОГЛАСНО ЧЕРТЕЖА') or col_map.get('Марка')

    for idx in range(2, data_end):
        cells     = all_rows[idx].get('values', [])
        sheet_row = idx + 1

        def cv(cn):
            if cn and cn - 1 < len(cells):
                return cells[cn - 1].get('formattedValue', '').strip()
            return ''

        pos       = cv(pos_col)
        status    = cv(status_col)
        date_fact = cv(datefact_col)
        date_plan = cv(dateplan_col)
        executor  = cv(exec_col)
        qty       = cv(qty_col)

        if not pos and not status:
            continue

        proj_prefix = ('[' + project_name + '] ') if project_name else ''
        ref = proj_prefix + sheet_name + ' стр.' + str(sheet_row) + ' «' + (pos[:20] or '?') + '»'

        if status == 'ПЛАН' and date_fact:
            issues.append('ПЛАН_С_ДАТОЙ | ' + ref + ': ПЛАН + ДАТА ФАКТ=' + date_fact)
        if status == 'ВЫПОЛНЕНО' and not date_fact:
            issues.append('ВЫПОЛНЕНО_БЕЗ_ДАТЫ | ' + ref + ': ВЫПОЛНЕНО, нет ДАТА ФАКТ')
        if status == 'ВЫПОЛНЕНО' and not executor:
            issues.append('ВЫПОЛНЕНО_БЕЗ_ИСПОЛНИТЕЛЯ | ' + ref + ': ВЫПОЛНЕНО, нет ИСПОЛНИТЕЛЬ')
        if date_fact and date_plan:
            df = parse_date(date_fact)
            dp = parse_date(date_plan)
            if df and dp and df < dp:
                issues.append('ДАТА_ПОРЯДОК | ' + ref + ': ДАТА ФАКТ (' + date_fact + ') < ДАТА ПЛАН (' + date_plan + ')')
        if pos and pos != 'ИТОГО' and (not qty or qty in ('0', '0.000', '0,000')):
            issues.append('КОЛ-ВО_НОЛЬ | ' + ref + ': КОЛ-ВО пустое или 0')

        # Проверка: строка с данными — СТАТУС и ОБЯЗАТЕЛЬНАЯ не должны быть пустыми
        # Условие: pos заполнен (≠ИТОГО) ИЛИ status заполнен — строка считается рабочей
        obyz_col = col_map.get('ОБЯЗАТЕЛЬНАЯ')
        is_data_row = (pos and pos != 'ИТОГО') or bool(status)
        if is_data_row:
            if not status:
                issues.append('СТАТУС_НЕ_ЗАПОЛНЕН | ' + ref + ': позиция заполнена, СТАТУС пуст (должен быть ПЛАН)')
            if obyz_col:
                obyz = cv(obyz_col)
                if not obyz:
                    issues.append('ОБЯЗАТЕЛЬНАЯ_НЕ_ЗАПОЛНЕНА | ' + ref + ': позиция заполнена, ОБЯЗАТЕЛЬНАЯ пуста (должна быть НЕТ)')

        for ci, cell in enumerate(cells):
            col_name = rev.get(ci + 1, '')
            if not col_name or col_name == 'ROW_ID' or col_name in SKIP_FONT_CHECK:
                continue
            fmt   = cell.get('effectiveFormat', {})
            tf    = fmt.get('textFormat', {})
            bghex = bg_hex(fmt.get('backgroundColor', {}))
            font  = tf.get('fontFamily', '')
            size  = tf.get('fontSize', 0)
            if font and font != 'Arial':
                issues.append('ОФОРМЛЕНИЕ | ' + ref + ' «' + col_name + '»: шрифт ' + font)
            if size and (size < 7 or size > 12):
                issues.append('ОФОРМЛЕНИЕ | ' + ref + ' «' + col_name + '»: размер ' + str(size) + 'pt')
            if bghex.upper() not in ALLOWED_BG:
                issues.append('ОФОРМЛЕНИЕ | ' + ref + ' «' + col_name + '»: фон #' + bghex)

        for cn in cfg['num_cols']:
            val = cv(col_map.get(cn))
            if val and not is_number(val):
                issues.append('ТИП_ДАННЫХ | ' + ref + ' «' + cn + '»: ожидается число, получено «' + val + '»')
        for cn in ('ДАТА ПЛАН', 'ДАТА ФАКТ'):
            val = cv(col_map.get(cn))
            if val and not is_date(val):
                issues.append('ТИП_ДАННЫХ | ' + ref + ' «' + cn + '»: ожидается дата, получено «' + val + '»')

        # Правило: ЧАСТИЧНО без ВЫПОЛНЕНО
        vyp_col = col_map.get('ВЫПОЛНЕНО')
        vyp_val = cv(vyp_col) if vyp_col else ''
        if status == 'ЧАСТИЧНО' and (not vyp_val or vyp_val in ('0', '0.000', '0,000')):
            issues.append('ЧАСТИЧНО_БЕЗ_ВЫПОЛНЕНО | ' + ref + ': ЧАСТИЧНО, ВЫПОЛНЕНО не заполнено')

        # Правило B: проверяем колонку БЛОК
        # БЛОК управляется зависимостями (не NEXT_SPEC), ВЫПОЛНЕНО может дублировать значение ПЛАН
        blok_col = col_map.get('БЛОК')
        blok_val = cv(blok_col) if blok_col else ''

        # Единственный реальный признак ошибки: в ИТОГО-строке есть БЛОК-значение
        # (проверяется отдельным правилом ИТОГО_БЛОК выше)
        # Остальные правила по БЛОК-колонке сняты — логика теперь dependency-based

    return issues


def check_sotrudniki_formulas(project_name: str, file_id: str) -> list:
    """Проверяет что в листе СОТРУДНИКИ правая таблица (G4:M4) содержит формулы, а не hardcoded данные."""
    issues = []
    try:
        gc = sh._gc()
        ss = gc.open_by_key(file_id)
        sheet_titles = [ws.title for ws in ss.worksheets()]
        if 'СОТРУДНИКИ' not in sheet_titles:
            return []  # листа нет — это другая проверка
        ws = ss.worksheet('СОТРУДНИКИ')
        # Читаем заголовки (строка 3) и формулы (строка 4)
        result = ss.values_get(
            'СОТРУДНИКИ!G3:M4',
            params={'valueRenderOption': 'FORMULA'}
        )
        rows = result.get('values', [])
        if len(rows) < 2:
            issues.append('СОТРУДНИКИ_ФОРМУЛА | [' + project_name + '] СОТРУДНИКИ: правая таблица не найдена (G3:M4 пусто)')
            return issues
        spec_headers = rows[0] if rows else []
        formula_row  = rows[1] if len(rows) > 1 else []
        for i, spec in enumerate(spec_headers):
            if not spec.strip():
                continue
            cell_val = formula_row[i] if i < len(formula_row) else ''
            col_letter = chr(ord('G') + i)
            if not cell_val:
                issues.append('СОТРУДНИКИ_ФОРМУЛА | [' + project_name + '] СОТРУДНИКИ: ' + col_letter + '4 пустая (спец «' + spec + '»)')
            elif not str(cell_val).startswith('='):
                issues.append('СОТРУДНИКИ_ФОРМУЛА | [' + project_name + '] СОТРУДНИКИ: ' + col_letter + '4 содержит данные вместо формулы (спец «' + spec + '»): ' + str(cell_val)[:40])
            elif '"' + spec + '"' in str(cell_val) or "'" + spec + "'" in str(cell_val):
                # Hardcoded название специализации прямо в формуле (не ссылка на ячейку)
                issues.append('СОТРУДНИКИ_ФОРМУЛА | [' + project_name + '] СОТРУДНИКИ: ' + col_letter + '4 формула содержит hardcoded «' + spec + '» вместо ссылки на ' + col_letter + '3')
    except Exception as e:
        issues.append('СОТРУДНИКИ_ФОРМУЛА | [' + project_name + '] Ошибка проверки СОТРУДНИКИ: ' + str(e))
    return issues



def check_dep_hangups(project_name: str) -> list:
    """Правило A: задача в БЛОК, но все зависимости уже ВЫПОЛНЕНЫ → зависание."""
    issues = []
    try:
        blocked = db.fetchall(
            """SELECT wo.element, wo.sheet_name, wo.row_num
               FROM work_orders wo
               WHERE wo.project_name=%s AND wo.status='БЛОК'
                 AND wo.element IS NOT NULL
                 AND EXISTS (
                   SELECT 1 FROM element_dependencies ed
                   WHERE ed.project_name=wo.project_name AND ed.element=wo.element
                 )""",
            [project_name]
        )
        for task in blocked:
            deps = db.fetchall(
                """SELECT requires_sheet, requires_position FROM element_dependencies
                   WHERE project_name=%s AND element=%s""",
                [project_name, task['element']]
            )
            all_done = all(
                (db.fetchone(
                    """SELECT COUNT(*) as cnt FROM work_orders
                       WHERE project_name=%s AND sheet_name=%s AND element=%s AND status != 'ВЫПОЛНЕНО'""",
                    [project_name, d['requires_sheet'], d['requires_position']]
                ) or {}).get('cnt', 1) == 0
                for d in deps
            ) if deps else False
            if all_done:
                ref = '[' + project_name + '] ' + task['sheet_name'] + ' стр.' + str(task['row_num']) + ' «' + task['element'] + '»'
                issues.append('БЛОК_ЗАВИСАНИЕ | ' + ref + ': в БЛОК, но зависимости ВЫПОЛНЕНО')
    except Exception as e:
        issues.append('БЛОК_ЗАВИСАНИЕ | Ошибка проверки: ' + str(e))
    return issues


def check_deps_health(project_name: str) -> list:
    """Проверяет что автогенерация зависимостей сработала корректно."""
    issues = []
    try:
        row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM element_dependencies WHERE project_name=%s",
            [project_name]
        )
        cnt = row['cnt'] if row else 0
        if cnt == 0:
            issues.append('ЗАВИСИМОСТИ_ЗДОРОВЬЕ | ' + project_name + ': 0 записей — автогенерация не сработала')
        elif cnt < DEPS_MIN_THRESHOLD:
            issues.append(
                'ЗАВИСИМОСТИ_ЗДОРОВЬЕ | ' + project_name +
                ': только ' + str(cnt) + ' записей (ожидается >=' + str(DEPS_MIN_THRESHOLD) + ') — возможен сбой матчинга'
            )
    except Exception as e:
        issues.append('ЗАВИСИМОСТИ_ЗДОРОВЬЕ | Ошибка проверки: ' + str(e))
    return issues


def format_alert(all_issues, new_fps, seen_fps, project_name=""):
    now        = datetime.now().strftime('%d.%m.%Y %H:%M')
    new_issues = [i for i in all_issues if fp(i) in new_fps]

    lines = []
    if project_name:
        lines.append('<b>' + project_name + '</b>')
    lines.append('<b>Мониторинг листов — ' + now + '</b>')
    lines.append('Всего нарушений: <b>' + str(len(all_issues)) + '</b> | Новых: <b>' + str(len(new_issues)) + '</b>')

    by_cat = defaultdict(list)
    for i in all_issues:
        cat = categorize(i)
        ref = short_ref(i)
        is_new = fp(i) in new_fps
        by_cat[cat].append((ref, is_new))

    for cat_key, label in CATEGORIES:
        items = by_cat.get(cat_key, [])
        if not items:
            continue
        lines.append('')
        lines.append('<b>' + label + '</b> — ' + str(len(items)) + ' шт.')
        by_sheet = defaultdict(list)
        for ref, is_new in items:
            sheet = ref.split(' ')[0]
            row_part = ref[len(sheet):].strip()
            marker = ' ⚠️' if is_new else ''
            by_sheet[sheet].append(row_part + marker)
        for sheet, rows in by_sheet.items():
            preview = ', '.join(rows[:4])
            suffix  = (' ... +' + str(len(rows) - 4) + ' ещё') if len(rows) > 4 else ''
            lines.append('  ' + sheet + ': ' + preview + suffix)

    if 'ПРОЧЕЕ' in by_cat:
        items = by_cat['ПРОЧЕЕ']
        lines.append('')
        lines.append('<b>Прочее</b> — ' + str(len(items)) + ' шт.')
        for ref, is_new in items[:5]:
            marker = ' ⚠️' if is_new else ''
            lines.append('  ' + ref + marker)

    return chr(10).join(lines)


def run_for_project(proj_name, file_id):
    """Проверяет один проект, возвращает список нарушений."""
    issues = []
    for sheet_name, cfg in SHEETS_CONFIG.items():
        try:
            issues.extend(check_sheet(sheet_name, cfg, file_id=file_id, project_name=proj_name))
        except Exception as e:
            issues.append('ПРОЧЕЕ | [' + proj_name + '] ' + sheet_name + ': ошибка — ' + str(e))
    try:
        issues.extend(check_sotrudniki_formulas(proj_name, file_id))
        issues.extend(check_dep_hangups(proj_name))
        issues.extend(check_deps_health(proj_name))
    except Exception as e:
        issues.append('ПРОЧЕЕ | [' + proj_name + '] зависания: ошибка — ' + str(e))
    return issues


def format_alert(all_issues_by_proj, new_fps, seen_fps):
    from datetime import datetime as _dt
    now = _dt.now().strftime('%d.%m.%Y %H:%M')
    total = sum(len(v) for v in all_issues_by_proj.values())
    all_flat = [i for v in all_issues_by_proj.values() for i in v]
    new_count = sum(1 for i in all_flat if fp(i) in new_fps)

    lines = ['<b>Мониторинг листов — ' + now + '</b>']
    lines.append('Всего нарушений: <b>' + str(total) + '</b> | Новых: <b>' + str(new_count) + '</b>')

    for proj_name, issues in all_issues_by_proj.items():
        if not issues:
            continue
        new_in_proj = sum(1 for i in issues if fp(i) in new_fps)
        lines.append('')
        lines.append('─' * 30)
        lines.append('📋 <b>' + proj_name + '</b> — ' + str(len(issues)) + ' шт.' +
                     (' (' + str(new_in_proj) + ' новых)' if new_in_proj else ''))

        by_cat = defaultdict(list)
        for i in issues:
            cat = categorize(i)
            # ПРОЧЕЕ: полный текст ошибки; остальные — короткий ref
            ref = i if cat == 'ПРОЧЕЕ' else short_ref(i)
            is_new = fp(i) in new_fps
            by_cat[cat].append((ref, is_new, i))  # i = оригинальный текст

        for cat_key, label in CATEGORIES:
            items = by_cat.get(cat_key, [])
            if not items:
                continue
            lines.append('')
            lines.append('<b>' + label + '</b> — ' + str(len(items)) + ' шт.')
            if cat_key == 'ОФОРМЛЕНИЕ':
                # Группируем по (лист, строка) — одна строка может дать шрифт+размер+фон
                import re as _re_oform
                by_row = defaultdict(lambda: {'violations': [], 'is_new': False})
                for ref, is_new, orig in items:
                    m = _re_oform.search(r'(\w+) стр\.(\d+)', ref)
                    if m:
                        key = (m.group(1), 'стр.' + m.group(2))
                        # Что именно не так — после »: в оригинальном тексте
                        hint_m = _re_oform.search(r'»:\s*(.{1,20})$', orig)
                        hint = hint_m.group(1).strip() if hint_m else ''
                        if hint and hint not in by_row[key]['violations']:
                            by_row[key]['violations'].append(hint)
                        if is_new:
                            by_row[key]['is_new'] = True
                by_sheet2 = defaultdict(list)
                for (sheet, row_str), info in by_row.items():
                    marker = ' ⚠️' if info['is_new'] else ''
                    v = ', '.join(info['violations'])
                    entry = row_str + (' (' + v + ')' if v else '') + marker
                    by_sheet2[sheet].append(entry)
                for sheet, rows in by_sheet2.items():
                    preview = ', '.join(rows[:4])
                    suffix  = (' ... +' + str(len(rows) - 4) + ' ещё') if len(rows) > 4 else ''
                    lines.append('  ' + sheet + ': ' + preview + suffix)
            else:
                by_sheet = defaultdict(list)
                for ref, is_new, _orig in items:
                    sheet = ref.split(' ')[0] if ' ' in ref else ref
                    row_part = ref[len(sheet):].strip()
                    marker = ' ⚠️' if is_new else ''
                    by_sheet[sheet].append(row_part + marker)
                for sheet, rows in by_sheet.items():
                    preview = ', '.join(rows[:4])
                    suffix  = (' ... +' + str(len(rows) - 4) + ' ещё') if len(rows) > 4 else ''
                    lines.append('  ' + sheet + ': ' + preview + suffix)

        if 'ПРОЧЕЕ' in by_cat:
            items = by_cat['ПРОЧЕЕ']
            lines.append('')
            lines.append('<b>Прочее</b> — ' + str(len(items)) + ' шт.')
            for ref, is_new, _orig in items[:5]:
                marker = ' ⚠️' if is_new else ''
                lines.append('  ' + ref + marker)

    return chr(10).join(lines)


def main():
    # Загружаем все активные проекты
    try:
        projects = db.fetchall("SELECT project_name, sheet_id FROM projects ORDER BY project_name")
    except Exception as e:
        print('Ошибка загрузки проектов: ' + str(e))
        return

    all_issues_by_proj = {}
    all_flat = []
    for proj in projects:
        proj_name = proj["project_name"]
        file_id   = proj["sheet_id"]
        issues = run_for_project(proj_name, file_id)
        all_issues_by_proj[proj_name] = issues
        all_flat.extend(issues)

    if not all_flat:
        save_state(set())
        print('Нарушений нет.')
        return

    seen_fps    = load_state()
    current_fps = {fp(i) for i in all_flat}
    new_fps     = current_fps - seen_fps

    text = format_alert(all_issues_by_proj, new_fps, seen_fps)
    send_alert(text)
    save_state(current_fps)
    print('Алерт отправлен: всего=' + str(len(all_flat)) + ', новых=' + str(len(new_fps)))

    # Запись результата в БД
    try:
        issues_data = json.dumps([
            {'project': p, 'category': categorize(i), 'text': i, 'ref': short_ref(i)}
            for p, lst in all_issues_by_proj.items() for i in lst
        ], ensure_ascii=False)
        db.execute(
            "INSERT INTO monitor_runs (project_name, total_issues, new_issues, alert_sent, issues) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            ['ALL', len(all_flat), len(new_fps), True, issues_data]
        )
    except Exception as e:
        print('БД: ошибка записи — ' + str(e))


if __name__ == '__main__':
    main()
