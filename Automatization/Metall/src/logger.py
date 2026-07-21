import logging
import json
import asyncio
from datetime import datetime
from src import db

ADMIN_CHAT_ID = 340620064
_bot_instance = None

def set_bot(bot):
    global _bot_instance
    _bot_instance = bot

LOG_FILE = '/root/naryady/test/logs/naryady.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('naryady')

def _resolve_full_name(user_tg_id: int = None, tg_username: str = None) -> str | None:
    try:
        if user_tg_id:
            row = db.fetchone(
                'SELECT full_name FROM employees WHERE telegram_id=%s LIMIT 1',
                [user_tg_id]
            )
            if row:
                return row['full_name']
        if tg_username:
            clean = tg_username.lstrip('@')
            row = db.fetchone(
                'SELECT full_name FROM employees WHERE telegram_username=%s LIMIT 1',
                [clean]
            )
            if row:
                return row['full_name']
        return None
    except Exception:
        return None

def audit(action: str, user_tg_id: int = None, username: str = None,
          details: dict = None, result: str = 'success', error_msg: str = None):
    details_json = json.dumps(details, ensure_ascii=False) if details else None
    full_name = _resolve_full_name(user_tg_id, username)
    log.info(f'AUDIT | user={full_name or username} | action={action} | result={result} | {details}')
    try:
        db.execute(
            '''INSERT INTO audit_log (user_tg_id, tg_username, username, action, details, result, error_msg)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)''',
            (user_tg_id, username, full_name, action, details_json, result, error_msg)
        )
    except Exception as e:
        log.error(f'Failed to write audit log: {e}')

def info(msg): log.info(msg)
def error(msg): log.error(msg)
def warning(msg): log.warning(msg)

def alert(msg: str):
    """Логирует ошибку и отправляет уведомление администратору в Telegram."""
    log.error(msg)
    if _bot_instance is None:
        return
    text = f"⚠️ <b>Ошибка бота</b>\n\n<code>{msg}</code>"
    async def _send():
        try:
            await _bot_instance.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode='HTML')
        except Exception as e:
            log.error(f"alert send failed: {e}")
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_send())
        else:
            loop.run_until_complete(_send())
    except Exception as e:
        log.error(f"alert dispatch failed: {e}")
