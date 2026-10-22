import logging
import sys
import os
import io
import re
import asyncio
from datetime import date, time as dtime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from src import config, db
from src import project_wizard as _wiz, logger as app_logger
from src import deps_parts
from src.sheets import update_task_status
from google.oauth2.service_account import Credentials as _SACredentials
from googleapiclient.discovery import build as _gdrive_build

def _download_drawing(file_id: str):
    """Download a file from Google Drive. Returns (bytes, name, mime_type)."""
    creds = _SACredentials.from_service_account_file(
        config.GOOGLE_SA_KEY,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    drive = _gdrive_build('drive', 'v3', credentials=creds)
    meta = drive.files().get(fileId=file_id, fields='mimeType,name', supportsAllDrives=True).execute()
    mime = meta.get('mimeType', '')
    if 'google-apps' in mime:
        request = drive.files().export_media(fileId=file_id, mimeType='application/pdf')
        mime = 'application/pdf'
    else:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    from googleapiclient.http import MediaIoBaseDownload
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue(), meta.get('name', 'drawing'), mime

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL))

WAITING_BLOCK_COMMENT   = 1
WAITING_PROJECT_LINK    = 2
WAITING_PARTIAL_QTY     = 3
WAITING_EARNINGS_DATE   = 4
WAITING_CORRECTION_QTY  = 5
WAITING_WIZARD_SPECS    = 6
WAITING_WIZARD_CONFIRM  = 7
WAITING_WIZARD_SOURCES  = 8
WAITING_WIZARD_SECTIONS = 9
WAITING_EMP_NAME        = 10
WAITING_APPROVE_NAME    = 11
TODAY = lambda: date.today().strftime('%d.%m.%Y')

SPECIALIZATIONS = ['ПИЛА', 'ПЛАЗМА', 'СБОРКА', 'СВАРКА', 'СВЕРЛЕНИЕ', 'ГРУНТОВКА', 'ПОКРАСКА']


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_worker(tg_username: str, tg_id: int = None):
    if tg_id:
        row = db.fetchone(
            "SELECT full_name, specialization, role FROM employees WHERE telegram_id=%s AND is_active=true",
            [tg_id]
        )
        if row:
            return row
    if tg_username:
        return db.fetchone(
            "SELECT full_name, specialization, role FROM employees WHERE telegram_username=%s AND is_active=true",
            [tg_username]
        )
    return None


def is_master(role: str) -> bool:
    return role in ('master', 'admin')


def get_projects():
    return db.fetchall(
        "SELECT id, project_name, status, created_at, last_synced_at FROM projects ORDER BY created_at DESC",
        []
    )


def get_all_blocked():
    return db.fetchall(
        """SELECT wo.id, wo.project_name, wo.sheet_name, wo.position, wo.element,
                  wo.executor, wo.comment, wo.updated_at
           FROM work_orders wo
           WHERE wo.status='БЛОК'
           ORDER BY wo.updated_at DESC""",
        []
    )



def get_earnings_by_date(date_str: str):
    return db.fetchall(
        """SELECT COALESCE(NULLIF(executor,''), NULL) as executor,
                  project_name, sheet_name,
                  COUNT(*) as tasks_count,
                  SUM(payment_sum) as total
           FROM work_orders
           WHERE status='ВЫПОЛНЕНО' AND payment_sum IS NOT NULL
             AND date_fact = %s
           GROUP BY executor, project_name, sheet_name
           ORDER BY executor NULLS LAST, project_name, sheet_name""",
        [date_str]
    )


def get_earnings_detail_by_date(date_str: str):
    return db.fetchall(
        """SELECT COALESCE(NULLIF(executor,''), NULL) as executor,
                  project_name, sheet_name,
                  position, element,
                  quantity, payment_sum
           FROM work_orders
           WHERE status='ВЫПОЛНЕНО' AND date_fact = %s
           ORDER BY executor NULLS LAST, project_name, sheet_name, position""",
        [date_str]
    )


def get_active_tasks(worker_name: str, specialization: str = None):
    tasks = db.fetchall(
        """SELECT id, project_name, sheet_name, file_id, row_num, position, element,
                  quantity, qty_done, unit_weight, total_weight, payment_sum,
                  date_plan, priority, mandatory, status, drawing_link
           FROM work_orders
           WHERE executor=%s AND status IN ('ПЛАН','ЧАСТИЧНО')
             AND date_plan <= CURRENT_DATE
           ORDER BY mandatory DESC, sheet_name, priority ASC NULLS LAST, position""",
        [worker_name]
    )
    for t in tasks:
        if t['sheet_name'].upper() in SHEETS_WITH_DEPS:
            try:
                t['deps_ready'] = _deps_ready(t['project_name'], t['element'], t['sheet_name'], int(t['quantity'] or 0) or None)
                if not t['deps_ready']:
                    t['deps_avail_qty'], _ = _deps_available_qty(
                        t['project_name'], t['element'], t['sheet_name'], int(t['quantity'] or 0)
                    )
                else:
                    t['deps_avail_qty'] = int(t['quantity'] or 0)
            except Exception as e:
                app_logger.alert(f"_deps_ready error task_id={t['id']} element={t['element']}: {e}")
                t['deps_ready'] = True
                t['deps_avail_qty'] = int(t['quantity'] or 0)
        else:
            t['deps_ready'] = True
            t['deps_avail_qty'] = int(t['quantity'] or 0)
    return tasks


def _remaining_qty(project_name: str, sheet_name: str, element: str):
    """Сколько штук элемента в листе ещё не выполнено (для расчёта по количеству деталей на сборку)."""
    r = db.fetchone(
        """SELECT COALESCE(SUM(quantity), 0) AS q FROM work_orders
           WHERE project_name=%s AND sheet_name=%s AND element=%s AND status != 'ВЫПОЛНЕНО' AND row_num > 0""",
        [project_name, sheet_name, element])
    q = int(r['q'] or 0) if r else 0
    return q if q > 0 else None


def _deps_ready(project_name: str, element: str, sheet_name: str = None, own_qty: int = None) -> bool:
    """Возвращает True если все зависимости для элемента выполнены (или зависимостей нет)."""
    if not element:
        return True
    _cov = set()
    if sheet_name:
        # Есть количество деталей на сборку (лист ЗАВИСИМОСТИ F/G): считаем по фактическому расходу
        _pres = deps_parts.compute(project_name, sheet_name, element,
                                   own_qty or _remaining_qty(project_name, sheet_name, element))
        if _pres:
            if _pres['blocked']:
                return False
            _cov = set(_pres['covered'])
    if sheet_name:
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position FROM element_dependencies
               WHERE project_name=%s AND element=%s AND waiting_sheet=%s""",
            [project_name, element, sheet_name]
        )
    else:
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position FROM element_dependencies
               WHERE project_name=%s AND element=%s""",
            [project_name, element]
        )
    if not deps:
        return True
    for dep in deps:
        if dep['requires_sheet'] in _cov:
            continue
        # Проверяем по element (не position): все строки с этим элементом в upstream листе
        rows = db.fetchall(
            """SELECT status, COALESCE(quantity, 0) as qty FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND element=%s""",
            [project_name, dep['requires_sheet'], dep['requires_position']]
        )
        # Нет строк — upstream не заведён, считаем что не блокирует
        if not rows:
            continue
        # Проверяем по qty: все должны быть ВЫПОЛНЕНО
        done_qty  = sum(int(r.get('qty') or 0) for r in rows if r['status'] == 'ВЫПОЛНЕНО')
        total_qty = sum(int(r.get('qty') or 0) for r in rows)
        if total_qty > 0 and done_qty < total_qty:
            return False
    return True


def _deps_blocking(project_name: str, element: str, sheet_name: str = None, own_qty: int = None) -> list[str]:
    """Возвращает список незакрытых зависимостей в виде читаемых строк."""
    if not element:
        return []
    _cov, _lines = set(), []
    if sheet_name:
        _pres = deps_parts.compute(project_name, sheet_name, element,
                                   own_qty or _remaining_qty(project_name, sheet_name, element))
        if _pres:
            _cov = set(_pres['covered'])
            if _pres['blocked']:
                _lines = deps_parts.blocking_lines(_pres)
    if sheet_name:
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position FROM element_dependencies
               WHERE project_name=%s AND element=%s AND waiting_sheet=%s""",
            [project_name, element, sheet_name]
        )
    else:
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position FROM element_dependencies
               WHERE project_name=%s AND element=%s""",
            [project_name, element]
        )
    blocking = list(_lines)
    for dep in deps:
        if dep['requires_sheet'] in _cov:
            continue
        rows = db.fetchall(
            """SELECT status, COALESCE(quantity, 0) as qty FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND element=%s""",
            [project_name, dep['requires_sheet'], dep['requires_position']]
        )
        if not rows:
            continue
        done_qty  = sum(int(r['qty']) for r in rows if r['status'] == 'ВЫПОЛНЕНО')
        total_qty = sum(int(r['qty']) for r in rows)
        if total_qty > 0 and done_qty < total_qty:
            blocking.append(f"{dep['requires_sheet']}: {dep['requires_position']} ({done_qty}/{total_qty} шт вып.)")
    return blocking


def _deps_available_qty(project_name: str, element: str, sheet_name: str, task_qty: int) -> tuple:
    """Возвращает (available_qty, task_qty) — сколько можно выполнить.
    Пропорционально: upstream готов на 50% → доступно floor(0.5 * task_qty).
    """
    if not element or task_qty <= 0:
        return (task_qty, task_qty)
    deps = db.fetchall(
        """SELECT requires_sheet, requires_position FROM element_dependencies
           WHERE project_name=%s AND element=%s AND waiting_sheet=%s""",
        [project_name, element, sheet_name]
    )
    if not deps:
        return (task_qty, task_qty)
    _pres = deps_parts.compute(project_name, sheet_name, element, task_qty)
    _cov = set(_pres['covered']) if _pres else set()
    min_ratio = 1.0
    for dep in deps:
        if dep['requires_sheet'] in _cov:
            continue
        rows = db.fetchall(
            """SELECT status, COALESCE(quantity, 0) as qty FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND element=%s""",
            [project_name, dep['requires_sheet'], dep['requires_position']]
        )
        if not rows:
            continue
        done_qty  = sum(int(r['qty']) for r in rows if r['status'] == 'ВЫПОЛНЕНО')
        total_qty = sum(int(r['qty']) for r in rows)
        if total_qty > 0:
            ratio = done_qty / total_qty
            if ratio < min_ratio:
                min_ratio = ratio
    available = int(min_ratio * task_qty)
    if _pres:
        available = min(available, max(0, int(_pres['avail'])))
    return (available, task_qty)


async def _notify_worker_unblocked(bot, executor: str, position: str, element: str):
    emp = db.fetchone(
        "SELECT telegram_username, telegram_id FROM employees WHERE full_name=%s AND is_active=true",
        [executor]
    )
    if not emp or (not emp['telegram_username'] and not emp['telegram_id']):
        return
    try:
        if emp['telegram_id']:
            _chat_id = emp['telegram_id']
        else:
            _chat_id = (await bot.get_chat(f"@{emp['telegram_username']}")).id
        await bot.send_message(
            chat_id=_chat_id,
            text=(
                f"✅ Блокировка снята\n\n"
                f"Позиция: {position} — {element or ''}\n"
                f"Можно продолжать работу."
            ),
            reply_markup=back_to_tasks_kb()
        )
    except Exception as e:
        app_logger.alert(f"_notify_worker_unblocked error for {executor}: {e}")


async def _notify_masters_dep_unblocked(bot, project_name: str, waiting_sheet: str,
                                         position: str, qty_unblocked: int, qty_still_blocked: int):
    masters = db.fetchall(
        "SELECT telegram_username FROM employees WHERE role='master' AND is_active=true",
        []
    )
    text = (
        f"🔔 Автоматическая разблокировка\n\n"
        f"📁 {project_name}\n"
        f"Спец: {waiting_sheet} | {position}\n\n"
        f"✅ {qty_unblocked} шт → ПЛАН (можно работать)\n"
    )
    if qty_still_blocked > 0:
        text += f"⛔ {qty_still_blocked} шт → ещё ждут"
    for m in masters:
        if not m['telegram_username']:
            continue
        try:
            chat = await bot.get_chat(f"@{m['telegram_username']}")
            await bot.send_message(chat_id=chat.id, text=text)
        except Exception as e:
            app_logger.alert(f"_notify_masters_dep_unblocked error for {m['telegram_username']}: {e}")


async def notify_deps_unblocked(bot, project_name: str, completed_sheet: str,
                                 completed_position: str, qty_done: int,
                                 completed_element: str = None):
    """Вызывается когда задача перешла в ЧАСТИЧНО или ВЫПОЛНЕНО.
    Ищет заблокированные задачи следующего уровня и разбивает их."""
    from src import sheets as _sheets
    import uuid as _uuid

    # Сопоставляем по element (не position): completed_element — код сборочной единицы
    match_key = completed_element or completed_position
    waiting_deps = db.fetchall(
        """SELECT DISTINCT waiting_sheet, element FROM element_dependencies
           WHERE project_name=%s AND requires_sheet=%s AND requires_position=%s""",
        [project_name, completed_sheet, match_key]
    )
    if not waiting_deps:
        return

    for dep in waiting_deps:
        waiting_sheet = dep['waiting_sheet']
        element       = dep['element']

        blocked_tasks = db.fetchall(
            """SELECT id, position, element, quantity, executor, file_id, sheet_name, row_num,
                      unit_weight, total_weight, payment_sum, drawing_link, date_plan, priority, comment
               FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND element=%s AND status='БЛОК'""",
            [project_name, waiting_sheet, element]
        )

        for task in blocked_tasks:
            qty_blocked  = int(task['quantity'] or 0)
            qty_unblock  = min(qty_done, qty_blocked)
            qty_still    = qty_blocked - qty_unblock

            if qty_unblock <= 0:
                continue

            _uw      = float(task['unit_weight']  or 0)
            _orig_q  = float(task['quantity']     or 1)
            _orig_ps = float(task['payment_sum']  or 0)

            _tw_plan  = round(_uw * qty_unblock, 3)             if _uw      else None
            _ps_plan  = round(_orig_ps / _orig_q * qty_unblock, 2) if _orig_ps else None
            _tw_block = round(_uw * qty_still, 3)               if _uw      else None
            _ps_block = round(_orig_ps / _orig_q * qty_still, 2)   if _orig_ps else None

            if qty_still == 0:
                # Полная разблокировка
                db.execute(
                    "UPDATE work_orders SET status='ПЛАН', comment=NULL WHERE id=%s",
                    [task['id']]
                )
                try:
                    update_task_status(task['file_id'], task['sheet_name'], task['row_num'], 'ПЛАН')
                    _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'КОММЕНТАРИЙ', '')
                except Exception as e:
                    app_logger.alert(f"notify_deps full unblock sheet error: {e}")
            else:
                # Частичная — разбиваем: оригинал → ПЛАН×qty_unblock, новый → БЛОК×qty_still
                new_row_id = str(_uuid.uuid4())
                db.execute(
                    """UPDATE work_orders SET status='ПЛАН', comment=NULL,
                       quantity=%s, total_weight=%s, payment_sum=%s WHERE id=%s""",
                    [qty_unblock, _tw_plan, _ps_plan, task['id']]
                )
                db.execute(
                    """INSERT INTO work_orders
                       (project_name, file_id, sheet_name, row_num, row_id,
                        position, element, quantity, unit_weight, total_weight, payment_sum,
                        executor, date_plan, priority, mandatory, status, drawing_link, comment)
                       SELECT project_name, file_id, sheet_name, -%s, %s,
                              position, element, %s, unit_weight, %s, %s,
                              executor, date_plan, priority, false, 'БЛОК', drawing_link,
                              'Ожидает готовности остатка'
                       FROM work_orders WHERE id=%s""",
                    [task['id'], new_row_id, qty_still, _tw_block, _ps_block, task['id']]
                )
                try:
                    update_task_status(task['file_id'], task['sheet_name'], task['row_num'], 'ПЛАН')
                    _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'КОЛ-ВО', qty_unblock)
                    _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'КОММЕНТАРИЙ', '')
                    if _tw_plan:
                        _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'МАССА ВСЕХ (кг)', _tw_plan)
                    if _ps_plan:
                        _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'СУММА К ОПЛАТЕ', _ps_plan)
                    from src.sheets import _NEXT_SPEC
                    _next = _NEXT_SPEC.get(task['sheet_name'].upper(), '')
                    remainder_data = {
                        'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': task['position'] or '',
                        'ЭЛЕМЕНТ':               task['element'] or '',
                        'КОЛ-ВО':               qty_still,
                        'МАССА ЕД. (кг)':       _uw if _uw else '',
                        'МАССА ВСЕХ (кг)':      _tw_block if _tw_block else '',
                        'СУММА К ОПЛАТЕ':       _ps_block if _ps_block else '',
                        'СТАТУС':               'БЛОК',
                        'БЛОК':                 f'⛔ {_next}' if _next else '',
                        'ОБЯЗАТЕЛЬНАЯ':         'НЕТ',
                        'ИСПОЛНИТЕЛЬ':          task['executor'] or '',
                        'ROW_ID':               new_row_id,
                        'ССЫЛКА НА ЧЕРТЁЖ':    task['drawing_link'] or '',
                        'КОММЕНТАРИЙ':          'Ожидает готовности остатка',
                    }
                    _sheets.insert_remainder_row(task['file_id'], task['sheet_name'], task['row_num'], remainder_data)
                except Exception as e:
                    app_logger.alert(f"notify_deps split sheet error: {e}")

            # Уведомляем рабочего (если назначен)
            if task['executor']:
                try:
                    await _notify_worker_unblocked(bot, task['executor'], task['position'], task['element'])
                except Exception as e:
                    app_logger.alert(f"notify_deps worker notify error: {e}")

            # Уведомляем мастеров
            try:
                await _notify_masters_dep_unblocked(
                    bot, project_name, waiting_sheet, task['position'], qty_unblock, qty_still
                )
            except Exception as e:
                app_logger.alert(f"notify_deps masters notify error: {e}")


async def notify_assembly_workers(bot, project_name: str, element: str):
    """Уведомить сборщиков что детали по элементу готовы."""
    workers = db.fetchall(
        """SELECT DISTINCT wo.executor FROM work_orders wo
           JOIN employees e ON e.full_name = wo.executor
           WHERE wo.project_name=%s AND wo.sheet_name='СБОРКА'
             AND wo.element=%s AND wo.status='ПЛАН'
             AND e.is_active=true""",
        [project_name, element]
    )
    for w in workers:
        emp = db.fetchone(
            "SELECT id, telegram_username, telegram_id FROM employees WHERE full_name=%s AND is_active=true",
            [w['executor']]
        )
        if not emp or (not emp['telegram_username'] and not emp['telegram_id']):
            continue
        try:
            if emp['telegram_id']:
                _chat_id = emp['telegram_id']
            else:
                _chat_id = (await bot.get_chat(f"@{emp['telegram_username']}")).id
            text = (
                f"✅ Детали готовы!\n\n"
                f"Элемент: {element}\n"
                f"Проект: {project_name}\n\n"
                f"Можно брать в сборку."
            )
            sent = await bot.send_message(chat_id=_chat_id, text=text)
            db.execute(
                """INSERT INTO notifications (tg_user_id, notif_type, message_id)
                   VALUES (%s, 'ASSEMBLY_READY', %s)""",
                [_chat_id, sent.message_id]
            )
        except Exception as e:
            app_logger.alert(f"notify_assembly error for {w['executor']}: {e}")


def get_blocked_tasks(worker_name: str, specialization: str = None):
    return db.fetchall(
        """SELECT id, position, element, quantity, comment, sheet_name
           FROM work_orders
           WHERE executor=%s AND status='БЛОК'
           ORDER BY sheet_name, position""",
        [worker_name]
    )


def get_done_today(worker_name: str, specialization: str = None):
    return db.fetchall(
        """SELECT id, position, element, quantity, payment_sum, sheet_name
           FROM work_orders
           WHERE executor=%s
             AND status='ВЫПОЛНЕНО' AND date_fact=CURRENT_DATE
           ORDER BY sheet_name, position""",
        [worker_name]
    )


def get_tariff(work_type: str, unit_weight):
    if unit_weight is None:
        return None
    return db.fetchone(
        """SELECT rate, unit FROM tariffs
           WHERE work_type=%s AND range_from <= %s AND range_to > %s
           LIMIT 1""",
        [work_type, float(unit_weight), float(unit_weight)]
    )


