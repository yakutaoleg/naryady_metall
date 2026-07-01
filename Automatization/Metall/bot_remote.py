import asyncio
import logging
import sys
import os
import io
import re
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from src import config, db, logger as app_logger
from src.sheets import update_task_status
from google.oauth2.service_account import Credentials as _SACredentials
from googleapiclient.discovery import build as _gdrive_build

def _download_drawing(file_id: str) -> bytes:
    """Download a file from Google Drive as PDF bytes."""
    creds = _SACredentials.from_service_account_file(
        config.GOOGLE_SA_KEY,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    drive = _gdrive_build('drive', 'v3', credentials=creds)
    # If it's a Google Doc/Sheet — export as PDF; otherwise download directly
    meta = drive.files().get(fileId=file_id, fields='mimeType,name', supportsAllDrives=True).execute()
    if 'google-apps' in meta.get('mimeType', ''):
        request = drive.files().export_media(fileId=file_id, mimeType='application/pdf')
    else:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    from googleapiclient.http import MediaIoBaseDownload
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue(), meta.get('name', 'drawing.pdf')

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL))

WAITING_BLOCK_COMMENT = 1
WAITING_PROJECT_LINK  = 2
TODAY = lambda: date.today().strftime('%d.%m.%Y')


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_worker(tg_username: str):
    if not tg_username:
        return None
    return db.fetchone(
        "SELECT full_name, specialization, role FROM employees WHERE telegram_username=%s AND is_active=true",
        [tg_username]
    )


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


def get_earnings(period: str):
    if period == 'today':
        date_filter = "AND date_fact = CURRENT_DATE"
    elif period == 'week':
        date_filter = "AND date_fact >= CURRENT_DATE - INTERVAL '7 days'"
    else:  # month
        date_filter = "AND date_fact >= date_trunc('month', CURRENT_DATE)"
    return db.fetchall(
        f"""SELECT executor, COUNT(*) as tasks_count, SUM(payment_sum) as total
            FROM work_orders
            WHERE status='ВЫПОЛНЕНО' AND payment_sum IS NOT NULL
            {date_filter}
            GROUP BY executor
            ORDER BY total DESC NULLS LAST""",
        []
    )


def get_active_tasks(worker_name: str, specialization: str):
    tasks = db.fetchall(
        """SELECT id, project_name, sheet_name, file_id, row_num, position, element,
                  quantity, unit_weight, total_weight, payment_sum,
                  date_plan, priority, mandatory, status, drawing_link
           FROM work_orders
           WHERE executor=%s AND sheet_name=%s AND status='ПЛАН'
           ORDER BY mandatory DESC, priority ASC NULLS LAST, position""",
        [worker_name, specialization]
    )
    for t in tasks:
        if specialization.upper() == 'СБОРКА':
            t['deps_ready'] = _deps_ready(t['project_name'], t['element'])
        else:
            t['deps_ready'] = True
    return tasks


def _deps_ready(project_name: str, element: str) -> bool:
    """Возвращает True если все зависимости для элемента выполнены (или зависимостей нет)."""
    if not element:
        return True
    deps = db.fetchall(
        """SELECT requires_sheet, requires_position FROM element_dependencies
           WHERE project_name=%s AND element=%s""",
        [project_name, element]
    )
    if not deps:
        return True
    for dep in deps:
        row = db.fetchone(
            """SELECT status FROM work_orders
               WHERE project_name=%s AND sheet_name=%s AND position=%s
               LIMIT 1""",
            [project_name, dep['requires_sheet'], dep['requires_position']]
        )
        if not row or row['status'] != 'ВЫПОЛНЕНО':
            return False
    return True


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
            "SELECT id, telegram_username FROM employees WHERE full_name=%s AND is_active=true",
            [w['executor']]
        )
        if not emp or not emp['telegram_username']:
            continue
        try:
            chat = await bot.get_chat(f"@{emp['telegram_username']}")
            text = (
                f"✅ Детали готовы!\n\n"
                f"Элемент: {element}\n"
                f"Проект: {project_name}\n\n"
                f"Можно брать в сборку."
            )
            sent = await bot.send_message(chat_id=chat.id, text=text)
            db.execute(
                """INSERT INTO notifications (employee_id, type, message, telegram_message_id)
                   VALUES (%s, 'ASSEMBLY_READY', %s, %s)""",
                [emp['id'], text, sent.message_id]
            )
        except Exception as e:
            app_logger.error(f"notify_assembly error for {w['executor']}: {e}")


