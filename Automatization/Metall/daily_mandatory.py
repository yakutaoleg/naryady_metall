# -*- coding: utf-8 -*-
"""
daily_mandatory.py — ставит mandatory=true на незакрытые задачи прошедших дней.
Запускается по cron в 20:00 MSK (18:00 UTC).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import db

def run():
    count = db.execute(
        """UPDATE work_orders
           SET mandatory = true
           WHERE status = 'ПЛАН'
             AND date_plan IS NOT NULL
             AND date_plan < CURRENT_DATE
             AND mandatory = false""",
        []
    )
    print(f"mandatory: updated rows")

if __name__ == '__main__':
    run()