def mandatory_remaining(worker_name: str, specialization: str = None):
    rows = db.fetchall(
        """SELECT position FROM work_orders
           WHERE executor=%s AND mandatory=true AND status='ПЛАН'
             AND date_plan IS NOT NULL AND date_plan < CURRENT_DATE
           ORDER BY position""",
        [worker_name]
    )
    return [r['position'] for r in rows]


async def notify_masters(bot, task_id: int, worker_name: str, specialization: str,
                         position: str, element: str, comment: str):
    masters = db.fetchall(
        "SELECT id, telegram_username, telegram_id FROM employees WHERE role='master' AND is_active=true AND notify_blocks=true",
        []
    )
    for master in masters:
        try:
            # Используем числовой telegram_id — надёжнее username
            chat_id = master.get('telegram_id')
            if not chat_id:
                if not master['telegram_username']:
                    continue
                chat = await bot.get_chat(f"@{master['telegram_username']}")
                chat_id = chat.id
            text = (
                f"\U0001f6ab Блокировка на позиции\n\n"
                f"Элемент: {element or '—'}\n"
                f"Позиция: {position}\n"
                f"Специализация: {', '.join(specialization) if isinstance(specialization, list) else (specialization or '—')}\n"
                f"Рабочий: {worker_name}\n"
                f"Причина: {comment}\n"
                f"Время: {TODAY()}"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Снять блок", callback_data=f"unblock:{task_id}"),
            ]])
            sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            db.execute(
                """INSERT INTO notifications (tg_user_id, notif_type, work_order_id, message_id)
                   VALUES (%s, 'BLOCK_ALERT', %s, %s)""",
                [chat_id, task_id, sent.message_id]
            )
        except Exception as e:
            app_logger.alert(f"notify_masters error for @{master['telegram_username']}: {e}")


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def main_menu_kb(role: str = ''):
    if is_master(role):
        rows = [
            [
                InlineKeyboardButton("🗂 Проекты", callback_data="projects"),
                InlineKeyboardButton("🚫 Блоки", callback_data="master:blocks"),
            ],
            [InlineKeyboardButton("📅 Планы на сегодня", callback_data=f"plans:{date.today().isoformat()}")],
            [InlineKeyboardButton("🔧 Исправить выполнение", callback_data="correct:workers")],
            [InlineKeyboardButton("📊 Выработка", callback_data=f"earnings:{date.today().isoformat()}")],
            [InlineKeyboardButton("🔍 Мониторинг", callback_data="run_monitor")],
        ]
        if role == 'admin':
            rows.append([InlineKeyboardButton("👥 Сотрудники", callback_data="admin:users")])
        rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="menu")])
    else:
        rows = [
            [
                InlineKeyboardButton("📋 Задачи на сегодня", callback_data="tasks"),
                InlineKeyboardButton("✅ Выполнено сегодня", callback_data="done_today"),
            ],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu")],
        ]
    return InlineKeyboardMarkup(rows)


def projects_list_kb(projects: list, archive: bool = False, archived_count: int = 0):
    rows = []
    for p in projects:
        status_icon = "📦" if archive else "🟢"
        rows.append([InlineKeyboardButton(
            f"{status_icon} {p['project_name']}",
            callback_data=f"project:detail:{p['id']}"
        )])
    if archive:
        rows.append([InlineKeyboardButton("← К активным проектам", callback_data="projects")])
        return InlineKeyboardMarkup(rows)
    if archived_count:
        rows.append([InlineKeyboardButton(f"📦 Архив ({archived_count})", callback_data="projects:archive")])
    rows.append([InlineKeyboardButton("➕ Добавить проект", callback_data="project:add")])
    rows.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def project_detail_kb(project_id: int, status: str):
    rows = []
    if status == 'АКТИВНЫЙ':
        rows.append([InlineKeyboardButton("🔄 Синхронизировать", callback_data=f"project:sync:{project_id}")])
        rows.append([InlineKeyboardButton("📦 В архив", callback_data=f"project:archive:{project_id}")])
    else:
        rows.append([InlineKeyboardButton("🟢 Восстановить", callback_data=f"project:restore:{project_id}")])
    rows.append([InlineKeyboardButton("🗑 Удалить проект", callback_data=f"project:delete:{project_id}")])
    rows.append([InlineKeyboardButton("← К проектам", callback_data="projects")])
    return InlineKeyboardMarkup(rows)


def tasks_list_kb(tasks: list, blocked: list, mandatory_left: list):
    buttons = []
    multi_spec = len({t['sheet_name'] for t in tasks}) > 1
    for t in tasks:
        if not t.get('deps_ready', True):
            _avail = t.get('deps_avail_qty', 0)
            _tot   = int(t['quantity'] or 0)
            prefix = f"✅ {_avail} из {_tot} " if _avail > 0 else "⛔ "
        elif t['mandatory']:
            prefix = "❗ "
        elif mandatory_left:
            prefix = "🔒 "
        else:
            prefix = ""
        qty_done_val = t.get('qty_done') or 0
        if t.get('status') == 'ЧАСТИЧНО' and qty_done_val and t['quantity']:
            qty_str = f"{int(t['quantity'] - qty_done_val)} (ост.)"
        else:
            qty_str = str(int(t['quantity'])) if t['quantity'] else '?'
        spec_tag = f"[{t['sheet_name']}] " if multi_spec else ""
        pos_part = f"{t['position']} — " if t['position'] else ""
        label = f"{prefix}{spec_tag}{pos_part}{t['element'] or ''} × {qty_str}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"task:{t['id']}")])

    for t in blocked:
        spec_tag = f"[{t['sheet_name']}] " if len({b['sheet_name'] for b in blocked}) > 1 else ""
        pos_part2 = f"{t['position']} — " if t['position'] else ""
        label = f"⛔ {spec_tag}{pos_part2}{t['element'] or ''} (заблок.)"
        buttons.append([InlineKeyboardButton(label, callback_data="blocked_info")])

    buttons.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def task_detail_kb(task_id: int, has_drawing: bool = False, qty_total=None, qty_done=0):
    rows = []
    if has_drawing:
        rows.append([InlineKeyboardButton("📄 Чертёж", callback_data=f"drawing:{task_id}")])
    qty_remaining = int(qty_total - qty_done) if qty_total else None
    done_label = "✅ Выполнено" + (f" ({qty_remaining} шт.)" if qty_remaining else "")
    rows.append([
        InlineKeyboardButton(done_label, callback_data=f"done:{task_id}"),
        InlineKeyboardButton("◧ Частично", callback_data=f"partial_ask:{task_id}"),
    ])
    rows.append([
        InlineKeyboardButton("⛔ БЛОК", callback_data=f"block_ask:{task_id}"),
        InlineKeyboardButton("← К задачам", callback_data="tasks"),
    ])
    return InlineKeyboardMarkup(rows)


def confirm_block_kb(task_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, заблокировать", callback_data=f"block_confirm:{task_id}"),
        InlineKeyboardButton("Отмена", callback_data=f"task:{task_id}"),
    ]])


def back_to_tasks_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← К задачам", callback_data="tasks"),
    ]])


def _correct_nav_row(date_str: str):
    from datetime import timedelta
    d        = date.fromisoformat(date_str)
    prev_str = (d - timedelta(days=1)).isoformat()
    next_str = (d + timedelta(days=1)).isoformat()
    nav = [InlineKeyboardButton((d - timedelta(days=1)).strftime('← %d.%m'),
                                callback_data=f"correct:workers:{prev_str}")]
    if d < date.today() - timedelta(days=1):
        nav.append(InlineKeyboardButton((d + timedelta(days=1)).strftime('%d.%m →'),
                                        callback_data=f"correct:workers:{next_str}"))
    return nav

def correct_workers_kb(workers: list, date_str: str):
    # workers = list of dicts: {executor, emp_id}
    rows = [[InlineKeyboardButton(f"👤 {w['executor']}", callback_data=f"correct:tasks:{w['emp_id']}:{date_str}")] for w in workers]
    rows.append(_correct_nav_row(date_str))
    rows.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def correct_tasks_kb(tasks: list, date_str: str):
    rows = []
    current_group = None
    for t in tasks:
        group = (t['project_name'], t['sheet_name'])
        if group != current_group:
            current_group = group
            sheet_icon = SHEET_ICONS.get(t['sheet_name'], '📋')
            rows.append([InlineKeyboardButton(
                f"📁 {t['project_name']}  {sheet_icon} {t['sheet_name']}",
                callback_data="noop"
            )])
        icon = '✅' if t['status'] == 'ВЫПОЛНЕНО' else '◧'
        dfact = t['date_fact'].strftime('%d.%m') if t['date_fact'] else '—'
        qty = int(t['quantity'] or 0)
        rows.append([InlineKeyboardButton(
            f"{icon} {t['position'] or t['element'] or '—'} × {qty} шт ({dfact})",
            callback_data=f"correct:action:{t['id']}"
        )])
    rows.append(_correct_nav_row(date_str))
    rows.append([InlineKeyboardButton("← Рабочие", callback_data=f"correct:workers:{date_str}")])
    return InlineKeyboardMarkup(rows)


def correct_action_kb(task_id: int, emp_id: int, date_str: str, show_qty: bool = True):
    rows = []
    if show_qty:
        rows.append([InlineKeyboardButton("◧ Исправить количество", callback_data=f"correct:qty:{task_id}")])
    rows.append([InlineKeyboardButton("❌ Отменить полностью", callback_data=f"correct:cancel:{task_id}")])
    rows.append([InlineKeyboardButton("← К задачам", callback_data=f"correct:tasks:{emp_id}:{date_str}")])
    return InlineKeyboardMarkup(rows)


def earnings_date_kb(date_str: str, page: int = 0, total_pages: int = 1):
    from datetime import timedelta
    d = date.fromisoformat(date_str)
    prev_str = (d - timedelta(days=1)).isoformat()
    next_str = (d + timedelta(days=1)).isoformat()
    prev_label = (d - timedelta(days=1)).strftime('← %d.%m')
    next_label = (d + timedelta(days=1)).strftime('%d.%m →')
    rows = []
    rows.append([
        InlineKeyboardButton(prev_label, callback_data=f"earnings:{prev_str}"),
        InlineKeyboardButton(next_label, callback_data=f"earnings:{next_str}"),
    ])
    if total_pages > 1:
        page_row = []
        if page > 0:
            page_row.append(InlineKeyboardButton(f"◀ стр {page}", callback_data=f"earnings:{date_str}:{page-1}"))
        page_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            page_row.append(InlineKeyboardButton(f"стр {page+2} ▶", callback_data=f"earnings:{date_str}:{page+1}"))
        rows.append(page_row)
    rows.append([InlineKeyboardButton("📅 Выбрать дату", callback_data="earnings_pick")])
    rows.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def back_to_menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Главное меню", callback_data="menu"),
    ]])


# ---------------------------------------------------------------------------
# Screen renderers
# ---------------------------------------------------------------------------

async def show_menu(update: Update, worker_name: str, specialization: str,
                    role: str = '', edit: bool = False):
    text = (
        f"👷 {worker_name}\n"
        f"🔧 Специализация: {', '.join(specialization) if isinstance(specialization, list) else (specialization or '—')}\n"
        f"📅 Сегодня: {TODAY()}\n\n"
        f"Выберите действие:"
    )
    kb = main_menu_kb(role)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, reply_markup=kb)


async def show_projects(update: Update, edit: bool = False, archive: bool = False):
    projects = get_projects()
    active = [p for p in projects if p['status'] == 'АКТИВНЫЙ']
    archived = sorted((p for p in projects if p['status'] != 'АКТИВНЫЙ'),
                      key=lambda p: (p['project_name'] or '').casefold())
    if archive:
        text = f"📦 Архив проектов: {len(archived)}" if archived else "📦 Архив пуст."
        kb = projects_list_kb(archived, archive=True)
    else:
        lines = ["🗂 Управление проектами\n"]
        if active:
            lines.append(f"🟢 Активных: {len(active)}")
        if archived:
            lines.append(f"📦 В архиве: {len(archived)}")
        if not projects:
            lines.append("Проектов пока нет.\nДобавьте первый проект.")
        text = "\n".join(lines)
        kb = projects_list_kb(active, archived_count=len(archived))
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, reply_markup=kb)


async def show_earnings(update: Update, date_str: str = None, edit: bool = False, page: int = 0):
    from collections import defaultdict
    if date_str is None:
        date_str = date.today().isoformat()
    rows = get_earnings_detail_by_date(date_str)
    d = date.fromisoformat(date_str)
    date_label = d.strftime('%d.%m.%Y')
    if not rows:
        text = '📊 <b>Выработка за ' + date_label + '</b>\n\nДанных пока нет.'
    else:
        result_lines = ['📊 <b>Выработка за ' + date_label + '</b>\n']
        by_exec = defaultdict(list)
        for r in rows:
            by_exec[r['executor']].append(r)
        grand_total = 0.0
        for exec_name, recs in by_exec.items():
            exec_label = exec_name or 'не указан'
            result_lines.append('<b>👤 ' + exec_label + '</b>')
            exec_total = 0.0
            proj_map = defaultdict(list)
            for rec in recs:
                proj_map[rec['project_name']].append(rec)
            for proj, proj_recs in proj_map.items():
                result_lines.append('  📁 <i>' + proj + '</i>')
                sheet_map = defaultdict(list)
                for rec in proj_recs:
                    sheet_map[rec['sheet_name']].append(rec)
                for sn, sheet_recs in sheet_map.items():
                    icon = SHEET_ICONS.get(sn.upper(), '🔧')
                    sheet_total = sum(float(r['payment_sum'] or 0) for r in sheet_recs)
                    result_lines.append('  ' + icon + ' <b>' + sn + '</b>')
                    for rec in sheet_recs:
                        pos  = rec['position'] or rec['element'] or '—'
                        qty  = int(rec['quantity']) if rec['quantity'] and float(rec['quantity']) == int(float(rec['quantity'])) else rec['quantity']
                        ps   = float(rec['payment_sum'] or 0)
                        ps_s = f'{ps:.2f} руб' if ps else '—'
                        result_lines.append('    • ' + pos + ' × ' + str(qty) + ' шт — ' + ps_s)
                    result_lines.append('    <i>Итого ' + sn + ': ' + f'{sheet_total:.2f}' + ' руб</i>')
                    exec_total += sheet_total
            result_lines.append('  <b>Итого ' + exec_label + ': ' + f'{exec_total:.2f}' + ' руб</b>')
            grand_total += exec_total
            result_lines.append('')
        result_lines.append('💰 <b>Итого за день: ' + f'{grand_total:.2f}' + ' руб</b>')
        text = '\n'.join(result_lines)
    lines = text.split("\n")
    pages = _split_pages(lines, max_chars=3900) if len(text) > 3900 else [text]
    total = len(pages)
    page = max(0, min(page, total - 1))
    kb = earnings_date_kb(date_str, page=page, total_pages=total)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(pages[page], reply_markup=kb, parse_mode='HTML')
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(pages[page], reply_markup=kb, parse_mode='HTML')


async def show_tasks(update: Update, worker_name: str, specialization: str = None,
                     bot=None, chat_id=None, banner: str = None):
    tasks   = get_active_tasks(worker_name)
    blocked = get_blocked_tasks(worker_name)
    mandatory_left = mandatory_remaining(worker_name)

    if not tasks and not blocked:
        text = f"🎉 Все задачи выполнены!\n📅 {TODAY()}"
        if bot and chat_id:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=back_to_menu_kb())
        else:
            await update.callback_query.edit_message_text(text, reply_markup=back_to_menu_kb())
        return

    mandatory_tasks = [t for t in tasks if t['mandatory']]
    optional_tasks  = [t for t in tasks if not t['mandatory']]
    multi_spec = len({t['sheet_name'] for t in tasks}) > 1

    prefix = [banner, ""] if banner else []
    lines = prefix + [f"📋 Задачи на сегодня", f"📅 {TODAY()}\n"]

    if mandatory_tasks:
        lines.append("❗ Обязательные:")
        for t in mandatory_tasks:
            spec_tag = f"[{t['sheet_name']}] " if multi_spec else ""
            pos_part = f"{t['position']} — " if t['position'] else ""
            lines.append(f"  • {spec_tag}{pos_part}{t['element'] or ''} × {t['quantity'] or '?'} шт")

    if optional_tasks:
        lock = " 🔒 (после обязательных)" if mandatory_left else ""
        lines.append(f"\nОстальные{lock}:")
        for t in optional_tasks:
            spec_tag = f"[{t['sheet_name']}] " if multi_spec else ""
            pos_part = f"{t['position']} — " if t['position'] else ""
            lines.append(f"  • {spec_tag}{pos_part}{t['element'] or ''} × {t['quantity'] or '?'} шт")

    if blocked:
        lines.append("\n⛔ Заблокированные (снимает руководитель):")
        for t in blocked:
            comment = f" — {t['comment']}" if t['comment'] else ""
            pos_part = f"{t['position']} — " if t['position'] else ""
            lines.append(f"  • {pos_part}{t['element'] or ''}{comment}")

    lines.append("\n👇 Нажмите на задачу:")
    tasks_text = "\n".join(lines)
    if len(tasks_text) > 4000:
        tasks_text = tasks_text[:3950] + "\n…(список обрезан)"
    tasks_kb   = tasks_list_kb(tasks, blocked, mandatory_left)
    if bot and chat_id:
        await bot.send_message(chat_id=chat_id, text=tasks_text, reply_markup=tasks_kb)
    else:
        await update.callback_query.edit_message_text(tasks_text, reply_markup=tasks_kb)


async def show_task_detail(update: Update, task: dict, specialization: str):
    tariff = get_tariff(task.get('sheet_name', ''), task.get('unit_weight'))

    lines = []
    mandatory_mark = "❗ " if task['mandatory'] else ""
    _pos = task['position'] or task['element'] or ''
    lines.append(f"{mandatory_mark}[{task['sheet_name']}] {_pos}")
    lines.append(f"Элемент: {task['element'] or '—'}")
    lines.append(f"Проект: {task['project_name']}")
    qty_done_val = task.get('qty_done') or 0
    if task.get('status') == 'ЧАСТИЧНО' and qty_done_val and task['quantity']:
        qty_remaining = int(task['quantity'] - qty_done_val)
        lines.append(f"Кол-во: {qty_remaining} шт (осталось из {int(task['quantity'])})")
    else:
        lines.append(f"Кол-во: {task['quantity'] or '?'} шт")

    if task.get('unit_weight'):
        lines.append(f"Масса ед.: {task['unit_weight']} кг  |  Масса всего: {task['total_weight'] or '?'} кг")
    if task.get('date_plan'):
        lines.append(f"Дата план: {task['date_plan']}")
    lines.append(f"Обязательная: {'ДА' if task['mandatory'] else 'НЕТ'}")

    lines.append("")
    if tariff:
        lines.append(f"💰 Тариф: {tariff['rate']} {tariff['unit']}")
    if task.get('payment_sum'):
        lines.append(f"💵 К оплате: {task['payment_sum']} руб")

    await update.callback_query.edit_message_text(
        "\n".join(lines),
        reply_markup=task_detail_kb(
            task['id'],
            has_drawing=bool(task.get('drawing_link')),
            qty_total=task.get('quantity'),
            qty_done=task.get('qty_done') or 0,
        )
    )


async def show_done_today(update: Update, worker_name: str, specialization: str):
    tasks = get_done_today(worker_name, specialization)
    if not tasks:
        text = f"За сегодня ({TODAY()}) выполненных задач нет."
    else:
        total = sum(float(t['payment_sum'] or 0) for t in tasks)
        lines = [f"✅ Выполнено сегодня ({TODAY()}) — {len(tasks)} шт:\n"]
        for t in tasks:
            pay = f"  |  {t['payment_sum']} руб" if t['payment_sum'] else ""
            pos_part = f"{t['position']} — " if t['position'] else ""
            lines.append(f"✅ {pos_part}{t['element'] or ''} × {t['quantity'] or '?'} шт{pay}")
        if total > 0:
            lines.append(f"\n💵 Итого за день: {total:.2f} руб")
        text = "\n".join(lines)

    await update.callback_query.edit_message_text(text, reply_markup=back_to_menu_kb())


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Раздел «Сотрудники» — управление пользователями (только для мастеров)
# ---------------------------------------------------------------------------

def _get_pending_count() -> int:
    row = db.fetchone("SELECT COUNT(*) as cnt FROM pending_users WHERE status='pending'", [])
    return row['cnt'] if row else 0


