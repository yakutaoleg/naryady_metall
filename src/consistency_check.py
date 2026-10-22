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


def check_components(warning, active_names):
    """Проверка заполнения количества деталей на сборку (лист ЗАВИСИМОСТИ, колонки F/G) для проектов, где оно есть."""
    for pr in db.fetchall("SELECT DISTINCT project_name FROM element_dependencies WHERE part IS NOT NULL", []):
        pn = pr['project_name']
        if pn not in active_names:
            continue
        # 1) деталь из зависимостей отсутствует в листе-источнике
        miss = db.fetchall(
            "SELECT DISTINCT ed.requires_sheet, ed.part FROM element_dependencies ed WHERE ed.project_name=%s AND ed.part IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM work_orders w WHERE w.project_name=ed.project_name AND w.sheet_name=ed.requires_sheet "
            "AND lower(trim(w.position))=lower(trim(ed.part)))", [pn])
        for r in miss:
            warning['деталь из зависимостей не найдена в листе'].append((pn, f"{r['requires_sheet']}: {r['part']}"))
        # 2) сборки без количества деталей
        nop = db.fetchall(
            "SELECT DISTINCT w.element FROM work_orders w WHERE w.project_name=%s AND w.sheet_name='СБОРКА' AND w.row_num>0 "
            "AND w.element IS NOT NULL AND upper(w.element)<>'ИТОГО' AND NOT EXISTS (SELECT 1 FROM element_dependencies ed "
            "WHERE ed.project_name=w.project_name AND ed.waiting_sheet='СБОРКА' AND ed.element=w.element AND ed.part IS NOT NULL)", [pn])
        if nop:
            warning['у сборки нет количества деталей'].append((pn, ', '.join(r['element'] for r in nop[:6]) + (f' … всего {len(nop)}' if len(nop) > 6 else '')))
        # 3) дубли строк (деталь указана дважды для одной сборки)
        dup = db.fetchall(
            "SELECT element, requires_sheet, part, count(*) c FROM element_dependencies WHERE project_name=%s AND part IS NOT NULL "
            "GROUP BY 1,2,3 HAVING count(*) > 1", [pn])
        for r in dup:
            warning['дубли строк с деталью в зависимостях'].append((pn, f"{r['element']}: {r['part']} ×{r['c']}"))
        # 4) нужно сборкам больше, чем заложено в листе деталей («готовый элемент» избытком не считаем)
        need = db.fetchall(
            "SELECT ed.requires_sheet rs, lower(trim(ed.part)) p, SUM(ed.qty_per * COALESCE(a.q, 0)) need "
            "FROM element_dependencies ed LEFT JOIN (SELECT element, SUM(quantity) q FROM work_orders "
            "WHERE project_name=%s AND sheet_name='СБОРКА' AND row_num>0 GROUP BY element) a ON a.element=ed.element "
            "WHERE ed.project_name=%s AND ed.waiting_sheet='СБОРКА' AND ed.part IS NOT NULL GROUP BY 1,2", [pn, pn])
        for r in need:
            h = db.fetchone(
                "SELECT COALESCE(SUM(quantity),0) have, COALESCE(SUM(quantity) FILTER (WHERE status='ВЫПОЛНЕНО'),0) done "
                "FROM work_orders WHERE project_name=%s AND sheet_name=%s AND lower(trim(position))=%s", [pn, r['rs'], r['p']])
            have, done, nd = float(h['have']), float(h['done']), float(r['need'])
            if have and have < nd - 0.01:
                warning['деталей в листе меньше, чем нужно сборкам'].append((pn, f"{r['rs']}: {r['p']} — в листе {have:g}, нужно {nd:g}"))
        # 5) собрано больше, чем изготовлено деталей
        over = db.fetchall(
            "SELECT ed.requires_sheet rs, lower(trim(ed.part)) p, SUM(ed.qty_per * COALESCE(d.q, 0)) used "
            "FROM element_dependencies ed LEFT JOIN (SELECT element, SUM(quantity) q FROM work_orders WHERE project_name=%s "
            "AND sheet_name='СБОРКА' AND row_num>0 AND status='ВЫПОЛНЕНО' GROUP BY element) d ON d.element=ed.element "
            "WHERE ed.project_name=%s AND ed.waiting_sheet='СБОРКА' AND ed.part IS NOT NULL GROUP BY 1,2", [pn, pn])
        for r in over:
            h = db.fetchone(
                "SELECT COALESCE(SUM(quantity) FILTER (WHERE status='ВЫПОЛНЕНО'),0) done, COALESCE(SUM(quantity),0) have "
                "FROM work_orders WHERE project_name=%s AND sheet_name=%s AND lower(trim(position))=%s", [pn, r['rs'], r['p']])
            if float(h['have']) and float(r['used']) > float(h['done']) + 0.01:
                warning['собрано больше, чем изготовлено деталей'].append(
                    (pn, f"{r['rs']}: {r['p']} — собрано на {float(r['used']):g}, изготовлено {float(h['done']):g}"))


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

    try:
        check_components(warning, {p['project_name'] for p in projects})
    except Exception as e:
        warning['проверка деталей на сборку не выполнена'].append(('', str(e)[:80]))

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
