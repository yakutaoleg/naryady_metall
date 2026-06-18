import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from src import config, db, logger as app_logger

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL))

WAITING_BLOCK_COMMENT = 1


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_worker(tg_username: str):
    if not tg_username:
        return None
    return db.fetchone(
        "SELECT full_name, specialization FROM employees WHERE telegram_username=%s AND is_active=true",
        [tg_username]
    )


def get_active_tasks(worker_name: str, specialization: str):
    return db.fetchall(
        """SELECT id, project_name, sheet_name, position, element,
                  quantity, unit_weight, total_weight, payment_sum,
                  date_plan, priority, mandatory, status, drawing_link
           FROM work_orders
           WHERE executor=%s AND sheet_name=%s AND status='ПЛАН'
           ORDER BY mandatory DESC, priority ASC NULLS LAST, position""",
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


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def main_menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Задачи в работу", callback_data="tasks"),
        InlineKeyboardButton("✅ Выполнено сегодня", callback_data="done_today"),
    ]])


def tasks_list_kb(tasks: list, mandatory_left: list):
    buttons = []
    for t in tasks:
        if t['mandatory']:
            prefix = "❗ "
        elif mandatory_left:
            prefix = "🔒 "
        else:
            prefix = ""
        qty = t['quantity'] or '?'
        label = f"{prefix}{t['position']} — {t['element'] or ''} × {qty}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"task:{t['id']}")])
    buttons.append([InlineKeyboardButton("← Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def task_detail_kb(task_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ВЫПОЛНЕНО", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("🚫 БЛОК", callback_data=f"block:{task_id}"),
    ], [
        InlineKeyboardButton("← К задачам", callback_data="tasks"),
    ]])


def back_to_tasks_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← К задачам", callback_data="tasks"),
    ]])


def back_to_menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Главное меню", callback_data="menu"),
    ]])


# ---------------------------------------------------------------------------
# Screen renderers
# ---------------------------------------------------------------------------

async def show_menu(update: Update, worker_name: str, specialization: str, edit: bool = False):
    text = f"👷 {worker_name}\n🔧 Специализация: {specialization}\n\nВыберите действие:"
    kb = main_menu_kb()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        msg = update.message or update.callback_query.message
        await msg.reply_text(text, reply_markup=kb)


async def show_tasks(update: Update, worker_name: str, specialization: str):
    tasks = get_active_tasks(worker_name, specialization)
    mandatory_left = mandatory_remaining(worker_name, specialization)

    if not tasks:
        text = "🎉 Все задачи выполнены! Новых задач нет."
        kb = back_to_menu_kb()
        await update.callback_query.edit_message_text(text, reply_markup=kb)
        return

    mandatory_tasks = [t for t in tasks if t['mandatory']]
    optional_tasks  = [t for t in tasks if not t['mandatory']]

    lines = [f"📋 Задачи | {specialization}\n"]

    if mandatory_tasks:
        lines.append("❗ Обязательные:")
        for t in mandatory_tasks:
            lines.append(f"  • {t['position']} — {t['element'] or ''} × {t['quantity'] or '?'} шт")

    if optional_tasks:
        lock = " 🔒 (после обязательных)" if mandatory_left else ""
        lines.append(f"\nОстальные{lock}:")
        for t in optional_tasks:
            lines.append(f"  • {t['position']} — {t['element'] or ''} × {t['quantity'] or '?'} шт")

    lines.append("\n👇 Нажмите на задачу:")
    await update.callback_query.edit_message_text(
        "\n".join(lines),
        reply_markup=tasks_list_kb(tasks, mandatory_left)
    )


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
        reply_markup=task_detail_kb(task['id'])
    )


async def show_done_today(update: Update, worker_name: str, specialization: str):
    tasks = get_done_today(worker_name, specialization)
    if not tasks:
        text = "За сегодня выполненных задач нет."
    else:
        total = sum(float(t['payment_sum'] or 0) for t in tasks)
        lines = [f"✅ Выполнено сегодня ({len(tasks)}):\n"]
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
    await show_menu(update, worker['full_name'], worker['specialization'])


def _get_worker_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return (worker_name, specialization) from context or DB."""
    worker_name    = context.user_data.get('worker_name')
    specialization = context.user_data.get('specialization')
    if not worker_name:
        user = update.effective_user
        worker = get_worker(user.username)
        if not worker:
            return None, None
        worker_name    = worker['full_name']
        specialization = worker['specialization']
        context.user_data['worker_name']    = worker_name
        context.user_data['specialization'] = specialization
    return worker_name, specialization


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    worker_name, specialization = _get_worker_context(update, context)
    if not worker_name:
        await query.edit_message_text("Сессия устарела. Нажмите /start")
        return

    if data == "menu":
        await show_menu(update, worker_name, specialization, edit=True)

    elif data == "tasks":
        await show_tasks(update, worker_name, specialization)

    elif data == "done_today":
        await show_done_today(update, worker_name, specialization)

    elif data.startswith("task:"):
        task_id = int(data.split(":")[1])
        task = db.fetchone("SELECT * FROM work_orders WHERE id=%s", [task_id])
        if not task:
            await query.edit_message_text("Задача не найдена.", reply_markup=back_to_tasks_kb())
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
        task = db.fetchone("SELECT position, executor FROM work_orders WHERE id=%s", [task_id])
        if not task or task['executor'] != worker_name:
            await query.edit_message_text("Ошибка доступа.", reply_markup=back_to_tasks_kb())
            return

        db.execute(
            "UPDATE work_orders SET status='ВЫПОЛНЕНО', date_fact=CURRENT_DATE WHERE id=%s AND status='ПЛАН'",
            [task_id]
        )
        app_logger.audit('set_done', user.id, user.username, {'task_id': task_id}, 'success')
        await show_tasks(update, worker_name, specialization)

    elif data.startswith("block:"):
        task_id = int(data.split(":")[1])
        context.user_data['block_task_id'] = task_id
        await query.edit_message_text(
            "🚫 БЛОК\n\nНапишите причину блокировки одним сообщением:"
        )
        return WAITING_BLOCK_COMMENT


async def receive_block_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker_name, specialization = _get_worker_context(update, context)
    task_id = context.user_data.pop('block_task_id', None)
    comment = update.message.text.strip()

    if not task_id:
        await update.message.reply_text("Ошибка: начните заново с /start")
        return ConversationHandler.END

    task = db.fetchone("SELECT position FROM work_orders WHERE id=%s", [task_id])
    db.execute(
        "UPDATE work_orders SET status='БЛОК', comment=%s WHERE id=%s AND status='ПЛАН'",
        [comment, task_id]
    )
    app_logger.audit('set_block', user.id, user.username,
                     {'task_id': task_id, 'comment': comment}, 'success')

    position = task['position'] if task else f"#{task_id}"
    await update.message.reply_text(
        f"🚫 {position} — заблокировано.\nПричина сохранена.",
        reply_markup=back_to_tasks_kb()
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

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r'^block:\d+$')],
        states={
            WAITING_BLOCK_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_block_comment)
            ],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))

    app_logger.info(f"Bot started. TEST_MODE={config.TEST_MODE}")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
