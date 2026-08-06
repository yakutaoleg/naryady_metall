import logging
import sys
import os
import io
import re
import asyncio
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from src import config, db
from src import project_wizard as _wiz, logger as app_logger
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

WAITING_BLOCK_COMMENT  = 1
WAITING_PROJECT_LINK   = 2
WAITING_PARTIAL_QTY    = 3
WAITING_EARNINGS_DATE  = 4
WAITING_CORRECTION_QTY = 5
WAITING_WIZARD_SPECS   = 6
WAITING_WIZARD_CONFIRM = 7
WAITING_WIZARD_SOURCES = 8
TODAY = lambda: date.today().strftime('%d.%m.%Y')


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


def scan_folder_and_register(folder_id: str, created_by: int) -> dict:
    """Сканирует папку Drive, находит таблицу и папку чертежей, сохраняет в projects.
    Возвращает {'ok': True, 'project_name': ..., 'id': ...} или {'ok': False, 'error': ...}
    """
    # Проверка на дубликат
    existing = db.fetchone("SELECT id, project_name FROM projects WHERE folder_id=%s", [folder_id])
    if existing:
        return {'ok': False, 'error': f"Проект «{existing['project_name']}» уже добавлен."}

    creds = _SACredentials.from_service_account_file(
        config.GOOGLE_SA_KEY,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    drive = _gdrive_build('drive', 'v3', credentials=creds)

    # Ищем Google Sheets файл в папке
    q_sheet = (
        f"mimeType='application/vnd.google-apps.spreadsheet' "
        f"and trashed=false and '{folder_id}' in parents"
    )
    sheets_res = drive.files().list(q=q_sheet, fields='files(id,name)', pageSize=10).execute()
    sheets_found = sheets_res.get('files', [])
    if not sheets_found:
        return {'ok': False, 'error': 'В папке не найдена таблица Google Sheets.'}
    if len(sheets_found) > 1:
        return {'ok': False, 'error': f'В папке найдено несколько таблиц ({len(sheets_found)}). Оставьте одну.'}

    sheet = sheets_found[0]
    project_name = sheet['name']
    sheet_id = sheet['id']

    # Ищем папку чертежей (любая подпапка)
    q_folder = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false and '{folder_id}' in parents"
    )
    folders_res = drive.files().list(q=q_folder, fields='files(id,name)', pageSize=10).execute()
    drawings_folder_id = None
    for f in folders_res.get('files', []):
        if 'черт' in f['name'].lower() or 'drawing' in f['name'].lower():
            drawings_folder_id = f['id']
            break
    if not drawings_folder_id and folders_res.get('files'):
        drawings_folder_id = folders_res['files'][0]['id']

    db.execute(
        """INSERT INTO projects (project_name, folder_id, sheet_id, drawings_folder_id, status, created_by)
           VALUES (%s, %s, %s, %s, 'АКТИВНЫЙ', %s)""",
        [project_name, folder_id, sheet_id, drawings_folder_id, created_by]
    )
    project = db.fetchone("SELECT id FROM projects WHERE folder_id=%s", [folder_id])
    return {'ok': True, 'project_name': project_name, 'id': project['id']}


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
             AND (date_plan IS NULL OR date_plan <= CURRENT_DATE)
           ORDER BY mandatory DESC, sheet_name, priority ASC NULLS LAST, position""",
        [worker_name]
    )
    for t in tasks:
        if t['sheet_name'].upper() in SHEETS_WITH_DEPS:
            try:
                t['deps_ready'] = _deps_ready(t['project_name'], t['element'], t['sheet_name'])
            except Exception as e:
                app_logger.alert(f"_deps_ready error task_id={t['id']} element={t['element']}: {e}")
                t['deps_ready'] = True
        else:
            t['deps_ready'] = True
    return tasks


def _deps_ready(project_name: str, element: str, sheet_name: str = None) -> bool:
    """Возвращает True если все зависимости для элемента выполнены (или зависимостей нет)."""
    if not element:
        return True
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
        # Проверяем по element (не position): все строки с этим элементом в upstream листе
        rows = db.fetchall(
            """SELECT status FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND element=%s""",
            [project_name, dep['requires_sheet'], dep['requires_position']]
        )
        # Нет строк — upstream не заведён, считаем что не блокирует
        # Достаточно хотя бы одной ВЫПОЛНЕНО строки (частичное выполнение не блокирует)
        if rows and not any(r['status'] == 'ВЫПОЛНЕНО' for r in rows):
            return False
    return True


def _deps_blocking(project_name: str, element: str, sheet_name: str = None) -> list[str]:
    """Возвращает список незакрытых зависимостей в виде читаемых строк."""
    if not element:
        return []
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
    blocking = []
    for dep in deps:
        rows = db.fetchall(
            """SELECT status FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND element=%s""",
            [project_name, dep['requires_sheet'], dep['requires_position']]
        )
        if rows and not any(r['status'] == 'ВЫПОЛНЕНО' for r in rows):
            done = sum(1 for r in rows if r['status'] == 'ВЫПОЛНЕНО')
            blocking.append(f"{dep['requires_sheet']}: {dep['requires_position']} ({done}/{len(rows)} вып.)")
    return blocking


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
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu")],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("📋 Задачи на сегодня", callback_data="tasks"),
                InlineKeyboardButton("✅ Выполнено сегодня", callback_data="done_today"),
            ],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu")],
        ]
    return InlineKeyboardMarkup(rows)


def projects_list_kb(projects: list):
    rows = []
    for p in projects:
        status_icon = "🟢" if p['status'] == 'АКТИВНЫЙ' else "📦"
        rows.append([InlineKeyboardButton(
            f"{status_icon} {p['project_name']}",
            callback_data=f"project:detail:{p['id']}"
        )])
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
    rows.append([InlineKeyboardButton("← К проектам", callback_data="projects")])
    return InlineKeyboardMarkup(rows)


def tasks_list_kb(tasks: list, blocked: list, mandatory_left: list):
    buttons = []
    multi_spec = len({t['sheet_name'] for t in tasks}) > 1
    for t in tasks:
        if not t.get('deps_ready', True):
            prefix = "⛔ "
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
            f"{icon} {t['position']} × {qty} шт ({dfact})",
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


async def show_projects(update: Update, edit: bool = False):
    projects = get_projects()
    active = [p for p in projects if p['status'] == 'АКТИВНЫЙ']
    archived = [p for p in projects if p['status'] != 'АКТИВНЫЙ']
    lines = ["🗂 Управление проектами\n"]
    if active:
        lines.append(f"🟢 Активных: {len(active)}")
    if archived:
        lines.append(f"📦 В архиве: {len(archived)}")
    if not projects:
        lines.append("Проектов пока нет.\nДобавьте первый проект.")
    text = "\n".join(lines)
    kb = projects_list_kb(projects)
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.username, user.id)

    app_logger.audit('bot_start', user.id, user.username,
                     {'found': bool(worker)}, 'success' if worker else 'not_found')

    if not worker:
        await update.message.reply_text(
            f"Привет!\nВы не найдены в системе.\n"
            f"Ваш Telegram: @{user.username}\n"
            f"Обратитесь к руководителю для регистрации."
        )
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
                      (date_plan < CURRENT_DATE AND mandatory = true) AS overdue
               FROM work_orders
               WHERE executor IS NOT NULL AND executor != ''
                 AND (date_plan = CURRENT_DATE
                  OR (mandatory = true AND date_plan < CURRENT_DATE AND status = 'ПЛАН'))
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
                        qty = f" × {int(t['quantity'])}" if t['quantity'] else ""
                        elem = f" ({t['element']})" if t['element'] else ""
                        sicon = STATUS_ICON.get(t['status'], '☐')
                        overdue_mark = f" (от {t['date_plan'].strftime('%d.%m')})" if t.get('overdue') else ""
                        lines.append(f"      {sicon} {t['position']}{elem}{qty}{overdue_mark}")
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
        # callback query expired (>48h) — notify user and stop
        try:
            await query.message.reply_text(
                "⚠️ Кнопка устарела — откройте меню заново командой /start"
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

    elif data == "projects":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        await show_projects(update, edit=True)

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

        # Живое отображение прогресса
        proj_name = project['project_name']
        sheet_lines: list[str] = []
        msg_text   = [f"🔄 Синхронизация: {proj_name}\n"]
        loop = asyncio.get_event_loop()

        def _build_text():
            return "\n".join(msg_text + sheet_lines[-12:])

        def _safe_edit(text):
            async def _do():
                try:
                    await query.edit_message_text(text)
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(_do(), loop)

        def progress_cb(phase, data):
            if phase == 'project_start':
                if data['project'] != proj_name:
                    msg_text.append(f"\n📁 {data['project']}")
                    sheet_lines.clear()
            elif phase == 'sheet_done':
                rows   = data['rows']
                splits = data.get('splits', 0)
                icon   = '✅' if rows > 0 else '➖'
                line   = f"{icon} {data['sheet']}: {rows} стр."
                if splits:
                    line += f" (+{splits} сплит)"
                sheet_lines.append(line)
                done  = data['sheets_done']
                total = data['total_sheets']
                filled = int(done / total * 10)
                bar = '▓' * filled + '░' * (10 - filled)
                progress_line = f"[{bar}] {done}/{total}"
                _safe_edit(_build_text() + f"\n⏳ {progress_line}")
            elif phase == 'deps_done':
                sheet_lines.append("\n🔗 Зависимости обновлены")
                _safe_edit(_build_text())
            elif phase == 'sync_done':
                total = data['total']
                final = _build_text() + f"\n\n✅ Готово! Обработано строк: {total}"
                async def _finalize():
                    try:
                        await query.edit_message_text(
                            final,
                            reply_markup=project_detail_kb(project_id, 'АКТИВНЫЙ')
                        )
                    except Exception:
                        pass
                asyncio.run_coroutine_threadsafe(_finalize(), loop)

        try:
            from src import sync as _sync
            await loop.run_in_executor(None, _sync.run, progress_cb)
        except Exception as e:
            app_logger.alert(f"sync trigger error: {e}")
            await query.answer("Ошибка синхронизации.", show_alert=True)

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

    elif data == "master:blocks":
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        blocked = get_all_blocked()
        if not blocked:
            await query.edit_message_text(
                "✅ Активных блоков нет.",
                reply_markup=back_to_menu_kb()
            )
            return
        lines = [f"🚫 Активные блоки — {len(blocked)} шт\n"]
        for b in blocked:
            when = b['updated_at'].strftime('%d.%m %H:%M') if b['updated_at'] else '—'
            lines.append(
                f"• [{b['sheet_name']}] {b['position']} — {b['element'] or '—'}\n"
                f"  👷 {b['executor']} | 📁 {b['project_name']}\n"
                f"  💬 {b['comment'] or '—'} | 🕐 {when}"
            )
        buttons = []
        for b in blocked:
            label = f"✅ Снять: {b['position']} — {b['element'] or ''} ({b['sheet_name']})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"unblock:{b['id']}")])
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
        context.user_data['partial_task_id'] = task_id
        await query.edit_message_text(
            f"◧ Частичное выполнение\n\n"
            f"Позиция: {task['position']} — {task['element'] or ''}\n"
            f"Всего: {qty_total} шт | Осталось: {remaining} шт\n\n"
            f"Сколько выполнили? (от 1 до {remaining})"
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
        await query.answer("Блок снят.")
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
        date_str = parts[2] if len(parts) > 2 else (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
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
        date_str = parts[1] if len(parts) > 1 else (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
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
            f"Сколько реально сделано? (от 1 до {max_qty})"
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
            "⛔ Укажите причину блокировки:\n(напишите ответным сообщением)"
        )
        return WAITING_BLOCK_COMMENT


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

    if qty_new <= 0 or qty_done_total > qty_total:
        remaining = qty_total - qty_done_before
        await update.message.reply_text(f"Некорректное число. Осталось: {remaining} шт.")
        context.user_data['partial_task_id'] = task_id
        return WAITING_PARTIAL_QTY

    today = TODAY()
    worker_name, specialization, _ = _get_worker_context(update, context)
    employee = db.fetchone("SELECT id FROM employees WHERE full_name=%s", [worker_name])
    employee_id = employee['id'] if employee else None

    unit_price = (float(task['payment_sum'] or 0) / float(task['quantity'] or 1)) if task['quantity'] else 0
    history_payment = round(unit_price * qty_new, 2)

    db.execute(
        "INSERT INTO work_order_history (work_order_id, row_id, employee_id, qty, payment_sum, date_done) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_DATE)",
        [task_id, task['row_id'], employee_id, qty_new, history_payment]
    )

    if qty_done_total == qty_total:
        db.execute(
            "UPDATE work_orders SET status='ВЫПОЛНЕНО', qty_done=%s, date_fact=CURRENT_DATE WHERE id=%s",
            [qty_done_total, task_id]
        )
        try:
            update_task_status(
                file_id=task['file_id'], sheet_name=task['sheet_name'], row_num=task['row_num'],
                status='ВЫПОЛНЕНО', date_fact=today, qty_done=qty_done_total,
            )
        except Exception as e:
            app_logger.alert(f"Sheets full-done write error: {e}")
        msg = f"✅ {task['position']} — {task['element'] or ''}\nВыполнено полностью | {today}"

    else:
        import uuid as _uuid
        remaining = qty_total - qty_done_total
        new_row_id = str(_uuid.uuid4())

        # Считаем пропорции ДО UPDATE — иначе после quantity=qty_new деление сокращается
        _orig_qty = float(task['quantity'] or 1)
        _tw_remain = round(float(task['total_weight'] or 0) / _orig_qty * remaining, 3) if task['total_weight'] else None
        _ps_remain = round(float(task['payment_sum']  or 0) / _orig_qty * remaining, 2) if task['payment_sum']  else None

        # Текущая строка → ВЫПОЛНЕНО с qty_new шт
        db.execute(
            "UPDATE work_orders SET status='ВЫПОЛНЕНО', qty_done=%s, quantity=%s, date_fact=CURRENT_DATE WHERE id=%s",
            [qty_new, qty_new, task_id]
        )

        # Новая строка-остаток в БД (используем заранее вычисленные значения)
        db.execute(
            "INSERT INTO work_orders "
            "(project_name, file_id, sheet_name, row_num, row_id, "
            " position, element, quantity, unit_weight, total_weight, payment_sum, "
            " executor, date_plan, priority, mandatory, status, drawing_link, qty_holes) "
            "SELECT project_name, file_id, sheet_name, -%s, %s, "
            "       position, element, %s, unit_weight, %s, %s, "
            "       NULL, date_plan, priority, true, 'ПЛАН', drawing_link, qty_holes "
            "FROM work_orders WHERE id=%s",
            [task_id, new_row_id, remaining, _tw_remain, _ps_remain, task_id]
        )

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

    await update.message.reply_text(msg, reply_markup=back_to_tasks_kb())
    return ConversationHandler.END


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
        scan = _wiz.scan_and_recognize(folder_id)
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
        files_txt += f"  • {rec['file']['name']} → {', '.join(rec['specs'])}\n"
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
    text_lines = [f"📋 *{folder_name} — наряды*\n", "Данные для каждой вкладки:"]
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
                icon   = "✅" if active else "☐"
                suffix = " (из группы СБОРКА)" if stype == 'fallback' else ""
                text_lines.append(f"  {icon} {spec}  ←  {fname}{suffix}")
                kb_rows.append([InlineKeyboardButton(
                    f"{'✅' if active else '☐'} {spec}: {fname}",
                    callback_data=f"wiz_src_toggle:{si}:{ei}",
                )])

    text_lines.append(f"\nНазвание файла: *{folder_name} - наряды*")
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
        pos_map = _wiz.cut_pdf_drawings(drawings_id)
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
        linked = _wiz.link_drawings(sheet_id, drawings_id, active_specs)
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
        sheet_id = _wiz.create_spreadsheet(project_name, folder_id)
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
        _wiz.setup_sheet_tabs(sheet_id, active_specs)
    except Exception as e:
        app_logger.alert(f"wizard setup_sheet_tabs error: {e}")
        await query.message.edit_text(f"❌ Ошибка при создании вкладок:\n{e}")
        return ConversationHandler.END

    # ── Шаг 3: Служебные вкладки ──────────────────────────────────────────────
    si = 3
    await _upd(si, si)
    try:
        _wiz.setup_service_tabs(sheet_id, active_specs)
    except Exception as e:
        app_logger.alert(f"wizard setup_service_tabs error: {e}")
        await query.message.edit_text(f"❌ Ошибка при создании служебных вкладок:\n{e}")
        return ConversationHandler.END

    # ── Шаг 4: Зависимости ────────────────────────────────────────────────────
    si = 4
    await _upd(si, si)
    try:
        _wiz.fill_deps_sheet(sheet_id, active_specs)
    except Exception as e:
        app_logger.alert(f"wizard fill_deps_sheet error: {e}")

    # ── Шаг 5: Загрузка данных из Excel ───────────────────────────────────────
    si = 5
    await _upd(si, si)
    try:
        _ref_files = [item['file'] for item in context.user_data.get('wiz_recognized', [])
                      if 'СПРАВОЧНИК' in item.get('specs', [])]
        counts = _wiz.load_excel_data(sheet_id, source_map, reference_files=_ref_files)
    except Exception as e:
        app_logger.alert(f"wizard load_excel_data error: {e}")
        await query.message.edit_text(f"❌ Ошибка при загрузке данных:\n{e}")
        return ConversationHandler.END

    # ── Шаг 6: Выпадающие списки ──────────────────────────────────────────────
    si = 6
    await _upd(si, si)
    try:
        _wiz.add_data_validations(sheet_id, active_specs, counts)
    except Exception as e:
        app_logger.alert(f"wizard add_data_validations error: {e}")

    # Нарезка PDF и привязка чертежей — отложено, см. ветку feature/pdf-naryad
    cut_count  = 0
    link_count = 0
    # ── Последний шаг: активация (проект становится виден для sync) ────────────
    si = len(active_steps) - 1
    await _upd(si, si)
    try:
        _wiz.activate_project(project_id)
    except Exception as e:
        app_logger.alert(f"wizard activate_project error: {e}")
        await query.message.edit_text(f"❌ Ошибка при активации проекта:\n{e}")
        return ConversationHandler.END

    # Итоговое сообщение
    rows_txt = "\n".join(f"  • {spec}: {cnt} строк" for spec, cnt in counts.items()) if counts else "  (нет данных)"
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    drawings_txt = ""

    await query.message.edit_text(
        f"✅ *Проект создан!*\n\n"
        f"📁 {folder_name}\n"
        f"🔗 [Открыть таблицу]({sheet_url})\n\n"
        f"Загружено:\n{rows_txt}{drawings_txt}\n\n"
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

def main():
    app = Application.builder().token(config.TG_TOKEN).build()
    app_logger.set_bot(app.bot)

    partial_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r'^partial_ask:\d+$')],
        states={
            WAITING_PARTIAL_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_partial_qty)
            ],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        per_message=False,
    )

    block_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r'^block_confirm:\d+$')],
        states={
            WAITING_BLOCK_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_block_comment)
            ],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        per_message=False,
    )

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
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
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

    correction_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r'^correct:qty:\d+$')],
        states={
            WAITING_CORRECTION_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_correction_qty)
            ],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(partial_conv)
    app.add_handler(block_conv)
    app.add_handler(project_conv)
    app.add_handler(CallbackQueryHandler(wizard_cut_drawings, pattern=r'^wiz_cut_drawings$'))
    app.add_handler(earnings_conv)
    app.add_handler(correction_conv)
    app.add_handler(CallbackQueryHandler(on_callback))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        import traceback
        err = context.error
        tb = ''.join(traceback.format_exception(type(err), err, err.__traceback__))

        # Не шумим на "Message is not modified" — это безвредно
        if 'Message is not modified' in str(err):
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

    app.post_init = post_init
    app_logger.info(f"Bot started. TEST_MODE={config.TEST_MODE}")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