def _emp_list_kb(employees: list, page: int = 0, inactive: bool = False) -> InlineKeyboardMarkup:
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    chunk = employees[start:start + PAGE_SIZE]
    rows = []
    for i in range(0, len(chunk), 2):
        pair = chunk[i:i+2]
        rows.append([
            InlineKeyboardButton(e['full_name'], callback_data=f"admin:user:{e['id']}")
            for e in pair
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"admin:users_page:{page-1}{'_i' if inactive else ''}"))
    if start + PAGE_SIZE < len(employees):
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"admin:users_page:{page+1}{'_i' if inactive else ''}"))
    if nav:
        rows.append(nav)
    bottom = []
    if not inactive:
        bottom.append(InlineKeyboardButton("👤 Неактивные", callback_data="admin:users_inactive"))
        pending = _get_pending_count()
        if pending:
            bottom.append(InlineKeyboardButton(f"📥 Заявки ({pending})", callback_data="admin:pending"))
    else:
        bottom.append(InlineKeyboardButton("← К активным", callback_data="admin:users"))
    rows.append(bottom)
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def show_emp_list(update: Update, page: int = 0, inactive: bool = False, edit: bool = True):
    employees = db.fetchall(
        "SELECT id, full_name FROM employees WHERE is_active=%s ORDER BY full_name",
        [not inactive]
    )
    title = "👥 Сотрудники" if not inactive else "👤 Неактивные сотрудники"
    if not employees:
        text = f"{title}\n\nСписок пуст."
    else:
        text = f"{title}\n\nВсего: {len(employees)}"
    kb = _emp_list_kb(employees, page=page, inactive=inactive)
    msg = update.callback_query.message
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.reply_text(text, reply_markup=kb)


def _spec_checkboxes_kb(emp_id: int, selected: list, save_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(SPECIALIZATIONS), 2):
        pair = SPECIALIZATIONS[i:i+2]
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if s in selected else '☐'} {s}",
                callback_data=f"admin:spec_toggle:{emp_id}:{s}"
            )
            for s in pair
        ])
    rows.append([
        InlineKeyboardButton("💾 Сохранить", callback_data=save_cb),
        InlineKeyboardButton("← Отмена",    callback_data=cancel_cb),
    ])
    return InlineKeyboardMarkup(rows)


async def show_emp_card(update: Update, emp_id: int, edit: bool = True):
    emp = db.fetchone(
        "SELECT id, full_name, role, specialization, is_active, telegram_id FROM employees WHERE id=%s",
        [emp_id]
    )
    if not emp:
        await update.callback_query.answer("Сотрудник не найден.", show_alert=True)
        return
    spec = emp['specialization'] or []
    spec_str = ', '.join(spec) if spec else '—'
    role_str = {'master': 'Мастер', 'admin': 'Администратор'}.get(emp['role'], 'Рабочий')
    active_str = '' if emp['is_active'] else '  · Неактивен'
    text = (
        f"👤 {emp['full_name']}{active_str}\n\n"
        f"Роль: {role_str}\n"
        f"Специализации: {spec_str}"
    )
    if emp['is_active']:
        open_orders = db.fetchone(
            "SELECT COUNT(*) as cnt FROM work_orders WHERE executor=%s AND status IN ('ПЛАН','ЧАСТИЧНО','БЛОК')",
            [emp['full_name']]
        )
        cnt = open_orders['cnt'] if open_orders else 0
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить имя",     callback_data=f"admin:name:{emp_id}"),
             InlineKeyboardButton("🎓 Специализации",    callback_data=f"admin:spec:{emp_id}")],
            [InlineKeyboardButton("👑 Изменить роль",    callback_data=f"admin:role:{emp_id}"),
             InlineKeyboardButton("🚫 Деактивировать",   callback_data=f"admin:deactivate:{emp_id}")],
            [InlineKeyboardButton("← Назад",             callback_data="admin:users")],
        ])
        if cnt > 0:
            text += f"\n\nОткрытых нарядов: {cnt}"
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Активировать", callback_data=f"admin:reactivate:{emp_id}")],
            [InlineKeyboardButton("← Назад",         callback_data="admin:users_inactive")],
        ])
    msg = update.callback_query.message
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.reply_text(text, reply_markup=kb)


async def show_spec_editor(update: Update, emp_id: int):
    emp = db.fetchone("SELECT full_name, specialization FROM employees WHERE id=%s", [emp_id])
    if not emp:
        await update.callback_query.answer("Сотрудник не найден.", show_alert=True)
        return
    selected = list(emp['specialization'] or [])
    text = f"Специализации — {emp['full_name']}:"
    kb = _spec_checkboxes_kb(emp_id, selected, f"admin:spec_save:{emp_id}", f"admin:user:{emp_id}")
    await update.callback_query.message.edit_text(text, reply_markup=kb)


async def _notify_masters_new_user(bot, pending_id: int, tg_name: str, is_reactivation: bool = False):
    masters = db.fetchall(
        "SELECT telegram_id FROM employees WHERE role='admin' AND is_active=true AND telegram_id IS NOT NULL",
        []
    )
    suffix = " (ранее был зарегистрирован)" if is_reactivation else ""
    text = (
        f"📥 Новый сотрудник хочет доступ{suffix}:\n"
        f"Telegram: {tg_name}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить",  callback_data=f"admin:approve:{pending_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:reject:{pending_id}"),
    ]])
    for m in masters:
        try:
            await bot.send_message(chat_id=m['telegram_id'], text=text, reply_markup=kb)
        except Exception:
            pass


def _approve_spec_kb(pending_id: int, selected: list) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(SPECIALIZATIONS), 2):
        pair = SPECIALIZATIONS[i:i+2]
        rows.append([
            InlineKeyboardButton(
                f"{'✅' if s in selected else '☐'} {s}",
                callback_data=f"admin:approve_spec_toggle:{pending_id}:{s}"
            )
            for s in pair
        ])
    rows.append([
        InlineKeyboardButton("Далее →", callback_data=f"admin:approve_spec_next:{pending_id}"),
    ])
    return InlineKeyboardMarkup(rows)


async def _do_approve_confirm(query, context, pending_id: int):
    emp_name = context.user_data.get('approve_name', '?')
    specs = context.user_data.get('approve_spec', [])
    role = context.user_data.get('approve_role', 'worker')
    role_str = {'master': 'Мастер', 'admin': 'Администратор'}.get(role, 'Рабочий')
    spec_str = ', '.join(specs) if specs else '—'
    text = (
        f"Подтвердите создание сотрудника:\n\n"
        f"Имя: {emp_name}\n"
        f"Роль: {role_str}\n"
        f"Специализации: {spec_str}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Создать", callback_data=f"admin:approve_confirm:{pending_id}"),
        InlineKeyboardButton("← Отмена",  callback_data=f"admin:pending"),
    ]])
    await query.edit_message_text(text, reply_markup=kb)


async def _do_approve_create(query, context, pending_id: int):
    pending = db.fetchone(
        "SELECT telegram_id, username, first_name, last_name FROM pending_users WHERE id=%s AND status='pending'",
        [pending_id]
    )
    if not pending:
        await query.edit_message_text("ℹ️ Этот сотрудник уже активирован другим мастером.")
        return
    emp_name = context.user_data.get('approve_name', '')
    specs    = context.user_data.get('approve_spec', [])
    role     = context.user_data.get('approve_role', 'worker')
    tg_id    = pending['telegram_id']
    username = pending['username']

    # Проверяем: уже есть в employees (деактивированный)?
    existing = db.fetchone("SELECT id FROM employees WHERE telegram_id=%s", [tg_id])
    try:
        if existing:
            db.execute(
                "UPDATE employees SET full_name=%s, role=%s, specialization=%s, is_active=true, "
                "telegram_username=%s WHERE id=%s",
                [emp_name, role, specs, username, existing['id']]
            )
        else:
            db.execute(
                "INSERT INTO employees (full_name, role, specialization, is_active, telegram_id, telegram_username) "
                "VALUES (%s, %s, %s, true, %s, %s)",
                [emp_name, role, specs, tg_id, username]
            )
    except Exception as e:
        if 'unique' in str(e).lower() and 'full_name' in str(e).lower():
            await query.edit_message_text(
                f"❌ Сотрудник с именем «{emp_name}» уже существует. Укажите другое имя."
            )
            return
        raise

    db.execute("UPDATE pending_users SET status='approved' WHERE id=%s", [pending_id])

    for k in ('approve_pending_id', 'approve_name', 'approve_spec', 'approve_role', 'approve_tg_name'):
        context.user_data.pop(k, None)

    await query.edit_message_text(f"✅ Сотрудник {emp_name} добавлен.")


async def receive_emp_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSM: мастер вводит новое имя существующего сотрудника."""
    emp_id = context.user_data.pop('edit_emp_id', None)
    name   = update.message.text.strip()
    if not emp_id:
        await update.message.reply_text("Ошибка: начните заново.")
        return ConversationHandler.END
    if not name:
        await update.message.reply_text("Имя не может быть пустым. Введите ещё раз:")
        context.user_data['edit_emp_id'] = emp_id
        return WAITING_EMP_NAME
    db.execute("UPDATE employees SET full_name=%s WHERE id=%s", [name, emp_id])
    await update.message.reply_text(f"✅ Имя обновлено: {name}")
    # Показываем карточку через новое сообщение
    class _FakeUpdate:
        class callback_query:
            message = update.message
            @staticmethod
            async def answer(*a, **kw): pass
        effective_user = update.effective_user
    await show_emp_card(_FakeUpdate(), emp_id, edit=False)
    return ConversationHandler.END


async def receive_approve_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSM: мастер вводит имя нового сотрудника в wizard одобрения."""
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Имя не может быть пустым. Введите ещё раз:")
        return WAITING_APPROVE_NAME
    context.user_data['approve_name'] = name
    pending_id = context.user_data.get('approve_pending_id')
    selected   = context.user_data.get('approve_spec', [])
    tg_name    = context.user_data.get('approve_tg_name', '')
    text = f"Специализации — {name}:\n(Telegram: {tg_name})"
    kb = _approve_spec_kb(pending_id, selected)
    await update.message.reply_text(text, reply_markup=kb)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /start — обработка незарегистрированных пользователей
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.username, user.id)

    app_logger.audit('bot_start', user.id, user.username,
                     {'found': bool(worker)}, 'success' if worker else 'not_found')

    if not worker:
        # Проверяем — может деактивирован
        deactivated = db.fetchone(
            "SELECT id, full_name FROM employees WHERE telegram_id=%s AND is_active=false",
            [user.id]
        )
        if not deactivated and user.username:
            deactivated = db.fetchone(
                "SELECT id, full_name FROM employees WHERE telegram_username=%s AND is_active=false",
                [user.username]
            )
        if deactivated:
            # Деактивированный — создаём заявку если ещё нет активной
            existing = db.fetchone(
                "SELECT id FROM pending_users WHERE telegram_id=%s AND status='pending'",
                [user.id]
            )
            if existing:
                await update.message.reply_text("⏳ Ваша заявка уже отправлена. Ожидайте активации.")
            else:
                tg_name = user.full_name + (f" (@{user.username})" if user.username else f" (id:{user.id})")
                row = db.fetchone(
                    "INSERT INTO pending_users (telegram_id, username, first_name, last_name) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    [user.id, user.username, user.first_name, user.last_name]
                )
                await _notify_masters_new_user(update.get_bot(), row['id'], tg_name, is_reactivation=True)
                await update.message.reply_text("⏳ Ваша заявка отправлена мастеру. Ожидайте активации.")
            return
        # Совсем новый пользователь
        existing = db.fetchone(
            "SELECT id FROM pending_users WHERE telegram_id=%s AND status='pending'",
            [user.id]
        )
        if existing:
            await update.message.reply_text("⏳ Ваша заявка уже отправлена. Ожидайте активации.")
        else:
            tg_name = user.full_name + (f" (@{user.username})" if user.username else f" (id:{user.id})")
            row = db.fetchone(
                "INSERT INTO pending_users (telegram_id, username, first_name, last_name) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                [user.id, user.username, user.first_name, user.last_name]
            )
            await _notify_masters_new_user(update.get_bot(), row['id'], tg_name)
            await update.message.reply_text("⏳ Ваша заявка отправлена мастеру. Ожидайте активации.")
        return

    # Сохраняем числовой chat_id — он надёжнее @username для уведомлений
    db.execute(
        "UPDATE employees SET telegram_id=%s WHERE full_name=%s",
        [user.id, worker['full_name']]
    )
    context.user_data['worker_name']    = worker['full_name']
    context.user_data['specialization'] = worker['specialization']
    context.user_data['role']           = worker.get('role', '')
    await show_menu(update, worker['full_name'], worker['specialization'], worker.get('role', ''))


def _get_worker_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker_name    = context.user_data.get('worker_name')
    specialization = context.user_data.get('specialization')
    role           = context.user_data.get('role', '')
    if not worker_name:
        user = update.effective_user
        worker = get_worker(user.username, user.id)
        if not worker:
            return None, None, None
        worker_name    = worker['full_name']
        specialization = worker['specialization']
        role           = worker.get('role', '')
        context.user_data['worker_name']    = worker_name
        context.user_data['specialization'] = specialization
        context.user_data['role']           = role
    return worker_name, specialization, role


SHEET_ICONS = {
    'ПЛАЗМА': '🔥', 'ПИЛА': '🪚', 'СВЕРЛЕНИЕ': '🔩',
    'СБОРКА': '🔧', 'СВАРКА': '⚡',
    'ГРУНТОВКА': '🖌', 'ПОКРАСКА': '🎨',
}

SHEETS_WITH_DEPS = {'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА'}

def plans_date_kb(date_str: str, page: int = 0, total_pages: int = 1):
    from datetime import timedelta
    d = date.fromisoformat(date_str)
    prev_str = (d - timedelta(days=1)).isoformat()
    next_str = (d + timedelta(days=1)).isoformat()
    prev_label = (d - timedelta(days=1)).strftime('← %d.%m')
    next_label = (d + timedelta(days=1)).strftime('%d.%m →')
    rows = []
    rows.append([
        InlineKeyboardButton(prev_label, callback_data=f"plans:{prev_str}"),
        InlineKeyboardButton(next_label, callback_data=f"plans:{next_str}"),
    ])
    if total_pages > 1:
        page_row = []
        if page > 0:
            page_row.append(InlineKeyboardButton(f"◀ стр {page}", callback_data=f"plans:{date_str}:{page-1}"))
        page_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            page_row.append(InlineKeyboardButton(f"стр {page+2} ▶", callback_data=f"plans:{date_str}:{page+1}"))
        rows.append(page_row)
    rows.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)

def _split_pages(lines: list, max_chars: int = 3900) -> list:
    """Разбивает список строк на страницы по max_chars символов."""
    pages, current, length = [], [], 0
    for line in lines:
        chunk = line + "\n"
        if length + len(chunk) > max_chars and current:
            pages.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(chunk)
    if current:
        pages.append("\n".join(current))
    return pages or [""]


def _available_qty_map(rows):
    """Для списка задач плана вычисляет доступное (не заблокированное) количество.
    Возвращает dict: (project_name, sheet_name, position_or_element) -> avail_qty (float).
    Если зависимостей нет или всё выполнено — возвращает полное quantity.
    """
    if not rows:
        return {}
    import math as _math_avq
    # Собираем уникальные задачи с зависимостями
    keys = set()
    for r in rows:
        keys.add((r['project_name'], r['sheet_name'], r['position'] or r['element'] or ''))
    result = {}
    for (proj, sheet, lookup) in keys:
        if not lookup:
            continue
        # Зависимости для этого элемента
        deps = db.fetchall(
            """SELECT requires_sheet, requires_position
               FROM element_dependencies
               WHERE project_name=%s AND waiting_sheet=%s AND element=%s""",
            [proj, sheet, lookup]
        )
        if not deps:
            continue  # нет зависимостей — используем quantity как есть
        # own total qty в текущем листе (sum всех строк)
        own_row = db.fetchone(
            """SELECT COALESCE(SUM(quantity), 0) as total,
                      COALESCE(SUM(CASE WHEN status='ВЫПОЛНЕНО' THEN quantity ELSE 0 END), 0) as done
               FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND (position=%s OR element=%s)""",
            [proj, sheet, lookup, lookup]
        )
        own_total = float(own_row['total'] or 0)
        own_done = float(own_row['done'] or 0)
        if own_total == 0:
            continue
        _pres = deps_parts.compute(proj, sheet, lookup, int(own_total - own_done) or None)
        min_avail = own_total
        all_done = True
        seen_sheets = set(_pres['covered']) if _pres else set()
        for d in deps:
            rs = d['requires_sheet']
            if rs in seen_sheets:
                continue
            seen_sheets.add(rs)
            rp = d['requires_position']
            up_row = db.fetchone(
                """SELECT COALESCE(SUM(quantity), 0) as total,
                          COALESCE(SUM(CASE WHEN status='ВЫПОЛНЕНО' THEN quantity ELSE 0 END), 0) as done
                   FROM work_orders
                   WHERE project_name=%s AND sheet_name=%s AND (element=%s OR position=%s)""",
                [proj, rs, rp, rp]
            )
            up_total = float(up_row['total'] or 0)
            up_done = float(up_row['done'] or 0)
            if up_total == 0:
                continue
            if up_done < up_total:
                all_done = False
                avail_raw = _math_avq.floor(up_done * own_total / up_total)
                if avail_raw < min_avail:
                    min_avail = avail_raw
        if _pres and not _pres['blocked'] and all_done:
            continue  # всё разблокировано — не переопределяем
        if all_done and not _pres:
            continue  # всё разблокировано — не переопределяем
        # Доступно = avail_raw - уже выполнено в текущем листе
        net = (min_avail - own_done) if not all_done else float('inf')
        if _pres:
            net = min(net, float(_pres['avail']))
        if net < 0:
            net = 0
        result[(proj, sheet, lookup)] = net
    return result