def get_blocked_tasks(worker_name: str, specialization: str):
    return db.fetchall(
        """SELECT id, position, element, quantity, comment
           FROM work_orders
           WHERE executor=%s AND sheet_name=%s AND status='БЛОК'
           ORDER BY position""",
        [worker_name, specialization]
    )


def get_done_today(worker_name: str, specialization: str):
    return db.fetchall(
        """SELECT id, position, element, quantity, payment_sum
           FROM work_orders
           WHERE executor=%s AND sheet_name=%s
             AND status='ВЫПОЛНЕНО' AND date_fact=CURRENT_DATE
           ORDER BY position""",
        [worker_name, specialization]
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


def mandatory_remaining(worker_name: str, specialization: str):
    rows = db.fetchall(
        """SELECT position FROM work_orders
           WHERE executor=%s AND sheet_name=%s AND mandatory=true AND status='ПЛАН'
           ORDER BY position""",
        [worker_name, specialization]
    )
    return [r['position'] for r in rows]


async def notify_masters(bot, task_id: int, worker_name: str, specialization: str,
                         position: str, element: str, comment: str):
    masters = db.fetchall(
        "SELECT id, telegram_username FROM employees WHERE role='master' AND is_active=true",
        []
    )
    for master in masters:
        if not master['telegram_username']:
            continue
        try:
            chat = await bot.get_chat(f"@{master['telegram_username']}")
            text = (
                f"\U0001f6ab Блокировка на позиции\n\n"
                f"Элемент: {element or '—'}\n"
                f"Позиция: {position}\n"
                f"Специализация: {specialization}\n"
                f"Рабочий: {worker_name}\n"
                f"Причина: {comment}\n"
                f"Время: {TODAY()}"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Снять блок", callback_data=f"unblock:{task_id}"),
            ]])
            sent = await bot.send_message(chat_id=chat.id, text=text, reply_markup=kb)
            db.execute(
                """INSERT INTO notifications (employee_id, type, work_order_id, message, telegram_message_id)
                   VALUES (%s, 'BLOCK_ALERT', %s, %s, %s)""",
                [master['id'], task_id, text, sent.message_id]
            )
        except Exception as e:
            app_logger.error(f"notify_masters error for @{master['telegram_username']}: {e}")


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def main_menu_kb(role: str = ''):
    rows = [
        [
            InlineKeyboardButton("📋 Задачи на сегодня", callback_data="tasks"),
            InlineKeyboardButton("✅ Выполнено сегодня", callback_data="done_today"),
        ],
        [InlineKeyboardButton("🔄 Обновить", callback_data="menu")],
    ]
    if is_master(role):
        rows.insert(1, [
            InlineKeyboardButton("🗂 Проекты", callback_data="projects"),
            InlineKeyboardButton("🚫 Блоки", callback_data="master:blocks"),
        ])
        rows.insert(2, [
            InlineKeyboardButton("📊 Выработка", callback_data="earnings:today"),
        ])
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
    for t in tasks:
        if not t.get('deps_ready', True):
            prefix = "⏳ "
        elif t['mandatory']:
            prefix = "❗ "
        elif mandatory_left:
            prefix = "🔒 "
        else:
            prefix = ""
        qty = t['quantity'] or '?'
        label = f"{prefix}{t['position']} — {t['element'] or ''} × {qty}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"task:{t['id']}")])

    for t in blocked:
        label = f"🚫 {t['position']} — {t['element'] or ''} (заблок.)"
        buttons.append([InlineKeyboardButton(label, callback_data="blocked_info")])

    buttons.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def task_detail_kb(task_id: int, has_drawing: bool = False):
    rows = []
    if has_drawing:
        rows.append([InlineKeyboardButton("📄 Чертёж", callback_data=f"drawing:{task_id}")])
    rows.append([
        InlineKeyboardButton("✅ ВЫПОЛНЕНО", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("🚫 БЛОК", callback_data=f"block_ask:{task_id}"),
    ])
    rows.append([InlineKeyboardButton("← К задачам", callback_data="tasks")])
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


def earnings_period_kb(active: str):
    periods = [('today', 'Сегодня'), ('week', 'Неделя'), ('month', 'Месяц')]
    row = []
    for p, label in periods:
        mark = "● " if p == active else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"earnings:{p}"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("← Главное меню", callback_data="menu")]])


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
        f"🔧 Специализация: {specialization}\n"
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


