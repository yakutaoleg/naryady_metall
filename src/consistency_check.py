"""Ночная проверка согласованности: таблицы Google ↔ БД ↔ состояние сервисов.
Только чтение (кроме отправки отчёта в Telegram администратору). Запуск: systemd-таймер naryady-check-prod."""
import sys, time, html, subprocess, collections, datetime
sys.path.insert(0, '/root/naryady/prod')
from src import db, sheets, config, logger

RECHECK_DELAY = 420        # сек: подтверждение расхождений вторым чтением (синк идёт раз в 5 мин)
SAMPLES = 3                # примеров на категорию в отчёте


def num(x):
    try:
        return float(str(x).replace(',', '.').replace('\xa0', '').strip())
    except Exception:
        return None


def col(hdr, *names):
    for n in names:
        if n in hdr:
            return hdr[n]
    return None


def read_project(p):
    """→ ({лист: {row_id: row}}, без_row_id: {лист: n}) или исключение."""
    ss = sheets._ss(p['sheet_id'])
    titles = [w.title for w in sheets._api_call(ss.worksheets)]
    have = [s for s in config.WORK_SHEETS if s in titles]
    raw = sheets.read_all_sheets_batch(p['sheet_id'], have)
    out, no_id = {}, {}
    for s in have:
        vals = raw.get(s) or []
        if len(vals) < 2:
            continue
        hdr = {h.strip(): i for i, h in enumerate(vals[1]) if h.strip()}
        c = {k: col(hdr, *v) for k, v in {
            'id': ('ROW_ID',), 'pos': ('ПОЗ. СОГЛАСНО ЧЕРТЕЖА', 'Марка'), 'el': ('ЭЛЕМЕНТ',), 'q': ('КОЛ-ВО',),
            'st': ('СТАТУС',), 'ex': ('ИСПОЛНИТЕЛЬ',)}.items()}
        rows, miss = {}, 0
        for ri, r in enumerate(vals[2:], start=3):
            if not any(str(x).strip() for x in r):
                continue
            if 'ИТОГО' in ' '.join(str(x) for x in r[:3]).upper():
                continue
            g = lambda k: (r[c[k]].strip() if c[k] is not None and c[k] < len(r) else '')
            rid = g('id')
            if not rid:
                if g('pos') or g('el'):    # ROW_ID назначается только строкам с позицией/элементом
                    miss += 1
                continue
            rows[rid] = {'sheet_row': ri, 'name': g('pos') or g('el'), 'q': num(g('q')), 'st': g('st'), 'ex': g('ex')}
        out[s] = rows
        if miss:
            no_id[s] = miss
    return out, no_id


def compare(p, data, employees):
    """Расхождения таблица↔БД: список (категория, лист, описание)."""
    found = []
    for s, rows in data.items():
        dbr = db.fetchall(
            "SELECT row_id::text AS rid, position, element, quantity, status, executor FROM work_orders "
            "WHERE project_name=%s AND sheet_name=%s AND row_num > 0 AND row_id IS NOT NULL", [p['project_name'], s])
        dbm = {r['rid']: r for r in dbr}
        for rid in set(rows) - set(dbm):
            if rows[rid]['name']:
                found.append(('нет в БД', s, f"{rows[rid]['name']} (стр. {rows[rid]['sheet_row']})"))
        for rid in set(dbm) - set(rows):
            r = dbm[rid]
            found.append(('«призрак» в БД', s, f"{r['position'] or r['element']}"))
        for rid, sr in rows.items():
            d = dbm.get(rid)
            if not d:
                continue
            nm = sr['name']
            if sr['st'] and sr['st'] != d['status'] and not (sr['st'] == 'ПЛАН' and d['status'] == 'ВЫПОЛНЕНО'):
                found.append(('статус лист≠БД', s, f"{nm}: {sr['st']}≠{d['status']}"))
            if sr['q'] is not None and d['quantity'] is not None and abs(sr['q'] - float(d['quantity'])) > 1e-6:
                found.append(('количество лист≠БД', s, f"{nm}: {sr['q']}≠{d['quantity']}"))
            if (sr['ex'] or '') != (d['executor'] or ''):
                found.append(('исполнитель лист≠БД', s, f"{nm}: '{sr['ex']}'≠'{d['executor'] or ''}'"))
            if sr['ex'] and sr['ex'] not in employees:
                found.append(('исполнитель не из сотрудников', s, f"'{sr['ex']}'"))
    return found


