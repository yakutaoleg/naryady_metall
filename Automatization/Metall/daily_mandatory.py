# -*- coding: utf-8 -*-
"""
daily_mandatory.py — ежедневные задачи в 21:00 МСК:
  1. Ставит mandatory=true на незакрытые задачи прошедших дней
  2. Отправляет мастерам сводный отчёт за день
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
from collections import defaultdict
from telegram import Bot
from src import db, config
from src.sheets import _gc, _api_call, _col_letter, SCOPES_RW


# ─── 1. Обновление обязательных задач ────────────────────────────────────────

def run_mandatory():
    rows = db.fetchall(
        """SELECT id, file_id, sheet_name, row_num
           FROM work_orders
           WHERE status = 'ПЛАН'
             AND date_plan IS NOT NULL
             AND date_plan < CURRENT_DATE
             AND mandatory = false""",
        []
    )
    if not rows:
        print("mandatory: нет задач для обновления")
    else:
        db.execute(
            """UPDATE work_orders
               SET mandatory = true
               WHERE status = 'ПЛАН'
                 AND date_plan IS NOT NULL
                 AND date_plan < CURRENT_DATE
                 AND mandatory = false""",
            []
        )
        print(f"mandatory: обновлено {len(rows)} строк в БД")

        gc = _gc(SCOPES_RW)
        ws_cache = {}
        written = 0
        for r in rows:
            key = (r['file_id'], r['sheet_name'])
            if key not in ws_cache:
                try:
                    ss = gc.open_by_key(r['file_id'])
                    ws = ss.worksheet(r['sheet_name'])
                    headers = ws.row_values(2)
                    col_map = {h.strip(): i + 1 for i, h in enumerate(headers) if h.strip()}
                    ws_cache[key] = (ws, col_map.get('ОБЯЗАТЕЛЬНАЯ'))
                except Exception as e:
                    print(f"  ошибка открытия {r['sheet_name']}: {e}")
                    ws_cache[key] = (None, None)

            ws, col_mandatory = ws_cache[key]
            if ws is None or col_mandatory is None:
                continue
            try:
                ws.update(values=[['ДА']], range_name=f'{_col_letter(col_mandatory)}{r["row_num"] + 2}')
                written += 1
            except Exception as e:
                print(f"  ошибка записи строки {r['row_num'] + 2}: {e}")

        print(f"mandatory: записано ДА в таблицу: {written} строк")


# ─── 2. Ежедневный отчёт ─────────────────────────────────────────────────────

def build_report() -> str:
    done_rows = db.fetchall(
        """SELECT project_name, sheet_name, position, element,
                  quantity, qty_done, status, executor
           FROM work_orders
           WHERE status IN ('ВЫПОЛНЕНО', 'ЧАСТИЧНО')
             AND date_fact = CURRENT_DATE
             AND executor IS NOT NULL AND executor != ''
           ORDER BY project_name, executor, sheet_name""",
        []
    )

    missed_rows = db.fetchall(
        """SELECT project_name, sheet_name, position, element,
                  quantity, executor
           FROM work_orders
           WHERE mandatory = true
             AND status = 'ПЛАН'
             AND date_plan <= CURRENT_DATE
             AND executor IS NOT NULL AND executor != ''
           ORDER BY project_name, executor""",
        []
    )

    if not done_rows and not missed_rows:
        return "📋 <b>Отчёт за день</b>\n\nНет закрытых позиций и незавершённых обязательных задач."

    lines = ["📋 <b>Отчёт за день</b>\n"]

    if done_rows:
        lines.append("✅ <b>Выполнено сегодня:</b>")
        by_project = defaultdict(lambda: defaultdict(list))
        for r in done_rows:
            icon = '✅' if r['status'] == 'ВЫПОЛНЕНО' else '◧'
            pos = r['position'] or r['element'] or '—'
            if r['status'] == 'ЧАСТИЧНО':
                qty_str = f"{int(r['qty_done'] or 0)}/{int(r['quantity'] or 0)} шт"
            else:
                qty_str = f"{int(r['quantity'] or 0)} шт"
            by_project[r['project_name']][r['executor']].append(f"    {icon} {pos} — {qty_str}")

        for proj, executors in by_project.items():
            lines.append(f"\n📁 <b>{proj}</b>")
            for executor, items in executors.items():
                lines.append(f"  👤 {executor}")
                lines.extend(items)

    if missed_rows:
        lines.append("\n⚠️ <b>Не выполнено (обязательные):</b>")
        by_project = defaultdict(list)
        for r in missed_rows:
            pos = r['position'] or r['element'] or '—'
            qty = int(r['quantity'] or 0)
            by_project[r['project_name']].append(
                f"    ☐ {pos} — {qty} шт ({r['executor']})"
            )
        for proj, items in by_project.items():
            lines.append(f"\n📁 <b>{proj}</b>")
            lines.extend(items)

    return "\n".join(lines)


async def send_report():
    report = build_report()
    masters = db.fetchall(
        """SELECT full_name, telegram_id, telegram_username
           FROM employees
           WHERE role='master'
             AND (telegram_id IS NOT NULL OR (telegram_username IS NOT NULL AND telegram_username != ''))""",
        []
    )
    if not masters:
        print("report: нет мастеров с telegram_id или telegram_username")
        return

    bot = Bot(token=config.TG_TOKEN)
    async with bot:
        for m in masters:
            chat_id = m['telegram_id'] or f"@{m['telegram_username']}"
            try:
                await bot.send_message(chat_id=chat_id, text=report, parse_mode='HTML')
                print(f"report: отправлено {m['full_name']} ({chat_id})")
            except Exception as e:
                print(f"report: ошибка отправки {m['full_name']} ({chat_id}): {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    run_mandatory()
    asyncio.run(send_report())