async def show_earnings(update: Update, period: str = 'today', edit: bool = False):
    rows = get_earnings(period)
    labels = {'today': 'сегодня', 'week': 'за 7 дней', 'month': 'за месяц'}
    period_label = labels.get(period, period)
    if not rows:
        text = f"📊 Выработка {period_label}\n\nДанных пока нет."
    else:
        total_all = sum(float(r['total'] or 0) for r in rows)
        lines = [f"📊 Выработка {period_label}\n"]
        for r in rows:
            total = float(r['total'] or 0)
            lines.append(f"👷 {r['executor']}\n  {r['tasks_count']} поз. — {total:.2f} руб")
        lines.append(f"\n💵 Итого: {total_all:.2f} руб")
        text = "\n".join(lines)
    kb = earnings_period_kb(period)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, reply_markup=kb)


async def show_tasks(update: Update, worker_name: str, specialization: str,
                     bot=None, chat_id=None):
    tasks   = get_active_tasks(worker_name, specialization)
    blocked = get_blocked_tasks(worker_name, specialization)
    mandatory_left = mandatory_remaining(worker_name, specialization)

    if not tasks and not blocked:
        text = f"🎉 Все задачи выполнены!\n📅 {TODAY()}"
        if bot and chat_id:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=back_to_menu_kb())
        else:
            await update.callback_query.edit_message_text(text, reply_markup=back_to_menu_kb())
        return

    mandatory_tasks = [t for t in tasks if t['mandatory']]
    optional_tasks  = [t for t in tasks if not t['mandatory']]

    lines = [f"📋 Задачи на сегодня | {specialization}", f"📅 {TODAY()}\n"]

    if mandatory_tasks:
        lines.append("❗ Обязательные:")
        for t in mandatory_tasks:
            lines.append(f"  • {t['position']} — {t['element'] or ''} × {t['quantity'] or '?'} шт")

    if optional_tasks:
        lock = " 🔒 (после обязательных)" if mandatory_left else ""
        lines.append(f"\nОстальные{lock}:")
        for t in optional_tasks:
            lines.append(f"  • {t['position']} — {t['element'] or ''} × {t['quantity'] or '?'} шт")

    if blocked:
        lines.append("\n🚫 Заблокированные (снимает руководитель):")
        for t in blocked:
            comment = f" — {t['comment']}" if t['comment'] else ""
            lines.append(f"  • {t['position']} — {t['element'] or ''}{comment}")

    lines.append("\n👇 Нажмите на задачу:")
    tasks_text = "\n".join(lines)
    tasks_kb   = tasks_list_kb(tasks, blocked, mandatory_left)
    if bot and chat_id:
        await bot.send_message(chat_id=chat_id, text=tasks_text, reply_markup=tasks_kb)
    else:
        await update.callback_query.edit_message_text(tasks_text, reply_markup=tasks_kb)


