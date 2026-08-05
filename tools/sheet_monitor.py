"""
sheet_monitor.py - мониторинг Google Sheets.
Запускается по cron каждые 15 минут (08:00-00:00).
Шлёт Telegram-алерт Олегу (340620064) только если есть нарушения.
Новые нарушения выделяются отдельно.
"""

import sys, os, json, hashlib, requests
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, '/root/naryady/prod')

from dotenv import load_dotenv
load_dotenv('/root/naryady/prod/.env')

import src.sheets as sh
import src.sheet_standards as std

TG_TOKEN    = os.environ['TG_TOKEN']
ADMIN_CHAT  = 340620064
FILE_ID     = '18xFgpV5QTXkNO-jVR4WCjgiHXVpuQ88KkiC9c62gzes'
STATE_FILE  = '/root/naryady/prod/logs/monitor_state.json'

SHEETS_CONFIG = {
    'ПИЛА':      {'num_cols': {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ'}},
    'СВАРКА':    {'num_cols': {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ'}},
    'СВЕРЛЕНИЕ': {'num_cols': {'КОЛ-ВО', 'МАССА ЕД. (кг)', 'МАССА ВСЕХ (кг)', 'СУММА К ОПЛАТЕ', 'КОЛ-ВО ОТВЕРСТИЙ'}},
    'ПОКРАСКА':  {'num_cols': {'Кол-во', 'СУММА К ОПЛАТЕ'}},
    'ГРУНТОВКА': {'num_cols': {'Кол-во', 'СУММА К ОПЛАТЕ'}},
}

SKIP_FONT_CHECK = {'СТАТУС', 'ОБЯЗАТЕЛЬНАЯ', 'ИСПОЛНИТЕЛЬ', 'БЛОК'}
ALLOWED_BG = {'FFFFFF', 'FFF9C4', 'E8F5E9', 'FFE0B2', 'FFEBEE', 'EEEEEE', 'EFEFEF'}

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
    ('ИТОГО_БЛОК',                'В строке ИТОГО заполнен БЛОК'),
    ('ОФОРМЛЕНИЕ',                'Нестандартное оформление строки'),
    ('ТИП_ДАННЫХ',                'Неверный тип данных в ячейке'),
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
    r = int(color.get('red',   1) * 255)
    g = int(color.get('green', 1) * 255)
    b = int(color.get('blue',  1) * 255)
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
    if 'ИТОГО' in t and 'БЛОК' in t  : return 'ИТОГО_БЛОК'
    if t.startswith('ОФОРМЛЕНИЕ')    : return 'ОФОРМЛЕНИЕ'
    if t.startswith('ТИП')           : return 'ТИП_ДАННЫХ'
    return 'ПРОЧЕЕ'


def short_ref(issue_text):
    """Выбирает короткую подпись: вкладка + строка + позиция."""
    # Ищем паттерн 'ЛИСТ стр.N «позиция»'
    import re
    m = re.search(r'(\w+) стр\.(\d+) «([^»]{1,25})', issue_text)
    if m:
        return m.group(1) + ' стр.' + m.group(2)
    # Для нарушений заголовка/ширины
    m2 = re.search(r'\| (\w+) [«]([^»]+)', issue_text)
    if m2:
        return m2.group(1) + ' «' + m2.group(2)[:15] + '»'
    return issue_text[:40]


def check_sheet(sheet_name, cfg):
    issues = []
    gc = sh._gc()
    ss = gc.open_by_key(FILE_ID)

    resp = ss._spreadsheets_get({
        'fields': 'sheets(properties(title),data(columnMetadata,rowData(values(formattedValue,effectiveFormat,dataValidation))))',
        'ranges': sheet_name + '!1:200',
        'includeGridData': True,
    })
    sheet_data = next(x for x in resp['sheets'] if x['properties']['title'] == sheet_name)
    col_meta  = sheet_data['data'][0].get('columnMetadata', [])
    all_rows  = sheet_data['data'][0].get('rowData', [])

    if len(all_rows) < 3:
        return ['ПРОЧЕЕ | ' + sheet_name + ': не удалось прочитать данные']

    header_cells = all_rows[1].get('values', [])
    col_map = sh._col_map(FILE_ID, sheet_name)
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

        ref = sheet_name + ' стр.' + str(sheet_row) + ' «' + (pos[:20] or '?') + '»'

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

    return issues


def format_alert(all_issues, new_fps, seen_fps):
    now   = datetime.now().strftime('%d.%m.%Y %H:%M')
    new_issues  = [i for i in all_issues if fp(i) in new_fps]
    old_issues  = [i for i in all_issues if fp(i) not in new_fps]

    lines = ['<b>Мониторинг листов — ' + now + '</b>']
    lines.append('Всего нарушений: <b>' + str(len(all_issues)) + '</b> | Новых: <b>' + str(len(new_issues)) + '</b>')

    if new_issues:
        lines.append('')
        lines.append('<b>НОВЫЕ:</b>')
        for i in new_issues[:15]:
            # убираем технический префикс категории
            text = i.split(' | ', 1)[-1] if ' | ' in i else i
            lines.append('  • ' + text)
        if len(new_issues) > 15:
            lines.append('  ...ещё ' + str(len(new_issues) - 15))

    if old_issues:
        lines.append('')
        lines.append('<b>ВСЕ НАРУШЕНИЯ ПО ВИДАМ:</b>')

        # Группируем по категории
        by_cat = defaultdict(list)
        for i in all_issues:
            cat = categorize(i)
            ref = short_ref(i)
            by_cat[cat].append(ref)

        cat_label = {k: lbl for k, lbl in CATEGORIES}
        for cat_key, label in CATEGORIES:
            refs = by_cat.get(cat_key, [])
            if not refs:
                continue
            lines.append('')
            lines.append('<b>' + label + '</b> — ' + str(len(refs)) + ' шт.')
            # Группируем refs по вкладке
            by_sheet = defaultdict(list)
            for ref in refs:
                sheet = ref.split(' ')[0]
                row_part = ref[len(sheet):].strip()
                by_sheet[sheet].append(row_part)
            for sheet, rows in by_sheet.items():
                preview = ', '.join(rows[:3])
                suffix  = (' ... +' + str(len(rows) - 3) + ' ещё') if len(rows) > 3 else ''
                lines.append('  ' + sheet + ': ' + preview + suffix)

        if 'ПРОЧЕЕ' in by_cat:
            refs = by_cat['ПРОЧЕЕ']
            lines.append('')
            lines.append('<b>Прочее</b> — ' + str(len(refs)) + ' шт.')
            for ref in refs[:5]:
                lines.append('  ' + ref)

    return '\n'.join(lines)


def main():
    all_issues = []
    for sheet_name, cfg in SHEETS_CONFIG.items():
        try:
            issues = check_sheet(sheet_name, cfg)
            all_issues.extend(issues)
        except Exception as e:
            all_issues.append('ПРОЧЕЕ | ' + sheet_name + ': ошибка проверки — ' + str(e))

    if not all_issues:
        save_state(set())
        return

    seen_fps    = load_state()
    current_fps = {fp(i) for i in all_issues}
    new_fps     = current_fps - seen_fps

    # Шлём алерт всегда если есть нарушения (чтобы не терять из виду)
    # Но если нет новых — шлём только раз в день (в 08:00)
    hour = datetime.now().hour
    if new_fps or hour == 8:
        text = format_alert(all_issues, new_fps, seen_fps)
        send_alert(text)
        print('Алерт отправлен: всего=' + str(len(all_issues)) + ', новых=' + str(len(new_fps)))
    else:
        print('Нет новых нарушений, тихо.')

    save_state(current_fps)


if __name__ == '__main__':
    main()