async def show_plans(update: Update, date_str: str = None, edit: bool = False, page: int = 0):
    from datetime import timedelta
    if date_str is None:
        date_str = date.today().isoformat()
    d = date.fromisoformat(date_str)
    today = date.today()
    STATUS_ICON = {'ПЛАН': '☐', 'ВЫПОЛНЕНО': '✅', 'БЛОК': '⛔', 'ЧАСТИЧНО': '◧'}

    if d == today:
        rows = db.fetchall(
            """SELECT project_name, sheet_name, executor, position, element, quantity, status, date_plan,
                      payment_sum,
                      (date_plan < CURRENT_DATE) AS overdue
               FROM work_orders
               WHERE executor IS NOT NULL AND executor != ''
                 AND (date_plan = CURRENT_DATE
                  OR (date_plan < CURRENT_DATE AND status IN ('ПЛАН', 'ЧАСТИЧНО')))
               ORDER BY project_name, sheet_name, executor,
                        CASE status WHEN 'БЛОК' THEN 0 WHEN 'ПЛАН' THEN 1 WHEN 'ЧАСТИЧНО' THEN 2 ELSE 3 END,
                        date_plan, position""",
            []
        )
    else:
        rows = db.fetchall(
            """SELECT project_name, sheet_name, executor, position, element, quantity, status, date_plan,
                      payment_sum,
                      false AS overdue
               FROM work_orders
               WHERE executor IS NOT NULL AND executor != ''
                 AND date_plan = %s
               ORDER BY project_name, sheet_name, executor,
                        CASE status WHEN 'БЛОК' THEN 0 WHEN 'ПЛАН' THEN 1 WHEN 'ЧАСТИЧНО' THEN 2 ELSE 3 END,
                        position""",
            [d]
        )

    delta = (d - today).days
    if delta == 0:
        day_label = "сегодня"
    elif delta == 1:
        day_label = "завтра"
    elif delta == 2:
        day_label = "послезавтра"
    elif delta == -1:
        day_label = "вчера"
    elif delta > 2:
        day_label = f"+{delta} дн."
    else:
        day_label = f"{delta} дн."
    date_label = f"{d.strftime('%d.%m')} ({day_label})"

    if not rows:
        text = chr(10).join([f"📅 Планы — {date_label}", "", "Нет задач на эту дату."])
    else:
        from collections import defaultdict
        by_proj = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for r in rows:
            by_proj[r['project_name']][r['sheet_name']][r['executor']].append(r)

        _avail_map = _available_qty_map(rows)
        lines = [f"📅 Планы — {date_label}"]
        SHEET_ORDER = ['ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА']
        grand_total = 0.0
        for proj_name in sorted(by_proj.keys()):
            lines.append("")
            lines.append(f"📁 {proj_name}")
            for sheet in SHEET_ORDER:
                if sheet not in by_proj[proj_name]:
                    continue
                icon = SHEET_ICONS.get(sheet, '🔧')
                lines.append(f"  {icon} {sheet}")
                for executor, tasks in by_proj[proj_name][sheet].items():
                    lines.append(f"    👷 {executor}")
                    exec_total = 0.0
                    for t in tasks:
                        _avq_key = (t['project_name'], t['sheet_name'], t['position'] or t['element'] or '')
                        _avq = _avail_map.get(_avq_key)
                        _qty_display = _avq if _avq is not None else float(t['quantity'] or 0)
                        qty = f" × {int(_qty_display)}" if _qty_display else ""
                        elem = f" ({t['element']})" if t['element'] else ""
                        sicon = STATUS_ICON.get(t['status'], '☐')
                        overdue_mark = f" (от {t['date_plan'].strftime('%d.%m')})" if t.get('overdue') else ""
                        pos_label = t["position"] if t["position"] else (t["element"] or "—")
                        elem_part = elem if t["position"] else ""
                        lines.append(f"      {sicon} {pos_label}{elem_part}{qty}{overdue_mark}")
                        if t['status'] != 'БЛОК':
                            exec_total += float(t['payment_sum'] or 0)
                    if exec_total > 0:
                        lines.append(f"      💵 {exec_total:.2f} руб")
                    grand_total += exec_total
        if grand_total > 0:
            lines.append("")
            lines.append(f"💰 Итого за день: {grand_total:.2f} руб")
        text = chr(10).join(lines)

    pages = _split_pages(text.split("\n")) if len(text) > 3900 else [text]
    total = len(pages)
    page = max(0, min(page, total - 1))
    kb = plans_date_kb(date_str, page=page, total_pages=total)
    if edit:
        await update.callback_query.edit_message_text(pages[page], reply_markup=kb)
    else:
        await update.effective_message.reply_text(pages[page], reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        # callback query expired (>48h) — сообщаем пользователю и выходим
        try:
            await query.message.reply_text(
                "⚠️ Вы нажали на кнопку из старого сообщения. "
                "Пожалуйста, откройте меню заново — /start"
            )
        except Exception:
            pass
        return
    user = update.effective_user
    data = query.data

    worker_name, specialization, role = _get_worker_context(update, context)
    if not worker_name:
        await query.edit_message_text("Сессия устарела. Нажмите /start")
        return

    app_logger.audit('button', user.id, user.username, {'data': data})

    if data == "menu":
        await show_menu(update, worker_name, specialization, role, edit=True)

    elif data.startswith("plans:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        parts = data.split(":")
        # plans:{date_str} или plans:{date_str}:{page}
        date_str = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_plans(update, date_str, edit=True, page=page)

    elif data == "noop":
        await query.answer()
        return

    elif data == "run_monitor":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        await query.answer("Запускаю мониторинг…")
        await query.edit_message_text("⏳ Запускаю мониторинг листов…")
        try:
            import subprocess, sys as _sys
            result = subprocess.run(
                [_sys.executable, '/root/naryady/prod/tools/sheet_monitor.py'],
                capture_output=True, text=True, timeout=300,
                cwd='/root/naryady/prod'
            )
            output = (result.stdout or '').strip() or (result.stderr or '').strip() or 'Готово'
            await query.edit_message_text(
                '✅ Мониторинг завершён. ' + output[:200],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Меню", callback_data="menu")]])
            )
        except Exception as _me:
            await query.edit_message_text(
                '❌ Ошибка мониторинга: ' + str(_me)[:300],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Меню", callback_data="menu")]])
            )

    elif data == "projects":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        await show_projects(update, edit=True)

    elif data == "projects:archive":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        await show_projects(update, edit=True, archive=True)

    elif data == "project:add":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        await query.edit_message_text(
            "📁 Новый проект\n\n"
            "Отправьте ссылку на папку проекта в Google Drive.\n\n"
            "Бот сам найдёт Excel-файлы с деталями, распознает специализации и создаст таблицу нарядов.\n\n"
            "Формат: https://drive.google.com/drive/folders/...",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="projects")
            ]])
        )
        return WAITING_PROJECT_LINK

    elif data.startswith("project:detail:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT * FROM projects WHERE id=%s", [project_id])
        if not project:
            await query.answer("Проект не найден.", show_alert=True)
            return
        synced_at = project.get('last_synced_at')
        synced_str = synced_at.strftime('%d.%m.%Y %H:%M') if synced_at else 'ещё не синхронизировался'

        sheet_stats = db.fetchall("""
            SELECT sheet_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status='ВЫПОЛНЕНО') AS done,
                   COUNT(*) FILTER (WHERE status='БЛОК') AS blocked
            FROM work_orders WHERE project_id=%s
            GROUP BY sheet_name ORDER BY sheet_name
        """, [project_id])

        total_all = sum(r['total'] for r in sheet_stats)
        done_all  = sum(r['done']  for r in sheet_stats)
        blocked_all = sum(r['blocked'] for r in sheet_stats)

        overdue = db.fetchone("""
            SELECT COUNT(*) AS cnt FROM work_orders
            WHERE project_id=%s AND status != 'ВЫПОЛНЕНО'
              AND date_plan IS NOT NULL AND date_plan < CURRENT_DATE
        """, [project_id])
        overdue_cnt = overdue['cnt'] if overdue else 0

        payment = db.fetchone("""
            SELECT COALESCE(SUM(CASE WHEN status='ВЫПОЛНЕНО' THEN payment_sum ELSE 0 END), 0) AS done
            FROM work_orders WHERE project_id=%s
        """, [project_id])

        pct_all = int(done_all / total_all * 100) if total_all else 0
        status_icon = '🟢' if project['status'] == 'АКТИВНЫЙ' else '📦'

        out = [f"🏗 {project['project_name']}"]
        out.append(f"{status_icon} {'АКТИВНЫЙ' if project['status'] == 'АКТИВНЫЙ' else 'АРХИВ'}  |  🔄 {synced_str}")
        out.append("━━━━━━━━━━━━━━━━")
        out.append(f"📊 Прогресс: {done_all}/{total_all} ({pct_all}%)")
        out.append("")
        out.append("🔧 По цехам:")

        SHEET_ORDER = ['ПЛАЗМА', 'ПИЛА', 'СВЕРЛЕНИЕ', 'СБОРКА', 'СВАРКА', 'ГРУНТОВКА', 'ПОКРАСКА']
        stats_map = {r['sheet_name']: r for r in sheet_stats}
        for sheet in SHEET_ORDER:
            if sheet not in stats_map:
                continue
            r = stats_map[sheet]
            pct = int(r['done'] / r['total'] * 100) if r['total'] else 0
            filled = int(pct / 10)
            bar = '█' * filled + '░' * (10 - filled)
            out.append(f"  {sheet:<12} {bar} {r['done']}/{r['total']}")

        if blocked_all:
            out.append("")
            out.append(f"🚫 Блоков: {blocked_all}")
        if overdue_cnt:
            out.append(f"⚠️ Просрочено: {overdue_cnt}")
        if payment and payment['done']:
            out.append(f"💰 Выполнено: {float(payment['done']):.0f} руб")

        await query.edit_message_text(
            chr(10).join(out),
            reply_markup=project_detail_kb(project_id, project['status'])
        )

    elif data.startswith("project:sync:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT project_name, sheet_id FROM projects WHERE id=%s", [project_id])
        if not project:
            await query.answer("Проект не найден.", show_alert=True)
            return
        await query.answer("🔄 Запускаю синхронизацию...")

        # Живое отображение прогресса: синк идёт отдельным процессом и пишет прогресс в файл
        import time as _time_s, json as _json_ps
        proj_name = project['project_name']
        pfile = f"/tmp/sync_progress_{project_id}_{int(_time_s.time())}.jsonl"
        sheet_lines: list[str] = []
        header = f"🔄 Синхронизация: {proj_name}\n"
        try:
            proc = _spawn_sync(project_id, pfile, force=True)
        except Exception as e:
            app_logger.alert(f"sync spawn error: {e}")
            await query.answer("Не удалось запустить синхронизацию.", show_alert=True)
            return
        pos, finished, total_rows, last_text = 0, False, 0, None
        deadline = _time_s.time() + 900
        while _time_s.time() < deadline:
            await asyncio.sleep(3)
            exited = proc.poll() is not None
            bar_line = ''
            try:
                with open(pfile, 'r', encoding='utf-8') as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except FileNotFoundError:
                chunk = ''
            for ln in chunk.splitlines():
                try:
                    ev = _json_ps.loads(ln)
                except Exception:
                    continue
                ph, dt = ev.get('phase'), ev.get('data') or {}
                if ph == 'sheet_done':
                    rows, splits = dt.get('rows', 0), dt.get('splits', 0)
                    line = f"{'✅' if rows > 0 else '➖'} {dt.get('sheet')}: {rows} стр."
                    if dt.get('skipped'):
                        line += " (без изменений)"
                    if splits:
                        line += f" (+{splits} сплит)"
                    sheet_lines.append(line)
                    done, total = dt.get('sheets_done', 0), dt.get('total_sheets', 0)
                    filled = int(done / total * 10) if total else 0
                    bar_line = f"\n⏳ [{'▓' * filled + '░' * (10 - filled)}] {done}/{total}"
                elif ph == 'deps_done':
                    sheet_lines.append("\n🔗 Зависимости обновлены")
                elif ph == 'sync_done':
                    finished, total_rows = True, dt.get('total', 0)
            text = header + "\n".join(sheet_lines[-12:]) + bar_line
            if finished:
                text += f"\n\n✅ Готово! Обработано строк: {total_rows}"
            elif exited:
                text += "\n\n❌ Синхронизация завершилась с ошибкой, подробности в логе."
            if text != last_text:
                try:
                    await query.edit_message_text(
                        text, reply_markup=project_detail_kb(project_id, 'АКТИВНЫЙ') if (finished or exited) else None)
                except Exception:
                    pass
                last_text = text
            if finished or exited:
                break

    elif data.startswith("project:archive:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT project_name FROM projects WHERE id=%s", [project_id])
        if not project:
            await query.answer("Проект не найден.", show_alert=True)
            return
        db.execute("UPDATE projects SET status='АРХИВ' WHERE id=%s", [project_id])
        await query.answer(f"Проект «{project['project_name']}» перемещён в архив.")
        await show_projects(update, edit=True)

    elif data.startswith("project:restore:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT project_name FROM projects WHERE id=%s", [project_id])
        if not project:
            await query.answer("Проект не найден.", show_alert=True)
            return
        db.execute("UPDATE projects SET status='АКТИВНЫЙ' WHERE id=%s", [project_id])
        await query.answer(f"Проект «{project['project_name']}» восстановлен.")
        await show_projects(update, edit=True)

    elif data.startswith("project:delete:") and "confirm" not in data:
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT project_name FROM projects WHERE id=%s", [project_id])
        if not project:
            await query.answer("Проект не найден.", show_alert=True)
            return
        name = project['project_name']
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, удалить", callback_data=f"project:delete_confirm:{project_id}")],
            [InlineKeyboardButton("← Отмена", callback_data=f"project:detail:{project_id}")],
        ])
        text = ('⚠️ <b>Удаление проекта</b>\n\n' + f'«{name}»\n\n' + 'Будут удалены:\n' + '• все наряды из БД\n' + '• зависимости\n' + '• запись проекта\n' + '• таблица Google Sheets\n\n' + 'Это действие <b>необратимо</b>. Продолжить?')
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)

    elif data.startswith("project:delete_confirm:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT id, project_name, sheet_id FROM projects WHERE id=%s", [project_id])
        if not project:
            await query.answer("Проект не найден.", show_alert=True)
            return
        name = project['project_name']
        sheet_id = project['sheet_id']
        # Удаление из БД
        db.execute("DELETE FROM element_dependencies WHERE project_name=%s", [name])
        db.execute("DELETE FROM work_orders WHERE project_id=%s", [project_id])
        db.execute("DELETE FROM projects WHERE id=%s", [project_id])
        # Удаление таблицы из Drive
        drive_err = None
        try:
            drive = await _bg(_wiz._drive_oauth)
            await _bg(drive.files().delete(fileId=sheet_id, supportsAllDrives=True).execute)
        except Exception as e:
            drive_err = str(e)
            logger.warning("Не удалось удалить Sheets файл %s: %s", sheet_id, e)
        msg = f"✅ Проект «{name}» удалён."
        if drive_err:
            msg += "\n\u26a0\ufe0f \u0422\u0430\u0431\u043b\u0438\u0446\u0430 \u0438\u0437 Drive \u043d\u0435 \u0443\u0434\u0430\u043b\u0435\u043d\u0430: " + drive_err
        await query.edit_message_text(msg, parse_mode='HTML')
        await show_projects(update)

    elif data.startswith("master:blocks"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        parts = data.split(":")
        page = int(parts[2]) if len(parts) > 2 else 0
        PAGE_SIZE = 10
        blocked = get_all_blocked()
        if not blocked:
            await query.edit_message_text(
                "✅ Активных блоков нет.",
                reply_markup=back_to_menu_kb()
            )
            return
        total = len(blocked)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        chunk = blocked[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = [f"🚫 Активные блоки — {total} шт (стр. {page+1}/{total_pages})\n"]
        for b in chunk:
            when = b['updated_at'].strftime('%d.%m %H:%M') if b['updated_at'] else '—'
            lines.append(
                f"• [{b['sheet_name']}] {b['position']} — {b['element'] or '—'}\n"
                f"  👷 {b['executor']} | 📁 {b['project_name']}\n"
                f"  💬 {b['comment'] or '—'} | 🕐 {when}"
            )
        buttons = []
        for b in chunk:
            label = f"✅ Снять: {b['position']} — {b['element'] or ''} ({b['sheet_name']})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"unblock:{b['id']}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("← Пред", callback_data=f"master:blocks:{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("След →", callback_data=f"master:blocks:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
        await query.edit_message_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif data.startswith("earnings:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        parts = data.split(":")
        # earnings:{date_str} или earnings:{date_str}:{page}
        date_str = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_earnings(update, date_str, edit=True, page=page)

    elif data == "earnings_pick":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        await query.edit_message_text(
            '📅 Введите дату в формате ДД.ММ.ГГГГ\n\nНапример: 14.07.2026\n\n/cancel — отмена'
        )
        return WAITING_EARNINGS_DATE

    elif data == "tasks":
        await show_tasks(update, worker_name, specialization)

    elif data == "done_today":
        await show_done_today(update, worker_name, specialization)

    elif data == "blocked_info":
        await query.answer("Задача заблокирована. Обратитесь к руководителю.", show_alert=True)

    elif data.startswith("task:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone("SELECT * FROM work_orders WHERE id=%s", [task_id])
        if not task:
            await query.edit_message_text("Задача не найдена.", reply_markup=back_to_tasks_kb())
            return

        try:
            _task_deps_ready = task['sheet_name'].upper() not in SHEETS_WITH_DEPS or _deps_ready(task['project_name'], task['element'], task['sheet_name'])
            _task_blocking = [] if _task_deps_ready else _deps_blocking(task['project_name'], task['element'], task['sheet_name'])
        except Exception as e:
            app_logger.alert(f"_deps_ready/_deps_blocking error task_id={task_id}: {e}")
            _task_deps_ready = True
            _task_blocking = []
        if not _task_deps_ready:
            _tqty  = int(task['quantity'] or 0)
            _avail, _ = _deps_available_qty(
                task['project_name'], task['element'], task['sheet_name'], _tqty
            )
            if _avail > 0:
                context.user_data['partial_deps_avail'] = _avail
                lines = [
                    f"✅ Задача частично доступна",
                    f"Позиция: {task['position']} — {task['element'] or ''}",
                    f"Готово деталей: {_avail} из {_tqty} шт",
                    "",
                    "Можно начать выполнение на доступное количество.",
                ]
                await query.answer()
                await query.edit_message_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"◧ Частично (макс {_avail} шт)",
                                             callback_data=f"partial_ask:{task_id}")],
                        [InlineKeyboardButton("← К задачам", callback_data="tasks")],
                    ])
                )
                return
            blocking = _task_blocking
            lines = ["⏳ Задача ещё не доступна", f"Элемент: {task['element']}", "Ожидает выполнения:"]
            for b in blocking:
                lines.append(f"  ☐ {b}")
            await query.answer()
            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← К задачам", callback_data="tasks")]])
            )
            return

        if not task['mandatory']:
            mandatory_left = mandatory_remaining(worker_name, specialization)
            if mandatory_left:
                lines = ["\u26a0\ufe0f Сначала выполните обязательные задачи:"]
                for p in mandatory_left:
                    lines.append(f"  • {p}")
                await query.answer()
                await query.edit_message_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("← К задачам", callback_data="tasks")]
                    ])
                )
                return

        app_logger.audit('view_task', user.id, user.username, {'task_id': task_id}, 'success')
        try:
            await show_task_detail(update, task, specialization)
        except Exception as e:
            if 'Message is not modified' in str(e):
                return  # безвредно — контент уже актуален
            app_logger.alert(f"show_task_detail error task_id={task_id}: {e}")
            await query.edit_message_text(
                "⚠️ Не удалось открыть задачу. Администратор уведомлён.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← К задачам", callback_data="tasks")]])
            )

    elif data.startswith("done:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone(
            "SELECT position, element, quantity, payment_sum, executor, file_id, sheet_name, row_num, project_name FROM work_orders WHERE id=%s",
            [task_id]
        )
        if not task or task['executor'] != worker_name:
            await query.edit_message_text("Ошибка доступа.", reply_markup=back_to_tasks_kb())
            return

        today_str = date.today().strftime('%d.%m.%Y')

        # Guard: если детали доступны только частично — запрещаем полное выполнение
        if task.get('element') and task.get('sheet_name', '').upper() in SHEETS_WITH_DEPS:
            _tqty_d = int(task['quantity'] or 0)
            _avail_d, _ = _deps_available_qty(
                task['project_name'], task['element'], task['sheet_name'], _tqty_d
            )
            if _avail_d < _tqty_d:
                await query.answer(
                    f"Доступно только {_avail_d} из {_tqty_d} шт. Используйте '◧ Частично'.",
                    show_alert=True
                )
                return

        # 1. Обновляем PostgreSQL
        db.execute(
            "UPDATE work_orders SET status='ВЫПОЛНЕНО', date_fact=CURRENT_DATE WHERE id=%s AND status IN ('ПЛАН','ЧАСТИЧНО')",
            [task_id]
        )

        # 2. Обновляем Google Sheets
        try:
            update_task_status(
                file_id=task['file_id'],
                sheet_name=task['sheet_name'],
                row_num=task['row_num'],
                status='ВЫПОЛНЕНО',
                date_fact=today_str
            )
        except Exception as e:
            app_logger.alert(f"Sheets write error: {e}")

        app_logger.audit('set_done', user.id, user.username, {'task_id': task_id}, 'success')

        # 3. Проверяем не разблокировалась ли сборка по этому элементу
        if task.get('element') and task.get('sheet_name', '').upper() != 'СБОРКА':
            if _deps_ready(task['project_name'], task['element']):
                try:
                    await notify_assembly_workers(context.bot, task['project_name'], task['element'])
                except Exception as e:
                    app_logger.alert(f"notify_assembly_workers error: {e}")

        # 4. Разблокируем зависимые задачи следующего уровня
        if task.get('project_name'):
            try:
                await notify_deps_unblocked(
                    context.bot, task['project_name'], task['sheet_name'],
                    task['position'], int(task['quantity'] or 0),
                    completed_element=task.get('element'),
                )
            except Exception as e:
                app_logger.alert(f"notify_deps_unblocked error: {e}")

        # 4. Редактируем карточку — показываем список с баннером "выполнено"
        pay = f" | 💵 {task['payment_sum']} руб" if task['payment_sum'] else ""
        banner = f"✅ {task['position']} — {task['element'] or ''} | Выполнено{pay}"
        await show_tasks(update, worker_name, specialization, banner=banner)

    elif data.startswith("pconf:yes:"):
        import time as _t_pc2
        _pp = data.split(":")
        _tid, _q = int(_pp[2]), int(_pp[3])
        _pc = context.user_data.pop('partial_confirm', None)
        if (not _pc or _pc.get('task_id') != _tid or _pc.get('qty') != _q
                or _t_pc2.time() - _pc.get('ts', 0) > 600):
            await query.answer("Подтверждение устарело. Откройте задачу заново.", show_alert=True)
            return
        await query.answer("Записываю…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _apply_partial(update, context, _tid, _q)

    elif data.startswith("partial_ask:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone(
            "SELECT position, element, quantity, qty_done FROM work_orders WHERE id=%s",
            [task_id]
        )
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        qty_total = int(task['quantity'] or 0)
        qty_done_already = int(task['qty_done'] or 0)
        remaining = qty_total - qty_done_already
        deps_avail = context.user_data.pop('partial_deps_avail', None)
        if deps_avail is None:
            task_meta = db.fetchone(
                "SELECT project_name, sheet_name FROM work_orders WHERE id=%s", [task_id]
            )
            if task_meta and task.get('element'):
                deps_avail, _ = _deps_available_qty(
                    task_meta['project_name'], task['element'], task_meta['sheet_name'], qty_total
                )
        max_allowed = min(remaining, deps_avail) if (deps_avail is not None and deps_avail < remaining) else remaining
        context.user_data['partial_task_id'] = task_id
        context.user_data['partial_deps_avail'] = max_allowed
        hint = (f"\n⚠️ Доступно: {max_allowed} шт (ждём остальные детали)\n"
                if max_allowed < remaining else "\n")
        await query.edit_message_text(
            f"◧ Частичное выполнение\n\n"
            f"Позиция: {task['position']} — {task['element'] or ''}\n"
            f"Всего: {qty_total} шт | Осталось: {remaining} шт"
            f"{hint}"
            f"Сколько выполнили? (от 1 до {max_allowed})",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="tasks")]])
        )
        return WAITING_PARTIAL_QTY

    elif data.startswith("drawing:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone("SELECT * FROM work_orders WHERE id=%s", [task_id])
        if not task or not task.get('drawing_link'):
            await query.answer("Чертёж не прикреплён.", show_alert=True)
            return
        m = re.search(r'/d/([a-zA-Z0-9_-]+)', task['drawing_link'])
        if not m:
            await query.answer("Некорректная ссылка на чертёж.", show_alert=True)
            return
        await query.answer("Загружаю чертёж...")
        try:
            file_bytes, _, mime = await asyncio.get_event_loop().run_in_executor(
                None, _download_drawing, m.group(1)
            )
            base_name = f"{task['position']} — {task['element'] or ''}"
            ext_map = {'image/png': 'png', 'image/jpeg': 'jpg', 'application/pdf': 'pdf'}
            ext = ext_map.get(mime, 'file')
            await context.bot.send_document(
                chat_id=user.id,
                document=io.BytesIO(file_bytes),
                filename=f"{base_name}.{ext}",
                caption=f"📄 {base_name}"
            )
            app_logger.audit('view_drawing', user.id, user.username, {'task_id': task_id}, 'success')
            # Re-send task card as new message so user doesn't scroll up
            task_full = db.fetchone("SELECT * FROM work_orders WHERE id=%s", [task_id])
            if task_full:
                from config import WORK_SHEETS
                specialization = task_full.get('sheet_name', '')
                tariff = get_tariff(specialization, task_full.get('unit_weight'))
                lines = []
                mandatory_mark = "❗ " if task_full['mandatory'] else ""
                lines.append(f"{mandatory_mark}[{task_full['sheet_name']}] {task_full['position']}")
                lines.append(f"Элемент: {task_full['element'] or '—'}")
                lines.append(f"Проект: {task_full['project_name']}")
                lines.append(f"Кол-во: {task_full['quantity'] or '?'} шт")
                if task_full.get('unit_weight'):
                    lines.append(f"Масса ед.: {task_full['unit_weight']} кг  |  Масса всего: {task_full['total_weight'] or '?'} кг")
                if task_full.get('date_plan'):
                    lines.append(f"Дата план: {task_full['date_plan']}")
                lines.append(f"Обязательная: {'ДА' if task_full['mandatory'] else 'НЕТ'}")
                lines.append("")
                if tariff:
                    lines.append(f"💰 Тариф: {tariff['rate']} {tariff['unit']}")
                if task_full.get('payment_sum'):
                    lines.append(f"💵 К оплате: {task_full['payment_sum']} руб")
                await context.bot.send_message(
                    chat_id=user.id,
                    text=chr(10).join(lines),
                    reply_markup=task_detail_kb(task_id, has_drawing=bool(task_full.get('drawing_link')))
                )
        except Exception as e:
            app_logger.alert(f"Drawing download error: {e}")
            await context.bot.send_message(chat_id=user.id, text="Не удалось загрузить чертёж.")

    elif data.startswith("unblock:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone(
            "SELECT position, element, executor, sheet_name, file_id, row_num FROM work_orders WHERE id=%s AND status='БЛОК'",
            [task_id]
        )
        if not task:
            await query.answer("Задача уже разблокирована или не найдена.", show_alert=True)
            return

        # 1. Снимаем блок в БД
        db.execute(
            "UPDATE work_orders SET status='ПЛАН', comment=NULL WHERE id=%s AND status='БЛОК'",
            [task_id]
        )

        # 2. Обновляем статус в Google Sheets
        try:
            if task.get('file_id') and task.get('row_num'):
                await asyncio.get_event_loop().run_in_executor(
                    None, update_task_status,
                    task['file_id'], task['sheet_name'], task['row_num'], 'ПЛАН', None, None
                )
        except Exception as e:
            app_logger.alert(f"unblock sheets update error: {e}")

        # 3. Редактируем сообщение мастера — убираем кнопку, меняем статус
        new_text = (
            f"✅ Блок снят\n\n"
            f"Элемент: {task['element'] or '—'}\n"
            f"Позиция: {task['position']}\n"
            f"Снял: мастер ({TODAY()})"
        )
        await query.edit_message_text(new_text)

        # 3. Уведомляем рабочего
        worker = db.fetchone(
            "SELECT telegram_username, telegram_id FROM employees WHERE full_name=%s AND is_active=true",
            [task['executor']]
        )
        if worker and (worker['telegram_username'] or worker['telegram_id']):
            try:
                if worker['telegram_id']:
                    _worker_chat_id = worker['telegram_id']
                else:
                    _worker_chat_id = (await context.bot.get_chat(f"@{worker['telegram_username']}")).id
                await context.bot.send_message(
                    chat_id=_worker_chat_id,
                    text=(
                        f"✅ Блокировка снята\n\n"
                        f"Позиция: {task['position']} — {task['element'] or ''}\n"
                        f"Можно продолжать работу."
                    ),
                    reply_markup=back_to_tasks_kb()
                )
            except Exception as e:
                app_logger.alert(f"unblock notify worker error: {e}")

        app_logger.audit('unblock', user.id, user.username, {'task_id': task_id}, 'success')

        # Показываем обновлённый список блоков или меню если блоков больше нет
        remaining = get_all_blocked()
        try:
            await query.answer("Блок снят.")
        except Exception:
            try:
                await query.message.reply_text(
                    "⚠️ Вы нажали на кнопку из старого сообщения. "
                    "Блок уже был снят — откройте меню заново /start"
                )
            except Exception:
                pass
        if remaining and is_master(role):
            lines = [f"🚫 Активные блоки — {len(remaining)} шт\n"]
            for b in remaining:
                when = b['updated_at'].strftime('%d.%m %H:%M') if b['updated_at'] else '—'
                lines.append(
                    f"• [{b['sheet_name']}] {b['position']} — {b['element'] or '—'}\n"
                    f"  👷 {b['executor']} | 📁 {b['project_name']}\n"
                    f"  💬 {b['comment'] or '—'} | 🕐 {when}"
                )
            buttons = []
            for b in remaining:
                label = f"✅ Снять: {b['position']} — {b['element'] or ''} ({b['sheet_name']})"
                buttons.append([InlineKeyboardButton(label, callback_data=f"unblock:{b['id']}")])
            buttons.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
            await context.bot.send_message(
                chat_id=user.id,
                text="\n\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await show_menu(update, worker_name, specialization, role, edit=False)

    elif data.startswith("correct:workers"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        import datetime as _dt
        parts = data.split(":")
        date_str = parts[2] if len(parts) > 2 and parts[2] else (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        try:
            _dt.date.fromisoformat(date_str)
        except ValueError:
            date_str = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        workers = db.fetchall(
            """SELECT DISTINCT wo.executor, COALESCE(e.id, 0) AS emp_id
               FROM work_orders wo
               LEFT JOIN employees e ON e.full_name = wo.executor
               WHERE wo.status IN ('ВЫПОЛНЕНО', 'ЧАСТИЧНО')
                 AND wo.date_fact = %s
                 AND wo.executor IS NOT NULL AND wo.executor != ''
               ORDER BY wo.executor""",
            [date_str]
        )
        d_label = date.fromisoformat(date_str).strftime('%d.%m.%Y')
        if not workers:
            await query.edit_message_text(
                f"🔧 Исправить выполнение — {d_label}\n\nНет выполненных задач за этот день.",
                reply_markup=InlineKeyboardMarkup([
                    _correct_nav_row(date_str),
                    [InlineKeyboardButton("← Меню", callback_data="menu")]
                ])
            )
            return
        await query.edit_message_text(
            f"🔧 Исправить выполнение — {d_label}\n\nВыберите рабочего:",
            reply_markup=correct_workers_kb(workers, date_str)
        )

    elif data.startswith("correct:tasks:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        import datetime as _dt
        # correct:tasks:{emp_id}:{date}
        rest  = data[len("correct:tasks:"):]
        parts = rest.rsplit(":", 1)
        emp_id   = int(parts[0]) if parts[0].isdigit() else 0
        date_str = parts[1] if len(parts) > 1 and parts[1] else (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        try:
            _dt.date.fromisoformat(date_str)
        except ValueError:
            date_str = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        # Имя по emp_id
        if emp_id:
            emp_row = db.fetchone("SELECT full_name FROM employees WHERE id=%s", [emp_id])
            w_name  = emp_row['full_name'] if emp_row else str(emp_id)
        else:
            w_name = str(emp_id)
        tasks = db.fetchall(
            """SELECT wo.id, wo.position, wo.element, wo.quantity, wo.qty_done, wo.status, wo.date_fact,
                      wo.sheet_name, wo.project_name, wo.file_id, wo.row_num,
                      COALESCE(e.id, 0) AS emp_id
               FROM work_orders wo
               LEFT JOIN employees e ON e.full_name = wo.executor
               WHERE wo.executor=%s
                 AND wo.status IN ('ВЫПОЛНЕНО', 'ЧАСТИЧНО')
                 AND wo.date_fact = %s
               ORDER BY wo.project_name, wo.sheet_name, wo.position""",
            [w_name, date_str]
        )
        d_label = date.fromisoformat(date_str).strftime('%d.%m.%Y')
        if not tasks:
            await query.edit_message_text(
                f"🔧 Исправить выполнение — {d_label}\n👤 {w_name}\n\nНет задач за этот день.",
                reply_markup=InlineKeyboardMarkup([
                    _correct_nav_row(date_str),
                    [InlineKeyboardButton("← Рабочие", callback_data=f"correct:workers:{date_str}")]
                ])
            )
            return
        await query.edit_message_text(
            f"🔧 Исправить выполнение — {d_label}\n👤 {w_name}\n\nВыберите задачу:",
            reply_markup=correct_tasks_kb(tasks, date_str)
        )

    elif data.startswith("correct:action:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        task_id = int(data.split(":")[2])
        task = db.fetchone(
            """SELECT id, position, element, quantity, qty_done, status, date_fact,
                      sheet_name, project_name, file_id, row_num, executor
               FROM work_orders WHERE id=%s""",
            [task_id]
        )
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        icon = '✅' if task['status'] == 'ВЫПОЛНЕНО' else '◧'
        date_str = task['date_fact'].strftime('%d.%m.%Y') if task['date_fact'] else '—'
        qty = int(task['quantity'] or 0)
        text = (
            f"{icon} {task['position']}\n"
            f"Спец: {task['sheet_name']} | Проект: {task['project_name']}\n"
            f"Исполнитель: {task['executor']}\n"
            f"Кол-во: {qty} шт | Дата: {date_str}\n"
            f"Статус: {task['status']}"
        )
        # Проверка блокировки: ЧАСТИЧНО, но остаток уже ВЫПОЛНЕНО сегодня
        if task['status'] == 'ЧАСТИЧНО':
            completed_remainder = db.fetchone(
                """SELECT id FROM work_orders
                   WHERE position=%s AND file_id=%s AND sheet_name=%s
                     AND status='ВЫПОЛНЕНО' AND date_fact=CURRENT_DATE AND id!=%s
                   LIMIT 1""",
                [task['position'], task['file_id'], task['sheet_name'], task_id]
            )
            if completed_remainder:
                text += (
                    "\n\n⛔ Нельзя исправить — остаток по этой позиции уже выполнен сегодня.\n"
                    "Сначала исправьте сегодняшнее выполнение."
                )
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("← К задачам", callback_data=f"correct:tasks:{task['executor']}")
                ]]))
                return
        # qty=1 → только отмена (нельзя исправить до 0)
        show_qty = qty > 1
        _ds_ca  = task['date_fact'].isoformat() if task.get('date_fact') else (date.today() - __import__('datetime').timedelta(days=1)).isoformat()
        _emp_ca = task.get('emp_id') or 0
        if not _emp_ca:
            _e = db.fetchone("SELECT id FROM employees WHERE full_name=%s", [task['executor']])
            _emp_ca = _e['id'] if _e else 0
        await query.edit_message_text(text, reply_markup=correct_action_kb(task_id, _emp_ca, _ds_ca, show_qty))

    elif data.startswith("correct:qty:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        task_id = int(data.split(":")[2])
        task = db.fetchone(
            "SELECT position, element, quantity, file_id, sheet_name FROM work_orders WHERE id=%s",
            [task_id]
        )
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        remainder = db.fetchone(
            """SELECT id, quantity FROM work_orders
               WHERE position=%s AND file_id=%s AND sheet_name=%s
                 AND status='ПЛАН' AND id!=%s
               ORDER BY id DESC LIMIT 1""",
            [task['position'], task['file_id'], task['sheet_name'], task_id]
        )
        qty_self = int(task['quantity'] or 0)
        total_original = qty_self + (int(remainder['quantity'] or 0) if remainder else 0)
        max_qty = total_original - 1
        context.user_data['correct_task_id']     = task_id
        context.user_data['correct_max_qty']     = max_qty
        context.user_data['correct_remainder_id'] = remainder['id'] if remainder else None
        await query.edit_message_text(
            f"◧ Исправить количество\n\n"
            f"Позиция: {task['position']} — {task['element'] or ''}\n"
            f"Выполнено по записи: {qty_self} шт\n\n"
            f"Сколько реально сделано? (от 1 до {max_qty})",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="menu")]])
        )
        return WAITING_CORRECTION_QTY

    elif data.startswith("correct:cancel:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        task_id = int(data.split(":")[2])
        from src import sheets as _sheets
        task = db.fetchone(
            """SELECT id, position, element, quantity, qty_done, status,
                      file_id, sheet_name, row_num, unit_weight, payment_sum, executor
               FROM work_orders WHERE id=%s""",
            [task_id]
        )
        if not task:
            await query.answer("Задача не найдена.", show_alert=True)
            return
        remainder = db.fetchone(
            """SELECT id, quantity, row_num, total_weight, payment_sum FROM work_orders
               WHERE position=%s AND file_id=%s AND sheet_name=%s
                 AND status='ПЛАН' AND id!=%s
               ORDER BY id DESC LIMIT 1""",
            [task['position'], task['file_id'], task['sheet_name'], task_id]
        )
        restored_qty = int(task['quantity'] or 0) + (int(remainder['quantity'] or 0) if remainder else 0)
        _uw = float(task['unit_weight'] or 0)
        _done_ps = float(task['payment_sum'] or 0)
        _rem_ps  = float(remainder['payment_sum'] or 0) if remainder else 0.0
        _tw_restored = round(_uw * restored_qty, 3) if _uw else None
        _ps_restored = round(_done_ps + _rem_ps, 2) if (_done_ps or _rem_ps) else None
        # БД: восстанавливаем оригинальную строку
        db.execute(
            """UPDATE work_orders
               SET status='ПЛАН', qty_done=NULL, date_fact=NULL,
                   quantity=%s, total_weight=%s, payment_sum=%s
               WHERE id=%s""",
            [restored_qty, _tw_restored, _ps_restored, task_id]
        )
        # БД: удаляем строку-остаток
        if remainder:
            db.execute("DELETE FROM work_orders WHERE id=%s", [remainder['id']])
        # Лист: обновляем оригинальную строку
        try:
            _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'СТАТУС', 'ПЛАН')
            _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'ДАТА ФАКТ', '')
            _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'ВЫПОЛНЕНО', '')
            _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'КОЛ-ВО', restored_qty)
            if _tw_restored:
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'МАССА ВСЕХ (кг)', _tw_restored)
            if _ps_restored:
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'СУММА К ОПЛАТЕ', _ps_restored)
        except Exception as e:
            app_logger.alert(f"correct_cancel sheets update error: {e}")
        # Лист: удаляем строку-остаток
        if remainder and remainder.get('row_num') and remainder['row_num'] > 0:
            try:
                _sheets.delete_row(task['file_id'], task['sheet_name'], remainder['row_num'])
            except Exception as e:
                app_logger.alert(f"correct_cancel delete row error: {e}")
        app_logger.audit('master_correct_cancel', user.id, user.username,
                         {'task_id': task_id, 'restored_qty': restored_qty}, 'success')
        await query.edit_message_text(
            f"✅ Отменено\n\n"
            f"Позиция: {task['position']} — {task['element'] or ''}\n"
            f"Возвращено в ПЛАН: {restored_qty} шт",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← К рабочему", callback_data=f"correct:tasks:{task.get('emp_id') or 0}:{task['date_fact'].isoformat() if task.get('date_fact') else ''}")
            ]])
        )

    elif data.startswith("block_ask:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone("SELECT position, element FROM work_orders WHERE id=%s", [task_id])
        pos = task['position'] if task else f"#{task_id}"
        el  = task['element'] if task else ''
        await query.edit_message_text(
            f"Вы уверены, что хотите заблокировать задачу?\n\n"
            f"⛔ {pos} — {el}\n\n"
            f"Снять блокировку сможет только руководитель.",
            reply_markup=confirm_block_kb(task_id)
        )

    elif data.startswith("block_confirm:"):
        task_id = int(data.split(":")[1])
        context.user_data['block_task_id'] = task_id
        await query.edit_message_text(
            "⛔ Укажите причину блокировки:\n(напишите ответным сообщением)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data="tasks")]])
        )
        return WAITING_BLOCK_COMMENT

    # -------------------------------------------------------------------
    # Раздел «Сотрудники»
    # -------------------------------------------------------------------

    elif data == "admin:users":
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        await show_emp_list(update, page=0, inactive=False)

    elif data == "admin:users_inactive":
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        await show_emp_list(update, page=0, inactive=True)

    elif data.startswith("admin:users_page:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        parts = data.split(":")
        raw = parts[2]
        inactive = raw.endswith("_i")
        page = int(raw.rstrip("_i"))
        await show_emp_list(update, page=page, inactive=inactive)

    elif data.startswith("admin:user:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        await show_emp_card(update, emp_id)

    elif data.startswith("admin:spec:") and not data.startswith("admin:spec_toggle:") and not data.startswith("admin:spec_save:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        await show_spec_editor(update, emp_id)

    elif data.startswith("admin:spec_toggle:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        parts = data.split(":")
        emp_id = int(parts[2])
        spec = parts[3]
        emp = db.fetchone("SELECT full_name, specialization FROM employees WHERE id=%s", [emp_id])
        if not emp:
            await query.answer("Сотрудник не найден.", show_alert=True)
            return
        selected = list(emp['specialization'] or [])
        if spec in selected:
            selected.remove(spec)
        else:
            selected.append(spec)
        db.execute("UPDATE employees SET specialization=%s WHERE id=%s", [selected, emp_id])
        text = f"Специализации — {emp['full_name']}:"
        kb = _spec_checkboxes_kb(emp_id, selected, f"admin:spec_save:{emp_id}", f"admin:user:{emp_id}")
        await query.edit_message_text(text, reply_markup=kb)

    elif data.startswith("admin:spec_save:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        await query.answer("✅ Специализации сохранены.")
        await show_emp_card(update, emp_id)

    elif data.startswith("admin:role:") and not data.startswith("admin:role_set:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        emp = db.fetchone("SELECT full_name, role FROM employees WHERE id=%s", [emp_id])
        if not emp:
            await query.answer("Сотрудник не найден.", show_alert=True)
            return
        cur = emp['role'] or 'worker'
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{'✅ ' if cur=='worker' else ''}👷 Рабочий", callback_data=f"admin:role_set:{emp_id}:worker"),
            InlineKeyboardButton(f"{'✅ ' if cur=='master' else ''}👑 Мастер",  callback_data=f"admin:role_set:{emp_id}:master"),
        ], [
            InlineKeyboardButton(f"{'✅ ' if cur=='admin' else ''}🔑 Администратор", callback_data=f"admin:role_set:{emp_id}:admin"),
        ], [InlineKeyboardButton("← Отмена", callback_data=f"admin:user:{emp_id}")]])
        await query.edit_message_text(f"Роль — {emp['full_name']}:", reply_markup=kb)

    elif data.startswith("admin:role_set:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        parts = data.split(":")
        emp_id = int(parts[2])
        new_role = parts[3]
        db.execute("UPDATE employees SET role=%s WHERE id=%s", [new_role, emp_id])
        await query.answer("✅ Роль обновлена.")
        await show_emp_card(update, emp_id)

    elif data.startswith("admin:name:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        emp = db.fetchone("SELECT full_name FROM employees WHERE id=%s", [emp_id])
        if not emp:
            await query.answer("Сотрудник не найден.", show_alert=True)
            return
        context.user_data['edit_emp_id'] = emp_id
        await query.edit_message_text(
            f"Введите новое имя для {emp['full_name']}:\n(любой формат, например: Гусак Александр)"
        )
        return WAITING_EMP_NAME

    elif data.startswith("admin:deactivate:") and not data.startswith("admin:deactivate_confirm:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        emp = db.fetchone("SELECT full_name FROM employees WHERE id=%s", [emp_id])
        if not emp:
            await query.answer("Сотрудник не найден.", show_alert=True)
            return
        open_cnt = db.fetchone(
            "SELECT COUNT(*) as cnt FROM work_orders WHERE executor=%s AND status IN ('ПЛАН','ЧАСТИЧНО','БЛОК')",
            [emp['full_name']]
        )
        cnt = open_cnt['cnt'] if open_cnt else 0
        warn = f"\n\n⚠️ У него {cnt} открытых нарядов. Они останутся на нём." if cnt > 0 else ""
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, деактивировать", callback_data=f"admin:deactivate_confirm:{emp_id}"),
            InlineKeyboardButton("← Отмена",              callback_data=f"admin:user:{emp_id}"),
        ]])
        await query.edit_message_text(f"Деактивировать {emp['full_name']}?{warn}", reply_markup=kb)

    elif data.startswith("admin:deactivate_confirm:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        db.execute("UPDATE employees SET is_active=false WHERE id=%s", [emp_id])
        await query.answer("🚫 Сотрудник деактивирован.")
        await show_emp_list(update, page=0, inactive=False)

    elif data.startswith("admin:reactivate:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        emp_id = int(data.split(":")[2])
        emp = db.fetchone("SELECT full_name FROM employees WHERE id=%s", [emp_id])
        if not emp:
            await query.answer("Сотрудник не найден.", show_alert=True)
            return
        db.execute("UPDATE employees SET is_active=true WHERE id=%s", [emp_id])
        # Закрываем pending заявки если есть
        db.execute(
            "UPDATE pending_users SET status='approved' WHERE telegram_id="
            "(SELECT telegram_id FROM employees WHERE id=%s) AND status='pending'",
            [emp_id]
        )
        await query.answer(f"✅ {emp['full_name']} активирован.")
        await show_emp_list(update, page=0, inactive=True)

    elif data == "admin:pending":
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        pending = db.fetchall(
            "SELECT id, telegram_id, username, first_name, last_name, requested_at "
            "FROM pending_users WHERE status='pending' ORDER BY requested_at",
            []
        )
        if not pending:
            await query.edit_message_text(
                "📥 Заявок нет.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="admin:users")]])
            )
            return
        rows = []
        for p in pending:
            name = f"{p['first_name'] or ''} {p['last_name'] or ''}".strip() or f"id:{p['telegram_id']}"
            if p['username']:
                name += f" (@{p['username']})"
            rows.append([
                InlineKeyboardButton(name, callback_data=f"admin:approve:{p['id']}"),
                InlineKeyboardButton("❌", callback_data=f"admin:reject:{p['id']}"),
            ])
        rows.append([InlineKeyboardButton("← Назад", callback_data="admin:users")])
        await query.edit_message_text("📥 Заявки на доступ:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("admin:reject:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        pending_id = int(data.split(":")[2])
        db.execute("UPDATE pending_users SET status='rejected' WHERE id=%s", [pending_id])
        await query.answer("Заявка отклонена.")
        # Обновляем список заявок
        pending_left = db.fetchall(
            "SELECT id, telegram_id, username, first_name, last_name FROM pending_users WHERE status='pending'", []
        )
        if not pending_left:
            await query.edit_message_text(
                "📥 Заявок нет.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="admin:users")]])
            )
        else:
            rows = []
            for p in pending_left:
                name = f"{p['first_name'] or ''} {p['last_name'] or ''}".strip() or f"id:{p['telegram_id']}"
                if p['username']:
                    name += f" (@{p['username']})"
                rows.append([
                    InlineKeyboardButton(name, callback_data=f"admin:approve:{p['id']}"),
                    InlineKeyboardButton("❌", callback_data=f"admin:reject:{p['id']}"),
                ])
            rows.append([InlineKeyboardButton("← Назад", callback_data="admin:users")])
            await query.edit_message_text("📥 Заявки на доступ:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("admin:approve:"):
        if not is_master(role):
            await query.answer("Нет доступа.", show_alert=True)
            return
        pending_id = int(data.split(":")[2])
        pending = db.fetchone(
            "SELECT id, telegram_id, username, first_name, last_name FROM pending_users WHERE id=%s AND status='pending'",
            [pending_id]
        )
        if not pending:
            await query.edit_message_text("ℹ️ Этот сотрудник уже активирован другим мастером.")
            return
        context.user_data['approve_pending_id'] = pending_id
        context.user_data['approve_spec'] = []
        context.user_data['approve_role'] = 'worker'
        tg_name = f"{pending['first_name'] or ''} {pending['last_name'] or ''}".strip() or f"id:{pending['telegram_id']}"
        if pending['username']:
            tg_name += f" (@{pending['username']})"
        context.user_data['approve_tg_name'] = tg_name
        await query.edit_message_text(
            f"Введите полное имя сотрудника\n(как будет отображаться в нарядах):\n\nTelegram: {tg_name}"
        )
        return WAITING_APPROVE_NAME

    elif data.startswith("admin:approve_spec_toggle:"):
        parts = data.split(":")
        spec = parts[3]
        selected = context.user_data.get('approve_spec', [])
        if spec in selected:
            selected.remove(spec)
        else:
            selected.append(spec)
        context.user_data['approve_spec'] = selected
        pending_id = context.user_data.get('approve_pending_id')
        tg_name = context.user_data.get('approve_tg_name', '')
        text = f"Специализации — {tg_name}:"
        kb = _approve_spec_kb(pending_id, selected)
        await query.edit_message_text(text, reply_markup=kb)

    elif data.startswith("admin:approve_spec_next:"):
        pending_id = int(data.split(":")[2])
        selected = context.user_data.get('approve_spec', [])
        cur_role = context.user_data.get('approve_role', 'worker')
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{'✅ ' if cur_role=='worker' else ''}👷 Рабочий", callback_data=f"admin:approve_role:{pending_id}:worker"),
            InlineKeyboardButton(f"{'✅ ' if cur_role=='master' else ''}👑 Мастер",  callback_data=f"admin:approve_role:{pending_id}:master"),
        ], [
            InlineKeyboardButton(f"{'✅ ' if cur_role=='admin' else ''}🔑 Администратор", callback_data=f"admin:approve_role:{pending_id}:admin"),
        ]])
        emp_name = context.user_data.get('approve_name', '')
        await query.edit_message_text(f"Роль — {emp_name}:", reply_markup=kb)

    elif data.startswith("admin:approve_role:"):
        parts = data.split(":")
        pending_id = int(parts[2])
        new_role = parts[3]
        context.user_data['approve_role'] = new_role
        await _do_approve_confirm(query, context, pending_id)

    elif data.startswith("admin:approve_confirm:"):
        pending_id = int(data.split(":")[2])
        await _do_approve_create(query, context, pending_id)


def _apply_partial_db(task_id, task, qty_new, qty_done_total, qty_total, employee_id, history_payment, new_row_id):
    """Одна транзакция: запись в историю + статус строки + (если есть остаток) строка-остаток.
    Падение процесса посреди операции не оставляет БД в половинчатом состоянии."""
    remaining = qty_total - qty_done_total
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_order_history (work_order_id, row_id, employee_id, qty, payment_sum, date_done) "
                "VALUES (%s, %s, %s, %s, %s, CURRENT_DATE)",
                [task_id, task['row_id'], employee_id, qty_new, history_payment]
            )
            if qty_done_total == qty_total:
                cur.execute(
                    "UPDATE work_orders SET status='ВЫПОЛНЕНО', qty_done=%s, date_fact=CURRENT_DATE WHERE id=%s",
                    [qty_done_total, task_id]
                )
                return
            orig_qty = float(task['quantity'] or 1)
            tw_remain = round(float(task['total_weight'] or 0) / orig_qty * remaining, 3) if task['total_weight'] else None
            ps_remain = round(float(task['payment_sum'] or 0) / orig_qty * remaining, 2) if task['payment_sum'] else None
            cur.execute(
                "UPDATE work_orders SET status='ВЫПОЛНЕНО', qty_done=%s, quantity=%s, date_fact=CURRENT_DATE WHERE id=%s",
                [qty_new, qty_new, task_id]
            )
            cur.execute(
                "INSERT INTO work_orders "
                "(project_name, file_id, sheet_name, row_num, row_id, "
                " position, element, quantity, unit_weight, total_weight, payment_sum, "
                " executor, date_plan, priority, mandatory, status, drawing_link, qty_holes) "
                "SELECT project_name, file_id, sheet_name, -%s, %s, "
                "       position, element, %s, unit_weight, %s, %s, "
                "       NULL, date_plan, priority, true, 'ПЛАН', drawing_link, qty_holes "
                "FROM work_orders WHERE id=%s",
                [task_id, new_row_id, remaining, tw_remain, ps_remain, task_id]
            )


async def receive_partial_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    task_id = context.user_data.pop('partial_task_id', None)
    text = update.message.text.strip()

    if not task_id:
        await update.message.reply_text("Ошибка: начните заново с /start")
        return ConversationHandler.END

    task = db.fetchone(
        "SELECT position, element, quantity, qty_done, file_id, sheet_name, row_num, row_id, "
        "executor, date_plan, priority, mandatory, drawing_link, comment, payment_sum, "
        "unit_weight, total_weight, project_name FROM work_orders WHERE id=%s",
        [task_id]
    )
    if not task:
        await update.message.reply_text("Задача не найдена.")
        return ConversationHandler.END

    try:
        qty_new = int(text)
    except ValueError:
        await update.message.reply_text("Введите целое число.")
        context.user_data['partial_task_id'] = task_id
        return WAITING_PARTIAL_QTY

    app_logger.audit('partial_qty_received', user.id, user.username,
                     {'task_id': task_id, 'qty_entered': qty_new}, 'pending')

    qty_total = int(task['quantity'] or 0)
    qty_done_before = int(task['qty_done'] or 0)
    qty_done_total = qty_done_before + qty_new

    max_partial = context.user_data.pop('partial_deps_avail', None)
    remaining = qty_total - qty_done_before
    max_allowed = min(remaining, max_partial) if (max_partial is not None and max_partial < remaining) else remaining
    if qty_new <= 0 or qty_done_total > qty_total or qty_new > max_allowed:
        if qty_new > max_allowed and max_allowed < remaining:
            await update.message.reply_text(
                f"Нельзя выполнить {qty_new} шт — доступно только {max_allowed} шт.\n"
                f"Введите число от 1 до {max_allowed}."
            )
        else:
            await update.message.reply_text(f"Некорректное число. Осталось: {remaining} шт.")
        context.user_data['partial_task_id'] = task_id
        context.user_data['partial_deps_avail'] = max_allowed
        return WAITING_PARTIAL_QTY

    import time as _t_pc
    context.user_data['partial_confirm'] = {'task_id': task_id, 'qty': qty_new, 'ts': _t_pc.time()}
    await update.message.reply_text(
        "◧ Подтвердите запись\n\n"
        f"Проект: {task['project_name'] or ''} · {task['sheet_name']}\n"
        f"Позиция: {task['position'] or ''} — {task['element'] or ''}\n"
        f"Записать: {qty_new} из {qty_total} шт"
        + (f" (останется {qty_total - qty_done_total} шт)" if qty_done_total < qty_total else " (выполнено полностью)"),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, записать", callback_data=f"pconf:yes:{task_id}:{qty_new}"),
            InlineKeyboardButton("✖ Нет", callback_data="tasks"),
        ]])
    )
    return ConversationHandler.END


async def _apply_partial(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: int, qty_new: int):
    """Запись частичного выполнения (после подтверждения «Да»). Состояние задачи перечитывается заново."""
    user = update.effective_user
    task = db.fetchone(
        "SELECT position, element, quantity, qty_done, file_id, sheet_name, row_num, row_id, "
        "executor, date_plan, priority, mandatory, drawing_link, comment, payment_sum, "
        "unit_weight, total_weight, project_name FROM work_orders WHERE id=%s",
        [task_id]
    )
    if not task:
        await update.effective_message.reply_text("Задача не найдена.")
        return
    qty_total = int(task['quantity'] or 0)
    qty_done_before = int(task['qty_done'] or 0)
    qty_done_total = qty_done_before + qty_new
    if qty_new <= 0 or qty_done_total > qty_total:
        await update.effective_message.reply_text("Данные по задаче изменились. Откройте задачу заново.")
        return

    today = TODAY()
    worker_name, specialization, _ = _get_worker_context(update, context)
    employee = db.fetchone("SELECT id FROM employees WHERE full_name=%s", [worker_name])
    employee_id = employee['id'] if employee else None

    unit_price = (float(task['payment_sum'] or 0) / float(task['quantity'] or 1)) if task['quantity'] else 0
    history_payment = round(unit_price * qty_new, 2)

    import uuid as _uuid0
    new_row_id = str(_uuid0.uuid4())
    _apply_partial_db(task_id, task, qty_new, qty_done_total, qty_total, employee_id, history_payment, new_row_id)

    if qty_done_total == qty_total:
        try:
            update_task_status(
                file_id=task['file_id'], sheet_name=task['sheet_name'], row_num=task['row_num'],
                status='ВЫПОЛНЕНО', date_fact=today, qty_done=qty_done_total,
            )
        except Exception as e:
            app_logger.alert(f"Sheets full-done write error: {e}")
        msg = f"✅ {task['position']} — {task['element'] or ''}\nВыполнено полностью | {today}"

    else:
        remaining = qty_total - qty_done_total

        try:
            from src import sheets as _sheets
            # Текущую строку → ВЫПОЛНЕНО
            update_task_status(
                file_id=task['file_id'], sheet_name=task['sheet_name'], row_num=task['row_num'],
                status='ВЫПОЛНЕНО', date_fact=today, qty_done=qty_new,
            )
            # Обновляем КОЛ-ВО, МАССА ВСЕХ, СУММА К ОПЛАТЕ текущей строки
            _qty = float(task['quantity'] or 1)
            _sheets.update_cell_by_header(
                task['file_id'], task['sheet_name'], task['row_num'], 'КОЛ-ВО', qty_new
            )
            if task['total_weight']:
                _tw_done = round(float(task['total_weight']) / _qty * qty_new, 3)
                _sheets.update_cell_by_header(
                    task['file_id'], task['sheet_name'], task['row_num'], 'МАССА ВСЕХ (кг)', _tw_done
                )
            if task['payment_sum']:
                _ps_done = round(float(task['payment_sum']) / _qty * qty_new, 2)
                _sheets.update_cell_by_header(
                    task['file_id'], task['sheet_name'], task['row_num'], 'СУММА К ОПЛАТЕ', _ps_done
                )
            # Вставляем строку-остаток сразу после
            _uw  = float(task['unit_weight'] or 0) or None
            _tw  = round(float(task['total_weight'] or 0) / _qty * remaining, 3) if task['total_weight'] else None
            _ps  = round(float(task['payment_sum']  or 0) / _qty * remaining, 2) if task['payment_sum']  else None
            remainder_data = {
                'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': task['position'] or '',
                'ЭЛЕМЕНТ':               task['element'] or '',
                'КОЛ-ВО':               remaining,
                'МАССА ЕД. (кг)':       _uw  if _uw  is not None else '',
                'МАССА ВСЕХ (кг)':      _tw  if _tw  is not None else '',
                'СУММА К ОПЛАТЕ':       _ps  if _ps  is not None else '',
                'СТАТУС':                'ПЛАН',
                'ОБЯЗАТЕЛЬНАЯ':          'НЕТ',
                'ИСПОЛНИТЕЛЬ':           '',
                'ROW_ID':                new_row_id,
                'ССЫЛКА НА ЧЕРТЁЖ':     task['drawing_link'] or '',
                'КОЛ-ВО ОТВЕРСТИЙ':     task['qty_holes'] if task.get('qty_holes') else '',
            }
            _sheets.insert_remainder_row(
                task['file_id'], task['sheet_name'], task['row_num'], remainder_data
            )
            # placeholder (row_num=-task_id) синк почистит сам при следующем запуске:
            # найдёт реальную строку с тем же row_id и удалит ghost-запись
        except Exception as e:
            app_logger.alert(f"Sheets partial split error: {e}")

        msg = (f"◧ {task['position']} — {task['element'] or ''}\n"
               f"Выполнено: {qty_new} шт | Остаток {remaining} шт добавлен в план")

    app_logger.audit('set_partial', user.id, user.username,
                     {'task_id': task_id, 'qty_done': qty_new}, 'success')

    # Разблокируем зависимые задачи следующего уровня
    try:
        await notify_deps_unblocked(
            context.bot, task['project_name'], task['sheet_name'],
            task['position'], qty_new,
            completed_element=task.get('element'),
        )
    except Exception as e:
        app_logger.alert(f"notify_deps_unblocked (partial) error: {e}")

    await update.effective_message.reply_text(msg, reply_markup=back_to_tasks_kb())


async def receive_correction_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    task_id      = context.user_data.pop('correct_task_id', None)
    max_qty      = context.user_data.pop('correct_max_qty', None)
    remainder_id = context.user_data.pop('correct_remainder_id', None)
    text = update.message.text.strip()

    if not task_id:
        await update.message.reply_text("Ошибка: начните заново с /start")
        return ConversationHandler.END

    try:
        qty_new = int(text)
    except ValueError:
        await update.message.reply_text("Введите целое число.")
        context.user_data['correct_task_id']      = task_id
        context.user_data['correct_max_qty']      = max_qty
        context.user_data['correct_remainder_id'] = remainder_id
        return WAITING_CORRECTION_QTY

    if qty_new <= 0 or qty_new > max_qty:
        await update.message.reply_text(f"Некорректное число. Введите от 1 до {max_qty}.")
        context.user_data['correct_task_id']      = task_id
        context.user_data['correct_max_qty']      = max_qty
        context.user_data['correct_remainder_id'] = remainder_id
        return WAITING_CORRECTION_QTY

    from src import sheets as _sheets
    task = db.fetchone(
        """SELECT id, position, element, quantity, status, date_fact,
                  file_id, sheet_name, row_num, unit_weight, total_weight, payment_sum, executor, drawing_link
           FROM work_orders WHERE id=%s""",
        [task_id]
    )
    if not task:
        await update.message.reply_text("Задача не найдена.")
        return ConversationHandler.END

    total_original = max_qty + 1
    new_remaining  = total_original - qty_new
    _uw = float(task['unit_weight'] or 0)

    # Общая сумма оплаты = текущая + остатка (если есть)
    _self_ps = float(task['payment_sum'] or 0)
    _rem_ps_old = 0.0
    if remainder_id:
        rem_row = db.fetchone("SELECT quantity, row_num, payment_sum FROM work_orders WHERE id=%s", [remainder_id])
        _rem_ps_old = float(rem_row['payment_sum'] or 0) if rem_row else 0.0
    _total_ps = _self_ps + _rem_ps_old

    _done_tw = round(_uw * qty_new, 3)          if _uw        else None
    _done_ps = round(_total_ps / total_original * qty_new,       2) if _total_ps else None
    _rem_tw  = round(_uw * new_remaining, 3)     if _uw        else None
    _rem_ps  = round(_total_ps / total_original * new_remaining, 2) if _total_ps else None

    # БД: обновляем оригинальную строку
    db.execute(
        "UPDATE work_orders SET qty_done=%s, quantity=%s, total_weight=%s, payment_sum=%s WHERE id=%s",
        [qty_new, qty_new, _done_tw, _done_ps, task_id]
    )

    if remainder_id:
        # Обновляем существующий остаток
        db.execute(
            "UPDATE work_orders SET quantity=%s, total_weight=%s, payment_sum=%s WHERE id=%s",
            [new_remaining, _rem_tw, _rem_ps, remainder_id]
        )
        try:
            _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'КОЛ-ВО', qty_new)
            if _done_tw:
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'МАССА ВСЕХ (кг)', _done_tw)
            if _done_ps:
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'СУММА К ОПЛАТЕ', _done_ps)
            if rem_row and rem_row.get('row_num') and rem_row['row_num'] > 0:
                rn = rem_row['row_num']
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], rn, 'КОЛ-ВО', new_remaining)
                if _rem_tw:
                    _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], rn, 'МАССА ВСЕХ (кг)', _rem_tw)
                if _rem_ps:
                    _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], rn, 'СУММА К ОПЛАТЕ', _rem_ps)
        except Exception as e:
            app_logger.alert(f"correct_qty sheets update error: {e}")
    else:
        # Остатка не было — создаём новый (как в partial flow)
        import uuid as _uuid
        new_row_id = str(_uuid.uuid4())
        db.execute(
            """INSERT INTO work_orders
               (project_name, file_id, sheet_name, row_num, row_id,
                position, element, quantity, unit_weight, total_weight, payment_sum,
                executor, date_plan, priority, mandatory, status, drawing_link)
               SELECT project_name, file_id, sheet_name, -%s, %s,
                      position, element, %s, unit_weight, %s, %s,
                      NULL, date_plan, priority, false, 'ПЛАН', drawing_link
               FROM work_orders WHERE id=%s""",
            [task_id, new_row_id, new_remaining, _rem_tw, _rem_ps, task_id]
        )
        try:
            _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'КОЛ-ВО', qty_new)
            if _done_tw:
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'МАССА ВСЕХ (кг)', _done_tw)
            if _done_ps:
                _sheets.update_cell_by_header(task['file_id'], task['sheet_name'], task['row_num'], 'СУММА К ОПЛАТЕ', _done_ps)
            remainder_data = {
                'ПОЗ. СОГЛАСНО ЧЕРТЕЖА': task['position'] or '',
                'ЭЛЕМЕНТ':               task['element'] or '',
                'КОЛ-ВО':               new_remaining,
                'МАССА ЕД. (кг)':       _uw if _uw else '',
                'МАССА ВСЕХ (кг)':      _rem_tw if _rem_tw else '',
                'СУММА К ОПЛАТЕ':       _rem_ps if _rem_ps else '',
                'СТАТУС':               'ПЛАН',
                'ОБЯЗАТЕЛЬНАЯ':         'НЕТ',
                'ИСПОЛНИТЕЛЬ':          '',
                'ROW_ID':               new_row_id,
                'ССЫЛКА НА ЧЕРТЁЖ':    task['drawing_link'] or '',
            }
            _sheets.insert_remainder_row(task['file_id'], task['sheet_name'], task['row_num'], remainder_data)
        except Exception as e:
            app_logger.alert(f"correct_qty sheets insert error: {e}")

    app_logger.audit('master_correct_qty', user.id, user.username,
                     {'task_id': task_id, 'qty_new': qty_new, 'new_remaining': new_remaining}, 'success')
    await update.message.reply_text(
        f"✅ Исправлено\n\n"
        f"Позиция: {task['position']} — {task['element'] or ''}\n"
        f"Выполнено: {qty_new} шт | Остаток: {new_remaining} шт в ПЛАН",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("← К рабочему", callback_data=f"correct:tasks:{task['executor']}")
        ]])
    )
    return ConversationHandler.END


