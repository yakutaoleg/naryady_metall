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

LOG_FILE = '/root/naryady/prod/logs/naryady.log'

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
    if action == 'sync_sheet' and result == 'success':
        return
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

_ALERT_LAST: dict = {}
_ALERT_DEDUP_SEC = 600


def _alert_http(text: str):
    """Отправка напрямую по HTTP — работает из любого потока и из отдельного процесса. 1 повтор при сбое."""
    import urllib.request, urllib.parse, os as _os, time as _t
    token = _os.environ.get('TG_TOKEN', '')
    if not token:
        from src import config as _cfg
        token = _cfg.TG_TOKEN
    payload = urllib.parse.urlencode({'chat_id': ADMIN_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=payload)
            urllib.request.urlopen(req, timeout=8)
            return
        except Exception as e:
            if attempt == 1:
                log.error(f"alert http failed: {e}")
            else:
                _t.sleep(2)


def alert(msg: str):
    """Логирует ошибку и отправляет уведомление администратору в Telegram.
    Одинаковые алерты не чаще раза в 10 минут. Из потока без event loop (синк в executor) — через HTTP."""
    import time as _t
    log.error(msg)
    _now = _t.time()
    _key = msg[:200]
    _last = _ALERT_LAST.get(_key)
    if _last and _now - _last < _ALERT_DEDUP_SEC:
        return
    _ALERT_LAST[_key] = _now
    text = f"⚠️ <b>Ошибка бота</b>\n\n<code>{msg[:800]}</code>"
    if _bot_instance is not None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            async def _send():
                for _attempt in range(2):
                    try:
                        await _bot_instance.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode='HTML')
                        return
                    except Exception as e:
                        if _attempt == 1:
                            log.error(f"alert send failed: {e}")
                        else:
                            await asyncio.sleep(2)
            asyncio.ensure_future(_send())
            return
    _alert_http(text)
