import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager
from src import config

import threading
_pool = None
_pool_lock = threading.Lock()

def _make_pool():
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1, maxconn=5,
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER,
        password=config.DB_PASS,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
    )

def get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _make_pool()
    return _pool

@contextmanager
def get_conn():
    global _pool
    pool = get_pool()
    conn = pool.getconn()
    # Если соединение умерло (ночной таймаут PostgreSQL) — пересоздаём пул
    if conn.closed:
        with _pool_lock:
            try:
                pool.closeall()
            except Exception:
                pass
            _pool = _make_pool()
            conn = _pool.getconn()
    else:
        # Ping: проверяем живость соединения
        try:
            with conn.cursor() as _cur:
                _cur.execute("SELECT 1")
        except Exception:
            with _pool_lock:
                try:
                    pool.closeall()
                except Exception:
                    pass
                _pool = _make_pool()
                conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            _pool.putconn(conn)
        except Exception:
            pass

def fetchall(sql, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

def fetchone(sql, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

def execute(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

@contextmanager
def transaction():
    with get_conn() as conn:
        yield conn