async def receive_block_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    task_id = context.user_data.pop('block_task_id', None)
    comment = update.message.text.strip()

    if not task_id:
        await update.message.reply_text("Ошибка: начните заново с /start")
        return ConversationHandler.END

    task = db.fetchone(
        "SELECT position, element, payment_sum, file_id, sheet_name, row_num FROM work_orders WHERE id=%s",
        [task_id]
    )

    # 1. Обновляем PostgreSQL
    db.execute(
        "UPDATE work_orders SET status='БЛОК', comment=%s WHERE id=%s AND status='ПЛАН'",
        [comment, task_id]
    )

    # 2. Обновляем Google Sheets
    try:
        update_task_status(
            file_id=task['file_id'],
            sheet_name=task['sheet_name'],
            row_num=task['row_num'],
            status='БЛОК',
            comment=comment
        )
    except Exception as e:
        app_logger.alert(f"Sheets write error: {e}")

    app_logger.audit('set_block', user.id, user.username,
                     {'task_id': task_id, 'comment': comment}, 'success')

    worker_name, specialization, _ = _get_worker_context(update, context)
    await notify_masters(
        context.bot, task_id,
        worker_name or str(user.username),
        specialization or '—',
        task['position'] if task else f'#{task_id}',
        task['element'] if task else '',
        comment
    )

    pos = task['position'] if task else f"#{task_id}"
    el  = task['element'] if task else ''
    pay = f"\n💵 К оплате: {task['payment_sum']} руб" if task and task['payment_sum'] else ""

    # 3. В историю чата — новое сообщение
    await update.message.reply_text(
        f"⛔ {pos} — {el}\nЗаблокировано | {TODAY()}\nПричина: {comment}{pay}",
        reply_markup=back_to_tasks_kb()
    )
    return ConversationHandler.END


