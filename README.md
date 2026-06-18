# Наряды — Цех Металла

Система управления нарядами для производства металлоконструкций.

## Компоненты

- **sync.py** — синхронизация Google Sheets → PostgreSQL (cron, каждые 15 мин)
- **bot.py** — Telegram бот для рабочих (systemd сервис)

## Окружения

| Окружение | Путь | Ветка | БД |
|---|---|---|---|
| Test | /root/naryady/test/ | develop | naryady_test |
| Prod | /root/naryady/prod/ | main | naryady |

## Запуск тестов

```bash
cd /root/naryady/test
pytest tests/ -v
```

## Структура логов

Все действия пишутся в:
- `logs/naryady.log` — файл
- таблица `audit_log` в PostgreSQL
