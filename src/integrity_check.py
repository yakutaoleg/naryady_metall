#!/usr/bin/env python3
"""
Ежедневная проверка целостности количеств по 4 листам:
СБОРКА, СВАРКА, ГРУНТОВКА, ПОКРАСКА.

Логика:
- Мажоритарное голосование: «правильным» считается значение которое встречается у большинства листов.
- Если по позиции хотя бы 1 лист отличается от большинства — алёрт.
- В алёрте показываем контекст: последний авто-сплит по этой позиции из audit_log.
"""
import sys, os, re, asyncio
from collections import Counter
sys.path.insert(0, '/root/naryady/prod')
from dotenv import load_dotenv
load_dotenv('/root/naryady/prod/.env')
from src import db
from telegram import Bot

ADMIN_CHAT_ID = 340620064
CHECK_SHEETS  = ['СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА']

def _norm(name: str) -> str:
    """Нормализует имя для сравнения: убирает дефисы, пробелы, lowercase."""
    return re.sub(r'[-\s]', '', (name or '').strip()).lower()

def _majority(values: list[int]) -> int:
    """Возвращает мажоритарное значение (чаще всего встречающееся).
    При ничьей — берём минимум (консервативно: больший считается аномалией).
    """
    c = Counter(values)
    max_count = max(c.values())
    candidates = [v for v, cnt in c.items() if cnt == max_count]
    return min(candidates)

def _last_split_context(project_name: str, position_norm: str) -> str | None:
    """Ищет последний авто-сплит по данной позиции в audit_log."""
    rows = db.fetchall("""
        SELECT ts, details
        FROM audit_log
        WHERE action = 'auto_split'
          AND details->>'project' = %s
          AND LOWER(REGEXP_REPLACE(details->>'position', '[-\\s]', '', 'g')) = %s
        ORDER BY ts DESC
        LIMIT 3
    """, [project_name, position_norm])

    if not rows:
        return None

    lines = []
    for r in rows:
        d   = r['details']
        ts  = r['ts'].strftime('%d.%m %H:%M') if r['ts'] else '?'
        lines.append(
            f"  🔀 {ts} — {d.get('sheet','?')}, "
            f"выполнено {d.get('qty_done','?')}, остаток {d.get('remaining','?')}"
        )
    return "Последние сплиты:\n" + '\n'.join(lines)

def run_check():
    projects = db.fetchall(
        "SELECT project_name FROM projects WHERE status='АКТИВНЫЙ'", []
    )
    all_alerts = []

    for proj in projects:
        pname = proj['project_name']

        rows = db.fetchall("""
            SELECT
                position,
                sheet_name,
                SUM(quantity)::numeric AS total_qty,
                SUM(CASE WHEN status = 'ВЫПОЛНЕНО' THEN quantity ELSE 0 END)::numeric AS done_qty,
                SUM(CASE WHEN status = 'ПЛАН'      THEN quantity ELSE 0 END)::numeric AS plan_qty,
                SUM(CASE WHEN status = 'ЧАСТИЧНО'  THEN quantity ELSE 0 END)::numeric AS partial_qty
            FROM work_orders
            WHERE project_name = %s
              AND sheet_name   = ANY(%s)
              AND quantity IS NOT NULL
              AND position IS NOT NULL
            GROUP BY position, sheet_name
        """, [pname, CHECK_SHEETS])

        # Группируем по нормализованному имени
        by_norm: dict[str, dict] = {}
        for r in rows:
            key = _norm(r['position'])
            if key not in by_norm:
                by_norm[key] = {}
            by_norm[key][r['sheet_name']] = {
                'position': r['position'],
                'total':    int(r['total_qty']   or 0),
                'done':     int(r['done_qty']    or 0),
                'plan':     int(r['plan_qty']    or 0),
                'partial':  int(r['partial_qty'] or 0),
            }

        proj_alerts = []
        for key, sheets_data in by_norm.items():
            if len(sheets_data) < 2:
                continue

            totals    = [d['total'] for d in sheets_data.values()]
            correct   = _majority(totals)
            anomalies = {s: d for s, d in sheets_data.items() if d['total'] != correct}

            if not anomalies:
                continue  # все согласны

            # Имя для отображения: берём из любого листа с «правильным» значением
            display_name = next(
                (d['position'] for d in sheets_data.values() if d['total'] == correct),
                list(sheets_data.values())[0]['position']
            )

            lines = [f"*{display_name}* — некорректное количество:"]
            for sheet in CHECK_SHEETS:
                if sheet not in sheets_data:
                    continue
                d      = sheets_data[sheet]
                marker = ' ⚠️' if sheet in anomalies else ''
                parts  = []
                if d['done']    > 0: parts.append(f"{d['done']} выполнено")
                if d['partial'] > 0: parts.append(f"{d['partial']} частично")
                if d['plan']    > 0: parts.append(f"{d['plan']} ПЛАН")
                detail = f" (из них {', '.join(parts)})" if parts else ''
                lines.append(f"  {sheet}: {d['total']} шт{detail}{marker}")

            # Контекст из audit_log
            ctx = _last_split_context(pname, key)
            if ctx:
                lines.append(ctx)

            proj_alerts.append('\n'.join(lines))

        if proj_alerts:
            header = f"⚠️ *Проверка количеств — {pname}*\n"
            all_alerts.append(header + '\n\n'.join(proj_alerts))

    return all_alerts

async def main():
    alerts = run_check()
    if not alerts:
        print("Всё OK, расхождений нет.")
        return
    bot = Bot(token=os.environ['TG_TOKEN'])
    for msg in alerts:
        # Telegram limit 4096 chars per message
        if len(msg) > 4000:
            # Разбиваем на блоки по проекту
            for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text=chunk, parse_mode='Markdown')
        else:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode='Markdown')
        print(msg)

if __name__ == '__main__':
    asyncio.run(main())