async def _bg(fn, *args, **kwargs):
    """Тяжёлый синхронный вызов (Google Sheets/Drive, Excel) в потоке: цикл бота остаётся свободным для других пользователей."""
    import functools
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *args, **kwargs))


async def receive_project_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: получаем ссылку на папку Drive, сканируем, показываем найденные специализации."""
    user = update.effective_user
    text = update.message.text.strip()

    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', text)
    if not m:
        await update.message.reply_text(
            "❌ Не удалось распознать ссылку на папку.\n"
            "Формат: https://drive.google.com/drive/folders/FOLDER_ID\n\n"
            "Попробуйте ещё раз или нажмите /cancel для отмены."
        )
        return WAITING_PROJECT_LINK

    folder_id = m.group(1)
    msg = await update.message.reply_text("🔍 Сканирую папку Drive...")

    try:
        scan = await _bg(_wiz.scan_and_recognize, folder_id)
    except Exception as e:
        app_logger.alert(f"wizard scan error: {e}")
        await msg.edit_text(f"❌ Ошибка сканирования:\n{e}\n\nПроверьте доступ или /cancel.")
        return WAITING_PROJECT_LINK

    if not scan['ok']:
        await msg.edit_text(f"❌ {scan['error']}\n\nПопробуйте другую папку или /cancel.")
        return WAITING_PROJECT_LINK

    if not scan['recognized']:
        await msg.edit_text(
            "❌ В папке не найдено Excel-файлов с распознанными специализациями.\n\n"
            "Папка должна содержать файлы деталей (пластины, профиль, сборочные марки).\n"
            "Попробуйте другую папку или /cancel."
        )
        return WAITING_PROJECT_LINK

    # Собираем все уникальные специализации
    all_specs: list[str] = []
    for rec in scan['recognized']:
        for s in rec['specs']:
            if s not in all_specs:
                all_specs.append(s)

    all_specs_ordered = _wiz.get_all_specs()  # все 8 в правильном порядке

    # Распознанные спец — всегда в порядке ALL_SPECS, не в порядке файлов
    _found = {s for rec in scan['recognized'] for s in rec['specs']}
    recognized_specs = [s for s in all_specs_ordered if s in _found]

    context.user_data['wiz_folder_id']   = folder_id
    context.user_data['wiz_folder_name'] = scan['folder_name']
    context.user_data['wiz_recognized']  = scan['recognized']
    context.user_data['wiz_drawings_id'] = scan.get('drawings_folder_id')
    context.user_data['wiz_all_specs']   = all_specs_ordered
    context.user_data['wiz_active_specs'] = recognized_specs[:]  # по умолчанию — только найденные

    await msg.delete()
    await _wizard_show_specs(update, context)
    return WAITING_WIZARD_SPECS


def _wizard_specs_keyboard(active_specs: list[str], all_specs: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура для выбора специализаций (галочки вкл/выкл)."""
    rows = []
    for spec in all_specs:
        checked = "✅" if spec in active_specs else "☐"
        rows.append([InlineKeyboardButton(
            f"{checked} {spec}",
            callback_data=f"wiz_toggle:{spec}"
        )])
    rows.append([
        InlineKeyboardButton("Отмена", callback_data="projects"),
        InlineKeyboardButton("Далее →", callback_data="wiz_next"),
    ])
    return InlineKeyboardMarkup(rows)


