import logging
import json
from datetime import datetime
from src import db

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

def audit(action: str, user_tg_id: int = None, username: str = None,
          details: dict = None, result: str = 'success', error_msg: str = None):
    details_json = json.dumps(details, ensure_ascii=False) if details else None
    log.info(f'AUDIT | user={username} | action={action} | result={result} | {details}')
    try:
        db.execute(
            '''INSERT INTO audit_log (user_tg_id, tg_username, action, details, result, error_msg)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s)''',
            (user_tg_id, username, action, details_json, result, error_msg)
        )
    except Exception as e:
        log.error(f'Failed to write audit log: {e}')

def info(msg): log.info(msg)
def error(msg): log.error(msg)
def warning(msg): log.warning(msg)
