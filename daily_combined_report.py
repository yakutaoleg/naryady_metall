#!/usr/bin/env python3
"""
daily_combined_report.py — ежедневный объединённый отчёт в 21:00:
  1. Расхождения количеств (integrity_check)
  2. Нарушения оформления (из monitor_runs)
"""
import sys, os, asyncio, json
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from telegram import Bot
from src import db
import src.integrity_check as ic

ADMIN_CHAT_ID = 340620064

CATEGORY_LABELS = {
    'ВЫПОЛНЕНО_БЕЗ_ДАТЫ':        'ВЫПОЛНЕНО без ДАТА ФАКТ',
    'ВЫПОЛНЕНО_БЕЗ_ИСПОЛНИТЕЛЯ': 'ВЫПОЛНЕНО без ИСПОЛНИТЕЛЯ',
    'ПЛАН_С_ДАТОЙ':               'ПЛАН + ДАТА ФАКТ заполнена',
    'ДАТА_ПОРЯДОК':               'ДАТА ФАКТ раньше ДАТА ПЛАН',
    'КОЛ-ВО_НОЛЬ':               'КОЛ-ВО пустое или 0',
    'ДРОПДАУН':                   'Дропдаун повреждён',
    'БЛОК_ЗАВИСАНИЕ':             'БЛОК-зависание',
    'ЧАСТИЧНО_БЕЗ_ВЫПОЛНЕНО':    'ЧАСТИЧНО без ВЫПОЛНЕНО',
    'СТАТУС_НЕ_ЗАПОЛНЕН':         'СТАТУС пустой',
    'ОБЯЗАТЕЛЬНАЯ_НЕ_ЗАПОЛНЕНА':  'ОБЯЗАТЕЛЬНАЯ пустая',
    'ОФОРМЛЕНИЕ':                 'Нестандартное оформление',
    'ПРОЧЕЕ':                     'Прочее',
}

SKIP_CATS = {'ШИРИНА', 'ШРИФТ_ЗАГОЛОВКА', 'ФОН_ЗАГОЛОВКА',
             'ЗАВИСИМОСТИ_ЗДОРОВЬЕ', 'БЛОК_КОЛОНКА', 'ИТОГО_БЛОК'}


def build_monitor_section():
    today = datetime.now().date()
    rows = db.fetchall(
        """SELECT DISTINCT ON (project_name)
               project_name, total_issues, new_issues, checked_at, issues
           FROM monitor_runs
           WHERE checked_at::date = %s
           ORDER BY project_name, checked_at DESC""",
        [today]
    )
    if not rows:
        return None

    lines = ['<b>Мониторинг оформления</b>']
    for r in rows:
        issues = r['issues'] if isinstance(r['issues'], list) else json.loads(r['issues'] or '[]')
        total  = r['total_issues']
        new_c  = r['new_issues']
        ts     = r['checked_at'].strftime('%H:%M') if r['checked_at'] else '?'

        if total == 0:
            lines.append(f'\n  OK {r["project_name"]} — нарушений нет ({ts})')
            continue

        new_str = f', {new_c} новых' if new_c else ''
        lines.append(f'\n  {r["project_name"]} — {total} нарушений{new_str} ({ts})')

        by_cat = defaultdict(list)
        for iss in issues:
            cat = iss.get('category', 'ПРОЧЕЕ')
            if cat in SKIP_CATS:
                continue
            ref = iss.get('ref') or iss.get('text', '')[:60]
            by_cat[cat].append(ref)

        for cat, refs in sorted(by_cat.items(), key=lambda x: -len(x[1])):
            label = CATEGORY_LABELS.get(cat, cat)
            shown = ', '.join(refs[:3])
            more  = len(refs) - 3
            suffix = f' +{more} ещё' if more > 0 else ''
            lines.append(f'    • {label} ({len(refs)} шт): {shown}{suffix}')

    return '\n'.join(lines)


def build_integrity_section():
    alerts = ic.run_check()
    if not alerts:
        return 'Расхождений количеств нет'

    lines = ['<b>Расхождения количеств</b>']
    for msg in alerts:
        clean = msg.replace('*', '').strip()
        lines.append('\n' + clean)
    return '\n'.join(lines)


async def main():
    today_str = datetime.now().strftime('%d.%m.%Y')
    bot = Bot(token=os.environ['TG_TOKEN'])

    parts = [f'<b>Ежедневный отчёт — {today_str}</b>']

    try:
        parts.append('\n' + build_integrity_section())
    except Exception as e:
        parts.append(f'\nОшибка проверки количеств: {e}')

    try:
        monitor = build_monitor_section()
        parts.append('\n' + (monitor if monitor else 'Данных мониторинга за сегодня нет'))
    except Exception as e:
        parts.append(f'\nОшибка мониторинга: {e}')

    full_text = '\n'.join(parts)
    for i in range(0, len(full_text), 4000):
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=full_text[i:i+4000],
            parse_mode='HTML'
        )
    print(f'Отправлено ({len(full_text)} символов)')


if __name__ == '__main__':
    asyncio.run(main())