async def _wizard_show_specs(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_msg=None):
    """Показывает экран выбора специализаций — все 8, распознанные отмечены."""
    folder_name  = context.user_data.get('wiz_folder_name', '—')
    active_specs = context.user_data.get('wiz_active_specs', [])
    all_specs    = context.user_data.get('wiz_all_specs', _wiz.get_all_specs())
    recognized   = context.user_data.get('wiz_recognized', [])

    # Какие файлы найдены
    files_txt = ""
    for rec in recognized:
        safe_name = rec['file']['name'].replace('_', r'\_')
        files_txt += f"  • {safe_name} → {', '.join(rec['specs'])}\n"
    if not files_txt:
        files_txt = "  (файлы не найдены)\n"

    text = (
        f"📁 *{folder_name}*\n\n"
        f"Найдено в папке:\n{files_txt}\n"
        f"Выберите специализации для проекта:"
    )
    kb = _wizard_specs_keyboard(active_specs, all_specs)

    if edit_msg:
        await edit_msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def wizard_toggle_spec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик переключения галочки специализации."""
    query = update.callback_query
    await query.answer()
    spec = query.data.split(":", 1)[1]

    active = context.user_data.get('wiz_active_specs', [])
    if spec in active:
        active.remove(spec)
    else:
        active.append(spec)
    context.user_data['wiz_active_specs'] = active

    await _wizard_show_specs(update, context, edit_msg=query.message)
    return WAITING_WIZARD_SPECS


async def wizard_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: строит source_map и показывает экран источников данных."""
    query = update.callback_query
    await query.answer()

    active_specs = context.user_data.get('wiz_active_specs', [])
    if not active_specs:
        await query.answer("Выберите хотя бы одну специализацию!", show_alert=True)
        return WAITING_WIZARD_SPECS

    recognized  = context.user_data.get('wiz_recognized', [])
    folder_name = context.user_data.get('wiz_folder_name', '—')

    # Строим маппинг spec → файл
    source_map = _wiz.build_source_map(active_specs, recognized)
    context.user_data['wiz_source_map'] = source_map

    await _wizard_show_sources(query.message, folder_name, source_map)
    return WAITING_WIZARD_SOURCES


async def _wizard_show_sources(msg, folder_name: str, source_map: list[dict]) -> None:
    """Отрисовывает экран источников данных с возможностью отжать файл."""
    _md_esc = lambda s: s.replace("_", "\_").replace("*", "\*").replace("`", "\`")
    folder_safe = _md_esc(folder_name)
    text_lines = [f"📋 *{folder_safe} — наряды*\n", "Данные для каждой вкладки:"]
    kb_rows = []

    for si, item in enumerate(source_map):
        spec    = item['spec']
        entries = item.get('entries', [])
        if not entries:
            text_lines.append(f"  ⬜ {spec}  —  пустая вкладка")
        else:
            for ei, entry in enumerate(entries):
                active = entry.get('active', True)
                stype  = entry['source_type']
                fname  = entry['file']['name']
                fname_safe = _md_esc(fname)
                icon   = "✅" if active else "☐"
                suffix = " (из группы СБОРКА)" if stype == 'fallback' else ""
                text_lines.append(f"  {icon} {spec}  ←  {fname_safe}{suffix}")
                kb_rows.append([InlineKeyboardButton(
                    f"{'✅' if active else '☐'} {spec}: {fname}",
                    callback_data=f"wiz_src_toggle:{si}:{ei}",
                )])

    text_lines.append(f"\nНазвание файла: *{folder_safe} - наряды*")
    text = "\n".join(text_lines)

    kb_rows.append([
        InlineKeyboardButton("← Назад", callback_data="wiz_sources_back"),
        InlineKeyboardButton("✅ Создать", callback_data="wiz_create"),
    ])
    kb = InlineKeyboardMarkup(kb_rows)
    await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")


async def wizard_toggle_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает активность конкретного файла-источника."""
    query = update.callback_query
    await query.answer()
    _, si_str, ei_str = query.data.split(':')
    si, ei = int(si_str), int(ei_str)

    source_map = context.user_data.get('wiz_source_map', [])
    try:
        entry = source_map[si]['entries'][ei]
        entry['active'] = not entry.get('active', True)
    except (IndexError, KeyError):
        return WAITING_WIZARD_SOURCES

    context.user_data['wiz_source_map'] = source_map
    folder_name = context.user_data.get('wiz_folder_name', '—')
    await _wizard_show_sources(query.message, folder_name, source_map)
    return WAITING_WIZARD_SOURCES

async def wizard_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться к выбору специализаций (из экрана источников)."""
    query = update.callback_query
    await query.answer()
    await _wizard_show_specs(update, context, edit_msg=query.message)
    return WAITING_WIZARD_SPECS


# Алиас для кнопки на экране источников

async def wizard_cut_drawings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нарезает PDF чертежей на отдельные страницы и привязывает к позициям."""
    query = update.callback_query
    await query.answer()

    info = context.user_data.pop('wiz_cut', None)
    if not info:
        await query.message.edit_text('❌ Данные сессии устарели. Начните создание проекта заново.')
        return

    sheet_id     = info['sheet_id']
    drawings_id  = info['drawings_id']
    active_specs = info['active_specs']

    await query.message.edit_text('⏳ Нарезаю PDF на отдельные страницы...')
    try:
        pos_map = await _bg(_wiz.cut_pdf_drawings, drawings_id)
    except Exception as e:
        app_logger.alert(f'cut_pdf_drawings error: {e}')
        await query.message.edit_text(f'❌ Ошибка при нарезке чертежей:\n{e}')
        return

    recognized = len(pos_map)
    await query.message.edit_text(
        f'✂️ Нарезка завершена: {recognized} позиций распознано.\n'
        f'⏳ Привязываю к строкам таблицы...'
    )
    try:
        linked = await _bg(_wiz.link_drawings, sheet_id, drawings_id, active_specs)
    except Exception as e:
        app_logger.alert(f'link_drawings after cut error: {e}')
        await query.message.edit_text(
            f'✂️ Нарезано: {recognized} позиций.\n'
            f'⚠️ Ошибка при привязке к таблице: {e}'
        )
        return

    await query.message.edit_text(
        f'✅ Готово!\n'
        f'✂️ Нарезано страниц: {recognized}\n'
        f'🔗 Привязано к строкам: {linked}'
    )


wizard_sources_back = wizard_back


async def wizard_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватчик: проверяет неизвестные секции перед созданием нарядов."""
    query = update.callback_query
    await query.answer()

    source_map = context.user_data.get('wiz_source_map') or []
    if not source_map:
        source_map = _wiz.build_source_map(
            context.user_data.get('wiz_active_specs', []),
            context.user_data.get('wiz_recognized', []),
        )
        context.user_data['wiz_source_map'] = source_map

    # Детектируем неизвестные секции (только если ещё не сделали)
    if 'wiz_unknown_sections' not in context.user_data:
        try:
            unknown = _wiz.detect_unknown_sections(source_map)
        except Exception as e:
            app_logger.alert(f'wizard detect_unknown_sections error: {e}')
            unknown = {}
        context.user_data['wiz_unknown_sections'] = unknown
        context.user_data['wiz_section_mapping'] = {}

    unknown = context.user_data.get('wiz_unknown_sections', {})
    # Если неизвестных нет → сразу к созданию
    if not unknown:
        return await _wizard_do_create(update, context)

    await _wizard_show_sections(query.message, unknown, context.user_data.get('wiz_section_mapping', {}))
    return WAITING_WIZARD_SECTIONS


async def _wizard_show_sections(msg, unknown: dict, current_mapping: dict) -> None:
    """Показывает экран маппинга неизвестных секций."""
    lines = ['⚠️ *Найдены неизвестные типы сечений*',
             'Укажи куда отнести каждое:']
    kb_rows = []
    for sec_val, positions in unknown.items():
        pos_preview = ', '.join(positions[:3]) + ('…' if len(positions) > 3 else '')
        mapped = current_mapping.get(sec_val, '')
        mark_pila  = ' ✅' if mapped == 'ПИЛА'       else ''
        mark_pla   = ' ✅' if mapped == 'ПЛАЗМА'     else ''
        mark_skip  = ' ✅' if mapped == 'ПРОПУСТИТЬ' else ''
        lines.append(f'`{sec_val}` (позиции: {pos_preview})')
        safe = sec_val.replace(':', '_')
        kb_rows.append([
            InlineKeyboardButton(f'→ ПИЛА{mark_pila}',       callback_data=f'wiz_sec:ПИЛА:{safe}'),
            InlineKeyboardButton(f'→ ПЛАЗМА{mark_pla}',      callback_data=f'wiz_sec:ПЛАЗМА:{safe}'),
            InlineKeyboardButton(f'→ Пропустить{mark_skip}', callback_data=f'wiz_sec:ПРОПУСТИТЬ:{safe}'),
        ])

    all_mapped = all(sec in current_mapping for sec in unknown)
    if all_mapped:
        kb_rows.append([InlineKeyboardButton('🚀 Создать наряды', callback_data='wiz_sections_done')])
    else:
        lines.append('_Выбери для каждого сечения куда оно относится_')

    kb_rows.append([InlineKeyboardButton('⬅️ Назад к источникам', callback_data='wiz_sources_back')])
    await msg.edit_text('\n'.join(lines), parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(kb_rows))


async def wizard_section_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора маппинга для одного сечения."""
    query = update.callback_query
    await query.answer()
    # callback_data = wiz_sec:<НАЗНАЧЕНИЕ>:<sec_val>
    parts = query.data.split(':', 2)
    if len(parts) < 3:
        return WAITING_WIZARD_SECTIONS
    _, destination, sec_safe = parts
    unknown = context.user_data.get('wiz_unknown_sections', {})
    # Найти оригинальный sec_val (с пробелами и т.п.)
    sec_val = next((k for k in unknown if k.replace(':', '_') == sec_safe), sec_safe)
    mapping = context.user_data.setdefault('wiz_section_mapping', {})
    mapping[sec_val] = destination
    await _wizard_show_sections(query.message, unknown, mapping)
    return WAITING_WIZARD_SECTIONS


async def wizard_sections_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Все секции выбраны → запускаем создание нарядов."""
    return await _wizard_do_create(update, context)


