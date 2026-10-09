"""Пульс: лёгкая проверка «жив ли бот и идёт ли синк». Запуск каждые 10 минут (systemd-таймер naryady-heartbeat-prod).
Сообщает администратору только об изменениях: новая проблема, снятие проблемы, напоминание раз в 6 часов."""
import sys, os, json, time, shutil, subprocess, urllib.request, datetime
sys.path.insert(0, '/root/naryady/prod')
from src import db, config, logger

STATE = '/root/naryady/prod/state/heartbeat.json'
FAULT_LOG = '/root/naryady/prod/logs/faulthandler.log'
SYNC_MAX_AGE_MIN = 20          # синк идёт раз в 5 минут; 20 минут тишины = проблема
REMIND_SEC = 6 * 3600
DISK_MIN_FREE = 0.10


def sh(*a):
    return subprocess.run(list(a), capture_output=True, text=True).stdout.strip()


def collect(prev):
    """→ ({ключ: описание}, новое_состояние_для_сохранения)."""
    problems, keep = {}, {}

    if sh('systemctl', 'is-active', 'naryady-bot-prod') != 'active':
        problems['bot_down'] = 'Бот не запущен (naryady-bot-prod)'
    else:
        started = sh('systemctl', 'show', 'naryady-bot-prod', '-p', 'ActiveEnterTimestampMonotonic', '--value')
        keep['bot_started'] = started
        if prev.get('bot_started') and prev['bot_started'] != started:
            problems['bot_restarted'] = 'Бот перезапускался с прошлой проверки (падение или рестарт)'
        try:
            urllib.request.urlopen(f'https://api.telegram.org/bot{config.TG_TOKEN}/getMe', timeout=10)
        except Exception as e:
            problems['telegram_api'] = f'Telegram API недоступен: {str(e)[:80]}'

    try:
        row = db.fetchone("SELECT 1 AS ok", [])
        assert row and row['ok'] == 1
        r = db.fetchone("SELECT count(*) AS n, max(last_synced_at) AS mx, "
                        "EXTRACT(EPOCH FROM now() - max(last_synced_at))/60 AS age_min "
                        "FROM projects WHERE status='АКТИВНЫЙ'", [])
        if r['n'] and (r['age_min'] is None or r['age_min'] > SYNC_MAX_AGE_MIN):
            problems['sync_stale'] = (f"Синк не обновлял ни один проект {int(r['age_min'])} мин"
                                      if r['age_min'] is not None else 'Синк ни разу не обновлял проекты')
    except Exception as e:
        problems['db_down'] = f'База данных недоступна: {str(e)[:80]}'

    if sh('systemctl', 'is-failed', 'naryady-sync-prod.service') == 'failed':
        problems['sync_failed'] = 'Сервис синка завершился с ошибкой (naryady-sync-prod)'

    du = shutil.disk_usage('/')
    if du.free / du.total < DISK_MIN_FREE:
        problems['disk'] = f'Мало места на диске: свободно {du.free / du.total:.0%}'

    try:
        size = os.path.getsize(FAULT_LOG)
    except OSError:
        size = 0
    keep['fault_size'] = size
    if prev.get('fault_size') is not None and size > prev['fault_size']:
        problems['fault_log'] = 'В logs/faulthandler.log появилась новая запись (аварийное падение процесса)'
    return problems, keep


def main():
    try:
        prev = json.load(open(STATE, encoding='utf-8'))
    except Exception:
        prev = {}
    problems, keep = collect(prev)
    now = time.time()
    active_prev = prev.get('problems', {})          # {ключ: время последнего сообщения}
    new, remind, cleared = [], [], []
    state_problems = {}
    for k, txt in problems.items():
        last = active_prev.get(k)
        if last is None:
            new.append(txt)
            state_problems[k] = now
        elif now - last >= REMIND_SEC:
            remind.append(txt)
            state_problems[k] = now
        else:
            state_problems[k] = last
    for k in active_prev:
        if k not in problems:
            cleared.append(k)

    stamp = datetime.datetime.now().strftime('%d.%m %H:%M')
    lines = []
    if new:
        lines += ['🚨 <b>Пульс %s</b>' % stamp] + [f' • {t}' for t in new]
    if remind:
        lines += ['⏰ <b>Всё ещё не исправлено:</b>'] + [f' • {t}' for t in remind]
    if cleared and not problems:
        lines.append(f'✅ Пульс {stamp}: проблемы сняты')
    if lines:
        logger._alert_http('\n'.join(lines))
    print(stamp, 'проблем:', len(problems), '| новых:', len(new), '| снято:', len(cleared))

    keep['problems'] = state_problems
    tmp = STATE + '.tmp'
    json.dump(keep, open(tmp, 'w', encoding='utf-8'))
    os.replace(tmp, STATE)


if __name__ == '__main__':
    main()