async def show_task_detail(update: Update, task: dict, specialization: str):
    tariff = get_tariff(specialization, task.get('unit_weight'))

    lines = []
    mandatory_mark = "❗ " if task['mandatory'] else ""
    lines.append(f"{mandatory_mark}[{task['sheet_name']}] {task['position']}")
    lines.append(f"Элемент: {task['element'] or '—'}")
    lines.append(f"Проект: {task['project_name']}")
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
        reply_markup=task_detail_kb(task['id'], has_drawing=bool(task.get('drawing_link')))
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
            lines.append(f"✅ {t['position']} — {t['element'] or ''} × {t['quantity'] or '?'} шт{pay}")
        if total > 0:
            lines.append(f"\n💵 Итого за день: {total:.2f} руб")
        text = "\n".join(lines)

    await update.callback_query.edit_message_text(text, reply_markup=back_to_menu_kb())


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.username)

    app_logger.audit('bot_start', user.id, user.username,
                     {'found': bool(worker)}, 'success' if worker else 'not_found')

    if not worker:
        await update.message.reply_text(
            f"Привет!\nВы не найдены в системе.\n"
            f"Ваш Telegram: @{user.username}\n"
            f"Обратитесь к руководителю для регистрации."
        )
        return

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
        worker = get_worker(user.username)
        if not worker:
            return None, None, None
        worker_name    = worker['full_name']
        specialization = worker['specialization']
        role           = worker.get('role', '')
        context.user_data['worker_name']    = worker_name
        context.user_data['specialization'] = specialization
        context.user_data['role']           = role
    return worker_name, specialization, role


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    worker_name, specialization, role = _get_worker_context(update, context)
    if not worker_name:
        await query.edit_message_text("Сессия устарела. Нажмите /start")
        return

    if data == "menu":
        await show_menu(update, worker_name, specialization, role, edit=True)

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
            "📁 Добавление проекта\n\n"
            "Отправьте ссылку на папку проекта в Google Drive.\n"
            "Папка должна содержать:\n"
            "• таблицу Google Sheets (наряды)\n"
            "• подпапку с чертежами (опционально)\n\n"
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
        lines = [
            f"🗂 {project['project_name']}\n",
            f"Статус: {'🟢 АКТИВНЫЙ' if project['status'] == 'АКТИВНЫЙ' else '📦 АРХИВ'}",
            f"Добавлен: {project['created_at'].strftime('%d.%m.%Y') if project['created_at'] else '—'}",
            f"🔄 Последняя синхронизация: {synced_str}",
        ]
        await query.edit_message_text(
            "\n".join(lines),
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
        try:
            import subprocess, sys
            subprocess.Popen(
                [sys.executable, '-m', 'src.sync'],
                cwd='/root/naryady/test',
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            await query.edit_message_text(
                f"🔄 Синхронизация запущена\n\n"
                f"📁 {project['project_name']}\n\n"
                f"Данные обновятся в течение минуты.",
                reply_markup=project_detail_kb(project_id, 'АКТИВНЫЙ')
            )
        except Exception as e:
            app_logger.error(f"sync trigger error: {e}")
            await query.answer("Ошибка запуска синхронизации.", show_alert=True)

    elif data.startswith("project:archive:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT project_name FROM projects WHERE id=%s", [project_id])
        db.execute("UPDATE projects SET status='АРХИВ' WHERE id=%s", [project_id])
        await query.answer(f"Проект «{project['project_name']}» перемещён в архив.")
        await show_projects(update, edit=True)

    elif data.startswith("project:restore:"):
        if not is_master(role):
            await query.answer("Доступ только для мастера.", show_alert=True)
            return
        project_id = int(data.split(":")[2])
        project = db.fetchone("SELECT project_name FROM projects WHERE id=%s", [project_id])
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
        period = data.split(":")[1]
        await show_earnings(update, period, edit=True)

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

        if specialization.upper() == 'СБОРКА' and not _deps_ready(task['project_name'], task['element']):
            await query.answer(
                f"⏳ Ожидает готовности деталей.\nЗавершите позиции ПЛАЗМА/ПИЛА по этому элементу.",
                show_alert=True
            )
            return

        if not task['mandatory']:
            mandatory_left = mandatory_remaining(worker_name, specialization)
            if mandatory_left:
                positions = ", ".join(mandatory_left)
                await query.answer(
                    f"Сначала выполните обязательные:\n{positions}",
                    show_alert=True
                )
                return

        app_logger.audit('view_task', user.id, user.username, {'task_id': task_id}, 'success')
        await show_task_detail(update, task, specialization)

    elif data.startswith("done:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone(
            "SELECT position, element, payment_sum, executor, file_id, sheet_name, row_num, project_name FROM work_orders WHERE id=%s",
            [task_id]
        )
        if not task or task['executor'] != worker_name:
            await query.edit_message_text("Ошибка доступа.", reply_markup=back_to_tasks_kb())
            return

        today_str = date.today().strftime('%d.%m.%Y')

        # 1. Обновляем PostgreSQL
        updated = db.execute(
            "UPDATE work_orders SET status='ВЫПОЛНЕНО', date_fact=CURRENT_DATE WHERE id=%s AND status='ПЛАН'",
            [task_id]
        )
        if not updated:
            await query.answer("Задача уже отмечена как выполненная.", show_alert=True)
            return

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
            app_logger.error(f"Sheets write error: {e}")

        app_logger.audit('set_done', user.id, user.username, {'task_id': task_id}, 'success')

        # 3. Проверяем не разблокировалась ли сборка по этому элементу
        if task.get('element') and task.get('sheet_name', '').upper() != 'СБОРКА':
            if _deps_ready(task['project_name'], task['element']):
                try:
                    await notify_assembly_workers(context.bot, task['project_name'], task['element'])
                except Exception as e:
                    app_logger.error(f"notify_assembly_workers error: {e}")

        # 4. Удаляем карточку, затем новым сообщением шлём подтверждение + список
        pay = f"\n💵 К оплате: {task['payment_sum']} руб" if task['payment_sum'] else ""
        await query.delete_message()
        await context.bot.send_message(
            chat_id=user.id,
            text=f"✅ {task['position']} — {task['element'] or ''}\nВыполнено | {TODAY()}{pay}"
        )
        await show_tasks(update, worker_name, specialization, bot=context.bot, chat_id=user.id)

    elif data.startswith("drawing:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone("SELECT position, element, drawing_link, executor FROM work_orders WHERE id=%s", [task_id])
        if not task or not task.get('drawing_link'):
            await query.answer("Чертёж не прикреплён.", show_alert=True)
            return
        worker_name, _, _ = _get_worker_context(update, context)
        if task['executor'] != worker_name:
            await query.answer("Нет доступа к этой задаче.", show_alert=True)
            return
        m = re.search(r'/d/([a-zA-Z0-9_-]+)', task['drawing_link'])
        if not m:
            await query.answer("Некорректная ссылка на чертёж.", show_alert=True)
            return
        await query.answer("Загружаю чертёж...")
        try:
            pdf_bytes, _ = await asyncio.get_event_loop().run_in_executor(
                None, _download_drawing, m.group(1)
            )
            name = f"{task['position']} — {task['element'] or ''}.pdf"
            await context.bot.send_document(
                chat_id=user.id,
                document=io.BytesIO(pdf_bytes),
                filename=name,
                caption=f"📄 {name}"
            )
            app_logger.audit('view_drawing', user.id, user.username, {'task_id': task_id}, 'success')
        except Exception as e:
            app_logger.error(f"Drawing download error: {e}")
            await context.bot.send_message(chat_id=user.id, text="Не удалось загрузить чертёж.")

    elif data.startswith("unblock:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone(
            "SELECT position, element, executor, sheet_name FROM work_orders WHERE id=%s AND status='БЛОК'",
            [task_id]
        )
        if not task:
            await query.answer("Задача уже разблокирована или не найдена.", show_alert=True)
            return

        # 1. Снимаем блок в БД
        updated = db.execute(
            "UPDATE work_orders SET status='ПЛАН', comment=NULL WHERE id=%s AND status='БЛОК'",
            [task_id]
        )
        if not updated:
            await query.answer("Блок уже снят другим мастером.", show_alert=True)
            return

        # 2. Редактируем сообщение мастера — убираем кнопку, меняем статус
        new_text = (
            f"✅ Блок снят\n\n"
            f"Элемент: {task['element'] or '—'}\n"
            f"Позиция: {task['position']}\n"
            f"Снял: мастер ({TODAY()})"
        )
        await query.edit_message_text(new_text)

        # 3. Уведомляем рабочего
        worker = db.fetchone(
            "SELECT telegram_username FROM employees WHERE full_name=%s AND is_active=true",
            [task['executor']]
        )
        if worker and worker['telegram_username']:
            try:
                worker_chat = await context.bot.get_chat(f"@{worker['telegram_username']}")
                await context.bot.send_message(
                    chat_id=worker_chat.id,
                    text=(
                        f"✅ Блокировка снята\n\n"
                        f"Позиция: {task['position']} — {task['element'] or ''}\n"
                        f"Можно продолжать работу."
                    ),
                    reply_markup=back_to_tasks_kb()
                )
            except Exception as e:
                app_logger.error(f"unblock notify worker error: {e}")

        app_logger.audit('unblock', user.id, user.username, {'task_id': task_id}, 'success')

        # Если остались ещё блоки — показываем обновлённый список
        remaining = get_all_blocked()
        if remaining and is_master(role):
            await query.answer("Блок снят.")

    elif data.startswith("block_ask:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone("SELECT position, element, executor FROM work_orders WHERE id=%s", [task_id])
        if task:
            worker_name, _, _ = _get_worker_context(update, context)
            if task['executor'] != worker_name:
                await query.answer("Нет доступа к этой задаче.", show_alert=True)
                return
        pos = task['position'] if task else f"#{task_id}"
        el  = task['element'] if task else ''
        await query.edit_message_text(
            f"Вы уверены, что хотите заблокировать задачу?\n\n"
            f"🚫 {pos} — {el}\n\n"
            f"Снять блокировку сможет только руководитель.",
            reply_markup=confirm_block_kb(task_id)
        )

    elif data.startswith("block_confirm:"):
        task_id = int(data.split(":")[1])
        context.user_data['block_task_id'] = task_id
        await query.edit_message_text(
            "🚫 Укажите причину блокировки:\n(напишите ответным сообщением)"
        )
        return WAITING_BLOCK_COMMENT


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
        app_logger.error(f"Sheets write error: {e}")

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
        f"🚫 {pos} — {el}\nЗаблокировано | {TODAY()}\nПричина: {comment}{pay}",
        reply_markup=back_to_tasks_kb()
    )
    return ConversationHandler.END


async def receive_project_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # Извлекаем folder_id из ссылки
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', text)
    if not m:
        await update.message.reply_text(
            "❌ Не удалось распознать ссылку на папку.\n"
            "Формат: https://drive.google.com/drive/folders/FOLDER_ID\n\n"
            "Попробуйте ещё раз или нажмите /cancel для отмены."
        )
        return WAITING_PROJECT_LINK

    folder_id = m.group(1)
    await update.message.reply_text("🔍 Сканирую папку...")

    try:
        result = scan_folder_and_register(folder_id, created_by=user.id)
    except Exception as e:
        app_logger.error(f"scan_folder error: {e}")
        await update.message.reply_text(
            f"❌ Ошибка при сканировании папки:\n{e}\n\nПроверьте доступ и попробуйте снова."
        )
        return WAITING_PROJECT_LINK

    if not result['ok']:
        await update.message.reply_text(
            f"❌ {result['error']}\n\nПопробуйте другую папку или /cancel для отмены."
        )
        return WAITING_PROJECT_LINK

    _, _, role = _get_worker_context(update, context)
    await update.message.reply_text(
        f"✅ Проект добавлен!\n\n"
        f"📁 {result['project_name']}\n\n"
        f"Данные появятся у рабочих после следующей синхронизации (до 15 мин).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗂 К проектам", callback_data="projects"),
            InlineKeyboardButton("← Меню", callback_data="menu"),
        ]])
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('block_task_id', None)
    await update.message.reply_text("Отменено.", reply_markup=back_to_menu_kb())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    app = Application.builder().token(config.TG_TOKEN).build()

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
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(block_conv)
    app.add_handler(project_conv)
    app.add_handler(CallbackQueryHandler(on_callback))

    app_logger.info(f"Bot started. TEST_MODE={config.TEST_MODE}")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