def main():
    t_start = time.time()
    projects = db.fetchall("SELECT id, project_name, sheet_id FROM projects WHERE status='АКТИВНЫЙ' ORDER BY id", [])
    employees = {r['full_name'] for r in db.fetchall("SELECT full_name FROM employees", [])}
    critical = collections.defaultdict(list)    # категория → [(проект/лист, текст)]
    warning = collections.defaultdict(list)
    total_sheet_rows = total_db_rows = 0
    unreachable = []
    candidates = {}                              # id проекта → набор расхождений первого чтения

    def scan(p):
        data, no_id = read_project(p)
        return data, no_id, compare(p, data, employees)

    for p in projects:
        try:
            data, no_id, found = scan(p)
        except Exception as e:
            unreachable.append((p, str(e)[:90]))
            continue
        total_sheet_rows += sum(len(v) for v in data.values())
        for s, n in no_id.items():
            warning['строки в таблице без ROW_ID (ручные)'].append((f"{p['project_name']} / {s}", f'{n} шт'))
        if found:
            candidates[p['id']] = (p, set(found))
        time.sleep(4)

    # подтверждение: расхождения должны сохраниться при втором чтении (иначе это просто недосинканная правка)
    persistent = []
    if candidates:
        time.sleep(RECHECK_DELAY)
        for pid, (p, first) in candidates.items():
            try:
                _, _, second = scan(p)
            except Exception as e:
                unreachable.append((p, 'повторное чтение: ' + str(e)[:70]))
                continue
            for cat, s, txt in sorted(first & set(second)):
                persistent.append((cat, p['project_name'], s, txt))
            time.sleep(4)
    for cat, pn, s, txt in persistent:
        (warning if cat == 'исполнитель не из сотрудников' else critical)[cat].append((f'{pn} / {s}', txt))

    for p, err in unreachable:
        critical['проект недоступен / ошибка чтения'].append((p['project_name'], err))

    total_db_rows = db.fetchone("SELECT COUNT(*) c FROM work_orders WHERE row_num > 0", [])['c']

    # ── проверки только по БД ─────────────────────────────────────────────────────
    for r in db.fetchall(
            "SELECT project_name, sheet_name, COALESCE(position, element) AS n FROM work_orders "
            "WHERE status='ВЫПОЛНЕНО' AND date_fact IS NULL AND date_plan >= CURRENT_DATE - 7 AND project_name IN (SELECT project_name FROM projects WHERE status='АКТИВНЫЙ')", []):
        warning['выполнено без даты факта (план за 7 дней)'].append((f"{r['project_name']} / {r['sheet_name']}", r['n']))
    for r in db.fetchall(
            "SELECT project_name, sheet_name, COALESCE(position, element) AS n FROM work_orders "
            "WHERE status='ВЫПОЛНЕНО' AND (executor IS NULL OR executor='') AND date_plan >= CURRENT_DATE - 7 AND project_name IN (SELECT project_name FROM projects WHERE status='АКТИВНЫЙ')", []):
        warning['выполнено без исполнителя (план за 7 дней)'].append((f"{r['project_name']} / {r['sheet_name']}", r['n']))
    for r in db.fetchall(
            "SELECT project_name, sheet_name, COALESCE(position, element) AS n FROM work_orders "
            "WHERE row_num <= 0 AND updated_at < now() - interval '1 hour' AND project_name IN (SELECT project_name FROM projects WHERE status='АКТИВНЫЙ')", []):
        warning['временные строки-остатки старше часа'].append((f"{r['project_name']} / {r['sheet_name']}", r['n']))
    try:
        from src import sync as _sync
        _act = {x['project_name'] for x in db.fetchall("SELECT project_name FROM projects WHERE status='АКТИВНЫЙ'", [])}
        for r in [x for x in _sync._repair_orphan_partials(dry_run=True) if x['project_name'] in _act]:
            critical['оборванное частичное выполнение'].append((f"{r['project_name']} / {r['sheet_name']}", f"{r['element'] or r['position']}"))
    except Exception as e:
        warning['проверка частичных выполнений не выполнена'].append(('', str(e)[:80]))
    for r in db.fetchall(
            "SELECT project_name, last_synced_at FROM projects WHERE status='АКТИВНЫЙ' "
            "AND (last_synced_at IS NULL OR last_synced_at < now() - interval '45 minutes')", []):
        critical['синк давно не обновлял проект (>45 мин)'].append((r['project_name'], str(r['last_synced_at'])[:16]))

    # ── сервисы ───────────────────────────────────────────────────────────────────
    sync_failed = subprocess.run(['systemctl', 'is-failed', '--quiet', 'naryady-sync-prod.service']).returncode == 0
    if sync_failed:
        warning['сервис синка в состоянии failed'].append(('naryady-sync-prod', 'последний запуск завершился ошибкой/таймаутом'))
    jr = subprocess.run(['journalctl', '-u', 'naryady-bot-prod', '--since', '24 hours ago', '--no-pager'],
                        capture_output=True, text=True).stdout
    crashes = sum(1 for ln in jr.splitlines() if 'status=11/SEGV' in ln or 'status=6/ABRT' in ln)
    if crashes:
        critical['падения процесса бота за сутки'].append(('naryady-bot-prod', f'{crashes} шт (см. logs/faulthandler.log)'))

    # ── отчёт ─────────────────────────────────────────────────────────────────────
    now = datetime.datetime.now().strftime('%d.%m %H:%M')
    lines = [f"🔎 <b>Проверка данных {now}</b>",
             f"Проектов: {len(projects)} | строк в таблицах: {total_sheet_rows} | в БД: {total_db_rows}"]

    def block(title, d):
        if not d:
            return
        lines.append(title)
        for cat, items in sorted(d.items(), key=lambda kv: -len(kv[1])):
            ex = '; '.join(html.escape(f'{w}: {t}' if t else w, quote=False) for w, t in items[:SAMPLES])
            lines.append(f" • {html.escape(cat)}: <b>{len(items)}</b>" + (f"\n    {ex}" if ex else ''))

    block("❗ <b>Требует внимания:</b>", critical)
    block("⚠️ <b>Замечания:</b>", warning)
    if not critical and not warning:
        lines.append("✅ Расхождений не найдено")
    lines.append(f"⏱ {round(time.time() - t_start)} с")
    text = '\n'.join(lines)[:3900]
    print(text.replace('<b>', '').replace('</b>', ''))
    logger._alert_http(text)


if __name__ == '__main__':
    main()
