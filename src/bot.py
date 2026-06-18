import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
import sys
sys.path.insert(0, '/root/naryady/test')
from src import config, db, logger as app_logger

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL))

WAITING_COMMENT = 1


def get_worker_name(tg_username):
    if not tg_username:
        return None
    row = db.fetchone('SELECT full_name AS name FROM employees WHERE telegram_username = %s', [tg_username])
    return row['name'] if row else None


def get_today_tasks(worker_name):
    return db.fetchall('''
        SELECT id, project_name, sheet_name, position, element,
               quantity, date_plan, priority, mandatory, status
        FROM work_orders
        WHERE executor = %s AND status = 'ПЛАН'
        ORDER BY mandatory DESC, priority ASC NULLS LAST, sheet_name, position
        LIMIT 50
    ''', [worker_name])


def format_task(task, idx):
    mandatory = '!' if task['mandatory'] else ' '
    priority = 'P' + str(task['priority']) if task['priority'] else '  '
    date = str(task['date_plan']) if task['date_plan'] else '     '
    qty = str(task['quantity']) if task['quantity'] else '?'
    return (
        f"{idx}. [{mandatory}{priority}] [{task['sheet_name']}] "
        f"{task['position']} - {task['element'] or ''} x{qty} | {date}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    worker_name = get_worker_name(username)

    app_logger.audit(
        action='bot_start', user_tg_id=user.id, username=username,
        details={'worker_name': worker_name},
        result='success' if worker_name else 'not_found'
    )

    if not worker_name:
        await update.message.reply_text(
            'Привет! Вы не найдены в системе.\n'
            f'Ваш Telegram: @{username}\n'
            'Обратитесь к руководителю для регистрации.'
        )
        return

    tasks = get_today_tasks(worker_name)
    if not tasks:
        await update.message.reply_text(f'Привет, {worker_name}! На сегодня задач нет.')
        return

    lines = [f'Привет, {worker_name}! Задачи на сегодня:\n']
    for i, t in enumerate(tasks, 1):
        lines.append(format_task(t, i))
    lines.append('\nИспользуйте /task <номер> для обновления статуса.')
    await update.message.reply_text('\n'.join(lines))


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker_name = get_worker_name(user.username)

    if not worker_name:
        await update.message.reply_text('Вы не зарегистрированы в системе.')
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text('Использование: /task <номер>')
        return

    idx = int(args[0]) - 1
    tasks = get_today_tasks(worker_name)
    if idx < 0 or idx >= len(tasks):
        await update.message.reply_text(f'Задача #{args[0]} не найдена.')
        return

    task = tasks[idx]
    text = (
        f"Задача #{idx+1}: [{task['sheet_name']}] {task['position']} - {task['element']}\n"
        f"Проект: {task['project_name']}\nВыберите статус:"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('ВЫПОЛНЕНО', callback_data=f"done:{task['id']}"),
        InlineKeyboardButton('БЛОК', callback_data=f"block:{task['id']}"),
    ]])
    await update.message.reply_text(text, reply_markup=keyboard)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    worker_name = get_worker_name(user.username)
    action, task_id_str = query.data.split(':', 1)
    task_id = int(task_id_str)

    if action == 'done':
        db.execute(
            "UPDATE work_orders SET status='ВЫПОЛНЕНО', date_fact=NOW()::date WHERE id=%s AND executor=%s AND status='ПЛАН'",
            [task_id, worker_name]
        )
        app_logger.audit('set_done', user.id, user.username, {'task_id': task_id}, 'success')
        await query.edit_message_text(f'Задача #{task_id} отмечена ВЫПОЛНЕНО.')

    elif action == 'block':
        context.user_data['block_task_id'] = task_id
        await query.edit_message_text(f'Задача #{task_id} - БЛОК.\nНапишите причину:')
        return WAITING_COMMENT


async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker_name = get_worker_name(user.username)
    comment = update.message.text.strip()
    task_id = context.user_data.get('block_task_id')

    if not task_id:
        await update.message.reply_text('Ошибка: начните с /task.')
        return ConversationHandler.END

    db.execute(
        "UPDATE work_orders SET status='БЛОК', comment=%s WHERE id=%s AND executor=%s AND status='ПЛАН'",
        [comment, task_id, worker_name]
    )
    app_logger.audit('set_block', user.id, user.username, {'task_id': task_id, 'comment': comment}, 'success')
    await update.message.reply_text(f'Задача #{task_id} заблокирована. Причина сохранена.')
    context.user_data.pop('block_task_id', None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text('Отменено.')
    return ConversationHandler.END


def main():
    app = Application.builder().token(config.TG_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern='^block:')],
        states={WAITING_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment)]},
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('task', cmd_task))
    app.add_handler(CallbackQueryHandler(on_callback, pattern='^done:'))
    app.add_handler(conv)

    app_logger.info(f'Bot started (TEST_MODE={config.TEST_MODE})')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