async def wizard_do_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for _wizard_do_create (for handlers)."""
    return await _wizard_do_create(update, context)


async def _wizard_do_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальный шаг: создаём Google Sheet, вкладки, грузим данные, регистрируем в БД."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    folder_id    = context.user_data['wiz_folder_id']
    folder_name  = context.user_data['wiz_folder_name']
    active_specs = context.user_data['wiz_active_specs']
    source_map   = context.user_data.get('wiz_source_map') or                    _wiz.build_source_map(active_specs, context.user_data.get('wiz_recognized', []))
    drawings_id  = context.user_data.get('wiz_drawings_id')
    project_name = f"{folder_name} - наряды"

    # ── Прогресс-экран ────────────────────────────────────────────────────────
    # Шаги: (название, нужны_чертежи)
    STEPS = [
        ("Создание таблицы",            False),
        ("Регистрация проекта",         False),
        ("Настройка вкладок",           False),
        ("Служебные вкладки",           False),
        ("Зависимости между этапами",   False),
        ("Загрузка данных из Excel",    False),
        ("Выпадающие списки",           False),
        ("Активация проекта",           False),
    ]
    active_steps = [(name, req) for name, req in STEPS]

    def _render(current: int, done_up_to: int, error_at: int = -1) -> str:
        lines = [f"*Создание проекта «{project_name}»*\n"]
        for i, (name, _) in enumerate(active_steps):
            if i < done_up_to:
                lines.append(f"✅  {name}")
            elif i == current and error_at == i:
                lines.append(f"❌  {name}")
            elif i == current:
                lines.append(f"⏳  *{name}...*")
            else:
                lines.append(f"◻️  {name}")
        return "\n".join(lines)

    async def _upd(current: int, done_up_to: int, err: int = -1):
        await query.message.edit_text(
            _render(current, done_up_to, err), parse_mode="Markdown"
        )

    # ── Шаг 0: Создание таблицы ───────────────────────────────────────────────
    si = 0
    await _upd(si, si)
    try:
        sheet_id = await _bg(_wiz.create_spreadsheet, project_name, folder_id)
    except Exception as e:
        app_logger.alert(f"wizard create_spreadsheet error: {e}")
        await _upd(si, si, err=si)
        await query.message.edit_text(f"❌ Не удалось создать таблицу:\n{e}")
        return ConversationHandler.END

    # ── Шаг 1: Регистрация (статус СОЗДАНИЕ — sync не видит) ─────────────────
    si = 1
    await _upd(si, si)
    try:
        proj = _wiz.register_project(
            project_name=project_name,
            folder_id=folder_id,
            sheet_id=sheet_id,
            drawings_folder_id=drawings_id,
            active_specs=active_specs,
            created_by=user.id,
            status='СОЗДАНИЕ',
        )
        if not proj['ok']:
            await query.message.edit_text(f"❌ {proj['error']}")
            return ConversationHandler.END
        project_id = proj['id']
    except Exception as e:
        app_logger.alert(f"wizard register_project error: {e}")
        await query.message.edit_text(f"❌ Ошибка при регистрации:\n{e}")
        return ConversationHandler.END

    # ── Шаг 2: Вкладки специализаций ──────────────────────────────────────────
    si = 2
    await _upd(si, si)
    try:
        await _bg(_wiz.setup_sheet_tabs, sheet_id, active_specs)
    except Exception as e:
        app_logger.alert(f"wizard setup_sheet_tabs error: {e}")
        await query.message.edit_text(f"❌ Ошибка при создании вкладок:\n{e}")
        return ConversationHandler.END

    # ── Шаг 3: Служебные вкладки ──────────────────────────────────────────────
    si = 3
    await _upd(si, si)
    try:
        await _bg(_wiz.setup_service_tabs, sheet_id, active_specs)
    except Exception as e:
        app_logger.alert(f"wizard setup_service_tabs error: {e}")
        await query.message.edit_text(f"❌ Ошибка при создании служебных вкладок:\n{e}")
        return ConversationHandler.END

    # ── Шаг 4: Зависимости ────────────────────────────────────────────────────
    si = 4
    await _upd(si, si)
    try:
        await _bg(_wiz.fill_deps_sheet, sheet_id, active_specs)
    except Exception as e:
        app_logger.alert(f"wizard fill_deps_sheet error: {e}")

    # ── Шаг 5: Загрузка данных из Excel ───────────────────────────────────────
    si = 5
    await _upd(si, si)
    try:
        _ref_files = [item['file'] for item in context.user_data.get('wiz_recognized', [])
                      if any(s in item.get('specs', []) for s in ('СБОРКА', 'СПРАВОЧНИК'))]
        _sec_mapping = context.user_data.get('wiz_section_mapping', {})
        # Дополняем маппинг из project_rules.json (секции уже известные)
        try:
            import json as _json_sm_r
            with open('/root/naryady/prod/project_rules.json', 'r', encoding='utf-8') as _f_smr:
                _rules_sm_r = _json_sm_r.load(_f_smr)
            _rules_sec = _rules_sm_r.get('section_mapping', {})
            if _rules_sec:
                _merged = dict(_rules_sec)
                _merged.update(_sec_mapping)  # wiz имеет приоритет над rules
                _sec_mapping = _merged
        except Exception:
            pass
        # Сохраняем маппинг сечений в project_rules.json для будущих проектов
        if _sec_mapping:
            try:
                import json as _json_sm
                with open('/root/naryady/prod/project_rules.json', 'r', encoding='utf-8') as _f_sm:
                    _rules_sm = _json_sm.load(_f_sm)
                _rules_sm.setdefault('section_mapping', {}).update(_sec_mapping)
                with open('/root/naryady/prod/project_rules.json', 'w', encoding='utf-8') as _f_sm:
                    _json_sm.dump(_rules_sm, _f_sm, ensure_ascii=False, indent=2)
            except Exception as _e_sm:
                app_logger.warning(f'section_mapping save failed: {_e_sm}')
        counts = await _bg(_wiz.load_excel_data, sheet_id, source_map, reference_files=_ref_files, section_mapping=_sec_mapping)
    except Exception as e:
        import traceback; app_logger.alert("wizard load_excel_data error: " + str(e) + chr(10) + traceback.format_exc())
        return ConversationHandler.END

    # ── Шаг 6: Выпадающие списки ──────────────────────────────────────────────
    si = 6
    await _upd(si, si)
    try:
        await _bg(_wiz.add_data_validations, sheet_id, active_specs, counts)
    except Exception as e:
        app_logger.alert(f"wizard add_data_validations error: {e}")

    # Привязка чертежей — ждём 30с чтобы quota read сбросилась после load_excel_data
    await asyncio.sleep(30)
    link_count = 0
    drawings_id = context.user_data.get('wiz_drawings_id')
    if drawings_id:
        try:
            link_count = await _bg(_wiz.link_drawings, sheet_id, drawings_id, active_specs)
            app_logger.info(f"wizard link_drawings: {link_count} ссылок привязано")
        except Exception as _le:
            app_logger.alert(f"wizard link_drawings error: {_le}")
    # ── Авто-генерация element_dependencies и запись в ЗАВИСИМОСТИ лист ────────
    try:
        from src import sync as _sync
        from src.sheets import _gc as _gc_sh_pre, SCOPES_RW as _SCOPES_RW_pre
        import time as _time_rb; _time_rb.sleep(120)  # ждём сброса read-квоты (2 окна)
        _sync._rebuild_element_dependencies(project_name, sheet_id)
        from src.db import get_conn as _gc_db
        import src.sheets as _sh_mod
        with _gc_db() as _conn:
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT waiting_sheet, element, requires_sheet, requires_position "
                "FROM element_dependencies WHERE project_name=%s ORDER BY waiting_sheet, element",
                (project_name,)
            )
            _deps = _cur.fetchall()
        if _deps:
            _ws_z = _sh_mod._ws(sheet_id, 'ЗАВИСИМОСТИ', rw=True)
            _sh_mod._wapi_call(_ws_z.clear)
            _sh_mod._wapi_call(
                _ws_z.update,
                [['', 'КТО ЖДЁТ (специализация)', 'ЭЛЕМЕНТ (который ждёт)', 'ЗАВИСИТ ОТ (специализация)', 'ПОЗИЦИЯ (должна быть выполнена)', '']],
                range_name='A1'
            )
            _sh_mod._wapi_call(
                _ws_z.update,
                [['', w, e, r, p, ''] for w, e, r, p in _deps],
                range_name='A2'
            )
            app_logger.info(f"wizard deps_sheet: {len(_deps)} records written")
        for _spec in active_specs:
            try:
                _sync._update_block_column(sheet_id, _spec, project_name)
            except Exception as _be:
                app_logger.alert(f"wizard block_col {_spec}: {_be}")
    except Exception as _de:
        app_logger.alert(f"wizard rebuild_deps error: {_de}")

    # ── Количество деталей на сборку → лист ЗАВИСИМОСТИ (колонки F/G); после перезаписи листа выше ──────────────
    comp_txt = ""
    try:
        from src import component_deps as _cd
        _afiles = _cd.assembly_files_from_source_map(source_map)
        if _afiles:
            _crep = await _bg(_cd.fill_component_deps, sheet_id, _afiles)
            app_logger.info(f"wizard component_deps: записано {_crep.get('written')} строк, сборок {_crep.get('assemblies')}, "
                            f"нераспознанных деталей {len(_crep.get('unresolved', []))}, расхождений {len(_crep.get('mismatches', []))}")
            _notes = []
            for _spec_m, _pos_m, _have_m, _need_m, _kind_m in _crep.get('mismatches', [])[:5]:
                _notes.append(f"  • {_spec_m}: {_pos_m} — в листе {_have_m:g}, нужно сборкам {_need_m:g} ({_kind_m})")
            for _a_m, _p_m in _crep.get('unresolved', [])[:3]:
                _notes.append(f"  • деталь {_p_m} (сборка {_a_m}) не найдена в листах ПЛАЗМА/ПИЛА")
            if _crep.get('not_in_sheet'):
                _notes.append(f"  • сборок из файла нет в листе СБОРКА: {len(_crep['not_in_sheet'])}")
            if _notes:
                comp_txt = "\n\n⚠️ Проверьте количество деталей на сборку:\n" + "\n".join(_notes).replace('_', ' ').replace('*', '').replace('`', '')
    except Exception as _ce:
        app_logger.alert(f"wizard component_deps error: {_ce}")

    # ── Последний шаг: активация (проект становится виден для sync) ────────────
    si = len(active_steps) - 1
    await _upd(si, si)
    try:
        _wiz.activate_project(project_id)
    except Exception as e:
        app_logger.alert(f"wizard activate_project error: {e}")
        await query.message.edit_text(f"❌ Ошибка при активации проекта:\n{e}")
        return ConversationHandler.END


    # Автосинхронизация в фоне (не блокируем визард)
    async def _bg_sync():
        try:
            _proc = _spawn_sync(project_id, None, force=True)
            while _proc.poll() is None:
                await asyncio.sleep(10)
            app_logger.info(f"wizard: bg sync завершён для нового проекта {project_id} (код {_proc.returncode})")
        except Exception as _se:
            app_logger.alert(f"wizard post-create sync error: {_se}")
    asyncio.ensure_future(_bg_sync())
    # Итоговое сообщение
    rows_txt = "\n".join(f"  • {spec}: {cnt} строк" for spec, cnt in counts.items()) if counts else "  (нет данных)"
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    drawings_txt = ""

    await query.message.edit_text(
        f"✅ *Проект создан!*\n\n"
        f"📁 {folder_name}\n"
        f"🔗 [Открыть таблицу]({sheet_url})\n\n"
        f"Загружено:\n{rows_txt}{drawings_txt}{comp_txt}\n\n"
        f"Данные появятся у рабочих после следующей синхронизации (до 15 мин).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗂 К проектам", callback_data="projects"),
            InlineKeyboardButton("← Меню", callback_data="menu"),
        ]]),
        parse_mode="Markdown"
    )

    # Очищаем wizard-данные из user_data
    for k in ['wiz_folder_id', 'wiz_folder_name', 'wiz_recognized', 'wiz_drawings_id', 'wiz_active_specs', 'wiz_all_specs', 'wiz_source_map']:
        context.user_data.pop(k, None)

    return ConversationHandler.END


async def receive_earnings_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parts = text.split('.')
        if len(parts) != 3:
            raise ValueError
        d = date(int(parts[2]), int(parts[1]), int(parts[0]))
        date_str = d.isoformat()
    except (ValueError, IndexError):
        await update.message.reply_text(
            '❌ Неверный формат. Введите дату ДД.ММ.ГГГГ\n\nНапример: 14.07.2026\n\n/cancel — отмена'
        )
        return WAITING_EARNINGS_DATE
    await show_earnings(update, date_str, edit=False)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('block_task_id', None)
    context.user_data.pop('correct_task_id', None)
    context.user_data.pop('correct_max_qty', None)
    context.user_data.pop('correct_remainder_id', None)
    await update.message.reply_text("Отменено.", reply_markup=back_to_menu_kb())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SYNC_PY = '/root/naryady/prod/src/sync.py'


def _spawn_sync(project_id=None, progress_file=None, force=False):
    """Запускает синк ОТДЕЛЬНЫМ процессом: сбой/SSL-ошибка синка не заденет бота,
    клиенты Google не делятся между потоками. Периодический синк — systemd-таймер naryady-sync-prod."""
    import subprocess, sys as _sys
    cmd = [_sys.executable, SYNC_PY]
    if project_id is not None:
        cmd += ['--project', str(project_id)]
    if force:
        cmd.append('--force')
    if progress_file:
        cmd += ['--progress-file', progress_file]
    logf = open('/root/naryady/prod/logs/sync.log', 'a')
    return subprocess.Popen(cmd, cwd='/root/naryady/prod', stdout=logf, stderr=logf, start_new_session=True)


async def _job_cleanup_audit(context):
    """Еженедельная очистка audit_log: sync_sheet > 14 дней, остальное > 90 дней."""
    try:
        deleted_tech = db.execute(
            "DELETE FROM audit_log WHERE action='sync_sheet' AND ts < NOW() - INTERVAL '14 days'",
            []
        )
        deleted_old = db.execute(
            "DELETE FROM audit_log WHERE action!='sync_sheet' AND ts < NOW() - INTERVAL '90 days'",
            []
        )
        app_logger.info(f"job_cleanup_audit: удалено {deleted_tech} tech + {deleted_old} user строк")
    except Exception as e:
        app_logger.alert(f"job_cleanup_audit error: {e}")


# Единый диалог «бот ждёт от пользователя текст»: у каждого пользователя одно ожидание ввода.
# Новый запрос ввода заменяет прежний (allow_reentry), любая другая кнопка и /start сбрасывают ожидание,
# забытый ввод истекает через 10 минут.
NAV_EXCLUDE_RE = r'^(?!project:add$|earnings_pick$|wiz_)'   # входы других диалогов (мастер проекта, заработок)
_PENDING_INPUT_KEYS = ('partial_task_id', 'partial_deps_avail', 'block_task_id', 'correct_task_id',
                       'correct_max_qty', 'correct_remainder_id', 'edit_emp_id')


def _clear_pending_input(context):
    for _k in _PENDING_INPUT_KEYS:
        context.user_data.pop(_k, None)


async def _start_and_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_pending_input(context)
    await cmd_start(update, context)
    return ConversationHandler.END


async def _nav_and_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь нажал другую кнопку: сбрасываем ожидание ввода и выполняем кнопку как обычно."""
    _clear_pending_input(context)
    await on_callback(update, context)
    return ConversationHandler.END


async def _stray_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сейчас я не жду от вас сообщения. Нажмите /start и выберите действие в меню.")


def build_input_conversation(entry_cb, h_partial, h_block, h_correction, h_emp_name, h_approve_name,
                             h_cancel, h_start, h_nav):
    txt = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_cb, pattern=r'^partial_ask:\d+$'),
            CallbackQueryHandler(entry_cb, pattern=r'^block_confirm:\d+$'),
            CallbackQueryHandler(entry_cb, pattern=r'^correct:qty:\d+$'),
            CallbackQueryHandler(entry_cb, pattern=r'^admin:name:\d+$'),
            CallbackQueryHandler(entry_cb, pattern=r'^admin:approve:\d+$'),
        ],
        states={
            WAITING_PARTIAL_QTY:    [MessageHandler(txt, h_partial)],
            WAITING_BLOCK_COMMENT:  [MessageHandler(txt, h_block)],
            WAITING_CORRECTION_QTY: [MessageHandler(txt, h_correction)],
            WAITING_EMP_NAME:       [MessageHandler(txt, h_emp_name)],
            WAITING_APPROVE_NAME:   [MessageHandler(txt, h_approve_name)],
        },
        fallbacks=[
            CommandHandler('cancel', h_cancel),
            CommandHandler('start', h_start),
            CallbackQueryHandler(h_nav, pattern=NAV_EXCLUDE_RE),
        ],
        allow_reentry=True,
        conversation_timeout=600,
        per_message=False,
    )


_FAULT_LOG = None


def main():
    global _FAULT_LOG
    import faulthandler, datetime as _dt_fh, os as _os_fh
    try:
        _FAULT_LOG = open('/root/naryady/prod/logs/faulthandler.log', 'a', buffering=1)
        _FAULT_LOG.write(f"\n=== start {_dt_fh.datetime.now().isoformat()} pid={_os_fh.getpid()} ===\n")
        faulthandler.enable(file=_FAULT_LOG, all_threads=True)
    except Exception as _e_fh:
        app_logger.warning(f'faulthandler не включён: {_e_fh}')
    app = Application.builder().token(config.TG_TOKEN).build()
    app_logger.set_bot(app.bot)

    project_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r'^project:add$')],
        states={
            WAITING_PROJECT_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_project_link)
            ],
            WAITING_WIZARD_SPECS: [
                CallbackQueryHandler(wizard_toggle_spec, pattern=r'^wiz_toggle:'),
                CallbackQueryHandler(wizard_next,        pattern=r'^wiz_next$'),
            ],
            WAITING_WIZARD_SOURCES: [
                CallbackQueryHandler(wizard_toggle_source, pattern=r'^wiz_src_toggle:'),
                CallbackQueryHandler(wizard_sources_back,  pattern=r'^wiz_sources_back$'),
                CallbackQueryHandler(wizard_create,        pattern=r'^wiz_create$'),
            ],
            WAITING_WIZARD_SECTIONS: [
                CallbackQueryHandler(wizard_section_pick,   pattern=r'^wiz_sec:'),
                CallbackQueryHandler(wizard_sections_done,  pattern=r'^wiz_sections_done$'),
                CallbackQueryHandler(wizard_sources_back,   pattern=r'^wiz_sources_back$'),
            ],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        allow_reentry=True,
        per_message=False,
    )

    earnings_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r'^earnings_pick$')],
        states={
            WAITING_EARNINGS_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_earnings_date)
            ],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        per_message=False,
    )

    input_conv = build_input_conversation(
        on_callback, receive_partial_qty, receive_block_comment, receive_correction_qty,
        receive_emp_name, receive_approve_name, cmd_cancel, _start_and_reset, _nav_and_reset)
    app.add_handler(input_conv)      # раньше /start: чтобы /start сбрасывал ожидание ввода
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(project_conv)
    app.add_handler(CallbackQueryHandler(wizard_cut_drawings, pattern=r'^wiz_cut_drawings$'))
    app.add_handler(earnings_conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, _stray_text))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        import traceback
        err = context.error
        tb = ''.join(traceback.format_exception(type(err), err, err.__traceback__))

        # Не шумим на безвредные Telegram-ошибки
        _err_str = str(err)
        if 'Message is not modified' in _err_str:
            return
        if 'Query is too old' in _err_str or 'query id is invalid' in _err_str:
            return

        app_logger.alert(f"Unhandled exception: {err}\n{tb}")

        # Сообщение пользователю
        user_text = (
            "⚠️ Произошла ошибка. Нажмите /start или кнопку Меню.\n"
            "При повторении ошибки обратитесь к администратору."
        )
        try:
            if isinstance(update, Update):
                if update.callback_query:
                    await update.callback_query.answer()
                    await update.callback_query.message.reply_text(user_text)
                elif update.effective_message:
                    await update.effective_message.reply_text(user_text)
        except Exception:
            pass

        # Алерт администратору
        ADMIN_CHAT_ID = 340620064
        user_info = ''
        if isinstance(update, Update) and update.effective_user:
            u = update.effective_user
            user_info = f"👤 {u.full_name} (@{u.username})\n"
        admin_text = (
            f"🔴 <b>Ошибка в боте</b>\n\n"
            f"{user_info}"
            f"<code>{str(err)[:300]}</code>"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode='HTML')
        except Exception:
            pass

    app.add_error_handler(error_handler)

    async def post_init(app):
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonCommands()
        )
        await app.bot.set_my_commands([
            BotCommand('start', 'Открыть меню'),
        ])
        # Периодический синк — systemd-таймер naryady-sync-prod (отдельный процесс), здесь не запускаем
        # Очистка audit_log каждое воскресенье в 03:00
        app.job_queue.run_daily(_job_cleanup_audit, time=dtime(3, 0), days=(6,))
        app_logger.info("JobQueue: cleanup_audit(вс 03:00) зарегистрирован; синк — отдельный процесс (systemd)")

    app.post_init = post_init
    app_logger.info(f"Bot started. TEST_MODE={config.TEST_MODE}")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
